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

    run = await trigger_automation(hass, "kitchen_id", wait_seconds=5)

    assert run["entity_id"] == "automation.kitchen_lights"
    assert run["finished"] is True
    assert "error" not in run
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
    await hass.async_block_till_done(wait_background_tasks=True)

    call_data = mock_automation_trigger.call_args.args[0].data
    assert call_data["skip_condition"] is False


@pytest.mark.asyncio
async def test_trigger_automation_returns_at_once_and_reports_errors(
    hass: HomeAssistant,
):
    import asyncio

    hass.states.async_set("automation.kitchen_lights", "on", {"id": "kitchen_id"})
    release = asyncio.Event()

    async def slow(_call):
        await release.wait()

    hass.services.async_register("automation", "trigger", slow)
    started = await trigger_automation(hass, "kitchen_id")
    assert started["finished"] is False
    assert started["context_id"]
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    hass.services.async_register(
        "automation", "trigger", AsyncMock(side_effect=RuntimeError("boom"))
    )
    waited = await trigger_automation(hass, "kitchen_id", wait_seconds=5)
    assert (waited["finished"], waited["error"]) == (True, "boom")


@pytest.mark.asyncio
async def test_a_cancelled_request_doesnt_cut_the_run_off(hass: HomeAssistant):
    """Issue #131: the run was awaited (blocking=True), so the MCP client
    giving up cancelled the automation - and the scripts it waited on -
    part-way through. Now the run is detached, like the UI's button."""
    import asyncio

    from homeassistant.setup import async_setup_component

    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "slow",
                "alias": "Slow",
                "triggers": [{"trigger": "event", "event_type": "never"}],
                "actions": [
                    {"wait_for_trigger": [{"trigger": "event", "event_type": "go"}]},
                    {"event": "done"},
                ],
            }
        },
    )
    await hass.async_block_till_done()
    done = []
    hass.bus.async_listen("done", done.append)

    request = asyncio.ensure_future(trigger_automation(hass, "slow", wait_seconds=30))
    await asyncio.sleep(0.05)
    request.cancel()  # the client's timeout
    with pytest.raises(asyncio.CancelledError):
        await request

    hass.bus.async_fire("go")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(done) == 1


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


# --- control_entities ------------------------------------------------------------


def _options(hass: HomeAssistant, **options):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.ha_dev_tools.const import DOMAIN

    entry = MockConfigEntry(domain=DOMAIN, options=options)
    entry.add_to_hass(hass)
    return entry


def _registered(hass: HomeAssistant, domain: str, object_id: str, *, area=None):
    """A registry entity on a device of its own; (entity_id, device)."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    source = MockConfigEntry(domain="demo")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id,
        identifiers={("demo", object_id)},
        name=f"{object_id} device",
    )
    if area is not None:
        device = dr.async_get(hass).async_update_device(device.id, area_id=area)
    entry = er.async_get(hass).async_get_or_create(
        domain,
        "demo",
        object_id,
        suggested_object_id=object_id,
        device_id=device.id,
        config_entry=source,
    )
    hass.states.async_set(entry.entity_id, "off")
    return entry.entity_id, device


@pytest.mark.parametrize(
    ("entity_id", "action", "service"),
    [
        ("switch.desk", "on", "switch.turn_on"),
        ("light.desk", "off", "light.turn_off"),
        ("fan.desk", "toggle", "fan.toggle"),
        ("input_boolean.away", "on", "input_boolean.turn_on"),
        ("climate.hall", "off", "climate.turn_off"),
        ("media_player.tv", "toggle", "media_player.toggle"),
        ("humidifier.bath", "on", "humidifier.turn_on"),
        ("cover.blind", "on", "cover.open_cover"),
        ("cover.blind", "off", "cover.close_cover"),
        ("cover.blind", "toggle", "cover.toggle"),
        ("valve.garden", "on", "valve.open_valve"),
        ("valve.garden", "off", "valve.close_valve"),
    ],
)
def test_plan_control_maps_each_domain_to_its_service(
    hass: HomeAssistant, entity_id, action, service
):
    from custom_components.ha_dev_tools.service_call_manager import plan_control

    hass.states.async_set(entity_id, "off", {"friendly_name": "Thing"})
    assert plan_control(hass, [entity_id], action) == [
        {
            "entity_id": entity_id,
            "name": "Thing",
            "area": None,
            "state_now": "off",
            "service": service,
            "data": {"entity_id": entity_id},
        }
    ]


def test_plan_control_brightness_and_areas(hass: HomeAssistant):
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import entity_registry as er

    from custom_components.ha_dev_tools.service_call_manager import plan_control

    kitchen = ar.async_get(hass).async_create("Kitchen")
    hall = ar.async_get(hass).async_create("Hall")
    by_device, _ = _registered(hass, "light", "ceiling", area=kitchen.id)
    by_entity, _ = _registered(hass, "light", "lamp", area=kitchen.id)
    # HA 2026.10+: only an entity with a name of its own has its own area.
    er.async_get(hass).async_update_entity(by_entity, area_id=hall.id, name="Lamp")

    plan = plan_control(hass, [by_device, by_entity], "on", brightness_pct=40)

    assert [(step["area"], step["data"]) for step in plan] == [
        ("Kitchen", {"entity_id": by_device, "brightness_pct": 40}),
        ("Hall", {"entity_id": by_entity, "brightness_pct": 40}),
    ]
    assert plan[0]["name"] == by_device  # no friendly_name: the entity_id


def test_plan_control_lists_every_problem(hass: HomeAssistant):
    from custom_components.ha_dev_tools.service_call_manager import (
        ControlRefusedError,
        plan_control,
    )

    hass.states.async_set("switch.desk", "off")
    with pytest.raises(ControlRefusedError) as err:
        plan_control(
            hass,
            [
                "lock.front_door",
                "alarm_control_panel.home",
                "siren.attic",
                "scene.evening",
                "light.gone",
                "switch.desk",
                "switch.desk",
            ],
            "off",
            brightness_pct=10,
        )
    message = str(err.value)
    for expected in (
        "brightness_pct goes with action 'on'",
        "an entity is listed more than once",
        "lock.front_door: lock is never controlled",
        "alarm_control_panel.home: alarm_control_panel is never controlled",
        "siren.attic: siren is never controlled",
        "scene.evening: scene isn't controllable here",
        "light.gone: no such entity",
        "switch.desk: brightness_pct is for lights only",
    ):
        assert expected in message

    with pytest.raises(ControlRefusedError, match="action is one of"):
        plan_control(hass, ["switch.desk"], "open")


def test_plan_control_refuses_excluded_entities_and_devices(hass: HomeAssistant):
    from homeassistant.helpers import device_registry as dr

    from custom_components.ha_dev_tools.const import (
        OPT_CONTROL_EXCLUDED_DEVICES,
        OPT_CONTROL_EXCLUDED_ENTITIES,
    )
    from custom_components.ha_dev_tools.service_call_manager import (
        ControlRefusedError,
        plan_control,
    )

    garage, garage_device = _registered(hass, "cover", "garage")
    renamed, renamed_device = _registered(hass, "switch", "pump")
    dr.async_get(hass).async_update_device(renamed_device.id, name_by_user="Pool")
    hass.states.async_set("switch.heater", "off")
    hass.states.async_set("switch.desk", "off")
    _options(
        hass,
        **{
            OPT_CONTROL_EXCLUDED_DEVICES: [garage_device.id, renamed_device.id],
            OPT_CONTROL_EXCLUDED_ENTITIES: ["switch.heater"],
        },
    )

    with pytest.raises(ControlRefusedError) as err:
        plan_control(hass, [garage, renamed, "switch.heater", "switch.desk"], "on")
    message = str(err.value)
    assert f"{garage}: its device 'garage device' is excluded" in message
    assert f"{renamed}: its device 'Pool' is excluded" in message
    assert "switch.heater: excluded from agent control" in message
    assert "switch.desk" not in message
    assert plan_control(hass, ["switch.desk"], "on")[0]["service"] == "switch.turn_on"


@pytest.mark.asyncio
async def test_control_entities_reports_each_outcome(hass: HomeAssistant):
    from custom_components.ha_dev_tools.service_call_manager import (
        control_entities,
        plan_control,
    )

    hass.states.async_set("switch.desk", "off")
    hass.states.async_set("switch.broken", "off")
    hass.states.async_set("switch.vanishing", "off")

    async def turn_on(call):
        entity_id = call.data["entity_id"]
        if entity_id == "switch.broken":
            raise RuntimeError("device offline")
        if entity_id == "switch.vanishing":
            hass.states.async_remove(entity_id)
            return
        hass.states.async_set(entity_id, "on")

    hass.services.async_register("switch", "turn_on", turn_on)
    plan = plan_control(
        hass, ["switch.broken", "switch.desk", "switch.vanishing"], "on"
    )

    assert await control_entities(hass, plan) == [
        {"entity_id": "switch.broken", "error": "device offline"},
        {
            "entity_id": "switch.desk",
            "service": "switch.turn_on",
            "state_before": "off",
            "state_after": "on",
        },
        {
            "entity_id": "switch.vanishing",
            "service": "switch.turn_on",
            "state_before": "off",
            "state_after": None,
        },
    ]
