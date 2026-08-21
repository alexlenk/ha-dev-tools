"""Tests for recorder-backed state history and logbook access (history_manager.py).

Uses a real recorder against a real `hass` instance (per CONTRIBUTING.md's
"prefer real HA machinery over mocks" guidance) rather than mocking
`recorder.history`/`logbook.processor` directly - those are exactly the
pieces of Home Assistant's own query machinery this module exists to
reuse correctly, so a mock would only prove this module calls a mock the
way it was told to, not that it calls the real thing correctly.
"""

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ha_dev_tools.history_manager import (
    RecorderNotAvailableError,
    get_entity_history,
    get_logbook_entries,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Shadow conftest.py's autouse fixture of the same name for this module.

    That fixture depends on `hass`, forcing it to instantiate before any
    other fixture gets a chance to run - including `recorder_mock`, whose
    own setup (via pytest_homeassistant_custom_component's `recorder_db_url`
    fixture) must run *before* `hass` exists, or its own ordering assertion
    fails. None of the tests in this module need custom-component discovery
    (they call history_manager's functions directly rather than going
    through config entry setup), so it's safe to no-op here instead.
    """
    yield


@pytest.mark.asyncio
async def test_get_entity_history_raises_when_recorder_not_set_up(hass: HomeAssistant):
    """Without the recorder set up, this fails clearly rather than a raw KeyError."""
    with pytest.raises(RecorderNotAvailableError):
        await get_entity_history(hass, ["sensor.x"], start_time=dt_util.utcnow())


@pytest.mark.asyncio
async def test_get_logbook_entries_raises_when_recorder_not_set_up(hass: HomeAssistant):
    with pytest.raises(RecorderNotAvailableError):
        await get_logbook_entries(hass, start_time=dt_util.utcnow())


@pytest.mark.asyncio
async def test_get_logbook_entries_raises_when_logbook_not_set_up(
    hass: HomeAssistant, recorder_mock: Recorder
):
    """Recorder alone isn't enough - logbook is a separate component that depends on it."""
    with pytest.raises(RecorderNotAvailableError):
        await get_logbook_entries(hass, start_time=dt_util.utcnow())


@pytest.mark.asyncio
async def test_get_entity_history_returns_recorded_states(
    hass: HomeAssistant, recorder_mock: Recorder
):
    start_time = dt_util.utcnow()

    hass.states.async_set("input_boolean.cleaning", "on")
    await hass.async_block_till_done()
    hass.states.async_set("input_boolean.cleaning", "off")
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    result = await get_entity_history(
        hass,
        ["input_boolean.cleaning"],
        start_time=start_time,
        end_time=dt_util.utcnow(),
    )

    states = result["entities"]["input_boolean.cleaning"]["states"]
    assert [s["state"] for s in states] == ["on", "off"]
    assert result["entities"]["input_boolean.cleaning"]["truncated"] is False


@pytest.mark.asyncio
async def test_get_entity_history_includes_unrecorded_entity_id_as_empty(
    hass: HomeAssistant, recorder_mock: Recorder
):
    """A requested entity_id with no rows in the window still comes back explicitly.

    Not silently dropped, unlike a raw get_significant_states() call.
    """
    start_time = dt_util.utcnow()

    result = await get_entity_history(
        hass, ["sensor.never_existed"], start_time=start_time
    )

    assert result["entities"]["sensor.never_existed"] == {
        "states": [],
        "count": 0,
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_get_entity_history_truncates_to_limit(
    hass: HomeAssistant, recorder_mock: Recorder
):
    start_time = dt_util.utcnow()

    for i in range(5):
        hass.states.async_set("counter.x", str(i))
        await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    result = await get_entity_history(
        hass,
        ["counter.x"],
        start_time=start_time,
        significant_changes_only=False,
        limit=2,
    )

    entry = result["entities"]["counter.x"]
    assert entry["count"] == 2
    assert entry["truncated"] is True
    # Kept the most recent states, not the oldest.
    assert [s["state"] for s in entry["states"]] == ["3", "4"]


@pytest.mark.asyncio
async def test_get_logbook_entries_includes_state_change(
    hass: HomeAssistant, recorder_mock: Recorder
):
    assert await async_setup_component(hass, "logbook", {})
    await hass.async_block_till_done()

    start_time = dt_util.utcnow()
    hass.states.async_set("input_boolean.cleaning", "on", {"friendly_name": "Cleaning"})
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    result = await get_logbook_entries(
        hass,
        start_time=start_time,
        end_time=dt_util.utcnow(),
        entity_ids=["input_boolean.cleaning"],
    )

    assert result["count"] >= 1
    assert any(
        e.get("entity_id") == "input_boolean.cleaning" for e in result["entries"]
    )
    # `when` is a JSON-safe ISO string, not the raw epoch float EventProcessor returns.
    assert all(isinstance(e["when"], str) for e in result["entries"])
