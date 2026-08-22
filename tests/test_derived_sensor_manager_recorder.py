"""Tests for the one derived-sensor domain that depends on recorder: filter.

Split into its own module for the same reason test_history_manager.py is:
`recorder_mock` (via pytest-homeassistant-custom-component's own
`recorder_db_url` fixture) must run *before* `hass` is instantiated, and
conftest.py's `auto_enable_custom_integrations` autouse fixture depends on
`hass`, so it has to be shadowed here to preserve that ordering.
"""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools.derived_sensor_manager import (
    FlowStepRequiredError,
    create_derived_sensor,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Shadow conftest.py's autouse fixture of the same name for this module.

    See test_history_manager.py's fixture of this name for the full
    reasoning - identical situation here since `filter` depends on
    `recorder`.
    """
    yield


@pytest.mark.usefixtures("recorder_mock")
async def test_filter_branches_by_filter_type(hass: HomeAssistant):
    """filter's flow branches into a type-specific step - prove branching works too."""
    hass.states.async_set("sensor.a", "1")

    steps: dict = {}
    result = None
    for _ in range(6):  # generous bound, real flow is 2 steps for "lowpass"
        try:
            result = await create_derived_sensor(hass, "filter", steps)
            break
        except FlowStepRequiredError as err:
            if err.step_id == "user":
                steps["user"] = {
                    "entity_id": "sensor.a",
                    "name": "Test Filter",
                    "filter": "lowpass",
                }
            elif err.step_id == "lowpass":
                steps["lowpass"] = {"time_constant": 10}
            else:
                raise AssertionError(f"unexpected step {err.step_id}") from err
    assert result is not None, "filter flow did not finish within bound"
    assert result["domain"] == "filter"
