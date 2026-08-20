"""Config validation and reload - never a full restart for automation changes.

`check_ha_config` wraps HA's own config-check helper (what `homeassistant.
check_config` / the UI's "Check configuration" button uses) with a
structured result instead of a plain joined string. `reload_domain` wraps
`<domain>.reload` service calls (automation/script/scene/input_boolean
etc. all support the pattern) - always prefer this over restarting.
"""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.check_config import async_check_ha_config_file


async def check_ha_config(hass: HomeAssistant) -> dict[str, Any]:
    """Validate the full HA configuration without writing or restarting anything."""
    result = await async_check_ha_config_file(hass)
    return {
        "valid": not result.errors,
        "errors": [
            {"message": err.message, "domain": err.domain} for err in result.errors
        ],
        "warnings": [
            {"message": warn.message, "domain": warn.domain} for warn in result.warnings
        ],
    }


async def reload_domain(hass: HomeAssistant, domain: str) -> dict[str, Any]:
    """Call `<domain>.reload` (e.g. automation, script, scene) instead of restarting."""
    if not hass.services.has_service(domain, "reload"):
        return {
            "reloaded": False,
            "error": f"Domain '{domain}' has no reload service",
        }
    await hass.services.async_call(domain, "reload", blocking=True)
    return {"reloaded": True, "domain": domain}
