"""Tests for dashboard read/write (dashboard_manager.py), against real HA lovelace."""
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools.dashboard_manager import get_dashboard, write_dashboard
from custom_components.ha_dev_tools.ws_call import WebSocketCommandError


@pytest.fixture(autouse=True)
async def setup_lovelace(hass: HomeAssistant):
    assert await async_setup_component(hass, "websocket_api", {})
    assert await async_setup_component(hass, "lovelace", {})


@pytest.fixture
async def admin_user(hass: HomeAssistant):
    return MockUser(is_owner=True).add_to_hass(hass)


@pytest.mark.asyncio
async def test_get_default_dashboard_raises_when_never_saved(hass: HomeAssistant, admin_user):
    """A fresh instance has no default dashboard config at all yet - not an
    empty one. Real behavior, confirmed here rather than assumed."""
    with pytest.raises(WebSocketCommandError) as exc_info:
        await get_dashboard(hass, admin_user)

    assert "not_found" in str(exc_info.value).lower() or "no config" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_write_then_read_default_dashboard(hass: HomeAssistant, admin_user):
    new_config = {"views": [{"title": "Test View", "cards": []}]}

    await write_dashboard(hass, admin_user, new_config)
    read_back = await get_dashboard(hass, admin_user, url_path=None)

    assert read_back["views"][0]["title"] == "Test View"
