"""Area/domain-scoped entity lookup.

Home Assistant instances commonly have hundreds of entities, most of them
irrelevant to any one task. `find_entities` resolves an area name (matching
HA's own fallback: an entity without its own area inherits its device's
area) plus optional domain/name filters, and reports live availability
alongside registry metadata - registries alone have no availability field
(see docs/RESTART_PLAN.md), only the state machine does.
"""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _resolve_area_id(area_reg: ar.AreaRegistry, area: str) -> str | None:
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


def _entity_area_id(
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
        area_id = _resolve_area_id(area_reg, area)
        if area_id is None:
            return {"entities": [], "truncated": False, "error": f"No area matching '{area}'"}

    matches: list[dict[str, Any]] = []
    truncated = False
    for entity in entity_reg.entities.values():
        if domain and entity.domain != domain:
            continue
        if not include_disabled and entity.disabled_by is not None:
            continue
        if area_id and _entity_area_id(entity, device_reg) != area_id:
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
                "area_id": _entity_area_id(entity, device_reg),
                "disabled": entity.disabled_by is not None,
                "hidden": entity.hidden_by is not None,
                "state": state.state if state else None,
            }
        )

    return {"entities": matches, "truncated": truncated}
