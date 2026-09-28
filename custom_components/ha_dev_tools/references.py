"""References to an entity_id, for update_entities' renames (issue #117).

Renaming an entity_id in the registry doesn't rename it anywhere else: an
automation, script, template or dashboard that names it just stops
working. HA itself only follows a rename in a few places - single-source
helpers (switch_as_x, utility_meter, derivative, ...) and the recorder -
so this finds the rest:

- YAML config: configuration.yaml, everything it includes, packages/ and
  custom_templates/ (config_snapshot.discover_files). Only files on the
  write allowlist (automations.yaml, scripts.yaml, packages/) can be
  rewritten; references elsewhere are listed for a manual edit.
- Storage-mode dashboards (a YAML-mode one is listed, never written).
- Persons' device_trackers.
- Config entries of UI helpers that name it (group, min_max, template,
  ...) - listed only; update_derived_sensor edits those.

A reference is the entity_id as a whole token: `sensor.power` matches in
`states('sensor.power')` and `states.sensor.power.state`, never inside
`sensor.power_total` or `binary_sensor.power`. Rewriting YAML is that
exact token replaced in the file's text - formatting, comments and quoting
stay byte-for-byte, and nothing but the id changes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant

from . import dashboard_manager, helper_manager
from .config_snapshot import discover_files
from .const import DOMAIN
from .file_manager import FileManager
from .security import SecurityManager
from .ws_call import WebSocketCommandError

# Domains whose YAML config a rewrite can change, reloaded afterwards.
RELOAD_DOMAINS = ("automation", "script", "scene", "template", "group")
_DEFAULT_DASHBOARD = "lovelace"  # the default dashboard's panel url_path


def pattern(entity_id: str) -> re.Pattern[str]:
    """`entity_id` as a whole token (see the module docstring)."""
    return re.compile(rf"(?<![\w]){re.escape(entity_id)}(?![\w])")


@dataclass
class Rewrite:
    """One rewritten file/dashboard's before/after, for mirroring."""

    path: str
    before: str
    after: str
    content_type: str


@dataclass
class RewriteResult:
    """What rewrite_references changed, and anything it couldn't."""

    rewritten: dict[str, list[Any]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    reloaded: list[str] = field(default_factory=list)
    mirror: list[Rewrite] = field(default_factory=list)


def _security(hass: HomeAssistant) -> SecurityManager:
    return hass.data[DOMAIN]["security_manager"]


def _yaml_references(
    config_dir: str, security: SecurityManager, entity_id: str
) -> list[dict[str, Any]]:
    """Blocking - run in an executor."""
    found = []
    regex = pattern(entity_id)
    for path in discover_files(config_dir, security):
        try:
            with open(f"{config_dir}/{path}", encoding="utf-8") as file:
                text = file.read()
        except (OSError, UnicodeDecodeError):
            continue
        lines = [
            number
            for number, line in enumerate(text.splitlines(), start=1)
            if regex.search(line)
        ]
        if lines:
            found.append(
                {
                    "path": path,
                    "lines": lines,
                    "writable": security.is_writable(f"/config/{path}"),
                }
            )
    return found


def _replace(node: Any, regex: re.Pattern[str], new: str) -> tuple[Any, int]:
    """`node` with every string value's references replaced, and the count."""
    if isinstance(node, str):
        return regex.subn(new, node)
    if isinstance(node, list):
        pairs = [_replace(item, regex, new) for item in node]
        return [item for item, _ in pairs], sum(count for _, count in pairs)
    if isinstance(node, dict):
        count = 0
        result = {}
        for key, value in node.items():
            result[key], found = _replace(value, regex, new)
            count += found
        return result, count
    return node, 0


async def _dashboards(
    hass: HomeAssistant, user: User, entity_id: str
) -> list[dict[str, Any]]:
    """Dashboards naming `entity_id`: url_path (None: default), mode, count
    and config."""
    found = []
    regex = pattern(entity_id)
    for dashboard in await dashboard_manager.list_dashboards(hass, user):
        url_path = dashboard["url_path"]
        url_path = None if url_path == _DEFAULT_DASHBOARD else url_path
        try:
            config = await dashboard_manager.get_dashboard(
                hass, user, url_path=url_path
            )
        except WebSocketCommandError:  # never saved: nothing to reference it
            continue
        _, count = _replace(config, regex, entity_id)
        if count:
            found.append(
                {
                    "url_path": url_path,
                    "mode": dashboard["mode"],
                    "count": count,
                    "config": config,
                }
            )
    return found


async def _persons(
    hass: HomeAssistant, user: User, entity_id: str
) -> list[dict[str, Any]]:
    if "person" not in hass.config.components:
        return []
    return [
        person
        for person in await helper_manager.list_helpers(hass, user, "person")
        if entity_id in person.get("device_trackers", [])
    ]


def _config_entries(hass: HomeAssistant, entity_id: str) -> list[dict[str, Any]]:
    regex = pattern(entity_id)
    return [
        {"entry_id": entry.entry_id, "domain": entry.domain, "title": entry.title}
        for entry in hass.config_entries.async_entries()
        if entry.domain != DOMAIN
        and regex.search(json.dumps([entry.data, entry.options], default=str))
    ]


async def find_references(
    hass: HomeAssistant, user: User, entity_id: str
) -> dict[str, list[dict[str, Any]]]:
    """Every reference to `entity_id` this can find (module docstring)."""
    yaml_refs = await hass.async_add_executor_job(
        _yaml_references, hass.config.config_dir, _security(hass), entity_id
    )
    return {
        "yaml": yaml_refs,
        "dashboards": [
            {key: value for key, value in dashboard.items() if key != "config"}
            for dashboard in await _dashboards(hass, user, entity_id)
        ],
        "persons": [
            {"id": person["id"], "name": person.get("name")}
            for person in await _persons(hass, user, entity_id)
        ],
        "config_entries": _config_entries(hass, entity_id),
    }


def _dashboard_storage_path(url_path: str | None) -> str:
    return f".storage/lovelace{f'.{url_path}' if url_path else ''}"


async def rewrite_references(
    hass: HomeAssistant, user: User, old: str, new: str
) -> RewriteResult:
    """Replace `old` with `new` wherever it can be written (see module
    docstring), then reload the YAML domains if a file changed. Each
    failure is reported, never raised - the rename itself already happened."""
    result = RewriteResult()
    regex = pattern(old)
    file_manager = FileManager(hass, _security(hass))

    yaml_refs = await hass.async_add_executor_job(
        _yaml_references, hass.config.config_dir, _security(hass), old
    )
    for ref in yaml_refs:
        if not ref["writable"]:
            continue
        try:
            before = await file_manager.read_file(ref["path"])
            after = regex.sub(new, before)
            await file_manager.write_file(ref["path"], after)
        except (OSError, PermissionError, ValueError, RuntimeError) as exc:
            result.errors.append(f"{ref['path']}: {exc}")
            continue
        result.rewritten.setdefault("yaml", []).append(ref["path"])
        result.mirror.append(Rewrite(ref["path"], before, after, "yaml"))

    for dashboard in await _dashboards(hass, user, old):
        if dashboard["mode"] != "storage":
            continue
        url_path = dashboard["url_path"]
        path = _dashboard_storage_path(url_path)
        config, _ = _replace(dashboard["config"], regex, new)
        try:
            await dashboard_manager.write_dashboard(
                hass, user, config, url_path=url_path
            )
        except (WebSocketCommandError, dashboard_manager.YamlModeDashboardError) as exc:
            result.errors.append(f"dashboard {url_path or 'default'}: {exc}")
            continue
        result.rewritten.setdefault("dashboards", []).append(url_path or "default")
        result.mirror.append(
            Rewrite(
                path,
                json.dumps(dashboard["config"], indent=2),
                json.dumps(config, indent=2),
                "json",
            )
        )

    for person in await _persons(hass, user, old):
        trackers = [
            new if tracker == old else tracker for tracker in person["device_trackers"]
        ]
        try:
            await helper_manager.update_helper(
                hass, user, "person", person["id"], {"device_trackers": trackers}
            )
        except WebSocketCommandError as exc:
            result.errors.append(f"person {person.get('name')}: {exc}")
            continue
        result.rewritten.setdefault("persons", []).append(person.get("name"))

    if result.rewritten.get("yaml"):
        for domain in RELOAD_DOMAINS:
            if hass.services.has_service(domain, "reload"):
                await hass.services.async_call(domain, "reload", blocking=True)
                result.reloaded.append(domain)
    return result
