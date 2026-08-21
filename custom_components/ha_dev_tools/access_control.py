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
  created it. This is the 4-hour hard cap. dev_tools never rewrites
  content - if it could, a tool call could reset its own ceiling
  forever, which defeats the point of having one.
- Its *mtime* is the last-used time. dev_tools IS allowed to bump this
  (touch_armed(), called after every successful tool call) - that's
  what implements "stays armed while actually being used, up to 30
  minutes idle." Extending an already-granted session is safe; it can
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
from datetime import timedelta
from pathlib import Path
from typing import Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import llm
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, OPT_DRY_RUN
from .ws_call import resolve_user

_LOGGER = logging.getLogger(__name__)

# Relative to the HA config directory. Also present in const.py's
# DEFAULT_DENYLIST as defense in depth - even if some future tool ever
# mistakenly routed a write through FileManager/SecurityManager's
# general path for this exact file, that layer would refuse it too.
ARM_FILE_NAME = ".storage/ha_dev_tools.armed"

IDLE_TIMEOUT = timedelta(minutes=30)
MAX_SESSION = timedelta(hours=4)
CLEANUP_INTERVAL = timedelta(minutes=5)


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


def _is_expired(path: Path, *, now: float | None = None) -> bool:
    """True if the arm file is missing, idle-expired, or past its hard cap."""
    now = time.time() if now is None else now
    try:
        stat = path.stat()
    except FileNotFoundError:
        return True
    if now - stat.st_mtime > IDLE_TIMEOUT.total_seconds():
        return True
    armed_at = _read_armed_at(path)
    if armed_at is None or now - armed_at > MAX_SESSION.total_seconds():
        return True
    return False


def check_armed(hass: HomeAssistant) -> None:
    """Raise NotArmedError unless a human has armed dev_tools recently.

    Reads the arm file directly via pathlib - never through FileManager/
    SecurityManager's general read path - so this check can never be
    weakened by a bug or future change in that generalized machinery.
    """
    path = _arm_file_path(hass)
    if _is_expired(path):
        raise NotArmedError(
            f"dev_tools is not armed. Create {path} (e.g. via SSH or the "
            "Terminal add-on) with the current unix timestamp as its "
            "content to enable it for up to 4 hours (extended by 30 "
            "minutes on each use, idle windows beyond 30 minutes expire it)."
        )


def touch_armed(hass: HomeAssistant) -> None:
    """Extend the idle window by bumping the arm file's mtime only.

    Best-effort: called after a successful tool call, and a failure here
    must never break the call that triggered it.
    """
    path = _arm_file_path(hass)
    try:
        os.utime(path, None)  # None -> both atime and mtime set to now
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


def _cleanup_if_expired(hass: HomeAssistant) -> None:
    path = _arm_file_path(hass)
    if not path.exists():
        return
    if _is_expired(path):
        try:
            path.unlink()
            _LOGGER.info("Removed expired dev_tools arm file: %s", path)
        except OSError as err:
            _LOGGER.debug("Could not remove expired dev_tools arm file: %s", err)


def async_setup_cleanup(hass: HomeAssistant) -> Callable[[], None]:
    """Start best-effort periodic cleanup of an expired arm file.

    Not load-bearing for security (see module docstring) - runs once
    immediately so a Home Assistant restart doesn't leave an
    already-long-expired file sitting around just because a scheduled
    tick hadn't fired yet, then on CLEANUP_INTERVAL after that.
    """
    _cleanup_if_expired(hass)

    @callback
    def _tick(_now: object) -> None:
        _cleanup_if_expired(hass)

    return async_track_time_interval(hass, _tick, CLEANUP_INTERVAL)
