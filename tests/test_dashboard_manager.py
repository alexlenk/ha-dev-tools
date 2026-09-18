"""Tests for dashboard read/write (dashboard_manager.py), against real HA lovelace."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools.dashboard_manager import (
    YamlModeDashboardError,
    get_dashboard,
    write_dashboard,
)
from custom_components.ha_dev_tools.ws_call import WebSocketCommandError


@pytest.fixture(autouse=True)
async def setup_lovelace(hass: HomeAssistant):
    assert await async_setup_component(hass, "websocket_api", {})
    assert await async_setup_component(hass, "lovelace", {})


@pytest.fixture
async def admin_user(hass: HomeAssistant):
    return MockUser(is_owner=True).add_to_hass(hass)


@pytest.mark.asyncio
async def test_get_default_dashboard_raises_when_never_saved(
    hass: HomeAssistant, admin_user
):
    """A fresh instance has no default dashboard config at all yet - not an
    empty one. Real behavior, confirmed here rather than assumed."""
    with pytest.raises(WebSocketCommandError) as exc_info:
        await get_dashboard(hass, admin_user)

    assert (
        "not_found" in str(exc_info.value).lower()
        or "no config" in str(exc_info.value).lower()
    )


@pytest.mark.asyncio
async def test_write_then_read_default_dashboard(hass: HomeAssistant, admin_user):
    new_config = {"views": [{"title": "Test View", "cards": []}]}

    await write_dashboard(hass, admin_user, new_config)
    read_back = await get_dashboard(hass, admin_user, url_path=None)

    assert read_back["views"][0]["title"] == "Test View"


# --- url_path threading (mocked call_ws_command) --------------------------------


@pytest.mark.asyncio
async def test_get_dashboard_passes_url_path_through(hass: HomeAssistant, admin_user):
    """A non-default dashboard's url_path must reach the WS call's kwargs."""
    with patch(
        "custom_components.ha_dev_tools.dashboard_manager.call_ws_command",
        new=AsyncMock(return_value={}),
    ) as mock_call:
        await get_dashboard(hass, admin_user, url_path="lovelace-second")

    assert mock_call.call_args.kwargs["url_path"] == "lovelace-second"


@pytest.mark.asyncio
async def test_write_dashboard_passes_url_path_through(hass: HomeAssistant, admin_user):
    """A non-default dashboard's url_path must reach the WS call's kwargs."""
    with patch(
        "custom_components.ha_dev_tools.dashboard_manager.call_ws_command",
        new=AsyncMock(return_value=None),
    ) as mock_call:
        await write_dashboard(
            hass, admin_user, {"views": []}, url_path="lovelace-second"
        )

    assert mock_call.call_args.kwargs["url_path"] == "lovelace-second"


# --- write_dashboard YAML-mode error translation ---------------------------------


@pytest.mark.asyncio
async def test_write_dashboard_yaml_mode_raises_clear_error(
    hass: HomeAssistant, admin_user
):
    """HA's 'Not supported' rejection for YAML-mode dashboards should be
    translated into the clearer, dedicated exception."""
    with patch(
        "custom_components.ha_dev_tools.dashboard_manager.call_ws_command",
        new=AsyncMock(
            side_effect=WebSocketCommandError("not_supported", "Not supported")
        ),
    ):
        with pytest.raises(YamlModeDashboardError, match="YAML mode"):
            await write_dashboard(hass, admin_user, {"views": []})


@pytest.mark.asyncio
async def test_write_dashboard_reraises_unrelated_ws_errors(
    hass: HomeAssistant, admin_user
):
    """Any other WebSocketCommandError should propagate unchanged, not get
    mistaken for the YAML-mode case."""
    with patch(
        "custom_components.ha_dev_tools.dashboard_manager.call_ws_command",
        new=AsyncMock(
            side_effect=WebSocketCommandError("unknown_error", "Something else broke")
        ),
    ):
        with pytest.raises(WebSocketCommandError, match="Something else broke"):
            await write_dashboard(hass, admin_user, {"views": []})
