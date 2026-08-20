"""Tests for config validation and reload (config_tools.py)."""
from unittest.mock import AsyncMock

import pytest

from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools import config_tools


@pytest.mark.asyncio
async def test_check_ha_config_valid_default(hass: HomeAssistant):
    """A default test hass config has no errors."""
    result = await config_tools.check_ha_config(hass)

    assert result["valid"] is True
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_reload_domain_calls_service(hass: HomeAssistant):
    mock = AsyncMock()
    hass.services.async_register("automation", "reload", mock)

    result = await config_tools.reload_domain(hass, "automation")

    assert result == {"reloaded": True, "domain": "automation"}
    mock.assert_called_once()


@pytest.mark.asyncio
async def test_reload_domain_without_reload_service(hass: HomeAssistant):
    result = await config_tools.reload_domain(hass, "not_a_real_domain")

    assert result["reloaded"] is False
    assert "error" in result
