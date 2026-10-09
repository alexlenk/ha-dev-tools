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
            "no references or profile",
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
