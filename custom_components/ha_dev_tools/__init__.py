"""
HA Dev Tools.

Registers a `dev_tools` LLM API into Home Assistant's own tool registry,
served over MCP by Home Assistant's native `mcp_server` integration - see
docs/ARCHITECTURE.md. Config-entry only; there is no configuration.yaml
setup path (see CHANGELOG's Removed entry for why).
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from . import access_control
from .automation_manager import AutomationManager
from .const import DOMAIN
from .file_manager import FileManager
from .llm_api import async_register as async_register_llm_api
from .log_manager import LogManager
from .security import SecurityManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HA Dev Tools from a config entry."""
    security_manager = SecurityManager(hass, entry.data.get("security", {}))
    file_manager = FileManager(hass, security_manager)
    log_manager = LogManager(hass, security_manager)
    automation_manager = AutomationManager(hass, file_manager)

    # Register the dev_tools LLM API, exposed over MCP by HA's native
    # mcp_server integration (no custom transport code needed here).
    unsub_llm_api = async_register_llm_api(
        hass, log_manager=log_manager, automation_manager=automation_manager
    )

    # Best-effort periodic cleanup of an expired access-control arm file
    # (custom_components/ha_dev_tools/access_control.py) - not load-bearing
    # for security, every gated tool call re-checks the file directly.
    unsub_arm_cleanup = access_control.async_setup_cleanup(hass)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["security_manager"] = security_manager
    hass.data[DOMAIN]["unsub_llm_api"] = unsub_llm_api
    hass.data[DOMAIN]["unsub_arm_cleanup"] = unsub_arm_cleanup

    _LOGGER.info("HA Dev Tools set up")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if DOMAIN in hass.data:
        unsub_llm_api = hass.data[DOMAIN].get("unsub_llm_api")
        if unsub_llm_api:
            unsub_llm_api()

        unsub_arm_cleanup = hass.data[DOMAIN].get("unsub_arm_cleanup")
        if unsub_arm_cleanup:
            unsub_arm_cleanup()

        hass.data.pop(DOMAIN)

    _LOGGER.info("HA Dev Tools unloaded")
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
