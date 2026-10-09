"""Merging statistics series into one (issue #136).

HA keeps exactly one series per statistic_id and can't combine two: a
rename or migrate onto an id that has history replaces it. Merging is for
when both histories matter - several old meters that became one (their
hours added), or a replacement that ran in parallel for a while before the
old device was retired.

Works on hourly rows read raw from the recorder, converted to the
target's unit (within one unit class - Wh and kWh, not kWh and m³):

- meter (sum) series: each series' per-hour *change* (sum minus the
  previous row's sum; a series' first row counts from 0, the sum's own
  baseline) is combined per hour, and the target's sum is rebuilt from
  them from the start - continuous, no jump where one series ends and the
  next begins. `state` stays the target's own where it has a row.
- measurement (mean) series: rows are taken as they are; overlapping hours
  are resolved by the overlap rule, never added.

Overlapping hours - in more than one series - follow `overlap`:
`refuse` (the default), `target_wins`, `source_wins` (sources in the
order given) or `add` (meters only).

Writing goes through HA's own import task, which overwrites the target's
rows by start and inserts the others. A live meter keeps recording during
the merge, and its next 5-minute compile continues from its latest
5-minute row (statistics_manager.py) - which still has the old sum basis.
So the rows from the point where the target's offset to the old sums
becomes constant are imported on the *old* basis, and that offset is then
added with HA's own sum adjustment, which shifts the hourly and 5-minute
rows from that point in one transaction on the recorder thread - including
any row a compile wrote in the meantime. 5-minute rows before that point
(only within the recorder's 10-day short-term window) keep the old basis;
only 5-minute graphs of that window show it.

Sources are left as they are; clear_statistics removes them afterwards
(with its own backups).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticMetaData
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import statistics_backup as backups
from . import statistics_manager as sm

OVERLAP_RULES = ("refuse", "target_wins", "source_wins", "add")
_HOUR = 3600.0
_EPSILON = 1e-9


@dataclass(slots=True)
class Series:
    """One input series: raw hourly rows in the target's unit."""

    statistic_id: str
    rows: list[dict[str, Any]]
    is_target: bool = False
    by_start: dict[float, dict[str, Any]] = field(init=False)
    changes: dict[float, float] = field(init=False)

    def __post_init__(self) -> None:
        self.by_start = {row["start_ts"]: row for row in self.rows}
        self.changes = {}
        previous = 0.0
        for row in self.rows:
            if row.get("sum") is None:
                continue
            self.changes[row["start_ts"]] = row["sum"] - previous
            previous = row["sum"]


def _iso(timestamp: float) -> str:
    return dt_util.utc_from_timestamp(timestamp).isoformat()


def combine(
    target: Series,
    sources: list[Series],
    *,
    has_sum: bool,
    overlap: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The merged rows (raw shape) and a report: overlaps and how they're
    resolved, seams where the contributing series change, and for meters
    the total change of the inputs vs the result."""
    order = [target, *sources] if overlap != "source_wins" else [*sources, target]
    hours = sorted({start for series in order for start in series.by_start})
    overlaps: dict[tuple[str, ...], list[float]] = {}
    seams: list[dict[str, Any]] = []
    merged: list[dict[str, Any]] = []
    total = 0.0
    previous: tuple[float, list[Series]] | None = None
    for hour in hours:
        present = [series for series in order if hour in series.by_start]
        if len(present) > 1:
            key = tuple(series.statistic_id for series in present)
            overlaps.setdefault(key, []).append(hour)
        chosen = present if overlap == "add" else present[:1]
        if previous is not None and [s.statistic_id for s in previous[1]] != [
            s.statistic_id for s in present
        ]:
            seams.append(_seam(previous, (hour, present)))
        previous = (hour, present)
        # State (and last_reset) follow the target's own row where there is
        # one, else the one series that row comes from.
        state_from = (
            target if target in present else chosen[0] if len(chosen) == 1 else None
        )
        row: dict[str, Any] = {"start_ts": hour}
        if state_from is not None:
            own = state_from.by_start[hour]
            row["state"] = own.get("state")
            row["last_reset_ts"] = own.get("last_reset_ts")
        if has_sum:
            total += sum(series.changes.get(hour, 0.0) for series in chosen)
            row["sum"] = total
        else:
            own = chosen[0].by_start[hour]
            row.update({column: own.get(column) for column in ("mean", "min", "max")})
        merged.append(row)

    report: dict[str, Any] = {
        "overlaps": [
            {
                "series": list(key),
                "hours": len(starts),
                "first": _iso(starts[0]),
                "last": _iso(starts[-1]),
                "resolution": (
                    "added"
                    if overlap == "add"
                    else f"{key[0]} wins" if overlap != "refuse" else "refused"
                ),
            }
            for key, starts in overlaps.items()
        ],
        "seams": seams,
    }
    if has_sum:
        inputs = sum(sum(series.changes.values()) for series in order)
        report["totals"] = {
            "inputs": round(inputs, 6),
            "result": round(total, 6),
            "dropped_by_overlap": round(inputs - total, 6),
        }
    return merged, report


def _seam(
    before: tuple[float, list[Series]], after: tuple[float, list[Series]]
) -> dict[str, Any]:
    """Where the set of contributing series changes: the gap between the
    two hours and, between two single series, the jump in state."""
    (hour_before, series_before), (hour_after, series_after) = before, after
    seam: dict[str, Any] = {
        "after": _iso(hour_before),
        "before": _iso(hour_after),
        "from": [series.statistic_id for series in series_before],
        "to": [series.statistic_id for series in series_after],
        "gap_hours": round((hour_after - hour_before) / _HOUR - 1, 2),
    }
    if len(series_before) == 1 and len(series_after) == 1:
        state_before = series_before[0].by_start[hour_before].get("state")
        state_after = series_after[0].by_start[hour_after].get("state")
        if state_before is not None and state_after is not None:
            seam["state_jump"] = round(state_after - state_before, 6)
    return seam


def rebase_point(
    target: Series,
    merged: list[dict[str, Any]],
    *,
    first_short_term: float | None,
    now: float,
) -> tuple[float | None, float]:
    """(start, offset): from `start` on, every target row - hourly and
    5-minute - gets `offset` added by HA's sum adjustment (see the module
    docstring); merged rows from `start` on are written `offset` lower, so
    the adjustment lands them where they belong.

    With hourly rows, `start` is where the target's offset to its old sums
    becomes constant. Without - a target only minutes old, with at most a
    few 5-minute rows (issue #141) - its own sums are all newer than the
    merged hours and count from 0, so the offset is the merged total, from
    its first 5-minute row's hour, or the current hour if it has none yet.
    (None, 0) when nothing needs shifting."""
    if target.rows:
        new_sums = {row["start_ts"]: row["sum"] for row in merged}
        starts = [row["start_ts"] for row in target.rows if row.get("sum") is not None]
        offset = new_sums[starts[-1]] - target.by_start[starts[-1]]["sum"]
        start = starts[-1]
        for hour in reversed(starts):
            if abs(new_sums[hour] - target.by_start[hour]["sum"] - offset) > _EPSILON:
                break
            start = hour
        return start, offset
    if not merged:
        return None, 0.0
    since = first_short_term if first_short_term is not None else now
    return since - since % _HOUR, merged[-1]["sum"]


@dataclass(slots=True)
class MergePlan:
    """What plan_merge works out, for the preview and the write."""

    target: dict[str, Any]
    sources: list[dict[str, Any]]
    metadata: dict[str, Any]
    overlap: str
    rows: list[dict[str, Any]]
    report: dict[str, Any]
    rebase: tuple[float | None, float]
    # A target without any 5-minute row yet gets one carrying the merged
    # sum, or its first compile would start again at 0 (issue #141).
    seed: bool = False
    # The target's own hourly rows by start, as they were planned against:
    # rows the merge leaves as they are aren't written again.
    existing: dict[float, dict[str, Any]] = field(default_factory=dict)

    def preview(self) -> dict[str, Any]:
        """JSON-safe summary for the tool's preview and result."""
        start, offset = self.rebase
        return {
            "target": self.target,
            "sources": self.sources,
            "overlap": self.overlap,
            "rows_written": len(self.rows),
            **self.report,
            **(
                {
                    "sum_shift": {
                        "from": _iso(start),
                        "offset": round(offset, 6),
                    }
                }
                if start is not None and abs(offset) > _EPSILON
                else {}
            ),
        }


def _in_range(
    rows: list[dict[str, Any]], start: datetime | None, end: datetime | None
) -> list[dict[str, Any]]:
    low = start.timestamp() if start else float("-inf")
    high = end.timestamp() if end else float("inf")
    return [row for row in rows if low <= row["start_ts"] < high]


async def plan_merge(
    hass: HomeAssistant,
    target_id: str,
    source_ids: list[str],
    *,
    overlap: str = "refuse",
    start: datetime | None = None,
    end: datetime | None = None,
    can_back_up: bool,
    allow_no_backup: bool = False,
) -> MergePlan:
    """Check and compute a merge without writing anything."""
    sources_wanted = list(dict.fromkeys(source_ids))
    ids = [target_id, *sources_wanted]
    described = await sm.describe_statistics(hass, ids)
    metadata = await sm.read_metadata(hass, ids)
    problems = [
        sm._unknown(statistic_id)
        for statistic_id in ids
        if statistic_id not in described
    ]
    if target_id in sources_wanted:
        problems.append("the target can't also be a source")
    if backups.is_backup(target_id):
        problems.append(
            "the target can't be a backup - restore_statistics restores one"
        )
    if start and end and start >= end:
        problems.append("start must be before end")
    if problems:
        raise sm.StatisticsChangeRefusedError("; ".join(problems))

    target_meta = metadata[target_id]
    has_sum = bool(target_meta["has_sum"])
    if overlap == "add" and not has_sum:
        problems.append(
            "overlap='add' only works for meter (sum) statistics - "
            "measurements can't be added"
        )
    raw = await sm.read_rows(hass, ids)
    inputs: list[Series] = []
    for source_id in sources_wanted:
        if bool(metadata[source_id]["has_sum"]) != has_sum:
            problems.append(
                f"'{source_id}' and the target differ in kind - one is a sum "
                "(meter) statistic and the other a mean (measurement) one"
            )
            continue
        convert, unit_problem = backups.unit_converter(
            dict(metadata[source_id]), target_meta.get("unit_of_measurement")
        )
        if unit_problem:
            problems.append(unit_problem)
            continue
        rows = backups.convert_rows(raw.get(source_id, []), convert)
        series = Series(source_id, rows)
        # Changes come from the whole series, so a range keeps the first
        # hour's own change rather than its sum since the series began.
        kept = {row["start_ts"] for row in _in_range(rows, start, end)}
        series.by_start = {k: v for k, v in series.by_start.items() if k in kept}
        series.changes = {k: v for k, v in series.changes.items() if k in kept}
        inputs.append(series)
    if not can_back_up and not allow_no_backup:
        problems.append(sm._no_backup())
    if problems:
        raise sm.StatisticsChangeRefusedError("; ".join(problems))

    target = Series(target_id, raw.get(target_id, []), is_target=True)
    rows, report = combine(target, inputs, has_sum=has_sum, overlap=overlap)
    if overlap == "refuse" and report["overlaps"]:
        raise sm.StatisticsChangeRefusedError(
            "these series overlap: "
            + "; ".join(
                f"{' and '.join(item['series'])} in {item['hours']} hour(s), "
                f"{item['first']} -> {item['last']}"
                for item in report["overlaps"]
            )
            + " - pass overlap=target_wins, source_wins or add (meters) to "
            "resolve them, or start/end to leave them out"
        )
    short_term = (await sm.read_rows(hass, [target_id], StatisticsShortTerm)).get(
        target_id, []
    )
    rebase = (
        rebase_point(
            target,
            rows,
            first_short_term=short_term[0]["start_ts"] if short_term else None,
            now=dt_util.utcnow().timestamp(),
        )
        if has_sum
        else (None, 0.0)
    )
    return MergePlan(
        target=described[target_id],
        sources=[described[source_id] for source_id in sources_wanted],
        metadata=dict(target_meta),
        overlap=overlap,
        rows=rows,
        report=report,
        rebase=rebase,
        # Only an entity's own statistics are compiled from 5-minute rows;
        # an external one (an integration's import) never reads them.
        seed=(
            has_sum
            and target_meta["source"] == "recorder"
            and not short_term
            and bool(rows)
        ),
        existing=target.by_start,
    )


def _unchanged(row: dict[str, Any], existing: dict[str, Any] | None) -> bool:
    """Whether importing `row` would leave the target's row as it is."""
    if existing is None:
        return False
    written, held = sm.statistic_data(row), sm.statistic_data(existing)
    return written.keys() == held.keys() and all(
        (
            written[key] == held[key]
            if key in ("start", "last_reset")
            else sm._close(written[key], held[key])
        )
        for key in written
    )


async def merge_statistics(hass: HomeAssistant, plan: MergePlan) -> dict[str, Any]:
    """Write the merged rows - from the rebase point on with the old sum
    basis, then HA's sum adjustment for the offset (see the module
    docstring). The caller backs the target up first.

    Two phases, each checked against the statistics before the next is
    queued: the import (and seed), then the adjustment. The adjustment adds
    to whatever the rows hold when it runs, so it must never run before
    the import - and on MySQL/MariaDB an import that hits a lock timeout
    re-queues itself behind anything queued after it. Rows the target
    already holds as planned aren't written again: from the rebase point
    on, that's all of a live meter's own hours."""
    target_id = plan.metadata["statistic_id"]
    start, offset = plan.rebase
    shift_from = start if start is not None and abs(offset) > _EPSILON else None
    rows = [
        (
            {**row, "sum": row["sum"] - offset}
            if shift_from is not None
            and row["start_ts"] >= shift_from
            and row.get("sum") is not None
            else row
        )
        for row in plan.rows
    ]
    written = [
        row for row in rows if not _unchanged(row, plan.existing.get(row["start_ts"]))
    ]
    metadata = cast(StatisticMetaData, plan.metadata)
    sm.check_importable(metadata)
    # The compile measures from this row's state, so it's the target
    # meter's own reading - the merged rows' state may be a source's.
    state = hass.states.get(target_id)
    reading = sm._number(state.state) if state else None
    seed = (
        None
        if not plan.seed
        else rows[-1] if reading is None else {**rows[-1], "state": reading}
    )

    def queue_import() -> None:
        if written:
            sm.queue_import(hass, metadata, written)
        if seed is not None:
            sm.queue_short_term_seed(hass, metadata, seed)

    async def imported() -> list[str]:
        return await sm.row_mismatches(hass, target_id, written)

    await sm.on_recorder_verified(
        hass, queue_import, imported, what="the merged rows' import"
    )
    if shift_from is not None:

        def queue_adjust() -> None:
            # The rows are in the target's own unit, so is the offset.
            sm.queue_adjust(
                hass, target_id, dt_util.utc_from_timestamp(shift_from), offset
            )

        async def adjusted() -> list[str]:
            return await sm.row_mismatches(
                hass,
                target_id,
                [row for row in plan.rows if row["start_ts"] >= shift_from],
            )

        await sm.on_recorder_verified(
            hass, queue_adjust, adjusted, what="the sum adjustment"
        )
    after = (await sm.describe_statistics(hass, [target_id]))[target_id]
    result: dict[str, Any] = {
        "merged": {**plan.preview(), "target": after, "rows_changed": len(written)}
    }
    if plan.metadata["has_sum"]:
        result["next_compile"] = await sm.continuity_check(hass, target_id)
    return result
