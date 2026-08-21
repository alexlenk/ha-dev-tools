"""Tests for Supervisor add-on info/logs (supervisor_manager.py).

Unlike the WS-loopback-backed tools, there's no way to stand up a real
Supervisor in this test environment - hassio's own setup requires
actually running under Home Assistant OS/Supervised. These tests verify
our own logic (the not-available guard, and the shape built from a
client's response) against mocks, not a real Supervisor round-trip - an
actual end-to-end check needs a real Supervised/HA OS instance.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.hassio.handler import HassioAPIError
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools.supervisor_manager import (
    _HASSIO_DATA_KEY,
    SupervisorNotAvailableError,
    get_addon_logs,
    list_addons,
)


@pytest.mark.asyncio
async def test_list_addons_raises_when_not_supervised(hass: HomeAssistant):
    """A default test hass has no hassio set up - Core-only, same as most real installs."""
    with pytest.raises(SupervisorNotAvailableError):
        await list_addons(hass)


@pytest.mark.asyncio
async def test_get_addon_logs_raises_when_not_supervised(hass: HomeAssistant):
    with pytest.raises(SupervisorNotAvailableError):
        await get_addon_logs(hass, "core_mosquitto")


@pytest.mark.asyncio
async def test_list_addons_shapes_response(hass: HomeAssistant, monkeypatch):
    from aiohasupervisor.models.addons import AddonState

    hass.data[_HASSIO_DATA_KEY] = MagicMock()

    mock_addon = MagicMock()
    mock_addon.slug = "core_mosquitto"
    mock_addon.name = "Mosquitto broker"
    mock_addon.state = AddonState.STARTED
    mock_addon.version = "6.4.0"
    mock_addon.version_latest = "6.4.0"
    mock_addon.update_available = False

    mock_client = MagicMock()
    mock_client.addons.list = AsyncMock(return_value=[mock_addon])
    monkeypatch.setattr(
        "custom_components.ha_dev_tools.supervisor_manager.get_supervisor_client",
        lambda hass: mock_client,
    )

    result = await list_addons(hass)

    assert result == [
        {
            "slug": "core_mosquitto",
            "name": "Mosquitto broker",
            "state": "started",
            "version": "6.4.0",
            "version_latest": "6.4.0",
            "update_available": False,
        }
    ]


@pytest.mark.asyncio
async def test_get_addon_logs_returns_lines(hass: HomeAssistant):
    hass.data[_HASSIO_DATA_KEY] = MagicMock()
    hass.data[_HASSIO_DATA_KEY].send_command = AsyncMock(
        return_value="line one\nline two\nline three\n"
    )

    result = await get_addon_logs(hass, "core_mosquitto")

    assert result == {
        "slug": "core_mosquitto",
        "lines": ["line one", "line two", "line three"],
    }
    hass.data[_HASSIO_DATA_KEY].send_command.assert_called_once_with(
        "/addons/core_mosquitto/logs", method="get", return_text=True
    )


@pytest.mark.asyncio
async def test_get_addon_logs_respects_lines_limit(hass: HomeAssistant):
    hass.data[_HASSIO_DATA_KEY] = MagicMock()
    hass.data[_HASSIO_DATA_KEY].send_command = AsyncMock(
        return_value="\n".join(f"line {i}" for i in range(10))
    )

    result = await get_addon_logs(hass, "core_mosquitto", lines=3)

    assert result["lines"] == ["line 7", "line 8", "line 9"]


@pytest.mark.asyncio
async def test_get_addon_logs_surfaces_api_error(hass: HomeAssistant):
    hass.data[_HASSIO_DATA_KEY] = MagicMock()
    hass.data[_HASSIO_DATA_KEY].send_command = AsyncMock(
        side_effect=HassioAPIError("Addon does not exist")
    )

    result = await get_addon_logs(hass, "not_a_real_addon")

    assert result == {"error": "Addon does not exist"}
