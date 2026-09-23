"""Tests for dashboard read/write (dashboard_manager.py), against real HA lovelace."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools.dashboard_manager import (
    YamlModeDashboardError,
    get_dashboard,
    list_dashboards,
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


# --- list_dashboards ---------------------------------------------------------
#
# list_dashboards goes through the frontend's own get_panels WS command
# (see dashboard_manager.py's docstring for why), which is only registered
# once the frontend component itself sets up - lovelace does NOT depend on
# frontend (it's the other way around: frontend depends on lovelace), so
# the module-level setup_lovelace fixture above isn't enough here. Every
# real Home Assistant install always has frontend loaded (it's required
# for the UI), but a bare websocket_api+lovelace test setup doesn't get it
# for free.


@pytest.fixture
async def setup_frontend(hass: HomeAssistant):
    assert await async_setup_component(hass, "frontend", {})


@pytest.mark.asyncio
async def test_list_dashboards_includes_default_panel(
    hass: HomeAssistant, admin_user, setup_frontend
):
    """A fresh instance always has at least the default "lovelace" panel
    registered (see _async_ensure_default_panel in home-assistant/core),
    even before any dashboard is ever explicitly created."""
    result = await list_dashboards(hass, admin_user)

    url_paths = {d["url_path"] for d in result}
    assert "lovelace" in url_paths


@pytest.mark.asyncio
async def test_list_dashboards_includes_newly_created_storage_dashboard(
    hass: HomeAssistant, admin_user, setup_frontend
):
    """A second, non-default storage-mode dashboard must show up too - this
    is exactly the gap issue #77 reported: a dashboard the caller doesn't
    already know the url_path of was otherwise unreachable."""
    from custom_components.ha_dev_tools.ws_call import call_ws_command

    await call_ws_command(
        hass,
        admin_user,
        "lovelace/dashboards/create",
        title="Solar",
        url_path="dashboard-solar",
        icon="mdi:solar-power",
    )

    result = await list_dashboards(hass, admin_user)

    solar = next(d for d in result if d["url_path"] == "dashboard-solar")
    assert solar["title"] == "Solar"
    assert solar["icon"] == "mdi:solar-power"
    assert solar["mode"] == "storage"
    assert solar["show_in_sidebar"] is True
    assert solar["require_admin"] is False


@pytest.mark.asyncio
async def test_list_dashboards_filters_out_non_lovelace_panels_and_maps_mode(
    hass: HomeAssistant, admin_user
):
    """get_panels returns every registered frontend panel, not just
    dashboards (e.g. "config", "developer-tools") - only component_name ==
    "lovelace" entries should be returned, with mode read from each panel's
    own config dict (this is how a YAML-mode dashboard is distinguished,
    since get_panels is the only place both modes show up together)."""
    fake_panels = {
        "lovelace": {
            "component_name": "lovelace",
            "title": "Overview",
            "icon": None,
            "config": {"mode": "storage"},
            "url_path": "lovelace",
            "require_admin": False,
            "show_in_sidebar": True,
        },
        "extra": {
            "component_name": "lovelace",
            "title": "Extra",
            "icon": "mdi:file",
            "config": {"mode": "yaml"},
            "url_path": "extra",
            "require_admin": True,
            "show_in_sidebar": False,
        },
        "config": {
            "component_name": "config",
            "title": "Settings",
            "icon": "mdi:cog",
            "config": None,
            "url_path": "config",
            "require_admin": True,
            "show_in_sidebar": True,
        },
    }
    with patch(
        "custom_components.ha_dev_tools.dashboard_manager.call_ws_command",
        new=AsyncMock(return_value=fake_panels),
    ):
        result = await list_dashboards(hass, admin_user)

    url_paths = {d["url_path"] for d in result}
    assert url_paths == {"lovelace", "extra"}
    extra = next(d for d in result if d["url_path"] == "extra")
    assert extra["mode"] == "yaml"
    assert extra["require_admin"] is True
    assert extra["show_in_sidebar"] is False
