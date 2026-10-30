"""`homeassistant: customize:` in packages (issue #183).

The only way to give an integration's entity a `state_class` (or a missing
`device_class`) - and a numeric sensor without one gets no long-term
statistics, so its history is gone after purge_keep_days. update_entities
can't (the registry has no state_class override), and the template and
platform-entry tools don't reach integration entities.

Per the owner's decisions on #183:
- only state_class, device_class, unit_of_measurement, friendly_name, icon
  and assumed_state can be set - others are refused;
- values are checked (state_class's three values and sensors only,
  device_class one of its domain's own); a combination HA won't use for
  statistics (device_class energy + state_class measurement, a unit the
  device class doesn't take) is a warning in the preview;
- packages only, and the package file is created when it doesn't exist;
- an entity customized in another file is refused (HA merges them
  silently, the later file winning);
- applied without a restart: HA's config check before and after the
  write (a new error rolls it back), homeassistant.reload_core_config,
  then each entity's state is rewritten (update_entity) and re-read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from io import StringIO
from pathlib import Path
from typing import Any

from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.components.button.const import ButtonDeviceClass
from homeassistant.components.cover.const import CoverDeviceClass
from homeassistant.components.event.const import EventDeviceClass
from homeassistant.components.humidifier.const import HumidifierDeviceClass
from homeassistant.components.media_player.const import MediaPlayerDeviceClass
from homeassistant.components.number.const import NumberDeviceClass
from homeassistant.components.sensor.const import (
    DEVICE_CLASS_STATE_CLASSES,
    DEVICE_CLASS_UNITS,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.components.switch.const import SwitchDeviceClass
from homeassistant.components.update.const import UpdateDeviceClass
from homeassistant.components.valve.const import ValveDeviceClass
from homeassistant.core import HomeAssistant, valid_entity_id
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.check_config import async_check_ha_config_file
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from .file_manager import FileManager, package_file
from .packages import new_package_problem
from .yaml_style import (
    merge_preserving_style,
    quote_ambiguous_scalars,
    spliced_or_full,
    surgical_edit,
    to_json_safe,
)

_LOGGER = logging.getLogger(__name__)

CUSTOMIZE_KEYS = (
    "state_class",
    "device_class",
    "unit_of_measurement",
    "friendly_name",
    "icon",
    "assumed_state",
)
DEVICE_CLASSES: dict[str, type[StrEnum]] = {
    "sensor": SensorDeviceClass,
    "binary_sensor": BinarySensorDeviceClass,
    "button": ButtonDeviceClass,
    "cover": CoverDeviceClass,
    "event": EventDeviceClass,
    "humidifier": HumidifierDeviceClass,
    "media_player": MediaPlayerDeviceClass,
    "number": NumberDeviceClass,
    "switch": SwitchDeviceClass,
    "update": UpdateDeviceClass,
    "valve": ValveDeviceClass,
}
CONFIG_FILE = "configuration.yaml"
HOMEASSISTANT = "homeassistant"
CUSTOMIZE = "customize"


class CustomizeError(ValueError):
    """A customize write that's refused - nothing changed."""


@dataclass(slots=True)
class CustomizePlan:
    file_path: str
    content_before: str | None  # None: the file is created
    content_after: str
    entities: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)

    def preview(self) -> dict[str, Any]:
        """What changes - each entity's entry before/after, not the file."""
        return {
            "file_path": self.file_path,
            "creates_file": self.content_before is None,
            "entities": self.entities,
            "warnings": self.warnings,
        }


def _new_yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _load(content: str) -> Any:
    return _new_yaml().load(content)


def _customize_of(document: Any) -> dict[str, Any] | None:
    """A document's `homeassistant: customize:` mapping, or None (absent,
    or not a mapping - an !include isn't followed)."""
    core = document.get(HOMEASSISTANT) if isinstance(document, dict) else None
    customize = core.get(CUSTOMIZE) if isinstance(core, dict) else None
    return customize if isinstance(customize, dict) else None


def check_value(entity_id: str, key: str, value: Any) -> str | None:
    """Why `value` can't be `key` for this entity, or None."""
    domain = entity_id.split(".", 1)[0]
    if key not in CUSTOMIZE_KEYS:
        return f"'{key}' can't be customized here (one of {', '.join(CUSTOMIZE_KEYS)})"
    if value is None:
        return None
    if key == "state_class":
        if domain != "sensor":
            return "state_class is for sensors only"
        if value not in [member.value for member in SensorStateClass]:
            return f"state_class is one of {[m.value for m in SensorStateClass]}"
    elif key == "device_class":
        classes = DEVICE_CLASSES.get(domain)
        if classes is None:
            return f"{domain} has no device classes"
        if value not in [member.value for member in classes]:
            return f"'{value}' isn't a {domain} device_class"
    elif key == "assumed_state":
        if not isinstance(value, bool):
            return "assumed_state is true or false"
    elif not isinstance(value, str) or not value:
        return f"{key} is a non-empty string"
    elif key == "icon":
        try:
            cv.icon(value)
        except Exception:  # noqa: BLE001 - voluptuous or probatio Invalid
            return f"icon is 'prefix:name' (e.g. mdi:water), not {value!r}"
    return None


def combination_warnings(entity_id: str, attributes: dict[str, Any]) -> list[str]:
    """What HA won't use for statistics in the effective attributes."""
    if entity_id.split(".", 1)[0] != "sensor":
        return []
    warnings = []
    device_class = attributes.get("device_class")
    state_class = attributes.get("state_class")
    unit = attributes.get("unit_of_measurement")
    try:
        sensor_class = SensorDeviceClass(device_class) if device_class else None
    except ValueError:
        sensor_class = None
    if sensor_class is not None and state_class:
        allowed = DEVICE_CLASS_STATE_CLASSES.get(sensor_class)
        if allowed is not None and SensorStateClass(state_class) not in allowed:
            warnings.append(
                f"{entity_id}: device_class {device_class} doesn't take state_class "
                f"{state_class} ({', '.join(sorted(c.value for c in allowed)) or 'none'})"
                " - HA logs it and compiles no statistics"
            )
    if sensor_class is not None and unit is not None:
        units = DEVICE_CLASS_UNITS.get(sensor_class)
        if units is not None and unit not in units:
            warnings.append(
                f"{entity_id}: device_class {device_class} doesn't take unit "
                f"'{unit}' - HA logs it and compiles no statistics"
            )
    return warnings


class CustomizeWriter:
    """Plans and applies customize entries in packages."""

    def __init__(self, hass: HomeAssistant, file_manager: FileManager) -> None:
        self.hass = hass
        self.file_manager = file_manager
        self._config_dir = Path(hass.config.config_dir)

    def _glob_packages(self) -> list[str]:
        packages = self._config_dir / "packages"
        if not packages.is_dir():
            return []
        return sorted(
            str(path.relative_to(self._config_dir)) for path in packages.rglob("*.yaml")
        )

    async def customized(self) -> dict[str, list[tuple[str, dict[str, Any]]]]:
        """{entity_id: [(file, its customize entry), ...]} across
        configuration.yaml and packages."""
        found: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        files = [CONFIG_FILE] + await self.hass.async_add_executor_job(
            self._glob_packages
        )
        for file_path in files:
            try:
                content = await self.file_manager.read_file(file_path)
            except (FileNotFoundError, PermissionError):
                continue
            document = await self.hass.async_add_executor_job(_load, content)
            for entity_id, entry in (_customize_of(document) or {}).items():
                found.setdefault(str(entity_id), []).append(
                    (file_path, dict(entry) if isinstance(entry, dict) else {})
                )
        return found

    async def plan(
        self, entities: dict[str, dict[str, Any]], *, package: str | None = None
    ) -> CustomizePlan:
        """Every check first - all problems at once - then the content.
        A None value removes that key; an entity left with none is removed."""
        if not entities:
            raise CustomizeError("entities is empty")
        problems: list[str] = []
        existing = await self.customized()
        target: str | None = package_file(package) if package is not None else None
        files: set[str] = set()
        for entity_id, attributes in entities.items():
            if not valid_entity_id(entity_id):
                problems.append(f"'{entity_id}' isn't an entity_id")
                continue
            if not isinstance(attributes, dict) or not attributes:
                problems.append(f"{entity_id}: give at least one attribute")
                continue
            for key, value in attributes.items():
                if problem := check_value(entity_id, key, value):
                    problems.append(f"{entity_id}: {problem}")
            where = [file_path for file_path, _ in existing.get(entity_id, [])]
            if len(where) > 1:
                problems.append(
                    f"{entity_id} is customized in {len(where)} files ({', '.join(where)})"
                    " - HA merges them; keep one"
                )
            elif where:
                files.add(where[0])
        if CONFIG_FILE in files:
            problems.append(
                "an entity is customized in configuration.yaml, which stays "
                "read-only - move its entry into a package first"
            )
        elif len(files) > 1 or (files and target is not None and target not in files):
            problems.append(
                f"these entities are customized in {', '.join(sorted(files))}"
                + (f", not {target}" if target and target not in files else "")
                + " - change them where they are (one file per call)"
            )
        file_path = target or (next(iter(files)) if files else None)
        if file_path is None and not problems:
            problems.append("no entity is customized yet - pass package to create them")
        if problems:
            raise CustomizeError("; ".join(problems))
        assert file_path is not None
        try:
            before: str | None = await self.file_manager.read_file(file_path)
        except FileNotFoundError:
            # A new package file (issue #189) - checked like every writer's.
            if problem := await new_package_problem(
                self.hass, self.file_manager, file_path
            ):
                raise CustomizeError(f"can't create '{file_path}': {problem}") from None
            before = None
        current = {
            entity_id: dict(entries[0][1])
            for entity_id, entries in existing.items()
            if entries[0][0] == file_path
        }
        after = before or ""
        report, warnings = [], []
        for entity_id, attributes in entities.items():
            old = current.get(entity_id, {})
            new = {
                key: value
                for key, value in {**old, **attributes}.items()
                if value is not None
            }
            removed = [key for key, value in attributes.items() if value is None]
            if missing := [key for key in removed if key not in old]:
                raise CustomizeError(
                    f"{entity_id}: {', '.join(missing)} isn't customized in {file_path}"
                )
            after = await self.hass.async_add_executor_job(
                _build, after, entity_id, new or None
            )
            state = self.hass.states.get(entity_id)
            effective = {**(dict(state.attributes) if state else {}), **new}
            for key in removed:
                effective.pop(key, None)
            if state is None:
                warnings.append(
                    f"{entity_id} doesn't exist now - customize applies once it does"
                )
            elif "state_class" in new and not _numeric(state.state):
                warnings.append(
                    f"{entity_id}'s state '{state.state}' isn't a number - a "
                    "state_class needs numeric states for statistics"
                )
            warnings += combination_warnings(entity_id, effective)
            report.append(
                {
                    "entity_id": entity_id,
                    "state": state.state if state else None,
                    "attributes_now": (
                        {key: state.attributes.get(key) for key in CUSTOMIZE_KEYS}
                        if state
                        else None
                    ),
                    "customize_before": to_json_safe(old) or None,
                    "customize_after": new or None,
                }
            )
        return CustomizePlan(file_path, before, after, report, warnings)

    async def apply(self, plan: CustomizePlan) -> dict[str, Any]:
        """Write, roll back on a new config error, reload core config and
        rewrite each entity's state; report what each one now has."""
        baseline = {error.message for error in (await self._check()).errors}
        await self.file_manager.write_file(
            plan.file_path, plan.content_after, validate_before_write=True
        )
        new_errors = [
            error.message
            for error in (await self._check()).errors
            if error.message not in baseline
        ]
        if new_errors:
            if plan.content_before is None:
                await self.file_manager.delete_file(plan.file_path)
            else:
                await self.file_manager.write_file(
                    plan.file_path, plan.content_before, validate_before_write=False
                )
            raise CustomizeError(
                "HA's config check failed after the write, so it was rolled "
                f"back: {'; '.join(new_errors)}"
            )
        await self.hass.services.async_call(
            HOMEASSISTANT, "reload_core_config", blocking=True
        )
        results = []
        for entry in plan.entities:
            entity_id = entry["entity_id"]
            result: dict[str, Any] = {"entity_id": entity_id}
            if self.hass.states.get(entity_id) is not None:
                try:
                    await self.hass.services.async_call(
                        HOMEASSISTANT,
                        "update_entity",
                        {"entity_id": entity_id},
                        blocking=True,
                    )
                except HomeAssistantError as err:
                    result["update_error"] = str(err)
            state = self.hass.states.get(entity_id)
            wanted = entry["customize_after"] or {}
            removed = [
                key for key in entry["customize_before"] or {} if key not in wanted
            ]
            now = (
                {key: state.attributes.get(key) for key in CUSTOMIZE_KEYS}
                if state
                else None
            )
            result["attributes_now"] = now
            result["applied"] = now is not None and all(
                now.get(key) == value for key, value in wanted.items()
            )
            if removed:
                result["removed"] = removed
            results.append(result)
        return {
            "file_path": plan.file_path,
            "created_file": plan.content_before is None,
            "reload": "homeassistant.reload_core_config",
            "entities": results,
            "note": (
                "statistics start from now - a new state_class doesn't "
                "backfill history. An entity not applied yet gets it at its "
                "next state update; a removed key stays until a restart"
            ),
        }

    async def _check(self) -> Any:
        return await async_check_ha_config_file(self.hass)


def _numeric(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _build(content: str, entity_id: str, attributes: dict[str, Any] | None) -> str:
    """Synchronous: `content` with the entity's customize entry set
    (attributes) or removed (None)."""
    yaml = _new_yaml()
    document = _load(content) if content.strip() else None
    spliceable = True
    if not isinstance(document, dict):
        document, spliceable = CommentedMap(), False
    core = document.get(HOMEASSISTANT)
    if not isinstance(core, dict):
        core, spliceable = CommentedMap(), False
        document[HOMEASSISTANT] = core
    customize = core.get(CUSTOMIZE)
    if not isinstance(customize, dict):
        customize, spliceable = CommentedMap(), False
        core[CUSTOMIZE] = customize
    source = content if spliceable else None
    prepared = quote_ambiguous_scalars(dict(attributes)) if attributes else None
    if prepared is None:
        if entity_id not in customize:
            return content
        # The last entry takes its empty customize: (and homeassistant:) along.
        if len(customize) > 1:
            container, key = customize, entity_id
        elif len(core) > 1:
            container, key = core, CUSTOMIZE
        else:
            container, key = document, HOMEASSISTANT

        def apply() -> None:
            del container[key]

        spliced = surgical_edit(source, container, key, "delete", yaml, apply)
    elif entity_id in customize:

        def apply() -> None:
            customize[entity_id] = merge_preserving_style(
                customize[entity_id], prepared
            )

        spliced = surgical_edit(source, customize, entity_id, "replace", yaml, apply)
    else:

        def apply() -> None:
            customize[entity_id] = prepared

        spliced = surgical_edit(source, customize, entity_id, "append", yaml, apply)
    if not document:
        # Nothing left but comments: the splice, or an empty file - a dump
        # would write `{}`.
        return spliced if spliced is not None and _load(spliced) is None else ""
    buffer = StringIO()
    yaml.dump(document, buffer)
    return spliced_or_full(spliced, buffer.getvalue(), _load)
