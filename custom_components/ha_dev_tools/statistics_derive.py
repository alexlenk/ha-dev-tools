"""Deriving a scaled statistic from a meter (issue #143).

HA's Energy dashboard prices grid import/export through cost and
compensation statistics it fills only while a source is configured:
swap the source and the money series starts empty, correct the meter
afterwards and the correction never gets priced. derive_statistics
builds such a series from the meter itself: every hour's change is
multiplied by a rate - a fixed number (a feed-in tariff), periods
(`from`/`value` tariff changes), or a mean statistic (a dynamic
price, hour by hour) - and accumulated into a running sum.

start/end bound the range; every hour in it the source covers gets a
derived row, and every hour the target has an old row in it gets
that row overwritten (a carry row, no change, where the source has
none), so no stale row survives the range. The sum continues the
target's own just before the range (a new target starts at 0), so
there's no seam at start - and a live target goes on recording from
the derived sum: its rows after the range still carry the old basis,
so from the range end (or the target's first 5-minute row, whichever
is earlier) they're shifted onto the new one by HA's own sum
adjustment, and the derived rows in that window are written the
offset lower so the adjustment lands them where they belong (issue
#141, as in statistics_merge). A recorder target without any 5-minute
row gets one carrying the derived sum, or its next compile would
start again at 0; a new external target isn't an entity, nothing
compiles it, and no seed is written.

The rate is per source_unit - the source's own unit by default, its
rows converted within one unit class - and the result is in the
target's unit, which the `unit` argument says for a new external
target ('ha_dev_tools:...'). Rate hours the source covers but the
rate doesn't are refused, listed: money must be explicit.

The preview shows the derived total, per-month totals with the old
ones for comparison, and how many of the target's rows the range
replaces; an existing target is backed up first, like every
destructive write (in the recorder and to the mirror repo).
"""

from __future__ import annotations

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
from .statistics_merge import Series

_EPSILON = 1e-9
_HOUR = 3600.0


def _iso(timestamp: float) -> str:
    return dt_util.utc_from_timestamp(timestamp).isoformat()


def _month(timestamp: float) -> str:
    return dt_util.utc_from_timestamp(timestamp).strftime("%Y-%m")


@dataclass(slots=True)
class Rate:
    """The factor, resolved for hourly lookup and for the preview."""

    kind: str  # "fixed" | "periods" | "statistic"
    summary: dict[str, Any]
    value: float | None = None
    periods: list[tuple[float, float]] | None = None  # (from_ts, value), sorted
    hourly: dict[float, float] | None = None  # start_ts -> mean

    def at(self, hour: float) -> float | None:
        """The rate for HOUR, or None when the rate doesn't cover it."""
        if self.kind == "fixed":
            return self.value
        if self.kind == "periods":
            applicable = None
            for from_ts, value in self.periods or ():
                if from_ts <= hour:
                    applicable = value
            return applicable
        return (self.hourly or {}).get(hour)


def parse_factor(
    factor: float | str | list[tuple[datetime, float]],
) -> Rate:
    """The tool's factor argument as a Rate.

    A number is a fixed rate; a string, a mean statistic to read hour
    by hour; a list of (from, value) pairs, rate periods. Rates can't
    be negative - a meter that went backwards still prices forward.
    """
    if isinstance(factor, bool) or factor is None:
        raise sm.StatisticsChangeRefusedError(
            "factor must be a number (a fixed rate), a mean statistic id "
            "(read hour by hour), or a list of {from, value} periods"
        )
    if isinstance(factor, (int, float)):
        if factor < 0:
            raise sm.StatisticsChangeRefusedError("factor can't be negative")
        value = float(factor)
        return Rate("fixed", {"kind": "fixed", "value": value}, value=value)
    if isinstance(factor, str):
        if not factor:
            raise sm.StatisticsChangeRefusedError(
                "factor, as a statistic id, can't be empty"
            )
        return Rate("statistic", {"kind": "statistic", "statistic_id": factor})
    if isinstance(factor, list):
        if not factor:
            raise sm.StatisticsChangeRefusedError(
                "factor, as periods, needs at least one {from, value}"
            )
        periods: list[tuple[float, float]] = []
        for from_when, value in factor:
            if value < 0:
                raise sm.StatisticsChangeRefusedError(
                    "factor periods can't be negative"
                )
            periods.append((from_when.timestamp(), float(value)))
        periods.sort()
        if len({from_ts for from_ts, _ in periods}) != len(periods):
            raise sm.StatisticsChangeRefusedError(
                "factor periods must have distinct 'from' times"
            )
        return Rate(
            "periods",
            {
                "kind": "periods",
                "periods": [
                    {"from": _iso(from_ts), "value": value}
                    for from_ts, value in periods
                ],
            },
            periods=periods,
        )
    raise sm.StatisticsChangeRefusedError(
        "factor must be a number, a statistic id, or a list of " "{from, value} periods"
    )


def compute(
    source_changes: dict[float, float],
    rate: Rate,
    old_rows: dict[float, dict[str, Any]],
    anchor: float,
    low: float,
    high: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The derived rows in [low, high) and the preview's month report.

    Every hour the source covers in the range gets its change priced,
    every hour the target has an old row in it gets that row
    overwritten - a carry row where the source has none, so no stale
    row survives. The sum continues `anchor`, the target's last sum
    before the range (0 for a new target).
    """
    source_hours = sorted(ts for ts in source_changes if low <= ts < high)
    if not source_hours:
        raise sm.StatisticsChangeRefusedError(
            "the source has no hourly rows in the range"
        )
    missing = [ts for ts in source_hours if rate.at(ts) is None]
    if missing:
        raise sm.StatisticsChangeRefusedError(
            f"the factor doesn't cover {len(missing)} of the source's "
            f"hours, {_iso(missing[0])} -> {_iso(missing[-1])} - pass "
            "start/end to leave them out, or extend the factor's "
            "periods/price series"
        )
    old_in_range = sorted(
        ts
        for ts, row in old_rows.items()
        if low <= ts < high and row.get("sum") is not None
    )
    hours = sorted(set(source_hours) | set(old_in_range))
    months: dict[str, dict[str, Any]] = {}
    running = anchor
    rows: list[dict[str, Any]] = []
    for hour in hours:
        change = source_changes.get(hour, 0.0)
        value = rate.at(hour)
        delta = change * value if value is not None else 0.0
        running += delta
        month = months.setdefault(
            _month(hour),
            {"month": _month(hour), "hours": 0, "derived": 0.0, "old": 0.0},
        )
        month["hours"] += 1
        month["derived"] += delta
        own = old_rows.get(hour)
        row: dict[str, Any] = {"start_ts": hour, "sum": running}
        if own is not None:
            row["state"] = own.get("state")
            row["last_reset_ts"] = own.get("last_reset_ts")
        else:
            row["state"] = running
        rows.append(row)
    # The old rows' per-month totals, for the preview's comparison.
    old_running = anchor
    for hour in old_in_range:
        month = months.setdefault(
            _month(hour),
            {"month": _month(hour), "hours": 0, "derived": 0.0, "old": 0.0},
        )
        old = old_rows[hour]["sum"]
        month["old"] += old - old_running
        old_running = old
    report = {
        "total": round(running - anchor, 6),
        "old_total": round(old_running - anchor, 6),
        "months": [
            {
                **month,
                "derived": round(month["derived"], 6),
                "old": round(month["old"], 6),
            }
            for month in (months[key] for key in sorted(months))
        ],
        "replaced_rows": len(old_in_range),
    }
    return rows, report


@dataclass(slots=True)
class DerivePlan:
    """What plan_derive works out, for the preview and the write."""

    target: dict[str, Any]
    source: dict[str, Any]
    rate: dict[str, Any]
    metadata: dict[str, Any]
    rows: list[dict[str, Any]]
    report: dict[str, Any]
    anchor: float
    shift: tuple[float | None, float]
    # A recorder target without any 5-minute row gets one carrying the
    # derived sum, or its next compile would start again at 0 (issue #141).
    seed: bool = False
    existing: bool = True

    def preview(self) -> dict[str, Any]:
        """JSON-safe summary for the tool's preview and result."""
        start, offset = self.shift
        return {
            "target": self.target,
            "source": self.source,
            "rate": self.rate,
            "rows_written": len(self.rows),
            "anchor": round(self.anchor, 6),
            **self.report,
            **(
                {"sum_shift": {"from": _iso(start), "offset": round(offset, 6)}}
                if start is not None and abs(offset) > _EPSILON
                else {}
            ),
        }


async def plan_derive(
    hass: HomeAssistant,
    source_id: str,
    target_id: str,
    factor: float | str | list[tuple[datetime, float]],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    unit: str | None = None,
    source_unit: str | None = None,
    can_back_up: bool,
    allow_no_backup: bool = False,
) -> DerivePlan:
    """Check and compute a derivation without writing anything."""
    rate = parse_factor(factor)
    ids = [source_id, target_id]
    if rate.kind == "statistic":
        ids.append(cast(str, rate.summary["statistic_id"]))
    described = await sm.describe_statistics(hass, ids)
    metadata = await sm.read_metadata(hass, ids)
    problems = []
    source_meta = metadata.get(source_id)
    if source_meta is None:
        problems.append(sm._unknown(source_id))
    elif not source_meta["has_sum"]:
        problems.append(
            f"'{source_id}' isn't a meter (sum) statistic - a "
            "measurement's rows don't accumulate, so there's nothing to "
            "price hour by hour"
        )
    target_meta = metadata.get(target_id)
    existing = target_meta is not None
    if target_id == source_id:
        problems.append("the target can't also be the source")
    if existing:
        if backups.is_backup(target_id):
            problems.append(
                "the target can't be a backup - restore_statistics restores one"
            )
        elif not target_meta["has_sum"]:
            problems.append(
                "the target isn't a meter (sum) statistic - derived rows "
                "are running sums"
            )
        if unit is not None and unit != target_meta.get("unit_of_measurement"):
            problems.append(
                f"the target is in {target_meta.get('unit_of_measurement')!r}, "
                f"not {unit!r}"
            )
    else:
        if not valid_statistic_id(target_id) or not target_id.startswith(f"{DOMAIN}:"):
            problems.append(
                f"a new target must be an external statistic id like "
                f"'{DOMAIN}:<name>'"
            )
        if unit is None:
            problems.append("a new target needs its unit - pass unit (e.g. 'EUR')")
    if rate.kind == "statistic":
        price_id = cast(str, rate.summary["statistic_id"])
        price_meta = metadata.get(price_id)
        if price_meta is None:
            problems.append(sm._unknown(price_id))
        elif price_meta["has_sum"]:
            problems.append(
                f"'{price_id}' isn't a mean (measurement) statistic - a "
                "meter has no hourly price to multiply by"
            )
    if start and end and start >= end:
        problems.append("start must be before end")
    convert = None
    if source_meta is not None and source_unit is not None:
        convert, unit_problem = backups.unit_converter(dict(source_meta), source_unit)
        if unit_problem:
            problems.append(unit_problem)
    if existing and not can_back_up and not allow_no_backup:
        problems.append(sm._no_backup())
    if problems:
        raise sm.StatisticsChangeRefusedError("; ".join(problems))

    raw = await sm.read_rows(hass, ids)
    source = Series(source_id, backups.convert_rows(raw.get(source_id, []), convert))
    if not source.changes:
        raise sm.StatisticsChangeRefusedError(f"'{source_id}' has no hourly rows")
    low = start.timestamp() if start else min(source.changes)
    high = end.timestamp() if end else max(source.changes) + _HOUR
    if low >= high:
        raise sm.StatisticsChangeRefusedError("the range is empty")
    if rate.kind == "statistic":
        rate.hourly = {
            row["start_ts"]: row["mean"]
            for row in raw.get(price_id, [])
            if row.get("mean") is not None
        }
        rate.summary["hours"] = len(rate.hourly)
        rate.summary["unit"] = (metadata.get(price_id) or {}).get("unit_of_measurement")

    old_rows = {row["start_ts"]: row for row in raw.get(target_id, [])}
    anchor = 0.0
    if existing:
        before = [
            row["sum"]
            for ts, row in sorted(old_rows.items())
            if ts < low and row.get("sum") is not None
        ]
        anchor = before[-1] if before else 0.0
    rows, report = compute(source.changes, rate, old_rows, anchor, low, high)

    shift: tuple[float | None, float] = (None, 0.0)
    seed = False
    if existing:
        old_in_range = sorted(
            ts
            for ts, row in old_rows.items()
            if low <= ts < high and row.get("sum") is not None
        )
        old_end = old_rows[old_in_range[-1]]["sum"] if old_in_range else anchor
        offset = rows[-1]["sum"] - old_end
        short_term = (await sm.read_rows(hass, [target_id], StatisticsShortTerm)).get(
            target_id, []
        )
        if abs(offset) > _EPSILON:
            shift_from = high
            if short_term and short_term[0]["start_ts"] < shift_from:
                first = short_term[0]["start_ts"]
                # From its hour, so the whole 5-minute series shifts and
                # any derived row in it is pre-lowered at write time - but
                # never before the range: the rows before it already sit
                # on the anchor basis the derived sum continues.
                shift_from = max(low, min(high, first - first % _HOUR))
            shift = (shift_from, offset)
        seed = bool(rows) and not short_term and target_meta["source"] == "recorder"

    metadata_out = (
        dict(target_meta)
        if existing
        else {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": f"Derived from {source_id}",
            "source": DOMAIN,
            "statistic_id": target_id,
            "unit_class": None,
            "unit_of_measurement": unit,
        }
    )
    if existing:
        target_description = {**described[target_id], "existing": True}
    else:
        target_description = {
            "statistic_id": target_id,
            "name": metadata_out["name"],
            "source": DOMAIN,
            "unit_of_measurement": unit,
            "existing": False,
        }
    return DerivePlan(
        target=target_description,
        source=described[source_id],
        rate=rate.summary,
        metadata=dict(metadata_out),
        rows=rows,
        report=report,
        anchor=anchor,
        shift=shift,
        seed=seed,
        existing=existing,
    )


async def derive_statistics(hass: HomeAssistant, plan: DerivePlan) -> dict[str, Any]:
    """Write the derived rows - any in the shift window the offset lower,
    then HA's sum adjustment lands them where they belong (see the
    module docstring). The caller backs the target up first."""
    target_id = plan.metadata["statistic_id"]
    shift_from, offset = plan.shift
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
    instance = sm._instance(hass)
    # The compile measures from this row's state, so the target
    # meter's own reading wins over the derived one (as in merge).
    state = hass.states.get(target_id)
    reading = sm._number(state.state) if state else None
    seed = rows[-1] if reading is None else {**rows[-1], "state": reading}

    def queue() -> None:
        sm.queue_import(hass, cast(StatisticMetaData, plan.metadata), rows)
        if plan.seed:
            sm.queue_short_term_seed(hass, cast(StatisticMetaData, plan.metadata), seed)
        if shift_from is not None:
            instance.async_adjust_statistics(
                target_id,
                dt_util.utc_from_timestamp(shift_from),
                offset,
                plan.metadata["unit_of_measurement"],
            )

    await sm.on_recorder(hass, queue)
    after = (await sm.describe_statistics(hass, [target_id])).get(target_id)
    result: dict[str, Any] = {"derived": {**plan.preview(), "target": after}}
    if plan.existing and plan.metadata["has_sum"]:
        result["next_compile"] = await sm.continuity_check(hass, target_id)
    return result
