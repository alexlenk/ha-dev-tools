"""Tests for derived-sensor config-entry CRUD (derived_sensor_manager.py)."""

import asyncio

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools.derived_sensor_manager import (
    DerivedSensorNotFoundError,
    FlowAbortedError,
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


# --- template: config flow's first step is a real MENU, not a form ------------
#
# template needs no special handling beyond accepting FlowResultType.MENU
# into _drive_flow's loop the same way as a FORM - HA represents the menu
# choice as a "next_step_id" field, discovered and supplied exactly like
# any other step's fields.


async def test_create_template_sensor_via_menu(hass: HomeAssistant):
    with pytest.raises(FlowStepRequiredError) as exc_info:
        await create_derived_sensor(hass, "template", {})

    err = exc_info.value
    assert err.step_id == "user"
    field_names = {f["name"] for f in err.schema}
    assert field_names == {"next_step_id"}

    with pytest.raises(FlowStepRequiredError) as exc_info2:
        await create_derived_sensor(
            hass, "template", {"user": {"next_step_id": "sensor"}}
        )

    err2 = exc_info2.value
    assert err2.step_id == "sensor"
    field_names2 = {f["name"] for f in err2.schema}
    assert {"name", "state"} <= field_names2

    created = await create_derived_sensor(
        hass,
        "template",
        {
            "user": {"next_step_id": "sensor"},
            "sensor": {"name": "Test Template Sensor", "state": "{{ 1 }}"},
        },
    )
    assert created["domain"] == "template"
    assert created["title"] == "Test Template Sensor"
    assert created["options"]["template_type"] == "sensor"


async def test_create_template_binary_sensor_generalizes_beyond_sensor(
    hass: HomeAssistant,
):
    """Prove the menu branch isn't hardcoded to 'sensor' - binary_sensor works too."""
    created = await create_derived_sensor(
        hass,
        "template",
        {
            "user": {"next_step_id": "binary_sensor"},
            "binary_sensor": {"name": "Test Binary", "state": "{{ true }}"},
        },
    )
    assert created["domain"] == "template"
    assert created["options"]["template_type"] == "binary_sensor"


async def test_update_template_sensor_skips_straight_to_platform_step(
    hass: HomeAssistant,
):
    """template's options 'init' step has no schema of its own - it auto-skips
    straight to the platform-specific step (choose_options_step reads the
    entry's stored template_type), unlike create's real menu."""
    created = await create_derived_sensor(
        hass,
        "template",
        {
            "user": {"next_step_id": "sensor"},
            "sensor": {"name": "Test Template Sensor", "state": "{{ 1 }}"},
        },
    )

    with pytest.raises(FlowStepRequiredError) as exc_info:
        await update_derived_sensor(hass, created["entry_id"], {})

    err = exc_info.value
    assert err.step_id == "sensor"

    updated = await update_derived_sensor(
        hass, created["entry_id"], {"sensor": {"state": "{{ 2 }}"}}
    )
    assert updated["options"]["state"] == "{{ 2 }}"


async def test_template_sensor_raw_invalid_raises_flow_aborted(hass: HomeAssistant):
    """Template's own sensor validator raises plain vol.Invalid (not
    SchemaFlowError) for a unit/device_class mismatch - confirms _drive_flow's
    VoluptuousInvalid guard actually catches this path, not just the
    SchemaFlowError-based one derivative/statistics/threshold use."""
    with pytest.raises(FlowAbortedError):
        await create_derived_sensor(
            hass,
            "template",
            {
                "user": {"next_step_id": "sensor"},
                "sensor": {
                    "name": "Bad Sensor",
                    "state": "{{ 1 }}",
                    "device_class": "energy",
                    "unit_of_measurement": "not_a_real_unit",
                },
            },
        )


async def test_menu_invalid_next_step_id_raises_flow_aborted(hass: HomeAssistant):
    with pytest.raises(FlowAbortedError):
        await create_derived_sensor(
            hass, "template", {"user": {"next_step_id": "not_a_real_platform"}}
        )


# --- _drive_flow must not hang on repeated invalid input -----------------


async def test_validation_error_raises_instead_of_hanging(hass: HomeAssistant):
    """statistics's 'options' step requires sampling_size or max_age; supplying
    neither triggers a SchemaFlowError, which HA reports by re-showing the
    identical step with errors set rather than aborting. The unguarded
    version of _drive_flow resubmitted the same rejected input forever -
    confirmed directly (had to be force-killed past 2 minutes) before the
    attempted_steps guard was added. wait_for is a safety net so a
    regression here fails fast instead of hanging the whole test run again.
    """
    steps = {
        "user": {"entity_id": "sensor.a", "name": "Test Statistics"},
        "state_characteristic": {"state_characteristic": "mean"},
        "options": {"precision": 2},  # missing sampling_size/max_age - invalid
    }

    with pytest.raises(FlowStepRequiredError) as exc_info:
        await asyncio.wait_for(
            create_derived_sensor(hass, "statistics", steps), timeout=5
        )

    err = exc_info.value
    assert err.step_id == "options"
    assert err.errors
