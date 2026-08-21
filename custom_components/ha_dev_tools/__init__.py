"""
Home Assistant Management Integration.

This integration provides secure read-only REST API endpoints for configuration file
access and log retrieval in Home Assistant. It enables external development tools to
programmatically view Home Assistant configuration files and retrieve logs from the
core system through authenticated API calls.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from . import access_control
from .api import ManagementAPIHandler
from .automation_manager import AutomationManager
from .const import DOMAIN, CONFIG_SCHEMA
from .file_manager import FileManager
from .llm_api import async_register as async_register_llm_api
from .log_manager import LogManager
from .security import SecurityManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = []


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Home Assistant Management integration from configuration.yaml."""
    _LOGGER.info("Setting up Home Assistant Management Integration")
    
    # Extract ha_dev_tools configuration from config dict
    domain_config = config.get(DOMAIN, {})
    
    # Extract security configuration from domain config
    security_config = domain_config.get("security", {})
    
    # Handle missing configuration gracefully - log info if no config provided
    if not security_config:
        _LOGGER.info("No security configuration provided, using defaults")
    else:
        _LOGGER.info("Loading security configuration from configuration.yaml")
    
    # Initialize the security manager with configuration
    security_manager = SecurityManager(hass, security_config)
    
    # Initialize the API handler
    api_handler = ManagementAPIHandler(hass, security_manager)
    
    # Register API endpoints with Home Assistant's web component
    await api_handler.register_api_endpoints()
    
    # Store the API handler in hass.data for access by other components
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["api_handler"] = api_handler
    hass.data[DOMAIN]["security_manager"] = security_manager
    
    _LOGGER.info("Home Assistant Management Integration setup completed")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Home Assistant Management Integration from a config entry."""
    _LOGGER.info("Setting up Home Assistant Management Integration from config entry")
    
    # Extract configuration from config entry if available
    security_config = entry.data.get("security", {})
    
    # Handle missing configuration gracefully
    if not security_config:
        _LOGGER.info("No security configuration in config entry, using defaults")
    else:
        _LOGGER.info("Loading security configuration from config entry")
    
    # Initialize the security manager with configuration
    security_manager = SecurityManager(hass, security_config)
    
    # Initialize the API handler
    api_handler = ManagementAPIHandler(hass, security_manager)
    
    # Register API endpoints
    await api_handler.register_api_endpoints()

    # Register the dev_tools LLM API, exposed over MCP by HA's native
    # mcp_server integration (no custom transport code needed here).
    file_manager = FileManager(hass, security_manager)
    log_manager = LogManager(hass, security_manager)
    automation_manager = AutomationManager(hass, file_manager)
    unsub_llm_api = async_register_llm_api(
        hass, log_manager=log_manager, automation_manager=automation_manager
    )

    # Best-effort periodic cleanup of an expired access-control arm file
    # (custom_components/ha_dev_tools/access_control.py) - not load-bearing
    # for security, every gated tool call re-checks the file directly.
    unsub_arm_cleanup = access_control.async_setup_cleanup(hass)

    # Store components in hass.data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["api_handler"] = api_handler
    hass.data[DOMAIN]["security_manager"] = security_manager
    hass.data[DOMAIN]["unsub_llm_api"] = unsub_llm_api
    hass.data[DOMAIN]["unsub_arm_cleanup"] = unsub_arm_cleanup

    _LOGGER.info("Home Assistant Management Integration config entry setup completed")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading Home Assistant Management Integration")
    
    # Clean up API endpoints and resources
    if DOMAIN in hass.data:
        api_handler = hass.data[DOMAIN].get("api_handler")
        if api_handler:
            await api_handler.cleanup()

        unsub_llm_api = hass.data[DOMAIN].get("unsub_llm_api")
        if unsub_llm_api:
            unsub_llm_api()

        unsub_arm_cleanup = hass.data[DOMAIN].get("unsub_arm_cleanup")
        if unsub_arm_cleanup:
            unsub_arm_cleanup()

        # Remove from hass.data
        hass.data.pop(DOMAIN)
    
    _LOGGER.info("Home Assistant Management Integration unloaded successfully")
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)