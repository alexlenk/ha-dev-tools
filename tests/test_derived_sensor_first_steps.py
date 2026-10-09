"""Every derived-sensor domain's first step comes back as plain JSON
(issue #137) - with a real recorder, which `filter` depends on; fixture
pattern as in test_history_manager.py."""

import json

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools.derived_sensor_manager import (
    DERIVED_SENSOR_DOMAINS,
    FlowStepRequiredError,
    create_derived_sensor,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Shadow conftest.py's fixture - see test_history_manager.py."""
    yield


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.parametrize("domain", DERIVED_SENSOR_DOMAINS)
async def test_first_step_schema_is_json(hass: HomeAssistant, domain: str):
    """HA 2026.10 stopped re-exporting probatio's to_field_list from
    config_validation; the fallback to voluptuous_serialize then leaked
    probatio's UNSUPPORTED sentinel into every first step's schema, and
    the response crashed with "Object of type _Unsupported is not JSON
    serializable" before needs_input could be returned."""
    with pytest.raises(FlowStepRequiredError) as needs_input:
        await create_derived_sensor(hass, domain, {})

    schema = needs_input.value.schema
    assert isinstance(schema, list)
    json.dumps(schema)
    if domain != "template":  # a menu: one next_step_id field
        assert all("name" in field for field in schema)
