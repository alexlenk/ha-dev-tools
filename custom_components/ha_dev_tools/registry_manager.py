"""Entity and device registry updates for update_entities (issue #117).

Rename, friendly name, room (area), device_class ("show as" for e.g. a
binary_sensor), icon, disabled and hidden for entities; name, room and
disabled for devices; and "Show as" for a switch (a lamp relay as a light,
...). The same changes the UI's entity/device settings dialogs make - and
made the same way: through HA's own `config/entity_registry/update` and
`config/device_registry/update` WS commands (ws_call.py), so HA's own
validation and side effects apply unchanged (e.g. enabling an entity of a
disabled device is refused; enabling one schedules its integration's
reload). "Show as" runs HA's own `switch_as_x` config flow, which also
hides the original switch, exactly as the UI's "Show as" does.

Every item of a batch is checked before anything changes (plan_updates):
an unknown entity/device/room, a taken entity_id, a field that doesn't
apply, or the same target twice refuses the whole batch. Rooms are matched
exactly - by area id or by name, ignoring case - never by substring the
way find_entities searches, so a typo can't land an entity in the wrong
room; a room that doesn't exist yet is created first with create_helper
(domain "area").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.auth.models import User
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant, split_entity_id, valid_entity_id
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from . import entity_manager
from .derived_sensor_manager import _drive_flow
from .ws_call import call_ws_command

# The types the UI's "Show as" offers for a switch (switch_as_x).
SHOW_AS_DOMAINS = ("cover", "fan", "light", "lock", "siren", "valve")
ENTITY_FIELDS = (
    "new_entity_id",
    "name",
    "area",
    "device_class",
    "icon",
    "disabled",
    "hidden",
    "show_as",
)
DEVICE_FIELDS = ("name", "area", "disabled")


class RegistryUpdateError(Exception):
    """A batch that can't be applied - nothing was changed."""


@dataclass
class PlannedUpdate:
    """One validated item: the WS changes to send, and what they change."""

    kind: str  # "entity" or "device"
    target: str  # entity_id or device_id
    ws_changes: dict[str, Any] = field(default_factory=dict)
    show_as: str | None = None
    changes: dict[str, dict[str, Any]] = field(default_factory=dict)


def _resolve_room(area_reg: ar.AreaRegistry, room: str) -> str | None:
    """An area id for an area id or an exact (case-insensitive) name."""
    if area_reg.async_get_area(room) is not None:
        return room
    wanted = room.casefold()
    for area in area_reg.async_list_areas():
        if area.name.casefold() == wanted:
            return area.id
    return None


def _room_name(area_reg: ar.AreaRegistry, area_id: str | None) -> str | None:
    area = area_reg.async_get_area(area_id) if area_id else None
    return area.name if area else area_id


def _is_wrapped(hass: HomeAssistant, entity_id: str) -> bool:
    return any(
        entry.options.get("entity_id") == entity_id
        for entry in hass.config_entries.async_entries("switch_as_x")
    )


def plan_updates(
    hass: HomeAssistant, items: list[dict[str, Any]]
) -> list[PlannedUpdate]:
    """Validate a whole batch; raise RegistryUpdateError listing every
    problem, or return what each item would change."""
    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    problems: list[str] = []
    plans: list[PlannedUpdate] = []
    seen: set[str] = set()
    new_ids: set[str] = set()

    for index, item in enumerate(items):
        where = f"item {index + 1}"
        targets = [key for key in ("entity_id", "device_id") if key in item]
        if len(targets) != 1:
            problems.append(f"{where}: give exactly one of entity_id or device_id")
            continue
        kind = targets[0].removesuffix("_id")
        target = str(item[targets[0]])
        where = f"{where} ({target})"
        if target in seen:
            problems.append(f"{where}: listed more than once")
            continue
        seen.add(target)
        allowed = ENTITY_FIELDS if kind == "entity" else DEVICE_FIELDS
        fields = {key: value for key, value in item.items() if key != targets[0]}
        if unknown := sorted(set(fields) - set(allowed)):
            problems.append(
                f"{where}: {', '.join(unknown)} can't be set on a {kind} "
                f"(allowed: {', '.join(allowed)})"
            )
            continue
        if not fields:
            problems.append(f"{where}: nothing to change")
            continue

        plan = PlannedUpdate(kind=kind, target=target)
        if kind == "entity":
            entry = entity_reg.async_get(target)
            if entry is None:
                problems.append(
                    f"{where}: not in the entity registry (an entity without "
                    "a unique_id, e.g. YAML-defined, can't be changed here)"
                )
                continue
            current: dict[str, Any] = {
                "new_entity_id": entry.entity_id,
                "name": entry.name,
                "area": _room_name(area_reg, entry.area_id),
                "device_class": entry.device_class,
                "icon": entry.icon,
                "disabled": entry.disabled_by is not None,
                "hidden": entry.hidden_by is not None,
                "show_as": None,
            }
        else:
            device = device_reg.async_get(target)
            if device is None:
                problems.append(f"{where}: no such device")
                continue
            current = {
                "name": device.name_by_user,
                "area": _room_name(area_reg, device.area_id),
                "disabled": device.disabled_by is not None,
            }

        for key, value in fields.items():
            if key == "area":
                area_id = (
                    None if value in (None, "") else _resolve_room(area_reg, value)
                )
                if value not in (None, "") and area_id is None:
                    rooms = sorted(area.name for area in area_reg.async_list_areas())
                    problems.append(
                        f"{where}: no room '{value}' (rooms: "
                        f"{', '.join(rooms) or 'none yet'}) - create it with "
                        "create_helper, domain 'area'"
                    )
                    continue
                plan.ws_changes["area_id"] = area_id
                value = _room_name(area_reg, area_id)
            elif key == "new_entity_id":
                if not valid_entity_id(value) or split_entity_id(value)[0] != (
                    split_entity_id(target)[0]
                ):
                    problems.append(
                        f"{where}: new_entity_id '{value}' must be a valid "
                        f"entity_id in the same domain ({split_entity_id(target)[0]})"
                    )
                    continue
                if value != target and (
                    value in new_ids
                    or entity_reg.async_get(value) is not None
                    or hass.states.get(value) is not None
                ):
                    problems.append(f"{where}: '{value}' is already taken")
                    continue
                new_ids.add(value)
                plan.ws_changes["new_entity_id"] = value
            elif key == "show_as":
                if split_entity_id(target)[0] != "switch":
                    problems.append(f"{where}: show_as only applies to a switch")
                    continue
                if value not in SHOW_AS_DOMAINS:
                    problems.append(
                        f"{where}: show_as must be one of {', '.join(SHOW_AS_DOMAINS)}"
                    )
                    continue
                if "new_entity_id" in fields:
                    problems.append(
                        f"{where}: rename and show_as can't be combined - "
                        "do them as two calls"
                    )
                    continue
                if _is_wrapped(hass, target):
                    problems.append(f"{where}: already shown as another type")
                    continue
                plan.show_as = value
            elif key in ("disabled", "hidden"):
                if not isinstance(value, bool):
                    problems.append(f"{where}: {key} must be true or false")
                    continue
                plan.ws_changes[f"{key}_by"] = "user" if value else None
            elif kind == "device" and key == "name":
                plan.ws_changes["name_by_user"] = value
            else:  # name, device_class, icon: None clears the override
                plan.ws_changes[key] = value
            if current.get(key) != value:
                plan.changes[key] = {"from": current.get(key), "to": value}

        plans.append(plan)

    if problems:
        raise RegistryUpdateError("Nothing was changed: " + "; ".join(problems))
    return plans


def _device_snapshot(device: dr.DeviceEntry) -> dict[str, Any]:
    return {
        "device_id": device.id,
        "name": device.name,
        "name_by_user": device.name_by_user,
        "area_id": device.area_id,
        "disabled_by": device.disabled_by.value if device.disabled_by else None,
    }


def snapshot(hass: HomeAssistant, plan: PlannedUpdate, target: str) -> Any:
    """JSON-safe registry data for one item's target, for mirroring."""
    if plan.kind == "device":
        device = dr.async_get(hass).async_get(target)
        return _device_snapshot(device) if device else None
    entry = er.async_get(hass).async_get(target)
    return entity_manager.entity_registry_snapshot(entry) if entry else None


async def _show_as(hass: HomeAssistant, entity_id: str, domain: str) -> str | None:
    """Run HA's own switch_as_x flow; return the new entity's id."""
    flow = hass.config_entries.flow
    init = await flow.async_init("switch_as_x", context={"source": SOURCE_USER})
    result = await _drive_flow(
        init_result=init,
        configure=flow.async_configure,
        abort=flow.async_abort,
        steps={"user": {"entity_id": entity_id, "target_domain": domain}},
    )
    return er.async_get(hass).async_get_entity_id(
        domain, "switch_as_x", result["result"].entry_id
    )


async def apply_updates(
    hass: HomeAssistant, user: User, plans: list[PlannedUpdate]
) -> list[dict[str, Any]]:
    """Apply validated items in order; one result per item.

    plan_updates already checked everything it can, so a failure here is
    HA refusing something at write time (e.g. enabling an entity of a
    disabled device). That item reports the error and the rest aren't
    attempted - each is marked, so a partly applied batch is never silent.
    """
    results: list[dict[str, Any]] = []
    for index, plan in enumerate(plans):
        result: dict[str, Any] = {f"{plan.kind}_id": plan.target}
        try:
            if plan.ws_changes:
                command: dict[str, Any] = {f"{plan.kind}_id": plan.target}
                command.update(plan.ws_changes)
                response = await call_ws_command(
                    hass, user, f"config/{plan.kind}_registry/update", **command
                )
                for key in ("require_restart", "reload_delay"):
                    if key in response:
                        result[key] = response[key]
            if "new_entity_id" in plan.ws_changes:
                result["entity_id"] = plan.ws_changes["new_entity_id"]
            if plan.show_as:
                result["shown_as"] = await _show_as(hass, plan.target, plan.show_as)
        except Exception as exc:  # noqa: BLE001 - reported per item, see above
            result["error"] = str(exc)
            results.append(result)
            results.extend(
                {
                    f"{rest.kind}_id": rest.target,
                    "not_applied": "an earlier item failed",
                }
                for rest in plans[index + 1 :]
            )
            break
        result["changed"] = plan.changes
        results.append(result)
    return results
