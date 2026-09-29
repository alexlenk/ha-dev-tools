"""Read-only access to the recorder's long-term statistics (issue #123).

The Energy dashboard and long-term graphs don't run on entity states but on
statistics, which the recorder keeps indefinitely - including for entities
that were deleted long ago, and for external statistics (e.g.
`tibber:energy_consumption_<home_id>`) that never were entities. None of
that is visible through states, the registry or state history, so finding
orphaned statistics, checking whether an Energy source is still fed, or
verifying a history migration used to mean the owner searching Developer
Tools -> Statistics by hand.

Like history_manager.py, this reuses the recorder's own query functions,
run on the recorder's executor: `list_statistic_ids` and
`statistics_during_period` (what Developer Tools -> Statistics and the
Energy dashboard call) and `validate_statistics` (the "Fix issue" list
there). The one addition is each statistic's first and last period, one
grouped MIN/MAX query over the long-term statistics table - HA has no
public function for it, and it's what tells an orphaned or no-longer-fed
statistic apart.

Read-only on purpose: importing, adjusting or clearing statistics is
destructive and hard to undo, so it isn't offered here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.recorder import get_instance, statistics
from homeassistant.components.recorder.db_schema import Statistics, StatisticsMeta
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from sqlalchemy import func, select

from .history_manager import RecorderNotAvailableError

PERIODS = ("5minute", "hour", "day", "week", "month")
TYPES = ("change", "last_reset", "max", "mean", "min", "state", "sum")
DEFAULT_LIST_LIMIT = 200
DEFAULT_ROW_LIMIT = 500


def _instance(hass: HomeAssistant) -> Any:
    if "recorder" not in hass.config.components:
        raise RecorderNotAvailableError("recorder")
    return get_instance(hass)


def _iso(timestamp: float | None) -> str | None:
    """The recorder's timestamps (period starts, last_reset) as ISO 8601."""
    if timestamp is None:
        return None
    return dt_util.utc_from_timestamp(timestamp).isoformat()


def _period_bounds(hass: HomeAssistant) -> dict[str, tuple[float, float]]:
    """{statistic_id: (first, last) period start} of the long-term table.

    Blocking - run on the recorder's executor."""
    stmt = (
        select(
            StatisticsMeta.statistic_id,
            func.min(Statistics.start_ts),
            func.max(Statistics.start_ts),
        )
        .join(Statistics, Statistics.metadata_id == StatisticsMeta.id)
        .group_by(StatisticsMeta.statistic_id)
    )
    with session_scope(hass=hass, read_only=True) as session:
        return {row[0]: (row[1], row[2]) for row in session.execute(stmt)}


def _has_entity(hass: HomeAssistant, statistic_id: str) -> bool | None:
    """Whether an entity with this id exists now; None for an external
    statistic (`source:id`), which is never an entity."""
    if ":" in statistic_id:
        return None
    return (
        hass.states.get(statistic_id) is not None
        or er.async_get(hass).async_get(statistic_id) is not None
    )


async def list_statistics(
    hass: HomeAssistant,
    *,
    search: str | None = None,
    statistic_type: str | None = None,
    source: str | None = None,
    unit: str | None = None,
    orphaned_only: bool = False,
    issues_only: bool = False,
    limit: int = DEFAULT_LIST_LIMIT,
) -> dict[str, Any]:
    """Every statistic, filtered, with whether its entity still exists, its
    first/last period and HA's own validation issues for it."""
    instance = _instance(hass)
    listed = await instance.async_add_executor_job(
        statistics.list_statistic_ids, hass, None, statistic_type
    )
    bounds = await instance.async_add_executor_job(_period_bounds, hass)
    issues = await instance.async_add_executor_job(statistics.validate_statistics, hass)
    wanted = search.casefold() if search else None

    rows = []
    for item in sorted(listed, key=lambda item: item["statistic_id"]):
        statistic_id = item["statistic_id"]
        if wanted and not (
            wanted in statistic_id.casefold()
            or wanted in (item.get("name") or "").casefold()
        ):
            continue
        if source and item["source"] != source:
            continue
        if unit and unit not in (
            item.get("statistics_unit_of_measurement"),
            item.get("unit_of_measurement"),
            item.get("display_unit_of_measurement"),
        ):
            continue
        has_entity = _has_entity(hass, statistic_id)
        if orphaned_only and has_entity is not False:
            continue
        found = [issue.as_dict() for issue in issues.get(statistic_id, [])]
        if issues_only and not found:
            continue
        first, last = bounds.get(statistic_id, (None, None))
        mean_type = item.get("mean_type")
        rows.append(
            {
                "statistic_id": statistic_id,
                "name": item.get("name"),
                "source": item["source"],
                "unit_of_measurement": item.get("statistics_unit_of_measurement")
                or item.get("unit_of_measurement"),
                "has_sum": item["has_sum"],
                "has_mean": bool(mean_type),
                "mean_type": getattr(mean_type, "name", mean_type),
                "has_entity": has_entity,
                "first_period": _iso(first),
                "last_period": _iso(last),
                "issues": found,
            }
        )
    return {
        "statistics": rows[:limit],
        "count": min(len(rows), limit),
        "total": len(rows),
        "truncated": len(rows) > limit,
    }


async def get_statistics(
    hass: HomeAssistant,
    statistic_ids: list[str],
    *,
    start_time: datetime,
    end_time: datetime | None = None,
    period: str = "hour",
    types: list[str] | None = None,
    units: dict[str, str] | None = None,
    limit: int = DEFAULT_ROW_LIMIT,
) -> dict[str, Any]:
    """Statistics rows per id, oldest first, at most `limit` per id.

    A cut series reports `next_start_time` - the start of its first row not
    returned - to continue from (a year of hourly data is ~8760 rows).
    Every requested id is in the result, with `known` saying whether the
    recorder has it at all, so a typo'd id isn't silently empty.
    """
    instance = _instance(hass)
    wanted = set(statistic_ids)
    raw = await instance.async_add_executor_job(
        statistics.statistics_during_period,
        hass,
        dt_util.as_utc(start_time),
        dt_util.as_utc(end_time) if end_time else None,
        wanted,
        period,
        units,
        set(types or TYPES),
    )
    known = {
        item["statistic_id"]
        for item in await instance.async_add_executor_job(
            statistics.list_statistic_ids, hass, wanted, None
        )
    }
    result: dict[str, Any] = {}
    for statistic_id in statistic_ids:
        rows = raw.get(statistic_id, [])
        kept = rows[:limit]
        series: dict[str, Any] = {
            "known": statistic_id in known,
            "rows": [
                {
                    key: _iso(value) if key in ("start", "end", "last_reset") else value
                    for key, value in row.items()
                }
                for row in kept
            ],
            "count": len(kept),
            "truncated": len(rows) > limit,
        }
        if len(rows) > limit:
            series["next_start_time"] = _iso(rows[limit]["start"])
        result[statistic_id] = series
    return {"period": period, "statistics": result}
