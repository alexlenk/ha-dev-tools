"""Tests for derived-sensor config-entry CRUD (derived_sensor_manager.py)."""

import asyncio
import json

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_dev_tools.derived_sensor_manager import (
    DerivedSensorNotFoundError,
    FlowAbortedError,
    FlowStepRequiredError,
    InvalidDerivedSensorDomainError,
    _current_step_values,
    _schema_field_names,
    _serialize_schema,
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


def test_serialize_schema_none_returns_empty_list():
    """A flow step result with no data_schema at all (FlowStepRequiredError's
    schema is optional) must serialize to an empty list, not raise on a
    None input - every flow step exercised elsewhere in this file happens
    to carry a real schema, so this is exercised directly."""
    assert _serialize_schema(None) == []


async def test_statistics_duplicate_entry_raises_flow_aborted(hass: HomeAssistant):
    """A real ABORT flow result (not the VoluptuousInvalid path the other
    FlowAbortedError tests exercise) - HA's own schema-flow config entries
    abort on an exact duplicate via _async_abort_entries_match."""
    steps = {
        "user": {"entity_id": "sensor.a", "name": "Test Statistics"},
        "state_characteristic": {"state_characteristic": "mean"},
        "options": {"sampling_size": 20, "precision": 2},
    }
    created = await create_derived_sensor(hass, "statistics", steps)
    assert created["domain"] == "statistics"

    with pytest.raises(FlowAbortedError, match="already_configured"):
        await create_derived_sensor(hass, "statistics", steps)


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


# --- issue #81: omitted fields keep their current values -----------------


async def _template_sensor_with_device(hass: HomeAssistant) -> tuple[dict, str]:
    owner = MockConfigEntry(domain="test")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("test", "1")}
    )
    created = await create_derived_sensor(
        hass,
        "template",
        {
            "user": {"next_step_id": "sensor"},
            "sensor": {
                "name": "Output Power",
                "state": "{{ 1 }}",
                "device_id": device.id,
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
                "additional_options": {"availability": "{{ true }}"},
            },
        },
    )
    return created, device.id


async def test_update_keeps_omitted_optional_fields(hass: HomeAssistant):
    """The exact #81 report: changing only `state` silently dropped
    `device_id`, because HA's options flow deletes every optional key the
    submitted input leaves out. Omitted fields - including ones nested in a
    section - must now keep their current values."""
    created, device_id = await _template_sensor_with_device(hass)

    updated = await update_derived_sensor(
        hass, created["entry_id"], {"sensor": {"state": "{{ 2 }}"}}
    )

    options = updated["options"]
    assert options["state"] == "{{ 2 }}"
    assert options["device_id"] == device_id
    assert options["unit_of_measurement"] == "W"
    assert options["device_class"] == "power"
    assert options["state_class"] == "measurement"
    assert options["additional_options"] == {"availability": "{{ true }}"}


async def test_update_none_clears_optional_field(hass: HomeAssistant):
    created, _ = await _template_sensor_with_device(hass)

    updated = await update_derived_sensor(
        hass, created["entry_id"], {"sensor": {"device_id": None}}
    )

    assert "device_id" not in updated["options"]
    assert updated["options"]["state"] == "{{ 1 }}"


async def test_needs_input_schema_is_json_serializable(hass: HomeAssistant):
    """#81's discovery call crashed with "Object of type _Unsupported is not
    JSON serializable" on HA 2026.9+ (probatio's UNSUPPORTED sentinel leaking
    through voluptuous_serialize) - the schema handed back must be plain JSON,
    and must carry each field's current value as its suggested_value."""
    created, device_id = await _template_sensor_with_device(hass)

    with pytest.raises(FlowStepRequiredError) as exc_info:
        await update_derived_sensor(hass, created["entry_id"], {})

    schema = exc_info.value.schema
    json.dumps(schema)
    fields = {field["name"]: field for field in schema}
    assert fields["device_id"]["description"] == {"suggested_value": device_id}


async def test_unknown_step_field_error_lists_accepted_fields(hass: HomeAssistant):
    """#80's utility_meter chase: a create-only field was rejected one at a
    time. The error now names every field the step accepts."""
    created = await create_derived_sensor(
        hass,
        "utility_meter",
        {"user": {"source": "sensor.a", "name": "Meter", "cycle": "monthly"}},
    )

    with pytest.raises(FlowAbortedError) as exc_info:
        await update_derived_sensor(
            hass, created["entry_id"], {"init": {"cycle": "quarter-hourly"}}
        )

    message = str(exc_info.value)
    assert "Fields this step accepts" in message
    assert "'source'" in message
    assert "'periodically_resetting'" in message


# --- issue #82: step-id-free `options` patch -----------------------------


async def test_options_patch_changes_only_given_fields(hass: HomeAssistant):
    created = await create_derived_sensor(
        hass,
        "min_max",
        {
            "user": {
                "entity_ids": ["sensor.a"],
                "type": "sum",
                "round_digits": 3,
                "name": "Solar Total",
            }
        },
    )

    updated = await update_derived_sensor(
        hass, created["entry_id"], options={"entity_ids": ["sensor.a", "sensor.b"]}
    )

    assert updated["options"]["entity_ids"] == ["sensor.a", "sensor.b"]
    assert updated["options"]["type"] == "sum"
    assert updated["options"]["round_digits"] == 3


async def test_options_patch_on_template_keeps_device(hass: HomeAssistant):
    """Template's options `init` step has no schema and auto-skips to the
    platform step - the patch needs no step id there either."""
    created, device_id = await _template_sensor_with_device(hass)

    updated = await update_derived_sensor(
        hass, created["entry_id"], options={"state": "{{ 3 }}"}
    )

    assert updated["options"]["state"] == "{{ 3 }}"
    assert updated["options"]["device_id"] == device_id


async def test_options_patch_rejects_create_only_field_without_writing(
    hass: HomeAssistant,
):
    created = await create_derived_sensor(
        hass,
        "utility_meter",
        {"user": {"source": "sensor.a", "name": "Meter", "cycle": "monthly"}},
    )

    with pytest.raises(FlowAbortedError) as exc_info:
        await update_derived_sensor(
            hass,
            created["entry_id"],
            options={"source": "sensor.b", "cycle": "quarter-hourly"},
        )

    message = str(exc_info.value)
    assert "['cycle']" in message
    assert "'source'" in message
    options = get_derived_sensor(hass, created["entry_id"])["options"]
    assert options["source"] == "sensor.a"
    assert options["cycle"] == "monthly"


async def test_steps_and_options_together_rejected(hass: HomeAssistant):
    with pytest.raises(FlowAbortedError):
        await update_derived_sensor(
            hass, "irrelevant", {"init": {"type": "min"}}, {"type": "min"}
        )


async def test_update_section_field_merges_into_section(hass: HomeAssistant):
    """A partial section dict is merged into that section's current values,
    not swapped in wholesale."""
    created, _ = await _template_sensor_with_device(hass)

    updated = await update_derived_sensor(
        hass,
        created["entry_id"],
        options={"additional_options": {"availability": "{{ false }}"}},
    )

    assert updated["options"]["additional_options"] == {"availability": "{{ false }}"}
    assert updated["options"]["state"] == "{{ 1 }}"


def test_step_schema_helpers_handle_no_schema_and_plain_keys():
    assert _schema_field_names(None) == []
    assert _current_step_values(None) == {}
    schema = vol.Schema({"plain": str, vol.Optional("marked"): str})
    assert _schema_field_names(schema) == ["plain", "marked"]
    assert _current_step_values(schema) == {}


async def test_non_object_step_input_raises_flow_aborted(hass: HomeAssistant):
    with pytest.raises(FlowAbortedError, match="must be an object"):
        await create_derived_sensor(hass, "min_max", {"user": ["not", "a", "dict"]})
