"""Deriving a meter statistic from another one, hour by hour (issue #143).

The Energy dashboard prices grid import/export through cost and
compensation statistics that HA fills live, only from the moment a source
is configured: swapping the source sensor starts an empty series, and a
correction to the kWh series (an "Adjust sum" catch-up) never gets priced.
This rebuilds such a series from the meter itself: each hour's change of
the source meter times a factor -

- a fixed number (a feed-in rate),
- rate periods, `[{from, value}, ...]` (tariff changes), or
- a measurement statistic's hourly mean (a dynamic price) - a missing
  hour takes the last known price (the result says how many and the
  longest gap), or with missing_price="refuse" stops the write.

The derived hours go into `target_statistic_id`: a new statistic under
this integration's own `ha_dev_tools:` prefix, or an existing meter
statistic whose `start`..`end` range is replaced - inside it the derived
series is authoritative (target hours the source has no row for count 0),
outside it the target keeps its own changes. Writing goes through
statistics_merge's writer, so a live target - the dashboard's own cost
sensor - goes on recording from the new sum (issue #141), and the
existing target is backed up first like for every statistics write.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.recorder.statistics import valid_statistic_id
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import statistics_backup as backups
from . import statistics_manager as sm
from . import statistics_merge as merge
from .const import DOMAIN

_NEW_PREFIX = f"{DOMAIN}:"


@dataclass(slots=True)
class _Factor:
    """The factor for each hour, and what the preview says about it."""

    values: dict[float, float]
    report: dict[str, Any]


def _periods_factor(
    periods: list[dict[str, Any]], hours: list[float]
) -> tuple[_Factor | None, str | None]:
    parsed = []
    for period in periods:
        start = dt_util.parse_datetime(str(period.get("from", "")))
        value = period.get("value")
        if start is None or not isinstance(value, int | float):
            return None, (
                "each rate period is {'from': ISO 8601 time, 'value': number}, "
                f"got {period!r}"
            )
        parsed.append((dt_util.as_utc(start).timestamp(), float(value)))
    parsed.sort()
    starts = [start for start, _ in parsed]
    if hours and hours[0] < starts[0]:
        return None, (
            f"no rate for the hours before {merge._iso(starts[0])} - add a "
            "period that starts earlier, or pass start"
        )
    values = {hour: parsed[bisect.bisect_right(starts, hour) - 1][1] for hour in hours}
    return (
        _Factor(
            values,
            {
                "kind": "periods",
                "periods": [
                    {"from": merge._iso(start), "value": value}
                    for start, value in parsed
                ],
            },
        ),
        None,
    )


MISSING_PRICE = ("carry", "refuse")


async def _price_factor(
    hass: HomeAssistant, price_id: str, hours: list[float], missing: str = "carry"
) -> tuple[_Factor | None, str | None]:
    meta = (await sm.read_metadata(hass, [price_id])).get(price_id)
    if meta is None:
        return None, sm._unknown(price_id)
    if meta["has_sum"]:
        return None, (
            f"'{price_id}' is a meter (sum) statistic - a price is a "
            "measurement, multiplied by its hourly mean"
        )
    means = {
        row["start_ts"]: row["mean"]
        for row in (await sm.read_rows(hass, [price_id])).get(price_id, [])
        if row.get("mean") is not None
    }
    if missing == "refuse" and (gaps := [hour for hour in hours if hour not in means]):
        return None, (
            f"'{price_id}' has no price for {len(gaps)} hour(s), "
            f"{merge._iso(gaps[0])} -> {merge._iso(gaps[-1])} - fill them, pass "
            "start/end around them, or missing_price='carry' to take the last "
            "known price"
        )
    values: dict[float, float] = {}
    last: float | None = None
    filled = longest = run = 0
    for hour in hours:
        if hour in means:
            last, run = means[hour], 0
        else:
            if last is None:
                return None, (
                    f"'{price_id}' has no price for {merge._iso(hour)} or "
                    "before - pass a later start"
                )
            filled, run = filled + 1, run + 1
            longest = max(longest, run)
        values[hour] = last
    return (
        _Factor(
            values,
            {
                "kind": "price",
                "statistic_id": price_id,
                "unit_of_measurement": meta.get("unit_of_measurement"),
                "missing_price": missing,
                "hours_without_price": filled,
                "longest_gap_hours": longest,
            },
        ),
        None,
    )


def _per_unit(factor: Any, price_unit: str | None, source_unit: str | None) -> Any:
    """The energy unit a factor is per: a price's own 'EUR/kWh' says so;
    otherwise source_unit, if given."""
    if isinstance(factor, str) and price_unit and "/" in price_unit:
        return price_unit.rsplit("/", 1)[1]
    return source_unit


def _month_totals(changes: dict[float, float]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for hour, change in sorted(changes.items()):
        month = dt_util.as_local(dt_util.utc_from_timestamp(hour)).strftime("%Y-%m")
        totals[month] = totals.get(month, 0.0) + change
    return {month: round(total, 6) for month, total in totals.items()}


async def plan_derive(
    hass: HomeAssistant,
    source_id: str,
    factor: Any,
    target_id: str,
    *,
    unit: str | None = None,
    source_unit: str | None = None,
    name: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    missing_price: str = "carry",
    can_back_up: bool,
    allow_no_backup: bool = False,
) -> merge.MergePlan:
    """Compute a derived series and how it goes into the target, without
    writing anything."""
    problems: list[str] = []
    if missing_price not in MISSING_PRICE:
        problems.append(f"missing_price is one of {MISSING_PRICE}")
    metadata = await sm.read_metadata(hass, [source_id, target_id])
    source_meta = metadata.get(source_id)
    target_meta = metadata.get(target_id)
    if source_meta is None:
        problems.append(sm._unknown(source_id))
    elif not source_meta["has_sum"]:
        problems.append(f"'{source_id}' isn't a meter (sum) statistic")
    if target_id == source_id:
        problems.append("the target can't be the source")
    if backups.is_backup(target_id):
        problems.append("the target can't be a backup")
    if target_meta is None:
        if not (target_id.startswith(_NEW_PREFIX) and valid_statistic_id(target_id)):
            problems.append(
                f"'{target_id}' doesn't exist - a new statistic is created "
                f"only under '{_NEW_PREFIX}' (a lowercase slug, e.g. "
                f"'{_NEW_PREFIX}grid_export_compensation')"
            )
    elif not target_meta["has_sum"]:
        problems.append(f"'{target_id}' isn't a meter (sum) statistic")
    elif unit is not None and unit != target_meta.get("unit_of_measurement"):
        problems.append(
            f"'{target_id}' is in {target_meta.get('unit_of_measurement')!r}, "
            f"not {unit!r}"
        )
    if start and end and start >= end:
        problems.append("start must be before end")
    if target_meta is not None and not can_back_up and not allow_no_backup:
        problems.append(sm._no_backup())
    if problems:
        raise sm.StatisticsChangeRefusedError("; ".join(problems))
    assert source_meta is not None

    low = start.timestamp() if start else float("-inf")
    high = end.timestamp() if end else float("inf")
    raw = await sm.read_rows(hass, [source_id, target_id])
    source = merge.Series(source_id, raw.get(source_id, []))
    hours = sorted(hour for hour in source.changes if low <= hour < high)
    if not hours:
        raise sm.StatisticsChangeRefusedError(
            f"'{source_id}' has no hourly rows in that range"
        )

    built: _Factor | None
    if isinstance(factor, bool):
        built, problem = None, "factor is a number, rate periods or a price statistic"
    elif isinstance(factor, int | float):
        built, problem = (
            _Factor(
                dict.fromkeys(hours, float(factor)),
                {"kind": "fixed", "value": float(factor)},
            ),
            None,
        )
    elif isinstance(factor, list):
        built, problem = _periods_factor(factor, hours)
    else:
        built, problem = await _price_factor(hass, str(factor), hours, missing_price)
    if built is None:
        raise sm.StatisticsChangeRefusedError(str(problem))

    convert, unit_problem = backups.unit_converter(
        dict(source_meta),
        _per_unit(factor, built.report.get("unit_of_measurement"), source_unit)
        or source_meta.get("unit_of_measurement"),
    )
    if unit_problem:
        raise sm.StatisticsChangeRefusedError(unit_problem)
    derived = {
        hour: (convert(source.changes[hour]) if convert else source.changes[hour])
        * built.values[hour]
        for hour in hours
    }

    target_unit = (
        target_meta.get("unit_of_measurement") if target_meta is not None else unit
    )
    if target_unit is None and built.report.get("unit_of_measurement"):
        target_unit = str(built.report["unit_of_measurement"]).split("/")[0]
    if target_meta is None and target_unit is None:
        raise sm.StatisticsChangeRefusedError(
            "pass unit for the new statistic (e.g. 'EUR')"
        )

    report: dict[str, Any] = {
        "factor": built.report,
        "range": {
            "start": merge._iso(hours[0]) if hours else None,
            "end": merge._iso(hours[-1]) if hours else None,
        },
        "derived": {
            "hours": len(derived),
            "total": round(sum(derived.values()), 6),
            "by_month": _month_totals(derived),
        },
    }
    if target_meta is None:
        total = 0.0
        rows = []
        for hour in hours:
            total += derived[hour]
            rows.append({"start_ts": hour, "sum": total})
        return merge.MergePlan(
            target={"statistic_id": target_id, "new": True},
            sources=[await _describe(hass, source_id)],
            metadata={
                "has_sum": True,
                "mean_type": 0,
                "name": name or f"{source_id} × {_factor_label(built.report)}",
                "source": DOMAIN,
                "statistic_id": target_id,
                "unit_class": None,
                "unit_of_measurement": target_unit,
            },
            overlap="derive",
            rows=rows,
            report=report,
            rebase=(None, 0.0),
        )

    target = merge.Series(target_id, raw.get(target_id, []), is_target=True)
    replaced = {
        hour: change for hour, change in target.changes.items() if low <= hour < high
    }
    old_months, new_months = _month_totals(replaced), _month_totals(derived)
    report["replaces"] = {
        "hours": len(replaced),
        "total": round(sum(replaced.values()), 6),
        "difference": round(sum(derived.values()) - sum(replaced.values()), 6),
        # What each month holds now, what it would, and the change.
        "by_month": [
            {
                "month": month,
                "before": old_months.get(month, 0.0),
                "after": new_months.get(month, 0.0),
                "difference": round(
                    new_months.get(month, 0.0) - old_months.get(month, 0.0), 6
                ),
            }
            for month in sorted(set(old_months) | set(new_months))
        ],
    }
    total = 0.0
    rows = []
    for hour in sorted(set(target.by_start) | set(derived)):
        if low <= hour < high:
            total += derived.get(hour, 0.0)
        else:
            total += target.changes.get(hour, 0.0)
        row: dict[str, Any] = {"start_ts": hour, "sum": total}
        if (own := target.by_start.get(hour)) is not None:
            row["state"] = own.get("state")
            row["last_reset_ts"] = own.get("last_reset_ts")
        rows.append(row)
    short_term = (await sm.read_rows(hass, [target_id], merge.StatisticsShortTerm)).get(
        target_id, []
    )
    live = target_meta["source"] == "recorder"
    return merge.MergePlan(
        target=await _describe(hass, target_id),
        sources=[await _describe(hass, source_id)],
        metadata=dict(target_meta),
        overlap="derive",
        rows=rows,
        report=report,
        rebase=(
            merge.rebase_point(
                target,
                rows,
                first_short_term=short_term[0]["start_ts"] if short_term else None,
                now=dt_util.utcnow().timestamp(),
            )
            if live
            else (None, 0.0)
        ),
        seed=live and not short_term and bool(rows),
        existing=target.by_start,
    )


async def _describe(hass: HomeAssistant, statistic_id: str) -> dict[str, Any]:
    return (await sm.describe_statistics(hass, [statistic_id]))[statistic_id]


def _factor_label(report: dict[str, Any]) -> str:
    if report["kind"] == "fixed":
        return str(report["value"])
    if report["kind"] == "price":
        return str(report["statistic_id"])
    return "rate periods"
