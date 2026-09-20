"""Energy dashboard config read/write - a separate HA subsystem from
Lovelace dashboards.

Issue #74: `get_dashboard`/`write_dashboard` (dashboard_manager.py) only
cover Lovelace (`lovelace/config`) - the Energy dashboard's own source
config (grid consumption/return entities, solar production per source,
battery in/out, gas/water sources, cost/compensation settings) lives in
`.storage/energy` and is read/written via its own websocket commands,
`energy/get_prefs`/`energy/save_prefs`, confirmed against
home-assistant/core's `homeassistant/components/energy/websocket_api.py`
(this repo's pinned HA version, 2026.8.2).

Unlike `lovelace/config/save` (a full-document replace),
`energy/save_prefs` is a genuine partial update - `EnergyManager.
async_update` (energy/data.py) only replaces the top-level keys actually
present in the call (`energy_sources`, `device_consumption`,
`device_consumption_water`), leaving any other key untouched. So
write_energy_config mirrors that: each field is optional, and omitting
one leaves that section as-is rather than clearing it.
"""

from __future__ import annotations

from typing import Any

from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant

from .ws_call import call_ws_command


async def get_energy_config(hass: HomeAssistant, user: User) -> dict[str, Any]:
    """Read the Energy dashboard's own source config (energy/get_prefs).

    Raises WebSocketCommandError (not_found) if the Energy dashboard has
    never been configured - real behavior of ws_get_prefs when
    EnergyManager.data is still None, not something this wrapper adds.
    """
    return await call_ws_command(hass, user, "energy/get_prefs")


async def write_energy_config(
    hass: HomeAssistant,
    user: User,
    *,
    energy_sources: list[dict[str, Any]] | None = None,
    device_consumption: list[dict[str, Any]] | None = None,
    device_consumption_water: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Update the Energy dashboard's source config (energy/save_prefs).

    Each field, if supplied, wholesale-replaces that section (not merged
    per-item within it) - matching HA's own EnergyManager.async_update
    semantics. Omitting a field leaves that section untouched. Returns
    the full resulting prefs, same shape as get_energy_config.
    """
    kwargs: dict[str, Any] = {}
    if energy_sources is not None:
        kwargs["energy_sources"] = energy_sources
    if device_consumption is not None:
        kwargs["device_consumption"] = device_consumption
    if device_consumption_water is not None:
        kwargs["device_consumption_water"] = device_consumption_water
    return await call_ws_command(hass, user, "energy/save_prefs", **kwargs)
