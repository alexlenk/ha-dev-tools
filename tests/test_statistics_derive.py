"""Tests for derive_statistics (issue #143), against a real recorder -
fixture pattern as in test_statistics_manager.py."""

import json
from datetime import timedelta

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ha_dev_tools import statistics_manager as sm
from custom_components.ha_dev_tools.statistics_derive import plan_derive
from custom_components.ha_dev_tools.statistics_merge import merge_statistics
from tests.test_statistics_manager import (
    START,
    _llm_context,
    _metadata,
    _mirror,
    _seed,
)
from tests.test_statistics_merge import _compile, _latest_short_term_sum, _meter


def _input(tool_name: str, **args):
    from homeassistant.helpers import llm

    return llm.ToolInput(tool_name=tool_name, tool_args=args)


EUR_METER = {"state_class": "total", "unit_of_measurement": "EUR"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Shadow conftest.py's fixture - see test_history_manager.py."""
    yield


async def _price(hass, means: dict[int, float], unit: str = "EUR/kWh") -> None:
    async_import_statistics(
        hass,
        {
            **_metadata("sensor.price", "recorder", "Price"),
            "has_sum": False,
            "mean_type": StatisticMeanType.ARITHMETIC,
            "unit_class": None,
            "unit_of_measurement": unit,
        },
        [
            {
                "start": START + timedelta(hours=hour),
                "mean": mean,
                "min": mean,
                "max": mean,
            }
            for hour, mean in means.items()
        ],
    )
    await async_wait_recording_done(hass)


async def _sums(hass, statistic_id):
    return [
        round(row["sum"], 6)
        for row in (await sm.read_rows(hass, [statistic_id]))[statistic_id]
    ]


# old_meter (from _seed): kWh, hours 0-2, sums 0, 1.5, 3 -> changes 0, 1.5, 1.5


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_new_statistic_from_a_fixed_rate(hass: HomeAssistant):
    await _seed(hass)
    plan = await plan_derive(
        hass,
        "sensor.old_meter",
        0.1,
        "ha_dev_tools:old_meter_compensation",
        unit="EUR",
        can_back_up=False,
    )
    preview = plan.preview()
    assert preview["factor"] == {"kind": "fixed", "value": 0.1}
    assert preview["derived"]["hours"] == 3
    assert preview["derived"]["total"] == 0.3
    # By local month, as the dashboard shows it: the test instance runs in
    # US/Pacific, where 2024-12-01 00:00 UTC is still November.
    assert preview["derived"]["by_month"] == {"2024-11": 0.3}
    assert preview["target"] == {
        "statistic_id": "ha_dev_tools:old_meter_compensation",
        "new": True,
    }

    result = await merge_statistics(hass, plan)
    await async_wait_recording_done(hass)
    assert result["next_compile"] == {"applies": False}
    assert await _sums(hass, "ha_dev_tools:old_meter_compensation") == [0, 0.15, 0.3]
    meta = (await sm.read_metadata(hass, ["ha_dev_tools:old_meter_compensation"]))[
        "ha_dev_tools:old_meter_compensation"
    ]
    assert (meta["source"], meta["unit_of_measurement"], meta["name"]) == (
        "ha_dev_tools",
        "EUR",
        "sensor.old_meter × 0.1",
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_rate_periods_and_a_range(hass: HomeAssistant):
    await _seed(hass)
    periods = [
        {"from": (START + timedelta(hours=2)).isoformat(), "value": 1},
        {"from": START.isoformat(), "value": 10},
    ]
    plan = await plan_derive(
        hass,
        "sensor.old_meter",
        periods,
        "ha_dev_tools:x",
        unit="EUR",
        start=START + timedelta(hours=1),
        can_back_up=False,
    )
    # hour 1: 1.5 x 10, hour 2: 1.5 x 1 - hour 0 left out by start.
    assert [row["sum"] for row in plan.rows] == [15, 16.5]
    assert plan.preview()["factor"]["periods"][0]["value"] == 10

    with pytest.raises(sm.StatisticsChangeRefusedError, match="no rate for the hours"):
        await plan_derive(
            hass,
            "sensor.old_meter",
            periods[:1],
            "ha_dev_tools:x",
            unit="EUR",
            can_back_up=False,
        )
    with pytest.raises(sm.StatisticsChangeRefusedError, match="each rate period"):
        await plan_derive(
            hass,
            "sensor.old_meter",
            [{"from": "soon"}],
            "ha_dev_tools:x",
            unit="EUR",
            can_back_up=False,
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_hourly_price_with_a_gap_and_unit_conversion(hass: HomeAssistant):
    await _seed(hass)
    await _meter(hass, "sensor.wh_meter", 0, [0, 1000, 3000, 6000], unit="Wh")
    await _price(hass, {0: 0.2, 1: 0.3})  # hours 2 and 3 carry 0.3

    plan = await plan_derive(
        hass,
        "sensor.wh_meter",
        "sensor.price",
        "ha_dev_tools:cost",
        can_back_up=False,
    )
    # Wh converted to kWh for the EUR/kWh price: 1 x .3, 2 x .3, 3 x .3
    assert [round(row["sum"], 6) for row in plan.rows] == [0, 0.3, 0.9, 1.8]
    assert plan.metadata["unit_of_measurement"] == "EUR"
    assert plan.preview()["factor"] == {
        "kind": "price",
        "statistic_id": "sensor.price",
        "unit_of_measurement": "EUR/kWh",
        "hours_without_price": 2,
        "longest_gap_hours": 2,
    }

    # Without hour 0's price there's nothing to carry into the first hour.
    await sm.clear_statistics(hass, ["sensor.price"])
    await _price(hass, {1: 0.3})
    with pytest.raises(sm.StatisticsChangeRefusedError, match="no price for"):
        await plan_derive(
            hass,
            "sensor.wh_meter",
            "sensor.price",
            "ha_dev_tools:cost",
            can_back_up=False,
        )
    for bad, expected in (
        ("sensor.old_meter", "is a meter"),
        ("sensor.nope", "no statistic 'sensor.nope'"),
    ):
        with pytest.raises(sm.StatisticsChangeRefusedError, match=expected):
            await plan_derive(
                hass,
                "sensor.wh_meter",
                bad,
                "ha_dev_tools:cost",
                unit="EUR",
                can_back_up=False,
            )


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_replacing_a_range_of_a_live_cost_sensor(hass: HomeAssistant, freezer):
    """The issue's case: the dashboard's own compensation sensor, live,
    with fragments and a gap - replaced in a range, kept outside it, and
    its next compile continues from the new sum."""
    freezer.move_to(START + timedelta(hours=6, minutes=2))
    await _seed(hass)  # the meter: hours 0-2
    hass.states.async_set("sensor.comp", "9", EUR_METER)
    await async_wait_recording_done(hass)
    # Old fragment in hour 1, nothing in hour 2, own hours 4-5 later.
    async_import_statistics(
        hass,
        {
            **_metadata("sensor.comp", "recorder", "Comp"),
            "unit_class": None,
            "unit_of_measurement": "EUR",
        },
        [
            {"start": START + timedelta(hours=1), "state": 5, "sum": 5},
            {"start": START + timedelta(hours=4), "state": 7, "sum": 7},
            {"start": START + timedelta(hours=5), "state": 9, "sum": 9},
        ],
    )
    await async_wait_recording_done(hass)
    meta = (await sm.read_metadata(hass, ["sensor.comp"]))["sensor.comp"]
    get_instance(hass).async_import_statistics(
        dict(meta),
        [{"start": START + timedelta(hours=5, minutes=55), "state": 9.0, "sum": 9.0}],
        StatisticsShortTerm,
    )
    await async_wait_recording_done(hass)

    plan = await plan_derive(
        hass,
        "sensor.old_meter",
        2,
        "sensor.comp",
        end=START + timedelta(hours=3),
        can_back_up=False,
        allow_no_backup=True,
    )
    preview = plan.preview()
    assert preview["replaces"] == {"hours": 1, "total": 5.0, "difference": 1.0}
    result = await merge_statistics(hass, plan)
    await async_wait_recording_done(hass)

    # derived: 0, 3, 3 in hours 0-2; then the target's own +2 (hour 4) and
    # +2 (hour 5) - its first row's 5 is replaced.
    assert await _sums(hass, "sensor.comp") == [0, 3, 6, 8, 10]
    assert result["next_compile"]["ok"] is True
    assert await _latest_short_term_sum(hass, "sensor.comp") == 10.0

    freezer.tick(timedelta(minutes=5))
    hass.states.async_set("sensor.comp", "10", EUR_METER)
    freezer.tick(timedelta(minutes=5))
    await async_wait_recording_done(hass)
    await _compile(hass)
    assert await _latest_short_term_sum(hass, "sensor.comp") == pytest.approx(11)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.parametrize(
    ("source", "factor", "target", "kwargs", "expected"),
    [
        ("sensor.nope", 1, "ha_dev_tools:x", {"unit": "EUR"}, "no statistic"),
        ("sensor.temp", 1, "ha_dev_tools:x", {"unit": "EUR"}, "isn't a meter"),
        ("sensor.old_meter", 1, "sensor.old_meter", {}, "can't be the source"),
        (
            "sensor.old_meter",
            1,
            "ha_dev_tools:backup_x_20240101t000000",
            {},
            "a backup",
        ),
        ("sensor.old_meter", 1, "sensor.not_there", {}, "only under 'ha_dev_tools:'"),
        ("sensor.old_meter", 1, "sensor.temp", {}, "isn't a meter"),
        ("sensor.old_meter", 1, "sensor.live_meter", {"unit": "EUR"}, "not 'EUR'"),
        (
            "sensor.old_meter",
            1,
            "ha_dev_tools:x",
            {"unit": "EUR", "start": START + timedelta(hours=2), "end": START},
            "start must be before end",
        ),
        (
            "sensor.old_meter",
            1,
            "sensor.live_meter",
            {"can_back_up": False},
            "allow_no_backup",
        ),
        (
            "sensor.old_meter",
            1,
            "ha_dev_tools:x",
            {"unit": "EUR", "start": START + timedelta(days=9)},
            "no hourly rows in that range",
        ),
        ("sensor.old_meter", 1, "ha_dev_tools:x", {}, "pass unit"),
        ("sensor.old_meter", True, "ha_dev_tools:x", {"unit": "EUR"}, "factor is"),
        (
            "sensor.old_meter",
            1,
            "ha_dev_tools:x",
            {"unit": "EUR", "source_unit": "m³"},
            "can't be converted",
        ),
    ],
)
async def test_plan_derive_refusals(
    hass: HomeAssistant, source, factor, target, kwargs, expected
):
    await _seed(hass)
    async_import_statistics(
        hass,
        {
            **_metadata("sensor.temp", "recorder", "t"),
            "has_sum": False,
            "mean_type": StatisticMeanType.ARITHMETIC,
        },
        [{"start": START, "mean": 1, "min": 0, "max": 2}],
    )
    await async_wait_recording_done(hass)
    arguments = {"can_back_up": True, **kwargs}
    with pytest.raises(sm.StatisticsChangeRefusedError, match=expected):
        await plan_derive(hass, source, factor, target, **arguments)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_derive_statistics_tool(hass: HomeAssistant):
    from custom_components.ha_dev_tools.llm_api import DeriveStatisticsTool

    await _seed(hass)
    tool = DeriveStatisticsTool()
    new = {
        "source_statistic_id": "sensor.old_meter",
        "factor": 0.08,
        "target_statistic_id": "ha_dev_tools:export_compensation",
        "unit": "EUR",
        "name": "Export compensation",
    }
    tool.parameters(new)
    tool.parameters({**new, "factor": [{"from": "2024-01-01", "value": 0.08}]})
    tool.parameters({**new, "factor": "sensor.price"})
    preview = await tool._preview_context(
        hass, _input(tool.name, **new), _llm_context()
    )
    assert preview["would_derive"]["derived"]["total"] == 0.24
    created = await tool._write(hass, _input(tool.name, **new), _llm_context())
    assert created["derived"]["target"]["rows"] == 3
    assert created["derived"]["target"]["name"] == "Export compensation"
    assert "backups" not in created  # nothing existed to back up

    refused = await tool._preview_context(
        hass, _input(tool.name, **{**new, "start": "soon"}), _llm_context()
    )
    assert "start" in refused["problems"]
    assert (
        await tool._write(
            hass, _input(tool.name, **{**new, "start": "soon"}), _llm_context()
        )
    )["error_type"] == "ValueError"

    # Into the existing statistic: backed up first.
    write, (enabled, pushed) = _mirror()
    with enabled, pushed:
        replaced = await tool._write(
            hass, _input(tool.name, **{**new, "factor": 0.1}), _llm_context()
        )
    assert replaced["derived"]["replaces"]["total"] == 0.24
    assert replaced["backups"]["ha_dev_tools:export_compensation"].startswith(
        "ha_dev_tools:backup_ha_dev_tools_export_compensation_"
    )
    assert json.loads(write.await_args.kwargs["content_after"])["statistics"]
    assert await _sums(hass, "ha_dev_tools:export_compensation") == [0, 0.15, 0.3]
