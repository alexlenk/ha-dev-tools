"""Area/domain-scoped entity lookup and hygiene reporting.

Home Assistant instances commonly have hundreds of entities, most of them
irrelevant to any one task. `find_entities` resolves an area name (matching
HA's own fallback: an entity without its own area inherits its device's
area) plus optional domain/name filters, and reports live availability
alongside registry metadata - registries alone have no availability field
(see docs/ARCHITECTURE.md), only the state machine does.

`entity_health_report` uses the same registry-plus-live-state approach to
turn "hundreds of entities" into a scannable per-integration summary
instead of a wall of text - it reports problem entities (disabled,
unavailable, unknown, or registered with no state at all) rather than
listing everything.

`delete_entity` removes an entity from the registry via
`EntityRegistry.async_remove` - the same in-process API HA's own UI uses,
never a direct write to `.storage/core.entity_registry` (that file is
unconditionally denylisted - see security.py - and shared across every
integration, so hand-editing it is never on the table here). This is a
soft delete on HA's own side: it moves the entry into the registry's
`deleted_entities` table rather than erasing it, so if the same
(platform, unique_id) re-registers later (integration reload/restart)
HA reconnects it automatically with its old entity_id and customizations
- no restore needed for that common case. Only entries with no
config_entry_id (truly orphaned - the entity's owning integration is
already gone) get a 30-day countdown before HA purges them for good;
`entity_registry_snapshot` exists so a caller (see llm_api.py's
DeleteEntityTool) can mirror a durable backup of that data before it
ages out, using the exact field set HA's own `RegistryEntry.
as_storage_fragment` persists to storage, so a snapshot has everything a
future restore would need.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class EntityNotFoundError(Exception):
    """Raised when an entity_id doesn't resolve to a registry entry."""


def resolve_area_id(area_reg: ar.AreaRegistry, area: str) -> str | None:
    """Resolve an area name or id to an area id, case-insensitively."""
    if area_reg.async_get_area(area) is not None:
        return area
    exact = area_reg.async_get_area_by_name(area)
    if exact is not None:
        return exact.id
    area_lower = area.lower()
    for entry in area_reg.async_list_areas():
        if entry.name.lower() == area_lower:
            return entry.id
    for entry in area_reg.async_list_areas():
        if area_lower in entry.name.lower():
            return entry.id
    return None


def entity_area_id(
    entity: er.RegistryEntry, device_reg: dr.DeviceRegistry
) -> str | None:
    """The entity's own area, falling back to its device's area (HA's own resolution order)."""
    if entity.area_id:
        return entity.area_id
    if entity.device_id:
        device = device_reg.async_get(entity.device_id)
        if device:
            return device.area_id
    return None


def find_entities(
    hass: HomeAssistant,
    *,
    area: str | None = None,
    domain: str | None = None,
    name_search: str | None = None,
    include_disabled: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Find entities scoped by area/domain/name search.

    Returns {"entities": [...], "truncated": bool} rather than a bare list,
    so a caller that hits `limit` knows to narrow the search instead of
    silently getting a partial answer.
    """
    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    limit = max(1, min(limit, MAX_LIMIT))

    area_id: str | None = None
    if area:
        area_id = resolve_area_id(area_reg, area)
        if area_id is None:
            return {
                "entities": [],
                "truncated": False,
                "error": f"No area matching '{area}'",
            }

    matches: list[dict[str, Any]] = []
    truncated = False
    for entity in entity_reg.entities.values():
        if domain and entity.domain != domain:
            continue
        if not include_disabled and entity.disabled_by is not None:
            continue
        if area_id and entity_area_id(entity, device_reg) != area_id:
            continue
        display_name = entity.name or entity.original_name or entity.entity_id
        if name_search and name_search.lower() not in display_name.lower():
            continue

        if len(matches) >= limit:
            truncated = True
            break

        state = hass.states.get(entity.entity_id)
        matches.append(
            {
                "entity_id": entity.entity_id,
                "name": display_name,
                "area_id": entity_area_id(entity, device_reg),
                "disabled": entity.disabled_by is not None,
                "hidden": entity.hidden_by is not None,
                "state": state.state if state else None,
            }
        )

    return {"entities": matches, "truncated": truncated}


HEALTH_REPORT_DEFAULT_LIMIT = 100
HEALTH_REPORT_MAX_LIMIT = 500


def entity_health_report(
    hass: HomeAssistant,
    *,
    area: str | None = None,
    integration: str | None = None,
    limit: int = HEALTH_REPORT_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Summarize entity health per integration.

    Returns per-integration counts (total/disabled/hidden/unavailable/
    unknown/missing/ok) plus a capped list of the actual problem entities -
    never the full entity list. The point is turning "hundreds of
    entities" into something scannable, not dumping them all; use
    find_entities with include_disabled=True for a raw listing instead.

    "missing" means the entity is registered and enabled but has no
    current state at all - typically its owning integration failed to
    load or hasn't set it up yet, distinct from "unavailable" (integration
    loaded, but this specific entity reports itself as unavailable).
    """
    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    limit = max(1, min(limit, HEALTH_REPORT_MAX_LIMIT))

    area_id: str | None = None
    if area:
        area_id = resolve_area_id(area_reg, area)
        if area_id is None:
            return {
                "by_integration": {},
                "problem_entities": [],
                "truncated": False,
                "error": f"No area matching '{area}'",
            }

    by_integration: dict[str, dict[str, int]] = {}
    problems: list[dict[str, Any]] = []
    truncated = False

    for entity in entity_reg.entities.values():
        if integration and entity.platform != integration:
            continue
        if area_id and entity_area_id(entity, device_reg) != area_id:
            continue

        bucket = by_integration.setdefault(
            entity.platform,
            {
                "total": 0,
                "disabled": 0,
                "hidden": 0,
                "unavailable": 0,
                "unknown": 0,
                "missing": 0,
                "ok": 0,
            },
        )
        bucket["total"] += 1
        if entity.hidden_by is not None:
            bucket["hidden"] += 1

        if entity.disabled_by is not None:
            bucket["disabled"] += 1
            status = "disabled"
        else:
            state = hass.states.get(entity.entity_id)
            if state is None:
                status = "missing"
            elif state.state == "unavailable":
                status = "unavailable"
            elif state.state == "unknown":
                status = "unknown"
            else:
                status = "ok"
            bucket[status] += 1

        if status != "ok":
            if len(problems) >= limit:
                truncated = True
            else:
                problems.append(
                    {
                        "entity_id": entity.entity_id,
                        "integration": entity.platform,
                        "area_id": entity_area_id(entity, device_reg),
                        "status": status,
                    }
                )

    return {
        "by_integration": by_integration,
        "problem_entities": problems,
        "truncated": truncated,
    }


def _isoformat(value: datetime) -> str:
    return value.isoformat()


def entity_registry_snapshot(entry: er.RegistryEntry) -> dict[str, Any]:
    """JSON-safe snapshot of a registry entry, for mirroring before a delete.

    Field set matches what HA's own `RegistryEntry.as_storage_fragment`
    persists to `.storage/core.entity_registry` (confirmed by reading that
    property directly), plus `domain` (not in storage, but cheap and
    useful for a future restore's `async_get_or_create(domain, ...)`
    call) - so this snapshot has everything a real restore would need,
    not just what happens to be convenient to read here.
    """
    return {
        "entity_id": entry.entity_id,
        "unique_id": entry.unique_id,
        "previous_unique_id": entry.previous_unique_id,
        "platform": entry.platform,
        "domain": entry.domain,
        "aliases": list(entry.compat_aliases),
        "area_id": entry.area_id,
        "categories": dict(entry.categories),
        "capabilities": (
            dict(entry.capabilities) if entry.capabilities is not None else None
        ),
        "config_entry_id": entry.config_entry_id,
        "config_subentry_id": entry.config_subentry_id,
        "created_at": _isoformat(entry.created_at),
        "modified_at": _isoformat(entry.modified_at),
        "device_class": entry.device_class,
        "device_id": entry.device_id,
        "disabled_by": entry.disabled_by.value if entry.disabled_by else None,
        "entity_category": (
            entry.entity_category.value if entry.entity_category else None
        ),
        "has_entity_name": entry.has_entity_name,
        "hidden_by": entry.hidden_by.value if entry.hidden_by else None,
        "icon": entry.icon,
        "id": entry.id,
        "labels": sorted(entry.labels),
        "name": entry.name,
        "object_id_base": entry.object_id_base,
        "options": dict(entry.options),
        "original_device_class": entry.original_device_class,
        "original_icon": entry.original_icon,
        "original_name": entry.original_name,
        "suggested_object_id": entry.suggested_object_id,
        "supported_features": entry.supported_features,
        "translation_key": entry.translation_key,
        "unit_of_measurement": entry.unit_of_measurement,
    }


def delete_entity(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Soft-delete an entity from the entity registry by entity_id.

    Uses EntityRegistry.async_remove - see this module's docstring for
    why that's a soft delete on HA's own side, not a hard erase.
    """
    entity_reg = er.async_get(hass)
    if entity_reg.async_get(entity_id) is None:
        raise EntityNotFoundError(f"No entity with id '{entity_id}' found")
    entity_reg.async_remove(entity_id)
    return {"deleted": True, "entity_id": entity_id}


def delete_entities(hass: HomeAssistant, entity_ids: list[str]) -> dict[str, Any]:
    """Soft-delete multiple entities from the entity registry in one call.

    Validates every id resolves before removing any of them - a typo
    partway through a long list shouldn't silently delete everything up
    to that point and stop; the caller finds out up front and can fix
    the list, same "refuse to guess" philosophy as delete_automation's
    duplicate/not-found handling.
    """
    entity_reg = er.async_get(hass)
    missing = [eid for eid in entity_ids if entity_reg.async_get(eid) is None]
    if missing:
        raise EntityNotFoundError(f"No entity with id(s): {', '.join(missing)}")
    for entity_id in entity_ids:
        entity_reg.async_remove(entity_id)
    return {"deleted": True, "entity_ids": list(entity_ids)}
