"""Tests for the narrow, allowlisted service-call helpers (service_call_manager.py) - issue #76."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools.service_call_manager import (
    AutomationNotRunningError,
    EntityNotFoundError,
    InvalidEntityDomainError,
    set_boolean_value,
    set_number_value,
    trigger_automation,
)

# --- trigger_automation --------------------------------------------------------


@pytest.fixture
def mock_automation_trigger(hass: HomeAssistant):
    mock = AsyncMock()
    hass.services.async_register("automation", "trigger", mock)
    return mock


@pytest.mark.asyncio
async def test_trigger_automation_calls_service_with_resolved_entity_id(
    hass: HomeAssistant, mock_automation_trigger
):
    """automation_id is the config `id:` field, not the entity_id -
    resolving it via the live automation.* entity's `id` attribute is the
    only reliable way (see audit_manager.find_automation_state)."""
    hass.states.async_set("automation.kitchen_lights", "on", {"id": "kitchen_id"})

    entity_id = await trigger_automation(hass, "kitchen_id")

    assert entity_id == "automation.kitchen_lights"
    mock_automation_trigger.assert_called_once()
    call_data = mock_automation_trigger.call_args.args[0].data
    assert call_data["entity_id"] == "automation.kitchen_lights"
    assert call_data["skip_condition"] is True


@pytest.mark.asyncio
async def test_trigger_automation_respects_skip_condition_false(
    hass: HomeAssistant, mock_automation_trigger
):
    hass.states.async_set("automation.kitchen_lights", "on", {"id": "kitchen_id"})

    await trigger_automation(hass, "kitchen_id", skip_condition=False)

    call_data = mock_automation_trigger.call_args.args[0].data
    assert call_data["skip_condition"] is False


@pytest.mark.asyncio
async def test_trigger_automation_raises_when_no_live_entity(hass: HomeAssistant):
    with pytest.raises(AutomationNotRunningError, match="unknown_id"):
        await trigger_automation(hass, "unknown_id")


# --- set_number_value ----------------------------------------------------------


@pytest.fixture
def mock_number_set_value(hass: HomeAssistant):
    mock = AsyncMock()
    hass.services.async_register("number", "set_value", mock)
    return mock


@pytest.fixture
def mock_input_number_set_value(hass: HomeAssistant):
    mock = AsyncMock()
    hass.services.async_register("input_number", "set_value", mock)
    return mock


@pytest.mark.asyncio
async def test_set_number_value_calls_number_domain_service(
    hass: HomeAssistant, mock_number_set_value
):
    hass.states.async_set("number.battery_charge_slot", "0")

    await set_number_value(hass, "number.battery_charge_slot", 42.5)

    call_data = mock_number_set_value.call_args.args[0].data
    assert call_data == {"entity_id": "number.battery_charge_slot", "value": 42.5}


@pytest.mark.asyncio
async def test_set_number_value_calls_input_number_domain_service(
    hass: HomeAssistant, mock_input_number_set_value
):
    hass.states.async_set("input_number.target_temp", "0")

    await set_number_value(hass, "input_number.target_temp", 21)

    call_data = mock_input_number_set_value.call_args.args[0].data
    assert call_data == {"entity_id": "input_number.target_temp", "value": 21}


@pytest.mark.asyncio
async def test_set_number_value_raises_for_invalid_domain(hass: HomeAssistant):
    with pytest.raises(InvalidEntityDomainError, match="switch"):
        await set_number_value(hass, "switch.garage_door", 1)


@pytest.mark.asyncio
async def test_set_number_value_raises_for_missing_entity(hass: HomeAssistant):
    with pytest.raises(EntityNotFoundError):
        await set_number_value(hass, "number.does_not_exist", 1)


# --- set_boolean_value ----------------------------------------------------------


@pytest.fixture
def mock_input_boolean_turn_on(hass: HomeAssistant):
    mock = AsyncMock()
    hass.services.async_register("input_boolean", "turn_on", mock)
    return mock


@pytest.fixture
def mock_input_boolean_turn_off(hass: HomeAssistant):
    mock = AsyncMock()
    hass.services.async_register("input_boolean", "turn_off", mock)
    return mock


@pytest.mark.asyncio
async def test_set_boolean_value_turns_on(
    hass: HomeAssistant, mock_input_boolean_turn_on
):
    hass.states.async_set("input_boolean.vacation_mode", "off")

    await set_boolean_value(hass, "input_boolean.vacation_mode", True)

    call_data = mock_input_boolean_turn_on.call_args.args[0].data
    assert call_data == {"entity_id": "input_boolean.vacation_mode"}


@pytest.mark.asyncio
async def test_set_boolean_value_turns_off(
    hass: HomeAssistant, mock_input_boolean_turn_off
):
    hass.states.async_set("input_boolean.vacation_mode", "on")

    await set_boolean_value(hass, "input_boolean.vacation_mode", False)

    call_data = mock_input_boolean_turn_off.call_args.args[0].data
    assert call_data == {"entity_id": "input_boolean.vacation_mode"}


@pytest.mark.asyncio
async def test_set_boolean_value_raises_for_invalid_domain(hass: HomeAssistant):
    with pytest.raises(InvalidEntityDomainError, match="switch"):
        await set_boolean_value(hass, "switch.garage_door", True)


@pytest.mark.asyncio
async def test_set_boolean_value_raises_for_missing_entity(hass: HomeAssistant):
    with pytest.raises(EntityNotFoundError):
        await set_boolean_value(hass, "input_boolean.does_not_exist", True)
