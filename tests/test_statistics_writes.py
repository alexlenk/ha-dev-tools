"""How statistics writes behave when the recorder doesn't simply run them
in order and succeed: an import or adjustment re-queued behind later work
(MySQL/MariaDB lock-wait timeouts and deadlocks), a task that ran but left
the rows as they were, an import HA would refuse, two writes on one
statistic at once, and a merge the size of real meter history."""

from datetime import timedelta

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder import statistics as recorder_statistics
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ha_dev_tools import statistics_backup as backups
from custom_components.ha_dev_tools import statistics_manager as sm
from custom_components.ha_dev_tools.statistics_merge import (
    merge_statistics,
    plan_merge,
)
from tests.test_statistics_manager import START, _input, _llm_context, _seed
from tests.test_statistics_merge import (
    METER,
    _compile,
    _latest_short_term_sum,
    _meter,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Shadow conftest.py's fixture - see test_history_manager.py."""
    yield


async def _live_target(hass: HomeAssistant, freezer) -> None:
    """test_live_meter_continues_from_the_merged_sum's setup: old_meter
    hours 0-2 (sums 0, 1.5, 3); new_meter live, hours 3-4 (sums 1, 2) and
    a 5-minute row at 2."""
    freezer.move_to(START + timedelta(hours=5, minutes=2))
    await _seed(hass)
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


async def _sums(hass: HomeAssistant, statistic_id: str) -> list[float]:
    rows = (await sm.read_rows(hass, [statistic_id])).get(statistic_id, [])
    return [row["sum"] for row in rows]


def _fail_once(monkeypatch, name: str, statistic_id: str, *, write: bool) -> list:
    """Make recorder.statistics.<name> report failure once for
    statistic_id, as it does on MySQL/MariaDB for a lock-wait timeout or a
    deadlock - its task then re-queues itself at the end of the queue.
    With write=False it reports success instead but changes nothing: a
    task that ran and left the rows as they were."""
    real = getattr(recorder_statistics, name)
    calls: list = []

    def flaky(instance, *args):
        target = args[0]["statistic_id"] if name == "import_statistics" else args[0]
        if target == statistic_id and not calls:
            calls.append(args)
            return not write
        return real(instance, *args)

    monkeypatch.setattr(recorder_statistics, name, flaky)
    return calls


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_requeued_import_doesnt_land_after_the_sum_adjustment(
    hass: HomeAssistant, freezer, monkeypatch
):
    """The adjustment adds to whatever the rows hold when it runs: queued
    behind an import that re-queued itself, it ran first, and the import
    then put the shifted hours back on the old basis."""
    await _live_target(hass, freezer)
    plan = await plan_merge(
        hass,
        "sensor.new_meter",
        ["sensor.old_meter"],
        can_back_up=False,
        allow_no_backup=True,
    )
    retried = _fail_once(
        monkeypatch, "import_statistics", "sensor.new_meter", write=True
    )

    result = await merge_statistics(hass, plan)
    await async_wait_recording_done(hass)

    assert retried  # the import did go round twice
    assert await _sums(hass, "sensor.new_meter") == [0, 1.5, 3, 4, 5]
    assert await _latest_short_term_sum(hass, "sensor.new_meter") == 5.0
    assert result["next_compile"]["ok"] is True
    # The merge's own hours 3-4 are already right on the old basis - only
    # the sources' hours were imported.
    assert result["merged"]["rows_changed"] == 3


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_requeued_adjustment_is_waited_for(
    hass: HomeAssistant, freezer, monkeypatch
):
    await _live_target(hass, freezer)
    plan = await plan_merge(
        hass,
        "sensor.new_meter",
        ["sensor.old_meter"],
        can_back_up=False,
        allow_no_backup=True,
    )
    retried = _fail_once(
        monkeypatch, "adjust_statistics", "sensor.new_meter", write=True
    )

    result = await merge_statistics(hass, plan)

    assert retried
    # Reported only once it's there - no wait needed before reading.
    assert (
        result["merged"]["target"]["last_period"]
        == (START + timedelta(hours=4)).isoformat()
    )
    assert await _sums(hass, "sensor.new_meter") == [0, 1.5, 3, 4, 5]
    assert result["next_compile"]["ok"] is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_an_import_that_doesnt_land_stops_before_the_adjustment(
    hass: HomeAssistant, freezer, monkeypatch
):
    await _live_target(hass, freezer)
    plan = await plan_merge(
        hass,
        "sensor.new_meter",
        ["sensor.old_meter"],
        can_back_up=False,
        allow_no_backup=True,
    )
    _fail_once(monkeypatch, "import_statistics", "sensor.new_meter", write=False)
    adjusted = []
    instance = get_instance(hass)
    monkeypatch.setattr(
        instance, "async_adjust_statistics", lambda *args: adjusted.append(args)
    )

    with pytest.raises(sm.StatisticsNotAppliedError, match="has no row for"):
        await merge_statistics(hass, plan)
    assert adjusted == []
    # The target as it was - no half-shifted series.
    assert await _sums(hass, "sensor.new_meter") == [1, 2]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_restore_refused_before_its_target_is_cleared(hass: HomeAssistant):
    await _seed(hass)
    made = await backups.create_backups(hass, ["sensor.old_meter"], "clear_statistics")
    await _meter(hass, "sensor.wh_meter", 10, [1000, 2000], unit="Wh")
    plan = await backups.plan_restore(
        hass, made["sensor.old_meter"], "sensor.wh_meter", can_back_up=True
    )
    assert plan["overwrites"]
    plan["metadata"]["unit_of_measurement"] = "apples"  # HA's import refuses

    with pytest.raises(sm.StatisticsChangeRefusedError, match="apples"):
        await backups.restore_statistics(hass, plan)
    await async_wait_recording_done(hass)
    assert await _sums(hass, "sensor.wh_meter") == [1000, 2000]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_an_unapplied_write_is_reported_with_its_backup(
    hass: HomeAssistant, freezer, monkeypatch
):
    """Through the tool: the error, what the target holds now, and the
    backup to restore from."""
    from custom_components.ha_dev_tools.llm_api import MergeStatisticsTool

    await _live_target(hass, freezer)
    real = recorder_statistics.import_statistics

    def drop(instance, metadata, *args):
        if metadata["statistic_id"] == "sensor.new_meter":
            return True  # "done", nothing written
        return real(instance, metadata, *args)

    monkeypatch.setattr(recorder_statistics, "import_statistics", drop)
    result = await MergeStatisticsTool()._write(
        hass,
        _input(
            "merge_statistics",
            target_statistic_id="sensor.new_meter",
            source_statistic_ids=["sensor.old_meter"],
            allow_no_backup=True,
        ),
        _llm_context(),
    )
    assert result["error_type"] == "StatisticsNotAppliedError"
    assert "Home Assistant's log" in result["error"]
    assert result["now"]["sensor.new_meter"]["rows"] == 2
    backup_id = result["backups"]["sensor.new_meter"]
    assert await _sums(hass, backup_id) == [1, 2]
    assert "restore_statistics" in result["restore"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_one_write_at_a_time_per_statistic(hass: HomeAssistant, monkeypatch):
    from custom_components.ha_dev_tools import llm_api
    from custom_components.ha_dev_tools.llm_api import (
        ClearStatisticsTool,
        MergeStatisticsTool,
    )

    await _seed(hass)
    await _meter(hass, "sensor.wh_meter", 10, [1000, 2000], unit="Wh")
    monkeypatch.setattr(llm_api, "_STATISTICS_DEADLINE", 0)
    merge = _input(
        "merge_statistics",
        target_statistic_id="sensor.old_meter",
        source_statistic_ids=["sensor.wh_meter"],
        allow_no_backup=True,
    )
    first = await MergeStatisticsTool()._write(hass, merge, _llm_context())
    assert first["still_running"] is True

    again = await MergeStatisticsTool()._write(hass, merge, _llm_context())
    assert again["error_type"] == "StatisticsBusyError"
    assert "sensor.old_meter" in again["error"]
    # A source is locked too - clearing it mid-merge would change what's
    # being merged.
    clear = await ClearStatisticsTool()._write(
        hass,
        _input(
            "clear_statistics", statistic_ids=["sensor.wh_meter"], allow_no_backup=True
        ),
        _llm_context(),
    )
    assert clear["error_type"] == "StatisticsBusyError"

    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    assert await _sums(hass, "sensor.old_meter") == [0, 1.5, 3, 4, 5]
    # Released once done, failed or not.
    monkeypatch.setattr(llm_api, "_STATISTICS_DEADLINE", 30)
    cleared = await ClearStatisticsTool()._write(
        hass,
        _input(
            "clear_statistics", statistic_ids=["sensor.wh_meter"], allow_no_backup=True
        ),
        _llm_context(),
    )
    assert "error" not in cleared, cleared
    assert hass.data[llm_api._STATISTICS_WRITES_RUNNING] == set()


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_merging_years_of_history_into_a_live_meter(hass: HomeAssistant, freezer):
    """Issue #145's size: ~20 months of hourly rows from the old meter into
    the live replacement, then the replacement's next compiles."""
    hours = 14_600
    freezer.move_to(START + timedelta(hours=hours + 48, minutes=2))
    assert await async_setup_component(hass, "sensor", {})
    await _meter(hass, "sensor.old_meter", 0, [index * 0.5 for index in range(hours)])
    hass.states.async_set("sensor.new_meter", "7", METER)
    await async_wait_recording_done(hass)
    await _meter(hass, "sensor.new_meter", hours + 40, [1, 2, 4, 5, 6, 7])
    meta = (await sm.read_metadata(hass, ["sensor.new_meter"]))["sensor.new_meter"]
    get_instance(hass).async_import_statistics(
        dict(meta),
        [
            {
                "start": START + timedelta(hours=hours + 45, minutes=55),
                "state": 7.0,
                "sum": 7.0,
            }
        ],
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
    total = (hours - 1) * 0.5
    assert plan.preview()["sum_shift"]["offset"] == pytest.approx(total)
    result = await merge_statistics(hass, plan)

    sums = await _sums(hass, "sensor.new_meter")
    assert len(sums) == hours + 6
    assert sums[hours - 1] == pytest.approx(total)
    assert sums[-1] == pytest.approx(total + 7)
    assert result["merged"]["rows_changed"] == hours
    assert result["next_compile"]["ok"] is True

    freezer.tick(timedelta(minutes=5))
    hass.states.async_set("sensor.new_meter", "9", METER)
    freezer.tick(timedelta(minutes=5))
    await async_wait_recording_done(hass)
    await _compile(hass)
    assert await _latest_short_term_sum(hass, "sensor.new_meter") == pytest.approx(
        total + 9
    )


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"statistic_id": "not an entity", "source": "recorder"}, "isn't an entity"),
        ({"statistic_id": "tibber:x", "source": "other"}, "for source 'other'"),
        ({"statistic_id": "nocolon", "source": "tibber"}, "valid external"),
        (
            {
                "statistic_id": "sensor.x",
                "source": "recorder",
                "unit_class": "no_such_class",
                "unit_of_measurement": "kWh",
            },
            "import refuses",
        ),
    ],
)
def test_check_importable_mirrors_has_import_validation(metadata, expected):
    with pytest.raises(sm.StatisticsChangeRefusedError, match=expected):
        sm.check_importable(metadata)
    sm.check_importable(
        {
            "statistic_id": "sensor.x",
            "source": "recorder",
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        }
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_row_mismatches_names_the_column(hass: HomeAssistant):
    await _seed(hass)  # old_meter: sums 0, 1.5, 3
    rows = (await sm.read_rows(hass, ["sensor.old_meter"]))["sensor.old_meter"]
    assert await sm.row_mismatches(hass, "sensor.old_meter", rows) == []
    # Float rounding isn't a mismatch; a real difference is.
    assert sm._close(3.0, 3.0 + 1e-12) and not sm._close(3.0, 3.001)
    assert sm._close(None, None) and not sm._close(None, 0)
    [problem] = await sm.row_mismatches(
        hass, "sensor.old_meter", [{**rows[1], "sum": 2.0}]
    )
    assert "sum is 1.5, not 2.0" in problem


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.parametrize("outcome", ["gone", "elsewhere"])
async def test_migrate_checks_where_the_series_ended_up(
    hass: HomeAssistant, monkeypatch, outcome
):
    from homeassistant.components.recorder.statistics import async_import_statistics

    from tests.test_statistics_manager import _metadata

    await _seed(hass)
    plan = await sm.plan_migrate(
        hass, "sensor.old_meter", "sensor.brand_new", can_back_up=False
    )
    instance = get_instance(hass)
    update = instance.async_update_statistics_metadata

    def not_a_move(statistic_id, **kwargs):
        if statistic_id != "sensor.old_meter":  # on_recorder's barrier
            return update(statistic_id, **kwargs)
        instance.async_clear_statistics([statistic_id])
        if outcome == "elsewhere":
            async_import_statistics(
                hass,
                _metadata("sensor.brand_new", "recorder", "x"),
                [{"start": START + timedelta(hours=50), "state": 0, "sum": 0}],
            )

    monkeypatch.setattr(instance, "async_update_statistics_metadata", not_a_move)
    result = await sm.migrate_statistics(hass, plan)
    assert result["moved"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("ClearStatisticsTool", {"statistic_ids": ["sensor.a"]}),
        (
            "MigrateStatisticsTool",
            {"from_statistic_id": "sensor.a", "to_statistic_id": "sensor.b"},
        ),
        (
            "MergeStatisticsTool",
            {"target_statistic_id": "sensor.b", "source_statistic_ids": ["sensor.a"]},
        ),
        (
            "DeriveStatisticsTool",
            {
                "source_statistic_id": "sensor.a",
                "target_statistic_id": "ha_dev_tools:c",
                "factor": 1,
            },
        ),
        ("RestoreStatisticsTool", {"backup_statistic_id": "sensor.a"}),
    ],
)
async def test_every_statistics_write_waits_its_turn(hass: HomeAssistant, tool, args):
    from custom_components.ha_dev_tools import llm_api

    hass.data[llm_api._STATISTICS_WRITES_RUNNING] = {"sensor.a"}
    result = await getattr(llm_api, tool)()._write(
        hass, _input("x", **args), _llm_context()
    )
    assert result["error_type"] == "StatisticsBusyError"
    assert "sensor.a" in result["error"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_migrate_refused_when_the_target_started_recording_since_the_plan(
    hass: HomeAssistant,
):
    """HA wouldn't rename onto it, but the seed row queued with the rename
    would land in its live series."""
    await _seed(hass)
    plan = await sm.plan_migrate(
        hass, "sensor.old_meter", "sensor.brand_new", can_back_up=False
    )
    await _meter(hass, "sensor.brand_new", 40, [5, 6])

    with pytest.raises(sm.StatisticsChangeRefusedError, match="preview again"):
        await sm.migrate_statistics(hass, plan)
    await async_wait_recording_done(hass)
    assert await _sums(hass, "sensor.brand_new") == [5, 6]
    assert await _sums(hass, "sensor.old_meter") == [0, 1.5, 3]
    short_term = await sm.read_rows(hass, ["sensor.brand_new"], StatisticsShortTerm)
    assert short_term == {}
