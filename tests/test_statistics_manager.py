"""Tests for statistics_manager.py (issue #123), against a real recorder.

Same fixture pattern as test_history_manager.py (see its module docstring):
`recorder_mock` via usefixtures, never next to `hass` as a parameter.
"""

import re
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


# --- clearing and moving statistics (issue #134) ----------------------------


async def _ids(hass: HomeAssistant) -> set[str]:
    return {row["statistic_id"] for row in (await list_statistics(hass))["statistics"]}


async def _use_in_energy(hass: HomeAssistant, statistic_id: str) -> None:
    from homeassistant.components.energy.data import async_get_manager

    manager = await async_get_manager(hass)
    await manager.async_update(
        {
            "energy_sources": [
                {
                    "type": "gas",
                    "stat_energy_from": statistic_id,
                    "stat_cost": None,
                    "entity_energy_price": None,
                    "number_energy_price": None,
                }
            ]
        }
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_plan_clear_refuses_unless_allowed(hass: HomeAssistant):
    from custom_components.ha_dev_tools.statistics_manager import (
        StatisticsChangeRefusedError,
        plan_clear,
    )

    await _seed(hass)
    await _use_in_energy(hass, "tibber:energy_consumption_home1")

    with pytest.raises(StatisticsChangeRefusedError) as refused:
        await plan_clear(
            hass,
            [
                "sensor.old_meter",
                "sensor.live_meter",
                "tibber:energy_consumption_home1",
                "sensor.typo",
            ],
            can_back_up=False,
        )
    message = str(refused.value)
    assert "no statistic 'sensor.typo'" in message
    assert "'sensor.live_meter' belongs to an existing entity" in message
    assert "energy_sources[0].stat_energy_from" in message
    assert "allow_no_backup=true" in message
    assert "sensor.old_meter" not in message

    planned = await plan_clear(
        hass,
        ["sensor.live_meter", "tibber:energy_consumption_home1", "sensor.live_meter"],
        can_back_up=False,
        allow_live=True,
        allow_energy=True,
        allow_no_backup=True,
    )
    assert [(info["statistic_id"], info["rows"]) for info in planned] == [
        ("sensor.live_meter", 2),
        ("tibber:energy_consumption_home1", 5),
    ]
    assert planned[1]["energy"] == ["energy_sources[0].stat_energy_from"]
    assert planned[1]["has_entity"] is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_cleared_series_restores_from_its_backup(
    hass: HomeAssistant, hass_ws_client
):
    from custom_components.ha_dev_tools.statistics_manager import (
        backup_document,
        clear_statistics,
    )

    await _seed(hass)
    before = await get_statistics(
        hass, ["sensor.old_meter"], start_time=START, types=["state", "sum"]
    )
    document = await backup_document(hass, ["sensor.old_meter", "sensor.typo"])
    [entry] = document["statistics"]
    assert entry["metadata"] == {
        "has_sum": True,
        "mean_type": 0,
        "name": "Hauptwohnung",
        "source": "recorder",
        "statistic_id": "sensor.old_meter",
        "unit_class": "energy",
        "unit_of_measurement": "kWh",
    }
    assert entry["stats"][1] == {
        "start": "2024-12-01T01:00:00+00:00",
        "state": 1.0,
        "sum": 1.5,
    }

    await clear_statistics(hass, ["sensor.old_meter"])
    await async_wait_recording_done(hass)
    assert "sensor.old_meter" not in await _ids(hass)

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "recorder/import_statistics", **entry})
    assert (await client.receive_json())["success"]
    await async_wait_recording_done(hass)
    after = await get_statistics(
        hass, ["sensor.old_meter"], start_time=START, types=["state", "sum"]
    )
    assert after == before


async def _replacement(hass: HomeAssistant, unit: str = "kWh") -> None:
    """A new entity that already collected one hour of its own."""
    hass.states.async_set(
        "sensor.new_meter",
        "7.5",
        {"state_class": "total_increasing", "unit_of_measurement": unit},
    )
    async_import_statistics(
        hass,
        {
            **_metadata("sensor.new_meter", "recorder", "New"),
            "unit_class": "energy" if unit == "kWh" else "volume",
            "unit_of_measurement": unit,
        },
        [
            {
                "start": START + timedelta(days=30),
                "last_reset": START,
                "state": 7,
                "sum": 0,
            }
        ],
    )
    await async_wait_recording_done(hass)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_migrate_moves_the_series_over_the_replacements_own(
    hass: HomeAssistant,
):
    from custom_components.ha_dev_tools.statistics_manager import (
        migrate_statistics,
        plan_migrate,
    )

    await _seed(hass)
    await _replacement(hass)
    plan = await plan_migrate(
        hass, "sensor.old_meter", "sensor.new_meter", can_back_up=True
    )
    assert plan["from"]["rows"] == 3
    assert plan["replaces"]["rows"] == 1
    assert plan["last_row"]["start_ts"] == (START + timedelta(hours=2)).timestamp()

    result = await migrate_statistics(hass, plan)
    await async_wait_recording_done(hass)

    assert result["moved"] is True
    moved = result["statistic"]
    assert (moved["statistic_id"], moved["rows"], moved["first_period"]) == (
        "sensor.new_meter",
        3,
        "2024-12-01T00:00:00+00:00",
    )
    assert moved["name"] == "Hauptwohnung"
    continuity = result["continuity"]
    assert (continuity["last_state"], continuity["current_state"]) == (2.0, 7.5)
    assert continuity["state_jump"] == 5.5
    assert continuity["hours_since_last_period"] > 24
    assert "sensor.old_meter" not in await _ids(hass)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_migrate_onto_an_id_without_statistics(hass: HomeAssistant):
    from custom_components.ha_dev_tools.statistics_manager import (
        migrate_statistics,
        plan_migrate,
    )

    await _seed(hass)
    hass.states.async_set("sensor.brand_new", "unknown", {"unit_of_measurement": "kWh"})
    plan = await plan_migrate(
        hass, "sensor.old_meter", "sensor.brand_new", can_back_up=False
    )
    assert plan["replaces"] is None
    result = await migrate_statistics(hass, plan)
    assert result["statistic"]["rows"] == 3
    # No numeric state yet, so nothing to compare the last state with.
    assert "state_jump" not in result["continuity"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.parametrize(
    ("from_id", "to_id", "unit", "expected"),
    [
        ("sensor.old_meter", "sensor.old_meter", "kWh", "the same statistic"),
        ("sensor.typo", "sensor.new_meter", "kWh", "no statistic 'sensor.typo'"),
        ("tibber:energy_consumption_home1", "sensor.new_meter", "kWh", "external"),
        ("sensor.live_meter", "sensor.new_meter", "kWh", "delete_entity"),
        ("sensor.old_meter", "tibber:x", "kWh", "'tibber:x' isn't an entity id"),
        ("sensor.old_meter", "sensor.new_meter", "m³", "units differ"),
        ("sensor.old_meter", "sensor.gas_meter", "kWh", "units differ"),
        ("sensor.old_meter", "sensor.temperature", "kWh", "one is a sum"),
        ("sensor.old_meter", "sensor.new_meter", "kWh", "allow_no_backup=true"),
    ],
)
async def test_plan_migrate_refusals(
    hass: HomeAssistant, from_id, to_id, unit, expected
):
    from custom_components.ha_dev_tools.statistics_manager import (
        StatisticsChangeRefusedError,
        plan_migrate,
    )

    await _seed(hass)
    await _replacement(hass, unit)
    async_import_statistics(
        hass,
        {
            **_metadata("sensor.temperature", "recorder", "Temperature"),
            "has_sum": False,
            "mean_type": StatisticMeanType.ARITHMETIC,
            "unit_class": "energy",
        },
        [{"start": START, "mean": 1, "min": 0, "max": 2}],
    )
    async_add_external_statistics(hass, _metadata("tibber:x", "tibber", "x"), _hours(1))
    hass.states.async_set("sensor.gas_meter", "1", {"unit_of_measurement": "m³"})
    await async_wait_recording_done(hass)
    with pytest.raises(StatisticsChangeRefusedError, match=re.escape(expected)):
        await plan_migrate(hass, from_id, to_id, can_back_up=False)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_rename_conflicts(hass: HomeAssistant):
    from custom_components.ha_dev_tools.statistics_manager import rename_conflicts

    await _seed(hass)
    warnings = await rename_conflicts(
        hass,
        [
            ("sensor.live_meter", "sensor.old_meter"),
            ("sensor.unrecorded", "sensor.old_meter"),
            ("sensor.live_meter", "sensor.free"),
        ],
    )
    assert set(warnings) == {"sensor.live_meter", "sensor.unrecorded"}
    assert warnings["sensor.live_meter"].startswith(
        "sensor.old_meter already has statistics 2024-12-01T00:00:00+00:00 -> "
        "2024-12-01T02:00:00+00:00; HA can't move"
    )
    assert "migrate_statistics from=sensor.live_meter to=sensor.old_meter" in (
        warnings["sensor.live_meter"]
    )
    assert "migrate_statistics" not in warnings["sensor.unrecorded"]


def _mirror(mirrored: bool = True):
    from unittest.mock import AsyncMock, patch

    from custom_components.ha_dev_tools.mirror import MirrorResult

    write = AsyncMock(
        return_value=MirrorResult(
            mirrored=mirrored,
            commits=("after",) if mirrored else (),
            reason=None if mirrored else "mirror push failed: boom",
        )
    )
    return write, (
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.is_mirror_enabled",
            return_value=True,
        ),
        patch("custom_components.ha_dev_tools.llm_api.mirror.mirror_write", write),
    )


def _input(name: str, **args):
    from homeassistant.helpers import llm

    return llm.ToolInput(tool_name=name, tool_args=args)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_clear_statistics_tool_backs_up_first(hass: HomeAssistant):
    import json

    from custom_components.ha_dev_tools.llm_api import ClearStatisticsTool

    await _seed(hass)
    tool = ClearStatisticsTool()
    args = {"statistic_ids": ["sensor.old_meter"]}

    refused = await tool._preview_context(
        hass, _input(tool.name, **args), _llm_context()
    )
    assert "allow_no_backup=true" in refused["problems"]
    assert (await tool._write(hass, _input(tool.name, **args), _llm_context()))[
        "error_type"
    ] == "StatisticsChangeRefusedError"

    write, (enabled, pushed) = _mirror()
    with enabled, pushed:
        preview = await tool._preview_context(
            hass, _input(tool.name, **args), _llm_context()
        )
        result = await tool._write(hass, _input(tool.name, **args), _llm_context())
    assert preview["would_clear"][0]["rows"] == 3
    assert result["cleared"][0]["statistic_id"] == "sensor.old_meter"
    assert result["mirror"] == {"mirrored": True, "commits": ["after"]}
    call = write.await_args.kwargs
    assert call["path"].startswith("statistics/cleared-")
    assert call["content_before"] is None
    backup = json.loads(call["content_after"])
    assert len(backup["statistics"][0]["stats"]) == 3
    await async_wait_recording_done(hass)
    assert "sensor.old_meter" not in await _ids(hass)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_clear_statistics_tool_when_the_backup_fails(hass: HomeAssistant):
    from custom_components.ha_dev_tools.llm_api import ClearStatisticsTool

    await _seed(hass)
    tool = ClearStatisticsTool()
    _, (enabled, pushed) = _mirror(mirrored=False)
    with enabled, pushed:
        failed = await tool._write(
            hass,
            _input(tool.name, statistic_ids=["sensor.old_meter"]),
            _llm_context(),
        )
        assert failed["error_type"] == "StatisticsBackupError"
        assert failed["mirror"]["reason"] == "mirror push failed: boom"
        assert "sensor.old_meter" in await _ids(hass)

        forced = await tool._write(
            hass,
            _input(tool.name, statistic_ids=["sensor.old_meter"], allow_no_backup=True),
            _llm_context(),
        )
    assert forced["mirror"]["mirrored"] is False
    await async_wait_recording_done(hass)
    assert "sensor.old_meter" not in await _ids(hass)

    # Mirroring off and explicitly allowed: no mirror key at all.
    unmirrored = await tool._write(
        hass,
        _input(
            tool.name,
            statistic_ids=["tibber:energy_consumption_home1"],
            allow_no_backup=True,
        ),
        _llm_context(),
    )
    assert "mirror" not in unmirrored
    assert unmirrored["cleared"][0]["rows"] == 5


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_migrate_statistics_tool(hass: HomeAssistant):
    import json

    from custom_components.ha_dev_tools.llm_api import MigrateStatisticsTool

    await _seed(hass)
    await _replacement(hass)
    await _use_in_energy(hass, "sensor.old_meter")
    tool = MigrateStatisticsTool()
    args = {
        "from_statistic_id": "sensor.old_meter",
        "to_statistic_id": "sensor.new_meter",
    }

    refused = await tool._preview_context(
        hass, _input(tool.name, **args), _llm_context()
    )
    assert "allow_no_backup=true" in refused["problems"]
    assert (await tool._write(hass, _input(tool.name, **args), _llm_context()))[
        "error_type"
    ] == "StatisticsChangeRefusedError"

    _, (enabled, failing) = _mirror(mirrored=False)
    with enabled, failing:
        failed = await tool._write(hass, _input(tool.name, **args), _llm_context())
    assert failed["error_type"] == "StatisticsBackupError"

    write, (enabled, pushed) = _mirror()
    with enabled, pushed:
        preview = await tool._preview_context(
            hass, _input(tool.name, **args), _llm_context()
        )
        result = await tool._write(hass, _input(tool.name, **args), _llm_context())
    assert preview["would_move"]["rows"] == 3
    assert preview["would_replace"]["rows"] == 1
    assert "write_energy_config" in preview["energy_note"]
    assert result["moved"] is True
    assert result["statistic"]["rows"] == 3
    assert result["mirror"]["mirrored"] is True
    assert "import_statistics" in result["restore"]
    backup = json.loads(write.await_args.kwargs["content_after"])
    assert [entry["metadata"]["statistic_id"] for entry in backup["statistics"]] == [
        "sensor.new_meter"
    ]
    assert backup["statistics"][0]["stats"] == [
        {
            "start": "2024-12-31T00:00:00+00:00",
            "last_reset": "2024-12-01T00:00:00+00:00",
            "state": 7.0,
            "sum": 0.0,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_migrate_statistics_tool_without_a_series_to_replace(
    hass: HomeAssistant,
):
    from custom_components.ha_dev_tools.llm_api import MigrateStatisticsTool

    await _seed(hass)
    tool = MigrateStatisticsTool()
    args = {"from_statistic_id": "sensor.old_meter", "to_statistic_id": "sensor.fresh"}
    preview = await tool._preview_context(
        hass, _input(tool.name, **args), _llm_context()
    )
    assert preview["would_replace"] is None
    assert "energy_note" not in preview
    result = await tool._write(hass, _input(tool.name, **args), _llm_context())
    assert result["moved"] is True
    assert "mirror" not in result


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_update_entities_preview_warns_about_a_rename_onto_statistics(
    hass: HomeAssistant,
):
    from homeassistant.helpers import entity_registry as er

    from custom_components.ha_dev_tools.llm_api import UpdateEntitiesTool

    await _seed(hass)
    er.async_get(hass).async_get_or_create(
        "sensor", "test", "spare", suggested_object_id="spare"
    )
    context = await UpdateEntitiesTool()._preview_context(
        hass,
        _input(
            "update_entities",
            items=[{"entity_id": "sensor.spare", "new_entity_id": "sensor.old_meter"}],
        ),
        _llm_context(),
    )
    assert "already has statistics" in context["statistics"]["sensor.spare"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_tools_report_a_recorder_that_never_confirms(
    hass: HomeAssistant, monkeypatch
):
    from homeassistant.components.recorder import get_instance

    from custom_components.ha_dev_tools import statistics_manager
    from custom_components.ha_dev_tools.llm_api import (
        ClearStatisticsTool,
        MigrateStatisticsTool,
    )

    await _seed(hass)
    await _replacement(hass)
    monkeypatch.setattr(statistics_manager, "RECORDER_TIMEOUT", 0.01)
    instance = get_instance(hass)
    # Queued work that never reports back.
    monkeypatch.setattr(instance, "async_clear_statistics", lambda *_, **__: None)
    monkeypatch.setattr(
        instance, "async_update_statistics_metadata", lambda *_, **__: None
    )

    _, (enabled, pushed) = _mirror()
    with enabled, pushed:
        cleared = await ClearStatisticsTool()._write(
            hass,
            _input("clear_statistics", statistic_ids=["sensor.old_meter"]),
            _llm_context(),
        )
        moved = await MigrateStatisticsTool()._write(
            hass,
            _input(
                "migrate_statistics",
                from_statistic_id="sensor.old_meter",
                to_statistic_id="sensor.new_meter",
            ),
            _llm_context(),
        )
    for result in (cleared, moved):
        assert result["error_type"] == "StatisticsTimeoutError"
        assert "still queued" in result["error"]
        assert result["mirror"]["mirrored"] is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_migrate_reports_a_move_ha_declined(hass: HomeAssistant, monkeypatch):
    from homeassistant.components.recorder import get_instance

    from custom_components.ha_dev_tools.statistics_manager import (
        migrate_statistics,
        plan_migrate,
    )

    await _seed(hass)
    plan = await plan_migrate(
        hass, "sensor.old_meter", "sensor.brand_new", can_back_up=False
    )
    # The recorder runs the task but leaves the series where it is.
    instance = get_instance(hass)
    update = instance.async_update_statistics_metadata
    monkeypatch.setattr(
        instance,
        "async_update_statistics_metadata",
        lambda statistic_id, on_done=None, **_: update(statistic_id, on_done=on_done),
    )
    result = await migrate_statistics(hass, plan)
    assert result["moved"] is False
    assert "didn't move the series" in result["note"]
    assert "continuity" not in result


async def _compile_now(hass: HomeAssistant) -> float | None:
    """Run the sensor's 5-minute compile for the period now falls in, as
    the recorder would, and return the newest short-term sum."""
    from homeassistant.components.recorder.db_schema import StatisticsShortTerm
    from pytest_homeassistant_custom_component.components.recorder.common import (
        do_adhoc_statistics,
    )

    from custom_components.ha_dev_tools.statistics_manager import read_rows

    now = dt_util.utcnow()
    period = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
    do_adhoc_statistics(hass, start=period)
    await async_wait_recording_done(hass)
    rows = (await read_rows(hass, ["sensor.new_meter"], StatisticsShortTerm)).get(
        "sensor.new_meter", []
    )
    return rows[-1]["sum"] if rows else None


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_migrated_meter_continues_its_sum_without_short_term_rows(
    hass: HomeAssistant, freezer
):
    """Issue #136: a dead entity's 5-minute rows are purged after 10 days;
    without one, the replacement's next compile restarted the sum at 0."""
    from custom_components.ha_dev_tools.statistics_manager import (
        migrate_statistics,
        plan_migrate,
    )

    freezer.move_to(START + timedelta(days=60, minutes=2))
    await _seed(hass)  # old_meter: 3 hourly rows, no short-term ones
    hass.states.async_set(
        "sensor.new_meter",
        "7.5",
        {
            "state_class": "total_increasing",
            "unit_of_measurement": "kWh",
            "device_class": "energy",
        },
    )
    await async_wait_recording_done(hass)
    plan = await plan_migrate(
        hass, "sensor.old_meter", "sensor.new_meter", can_back_up=False
    )
    await migrate_statistics(hass, plan)
    await async_wait_recording_done(hass)

    freezer.tick(timedelta(minutes=5))
    hass.states.async_set(
        "sensor.new_meter",
        "8.5",
        {
            "state_class": "total_increasing",
            "unit_of_measurement": "kWh",
            "device_class": "energy",
        },
    )
    freezer.tick(timedelta(minutes=5))
    await async_wait_recording_done(hass)
    # The old meter ended at sum 3.0 with state 2; the new one reads 8.5.
    assert await _compile_now(hass) == pytest.approx(3.0 + 8.5 - 2)
