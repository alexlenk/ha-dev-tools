"""Tests for merge_statistics, in-recorder backups and restore_statistics
(issue #136), against a real recorder - fixture pattern as in
test_statistics_manager.py."""

import json
from datetime import timedelta

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_import_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.ha_dev_tools import statistics_backup as backups
from custom_components.ha_dev_tools import statistics_manager as sm
from custom_components.ha_dev_tools.statistics_merge import (
    Series,
    combine,
    merge_statistics,
    plan_merge,
    rebase_point,
)
from tests.test_statistics_manager import (
    START,
    _input,
    _llm_context,
    _metadata,
    _mirror,
    _seed,
)

H = 3600.0
T0 = START.timestamp()
METER = {
    "state_class": "total_increasing",
    "unit_of_measurement": "kWh",
    "device_class": "energy",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Shadow conftest.py's fixture - see test_history_manager.py."""
    yield


def _rows(first_hour: int, sums: list[float], states: list[float] | None = None):
    return [
        {
            "start_ts": T0 + (first_hour + index) * H,
            "sum": value,
            "state": (states or sums)[index],
            "last_reset_ts": None,
        }
        for index, value in enumerate(sums)
    ]


# --- the merge logic ---------------------------------------------------------


def test_sequential_meters_become_one_continuous_series():
    old = Series("sensor.old", _rows(0, [1, 3, 6], [101, 103, 106]))
    new = Series("sensor.new", _rows(5, [2, 4], [7, 9]), is_target=True)

    rows, report = combine(new, [old], has_sum=True, overlap="refuse")

    assert [row["sum"] for row in rows] == [1, 3, 6, 8, 10]
    assert [row["state"] for row in rows] == [101, 103, 106, 7, 9]
    assert report["overlaps"] == []
    [seam] = report["seams"]
    assert (seam["from"], seam["to"], seam["gap_hours"]) == (
        ["sensor.old"],
        ["sensor.new"],
        2.0,  # hours 3 and 4
    )
    assert seam["state_jump"] == -99
    assert report["totals"] == {"inputs": 10, "result": 10, "dropped_by_overlap": 0}
    # The target's rows moved up by a constant 6 - where HA's adjustment
    # takes over, so its 5-minute rows follow.
    assert rebase_point(new, rows, first_short_term=None, now=0) == (T0 + 5 * H, 6)


def test_overlap_rules_for_meters():
    target = Series("sensor.target", _rows(0, [1, 2, 3]), is_target=True)
    source = Series("sensor.source", _rows(1, [10, 20, 30], [50, 60, 70]))

    added, report = combine(target, [source], has_sum=True, overlap="add")
    assert [row["sum"] for row in added] == [1, 12, 23, 33]
    # The target's own state wherever it has a row.
    assert [row["state"] for row in added] == [1, 2, 3, 70]
    assert report["overlaps"][0]["hours"] == 2
    assert report["overlaps"][0]["resolution"] == "added"
    assert report["totals"]["dropped_by_overlap"] == 0

    target_wins, report = combine(target, [source], has_sum=True, overlap="target_wins")
    assert [row["sum"] for row in target_wins] == [1, 2, 3, 13]
    assert report["overlaps"][0]["resolution"] == "sensor.target wins"
    assert report["totals"]["dropped_by_overlap"] == 20

    source_wins, report = combine(target, [source], has_sum=True, overlap="source_wins")
    assert [row["sum"] for row in source_wins] == [1, 11, 21, 31]
    assert report["overlaps"][0]["series"] == ["sensor.source", "sensor.target"]

    # The rebase point is where the offset stops changing: the last hour.
    assert rebase_point(target, source_wins, first_short_term=None, now=0) == (
        T0 + 2 * H,
        18,
    )


def test_rebase_for_a_target_without_hourly_rows():
    """Issue #141: a target minutes old has no hourly rows, only its own
    5-minute ones counting from 0 - they move up by the whole merged total,
    from their first hour (or the current one, when there are none yet)."""
    empty = Series("sensor.new", [], is_target=True)
    merged = [{"start_ts": T0, "sum": 2.0}, {"start_ts": T0 + H, "sum": 5.0}]

    assert rebase_point(
        empty, merged, first_short_term=T0 + 30 * H + 50 * 60, now=0
    ) == (T0 + 30 * H, 5.0)
    assert rebase_point(empty, merged, first_short_term=None, now=T0 + 40 * H + 7) == (
        T0 + 40 * H,
        5.0,
    )
    assert rebase_point(empty, [], first_short_term=None, now=0) == (None, 0.0)


def test_several_sources_are_added_like_the_tibber_case():
    homes = [
        Series(f"tibber:home_{index}", _rows(0, [index, 2 * index]))
        for index in (1, 2, 3)
    ]
    target = Series("tibber:all", _rows(2, [5]), is_target=True)

    rows, report = combine(target, homes, has_sum=True, overlap="add")

    assert [row["sum"] for row in rows] == [6, 12, 17]
    assert "state" not in rows[0]
    assert len(report["overlaps"]) == 1
    assert report["totals"]["inputs"] == report["totals"]["result"] == 17


def test_measurements_are_taken_not_added():
    def means(first_hour, values):
        return [
            {
                "start_ts": T0 + (first_hour + index) * H,
                "mean": value,
                "min": value - 1,
                "max": value + 1,
            }
            for index, value in enumerate(values)
        ]

    target = Series("sensor.t", means(1, [20, 21]), is_target=True)
    source = Series("sensor.s", means(0, [10, 11]))

    rows, report = combine(target, [source], has_sum=False, overlap="target_wins")

    assert [(row["mean"], row["min"]) for row in rows] == [(10, 9), (20, 19), (21, 20)]
    assert "totals" not in report
    assert "state_jump" not in report["seams"][0]


# --- planning and writing against the recorder ---------------------------------


async def _meter(hass, statistic_id: str, first_hour: int, sums, unit="kWh"):
    async_import_statistics(
        hass,
        {
            **_metadata(statistic_id, "recorder", statistic_id),
            "unit_of_measurement": unit,
            "unit_class": "energy" if unit in ("kWh", "Wh") else "volume",
        },
        [
            {
                "start": START + timedelta(hours=first_hour + index),
                "state": value,
                "sum": value,
            }
            for index, value in enumerate(sums)
        ],
    )
    await async_wait_recording_done(hass)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.parametrize(
    ("target", "sources", "kwargs", "expected"),
    [
        ("sensor.typo", ["sensor.old_meter"], {}, "no statistic 'sensor.typo'"),
        ("sensor.old_meter", ["sensor.old_meter"], {}, "can't also be a source"),
        (
            "ha_dev_tools:backup_x_20260101t000000",
            ["sensor.old_meter"],
            {},
            "restore_statistics restores one",
        ),
        (
            "sensor.old_meter",
            ["sensor.wh_meter"],
            {"start": START + timedelta(hours=2), "end": START},
            "start must be before end",
        ),
        ("sensor.temperature", ["sensor.temperature_2"], {"overlap": "add"}, "add"),
        ("sensor.old_meter", ["sensor.temperature"], {}, "differ in kind"),
        ("sensor.old_meter", ["sensor.gas"], {}, "can't be converted to 'kWh'"),
        ("sensor.old_meter", ["sensor.live_meter"], {}, "these series overlap"),
        (
            "sensor.old_meter",
            ["sensor.wh_meter"],
            {"can_back_up": False},
            "allow_no_backup=true",
        ),
    ],
)
async def test_plan_merge_refusals(
    hass: HomeAssistant, target, sources, kwargs, expected
):
    await _seed(hass)
    await _meter(hass, "sensor.wh_meter", 10, [1000, 2000], unit="Wh")
    await _meter(hass, "sensor.gas", 10, [1, 2], unit="m³")
    for statistic_id in ("sensor.temperature", "sensor.temperature_2"):
        async_import_statistics(
            hass,
            {
                **_metadata(statistic_id, "recorder", statistic_id),
                "has_sum": False,
                "mean_type": StatisticMeanType.ARITHMETIC,
            },
            [{"start": START, "mean": 1, "min": 0, "max": 2}],
        )
    async_add_external_statistics(
        hass,
        _metadata("ha_dev_tools:backup_x_20260101t000000", "ha_dev_tools", "x"),
        [{"start": START, "state": 0, "sum": 0}],
    )
    await async_wait_recording_done(hass)
    arguments = {"can_back_up": True, **kwargs}
    with pytest.raises(sm.StatisticsChangeRefusedError, match=expected):
        await plan_merge(hass, target, sources, **arguments)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_merge_converts_units_and_limits_the_sources_range(hass: HomeAssistant):
    await _seed(hass)  # old_meter: kWh, hours 0-2, sums 0, 1.5, 3
    await _meter(hass, "sensor.wh_meter", 10, [1000, 2000, 4000], unit="Wh")

    plan = await plan_merge(
        hass,
        "sensor.old_meter",
        ["sensor.wh_meter"],
        start=START + timedelta(hours=11),
        can_back_up=True,
    )
    # Hour 10 is left out; hour 11 keeps its own change (1 kWh), not the
    # sum since the Wh series began.
    assert [row["sum"] for row in plan.rows] == [0, 1.5, 3, 4, 6]
    preview = plan.preview()
    assert preview["rows_written"] == 5
    assert "sum_shift" not in preview  # the target's own rows don't move

    await merge_statistics(hass, plan)
    await async_wait_recording_done(hass)
    rows = (await sm.read_rows(hass, ["sensor.old_meter"]))["sensor.old_meter"]
    assert [row["sum"] for row in rows] == [0, 1.5, 3, 4, 6]
    # Sources stay as they are.
    assert len((await sm.read_rows(hass, ["sensor.wh_meter"]))["sensor.wh_meter"]) == 3


async def _latest_short_term_sum(hass, statistic_id):
    rows = (await sm.read_rows(hass, [statistic_id], StatisticsShortTerm)).get(
        statistic_id, []
    )
    return rows[-1]["sum"] if rows else None


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_live_meter_continues_from_the_merged_sum(hass: HomeAssistant, freezer):
    """The #136 replacement case: a new reader ran in parallel and has its
    own history and 5-minute rows; its next compile must continue from the
    merged sum, not jump back."""
    freezer.move_to(START + timedelta(hours=5, minutes=2))
    await _seed(hass)  # old_meter: hours 0-2, sums 0, 1.5, 3, states 0-2
    hass.states.async_set("sensor.new_meter", "3", METER)
    await async_wait_recording_done(hass)
    await _meter(hass, "sensor.new_meter", 3, [1, 2])
    meta = (await sm.read_metadata(hass, ["sensor.new_meter"]))["sensor.new_meter"]
    get_instance(hass).async_import_statistics(
        dict(meta),
        [{"start": START + timedelta(hours=4, minutes=55), "state": 2.0, "sum": 2.0}],
        StatisticsShortTerm,
    )
    await async_wait_recording_done(hass)

    plan = await plan_merge(
        hass,
        "sensor.new_meter",
        ["sensor.old_meter"],
        can_back_up=False,
        allow_no_backup=True,
    )
    assert plan.preview()["sum_shift"] == {
        "from": (START + timedelta(hours=3)).isoformat(),
        "offset": 3.0,
    }
    made = await backups.create_backups(hass, ["sensor.new_meter"], "merge_statistics")
    await merge_statistics(hass, plan)
    await async_wait_recording_done(hass)

    rows = (await sm.read_rows(hass, ["sensor.new_meter"]))["sensor.new_meter"]
    assert [row["sum"] for row in rows] == [0, 1.5, 3, 4, 5]
    assert await _latest_short_term_sum(hass, "sensor.new_meter") == 5.0

    freezer.tick(timedelta(minutes=5))
    hass.states.async_set("sensor.new_meter", "4", METER)
    freezer.tick(timedelta(minutes=5))
    await async_wait_recording_done(hass)
    now = dt_util.utcnow()
    do_adhoc_statistics(
        hass, start=now.replace(minute=now.minute - now.minute % 5, second=0)
    )
    await async_wait_recording_done(hass)
    # 5 + (4 - 2): the state's own increase on top of the merged sum.
    assert await _latest_short_term_sum(hass, "sensor.new_meter") == pytest.approx(7)

    # The backup holds the target as it was, and puts it back.
    backup_id = made["sensor.new_meter"]
    restore = await backups.plan_restore(hass, backup_id, None, can_back_up=True)
    assert restore["target"] == "sensor.new_meter"
    result = await backups.restore_statistics(hass, restore)
    await async_wait_recording_done(hass)
    assert result["complete"] is True
    rows = (await sm.read_rows(hass, ["sensor.new_meter"]))["sensor.new_meter"]
    assert [row["sum"] for row in rows] == [1, 2]
    assert await _latest_short_term_sum(hass, "sensor.new_meter") == 2.0


# --- backups and restore --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_backups_are_listed_with_their_origin_and_age(
    hass: HomeAssistant, freezer
):
    freezer.move_to(START + timedelta(days=1))
    await _seed(hass)
    made = await backups.create_backups(
        hass,
        ["sensor.old_meter", "tibber:energy_consumption_home1"],
        "clear_statistics",
    )
    assert made == {
        "sensor.old_meter": "ha_dev_tools:backup_sensor_old_meter_20241202t000000",
        "tibber:energy_consumption_home1": (
            "ha_dev_tools:backup_tibber_energy_consumption_home1_20241202t000000"
        ),
    }
    # Backups aren't backed up again.
    assert await backups.create_backups(hass, list(made.values()), "x") == {}

    freezer.tick(timedelta(days=91))
    listed = await sm.list_statistics(hass, source="ha_dev_tools")
    [old] = [
        row
        for row in listed["statistics"]
        if row["statistic_id"] == made["sensor.old_meter"]
    ]
    assert old["backup"] == {
        "of": "sensor.old_meter",
        "operation": "clear_statistics",
        "created": "2024-12-02T00:00:00+00:00",
        "age_days": 91,
        "stale": True,
    }
    assert old["has_entity"] is None
    assert backups.backup_info("Something else") is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_an_incomplete_backup_stops_the_write(hass: HomeAssistant, monkeypatch):
    await _seed(hass)
    monkeypatch.setattr(sm, "queue_import", lambda *_: None)
    with pytest.raises(
        sm.StatisticsBackupError, match="has no row for.*nothing was changed"
    ):
        await backups.create_backups(hass, ["sensor.old_meter"], "clear_statistics")


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_restore_onto_a_cleared_or_other_target(hass: HomeAssistant):
    await _seed(hass)
    made = await backups.create_backups(hass, ["sensor.old_meter"], "clear_statistics")
    await sm.clear_statistics(hass, ["sensor.old_meter"])

    plan = await backups.plan_restore(
        hass, made["sensor.old_meter"], None, can_back_up=False
    )
    assert plan["overwrites"] is None  # gone - recreated, nothing to back up
    result = await backups.restore_statistics(hass, plan)
    assert result["restored"]["rows"] == 3
    assert result["restored"]["name"] is None

    # Onto another id, converting kWh to Wh.
    await _meter(hass, "sensor.wh_meter", 10, [1000], unit="Wh")
    plan = await backups.plan_restore(
        hass, made["sensor.old_meter"], "sensor.wh_meter", can_back_up=True
    )
    assert plan["overwrites"]["rows"] == 1
    await backups.restore_statistics(hass, plan)
    await async_wait_recording_done(hass)
    rows = (await sm.read_rows(hass, ["sensor.wh_meter"]))["sensor.wh_meter"]
    assert [row["sum"] for row in rows] == [0, 1500, 3000]

    # An external target keeps its own source.
    plan = await backups.plan_restore(
        hass, made["sensor.old_meter"], "tibber:restored_copy", can_back_up=True
    )
    assert plan["metadata"]["source"] == "tibber"


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.parametrize(
    ("backup_id", "target", "can_back_up", "expected"),
    [
        ("sensor.old_meter", None, True, "isn't a statistics backup"),
        ("ha_dev_tools:backup_gone_20240101t000000", None, True, "no statistic"),
        ("ha_dev_tools:backup_unnamed_20240101t000000", None, True, "pass target"),
        ("BACKUP", "ha_dev_tools:backup_unnamed_20240101t000000", True, "a backup"),
        ("BACKUP", "Not an id", True, "isn't a statistic id"),
        ("BACKUP", "sensor.temperature", True, "differ in kind"),
        ("BACKUP", "sensor.gas", True, "can't be converted"),
        ("BACKUP", None, False, "allow_no_backup=true"),
    ],
)
async def test_plan_restore_refusals(
    hass: HomeAssistant, backup_id, target, can_back_up, expected
):
    await _seed(hass)
    made = await backups.create_backups(hass, ["sensor.old_meter"], "merge_statistics")
    await _meter(hass, "sensor.gas", 10, [1], unit="m³")
    async_import_statistics(
        hass,
        {
            **_metadata("sensor.temperature", "recorder", "t"),
            "has_sum": False,
            "mean_type": StatisticMeanType.ARITHMETIC,
        },
        [{"start": START, "mean": 1, "min": 0, "max": 2}],
    )
    async_add_external_statistics(
        hass,
        {
            **_metadata(
                "ha_dev_tools:backup_unnamed_20240101t000000", "ha_dev_tools", "x"
            ),
            "name": "renamed",
        },
        [{"start": START, "state": 0, "sum": 0}],
    )
    await async_wait_recording_done(hass)
    if backup_id == "BACKUP":
        backup_id = made["sensor.old_meter"]
    with pytest.raises(sm.StatisticsChangeRefusedError, match=expected):
        await backups.plan_restore(hass, backup_id, target, can_back_up=can_back_up)


# --- the tools ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_merge_statistics_tool(hass: HomeAssistant):
    from custom_components.ha_dev_tools.llm_api import MergeStatisticsTool

    await _seed(hass)
    await _meter(hass, "sensor.wh_meter", 10, [1000, 2000], unit="Wh")
    tool = MergeStatisticsTool()
    args = {
        "target_statistic_id": "sensor.old_meter",
        "source_statistic_ids": ["sensor.wh_meter"],
    }
    refused = await tool._preview_context(
        hass, _input(tool.name, **args), _llm_context()
    )
    assert "allow_no_backup=true" in refused["problems"]
    bad = await tool._write(
        hass, _input(tool.name, **args, start="yesterday"), _llm_context()
    )
    assert bad["error_type"] == "ValueError"

    write, (enabled, pushed) = _mirror()
    with enabled, pushed:
        preview = await tool._preview_context(
            hass,
            _input(tool.name, **args, end="2024-12-02T00:00:00+00:00"),
            _llm_context(),
        )
        result = await tool._write(hass, _input(tool.name, **args), _llm_context())
    assert preview["would_merge"]["rows_written"] == 5
    assert result["merged"]["target"]["rows"] == 5
    backup_id = result["backups"]["sensor.old_meter"]
    assert backup_id.startswith("ha_dev_tools:backup_sensor_old_meter_")
    assert "restore_statistics" in result["restore"]
    assert result["mirror"]["mirrored"] is True
    mirrored = json.loads(write.await_args.kwargs["content_after"])
    assert len(mirrored["statistics"][0]["stats"]) == 3

    # The guard reports a failed in-recorder backup and changes nothing.
    from unittest.mock import patch

    with (
        enabled,
        pushed,
        patch.object(sm, "queue_import", lambda *_: None),
    ):
        failed = await tool._write(
            hass, _input(tool.name, **args, overlap="target_wins"), _llm_context()
        )
    assert failed["error_type"] == "StatisticsBackupError"
    assert "has no row for" in failed["error"]
    assert "nothing was changed" in failed["error"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_restore_statistics_tool(hass: HomeAssistant):
    from custom_components.ha_dev_tools.llm_api import (
        ClearStatisticsTool,
        RestoreStatisticsTool,
    )

    await _seed(hass)
    _, (enabled, pushed) = _mirror()
    with enabled, pushed:
        cleared = await ClearStatisticsTool()._write(
            hass,
            _input("clear_statistics", statistic_ids=["sensor.old_meter"]),
            _llm_context(),
        )
        backup_id = cleared["backups"]["sensor.old_meter"]
        tool = RestoreStatisticsTool()
        args = {"backup_statistic_id": backup_id}
        preview = await tool._preview_context(
            hass, _input(tool.name, **args), _llm_context()
        )
        restored = await tool._write(hass, _input(tool.name, **args), _llm_context())
        # Clearing a backup makes no backup of the backup.
        removed = await ClearStatisticsTool()._write(
            hass,
            _input("clear_statistics", statistic_ids=[backup_id]),
            _llm_context(),
        )
    assert preview["onto"] == "sensor.old_meter"
    assert preview["would_overwrite"] is None
    assert preview["would_restore"]["backup"]["of"] == "sensor.old_meter"
    assert restored["complete"] is True
    assert "backups" not in restored  # nothing was there to overwrite
    assert "backups" not in removed

    refused = await tool._preview_context(
        hass, _input(tool.name, backup_statistic_id="sensor.x"), _llm_context()
    )
    assert "isn't a statistics backup" in refused["problems"]
    assert (
        await tool._write(
            hass, _input(tool.name, backup_statistic_id="sensor.x"), _llm_context()
        )
    )["error_type"] == "StatisticsChangeRefusedError"


def test_unit_conversion_without_a_stored_unit_class():
    """Statistics imported by older HA versions can lack unit_class."""
    convert, problem = backups.unit_converter(
        {"statistic_id": "sensor.x", "unit_of_measurement": "Wh", "unit_class": None},
        "kWh",
    )
    assert (convert(1500), problem) == (1.5, None)
    convert, problem = backups.unit_converter(
        {"statistic_id": "sensor.x", "unit_of_measurement": "apples"}, "kWh"
    )
    assert convert is None
    assert "can't be converted" in problem


async def _compile(hass):
    now = dt_util.utcnow()
    do_adhoc_statistics(
        hass, start=now.replace(minute=now.minute - now.minute % 5, second=0)
    )
    await async_wait_recording_done(hass)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.parametrize("own_5min_row", [True, False])
async def test_fresh_live_meter_continues_from_the_merged_sum(
    hass: HomeAssistant, freezer, own_5min_row
):
    """Issue #141's dress rehearsal: a target minutes old - one 5-minute
    row at sum 0 and no hourly rows yet, or none at all - got the merged
    hourly rows, but its next compile continued from 0."""
    freezer.move_to(START + timedelta(days=30, hours=12, minutes=52))
    await _seed(hass)  # old_meter: hours 0-2 a month ago, sums 0, 1.5, 3
    hass.states.async_set("sensor.new_meter", "0", METER)
    await async_wait_recording_done(hass)
    if own_5min_row:
        await _meter(hass, "sensor.new_meter", 0, [])  # metadata only
        meta = (await sm.read_metadata(hass, ["sensor.new_meter"]))["sensor.new_meter"]
        get_instance(hass).async_import_statistics(
            dict(meta),
            [
                {
                    "start": START + timedelta(days=30, hours=12, minutes=45),
                    "state": 0.0,
                    "sum": 0.0,
                }
            ],
            StatisticsShortTerm,
        )
        await async_wait_recording_done(hass)
    else:
        await _meter(hass, "sensor.new_meter", 0, [])

    plan = await plan_merge(
        hass,
        "sensor.new_meter",
        ["sensor.old_meter"],
        can_back_up=False,
        allow_no_backup=True,
    )
    result = await merge_statistics(hass, plan)
    await async_wait_recording_done(hass)

    check = result["next_compile"]
    assert check["ok"] is True, check
    assert check["latest_5min"]["sum"] == 3.0

    freezer.tick(timedelta(minutes=5))
    hass.states.async_set("sensor.new_meter", "2", METER)
    freezer.tick(timedelta(minutes=5))
    await async_wait_recording_done(hass)
    await _compile(hass)
    # 3 merged + the 2 the meter has counted since - not 2 alone.
    assert await _latest_short_term_sum(hass, "sensor.new_meter") == pytest.approx(5)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_continuity_check_reports_a_drop(hass: HomeAssistant):
    await _seed(hass)
    meta = (await sm.read_metadata(hass, ["sensor.old_meter"]))["sensor.old_meter"]
    assert (await sm.continuity_check(hass, "sensor.old_meter")) == {
        "applies": True,
        "last_hourly": {"start": (START + timedelta(hours=2)).isoformat(), "sum": 3},
        "ok": False,
        "note": "no 5-minute row - the entity's next compile would start its "
        "sum again at 0",
    }
    get_instance(hass).async_import_statistics(
        dict(meta),
        [{"start": START + timedelta(hours=3), "state": 0.0, "sum": 1.0}],
        StatisticsShortTerm,
    )
    await async_wait_recording_done(hass)
    check = await sm.continuity_check(hass, "sensor.old_meter")
    assert check["ok"] is False
    assert "drop by 2.0" in check["note"]
    assert (await sm.continuity_check(hass, "tibber:energy_consumption_home1")) == {
        "applies": False
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_long_write_answers_still_running_and_notifies(
    hass: HomeAssistant, monkeypatch
):
    """Issue #141: a merge that outlasts the client's timeout used to come
    back as an error while it was still happening."""
    from custom_components.ha_dev_tools import llm_api
    from custom_components.ha_dev_tools.llm_api import MergeStatisticsTool

    await _seed(hass)
    await _meter(hass, "sensor.wh_meter", 10, [1000, 2000], unit="Wh")
    monkeypatch.setattr(llm_api, "_STATISTICS_DEADLINE", 0)
    result = await MergeStatisticsTool()._write(
        hass,
        _input(
            "merge_statistics",
            target_statistic_id="sensor.old_meter",
            source_statistic_ids=["sensor.wh_meter"],
            allow_no_backup=True,
        ),
        _llm_context(),
    )
    assert result["still_running"] is True
    assert "Don't call it again" in result["note"]

    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    notifications = hass.data["persistent_notification"]
    [notice] = [n for n in notifications.values() if "merge_statistics" in n["title"]]
    assert "finished" in notice["message"]
    rows = (await sm.read_rows(hass, ["sensor.old_meter"]))["sensor.old_meter"]
    assert len(rows) == 5


def test_notification_for_failed_and_cancelled_writes(hass: HomeAssistant):
    from unittest.mock import MagicMock, patch

    from custom_components.ha_dev_tools.llm_api import _notify_statistics_result

    with patch(
        "custom_components.ha_dev_tools.llm_api.persistent_notification.async_create"
    ) as create:
        for task, expected in (
            (MagicMock(cancelled=lambda: True), "was cancelled"),
            (
                MagicMock(
                    cancelled=lambda: False, exception=lambda: RuntimeError("boom")
                ),
                "failed: boom",
            ),
            (
                MagicMock(
                    cancelled=lambda: False,
                    exception=lambda: None,
                    result=lambda: {"error": "refused"},
                ),
                "failed: refused",
            ),
        ):
            _notify_statistics_result(hass, "clear_statistics", task)
            assert expected in create.call_args.args[1]
