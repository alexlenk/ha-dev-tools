"""Out-of-band presence gate + admin check for every dev_tools tool call.

Why this exists: dev_tools' tools include raw file read/write and
automation writes that can define shell_command/rest_command actions -
capability that in a stock Home Assistant install normally requires a
separate credential (SSH, the Terminal add-on) entirely apart from a
regular HA login/token. Home Assistant's own auth model has no concept
of a scoped-down token (confirmed by reading auth/models.py's
RefreshToken - no scope field at all), so any of a user's existing HA
tokens - a mobile app token, a browser session, a long-lived token
pasted into some other script years ago - is exactly as powerful as
every other token that user holds. Once dev_tools is reachable over
MCP, a leaked ordinary HA credential stops being merely bad and becomes
SSH-key-equivalent, without ever having been managed with that
severity in mind.

This module restores that separation with two independent checks, both
required before any tool's real logic runs:

- check_armed(): a human must have proven out-of-band filesystem access
  (SSH, the Terminal add-on - the same channel raw file editing already
  requires today) by creating an "arm file" outside dev_tools' own
  reach. A leaked HA token alone is not enough on its own to do
  anything here.
- require_admin(): the resolved calling user must be a real HA admin,
  checked here rather than trusted to mcp_server's own admin gate alone
  - that gate only covers the explicit /api/mcp/<api_id> URL; the bare
  /api/mcp endpoint serves whatever APIs a config entry lists with no
  admin check at all (verified by reading mcp_server/http.py).

The arm file's two timestamps carry different, deliberately asymmetric
meanings:
- Its *content* is the original arm time, written once by the human who
  created it. This is the hard cap (4 hours by default). dev_tools never rewrites
  content - if it could, a tool call could reset its own ceiling
  forever, which defeats the point of having one.
- Its *mtime* is the last-used time. dev_tools IS allowed to bump this
  (touch_armed(), called after every successful tool call) - that's
  what implements "stays armed while actually being used, up to 30
  minutes idle" (by default).

Both limits can be set in the integration's Configure page, within fixed
bounds (const.py: 5 minutes - 8 hours idle, 15 minutes - 24 hours cap,
idle never above the cap), so an arm always expires on its own. They're
read fresh on every check, like dry-run - a shorter setting applies to an
active arm at once. Extending an already-granted session is safe; it can
  only ever move toward expiry, never manufacture a fresh grant from
  nothing (only a human creating the file from scratch can do that).

The periodic cleanup task in this module is deliberately NOT what
enforces any of this - check_armed() always re-derives state fresh from
the file on disk, so a missed cleanup tick (e.g. Home Assistant
restarted before the next scheduled run) can never leave dev_tools
armed longer than intended. Cleanup exists purely so an expired file
doesn't linger physically on disk.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, Callable, cast

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import llm
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    ARM_IDLE_MINUTES_DEFAULT,
    ARM_IDLE_MINUTES_RANGE,
    ARM_MAX_MINUTES_DEFAULT,
    ARM_MAX_MINUTES_RANGE,
    DOMAIN,
    OPT_ARM_IDLE_MINUTES,
    OPT_ARM_MAX_MINUTES,
    OPT_DRY_RUN,
)
from .ws_call import resolve_user

_LOGGER = logging.getLogger(__name__)

# Relative to the HA config directory. Also present in const.py's
# DEFAULT_DENYLIST as defense in depth - even if some future tool ever
# mistakenly routed a write through FileManager/SecurityManager's
# general path for this exact file, that layer would refuse it too.
ARM_FILE_NAME = ".storage/ha_dev_tools.armed"

IDLE_TIMEOUT = timedelta(minutes=ARM_IDLE_MINUTES_DEFAULT)
MAX_SESSION = timedelta(minutes=ARM_MAX_MINUTES_DEFAULT)
CLEANUP_INTERVAL = timedelta(minutes=5)
# How far an arm time may lie ahead of this host's clock (see _is_expired).
CLOCK_SKEW = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class ArmLimits:
    """The arm window's limits in effect: idle window and hard cap."""

    idle: timedelta
    cap: timedelta


DEFAULT_LIMITS = ArmLimits(IDLE_TIMEOUT, MAX_SESSION)


def _bounded(value: object, default: int, bounds: tuple[int, int]) -> int:
    try:
        number = int(cast(Any, value))
    except (TypeError, ValueError):
        return default
    return min(max(number, bounds[0]), bounds[1])


def arm_limits(hass: HomeAssistant) -> ArmLimits:
    """The configured limits, read fresh - clamped to their bounds, and the
    idle window never above the cap, however the options got there."""
    entries = hass.config_entries.async_entries(DOMAIN)
    options = entries[0].options if entries else {}
    idle = _bounded(
        options.get(OPT_ARM_IDLE_MINUTES),
        ARM_IDLE_MINUTES_DEFAULT,
        ARM_IDLE_MINUTES_RANGE,
    )
    cap = _bounded(
        options.get(OPT_ARM_MAX_MINUTES), ARM_MAX_MINUTES_DEFAULT, ARM_MAX_MINUTES_RANGE
    )
    return ArmLimits(timedelta(minutes=min(idle, cap)), timedelta(minutes=cap))


def _duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes % 60:
        return f"{minutes} minutes"
    hours = minutes // 60
    return f"{hours} hour{'s' if hours != 1 else ''}"


class NotArmedError(Exception):
    """dev_tools is not currently armed, or its arm window has expired."""


class NotAdminError(Exception):
    """The resolved calling user is not a Home Assistant admin."""


def _arm_file_path(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(ARM_FILE_NAME))


def _read_armed_at(path: Path) -> float | None:
    """Parse the original arm timestamp from the file's content.

    Returns None if the file is empty, unreadable, or doesn't parse -
    treated as expired by every caller, never as "no cap" (fail closed).
    """
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _is_expired(
    path: Path, *, now: float | None = None, limits: ArmLimits = DEFAULT_LIMITS
) -> bool:
    """True if the arm file is missing, idle-expired, or past its hard cap."""
    now = time.time() if now is None else now
    try:
        stat = path.stat()
    except FileNotFoundError:
        return True
    if now - stat.st_mtime > limits.idle.total_seconds():
        return True
    armed_at = _read_armed_at(path)
    if armed_at is None or now - armed_at > limits.cap.total_seconds():
        return True
    # A future arm time would hold the hard cap off indefinitely (until
    # then plus 4 hours) - only a mistyped or deliberately forward-dated
    # file has one, so it fails closed like an unparseable one.
    return armed_at > now + CLOCK_SKEW.total_seconds()


def _armed_error_message(path: Path, limits: ArmLimits = DEFAULT_LIMITS) -> str | None:
    """Synchronous check, run only via the executor - see check_armed()."""
    if not _is_expired(path, limits=limits):
        return None
    idle = _duration(limits.idle)
    return (
        "dev_tools is not armed. Run this on the Home Assistant host "
        "(e.g. via SSH or the Terminal add-on), exactly as written:\n\n"
        f"    date +%s > {path}\n\n"
        f"That enables it for up to {_duration(limits.cap)} (extended by "
        f"{idle} on each use, idle windows beyond {idle} expire it)."
    )


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def _arm_status(
    path: Path, *, now: float | None = None, limits: ArmLimits = DEFAULT_LIMITS
) -> dict[str, Any]:
    """Synchronous arm-window status, run only via the executor - see arm_status()."""
    now = time.time() if now is None else now
    if _is_expired(path, now=now, limits=limits):
        return {"armed": False, "arm_command": f"date +%s > {path}"}
    idle_expires = path.stat().st_mtime + limits.idle.total_seconds()
    # _is_expired() already failed closed on an unparseable file, so a
    # still-armed file always has a readable arm timestamp here.
    hard_cap_expires = cast(float, _read_armed_at(path)) + limits.cap.total_seconds()
    expires = min(idle_expires, hard_cap_expires)
    return {
        "armed": True,
        "expires_at": _iso(expires),
        "minutes_left": int((expires - now) // 60),
        "idle_expires_at": _iso(idle_expires),
        "hard_cap_expires_at": _iso(hard_cap_expires),
        "note": (
            f"Each successful call extends the idle window by "
            f"{_duration(limits.idle)}, up to the hard cap. Re-arm on the "
            "Home Assistant host before a long batch that would outlast it."
        ),
    }


async def arm_status(hass: HomeAssistant) -> dict[str, Any]:
    """Whether dev_tools is armed, and when that expires (issue #63).

    Read-only, from the same file and rules check_armed() uses, so a caller
    can see expiry coming instead of finding out from a failed write
    mid-batch. Exposes nothing a caller couldn't already learn: a
    NotArmedError (returned to anyone who calls a gated tool, before the
    admin check) already carries the arm command.
    """
    path = _arm_file_path(hass)
    return await hass.async_add_executor_job(
        partial(_arm_status, path, limits=arm_limits(hass))
    )


async def check_armed(hass: HomeAssistant) -> None:
    """Raise NotArmedError unless a human has armed dev_tools recently.

    Reads the arm file directly via pathlib - never through FileManager/
    SecurityManager's general read path - so this check can never be
    weakened by a bug or future change in that generalized machinery.
    The file I/O runs in the executor so this never blocks the event loop.
    """
    path = _arm_file_path(hass)
    message = await hass.async_add_executor_job(
        _armed_error_message, path, arm_limits(hass)
    )
    if message is not None:
        raise NotArmedError(message)


async def touch_armed(hass: HomeAssistant) -> None:
    """Extend the idle window by bumping the arm file's mtime only.

    Best-effort: called after a successful tool call, and a failure here
    must never break the call that triggered it. Runs in the executor so
    this never blocks the event loop.
    """
    path = _arm_file_path(hass)
    try:
        # None -> both atime and mtime set to now
        await hass.async_add_executor_job(os.utime, path, None)
    except OSError as err:
        _LOGGER.debug("Could not extend dev_tools arm file: %s", err)


async def require_admin(hass: HomeAssistant, llm_context: llm.LLMContext) -> None:
    """Raise NotAdminError unless the resolved calling user is a real admin.

    Independent of mcp_server's own admin gate, which only covers the
    explicit /api/mcp/<api_id> URL - not the bare /api/mcp endpoint that
    serves a config entry's configured APIs with no admin check.
    """
    user = await resolve_user(hass, llm_context)
    if not user.is_admin:
        raise NotAdminError(
            f"User '{user.name}' is not a Home Assistant admin - dev_tools "
            "requires admin access."
        )


def is_dry_run(hass: HomeAssistant) -> bool:
    """True if this integration's dry-run option is currently enabled.

    Read fresh from the config entry's options on every call, the same
    "always re-derive, never cache" approach check_armed() uses for the
    arm file - toggling the option in the integration's Configure dialog
    takes effect on the very next tool call, no reload needed.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        return False
    return bool(entries[0].options.get(OPT_DRY_RUN, False))


def _cleanup_if_expired(
    hass: HomeAssistant, limits: ArmLimits = DEFAULT_LIMITS
) -> None:
    path = _arm_file_path(hass)
    if not path.exists():
        return
    if _is_expired(path, limits=limits):
        try:
            path.unlink()
            _LOGGER.info("Removed expired dev_tools arm file: %s", path)
        except OSError as err:
            _LOGGER.debug("Could not remove expired dev_tools arm file: %s", err)


async def async_setup_cleanup(hass: HomeAssistant) -> Callable[[], None]:
    """Start best-effort periodic cleanup of an expired arm file.

    Not load-bearing for security (see module docstring) - runs once
    immediately so a Home Assistant restart doesn't leave an
    already-long-expired file sitting around just because a scheduled
    tick hadn't fired yet, then on CLEANUP_INTERVAL after that. All file
    I/O runs in the executor so this never blocks the event loop.
    """
    await hass.async_add_executor_job(_cleanup_if_expired, hass, arm_limits(hass))

    @callback
    def _tick(_now: object) -> None:
        # Fire-and-forget: async_add_executor_job tracks the resulting
        # future itself, so this callback doesn't need to await it.
        hass.async_add_executor_job(_cleanup_if_expired, hass, arm_limits(hass))

    return async_track_time_interval(hass, _tick, CLEANUP_INTERVAL)
