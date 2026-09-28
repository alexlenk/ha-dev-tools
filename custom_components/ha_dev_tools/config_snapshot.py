"""Hand-edited config: snapshots to the mirror repo, and raw reads (issue #105).

The mirror (mirror.py) only ever saw files a write tool touched, so
configuration.yaml, whatever it `!include`s, packages edited by hand and
custom_templates/ had no versioned copy - exactly the config that's hardest
to rebuild. Two pieces close that gap:

- Snapshots: every hand-edited config file is committed to the mirror repo
  as-is (one commit per changed file) - at HA start, once a day, and on
  every check_config - but only while HA's own config check passes, so the
  mirror's latest copy is always a last-known-good one to recover from.
- get_config_file: a file's raw text, live or from the mirror, with its
  YAML tags (`!secret`, `!include`, ...) as written - so an agent can see
  and repair a block that no longer parses, where the structured get_*
  tools can't read the file at all.

Both release a file only if no line of it looks like a literal credential
(mirror_secrets.find_file_credentials - `!secret` references are fine),
and never a secrets.yaml, wherever it is (security.is_denylisted).

Which files: configuration.yaml and packages/**/*.yaml and
custom_templates/**/*.jinja where the read allowlist allows them, plus
every file an included file `!include`s / `!include_dir_*`s, followed the
way HA's own loader does (relative to the including file, hidden files and
folders skipped). An included file is part of the file that includes it -
HA reads it as such - so it's covered by that file's read permission rather
than needing its own allowlist entry; the denylist still applies to it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers.check_config import async_check_ha_config_file
from homeassistant.helpers.event import async_track_time_interval

from . import mirror, mirror_secrets
from .const import DOMAIN
from .security import SecurityManager

_LOGGER = logging.getLogger(__name__)

SNAPSHOT_INTERVAL = timedelta(days=1)
# A config file bigger than this is almost certainly not hand-written.
MAX_FILE_BYTES = 1_000_000

_INCLUDE = re.compile(
    r"!(include(?:_dir_(?:list|named|merge_list|merge_named))?)[ \t]+"
    r"(['\"]?)([^'\"\s#]+)\2"
)
_GLOBS = (("packages", "*.yaml"), ("custom_templates", "*.jinja"))
_YAML_SUFFIXES = (".yaml", ".yml")
# What `!include` can load: YAML, and JSON (a subset of YAML).
_INCLUDABLE_SUFFIXES = (*_YAML_SUFFIXES, ".json")


class ConfigFileError(Exception):
    """A get_config_file request that can't be served, with the reason."""


def discover_files(config_dir: str, security: SecurityManager) -> dict[str, str]:
    """Every hand-edited config file, as {relative path: why it's included}.

    Blocking (walks the config folder) - run in an executor.
    """
    root = Path(config_dir).resolve()
    found: dict[str, str] = {}

    def relative(path: Path) -> str | None:
        try:
            rel = path.resolve().relative_to(root)
        except ValueError:  # outside /config, e.g. through a symlink
            return None
        if any(part.startswith(".") for part in rel.parts):
            return None
        rel_path = rel.as_posix()
        return None if security.is_denylisted(f"/config/{rel_path}") else rel_path

    def add(path: Path, via: str) -> None:
        rel_path = relative(path)
        if rel_path is None or rel_path in found or not path.is_file():
            return
        found[rel_path] = via
        if path.suffix in _YAML_SUFFIXES:
            follow_includes(path, rel_path)

    def follow_includes(path: Path, rel_path: str) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for line in text.splitlines():
            for match in _INCLUDE.finditer(line.split("#", 1)[0]):
                tag, target = match.group(1), path.parent / match.group(3)
                via = f"!{tag} in {rel_path}"
                if tag == "include":
                    if target.suffix.lower() in _INCLUDABLE_SUFFIXES:
                        add(target, via)
                elif target.is_dir():
                    for item in sorted(target.rglob("*.yaml")):
                        add(item, via)

    # The allowlist is written as /config/... paths (security.py).
    if security.is_readable("/config/configuration.yaml"):
        add(root / "configuration.yaml", "configuration.yaml")
    for folder, pattern in _GLOBS:
        for item in sorted((root / folder).rglob(pattern)):
            rel_path = relative(item)
            if rel_path is not None and security.is_readable(f"/config/{rel_path}"):
                add(item, folder)
    return found


def _read(config_dir: str, rel_path: str) -> str:
    path = Path(config_dir) / rel_path
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ConfigFileError(
            f"'{rel_path}' is larger than {MAX_FILE_BYTES} bytes - not read."
        )
    return path.read_text(encoding="utf-8")


def _security(hass: HomeAssistant) -> SecurityManager:
    return hass.data[DOMAIN]["security_manager"]


def _normalize(path: str) -> str:
    path = path.strip().lstrip("/")
    return path[len("config/") :] if path.startswith("config/") else path


async def get_config_file(
    hass: HomeAssistant, path: str | None = None, source: str = "live"
) -> dict[str, Any]:
    """A hand-edited config file's raw text (`source` "live" or "mirror"),
    or with no `path`, the list of files this covers. Raises
    ConfigFileError when the file isn't covered, missing, or holds what
    looks like a literal credential."""
    config_dir = hass.config.config_dir
    files = await hass.async_add_executor_job(
        discover_files, config_dir, _security(hass)
    )
    if path is None:
        return {"files": [{"path": rel, "via": via} for rel, via in files.items()]}
    rel_path = _normalize(path)
    if rel_path not in files:
        raise ConfigFileError(
            f"'{rel_path}' isn't a hand-edited config file this tool covers "
            "(configuration.yaml, what it !includes, packages/, "
            "custom_templates/ - call without a path to list them)."
        )
    if source == "mirror":
        if not mirror.is_mirror_enabled(hass):
            raise ConfigFileError("Mirroring isn't set up - no mirrored copies.")
        try:
            content = await mirror.read_mirrored(hass, rel_path)
        except Exception as exc:  # noqa: BLE001 - any GitHub/network failure
            raise ConfigFileError(f"Reading the mirror failed: {exc}") from exc
        if content is None:
            raise ConfigFileError(f"The mirror has no copy of '{rel_path}' yet.")
    else:
        try:
            content = await hass.async_add_executor_job(_read, config_dir, rel_path)
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigFileError(f"Reading '{rel_path}' failed: {exc}") from exc
    findings = mirror_secrets.find_file_credentials(rel_path, content)
    if findings:
        raise ConfigFileError(
            f"'{rel_path}' is withheld: it looks like it holds a literal "
            f"credential ({', '.join(findings)}). Move it to secrets.yaml "
            "(`!secret`) and the file can be read."
        )
    return {
        "path": rel_path,
        "source": source,
        "via": files[rel_path],
        "content": content,
    }


def _lock(hass: HomeAssistant) -> asyncio.Lock:
    return hass.data[DOMAIN].setdefault("snapshot_lock", asyncio.Lock())


async def async_snapshot(hass: HomeAssistant) -> dict[str, Any]:
    """Commit every hand-edited config file that changed to the mirror repo.
    The caller checks the config is valid first (see the module docstring)."""
    async with _lock(hass):
        config_dir = hass.config.config_dir

        def collect() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
            readable, unreadable = [], []
            for rel_path in discover_files(config_dir, _security(hass)):
                try:
                    readable.append((rel_path, _read(config_dir, rel_path)))
                except (OSError, UnicodeDecodeError, ConfigFileError) as exc:
                    unreadable.append((rel_path, f"not read: {exc}"))
            return readable, unreadable

        files, unreadable = await hass.async_add_executor_job(collect)
        result = await mirror.mirror_snapshots(hass, files)
    return {
        "committed": list(result.committed),
        "unchanged": len(result.unchanged),
        "skipped": [
            {"path": path, "reason": reason}
            for path, reason in (*unreadable, *result.skipped)
        ],
    }


async def async_snapshot_if_valid(hass: HomeAssistant) -> dict[str, Any] | None:
    """Snapshot when mirroring is on and HA's config check passes; None if
    mirroring is off. A failing check is reported instead of snapshotted."""
    if not mirror.is_mirror_enabled(hass):
        return None
    check = await async_check_ha_config_file(hass)
    if check.errors:
        return {"skipped_all": "configuration check failed - kept last good copy"}
    return await async_snapshot(hass)


@callback
def async_setup_snapshots(hass: HomeAssistant) -> Callable[[], None]:
    """Snapshot at HA start (or now, if already running) and daily."""

    async def run(*_: Any) -> None:
        try:
            await async_snapshot_if_valid(hass)
        except Exception:  # noqa: BLE001 - a background job must not crash
            _LOGGER.exception("Config snapshot failed")

    unsubs: dict[str, Callable[[], None]] = {
        "daily": async_track_time_interval(hass, run, SNAPSHOT_INTERVAL)
    }

    @callback
    def start(*_: Any) -> None:
        unsubs.pop("started", None)  # a fired listen_once can't be removed
        hass.async_create_background_task(run(), "ha_dev_tools config snapshot")

    if hass.state is CoreState.running:
        start()
    else:
        unsubs["started"] = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, start
        )

    @callback
    def unsub() -> None:
        for cancel in unsubs.values():
            cancel()

    return unsub
