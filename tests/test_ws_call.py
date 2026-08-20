"""Tests for in-process WebSocket API command invocation (ws_call.py).

Proves the loopback pattern actually works against real HA WS command
dispatch (schema validation, admin enforcement, async handler scheduling)
before any real tool is built on top of it - see docs/RESTART_PLAN.md's
Phase 4 note. Uses a toy command to prove the mechanics, then a real
`input_boolean` CRUD round-trip to prove genuine interop with the exact
class of component (helpers) this exists for.
"""
import voluptuous as vol
import pytest

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools.ws_call import WebSocketCommandError, call_ws_command


@pytest.fixture
async def admin_user(hass: HomeAssistant):
    """A real admin User, matching what a resolved MCP caller should be.

    User.is_admin is `is_owner or in the admin group` - is_owner=True is the
    simplest way to get a real admin User via MockUser, which doesn't take
    is_admin directly.
    """
    return MockUser(is_owner=True).add_to_hass(hass)


@pytest.fixture
async def non_admin_user(hass: HomeAssistant):
    return MockUser(is_owner=False).add_to_hass(hass)


@pytest.fixture(autouse=True)
async def setup_websocket_api(hass: HomeAssistant):
    assert await async_setup_component(hass, "websocket_api", {})


@websocket_api.websocket_command({vol.Required("type"): "test/echo", vol.Required("value"): str})
@websocket_api.async_response
async def _echo_command(hass, connection, msg):
    connection.send_result(msg["id"], {"echoed": msg["value"]})


@websocket_api.websocket_command({vol.Required("type"): "test/admin_only"})
@websocket_api.require_admin
@websocket_api.async_response
async def _admin_only_command(hass, connection, msg):
    connection.send_result(msg["id"], {"ok": True})


@pytest.mark.asyncio
async def test_call_ws_command_round_trip(hass: HomeAssistant, admin_user):
    websocket_api.async_register_command(hass, _echo_command)

    result = await call_ws_command(hass, admin_user, "test/echo", value="hello")

    assert result == {"echoed": "hello"}


@pytest.mark.asyncio
async def test_call_ws_command_unknown_command_raises(hass: HomeAssistant, admin_user):
    with pytest.raises(WebSocketCommandError) as exc_info:
        await call_ws_command(hass, admin_user, "test/does_not_exist")

    assert "unknown_command" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_call_ws_command_enforces_real_admin_check(
    hass: HomeAssistant, admin_user, non_admin_user
):
    websocket_api.async_register_command(hass, _admin_only_command)

    result = await call_ws_command(hass, admin_user, "test/admin_only")
    assert result == {"ok": True}

    with pytest.raises(WebSocketCommandError):
        await call_ws_command(hass, non_admin_user, "test/admin_only")


@pytest.mark.asyncio
async def test_call_ws_command_against_real_input_boolean_crud(hass: HomeAssistant, admin_user):
    """The actual use case: CRUD on a helper domain via its real WS commands."""
    assert await async_setup_component(hass, "input_boolean", {})

    created = await call_ws_command(
        hass, admin_user, "input_boolean/create", name="Test Helper"
    )
    assert created["name"] == "Test Helper"
    item_id = created["id"]

    listed = await call_ws_command(hass, admin_user, "input_boolean/list")
    assert any(item["id"] == item_id for item in listed)

    updated = await call_ws_command(
        hass, admin_user, "input_boolean/update", input_boolean_id=item_id, name="Renamed"
    )
    assert updated["name"] == "Renamed"

    await call_ws_command(hass, admin_user, "input_boolean/delete", input_boolean_id=item_id)

    listed_after = await call_ws_command(hass, admin_user, "input_boolean/list")
    assert not any(item["id"] == item_id for item in listed_after)
