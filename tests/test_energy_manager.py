"""Tests for Energy dashboard config read/write (energy_manager.py),
against real HA energy prefs storage. Mirrors test_dashboard_manager.py's
structure - one difference: the `energy` component depends on
`recorder`/`history`, so this follows test_history_manager.py's
`recorder_mock` ordering pattern (applied via `@pytest.mark.usefixtures`,
never as a second `hass`-adjacent fixture, and `energy`/`websocket_api`
set up inside each test body rather than an autouse fixture - either
would force `hass` to instantiate before `recorder_mock`'s own ordering
assertion allows)."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools.energy_manager import (
    get_energy_config,
    write_energy_config,
)
from custom_components.ha_dev_tools.ws_call import WebSocketCommandError


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Shadow conftest.py's autouse fixture of the same name - see
    test_history_manager.py's identical fixture for the full reasoning
    (it depends on `hass`, which would otherwise instantiate ahead of
    `recorder_mock`)."""
    yield


async def _setup_energy(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "websocket_api", {})
    assert await async_setup_component(hass, "energy", {})


@pytest.fixture
async def admin_user(hass: HomeAssistant):
    return MockUser(is_owner=True).add_to_hass(hass)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_get_energy_config_raises_when_never_configured(
    hass: HomeAssistant, admin_user
):
    """A fresh instance has no energy prefs at all yet - not empty ones.
    Real behavior (EnergyManager.data is None), confirmed here rather
    than assumed."""
    await _setup_energy(hass)

    with pytest.raises(WebSocketCommandError) as exc_info:
        await get_energy_config(hass, admin_user)

    assert "no prefs" in str(exc_info.value).lower()


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_write_then_read_energy_config(hass: HomeAssistant, admin_user):
    await _setup_energy(hass)

    await write_energy_config(
        hass,
        admin_user,
        energy_sources=[
            {"type": "grid", "stat_energy_from": "sensor.grid_import", "cost_adjustment_day": 0},
        ],
    )
    read_back = await get_energy_config(hass, admin_user)

    assert read_back["energy_sources"][0]["type"] == "grid"


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_write_energy_config_omitted_field_leaves_section_untouched(
    hass: HomeAssistant, admin_user
):
    """Partial-update semantics: writing only device_consumption must not
    clear energy_sources set by an earlier call - matches HA's own
    EnergyManager.async_update (only keys present in the update are
    replaced)."""
    await _setup_energy(hass)

    await write_energy_config(
        hass,
        admin_user,
        energy_sources=[
            {"type": "grid", "stat_energy_from": "sensor.grid_import", "cost_adjustment_day": 0},
        ],
    )

    await write_energy_config(
        hass,
        admin_user,
        device_consumption=[{"stat_consumption": "sensor.dishwasher_energy"}],
    )
    read_back = await get_energy_config(hass, admin_user)

    assert read_back["energy_sources"][0]["type"] == "grid"
    assert (
        read_back["device_consumption"][0]["stat_consumption"]
        == "sensor.dishwasher_energy"
    )


# --- kwargs threading (mocked call_ws_command) ------------------------------------


@pytest.mark.asyncio
async def test_write_energy_config_only_passes_supplied_fields(
    hass: HomeAssistant, admin_user
):
    """None (omitted) fields must not reach the WS call's kwargs at all -
    energy/save_prefs treats an omitted key as "leave untouched", not as
    an explicit empty list. Mocked call_ws_command, so no real energy/
    recorder setup needed here."""
    with patch(
        "custom_components.ha_dev_tools.energy_manager.call_ws_command",
        new=AsyncMock(return_value={}),
    ) as mock_call:
        await write_energy_config(
            hass, admin_user, device_consumption=[{"stat_consumption": "sensor.x"}]
        )

    assert mock_call.call_args.kwargs == {
        "device_consumption": [{"stat_consumption": "sensor.x"}]
    }


@pytest.mark.asyncio
async def test_write_energy_config_passes_device_consumption_water(
    hass: HomeAssistant, admin_user
):
    """The third optional field must reach the WS call's kwargs too, same
    as energy_sources/device_consumption."""
    with patch(
        "custom_components.ha_dev_tools.energy_manager.call_ws_command",
        new=AsyncMock(return_value={}),
    ) as mock_call:
        await write_energy_config(
            hass,
            admin_user,
            device_consumption_water=[{"stat_consumption": "sensor.pool_water"}],
        )

    assert mock_call.call_args.kwargs == {
        "device_consumption_water": [{"stat_consumption": "sensor.pool_water"}]
    }


@pytest.mark.asyncio
async def test_write_energy_config_reraises_ws_errors(hass: HomeAssistant, admin_user):
    with patch(
        "custom_components.ha_dev_tools.energy_manager.call_ws_command",
        new=AsyncMock(
            side_effect=WebSocketCommandError("unknown_error", "Something broke")
        ),
    ):
        with pytest.raises(WebSocketCommandError, match="Something broke"):
            await write_energy_config(hass, admin_user, energy_sources=[])
