"""Writing statistic values that no other statistic has (issue #166).

derive_statistics scales an existing meter, merge_statistics combines
existing series, redistribute_statistics moves energy between hours with
every total unchanged - none of them can write new values. This can:

- **import**: hourly rows for one statistic, given inline or as a JSON/CSV
  file in the mirror repo. A meter or cost (`kind: sum`) takes each hour's
  *change* (`{start, change}`) and builds the sums from the statistic's
  own sum just before the range; inside `start..end` the rows replace what
  is there (an existing hour without a given row counts 0), after `end`
  every row keeps its own change and the sums carry on seamlessly. A
  measurement (`kind: mean`, e.g. a price) takes `{start, mean, min?,
  max?}`; an existing hour in the range must be given too.
- **adjust**: set the change of single hours of a meter (`[{start,
  change}]`); every later sum moves by the difference.

The seam after the range is our own sum adjustment
(statistics_manager.queue_adjust: hourly and 5-minute rows in one
transaction, retried on lock errors) - never HA's own adjust_statistics,
which can commit half of it on MySQL/MariaDB (#150). Import first, then
the adjustment, each checked against the recorder before the next is
queued - as statistics_merge.merge_statistics.

5-minute rows inside the range (the recorder keeps them ~10 days) are
rewritten too: each written hour split evenly over its 12 slots, or by
the row's own `five_minute` values (12, adding up to the hour); an hour
adjust mode leaves as it was keeps its 5-minute shape, moved to its new
sum. A new statistic (under `ha_dev_tools:`) has none - nothing compiles
it.

Refused: a meter hour that would go down, unless allow_negative; an
hour that isn't complete yet; anything not on an hour; a mean import
that leaves out hours the statistic has in the range. All or nothing:
every problem is listed, and nothing is written while there is one.
"""

from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models import (
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import valid_statistic_id
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import statistics_backup as backups
from . import statistics_manager as sm
from .const import DOMAIN
from .statistics_redistribute import HOUR, SLOT, SLOTS_PER_HOUR, Series

KINDS = ("sum", "mean")
MODES = ("import", "adjust")
# How far 12 five_minute values may miss their hour's change.
SLOT_TOLERANCE = 0.001
_NEW_PREFIX = f"{DOMAIN}:"
_EPSILON = 1e-9


def _iso(timestamp: float) -> str:
    return dt_util.utc_from_timestamp(timestamp).isoformat()


def _finite(value: Any) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


@dataclass(slots=True)
class WritePlan:
    """What plan_write works out, for the preview and the write."""

    target: dict[str, Any]
    metadata: dict[str, Any]
    rows: list[dict[str, Any]]
    short_term_rows: list[dict[str, Any]]
    # (from, offset): every row from `from` on moves by `offset`.
    seam: tuple[float, float] | None
    # The rows the seam moves, as they'll be once it has - for the check:
    # (hourly, 5-minute).
    shifted: tuple[list[dict[str, Any]], list[dict[str, Any]]]
    report: dict[str, Any]

    def preview(self) -> dict[str, Any]:
        return {"target": self.target, **self.report}


def parse_rows(content: str, path: str) -> list[dict[str, Any]]:
    """Rows from a file's text: JSON (a list, or {"rows": [...]}) or CSV
    with a header (start, then change or mean/min/max)."""
    if path.lower().endswith(".csv"):
        reader = csv.DictReader(io.StringIO(content))
        rows: list[dict[str, Any]] = []
        for number, line in enumerate(reader, start=2):
            row: dict[str, Any] = {}
            for key, value in line.items():
                if key is None or value is None or value == "":
                    continue
                key = key.strip()
                if key == "start":
                    row[key] = value.strip()
                    continue
                try:
                    row[key] = float(value)
                except ValueError:
                    raise sm.StatisticsChangeRefusedError(
                        f"{path} line {number}: {key} {value!r} isn't a number"
                    ) from None
            rows.append(row)
        return rows
    try:
        document = json.loads(content)
    except ValueError as err:
        raise sm.StatisticsChangeRefusedError(
            f"{path} is neither CSV (by its name) nor JSON: {err}"
        ) from None
    if isinstance(document, dict):
        document = document.get("rows")
    if not isinstance(document, list):
        raise sm.StatisticsChangeRefusedError(
            f"{path} holds a list of rows, or {{'rows': [...]}}"
        )
    return document


@dataclass(slots=True)
class _Row:
    start: float
    values: dict[str, float]
    five_minute: list[float] | None


def _parse(
    rows: list[Any], kind: str, now_hour: float, problems: list[str]
) -> dict[float, _Row]:
    """{hour: row} from the given rows, with every problem collected."""
    value_keys = ("change",) if kind == "sum" else ("mean", "min", "max")
    parsed: dict[float, _Row] = {}
    for index, item in enumerate(rows):
        where = f"row {index}"
        if not isinstance(item, dict):
            problems.append(f"{where} is an object, got {item!r}")
            continue
        start = dt_util.parse_datetime(str(item.get("start", "")))
        if start is None:
            problems.append(
                f"{where}: start is an ISO 8601 time, got {item.get('start')!r}"
            )
            continue
        timestamp = dt_util.as_utc(start).timestamp()
        where = f"row {index} ({_iso(timestamp)})"
        if timestamp % HOUR:
            problems.append(f"{where}: start is the start of an hour")
            continue
        if timestamp >= now_hour:
            problems.append(f"{where}: that hour isn't complete yet")
            continue
        if timestamp in parsed:
            problems.append(f"{where}: that hour is given twice")
            continue
        unknown = set(item) - {"start", "five_minute", *value_keys}
        if unknown:
            problems.append(
                f"{where}: {', '.join(sorted(unknown))} isn't a {kind} row's "
                f"(start, {', '.join(value_keys)}, five_minute)"
            )
            continue
        main = value_keys[0]
        if not _finite(item.get(main)):
            problems.append(
                f"{where}: {main} is a finite number, got {item.get(main)!r}"
            )
            continue
        values = {main: float(item[main])}
        for key in value_keys[1:]:
            if key in item:
                if not _finite(item[key]):
                    problems.append(f"{where}: {key} is a finite number")
                    break
                values[key] = float(item[key])
        else:
            if kind == "mean":
                low = values.setdefault("min", values["mean"])
                high = values.setdefault("max", values["mean"])
                if not low <= values["mean"] <= high:
                    problems.append(f"{where}: min <= mean <= max")
                    continue
            five = item.get("five_minute")
            if five is not None and (
                not isinstance(five, list)
                or len(five) != SLOTS_PER_HOUR
                or not all(_finite(value) for value in five)
            ):
                problems.append(f"{where}: five_minute is {SLOTS_PER_HOUR} numbers")
                continue
            parsed[timestamp] = _Row(
                timestamp,
                values,
                [float(value) for value in five] if five is not None else None,
            )
    return parsed


def _by_month(changes: dict[float, float]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for hour, change in sorted(changes.items()):
        month = dt_util.as_local(dt_util.utc_from_timestamp(hour)).strftime("%Y-%m")
        totals[month] = totals.get(month, 0.0) + change
    return totals


def _month_report(
    before: dict[float, float], after: dict[float, float]
) -> list[dict[str, Any]]:
    old, new = _by_month(before), _by_month(after)
    return [
        {
            "month": month,
            "before": round(old.get(month, 0.0), 6),
            "after": round(new.get(month, 0.0), 6),
            "difference": round(new.get(month, 0.0) - old.get(month, 0.0), 6),
        }
        for month in sorted(set(old) | set(new))
    ]


async def plan_write(
    hass: HomeAssistant,
    statistic_id: str,
    rows: list[Any],
    *,
    mode: str = "import",
    kind: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    unit: str | None = None,
    name: str | None = None,
    allow_negative: bool = False,
    can_back_up: bool,
    allow_no_backup: bool = False,
) -> WritePlan:
    """Work out a write without writing anything."""
    problems: list[str] = []
    if mode not in MODES:
        problems.append(f"mode is one of {MODES}")
    if kind is not None and kind not in KINDS:
        problems.append(f"kind is one of {KINDS}")
    if start and end and start >= end:
        problems.append("start must be before end")
    if not rows:
        problems.append("no rows given")
    meta = (await sm.read_metadata(hass, [statistic_id])).get(statistic_id)
    if meta is None:
        if mode == "adjust":
            problems.append(f"{sm._unknown(statistic_id)} - adjust needs one")
        elif not (
            statistic_id.startswith(_NEW_PREFIX) and valid_statistic_id(statistic_id)
        ):
            problems.append(
                f"'{statistic_id}' doesn't exist - a new statistic is created only "
                f"under '{_NEW_PREFIX}' (a lowercase slug)"
            )
        elif kind is None or unit is None:
            problems.append("a new statistic needs kind and unit")
    else:
        own_kind = (
            "sum" if meta["has_sum"] else "mean" if meta.get("mean_type") else None
        )
        if own_kind is None:
            problems.append(f"'{statistic_id}' has neither a sum nor a mean")
        elif kind is not None and kind != own_kind:
            problems.append(f"'{statistic_id}' is a {own_kind} statistic, not {kind}")
        else:
            kind = own_kind
        if unit is not None and unit != meta.get("unit_of_measurement"):
            problems.append(
                f"'{statistic_id}' is in {meta.get('unit_of_measurement')!r}, not "
                f"{unit!r}"
            )
        if backups.is_backup(statistic_id):
            problems.append(
                "a backup can't be written - restore_statistics restores one"
            )
        if not can_back_up and not allow_no_backup:
            problems.append(sm._no_backup())
    if mode == "adjust" and kind == "mean":
        problems.append("adjust is for a meter (sum) - import a mean's hours instead")
    if problems:
        raise sm.StatisticsChangeRefusedError("; ".join(problems))
    assert kind is not None

    now = dt_util.utcnow().timestamp()
    given = _parse(rows, kind, now - now % HOUR, problems)
    if problems:
        raise sm.StatisticsChangeRefusedError("; ".join(problems))
    hours = sorted(given)
    low = start.timestamp() if start else hours[0]
    high = end.timestamp() if end else hours[-1] + HOUR
    if mode == "adjust":
        low, high = hours[0], hours[-1] + HOUR
    if high > now - now % HOUR:
        raise sm.StatisticsChangeRefusedError(
            f"end {_iso(high)} is after the last complete hour - the current "
            "hour is still being recorded"
        )
    outside = [hour for hour in hours if not low <= hour < high]
    if outside:
        raise sm.StatisticsChangeRefusedError(
            f"{len(outside)} row(s) outside start..end, the first {_iso(outside[0])}"
        )

    if meta is None:
        return _new_statistic(statistic_id, kind, unit, name, given)
    raw = await sm.read_rows(hass, [statistic_id])
    short = await sm.read_rows(hass, [statistic_id], StatisticsShortTerm)
    series = Series(statistic_id, raw.get(statistic_id, []), HOUR)
    short_series = Series(statistic_id, short.get(statistic_id, []), SLOT)
    # From the raw rows: Series keeps only rows with a sum, a mean's have none.
    oldest = min((row["start_ts"] for row in short.get(statistic_id, [])), default=None)
    five_from = oldest - oldest % HOUR if oldest is not None else float("inf")
    for hour in hours:
        if given[hour].five_minute is not None and hour < five_from:
            problems.append(
                f"{_iso(hour)}: five_minute given, but '{statistic_id}' has no "
                "5-minute rows for it"
            )
    if problems:
        raise sm.StatisticsChangeRefusedError("; ".join(problems))
    target = (await sm.describe_statistics(hass, [statistic_id]))[statistic_id]
    if kind == "mean":
        # Series keeps only rows with a sum - a mean's are its raw rows.
        return _mean_plan(
            target, dict(meta), raw.get(statistic_id, []), given, low, high, five_from
        )
    return _sum_plan(
        target,
        dict(meta),
        series,
        short_series,
        given,
        mode,
        low,
        high,
        five_from,
        allow_negative,
    )


def _new_statistic(
    statistic_id: str,
    kind: str,
    unit: str | None,
    name: str | None,
    given: dict[float, _Row],
) -> WritePlan:
    rows: list[dict[str, Any]] = []
    total = 0.0
    for hour in sorted(given):
        row = given[hour]
        if row.five_minute is not None:
            raise sm.StatisticsChangeRefusedError(
                "five_minute is for a statistic with 5-minute rows - a new one "
                "has none"
            )
        if kind == "sum":
            total += row.values["change"]
            rows.append({"start_ts": hour, "sum": total})
        else:
            rows.append({"start_ts": hour, **row.values})
    report: dict[str, Any] = {
        "kind": kind,
        "mode": "import",
        "range": {
            "start": _iso(rows[0]["start_ts"]),
            "end": _iso(rows[-1]["start_ts"]),
        },
        "rows_written": len(rows),
    }
    if kind == "sum":
        changes = {hour: given[hour].values["change"] for hour in given}
        report["total"] = round(total, 6)
        report["by_month"] = _month_report({}, changes)
    return WritePlan(
        target={"statistic_id": statistic_id, "new": True},
        metadata={
            "has_sum": kind == "sum",
            "mean_type": (
                StatisticMeanType.NONE
                if kind == "sum"
                else StatisticMeanType.ARITHMETIC
            ),
            "name": name or statistic_id,
            "source": DOMAIN,
            "statistic_id": statistic_id,
            "unit_class": None,
            "unit_of_measurement": unit,
        },
        rows=rows,
        short_term_rows=[],
        seam=None,
        shifted=([], []),
        report=report,
    )


def _mean_plan(
    target: dict[str, Any],
    metadata: dict[str, Any],
    raw_rows: list[dict[str, Any]],
    given: dict[float, _Row],
    low: float,
    high: float,
    five_from: float,
) -> WritePlan:
    existing = {
        row["start_ts"]: row
        for row in raw_rows
        if low <= row["start_ts"] < high and row.get("mean") is not None
    }
    left_out = sorted(hour for hour in existing if hour not in given)
    if left_out:
        raise sm.StatisticsChangeRefusedError(
            f"the import leaves out {len(left_out)} hour(s) '{metadata['statistic_id']}' "
            f"has in the range, {_iso(left_out[0])} -> {_iso(left_out[-1])} - give "
            "them too, or narrow start/end"
        )
    rows = [{"start_ts": hour, **given[hour].values} for hour in sorted(given)]
    short_rows = []
    for hour in sorted(given):
        if hour < five_from:
            continue
        row = given[hour]
        for index in range(SLOTS_PER_HOUR):
            value = row.five_minute[index] if row.five_minute else None
            short_rows.append(
                {"start_ts": hour + index * SLOT}
                | (
                    {"mean": value, "min": value, "max": value}
                    if value is not None
                    else dict(row.values)
                )
            )
    before = {hour: row["mean"] for hour, row in existing.items()}
    after = {hour: given[hour].values["mean"] for hour in given}
    report: dict[str, Any] = {
        "kind": "mean",
        "mode": "import",
        "range": {"start": _iso(low), "end": _iso(high - HOUR)},
        "rows_written": len(rows),
        "hours_replaced": len(existing),
        "hours_added": len(set(given) - set(existing)),
        "mean_before": (
            round(sum(before.values()) / len(before), 6) if before else None
        ),
        "mean_after": round(sum(after.values()) / len(after), 6),
        "five_minute_rows_written": len(short_rows),
    }
    return WritePlan(target, metadata, rows, short_rows, None, ([], []), report)


def _sum_plan(
    target: dict[str, Any],
    metadata: dict[str, Any],
    series: Series,
    short_series: Series,
    given: dict[float, _Row],
    mode: str,
    low: float,
    high: float,
    five_from: float,
    allow_negative: bool,
) -> WritePlan:
    before_row = series.last_row(low - HOUR)
    anchor = before_row["sum"] if before_row is not None else 0.0
    base_state = before_row.get("state") if before_row is not None else None
    last_reset = before_row.get("last_reset_ts") if before_row is not None else None
    # Every hour the range holds a row for - an hour with only 5-minute rows
    # too, or they'd stay on the old sums.
    in_range = {hour for hour in series.by_start if low <= hour < high} | {
        slot - slot % HOUR for slot in short_series.by_start if low <= slot < high
    }
    written = sorted(in_range | set(given))
    old_change = {hour: series.change(hour) or 0.0 for hour in written}
    if before_row is None and written and written[0] in series.by_start:
        # The series' very first row: its change is its own sum.
        old_change[written[0]] = series.by_start[written[0]]["sum"]

    def change(hour: float) -> float:
        if hour in given:
            return given[hour].values["change"]
        return old_change[hour] if mode == "adjust" else 0.0

    new_change = {hour: change(hour) for hour in written}
    problems: list[str] = []
    negative = [hour for hour, value in new_change.items() if value < -_EPSILON]
    if negative and not allow_negative:
        problems.append(
            f"{len(negative)} hour(s) would go down ({', '.join(map(_iso, negative[:5]))}"
            f"{', ...' if len(negative) > 5 else ''}) - pass allow_negative for a "
            "real decrease (a credit, a correction)"
        )
    for hour in written:
        five = given[hour].five_minute if hour in given else None
        if five is not None and abs(sum(five) - new_change[hour]) > SLOT_TOLERANCE:
            problems.append(
                f"{_iso(hour)}: the five_minute values add up to "
                f"{round(sum(five), 6)}, not the hour's change {new_change[hour]}"
            )
        if five is not None and not allow_negative and min(five) < -_EPSILON:
            problems.append(f"{_iso(hour)}: a five_minute value is negative")
    if problems:
        raise sm.StatisticsChangeRefusedError("; ".join(problems))

    rows: list[dict[str, Any]] = []
    short_rows: list[dict[str, Any]] = []
    running = anchor
    for hour in written:
        hour_start = running
        running += new_change[hour]
        own = series.by_start.get(hour)
        if base_state is not None:
            state = base_state + running - anchor
        else:
            state = own.get("state") if own is not None else None
        reset = own.get("last_reset_ts") if own is not None else last_reset
        rows.append(
            {"start_ts": hour, "sum": running, "state": state, "last_reset_ts": reset}
        )
        if hour < five_from:
            continue
        slots = [hour + index * SLOT for index in range(SLOTS_PER_HOUR)]
        if mode == "adjust" and hour not in given:
            # Its own shape, moved to its new sum.
            delta = hour_start - (series.sum_end(hour - HOUR) or 0.0)
            for slot in slots:
                if (row := short_series.by_start.get(slot)) is not None:
                    short_rows.append(
                        {
                            **row,
                            "sum": row["sum"] + delta,
                            "state": (
                                row["state"] + delta
                                if row.get("state") is not None
                                else None
                            ),
                        }
                    )
            continue
        row_given = given.get(hour)
        values = (
            row_given.five_minute
            if row_given is not None and row_given.five_minute is not None
            else [new_change[hour] / SLOTS_PER_HOUR] * SLOTS_PER_HOUR
        )
        slot_sum = hour_start
        for index, (slot, value) in enumerate(zip(slots, values, strict=True)):
            slot_sum += value
            last = index == SLOTS_PER_HOUR - 1
            slot_end = running if last else slot_sum
            short_rows.append(
                {
                    "start_ts": slot,
                    "sum": slot_end,
                    "state": (
                        base_state + slot_end - anchor
                        if base_state is not None
                        else state
                    ),
                    "last_reset_ts": reset,
                }
            )

    old_end = series.sum_end(high - HOUR)
    offset = running - (old_end if old_end is not None else anchor)
    # Everything after the range moves by the offset - the 5-minute rows
    # too: a live meter's next compile carries on from the newest of them.
    after_rows = [row for row in series.rows if row["start_ts"] >= high]
    after_short = [row for row in short_series.rows if row["start_ts"] >= high]
    seam = (
        (high, offset)
        if (after_rows or after_short) and abs(offset) > _EPSILON
        else None
    )

    def moved(rows_after: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"start_ts": row["start_ts"], "sum": row["sum"] + offset}
            for row in rows_after
        ]

    shifted = (moved(after_rows), moved(after_short)) if seam else ([], [])
    before = {hour: old_change[hour] for hour in written if hour in series.by_start}
    report: dict[str, Any] = {
        "kind": "sum",
        "mode": mode,
        "range": {"start": _iso(low), "end": _iso(high - HOUR)},
        "rows_written": len(rows),
        "rows_given": len(given),
        "total_before": round(sum(before.values()), 6),
        "total_after": round(sum(new_change.values()), 6),
        "difference": round(sum(new_change.values()) - sum(before.values()), 6),
        "by_month": _month_report(before, new_change),
        "largest_hour_before": round(max(before.values()), 6) if before else None,
        "largest_hour_after": round(max(new_change.values()), 6),
        "seams": {
            "start": {"sum_before": round(anchor, 6), "at": _iso(low)},
            "end": {
                "from": _iso(high),
                "offset": round(offset, 6),
                "rows_moved": len(after_rows) if seam else 0,
                "five_minute_rows_moved": len(after_short) if seam else 0,
            },
        },
        "five_minute_rows_written": len(short_rows),
    }
    if five_from != float("inf"):
        report["five_minute_from"] = _iso(five_from)
    if negative:
        report["negative_hours"] = [_iso(hour) for hour in negative]
    if mode == "import" and (
        zeroed := [
            hour
            for hour in written
            if hour not in given and abs(old_change.get(hour, 0.0)) > _EPSILON
        ]
    ):
        # Existing hours in the range without a given row - set to 0, as
        # the range replaces what's there; said out loud, not just totalled.
        report["hours_set_to_zero"] = {
            "count": len(zeroed),
            "first": _iso(zeroed[0]),
            "last": _iso(zeroed[-1]),
        }
    return WritePlan(target, metadata, rows, short_rows, seam, shifted, report)


async def write_statistics(hass: HomeAssistant, plan: WritePlan) -> dict[str, Any]:
    """Write a plan: the rows (and 5-minute rows), checked against the
    recorder, then the seam's adjustment, checked too. The caller backs the
    statistic up first."""
    statistic_id = plan.metadata["statistic_id"]
    metadata = cast(StatisticMetaData, plan.metadata)
    sm.check_importable(metadata)

    def queue_rows() -> None:
        sm.queue_import(hass, metadata, plan.rows)
        if plan.short_term_rows:
            sm.queue_short_term_rows(hass, metadata, plan.short_term_rows)

    async def rows_written() -> list[str]:
        return await sm.row_mismatches(
            hass, statistic_id, plan.rows
        ) + await sm.row_mismatches(
            hass, statistic_id, plan.short_term_rows, StatisticsShortTerm
        )

    await sm.on_recorder_verified(hass, queue_rows, rows_written, what="the rows")
    if plan.seam is not None:
        start, offset = plan.seam

        def queue_seam() -> None:
            sm.queue_adjust(
                hass, statistic_id, dt_util.utc_from_timestamp(start), offset
            )

        async def seam_written() -> list[str]:
            hourly, five_minute = plan.shifted
            return await sm.row_mismatches(
                hass, statistic_id, hourly
            ) + await sm.row_mismatches(
                hass, statistic_id, five_minute, StatisticsShortTerm
            )

        await sm.on_recorder_verified(
            hass, queue_seam, seam_written, what="the sums after the range"
        )
    after = (await sm.describe_statistics(hass, [statistic_id]))[statistic_id]
    result: dict[str, Any] = {"written": {**plan.preview(), "target": after}}
    if plan.metadata["has_sum"] and not plan.target.get("new"):
        result["next_compile"] = await sm.continuity_check(hass, statistic_id)
    return result
