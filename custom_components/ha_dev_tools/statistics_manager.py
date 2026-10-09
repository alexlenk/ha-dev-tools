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

Writes (issue #134) are limited to what a history migration needs:
clearing statistics (`Recorder.async_clear_statistics`, what WS
`recorder/clear_statistics` and the "Fix issue" dialog's Delete call) and
moving a series onto another statistic_id
(`Recorder.async_update_statistics_metadata(new_statistic_id=...)`, what
the recorder itself does when an entity is renamed). Both work on the
recorder's queue, and HA refuses to move a series onto an id that already
has one - it can't merge two series - which is why a rename onto a
replacement entity silently leaves the history behind. A cleared series
can be backed up first in `recorder/import_statistics`'s own shape, so it
can be restored with one call.

Issue #136 adds merging and in-recorder backups (statistics_merge.py,
statistics_backup.py), built on the same queue: HA's own import task
(`Recorder.async_import_statistics`, for long- and short-term rows) and
sum adjustment (`Recorder.async_adjust_statistics`). One thing every write
that changes a meter's sums has to respect: the sensor's 5-minute compile
continues its `sum` from the latest *short-term* row only
(`get_latest_short_term_statistics_with_session`, no fall-back to the
hourly table) and starts again at 0 when there is none - so a series that
gets a new sum basis, or loses its short-term rows, needs them to match.
Editing single rows (`recorder/adjust_sum_statistics` by hand) isn't
offered.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import partial
from typing import Any, cast

from homeassistant.components.recorder import get_instance, statistics
from homeassistant.components.recorder.db_schema import (
    Statistics,
    StatisticsBase,
    StatisticsMeta,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.models import StatisticMetaData
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant, valid_entity_id
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from sqlalchemy import func, select
from sqlalchemy.sql import Select

from .const import DOMAIN
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


def _period_bounds(
    hass: HomeAssistant, statistic_ids: list[str] | None = None
) -> dict[str, tuple[float, float, int]]:
    """{statistic_id: (first, last period start, row count)} of the
    long-term table, for every statistic or just `statistic_ids`.

    Blocking - run on the recorder's executor."""
    stmt: Select[Any] = (
        select(
            StatisticsMeta.statistic_id,
            func.min(Statistics.start_ts),
            func.max(Statistics.start_ts),
            func.count(Statistics.id),
        )
        .join(Statistics, Statistics.metadata_id == StatisticsMeta.id)
        .group_by(StatisticsMeta.statistic_id)
    )
    if statistic_ids is not None:
        stmt = stmt.where(StatisticsMeta.statistic_id.in_(statistic_ids))
    with session_scope(hass=hass, read_only=True) as session:
        return {row[0]: (row[1], row[2], row[3]) for row in session.execute(stmt)}


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
    # statistics_backup builds on this module, so it's imported here.
    from .statistics_backup import backup_info

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
        first, last, _ = bounds.get(statistic_id, (None, None, 0))
        mean_type = item.get("mean_type")
        extra: dict[str, Any] = {}
        if item["source"] == DOMAIN:
            extra["backup"] = backup_info(item.get("name"))
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
                **extra,
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


# --- clearing and moving statistics (issue #134) ------------------------------

_VALUE_COLUMNS = ("mean", "min", "max", "state", "sum")
RESTORE_HINT = (
    "Undo with restore_statistics, passing the 'backups' entry for a "
    "statistic as backup_statistic_id. The mirror file holds the same "
    "series in WS recorder/import_statistics' shape (metadata and stats), "
    "for restoring from outside Home Assistant."
)


# How long a statistics write waits for the recorder to run what it queued.
# HA's import checks every row on its own, so copying a long series takes
# minutes on a real install (issue #145: ~14,600 hourly rows outlasted the
# 60 s this used to be, and the write after the backup never ran). Nothing
# needs a short wait here: the tools answer `still_running` long before
# (llm_api._STATISTICS_DEADLINE) and report the outcome when it's known.
RECORDER_TIMEOUT = 30 * 60


class StatisticsChangeRefusedError(Exception):
    """A clear or move was refused before anything changed."""


class StatisticsTimeoutError(Exception):
    """The recorder didn't confirm queued work in time - it may still run."""


class StatisticsBackupError(Exception):
    """A backup couldn't be made, so nothing was changed."""


class StatisticsNotAppliedError(Exception):
    """The recorder ran the queued work, but the statistics don't hold what
    was written - a task failed inside the recorder (logged there) or was
    re-queued and still hadn't run."""


class StatisticsBusyError(Exception):
    """Another statistics write on the same statistic is still running."""


async def energy_references(hass: HomeAssistant) -> dict[str, list[str]]:
    """{statistic_id: [where the Energy dashboard uses it]}.

    Every statistic the Energy prefs (`.storage/energy`) point at is under a
    `stat_*` key (stat_energy_from/_to, stat_cost, stat_compensation,
    stat_consumption, stat_rate, stat_soc, ...), at any depth, so walk them
    rather than list every source type's fields."""
    from homeassistant.components.energy.data import async_get_manager

    refs: dict[str, list[str]] = {}

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                where = f"{path}.{key}" if path else key
                if key.startswith("stat_") and isinstance(value, str):
                    refs.setdefault(value, []).append(where)
                else:
                    walk(value, where)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk((await async_get_manager(hass)).data or {}, "")
    return refs


async def describe_statistics(
    hass: HomeAssistant, statistic_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """What each known id holds - source, unit, whether its entity exists,
    first/last period, long-term row count, Energy dashboard use. Unknown
    ids are left out."""
    instance = _instance(hass)
    metadata = await instance.async_add_executor_job(
        partial(statistics.get_metadata, hass, statistic_ids=set(statistic_ids))
    )
    bounds = await instance.async_add_executor_job(_period_bounds, hass, statistic_ids)
    energy = await energy_references(hass)
    described: dict[str, dict[str, Any]] = {}
    for statistic_id in statistic_ids:
        if statistic_id not in metadata:
            continue
        meta = metadata[statistic_id][1]
        first, last, rows = bounds.get(statistic_id, (None, None, 0))
        described[statistic_id] = {
            "statistic_id": statistic_id,
            "name": meta.get("name"),
            "source": meta["source"],
            "unit_of_measurement": meta.get("unit_of_measurement"),
            "has_sum": meta["has_sum"],
            "has_entity": _has_entity(hass, statistic_id),
            "first_period": _iso(first),
            "last_period": _iso(last),
            "rows": rows,
            "energy": energy.get(statistic_id, []),
        }
    return described


def _unknown(statistic_id: str) -> str:
    return f"no statistic '{statistic_id}' (list_statistics shows what exists)"


def _no_backup() -> str:
    return (
        "mirroring is off, so the series can't be backed up first and "
        "clearing it can't be undone - enable mirroring, or pass "
        "allow_no_backup=true"
    )


async def plan_clear(
    hass: HomeAssistant,
    statistic_ids: list[str],
    *,
    can_back_up: bool,
    allow_live: bool = False,
    allow_energy: bool = False,
    allow_no_backup: bool = False,
) -> list[dict[str, Any]]:
    """What clearing `statistic_ids` would delete; refuses the whole batch
    if any id is unknown, still belongs to an entity (unless allow_live),
    feeds the Energy dashboard (unless allow_energy), or can't be backed up
    (unless allow_no_backup)."""
    wanted = list(dict.fromkeys(statistic_ids))
    described = await describe_statistics(hass, wanted)
    problems: list[str] = []
    for statistic_id in wanted:
        info = described.get(statistic_id)
        if info is None:
            problems.append(_unknown(statistic_id))
            continue
        if info["has_entity"] and not allow_live:
            problems.append(
                f"'{statistic_id}' belongs to an existing entity - it's the "
                "series that entity records into; pass allow_live=true to "
                "clear it anyway"
            )
        if info["energy"] and not allow_energy:
            problems.append(
                f"'{statistic_id}' is used by the Energy dashboard "
                f"({', '.join(info['energy'])}); pass allow_energy=true to "
                "clear it anyway"
            )
    if not can_back_up and not allow_no_backup:
        problems.append(_no_backup())
    if problems:
        raise StatisticsChangeRefusedError("; ".join(problems))
    return [described[statistic_id] for statistic_id in wanted]


def _raw_rows(
    hass: HomeAssistant,
    statistic_ids: list[str],
    table: type[StatisticsBase] = Statistics,
) -> dict[str, list[dict[str, Any]]]:
    """Every row of `statistic_ids` in `table` (hourly by default), raw - no
    unit conversion, timestamps as floats - oldest first.

    Blocking - run on the recorder's executor."""
    stmt = (
        select(
            StatisticsMeta.statistic_id,
            table.start_ts,
            table.last_reset_ts,
            *(getattr(table, column) for column in _VALUE_COLUMNS),
        )
        .join(table, table.metadata_id == StatisticsMeta.id)
        .where(StatisticsMeta.statistic_id.in_(statistic_ids))
        .order_by(StatisticsMeta.statistic_id, table.start_ts)
    )
    rows: dict[str, list[dict[str, Any]]] = {}
    with session_scope(hass=hass, read_only=True) as session:
        for statistic_id, start, last_reset, *values in session.execute(stmt):
            rows.setdefault(statistic_id, []).append(
                {
                    "start_ts": start,
                    "last_reset_ts": last_reset,
                    **dict(zip(_VALUE_COLUMNS, values, strict=True)),
                }
            )
    return rows


def _import_shape(row: dict[str, Any]) -> dict[str, Any]:
    """A raw row in recorder/import_statistics' JSON row shape."""
    shaped: dict[str, Any] = {"start": _iso(row["start_ts"])}
    if row.get("last_reset_ts") is not None:
        shaped["last_reset"] = _iso(row["last_reset_ts"])
    shaped.update(
        (column, row[column])
        for column in _VALUE_COLUMNS
        if row.get(column) is not None
    )
    return shaped


def _series_rows(
    hass: HomeAssistant, statistic_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Every long-term row of `statistic_ids`, in import_statistics' shape.

    Blocking - run on the recorder's executor."""
    return {
        statistic_id: [_import_shape(row) for row in rows]
        for statistic_id, rows in _raw_rows(hass, statistic_ids).items()
    }


async def read_rows(
    hass: HomeAssistant,
    statistic_ids: list[str],
    table: type[StatisticsBase] = Statistics,
) -> dict[str, list[dict[str, Any]]]:
    """_raw_rows, on the recorder's executor."""
    return cast(
        dict[str, list[dict[str, Any]]],
        await _instance(hass).async_add_executor_job(
            _raw_rows, hass, statistic_ids, table
        ),
    )


async def read_metadata(
    hass: HomeAssistant, statistic_ids: list[str]
) -> dict[str, StatisticMetaData]:
    """{statistic_id: metadata} of the known ones among `statistic_ids`."""
    found = await _instance(hass).async_add_executor_job(
        partial(statistics.get_metadata, hass, statistic_ids=set(statistic_ids))
    )
    return {statistic_id: meta for statistic_id, (_, meta) in found.items()}


def statistic_data(row: dict[str, Any]) -> dict[str, Any]:
    """A raw row as the StatisticData the import functions take."""
    data: dict[str, Any] = {"start": dt_util.utc_from_timestamp(row["start_ts"])}
    if row.get("last_reset_ts") is not None:
        data["last_reset"] = dt_util.utc_from_timestamp(row["last_reset_ts"])
    data.update(
        (column, row[column])
        for column in _VALUE_COLUMNS
        if row.get(column) is not None
    )
    return data


def check_importable(metadata: StatisticMetaData | dict[str, Any]) -> None:
    """Refuse what HA's import would reject while it's being queued -
    checked before anything is queued, so a write never stops half-way (a
    restore must not clear its target and then fail to import). Mirrors
    recorder.statistics.async_import_statistics / async_add_external_
    statistics / _async_import_statistics."""
    statistic_id = metadata["statistic_id"]
    source = metadata["source"]
    if source == "recorder":
        if not valid_entity_id(statistic_id):
            raise StatisticsChangeRefusedError(
                f"'{statistic_id}' isn't an entity id, as an entity's own "
                "statistics need"
            )
    elif not (
        statistics.valid_statistic_id(statistic_id)
        and statistic_id.split(":", 1)[0] == source
    ):
        raise StatisticsChangeRefusedError(
            f"'{statistic_id}' isn't a valid external statistic id for "
            f"source '{source}'"
        )
    unit_class = metadata.get("unit_class")
    if unit_class is not None:
        converter = statistics.UNIT_CLASS_TO_UNIT_CONVERTER.get(unit_class)
        if converter is None or (
            metadata.get("unit_of_measurement") not in converter.VALID_UNITS
        ):
            raise StatisticsChangeRefusedError(
                f"'{statistic_id}' has unit {metadata.get('unit_of_measurement')!r} "
                f"in unit class {unit_class!r}, which HA's import refuses"
            )


def queue_import(
    hass: HomeAssistant, metadata: StatisticMetaData, rows: list[dict[str, Any]]
) -> None:
    """Queue hourly rows (raw shape) for import into metadata's statistic,
    through the same validation as WS recorder/import_statistics. Existing
    rows with the same start are overwritten, others inserted."""
    check_importable(metadata)
    data = [statistic_data(row) for row in rows]
    if metadata["source"] == "recorder":
        importer = statistics.async_import_statistics
    else:
        importer = statistics.async_add_external_statistics
    importer(hass, cast(StatisticMetaData, dict(metadata)), cast(Any, data))


def queue_short_term_seed(
    hass: HomeAssistant, metadata: StatisticMetaData, last_row: dict[str, Any]
) -> None:
    """Queue a 5-minute row carrying a meter series' last hourly sum/state,
    so the sensor's next compile continues from it rather than from 0 (see
    the module docstring). Placed in the last 5-minute slot of that hour,
    where the compile that made the hourly row took them from - so it
    rewrites that row with the same values when it still exists."""
    seed = statistic_data(
        {
            **{column: last_row.get(column) for column in ("state", "sum")},
            "last_reset_ts": last_row.get("last_reset_ts"),
            "start_ts": last_row["start_ts"] + 55 * 60,
        }
    )
    _instance(hass).async_import_statistics(dict(metadata), [seed], StatisticsShortTerm)


async def backup_document(
    hass: HomeAssistant, statistic_ids: list[str]
) -> dict[str, Any]:
    """The series of `statistic_ids` as recorder/import_statistics payloads -
    metadata plus every hourly row - so a mistaken clear is one import call
    per entry to undo."""
    instance = _instance(hass)
    metadata = await instance.async_add_executor_job(
        partial(statistics.get_metadata, hass, statistic_ids=set(statistic_ids))
    )
    rows = await instance.async_add_executor_job(_series_rows, hass, statistic_ids)
    entries = []
    for statistic_id in statistic_ids:
        if statistic_id not in metadata:
            continue
        meta = metadata[statistic_id][1]
        entries.append(
            {
                "metadata": {
                    "has_sum": meta["has_sum"],
                    "mean_type": int(meta.get("mean_type") or 0),
                    "name": meta.get("name"),
                    "source": meta["source"],
                    "statistic_id": statistic_id,
                    "unit_class": meta.get("unit_class"),
                    "unit_of_measurement": meta.get("unit_of_measurement"),
                },
                "stats": rows.get(statistic_id, []),
            }
        )
    return {
        "backed_up_at": dt_util.utcnow().isoformat(),
        "restore": RESTORE_HINT,
        "statistics": entries,
    }


def _resolve(done: Any) -> None:
    if not done.done():
        done.set_result(None)


async def on_recorder(
    hass: HomeAssistant, queue: Callable[[], None], *, what: str = "the change"
) -> None:
    """Run `queue` - which queues recorder tasks - and wait until the
    recorder thread has run them all. The queue is FIFO and `queue` runs
    without yielding, so nothing (not even a statistics compile) gets in
    between its tasks; a no-op metadata update with on_done marks the end."""
    instance = _instance(hass)
    done = hass.loop.create_future()

    def finished() -> None:
        hass.loop.call_soon_threadsafe(_resolve, done)

    queue()
    instance.async_update_statistics_metadata(DOMAIN, on_done=finished)
    try:
        async with asyncio.timeout(RECORDER_TIMEOUT):
            await done
    except TimeoutError as exc:
        raise StatisticsTimeoutError(
            f"the recorder hasn't finished {what} within "
            f"{RECORDER_TIMEOUT // 60} min - it's queued and may still run"
        ) from exc


# Extra barrier rounds after a write that doesn't verify yet: on
# MySQL/MariaDB, an import or adjust task that hits a lock-wait timeout or a
# deadlock re-queues itself at the end of the queue - behind the barrier
# (recorder.tasks.ImportStatisticsTask / AdjustStatisticsTask) - and one
# more round lets it run. On SQLite a failing task is dropped (and logged),
# so the check fails for good.
VERIFY_ROUNDS = 3


async def on_recorder_verified(
    hass: HomeAssistant,
    queue: Callable[[], None],
    verify: Callable[[], Awaitable[list[str]]],
    *,
    what: str,
) -> None:
    """on_recorder, then check the statistics hold what was written:
    `verify` returns the problems (empty when it landed). Raises
    StatisticsNotAppliedError naming them if it still hasn't after
    VERIFY_ROUNDS rounds - never reports a write as done that isn't
    (issue #145)."""
    await on_recorder(hass, queue, what=what)
    for _ in range(VERIFY_ROUNDS):
        if not (problems := await verify()):
            return
        await on_recorder(hass, lambda: None, what=what)
    if problems := await verify():
        raise StatisticsNotAppliedError(
            f"{what} didn't land as planned - {'; '.join(problems[:5])}"
            + (f" (and {len(problems) - 5} more)" if len(problems) > 5 else "")
            + ". Home Assistant's log names the recorder error"
        )


def _close(a: Any, b: Any) -> bool:
    """Equal as stored - all value columns are doubles; the margin only
    absorbs float rounding (a rebased sum is sum - offset + offset)."""
    if a is None or b is None:
        return a is b
    return abs(float(a) - float(b)) <= 1e-6 + 1e-12 * abs(float(b))


async def row_mismatches(
    hass: HomeAssistant, statistic_id: str, expected: list[dict[str, Any]]
) -> list[str]:
    """Where `statistic_id`'s hourly rows differ from `expected` (raw
    shape; only the values `expected` sets are compared)."""
    actual = {
        row["start_ts"]: row
        for row in (await read_rows(hass, [statistic_id])).get(statistic_id, [])
    }
    problems = []
    for row in expected:
        got = actual.get(row["start_ts"])
        if got is None:
            problems.append(f"{statistic_id} has no row for {_iso(row['start_ts'])}")
            continue
        for column in _VALUE_COLUMNS:
            if row.get(column) is not None and not _close(got[column], row[column]):
                problems.append(
                    f"{statistic_id} {_iso(row['start_ts'])}: {column} is "
                    f"{got[column]}, not {row[column]}"
                )
    return problems


async def clear_statistics(hass: HomeAssistant, statistic_ids: list[str]) -> None:
    """Delete `statistic_ids`' long- and short-term statistics and metadata,
    and check they're gone. A live entity may start a new series right
    after - only hourly rows from before the clear count as left over."""
    instance = _instance(hass)
    # The first compile after the clear writes the hour before the one it
    # runs in - anything older is left over.
    now = dt_util.utcnow().timestamp()
    before = now - now % 3600 - 3600

    async def verify() -> list[str]:
        left = await read_rows(hass, statistic_ids)
        return [
            f"{statistic_id} still has its rows"
            for statistic_id, rows in left.items()
            if any(row["start_ts"] < before for row in rows)
        ]

    await on_recorder_verified(
        hass,
        lambda: instance.async_clear_statistics(statistic_ids),
        verify,
        what="the clear",
    )


def _last_row(hass: HomeAssistant, statistic_id: str) -> dict[str, Any] | None:
    """The newest long-term row of `statistic_id`, raw. Blocking."""
    rows = _raw_rows(hass, [statistic_id]).get(statistic_id)
    return rows[-1] if rows else None


async def continuity_check(hass: HomeAssistant, statistic_id: str) -> dict[str, Any]:
    """Whether a meter's next 5-minute compile will continue its sum: the
    compile starts from the newest 5-minute row (see the module docstring),
    so that row's sum must not be below the last hourly one - or the next
    hour drops (issue #141). Only an entity's own statistics are compiled;
    external ones are written by their integration."""
    metadata = (await read_metadata(hass, [statistic_id])).get(statistic_id)
    if metadata is None or metadata["source"] != "recorder":
        return {"applies": False}
    instance = _instance(hass)
    hourly = await instance.async_add_executor_job(_last_row, hass, statistic_id)
    short = (await read_rows(hass, [statistic_id], StatisticsShortTerm)).get(
        statistic_id, []
    )
    check: dict[str, Any] = {"applies": True}
    if hourly is not None:
        check["last_hourly"] = {"start": _iso(hourly["start_ts"]), "sum": hourly["sum"]}
    if not short:
        check["ok"] = hourly is None
        if hourly is not None:
            check["note"] = (
                "no 5-minute row - the entity's next compile would start its "
                "sum again at 0"
            )
        return check
    latest = short[-1]
    check["latest_5min"] = {"start": _iso(latest["start_ts"]), "sum": latest["sum"]}
    drop = (hourly or {}).get("sum", 0.0) - (latest["sum"] or 0.0)
    check["ok"] = drop <= 1e-6
    if not check["ok"]:
        check["note"] = (
            f"the next compile continues from the 5-minute sum, so the next "
            f"hour would drop by {round(drop, 6)}"
        )
    return check


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def plan_migrate(
    hass: HomeAssistant,
    from_id: str,
    to_id: str,
    *,
    can_back_up: bool,
    allow_no_backup: bool = False,
) -> dict[str, Any]:
    """What moving `from_id`'s series onto `to_id` would do.

    `from_id` must be an entity's own statistic (source recorder) whose
    entity is gone - otherwise it would keep recording into it. A series
    already under `to_id` - e.g. the hours a replacement entity collected
    since it appeared - is cleared first, as HA can't merge two series;
    the units and sum/mean kind must match."""
    if from_id == to_id:
        raise StatisticsChangeRefusedError("from and to are the same statistic")
    described = await describe_statistics(hass, [from_id, to_id])
    source, target = described.get(from_id), described.get(to_id)
    problems: list[str] = []
    if source is None:
        problems.append(_unknown(from_id))
    elif source["source"] != "recorder":
        problems.append(
            f"'{from_id}' is an external statistic (source "
            f"'{source['source']}') - only an entity's own statistics can be "
            "moved"
        )
    elif source["has_entity"]:
        problems.append(
            f"'{from_id}' still belongs to an existing entity, which would "
            "keep recording into it - delete it (delete_entity) or rename it "
            "first"
        )
    if not valid_entity_id(to_id):
        # External statistics ('source:id') never are.
        problems.append(f"'{to_id}' isn't an entity id")
    if source is not None:
        state = hass.states.get(to_id)
        to_unit = (
            target["unit_of_measurement"]
            if target is not None
            else state.attributes.get("unit_of_measurement") if state else None
        )
        if (target is not None or state is not None) and to_unit != source[
            "unit_of_measurement"
        ]:
            problems.append(
                f"units differ: '{from_id}' is in "
                f"{source['unit_of_measurement']!r}, '{to_id}' in {to_unit!r}"
            )
        if target is not None and target["has_sum"] != source["has_sum"]:
            problems.append(
                "one is a sum (meter) statistic and the other a mean "
                "(measurement) one"
            )
    if target is not None and not can_back_up and not allow_no_backup:
        problems.append(_no_backup())
    if problems:
        raise StatisticsChangeRefusedError("; ".join(problems))
    assert source is not None
    last = await _instance(hass).async_add_executor_job(_last_row, hass, from_id)
    metadata = (await read_metadata(hass, [from_id]))[from_id]
    return {
        "from": source,
        "to": to_id,
        "replaces": target,
        "last_row": last,
        "metadata": {**metadata, "statistic_id": to_id},
    }


async def migrate_statistics(
    hass: HomeAssistant, plan: dict[str, Any]
) -> dict[str, Any]:
    """Apply plan_migrate's plan, then report where the series ended up and
    how it lines up with the entity's current state."""
    from_id, target = plan["from"]["statistic_id"], plan["to"]
    instance = _instance(hass)
    last = plan["last_row"]
    # HA refuses to rename onto an id in use, but the seed below would still
    # land - in that series' 5-minute rows, which its next compile then
    # continues from. A target that got a series since the plan (an entity
    # recording for the first time) is refused here instead.
    if not plan["replaces"] and target in await read_metadata(hass, [target]):
        raise StatisticsChangeRefusedError(
            f"'{target}' has statistics of its own since this was planned - "
            "preview again (they're replaced, after a backup)"
        )

    def queue() -> None:
        # Queued together so nothing - not even the 5-minute compile that
        # would recreate the cleared series - runs between them.
        if plan["replaces"]:
            instance.async_clear_statistics([target])
        instance.async_update_statistics_metadata(from_id, new_statistic_id=target)
        if plan["from"]["has_sum"] and last is not None:
            # The moved series' own 5-minute rows are gone once it's been
            # dead for longer than the recorder keeps them (10 days by
            # default) - without one, the entity's next compile would
            # restart the sum at 0 (issue #136).
            queue_short_term_seed(hass, plan["metadata"], last)

    async def verify() -> list[str]:
        after = await describe_statistics(hass, [from_id, target])
        if from_id in after:
            return [f"{from_id} is still there"]
        if target not in after:
            return [f"{target} isn't there"]
        if after[target].get("first_period") != plan["from"]["first_period"]:
            return [f"{target} doesn't start where {from_id} did"]
        return []

    try:
        await on_recorder_verified(hass, queue, verify, what="the move")
        moved = True
    except StatisticsNotAppliedError:
        moved = False
    after = await describe_statistics(hass, [from_id, target])
    result: dict[str, Any] = {"moved": moved, "statistic": after.get(target)}
    if not moved:
        result["note"] = (
            "HA didn't move the series - Home Assistant's log says why; "
            "'statistic' is what's under the new id now"
        )
    if moved and last is not None:
        since = dt_util.utcnow() - dt_util.utc_from_timestamp(last["start_ts"])
        continuity: dict[str, Any] = {
            "last_period": _iso(last["start_ts"]),
            "hours_since_last_period": round(since.total_seconds() / 3600, 1),
        }
        state = hass.states.get(target)
        current = _number(state.state) if state else None
        if last.get("state") is not None and current is not None:
            continuity.update(
                last_state=last["state"],
                current_state=current,
                state_jump=round(current - last["state"], 6),
            )
        result["continuity"] = continuity
    if moved and plan["from"]["has_sum"]:
        result["next_compile"] = await continuity_check(hass, target)
    return result


async def rename_conflicts(
    hass: HomeAssistant, renames: list[tuple[str, str]]
) -> dict[str, str]:
    """{old entity_id: warning} for renames whose new id already has
    statistics - HA won't move the entity's statistics onto it."""
    ids = [entity_id for rename in renames for entity_id in rename]
    described = await describe_statistics(hass, ids)
    warnings = {}
    for old, new in renames:
        existing = described.get(new)
        if existing is None:
            continue
        warning = (
            f"{new} already has statistics {existing['first_period']} -> "
            f"{existing['last_period']}; HA can't move this entity's "
            "statistics onto it, so the entity continues that existing series"
        )
        if own := described.get(old):
            warning += (
                f" and its own history ({own['first_period']} -> "
                f"{own['last_period']}) stays under {old} as an orphan. To "
                f"keep it, rename, then migrate_statistics from={old} "
                f"to={new} (replaces the series under {new})"
            )
        warnings[old] = warning + "."
    return warnings
