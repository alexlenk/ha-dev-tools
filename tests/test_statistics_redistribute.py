"""Tests for redistribute_statistics (issue #155), against a real recorder -
fixture pattern as in test_statistics_manager.py."""

from datetime import timedelta

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ha_dev_tools import statistics_manager as sm
from custom_components.ha_dev_tools.statistics_redistribute import (
    detect_windows,
    plan_redistribute,
    redistribute_statistics,
)
from tests.test_statistics_manager import START, _llm_context, _metadata, _mirror

H = 3600.0
T0 = START.timestamp()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Shadow conftest.py's fixture - see test_history_manager.py."""
    yield


def _input(tool_name: str, **args):
    from homeassistant.helpers import llm

    return llm.ToolInput(tool_name=tool_name, tool_args=args)


def _at(hour: float):
    return START + timedelta(hours=hour)


def _meta(statistic_id: str, unit: str = "kWh") -> dict:
    return {
        **_metadata(statistic_id, "recorder", statistic_id),
        "unit_of_measurement": unit,
        "unit_class": "energy" if unit in ("kWh", "Wh") else "volume",
    }


async def _hourly(hass, statistic_id: str, sums: dict[int, float], unit="kWh"):
    """Hourly rows at the given hours only - a missing hour is a gap."""
    async_import_statistics(
        hass,
        _meta(statistic_id, unit),
        [
            {"start": _at(hour), "state": 1000 + value, "sum": value}
            for hour, value in sorted(sums.items())
        ],
    )
    await async_wait_recording_done(hass)


async def _short(hass, statistic_id: str, sums: dict[float, float]):
    meta = (await sm.read_metadata(hass, [statistic_id]))[statistic_id]
    get_instance(hass).async_import_statistics(
        dict(meta),
        [
            {"start": _at(hour), "state": 1000 + value, "sum": value}
            for hour, value in sorted(sums.items())
        ],
        StatisticsShortTerm,
    )
    await async_wait_recording_done(hass)


def _cumulative(changes: dict[int, float]) -> dict[int, float]:
    total, sums = 0.0, {}
    for hour in sorted(changes):
        total += changes[hour]
        sums[hour] = total
    return sums


async def _changes(hass, statistic_id: str, hours) -> list[float]:
    rows = {
        row["start_ts"]: row["sum"]
        for row in (await sm.read_rows(hass, [statistic_id]))[statistic_id]
    }
    return [round(rows[T0 + h * H] - rows[T0 + (h - 1) * H], 6) for h in hours]


def _solar(hour: int) -> float:
    """A daylight shape: 0 at night, peaking at noon (UTC, for simplicity)."""
    local = hour % 24
    return float(min(local - 5, 18 - local)) if 6 <= local <= 17 else 0.0


# --- a reference meter --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_reference_meter_shapes_the_window_exactly(
    hass: HomeAssistant, freezer
):
    """The export case: the reader was silent hours 24-40 and booked them
    all into hour 41; an inverter's own counter recorded the real hours."""
    freezer.move_to(_at(50))
    truth = {hour: _solar(hour) for hour in range(48)}
    true_sums = _cumulative(truth)
    await _hourly(hass, "sensor.deye_sold", true_sums)
    await _hourly(
        hass,
        "sensor.export",
        {hour: value for hour, value in true_sums.items() if not 24 <= hour <= 40},
    )

    plan = await plan_redistribute(
        hass,
        "sensor.export",
        windows=[{"start": _at(24), "catchup_hour": _at(41)}],
        reference_ids=["sensor.deye_sold"],
        can_back_up=False,
        allow_no_backup=True,
    )
    [window] = plan.report["windows"]
    assert window["moved"] == sum(truth[hour] for hour in range(24, 42))
    assert window["methods"] == {"reference:sensor.deye_sold": 18}
    assert window["references"][0]["ratio_to_moved"] == 1.0
    assert window["quality"][0]["mean_abs_error"] == 0
    assert window["max_hour_before"] == window["moved"]
    assert window["max_hour_after"] == 6
    assert "warnings" not in window

    await redistribute_statistics(hass, plan)
    assert await _changes(hass, "sensor.export", range(1, 48)) == [
        truth[hour] for hour in range(1, 48)
    ]
    # Readings follow the sums; the catch-up hour keeps its own.
    rows = {
        row["start_ts"]: row
        for row in (await sm.read_rows(hass, ["sensor.export"]))["sensor.export"]
    }
    assert rows[T0 + 30 * H]["state"] == pytest.approx(1000 + true_sums[30])
    assert rows[T0 + 41 * H]["sum"] == true_sums[41]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_far_off_or_flat_reference_is_warned_about(
    hass: HomeAssistant, freezer
):
    freezer.move_to(_at(80))
    await _hourly(
        hass, "sensor.meter", {0: 0, 1: 1, **{hour: 1 for hour in range(2, 40)}, 40: 41}
    )
    # A flat 2/hour "estimate", 40 hours: twice the 40 moved.
    await _hourly(hass, "sensor.cloud", {hour: 2.0 * hour for hour in range(0, 41)})
    plan = await plan_redistribute(
        hass,
        "sensor.meter",
        windows=[{"start": _at(2), "catchup_hour": _at(40)}],
        reference_ids=["sensor.cloud"],
        can_back_up=False,
        allow_no_backup=True,
    )
    warnings = " ".join(plan.report["windows"][0]["warnings"])
    assert "measured 195 %" in warnings
    assert "suspiciously constant" in warnings


# --- a profile, and detection -------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_detected_windows_shaped_by_the_meters_own_week(
    hass: HomeAssistant, freezer
):
    """Three weeks of a daily pattern, two outages in the middle week."""
    await hass.config.async_update(time_zone="UTC")
    freezer.move_to(_at(24 * 21 + 2))
    truth = {hour: _solar(hour) + 0.5 for hour in range(24 * 21)}
    true_sums = _cumulative(truth)
    silent = set(range(24 * 8, 24 * 8 + 10)) | set(range(24 * 10 + 3, 24 * 10 + 20))
    await _hourly(
        hass,
        "sensor.import",
        {hour: value for hour, value in true_sums.items() if hour not in silent},
    )

    series_rows = (await sm.read_rows(hass, ["sensor.import"]))["sensor.import"]
    from custom_components.ha_dev_tools.statistics_redistribute import Series

    found = detect_windows(Series("x", series_rows), min_silent_hours=6, min_catchup=5)
    assert [(w.start, w.catchup) for w in found] == [
        (T0 + 24 * 8 * H, T0 + (24 * 8 + 10) * H),
        (T0 + (24 * 10 + 3) * H, T0 + (24 * 10 + 20) * H),
    ]

    plan = await plan_redistribute(
        hass,
        "sensor.import",
        detect={"min_silent_hours": 6, "min_catchup": 5},
        profile_weeks=1,
        can_back_up=False,
        allow_no_backup=True,
    )
    assert [window["methods"] for window in plan.report["windows"]] == [
        {"profile": 11},
        {"profile": 18},
    ]
    await redistribute_statistics(hass, plan)
    hours = sorted(silent | {24 * 8 + 10, 24 * 10 + 20})
    assert await _changes(hass, "sensor.import", hours) == pytest.approx(
        [truth[hour] for hour in hours]
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_merged_target_moves_only_its_stalled_components_catchup(
    hass: HomeAssistant, freezer
):
    """grid_import = meter a + meter b; a stalled hours 10-15 and caught up
    at 16. In the target that's not a run of zeros - b went on - so the
    window is found on a, and only a's catch-up is spread."""
    freezer.move_to(_at(30))
    a = {hour: 1.0 for hour in range(21)}
    b = {hour: 2.0 for hour in range(21)}
    a_sums = _cumulative(a)
    await _hourly(
        hass, "sensor.a", {h: v for h, v in a_sums.items() if not 10 <= h <= 15}
    )
    recorded_a = {h: 0 if 10 <= h <= 15 else 7 if h == 16 else 1 for h in a}
    await _hourly(
        hass, "sensor.total", _cumulative({h: recorded_a[h] + b[h] for h in b})
    )
    target_before = await _changes(hass, "sensor.total", range(10, 17))
    assert target_before == [2, 2, 2, 2, 2, 2, 9]

    plan = await plan_redistribute(
        hass,
        "sensor.total",
        detect={"min_silent_hours": 3, "min_catchup": 3},
        detect_on="sensor.a",
        can_back_up=False,
        allow_no_backup=True,
    )
    [window] = plan.report["windows"]
    assert window["moved"] == 7
    assert "spread evenly" in window["warnings"][0]
    await redistribute_statistics(hass, plan)
    assert await _changes(hass, "sensor.total", range(1, 21)) == [3.0] * 20


# --- values, at mixed resolution ----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_values_mixed_hourly_and_5_minute(hass: HomeAssistant, freezer):
    """The window's first hours are past the 5-minute retention: one value
    each; the rest has 5-minute rows: 12 each - rewritten too."""
    freezer.move_to(_at(30))
    hourly = {hour: float(hour + 1) for hour in range(22)}  # 1/hour up to 22
    hourly[26] = 27.0  # silent 22-25, catch-up of 5 at hour 26
    hourly.update({27: 28.0, 28: 29.0})
    await _hourly(hass, "sensor.m", hourly)
    # 5-minute rows from hour 24 on: a stale one, then hour 26's catch-up.
    await _short(
        hass,
        "sensor.m",
        {
            24: 22.0,
            **{26 + i / 12: 22.0 + (5.0 if i == 11 else 0) for i in range(12)},
            27 + 11 / 12: 28.0,
            28 + 11 / 12: 29.0,
        },
    )
    window = [{"start": _at(22), "catchup_hour": _at(26)}]

    with pytest.raises(
        sm.StatisticsChangeRefusedError, match="2 hourly and 36 5-minute"
    ):
        await plan_redistribute(
            hass,
            "sensor.m",
            windows=window,
            values=[1, 1, 1, 1, 1],
            can_back_up=False,
            allow_no_backup=True,
        )
    values = [0.5, 0.5] + [4 / 36] * 36
    plan = await plan_redistribute(
        hass,
        "sensor.m",
        windows=window,
        values=values,
        can_back_up=False,
        allow_no_backup=True,
    )
    assert plan.report["windows"][0]["values_needed"] == {
        "hourly": 2,
        "five_minute": 36,
        "five_minute_from": _at(24).isoformat(),
    }
    result = await redistribute_statistics(hass, plan)
    assert await _changes(hass, "sensor.m", range(22, 28)) == pytest.approx(
        [0.5, 0.5, 4 / 3, 4 / 3, 4 / 3, 1.0]
    )
    short = {
        row["start_ts"]: row["sum"]
        for row in (await sm.read_rows(hass, ["sensor.m"], StatisticsShortTerm))[
            "sensor.m"
        ]
    }
    assert len([start for start in short if T0 + 24 * H <= start < T0 + 27 * H]) == 36
    assert short[T0 + 24 * H] == pytest.approx(23 + 4 / 36)
    assert short[T0 + 26 * H + 55 * 60] == 27.0  # where the next compile goes on
    assert result["next_compile"]["ok"] is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_values_sparse_and_normalized(hass: HomeAssistant, freezer):
    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", {0: 0, 1: 1, 9: 10, 10: 11})
    window = [{"start": _at(2), "catchup_hour": _at(9)}]
    sparse = [
        {"start": _at(3).isoformat(), "value": 4.0},
        {"start": _at(9).isoformat(), "value": 5.1},
    ]
    with pytest.raises(sm.StatisticsChangeRefusedError, match="pass normalize"):
        await plan_redistribute(
            hass,
            "sensor.m",
            windows=window,
            values=sparse,
            can_back_up=False,
            allow_no_backup=True,
        )
    plan = await plan_redistribute(
        hass,
        "sensor.m",
        windows=window,
        values=sparse,
        normalize=True,
        can_back_up=False,
        allow_no_backup=True,
    )
    assert plan.report["windows"][0]["normalized_by"] == pytest.approx(9 / 9.1)
    await redistribute_statistics(hass, plan)
    changes = await _changes(hass, "sensor.m", range(2, 11))
    assert changes[0] == 0 and changes[1] == pytest.approx(4 * 9 / 9.1)
    assert sum(changes[:8]) == pytest.approx(9)
    assert changes[8] == 1  # the hour after: untouched


# --- values for several windows (issue #159) ----------------------------------


async def _two_outages(hass):
    """Windows 2 -> 5 and 7 -> 10, each holding 5; 1 an hour elsewhere."""
    await _hourly(hass, "sensor.m", {0: 0, 1: 1, 5: 6, 6: 7, 10: 12, 11: 13})
    return [
        {"start": _at(2), "catchup_hour": _at(5), "values": [1, 2, 1, 1]},
        {"start": _at(7), "catchup_hour": _at(10), "values": [2, 1, 1, 1]},
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_values_for_several_windows_in_one_write(hass: HomeAssistant, freezer):
    freezer.move_to(_at(30))
    windows = await _two_outages(hass)

    plan = await plan_redistribute(
        hass, "sensor.m", windows=windows, can_back_up=False, allow_no_backup=True
    )
    reports = plan.report["windows"]
    assert [report["moved"] for report in reports] == [5, 5]
    assert [report["methods"] for report in reports] == [{"values": 4}] * 2
    # Compact: several windows, no detail.
    assert not any("by_hour" in report or "by_day" in report for report in reports)
    assert reports[0]["by_month"] and reports[0]["max_hour_after"] == 2
    assert "detail: true" in plan.report["detail"]
    detailed = await plan_redistribute(
        hass,
        "sensor.m",
        windows=windows,
        detail=True,
        can_back_up=False,
        allow_no_backup=True,
    )
    assert [len(report["by_hour"]) for report in detailed.report["windows"]] == [4, 4]
    assert "by_day" in detailed.report["windows"][1]
    assert "detail" not in detailed.report

    await redistribute_statistics(hass, plan)
    assert await _changes(hass, "sensor.m", range(2, 12)) == pytest.approx(
        [1, 2, 1, 1, 1, 2, 1, 1, 1, 1]
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_one_window_in_its_own_values_is_as_before(hass: HomeAssistant, freezer):
    """A single window with its own values: the full report, as with the
    top-level values."""
    freezer.move_to(_at(30))
    windows = await _two_outages(hass)
    plan = await plan_redistribute(
        hass, "sensor.m", windows=windows[:1], can_back_up=False, allow_no_backup=True
    )
    assert len(plan.report["windows"][0]["by_hour"]) == 4
    assert "detail" not in plan.report
    top = await plan_redistribute(
        hass,
        "sensor.m",
        windows=[{key: windows[0][key] for key in ("start", "catchup_hour")}],
        values=windows[0]["values"],
        can_back_up=False,
        allow_no_backup=True,
    )
    assert top.rows == plan.rows


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_every_failing_window_is_named_and_nothing_is_written(
    hass: HomeAssistant, freezer
):
    freezer.move_to(_at(30))
    windows = await _two_outages(hass)
    await _hourly(hass, "sensor.m", {13: 14, 16: 18, 17: 19})  # 14 -> 16 holds 4
    windows[0]["values"] = [1, 1, 1, 1.5]  # adds up to 4.5, not 5
    windows[1]["values"] = [-1, 3, 2, 1]
    windows.append({"start": _at(14), "catchup_hour": _at(16), "values": [1, 2, 1]})
    before = await sm.read_rows(hass, ["sensor.m"])

    with pytest.raises(sm.StatisticsChangeRefusedError) as refused:
        await plan_redistribute(
            hass, "sensor.m", windows=windows, can_back_up=False, allow_no_backup=True
        )
    message = str(refused.value)
    assert f"window {_at(2).isoformat()}: the values add up to 4.5" in message
    assert f"window {_at(7).isoformat()}: values can't be negative" in message
    assert _at(14).isoformat() not in message
    assert await sm.read_rows(hass, ["sensor.m"]) == before

    # A window refused on its own rows is named too, the others still checked.
    windows[2] = {"start": _at(12), "catchup_hour": _at(16), "values": [1] * 5}
    with pytest.raises(sm.StatisticsChangeRefusedError) as refused:
        await plan_redistribute(
            hass, "sensor.m", windows=windows, can_back_up=False, allow_no_backup=True
        )
    assert "goes down" not in str(refused.value)
    assert "values can't be negative" in str(refused.value)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"drop_values": 1}, "1 of 2 windows have values"),
        ({"top_values": [1]}, "at the top or in each window, not both"),
        ({"profile_weeks": 1}, "no profile with them"),
    ],
)
async def test_values_in_windows_refusals(
    hass: HomeAssistant, freezer, change, expected
):
    freezer.move_to(_at(30))
    windows = await _two_outages(hass)
    if "drop_values" in change:
        del windows[change["drop_values"]]["values"]
    kwargs = {}
    if "top_values" in change:
        kwargs["values"] = change["top_values"]
    if "profile_weeks" in change:
        kwargs["profile_weeks"] = change["profile_weeks"]
    with pytest.raises(sm.StatisticsChangeRefusedError, match=expected):
        await plan_redistribute(
            hass,
            "sensor.m",
            windows=windows,
            can_back_up=False,
            allow_no_backup=True,
            **kwargs,
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_redistribute_tool_several_windows_one_backup(
    hass: HomeAssistant, freezer
):
    from custom_components.ha_dev_tools.llm_api import RedistributeStatisticsTool

    freezer.move_to(_at(30))
    windows = await _two_outages(hass)
    args = {
        "statistic_id": "sensor.m",
        "windows": [
            {
                "start": item["start"].isoformat(),
                "catchup_hour": item["catchup_hour"].isoformat(),
                "values": item["values"],
            }
            for item in windows
        ],
    }
    tool = RedistributeStatisticsTool()
    tool.parameters({**args, "detail": True})
    preview = await tool._preview_context(
        hass,
        _input(tool.name, **args, detail=True, allow_no_backup=True),
        _llm_context(),
    )
    assert "by_hour" in preview["would_redistribute"]["windows"][0]

    write, (enabled, pushed) = _mirror()
    with enabled, pushed:
        result = await tool._write(hass, _input(tool.name, **args), _llm_context())
    assert list(result["backups"]) == ["sensor.m"]
    assert result["redistributed"]["rows_written"] == 8
    assert "by_hour" not in result["redistributed"]["windows"][0]
    assert await _changes(hass, "sensor.m", range(2, 12)) == pytest.approx(
        [1, 2, 1, 1, 1, 2, 1, 1, 1, 1]
    )


# --- refusals -------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, "exactly one of windows or detect"),
        (
            {
                "windows": [{"start": _at(2), "catchup_hour": _at(9)}],
                "values": [-1] + [1.25] * 6 + [2.5],
            },
            "can't be negative",
        ),
        (
            {"windows": [{"start": _at(2), "catchup_hour": _at(9)}], "max_per_hour": 1},
            "above max_per_hour",
        ),
        (
            {
                "windows": [
                    {"start": _at(2), "catchup_hour": _at(9)},
                    {"start": _at(5), "catchup_hour": _at(9)},
                ]
            },
            "overlap",
        ),
        (
            {"windows": [{"start": _at(2), "catchup_hour": _at(9)}] * 2, "values": [1]},
            "exactly one explicit window",
        ),
        ({"windows": [{"start": _at(12), "catchup_hour": _at(14)}]}, "goes down"),
        ({"windows": [{"start": _at(15), "catchup_hour": _at(19)}]}, "isn't complete"),
        (
            {
                "windows": [{"start": _at(2), "catchup_hour": _at(9)}],
                "detect_on": "sensor.gas",
            },
            "can't be converted",
        ),
        (
            {"windows": [{"start": _at(2.5), "catchup_hour": _at(9)}]},
            "start of an hour",
        ),
        (
            {"windows": [{"start": _at(2), "catchup_hour": _at(8)}]},
            "no row for the catch-up",
        ),
        ({"detect": {"min_silent_hours": 50, "min_catchup": 1}}, "no window found"),
    ],
)
async def test_plan_redistribute_refusals(
    hass: HomeAssistant, freezer, kwargs, expected
):
    freezer.move_to(_at(19.5))
    await _hourly(hass, "sensor.m", {0: 0, 1: 1, 9: 10, 10: 11, 11: 12, 13: 3, 14: 4})
    await _hourly(hass, "sensor.gas", {0: 0, 1: 1}, unit="m³")
    with pytest.raises(sm.StatisticsChangeRefusedError, match=expected):
        await plan_redistribute(
            hass, "sensor.m", can_back_up=False, allow_no_backup=True, **kwargs
        )


# --- the tool ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_redistribute_statistics_tool(hass: HomeAssistant, freezer):
    from custom_components.ha_dev_tools.llm_api import RedistributeStatisticsTool

    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", {0: 0, 1: 1, 9: 10, 10: 11})
    tool = RedistributeStatisticsTool()
    args = {
        "statistic_id": "sensor.m",
        "windows": [{"start": _at(2).isoformat(), "catchup_hour": _at(9).isoformat()}],
    }
    tool.parameters(args)
    tool.parameters(
        {"statistic_id": "x", "detect": {"min_silent_hours": 2, "min_catchup": 1}}
    )
    preview = await tool._preview_context(
        hass, _input(tool.name, **args, allow_no_backup=True), _llm_context()
    )
    assert preview["would_redistribute"]["windows"][0]["moved"] == 9
    assert "spread evenly" in preview["would_redistribute"]["windows"][0]["warnings"][0]
    refused = await tool._preview_context(
        hass,
        _input(
            tool.name,
            statistic_id="sensor.m",
            windows=[{"start": "soon", "catchup_hour": "x"}],
        ),
        _llm_context(),
    )
    assert "start" in refused["problems"]

    write, (enabled, pushed) = _mirror()
    with enabled, pushed:
        result = await tool._write(hass, _input(tool.name, **args), _llm_context())
    assert result["redistributed"]["rows_written"] == 8
    assert result["backups"]["sensor.m"].startswith("ha_dev_tools:backup_sensor_m_")
    assert await _changes(hass, "sensor.m", range(2, 11)) == pytest.approx(
        [9 / 8] * 8 + [1]
    )
    assert dt_util.parse_datetime(result["redistributed"]["windows"][0]["start"])


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_5_minute_rows_follow_the_references_5_minute_shape(
    hass: HomeAssistant, freezer
):
    freezer.move_to(_at(6))
    await _hourly(hass, "sensor.m", {0: 0, 1: 1, 3: 1 + 12, 4: 14})
    await _hourly(hass, "sensor.ref", {0: 0, 1: 1, 2: 7, 3: 13, 4: 14})
    # 5-minute rows from hour 2: the meter stalled, the reference didn't -
    # in hour 2 it only counted in the second half hour.
    await _short(hass, "sensor.m", {2: 1.0, 3 + 11 / 12: 13.0, 4 + 11 / 12: 14.0})
    await _short(
        hass,
        "sensor.ref",
        {
            1 + 11 / 12: 1.0,
            **{2 + i / 12: 1.0 + (i - 5 if i > 5 else 0) for i in range(12)},
            **{3 + i / 12: 7.0 + (i + 1) / 2 for i in range(12)},
        },
    )
    plan = await plan_redistribute(
        hass,
        "sensor.m",
        windows=[{"start": _at(2), "catchup_hour": _at(3)}],
        reference_ids=["sensor.ref"],
        can_back_up=False,
        allow_no_backup=True,
    )
    await redistribute_statistics(hass, plan)
    short = {
        round((row["start_ts"] - T0) / 300): row["sum"]
        for row in (await sm.read_rows(hass, ["sensor.m"], StatisticsShortTerm))[
            "sensor.m"
        ]
    }
    # Hour 2: flat for half an hour, then 1 per 5 minutes, as the reference.
    assert [round(short[24 + i] - 1, 6) for i in range(12)] == [
        0,
        0,
        0,
        0,
        0,
        0,
        1,
        2,
        3,
        4,
        5,
        6,
    ]
    assert short[35] == 7.0  # hour 2's end: the reference's 6 on top of 1
    assert short[47] == 13.0  # hour 3's last slot: the catch-up end, as it was


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_shape_fallbacks_and_reports(hass: HomeAssistant, freezer):
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", {0: 0, 1: 1, 9: 10})
    # A reference that measured nothing over the window: all weights 0.
    await _hourly(hass, "sensor.idle", {hour: 0 for hour in range(12)})
    async_add_external_statistics(
        hass,
        {
            **_metadata("ha_dev_tools:m_cost", "ha_dev_tools", "sensor.m × 0.3"),
            "unit_of_measurement": "EUR",
            "unit_class": None,
        },
        [{"start": START, "state": 0, "sum": 0}],
    )
    await async_wait_recording_done(hass)
    plan = await plan_redistribute(
        hass,
        "sensor.m",
        windows=[{"start": _at(2), "catchup_hour": _at(9)}],
        reference_ids=["sensor.idle"],
        can_back_up=False,
        allow_no_backup=True,
    )
    [window] = plan.report["windows"]
    assert "zero over the whole window" in window["warnings"][0]
    # One clean hour next to the window (hour 1): the meter 1, the reference 0.
    assert window["quality"] == [
        {"reference": "sensor.idle", "hours": 1, "mean_abs_error": 1.0, "ratio": 0.0}
    ]
    assert plan.report["derived_series"]["statistics"] == ["ha_dev_tools:m_cost"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.parametrize(
    ("statistic_id", "kwargs", "expected"),
    [
        (
            "sensor.m",
            {"values": [1] * 8, "profile_weeks": 1},
            "no profile with them",
        ),
        ("ha_dev_tools:backup_x_20260101t000000", {}, "a backup can't be edited"),
        ("sensor.temp", {}, "isn't a meter"),
        (
            "sensor.m",
            {"can_back_up": False, "allow_no_backup": False},
            "allow_no_backup",
        ),
        ("sensor.m", {"window": (9, 2)}, "must come after start"),
        ("sensor.m", {"window": (0, 9)}, "no row before"),
        ("sensor.r", {"window": (2, 4)}, "is reset at"),
        ("sensor.m", {"detect_on": "sensor.big"}, "would go down"),
        ("sensor.m", {"values": [{"start": "x", "value": 1}]}, "each value is"),
        (
            "sensor.m",
            {"values": [{"start": _at(1).isoformat(), "value": 1}]},
            "isn't one of the window's periods",
        ),
    ],
)
async def test_more_refusals(
    hass: HomeAssistant, freezer, statistic_id, kwargs, expected
):
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", {0: 0, 1: 1, 9: 10})
    await _hourly(hass, "sensor.big", {0: 0, 1: 1, 9: 30})
    async_import_statistics(
        hass,
        _meta("sensor.r"),
        [
            {"start": _at(1), "state": 1, "sum": 1},
            {"start": _at(3), "state": 0, "sum": 1, "last_reset": _at(3)},
            {"start": _at(4), "state": 2, "sum": 3, "last_reset": _at(3)},
        ],
    )
    async_import_statistics(
        hass,
        {
            **_meta("sensor.temp"),
            "has_sum": False,
            "mean_type": StatisticMeanType.ARITHMETIC,
            "unit_class": None,
            "unit_of_measurement": "°C",
        },
        [{"start": START, "mean": 1, "min": 1, "max": 1}],
    )
    async_add_external_statistics(
        hass,
        {
            **_metadata("ha_dev_tools:backup_x_20260101t000000", "ha_dev_tools", "x"),
            "unit_class": "energy",
        },
        [{"start": START, "state": 0, "sum": 0}],
    )
    await async_wait_recording_done(hass)
    start, catchup = kwargs.pop("window", (2, 9))
    arguments = {
        "windows": [{"start": _at(start), "catchup_hour": _at(catchup)}],
        "can_back_up": True,
        **kwargs,
    }
    with pytest.raises(sm.StatisticsChangeRefusedError, match=expected):
        await plan_redistribute(hass, statistic_id, **arguments)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_5_minute_rows_spread_evenly_without_a_5_minute_shape(
    hass: HomeAssistant, freezer
):
    from custom_components.ha_dev_tools.statistics_redistribute import (
        Series,
        Window,
        _quality,
    )

    assert detect_windows(Series("x", []), min_silent_hours=1, min_catchup=1) == []
    assert _quality(Series("a", []), Series("b", []), Window(T0, T0 + H)) == {
        "reference": "b",
        "hours": 0,
    }

    freezer.move_to(_at(6))
    await _hourly(hass, "sensor.m", {0: 0, 1: 1, 3: 13})
    # A reference with hourly rows only: no 5-minute shape to borrow.
    await _hourly(hass, "sensor.ref", {0: 0, 1: 1, 2: 7, 3: 13})
    await _short(hass, "sensor.m", {2: 1.0, 3 + 11 / 12: 13.0})
    plan = await plan_redistribute(
        hass,
        "sensor.m",
        windows=[{"start": _at(2), "catchup_hour": _at(3)}],
        reference_ids=["sensor.ref"],
        can_back_up=False,
        allow_no_backup=True,
    )
    hour_2 = [row for row in plan.short_term_rows if row["start_ts"] < T0 + 3 * H]
    assert [round(row["sum"], 6) for row in hour_2] == [
        round(1 + 0.5 * (i + 1), 6) for i in range(12)
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_redistribute_tool_detects_waits_its_turn_and_refuses(
    hass: HomeAssistant, freezer
):
    from custom_components.ha_dev_tools import llm_api
    from custom_components.ha_dev_tools.llm_api import RedistributeStatisticsTool

    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", {0: 0, 1: 1, 9: 10, 10: 11})
    tool = RedistributeStatisticsTool()
    detect = {
        "statistic_id": "sensor.m",
        "detect": {
            "min_silent_hours": 3,
            "min_catchup": 2,
            "start": _at(0).isoformat(),
            "end": _at(20).isoformat(),
        },
        "allow_no_backup": True,
    }
    preview = await tool._preview_context(
        hass, _input(tool.name, **detect), _llm_context()
    )
    assert preview["would_redistribute"]["windows"][0]["start"] == _at(2).isoformat()

    hass.data[llm_api._STATISTICS_WRITES_RUNNING] = {"sensor.m"}
    busy = await tool._write(hass, _input(tool.name, **detect), _llm_context())
    assert busy["error_type"] == "StatisticsBusyError"
    hass.data[llm_api._STATISTICS_WRITES_RUNNING] = set()

    refused = await tool._write(
        hass,
        _input(
            tool.name,
            **{**detect, "detect": {"min_silent_hours": 50, "min_catchup": 1}},
        ),
        _llm_context(),
    )
    assert "no window found" in refused["error"]


# --- second review --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_detection_with_a_start_keeps_the_whole_window(
    hass: HomeAssistant, freezer
):
    """An outage that began before detect.start: counted from the series'
    start, so its catch-up isn't squeezed into the hours after `start`."""
    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", {0: 0, 1: 1, 20: 21, 21: 22})
    plan = await plan_redistribute(
        hass,
        "sensor.m",
        detect={"min_silent_hours": 3, "min_catchup": 2, "start": _at(10)},
        can_back_up=False,
        allow_no_backup=True,
    )
    [window] = plan.report["windows"]
    assert (window["start"], window["catchup_hour"]) == (
        _at(2).isoformat(),
        _at(20).isoformat(),
    )
    # A catch-up before `start` isn't picked.
    with pytest.raises(sm.StatisticsChangeRefusedError, match="no window found"):
        await plan_redistribute(
            hass,
            "sensor.m",
            detect={"min_silent_hours": 3, "min_catchup": 2, "start": _at(21)},
            can_back_up=False,
            allow_no_backup=True,
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([float("nan")] * 8, "finite number"),
        ([float("inf")] + [0] * 7, "finite number"),
        ([True] * 8, "finite number"),
        ([{"start": _at(3).isoformat(), "value": float("nan")}], "each value is"),
        ([{"start": _at(3).isoformat(), "value": True}], "each value is"),
    ],
)
async def test_values_must_be_finite_numbers(
    hass: HomeAssistant, freezer, values, expected
):
    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", {0: 0, 1: 1, 9: 10})
    with pytest.raises(sm.StatisticsChangeRefusedError, match=expected):
        await plan_redistribute(
            hass,
            "sensor.m",
            windows=[{"start": _at(2), "catchup_hour": _at(9)}],
            values=values,
            can_back_up=False,
            allow_no_backup=True,
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_meter_cant_be_its_own_reference(hass: HomeAssistant, freezer):
    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", {0: 0, 1: 1, 9: 10})
    with pytest.raises(sm.StatisticsChangeRefusedError, match="its own reference"):
        await plan_redistribute(
            hass,
            "sensor.m",
            windows=[{"start": _at(2), "catchup_hour": _at(9)}],
            reference_ids=["sensor.m"],
            can_back_up=False,
            allow_no_backup=True,
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_values_are_compared_with_a_reference_not_shaped_by_it(
    hass: HomeAssistant, freezer
):
    freezer.move_to(_at(30))
    await _hourly(hass, "sensor.m", {0: 0, 1: 1, 4: 4})
    await _hourly(hass, "sensor.ref", {0: 0, 1: 1, 2: 1, 3: 3, 4: 4})
    plan = await plan_redistribute(
        hass,
        "sensor.m",
        windows=[{"start": _at(2), "catchup_hour": _at(4)}],
        values=[1, 1, 1],
        reference_ids=["sensor.ref"],
        can_back_up=False,
        allow_no_backup=True,
    )
    [window] = plan.report["windows"]
    assert window["methods"] == {"values": 3}
    assert window["reference_by_hour"] == {"sensor.ref": [0, 2, 1]}
    assert [hour["after"] for hour in window["by_hour"]] == [1, 1, 1]
    assert [hour["before"] for hour in window["by_hour"]] == [0, 0, 3]
    assert window["references"][0]["ratio_to_moved"] == 1.0
    assert window["quality"][0]["hours"] == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_an_ha_dev_tools_statistic_can_be_redistributed(
    hass: HomeAssistant, freezer
):
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    freezer.move_to(_at(30))
    async_add_external_statistics(
        hass,
        {
            **_metadata("ha_dev_tools:grid", "ha_dev_tools", "Grid"),
            "unit_class": "energy",
        },
        [
            {"start": _at(hour), "state": value, "sum": value}
            for hour, value in ((0, 0), (1, 1), (9, 10), (10, 11))
        ],
    )
    await async_wait_recording_done(hass)
    plan = await plan_redistribute(
        hass,
        "ha_dev_tools:grid",
        windows=[{"start": _at(2), "catchup_hour": _at(9)}],
        can_back_up=False,
        allow_no_backup=True,
    )
    result = await redistribute_statistics(hass, plan)
    assert await _changes(hass, "ha_dev_tools:grid", range(2, 11)) == pytest.approx(
        [9 / 8] * 8 + [1]
    )
    assert result["next_compile"] == {"applies": False}


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_live_meter_goes_on_compiling_and_nothing_outside_moves(
    hass: HomeAssistant, freezer
):
    """End to end on a live sensor within the 5-minute retention: the
    rows outside the window - hourly and 5-minute - stay exactly as they
    were, and HA's own next compiles continue without a step."""
    from homeassistant.setup import async_setup_component
    from pytest_homeassistant_custom_component.components.recorder.common import (
        do_adhoc_statistics,
    )

    meter = {
        "state_class": "total_increasing",
        "unit_of_measurement": "kWh",
        "device_class": "energy",
    }
    assert await async_setup_component(hass, "sensor", {})
    freezer.move_to(
        _at(
            12,
        )
        + timedelta(minutes=2)
    )
    hass.states.async_set("sensor.live", "1010", meter)
    await async_wait_recording_done(hass)
    # Hourly: 1/hour to hour 3, silent 4-8, catch-up of 6 at 9, then 10-11.
    sums = {h: float(h + 1) for h in range(4)} | {9: 10.0, 10: 11.0, 11: 12.0}
    async_import_statistics(
        hass,
        _meta("sensor.live"),
        [{"start": _at(h), "state": 1000 + v, "sum": v} for h, v in sums.items()],
    )
    await async_wait_recording_done(hass)
    # 5-minute rows from hour 2 on, as the recorder keeps them: hours 2-3,
    # nothing while silent, hour 9 with the spike, hours 10-11.
    short = {}
    for h in (2, 3, 9, 10, 11):
        start_sum = sums.get(h - 1, 3.0 if h == 9 else None) or sums[h] - 1
        for i in range(12):
            value = start_sum + (sums[h] - start_sum) * (i + 1) / 12
            short[h + i / 12] = value
    await _short(hass, "sensor.live", short)

    def snapshot(rows):
        return {
            row["start_ts"]: (row["sum"], row["state"])
            for row in rows
            if not T0 + 4 * H <= row["start_ts"] < T0 + 10 * H
        }

    hourly_before = snapshot((await sm.read_rows(hass, ["sensor.live"]))["sensor.live"])
    short_before = snapshot(
        (await sm.read_rows(hass, ["sensor.live"], StatisticsShortTerm))["sensor.live"]
    )

    plan = await plan_redistribute(
        hass,
        "sensor.live",
        windows=[{"start": _at(4), "catchup_hour": _at(9)}],
        can_back_up=False,
        allow_no_backup=True,
    )
    assert plan.report["windows"][0]["values_needed"]["five_minute"] == 72
    result = await redistribute_statistics(hass, plan)
    assert result["next_compile"]["ok"] is True

    assert (
        snapshot((await sm.read_rows(hass, ["sensor.live"]))["sensor.live"])
        == hourly_before
    )
    assert (
        snapshot(
            (await sm.read_rows(hass, ["sensor.live"], StatisticsShortTerm))[
                "sensor.live"
            ]
        )
        == short_before
    )
    assert await _changes(hass, "sensor.live", range(4, 12)) == pytest.approx([1.0] * 8)
    window_short = [
        row
        for row in (await sm.read_rows(hass, ["sensor.live"], StatisticsShortTerm))[
            "sensor.live"
        ]
        if T0 + 4 * H <= row["start_ts"] < T0 + 10 * H
    ]
    assert len(window_short) == 72
    sums_5 = [row["sum"] for row in window_short]
    assert all(b >= a for a, b in zip([4.0, *sums_5], sums_5))  # never down
    assert window_short[-1]["sum"] == 10.0 and window_short[-1]["state"] == 1010.0

    # HA's own compile goes on from the newest 5-minute row (hour 11's
    # last, reading 1012): the meter reads 1014, so +2 on top of 12.
    freezer.tick(timedelta(minutes=5))
    hass.states.async_set("sensor.live", "1014", meter)
    freezer.tick(timedelta(minutes=5))
    await async_wait_recording_done(hass)
    now = dt_util.utcnow()
    do_adhoc_statistics(
        hass, start=now.replace(minute=now.minute - now.minute % 5, second=0)
    )
    await async_wait_recording_done(hass)
    latest = (await sm.read_rows(hass, ["sensor.live"], StatisticsShortTerm))[
        "sensor.live"
    ][-1]
    assert latest["sum"] == pytest.approx(14.0)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_component_and_reference_in_wh_under_a_kwh_target(
    hass: HomeAssistant, freezer
):
    freezer.move_to(_at(30))
    a = {h: 0.0 if 2 <= h <= 4 else 4000.0 if h == 5 else 1000.0 for h in range(8)}
    await _hourly(
        hass, "sensor.a_wh", {h: v for h, v in _cumulative(a).items()}, unit="Wh"
    )
    await _hourly(
        hass, "sensor.total", _cumulative({h: a[h] / 1000 + 2 for h in range(8)})
    )
    await _hourly(
        hass,
        "sensor.ref_wh",
        _cumulative({h: 2000.0 if h == 3 else 1000.0 for h in range(8)}),
        unit="Wh",
    )
    plan = await plan_redistribute(
        hass,
        "sensor.total",
        windows=[{"start": _at(2), "catchup_hour": _at(5)}],
        detect_on="sensor.a_wh",
        reference_ids=["sensor.ref_wh"],
        can_back_up=False,
        allow_no_backup=True,
    )
    [window] = plan.report["windows"]
    assert window["moved"] == 4  # kWh, the target's unit
    assert window["references"][0]["ratio_to_moved"] == pytest.approx(5 / 4)
    await redistribute_statistics(hass, plan)
    # a's 4 kWh shaped 1:2:1:1 by the reference (scaled to 4), plus b's 2.
    assert await _changes(hass, "sensor.total", range(2, 8)) == pytest.approx(
        [2.8, 3.6, 2.8, 2.8, 3.0, 3.0]
    )
