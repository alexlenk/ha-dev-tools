"""Tests for derived-sensor config-entry CRUD (derived_sensor_manager.py)."""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools.derived_sensor_manager import (
    DerivedSensorNotFoundError,
    FlowStepRequiredError,
    InvalidDerivedSensorDomainError,
    create_derived_sensor,
    delete_derived_sensor,
    get_derived_sensor,
    list_derived_sensors,
    reload_derived_sensor,
    update_derived_sensor,
)


@pytest.fixture(autouse=True)
def _source_entities(hass: HomeAssistant):
    hass.states.async_set("sensor.a", "1")
    hass.states.async_set("sensor.b", "2")


async def test_create_min_max_discovers_then_creates(hass: HomeAssistant):
    with pytest.raises(FlowStepRequiredError) as exc_info:
        await create_derived_sensor(hass, "min_max", {})

    err = exc_info.value
    assert err.step_id == "user"
    field_names = {f["name"] for f in err.schema}
    assert {"entity_ids", "type", "round_digits", "name"} <= field_names

    created = await create_derived_sensor(
        hass,
        "min_max",
        {
            err.step_id: {
                "entity_ids": ["sensor.a", "sensor.b"],
                "type": "max",
                "round_digits": 2,
                "name": "Test MinMax",
            }
        },
    )
    assert created["domain"] == "min_max"
    assert created["title"] == "Test MinMax"
    assert created["options"]["type"] == "max"


async def test_list_and_get(hass: HomeAssistant):
    created = await create_derived_sensor(
        hass,
        "min_max",
        {
            "user": {
                "entity_ids": ["sensor.a", "sensor.b"],
                "type": "max",
                "round_digits": 2,
                "name": "Test MinMax",
            }
        },
    )

    listed = list_derived_sensors(hass, "min_max")
    assert any(e["entry_id"] == created["entry_id"] for e in listed)

    fetched = get_derived_sensor(hass, created["entry_id"])
    assert fetched["entry_id"] == created["entry_id"]


async def test_list_across_all_domains_does_not_error(hass: HomeAssistant):
    await create_derived_sensor(
        hass,
        "min_max",
        {
            "user": {
                "entity_ids": ["sensor.a", "sensor.b"],
                "type": "max",
                "round_digits": 2,
                "name": "Test MinMax",
            }
        },
    )

    listed = list_derived_sensors(hass)
    assert any(e["domain"] == "min_max" for e in listed)


async def test_update_min_max(hass: HomeAssistant):
    created = await create_derived_sensor(
        hass,
        "min_max",
        {
            "user": {
                "entity_ids": ["sensor.a", "sensor.b"],
                "type": "max",
                "round_digits": 2,
                "name": "Test MinMax",
            }
        },
    )

    with pytest.raises(FlowStepRequiredError) as exc_info:
        await update_derived_sensor(hass, created["entry_id"], {})

    err = exc_info.value
    updated = await update_derived_sensor(
        hass,
        created["entry_id"],
        {
            err.step_id: {
                "entity_ids": ["sensor.a", "sensor.b"],
                "type": "min",
                "round_digits": 4,
            }
        },
    )
    assert updated["options"]["type"] == "min"
    assert updated["options"]["round_digits"] == 4


async def test_delete(hass: HomeAssistant):
    created = await create_derived_sensor(
        hass,
        "min_max",
        {
            "user": {
                "entity_ids": ["sensor.a", "sensor.b"],
                "type": "max",
                "round_digits": 2,
                "name": "Test MinMax",
            }
        },
    )

    deleted = await delete_derived_sensor(hass, created["entry_id"])
    assert deleted["deleted"] is True

    with pytest.raises(DerivedSensorNotFoundError):
        get_derived_sensor(hass, created["entry_id"])


async def test_reload(hass: HomeAssistant):
    created = await create_derived_sensor(
        hass,
        "min_max",
        {
            "user": {
                "entity_ids": ["sensor.a", "sensor.b"],
                "type": "max",
                "round_digits": 2,
                "name": "Test MinMax",
            }
        },
    )

    result = await reload_derived_sensor(hass, created["entry_id"])
    assert result["reloaded"] is True


async def test_invalid_domain_rejected(hass: HomeAssistant):
    with pytest.raises(InvalidDerivedSensorDomainError):
        await create_derived_sensor(hass, "not_a_domain", {})
    with pytest.raises(InvalidDerivedSensorDomainError):
        list_derived_sensors(hass, "not_a_domain")


async def test_not_found_rejected(hass: HomeAssistant):
    with pytest.raises(DerivedSensorNotFoundError):
        get_derived_sensor(hass, "nonexistent")
    with pytest.raises(DerivedSensorNotFoundError):
        await update_derived_sensor(hass, "nonexistent", {})
    with pytest.raises(DerivedSensorNotFoundError):
        await delete_derived_sensor(hass, "nonexistent")
    with pytest.raises(DerivedSensorNotFoundError):
        await reload_derived_sensor(hass, "nonexistent")


async def test_utility_meter_single_step(hass: HomeAssistant):
    """Confirm this generalizes beyond min_max to a second single-step domain."""
    with pytest.raises(FlowStepRequiredError) as exc_info:
        await create_derived_sensor(hass, "utility_meter", {})

    err = exc_info.value
    created = await create_derived_sensor(
        hass,
        "utility_meter",
        {
            err.step_id: {
                "source": "sensor.a",
                "name": "Test Utility Meter",
                "cycle": "monthly",
            }
        },
    )
    assert created["domain"] == "utility_meter"


async def test_statistics_multi_step(hass: HomeAssistant):
    """statistics has a fixed 3-step flow - prove multi-step driving actually works."""
    steps: dict = {}
    result = None
    for _ in range(6):  # generous bound, real flow is 3 steps
        try:
            result = await create_derived_sensor(hass, "statistics", steps)
            break
        except FlowStepRequiredError as err:
            if err.step_id == "user":
                steps["user"] = {"entity_id": "sensor.a", "name": "Test Statistics"}
            elif err.step_id == "state_characteristic":
                steps["state_characteristic"] = {"state_characteristic": "mean"}
            elif err.step_id == "options":
                steps["options"] = {"sampling_size": 20, "precision": 2}
            else:
                raise AssertionError(f"unexpected step {err.step_id}") from err
    assert result is not None, "statistics flow did not finish within bound"
    assert result["domain"] == "statistics"


async def test_threshold_single_step(hass: HomeAssistant):
    with pytest.raises(FlowStepRequiredError) as exc_info:
        await create_derived_sensor(hass, "threshold", {})

    created = await create_derived_sensor(
        hass,
        "threshold",
        {
            exc_info.value.step_id: {
                "name": "Test Threshold",
                "entity_id": "sensor.a",
                "upper": 10,
            }
        },
    )
    assert created["domain"] == "threshold"


async def test_derivative_single_step(hass: HomeAssistant):
    with pytest.raises(FlowStepRequiredError) as exc_info:
        await create_derived_sensor(hass, "derivative", {})

    created = await create_derived_sensor(
        hass,
        "derivative",
        {
            exc_info.value.step_id: {
                "name": "Test Derivative",
                "source": "sensor.a",
                "time_window": {"minutes": 5},
            }
        },
    )
    assert created["domain"] == "derivative"


async def test_integration_riemann_sum_single_step(hass: HomeAssistant):
    """The 'Integration - Riemann sum' helper - domain name is just 'integration'."""
    with pytest.raises(FlowStepRequiredError) as exc_info:
        await create_derived_sensor(hass, "integration", {})

    created = await create_derived_sensor(
        hass,
        "integration",
        {
            exc_info.value.step_id: {
                "name": "Test Integration",
                "source": "sensor.a",
            }
        },
    )
    assert created["domain"] == "integration"
