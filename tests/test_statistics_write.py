"""Tests for write_statistics (issue #166), against a real recorder -
fixture pattern as in test_statistics_redistribute.py."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ha_dev_tools import statistics_manager as sm
from custom_components.ha_dev_tools.statistics_write import (
    parse_rows,
    plan_write,
    write_statistics,
)
from tests.test_statistics_manager import _llm_context, _metadata, _mirror
from tests.test_statistics_merge import _compile, _latest_short_term_sum
from tests.test_statistics_redistribute import (
    T0,
    H,
    _at,
    _changes,
    _hourly,
    _input,
    _short,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Shadow conftest.py's fixture - see test_history_manager.py."""
    yield


def _rows(changes: dict[int, float]) -> list[dict]:
    return [
        {"start": _at(hour).isoformat(), "change": change}
        for hour, change in changes.items()
    ]


async def _plan(hass, statistic_id, rows, **kwargs):
    return await plan_write(
        hass, statistic_id, rows, can_back_up=False, allow_no_backup=True, **kwargs
    )


async def _short_sums(hass, statistic_id) -> dict[float, float]:
    rows = (await sm.read_rows(hass, [statistic_id], StatisticsShortTerm)).get(
        statistic_id, []
    )
    return {row["start_ts"]: round(row["sum"], 6) for row in rows}


# sensor.m: hours 0-9, 1 an hour (sums 1..10 from a 0 at hour 0).
METER = {hour: float(hour) for hour in range(10)}


# --- import into a meter ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_import_replaces_the_range_and_the_sums_carry_on(
    hass: HomeAssistant, freezer
):
    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", METER)
    # Hours 3-5: 5 and 2 given, hour 4 not - it counts 0.
    plan = await _plan(hass, "sensor.m", _rows({3: 5, 5: 2}), start=_at(3), end=_at(6))
    report = plan.preview()
    assert report["rows_written"] == 3
    assert (report["total_before"], report["total_after"]) == (3.0, 7.0)
    assert report["seams"]["start"]["sum_before"] == 2.0
    assert report["seams"]["end"]["offset"] == 4.0
    assert report["seams"]["end"]["rows_moved"] == 4
    assert report["by_month"][0]["difference"] == 4.0
    assert report["hours_set_to_zero"] == {
        "count": 1,
        "first": _at(4).isoformat(),
        "last": _at(4).isoformat(),
    }

    await write_statistics(hass, plan)
    assert await _changes(hass, "sensor.m", range(1, 10)) == [1, 1, 5, 0, 2, 1, 1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_adjust_sets_single_hours(hass: HomeAssistant, freezer):
    """The issue's spike: one hour set, every later sum moves, the hours
    between keep their own change."""
    freezer.move_to(_at(30))
    spiky = {**METER, **{hour: METER[hour] + 5000 for hour in range(4, 10)}}
    await _hourly(hass, "sensor.m", spiky)
    plan = await _plan(hass, "sensor.m", _rows({4: 1, 7: 2}), mode="adjust")
    assert plan.report["range"] == {
        "start": _at(4).isoformat(),
        "end": _at(7).isoformat(),
    }
    await write_statistics(hass, plan)
    assert await _changes(hass, "sensor.m", range(1, 10)) == [1, 1, 1, 1, 1, 1, 2, 1, 1]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_meter_hour_going_down_needs_allow_negative(
    hass: HomeAssistant, freezer
):
    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", METER)
    with pytest.raises(sm.StatisticsChangeRefusedError) as refused:
        await _plan(hass, "sensor.m", _rows({3: -1, 5: -2}), mode="adjust")
    assert "2 hour(s) would go down" in str(refused.value)
    assert _at(3).isoformat() in str(refused.value)
    plan = await _plan(
        hass, "sensor.m", _rows({3: -1}), mode="adjust", allow_negative=True
    )
    assert plan.report["negative_hours"] == [_at(3).isoformat()]
    await write_statistics(hass, plan)
    assert await _changes(hass, "sensor.m", [3, 4]) == [-1, 1]


# --- 5-minute rows ------------------------------------------------------------


async def _recent_meter(hass, freezer):
    """sensor.m hours 0-5, and 5-minute rows for hours 3-5 matching them."""
    freezer.move_to(_at(6.5))
    await _hourly(hass, "sensor.m", {hour: float(hour) for hour in range(6)})
    await _short(
        hass,
        "sensor.m",
        {3 + index / 12: 2 + (index + 1) / 12 for index in range(36)},
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_5_minute_rows_are_split_evenly_or_as_given(hass: HomeAssistant, freezer):
    await _recent_meter(hass, freezer)
    plan = await _plan(
        hass,
        "sensor.m",
        [
            {"start": _at(3).isoformat(), "change": 12},
            {"start": _at(4).isoformat(), "change": 6, "five_minute": [0.5] * 12},
        ],
    )
    assert plan.report["five_minute_rows_written"] == 24
    assert plan.report["five_minute_from"] == _at(3).isoformat()
    await write_statistics(hass, plan)
    sums = await _short_sums(hass, "sensor.m")
    assert [sums[T0 + 3 * H + index * 300] for index in (0, 11)] == [3.0, 14.0]
    assert sums[T0 + 4 * H + 11 * 300] == 20.0
    # Hour 5's 5-minute rows moved with the seam (+16: hour 4 ended at 4,
    # now 20), in step with its hourly row - a live meter's next compile
    # carries on from them.
    assert sums[T0 + 5 * H + 11 * 300] == 21.0
    assert await _changes(hass, "sensor.m", [3, 4, 5]) == [12, 6, 1]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_5_minute_refusals(hass: HomeAssistant, freezer):
    await _recent_meter(hass, freezer)
    for rows, expected in (
        (
            [{"start": _at(4).isoformat(), "change": 6, "five_minute": [1] * 12}],
            "add up to 12",
        ),
        (
            [{"start": _at(1).isoformat(), "change": 1, "five_minute": [1 / 12] * 12}],
            "has no 5-minute rows for it",
        ),
        (
            [{"start": _at(4).isoformat(), "change": 1, "five_minute": [1] * 3}],
            "five_minute is 12 numbers",
        ),
    ):
        with pytest.raises(sm.StatisticsChangeRefusedError, match=expected):
            await _plan(hass, "sensor.m", rows)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_live_meter_goes_on_compiling_from_the_new_sum(
    hass: HomeAssistant, freezer
):
    from datetime import timedelta

    meter = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": "kWh",
    }
    from homeassistant.setup import async_setup_component

    # The sensor integration runs the 5-minute compile.
    assert await async_setup_component(hass, "sensor", {})
    freezer.move_to(_at(6) + timedelta(minutes=2))
    hass.states.async_set("sensor.live", "1005", meter)
    await async_wait_recording_done(hass)
    await _hourly(hass, "sensor.live", {hour: float(hour) for hour in range(6)})
    await _short(hass, "sensor.live", {5 + 11 / 12: 5.0})

    plan = await _plan(hass, "sensor.live", _rows({2: 10}))
    result = await write_statistics(hass, plan)
    assert result["next_compile"]["ok"] is True
    assert await _latest_short_term_sum(hass, "sensor.live") == pytest.approx(14)

    freezer.tick(timedelta(minutes=5))
    hass.states.async_set("sensor.live", "1006", meter)
    freezer.tick(timedelta(minutes=5))
    await async_wait_recording_done(hass)
    await _compile(hass)
    assert await _latest_short_term_sum(hass, "sensor.live") == pytest.approx(15)


# --- mean statistics and new statistics ----------------------------------------


async def _price(hass, means: dict[int, float]):
    async_import_statistics(
        hass,
        {
            **_metadata("sensor.price", "recorder", "Price"),
            "has_sum": False,
            "mean_type": StatisticMeanType.ARITHMETIC,
            "unit_class": None,
            "unit_of_measurement": "EUR/kWh",
        },
        [
            {"start": _at(hour), "mean": mean, "min": mean, "max": mean}
            for hour, mean in means.items()
        ],
    )
    await async_wait_recording_done(hass)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_mean_import_must_give_every_hour_it_replaces(
    hass: HomeAssistant, freezer
):
    freezer.move_to(_at(30))
    await _price(hass, {0: 0.2, 1: 0.3, 2: 0.4})
    rows = [
        {"start": _at(0).isoformat(), "mean": 0.25},
        {"start": _at(2).isoformat(), "mean": 0.45, "min": 0.4, "max": 0.5},
    ]
    with pytest.raises(sm.StatisticsChangeRefusedError, match="leaves out 1 hour"):
        await _plan(hass, "sensor.price", rows)
    rows.insert(1, {"start": _at(1).isoformat(), "mean": 0.35})
    rows.append({"start": _at(3).isoformat(), "mean": 0.5})
    plan = await _plan(hass, "sensor.price", rows)
    assert (plan.report["hours_replaced"], plan.report["hours_added"]) == (3, 1)
    await write_statistics(hass, plan)
    stored = (await sm.read_rows(hass, ["sensor.price"]))["sensor.price"]
    assert [(r["mean"], r["min"], r["max"]) for r in stored] == [
        (0.25, 0.25, 0.25),
        (0.35, 0.35, 0.35),
        (0.45, 0.4, 0.5),
        (0.5, 0.5, 0.5),
    ]
    with pytest.raises(sm.StatisticsChangeRefusedError, match="min <= mean <= max"):
        await _plan(
            hass, "sensor.price", [{"start": _at(0).isoformat(), "mean": 1, "max": 0.5}]
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_new_statistics(hass: HomeAssistant, freezer):
    freezer.move_to(_at(30))
    fees = await _plan(
        hass,
        "ha_dev_tools:base_fees",
        _rows({0: 0.5, 1: 0.5, 2: 0.5}),
        kind="sum",
        unit="EUR",
        name="Base fees",
    )
    assert fees.target == {"statistic_id": "ha_dev_tools:base_fees", "new": True}
    await write_statistics(hass, fees)
    assert [
        r["sum"]
        for r in (await sm.read_rows(hass, ["ha_dev_tools:base_fees"]))[
            "ha_dev_tools:base_fees"
        ]
    ] == [0.5, 1.0, 1.5]

    price = await _plan(
        hass,
        "ha_dev_tools:model_price",
        [{"start": _at(0).isoformat(), "mean": 0.31}],
        kind="mean",
        unit="EUR/kWh",
    )
    await write_statistics(hass, price)
    meta = (await sm.read_metadata(hass, ["ha_dev_tools:model_price"]))[
        "ha_dev_tools:model_price"
    ]
    assert (
        meta["has_sum"] is False and meta["mean_type"] == StatisticMeanType.ARITHMETIC
    )


# --- refusals -------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.parametrize(
    ("statistic_id", "rows", "kwargs", "expected"),
    [
        (
            "sensor.m",
            [{"start": "2024-12-01T00:30:00+00:00", "change": 1}],
            {},
            "start of an hour",
        ),
        ("sensor.m", [{"start": "soon", "change": 1}], {}, "ISO 8601"),
        ("sensor.m", _rows({1: 1}) * 2, {}, "given twice"),
        (
            "sensor.m",
            [{"start": _at(1).isoformat(), "change": True}],
            {},
            "finite number",
        ),
        (
            "sensor.m",
            [{"start": _at(1).isoformat(), "change": 1, "mean": 2}],
            {},
            "isn't a sum row's",
        ),
        (
            "sensor.m",
            [{"start": _at(40).isoformat(), "change": 1}],
            {},
            "isn't complete yet",
        ),
        ("sensor.m", _rows({1: 1}), {"end": _at(40)}, "after the last complete hour"),
        (
            "sensor.m",
            _rows({8: 1}),
            {"start": _at(1), "end": _at(3)},
            "outside start..end",
        ),
        ("sensor.m", _rows({1: 1}), {"kind": "mean"}, "is a sum statistic, not mean"),
        ("sensor.m", _rows({1: 1}), {"unit": "m³"}, "is in 'kWh', not 'm³'"),
        ("sensor.nope", _rows({1: 1}), {"mode": "adjust"}, "adjust needs one"),
        ("sensor.nope", _rows({1: 1}), {}, "created only under"),
        ("ha_dev_tools:x", _rows({1: 1}), {}, "needs kind and unit"),
        ("sensor.m", [], {}, "no rows given"),
        ("sensor.m", _rows({1: 1}), {"mode": "replace"}, "mode is one of"),
    ],
)
async def test_refusals(
    hass: HomeAssistant, freezer, statistic_id, rows, kwargs, expected
):
    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", METER)
    with pytest.raises(sm.StatisticsChangeRefusedError, match=expected):
        await _plan(hass, statistic_id, rows, **kwargs)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_every_row_problem_is_listed(hass: HomeAssistant, freezer):
    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", METER)
    with pytest.raises(sm.StatisticsChangeRefusedError) as refused:
        await _plan(
            hass,
            "sensor.m",
            [
                {"start": "2024-12-01T00:30:00+00:00", "change": 1},
                {"start": _at(2).isoformat(), "change": "x"},
                {"start": _at(40).isoformat(), "change": 1},
            ],
        )
    message = str(refused.value)
    assert "row 0" in message and "row 1" in message and "row 2" in message


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_an_existing_statistic_needs_a_backup(hass: HomeAssistant, freezer):
    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", METER)
    with pytest.raises(sm.StatisticsChangeRefusedError, match="allow_no_backup"):
        await plan_write(hass, "sensor.m", _rows({1: 1}), can_back_up=False)


# --- files --------------------------------------------------------------------------


def test_parse_rows_from_csv_and_json():
    csv_text = (
        "start,change\n2024-12-01T01:00:00+00:00,1.5\n2024-12-01T02:00:00+00:00,\n"
    )
    assert parse_rows(csv_text, "fees.csv") == [
        {"start": "2024-12-01T01:00:00+00:00", "change": 1.5},
        {"start": "2024-12-01T02:00:00+00:00"},
    ]
    rows = [{"start": "2024-12-01T01:00:00+00:00", "mean": 0.3}]
    assert parse_rows(json.dumps({"rows": rows}), "price.json") == rows
    assert parse_rows(json.dumps(rows), "price.json") == rows
    for content, path, expected in (
        ("start,change\nx,abc\n", "a.csv", "line 2: change 'abc'"),
        ("not json", "a.json", "neither CSV"),
        ('{"other": 1}', "a.json", "a list of rows"),
    ):
        with pytest.raises(sm.StatisticsChangeRefusedError, match=expected):
            parse_rows(content, path)


# --- the tool -----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_write_statistics_tool(hass: HomeAssistant, freezer):
    from custom_components.ha_dev_tools.llm_api import WriteStatisticsTool

    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", METER)
    tool = WriteStatisticsTool()
    args = {"statistic_id": "sensor.m", "rows": _rows({3: 4})}
    tool.parameters(args)
    preview = await tool._preview_context(
        hass, _input(tool.name, **args, allow_no_backup=True), _llm_context()
    )
    assert preview["would_write"]["total_after"] == 4
    both = await tool._preview_context(
        hass, _input(tool.name, **args, file="x.csv"), _llm_context()
    )
    assert "exactly one of rows or file" in both["problems"]

    write, (enabled, pushed) = _mirror()
    with enabled, pushed:
        result = await tool._write(hass, _input(tool.name, **args), _llm_context())
    assert result["written"]["rows_written"] == 1
    assert result["backups"]["sensor.m"].startswith("ha_dev_tools:backup_sensor_m_")
    assert await _changes(hass, "sensor.m", [3, 4]) == [4, 1]

    # From a file in the mirror repo.
    csv_text = f"start,change\n{_at(5).isoformat()},3\n"
    with (
        enabled,
        pushed,
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.read_mirrored",
            AsyncMock(return_value=csv_text),
        ) as read,
    ):
        result = await tool._write(
            hass,
            _input(tool.name, statistic_id="sensor.m", file="analysis/fix.csv"),
            _llm_context(),
        )
    read.assert_awaited_once_with(hass, "analysis/fix.csv")
    assert await _changes(hass, "sensor.m", [5, 6]) == [3, 1]
    missing = await tool._preview_context(
        hass,
        _input(tool.name, statistic_id="sensor.m", file="nope.csv"),
        _llm_context(),
    )
    assert "mirroring is off" in missing["problems"]


# --- more paths ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_adjust_keeps_the_5_minute_shape_of_the_hours_between(
    hass: HomeAssistant, freezer
):
    """An hour adjust mode doesn't set keeps its own 5-minute changes,
    just moved to its new sum."""
    await _recent_meter(hass, freezer)
    # Hour 4 gets an uneven shape: all of its 1 in the last slot.
    await _short(
        hass, "sensor.m", {4 + index / 12: 3.0 + (index == 11) for index in range(12)}
    )
    plan = await _plan(hass, "sensor.m", _rows({3: 4, 5: 1}), mode="adjust")
    await write_statistics(hass, plan)
    sums = await _short_sums(hass, "sensor.m")
    hour4 = [sums[T0 + 4 * H + index * 300] for index in range(12)]
    assert hour4 == [6.0] * 11 + [7.0]  # +3 from hour 3, shape intact
    assert await _changes(hass, "sensor.m", [3, 4, 5]) == [4, 1, 1]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_mean_import_writes_its_5_minute_rows(hass: HomeAssistant, freezer):
    freezer.move_to(_at(6.5))
    await _price(hass, {4: 0.2, 5: 0.3})
    meta = (await sm.read_metadata(hass, ["sensor.price"]))["sensor.price"]
    from homeassistant.components.recorder import get_instance

    get_instance(hass).async_import_statistics(
        dict(meta),
        [
            {"start": _at(4 + index / 12), "mean": 0.2, "min": 0.2, "max": 0.2}
            for index in range(24)
        ],
        StatisticsShortTerm,
    )
    await async_wait_recording_done(hass)
    plan = await _plan(
        hass,
        "sensor.price",
        [
            {"start": _at(4).isoformat(), "mean": 0.25},
            {
                "start": _at(5).isoformat(),
                "mean": 0.3,
                "five_minute": [0.3] * 11 + [0.6],
            },
        ],
    )
    assert plan.report["five_minute_rows_written"] == 24
    await write_statistics(hass, plan)
    rows = (await sm.read_rows(hass, ["sensor.price"], StatisticsShortTerm))[
        "sensor.price"
    ]
    assert {row["mean"] for row in rows[:12]} == {0.25}
    assert rows[-1]["mean"] == rows[-1]["max"] == 0.6


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_import_from_the_series_first_row(hass: HomeAssistant, freezer):
    """No row before the range: the first hour's change is its own sum."""
    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", {0: 2.0, 1: 3.0, 2: 4.0})
    plan = await _plan(hass, "sensor.m", _rows({1: 5}), start=_at(0), end=_at(2))
    assert plan.report["total_before"] == 3.0  # hour 0's 2 and hour 1's 1
    await write_statistics(hass, plan)
    rows = (await sm.read_rows(hass, ["sensor.m"]))["sensor.m"]
    assert [row["sum"] for row in rows] == [0.0, 5.0, 6.0]
    assert rows[0]["state"] == 1002.0  # its own reading, kept


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.parametrize(
    ("statistic_id", "rows", "kwargs", "expected"),
    [
        ("sensor.m", ["x"], {}, "row 0 is an object"),
        (
            "sensor.price",
            [{"start": _at(1).isoformat(), "mean": 1, "min": "x"}],
            {},
            "min is a finite number",
        ),
        ("sensor.m", _rows({1: 1}), {"kind": "count"}, "kind is one of"),
        (
            "sensor.m",
            _rows({1: 1}),
            {"start": _at(3), "end": _at(2)},
            "start must be before end",
        ),
        (
            "sensor.price",
            [{"start": _at(1).isoformat(), "mean": 1}],
            {"mode": "adjust"},
            "adjust is for a meter",
        ),
        (
            "ha_dev_tools:new",
            [{"start": _at(1).isoformat(), "change": 1, "five_minute": [1 / 12] * 12}],
            {"kind": "sum", "unit": "EUR"},
            "a new one has none",
        ),
        ("sensor.flat", _rows({1: 1}), {}, "neither a sum nor a mean"),
    ],
)
async def test_more_refusals(
    hass: HomeAssistant, freezer, statistic_id, rows, kwargs, expected
):
    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", METER)
    await _price(hass, {1: 0.3})
    async_import_statistics(
        hass,
        {
            **_metadata("sensor.flat", "recorder", "Flat"),
            "has_sum": False,
            "mean_type": StatisticMeanType.NONE,
            "unit_class": None,
            "unit_of_measurement": "x",
        },
        [{"start": _at(1), "state": 1}],
    )
    await async_wait_recording_done(hass)
    with pytest.raises(sm.StatisticsChangeRefusedError, match=expected):
        await _plan(hass, statistic_id, rows, **kwargs)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_backup_and_a_negative_five_minute_value_are_refused(
    hass: HomeAssistant, freezer
):
    from custom_components.ha_dev_tools import statistics_backup as backups

    await _recent_meter(hass, freezer)
    backup_id = "ha_dev_tools:backup_sensor_m_20241201t000000"
    with patch.object(backups, "is_backup", side_effect=lambda sid: sid == "sensor.m"):
        with pytest.raises(sm.StatisticsChangeRefusedError, match="a backup can't be"):
            await _plan(hass, "sensor.m", _rows({3: 1}))
    assert backup_id  # named as backups are
    with pytest.raises(
        sm.StatisticsChangeRefusedError, match="five_minute value is negative"
    ):
        await _plan(
            hass,
            "sensor.m",
            [
                {
                    "start": _at(4).isoformat(),
                    "change": 1,
                    "five_minute": [-1] + [2 / 11] * 11,
                }
            ],
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_write_statistics_tool_refusals(hass: HomeAssistant, freezer):
    from custom_components.ha_dev_tools import llm_api
    from custom_components.ha_dev_tools.llm_api import WriteStatisticsTool

    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", METER)
    tool = WriteStatisticsTool()
    write, (enabled, pushed) = _mirror()
    with (
        enabled,
        pushed,
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.read_mirrored",
            AsyncMock(return_value=None),
        ),
    ):
        missing = await tool._preview_context(
            hass,
            _input(tool.name, statistic_id="sensor.m", file="nope.csv"),
            _llm_context(),
        )
        refused = await tool._write(
            hass,
            _input(tool.name, statistic_id="sensor.m", rows=_rows({3: -1})),
            _llm_context(),
        )
    assert "has no 'nope.csv'" in missing["problems"]
    assert "would go down" in refused["error"]
    hass.data.setdefault(llm_api._STATISTICS_WRITES_RUNNING, set()).add("sensor.m")
    busy = await tool._write(
        hass,
        _input(tool.name, statistic_id="sensor.m", rows=_rows({3: 1})),
        _llm_context(),
    )
    assert "still" in json.dumps(busy).lower() or "busy" in json.dumps(busy).lower()
