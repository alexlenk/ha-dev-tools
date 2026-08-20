"""Tests for config validation and reload (config_tools.py)."""
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools import config_tools

_MINIMAL_CONFIG = """
homeassistant:
  name: Test Home
  latitude: 32.87336
  longitude: 117.22743
  elevation: 430
  unit_system: metric
  time_zone: America/Los_Angeles
"""


@pytest.mark.asyncio
async def test_check_ha_config_valid_default(hass: HomeAssistant, tmp_path: Path):
    """A minimal valid config has no errors.

    Writes its own configuration.yaml into an isolated tmp_path rather than
    relying on hass.config.config_dir's ambient default - async_check_ha_
    config_file needs a real file to find, and the package's shared default
    testing_config directory isn't guaranteed untouched by whatever else ran
    earlier in a full-suite pytest process (same class of issue as the
    sys.modules leak fixed elsewhere in this suite).
    """
    (tmp_path / "configuration.yaml").write_text(_MINIMAL_CONFIG)
    hass.config.config_dir = str(tmp_path)

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
