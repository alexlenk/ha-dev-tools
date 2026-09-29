"""Tests for statistics_manager.py (issue #123), against a real recorder.

Same fixture pattern as test_history_manager.py (see its module docstring):
`recorder_mock` via usefixtures, never next to `hass` as a parameter.
"""

from datetime import timedelta

import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_import_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ha_dev_tools.history_manager import RecorderNotAvailableError
from custom_components.ha_dev_tools.statistics_manager import (
    get_statistics,
    list_statistics,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Shadow conftest.py's fixture - see test_history_manager.py."""
    yield


START = dt_util.parse_datetime("2024-12-01T00:00:00+00:00")


def _metadata(statistic_id: str, source: str, name: str) -> dict:
    return {
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": name,
        "source": source,
        "statistic_id": statistic_id,
        "unit_class": "energy",
        "unit_of_measurement": "kWh",
    }


def _hours(count: int) -> list[dict]:
    return [
        {"start": START + timedelta(hours=hour), "state": hour, "sum": hour * 1.5}
        for hour in range(count)
    ]


async def _seed(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "sensor", {})
    hass.states.async_set(
        "sensor.live_meter",
        "12",
        {
            "state_class": "total_increasing",
            "unit_of_measurement": "kWh",
            "device_class": "energy",
        },
    )
    async_import_statistics(
        hass, _metadata("sensor.old_meter", "recorder", "Hauptwohnung"), _hours(3)
    )
    async_import_statistics(
        hass, _metadata("sensor.live_meter", "recorder", "Live"), _hours(2)
    )
    async_add_external_statistics(
        hass,
        _metadata("tibber:energy_consumption_home1", "tibber", "Tibber home"),
        _hours(5),
    )
    await async_wait_recording_done(hass)


@pytest.mark.asyncio
async def test_statistics_need_the_recorder(hass: HomeAssistant):
    with pytest.raises(RecorderNotAvailableError):
        await list_statistics(hass)
    with pytest.raises(RecorderNotAvailableError):
        await get_statistics(hass, ["sensor.x"], start_time=START)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_list_statistics_finds_orphans_sources_and_periods(hass: HomeAssistant):
    await _seed(hass)

    result = await list_statistics(hass)
    rows = {row["statistic_id"]: row for row in result["statistics"]}
    assert set(rows) == {
        "sensor.live_meter",
        "sensor.old_meter",
        "tibber:energy_consumption_home1",
    }
    old = rows["sensor.old_meter"]
    assert old["has_entity"] is False
    assert old["name"] == "Hauptwohnung"
    assert (old["has_sum"], old["has_mean"]) == (True, False)
    assert old["unit_of_measurement"] == "kWh"
    assert old["first_period"] == "2024-12-01T00:00:00+00:00"
    assert old["last_period"] == "2024-12-01T02:00:00+00:00"
    assert rows["sensor.live_meter"]["has_entity"] is True
    tibber = rows["tibber:energy_consumption_home1"]
    assert (tibber["source"], tibber["has_entity"]) == ("tibber", None)
    assert tibber["last_period"] == "2024-12-01T04:00:00+00:00"
    assert result["truncated"] is False

    orphans = await list_statistics(hass, orphaned_only=True)
    assert [row["statistic_id"] for row in orphans["statistics"]] == [
        "sensor.old_meter"
    ]
    # HA's own validation: the old meter's entity is gone.
    assert orphans["statistics"][0]["issues"]
    issues = await list_statistics(hass, issues_only=True)
    assert "sensor.old_meter" in [row["statistic_id"] for row in issues["statistics"]]

    assert [
        row["statistic_id"]
        for row in (await list_statistics(hass, search="HAUPT"))["statistics"]
    ] == ["sensor.old_meter"]
    assert [
        row["statistic_id"]
        for row in (await list_statistics(hass, source="tibber"))["statistics"]
    ] == ["tibber:energy_consumption_home1"]
    assert (await list_statistics(hass, unit="W"))["statistics"] == []
    assert len((await list_statistics(hass, unit="kWh"))["statistics"]) == 3
    assert (await list_statistics(hass, statistic_type="mean"))["statistics"] == []
    limited = await list_statistics(hass, limit=1)
    assert (limited["count"], limited["total"], limited["truncated"]) == (1, 3, True)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_get_statistics_pages_through_a_series(hass: HomeAssistant):
    await _seed(hass)

    result = await get_statistics(
        hass,
        ["tibber:energy_consumption_home1", "sensor.typo"],
        start_time=START,
        types=["sum", "change"],
        limit=2,
    )
    tibber = result["statistics"]["tibber:energy_consumption_home1"]
    assert result["period"] == "hour"
    assert tibber["known"] is True
    assert tibber["count"] == 2
    assert tibber["truncated"] is True
    assert tibber["rows"][0]["start"] == "2024-12-01T00:00:00+00:00"
    assert tibber["rows"][1]["sum"] == 1.5
    assert "change" in tibber["rows"][1]
    assert tibber["next_start_time"] == "2024-12-01T02:00:00+00:00"
    assert result["statistics"]["sensor.typo"] == {
        "known": False,
        "rows": [],
        "count": 0,
        "truncated": False,
    }

    rest = await get_statistics(
        hass,
        ["tibber:energy_consumption_home1"],
        start_time=dt_util.parse_datetime(tibber["next_start_time"]),
        end_time=START + timedelta(hours=10),
    )
    rows = rest["statistics"]["tibber:energy_consumption_home1"]["rows"]
    assert [row["start"][11:16] for row in rows] == ["02:00", "03:00", "04:00"]
    assert (
        "next_start_time" not in rest["statistics"]["tibber:energy_consumption_home1"]
    )

    daily = await get_statistics(
        hass,
        ["sensor.old_meter"],
        start_time=START,
        period="day",
        types=["sum", "last_reset"],
    )
    assert daily["statistics"]["sensor.old_meter"]["count"] == 1
    assert daily["statistics"]["sensor.old_meter"]["rows"][0]["last_reset"] is None


# --- the tools ---------------------------------------------------------------


def _llm_context():
    from tests.test_llm_api import _llm_context as build

    return build()


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_statistics_tools(hass: HomeAssistant):
    from homeassistant.helpers import llm

    from custom_components.ha_dev_tools.llm_api import (
        GetStatisticsTool,
        ListStatisticsTool,
    )

    await _seed(hass)
    listed = await ListStatisticsTool()._run(
        hass,
        llm.ToolInput(tool_name="list_statistics", tool_args={"orphaned_only": True}),
        _llm_context(),
    )
    assert [row["statistic_id"] for row in listed["statistics"]] == ["sensor.old_meter"]

    rows = await GetStatisticsTool()._run(
        hass,
        llm.ToolInput(
            tool_name="get_statistics",
            tool_args={
                "statistic_ids": ["sensor.old_meter"],
                "start_time": "2024-12-01T00:00:00+00:00",
                "end_time": "2024-12-02T00:00:00+00:00",
                "types": ["sum"],
            },
        ),
        _llm_context(),
    )
    assert rows["statistics"]["sensor.old_meter"]["count"] == 3

    bad = await GetStatisticsTool()._run(
        hass,
        llm.ToolInput(
            tool_name="get_statistics",
            tool_args={"statistic_ids": ["x.y"], "start_time": "yesterday"},
        ),
        _llm_context(),
    )
    assert bad["error_type"] == "ValueError"


@pytest.mark.asyncio
async def test_statistics_tools_without_the_recorder(hass: HomeAssistant):
    from homeassistant.helpers import llm

    from custom_components.ha_dev_tools.llm_api import ListStatisticsTool

    result = await ListStatisticsTool()._run(
        hass, llm.ToolInput(tool_name="list_statistics", tool_args={}), _llm_context()
    )
    assert result["error_type"] == "RecorderNotAvailableError"
