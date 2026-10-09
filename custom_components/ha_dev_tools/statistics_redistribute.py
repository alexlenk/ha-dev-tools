"""Spreading a catch-up hour back over the hours a meter missed (issue #155).

When a meter reader goes quiet (a cloud outage, the recorder down), HA
books everything it missed into the first hour after it comes back: one
huge spike instead of the usage spread over the silent hours. Totals are
right; hour, day and month views - and the costs derived from them - are
not. This moves that energy back into the window it belongs to without
changing any total: the sum at the end of the catch-up hour stays what it
is, so every later hour, the live state and the next compile are
untouched. Only hours inside the window change.

A window runs from its first silent hour to the catch-up hour, both
included. It's given explicitly or detected - a run of at least
`min_silent_hours` hours without a change (no row, or change 0) followed by
an hour above `min_catchup`. Detection, and the amount moved, can be on a
*component* (`detect_on`) rather than the statistic written: after a merge
the target is the sum of several meters, and a stall in one of them
doesn't show as zeros in the target - so only that component's catch-up is
moved, the other components' hours stay as they are.

The shape - how the window's total is split over its hours - comes from,
per hour, the first source that has it:

1. reference statistics, in order: another meter's hourly changes over the
   window (an inverter's own export counter, say), scaled so the window
   total is exactly the catch-up - only the shape is borrowed;
2. a profile: the series' own mean by hour of the week (HA's time zone)
   over `profile_weeks` weeks of data around the window;
3. even, as a last resort - with a warning.

Or `values`: the caller's own numbers, one per hour - and, for the part of
the window the meter still has 5-minute rows for, one per 5-minute slot
(12 per hour) - checked, never shaped: their sum must be the window total
(or within 2 % with `normalize`), none negative or non-finite, none
above `max_per_hour`. References given with values only compare: their
ratio and fit, and their hours next to the values', hour by hour.

5-minute rows inside the window (the recorder keeps them ~10 days) are
rewritten too, so 5-minute graphs match: each hour's new value split by
the first reference's 5-minute changes in that hour, or evenly - or, in
values mode, as given.

Refused: a window with a negative hour or a meter reset, overlapping
windows, a catch-up hour that isn't complete yet. Reported: the ratio of
each reference's total to the catch-up (a warning beyond ±15 %), a
reference that looks like a flat estimate rather than a measurement, the
fit of each reference to the meter on clean hours around the window,
before/after by day and month, and which source shaped how many hours.
"""

from __future__ import annotations

import bisect
import math
import statistics as stats_lib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from homeassistant.components.recorder import statistics as recorder_statistics
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticMetaData
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import statistics_backup as backups
from . import statistics_manager as sm
from .const import DOMAIN

HOUR = 3600.0
SLOT = 300.0
SLOTS_PER_HOUR = 12
_EPSILON = 1e-9
# How far given values may miss the window total, and - with normalize -
# how far they may miss it to be scaled onto it.
SUM_TOLERANCE = 0.001
NORMALIZE_LIMIT = 0.02
# A reference total this far off the catch-up is worth a look.
RATIO_WARNING = 0.15
# Clean hours before and after a window a reference is compared on.
QUALITY_HOURS = 48
# A reference this flat (coefficient of variation) over at least a day is
# more likely an estimate than a measurement.
FLAT_VARIATION = 0.05
FLAT_MIN_HOURS = 24


def _iso(timestamp: float) -> str:
    return dt_util.utc_from_timestamp(timestamp).isoformat()


@dataclass(slots=True)
class Series:
    """A meter series' raw rows (hourly or 5-minute), in the target's unit."""

    statistic_id: str
    rows: list[dict[str, Any]]
    step: float = HOUR
    by_start: dict[float, dict[str, Any]] = field(init=False)
    starts: list[float] = field(init=False)

    def __post_init__(self) -> None:
        self.rows = [row for row in self.rows if row.get("sum") is not None]
        self.by_start = {row["start_ts"]: row for row in self.rows}
        self.starts = [row["start_ts"] for row in self.rows]

    def last_row(self, start: float) -> dict[str, Any] | None:
        """The newest row starting at or before `start` - the meter's
        state at the end of that period, carried over a gap."""
        index = bisect.bisect_right(self.starts, start) - 1
        return self.rows[index] if index >= 0 else None

    def sum_end(self, start: float) -> float | None:
        row = self.last_row(start)
        return row["sum"] if row is not None else None

    def change(self, start: float) -> float | None:
        """The change over the period starting at `start`, gaps counting 0."""
        end, before = self.sum_end(start), self.sum_end(start - self.step)
        return None if end is None or before is None else end - before

    def measured(self, start: float) -> float | None:
        """The change over the period starting at `start`, only where both
        it and the period before have a row - a measurement, not a gap."""
        row, before = self.by_start.get(start), self.by_start.get(start - self.step)
        if row is None or before is None:
            return None
        return row["sum"] - before["sum"]


@dataclass(slots=True)
class Window:
    """From its first silent hour to the catch-up hour, both included."""

    start: float
    catchup: float

    @property
    def hours(self) -> list[float]:
        count = round((self.catchup - self.start) / HOUR) + 1
        return [self.start + index * HOUR for index in range(count)]


@dataclass(slots=True)
class RedistributePlan:
    """What plan_redistribute works out, for the preview and the write."""

    target: dict[str, Any]
    metadata: dict[str, Any]
    rows: list[dict[str, Any]]
    short_term_rows: list[dict[str, Any]]
    report: dict[str, Any]

    def preview(self) -> dict[str, Any]:
        return {"target": self.target, **self.report}


def detect_windows(
    series: Series,
    *,
    min_silent_hours: int,
    min_catchup: float,
    low: float = float("-inf"),
    high: float = float("inf"),
) -> list[Window]:
    """Every run of at least min_silent_hours hours without a change (no
    row, or change 0) followed by an hour above min_catchup, whose catch-up
    hour is in [low, high). The silent hours are counted from the series'
    start, not from `low`: an outage that began before it keeps its whole
    window, rather than its catch-up being squeezed into the part after."""
    if not series.rows:
        return []
    first = series.starts[0] + HOUR
    last = series.starts[-1]
    windows: list[Window] = []
    silent = 0
    hour = first
    while hour <= last:
        change = series.change(hour) or 0.0
        if abs(change) < _EPSILON:
            silent += 1
        else:
            if (
                silent >= min_silent_hours
                and change > min_catchup
                and low <= hour < high
            ):
                windows.append(Window(hour - silent * HOUR, hour))
            silent = 0
        hour += HOUR
    return windows


def _local_hour_of_week(hour: float) -> tuple[int, int]:
    local = dt_util.as_local(dt_util.utc_from_timestamp(hour))
    return local.weekday(), local.hour


def _profile(
    series: Series, windows: list[Window], window: Window, weeks: int
) -> dict[tuple[int, int], float]:
    """The series' mean change by local hour of the week, over `weeks`
    weeks either side of `window`, from measured hours outside any window."""
    blocked = {hour for each in windows for hour in each.hours}
    span = weeks * 7 * 24 * HOUR
    samples: dict[tuple[int, int], list[float]] = {}
    for start in series.starts:
        if (
            not (
                window.start - span <= start < window.start
                or window.catchup < start <= window.catchup + span
            )
            or start in blocked
        ):
            continue
        change = series.measured(start)
        if change is not None and change >= 0:
            samples.setdefault(_local_hour_of_week(start), []).append(change)
    return {key: sum(values) / len(values) for key, values in samples.items()}


def _check_window(series: Series, window: Window, what: str) -> list[str]:
    """A negative hour or a meter reset inside the window refuses it."""
    problems = []
    before = series.last_row(window.start - HOUR)
    if before is None:
        return [f"{what} has no row before {_iso(window.start)}"]
    for hour in window.hours:
        change = series.change(hour)
        if change is not None and change < -_EPSILON:
            problems.append(f"{what} goes down at {_iso(hour)} (a meter reset?)")
        row = series.by_start.get(hour)
        if row is not None and row.get("last_reset_ts") != before.get("last_reset_ts"):
            problems.append(f"{what} is reset at {_iso(hour)}")
    return problems


def _quality(component: Series, reference: Series, window: Window) -> dict[str, Any]:
    """How well a reference's hours match the meter's own, on measured
    hours just before and after the window."""
    pairs = []
    for offset in range(1, QUALITY_HOURS + 1):
        for hour in (window.start - offset * HOUR, window.catchup + offset * HOUR):
            mine, theirs = component.measured(hour), reference.measured(hour)
            if mine is not None and theirs is not None:
                pairs.append((mine, theirs))
    if not pairs:
        return {"reference": reference.statistic_id, "hours": 0}
    own_total = sum(mine for mine, _ in pairs)
    return {
        "reference": reference.statistic_id,
        "hours": len(pairs),
        "mean_abs_error": round(
            sum(abs(mine - theirs) for mine, theirs in pairs) / len(pairs), 6
        ),
        "ratio": (
            round(sum(theirs for _, theirs in pairs) / own_total, 4)
            if own_total > _EPSILON
            else None
        ),
    }


def _flat(changes: list[float]) -> bool:
    if len(changes) < FLAT_MIN_HOURS:
        return False
    mean = sum(changes) / len(changes)
    return mean > _EPSILON and stats_lib.pstdev(changes) / mean < FLAT_VARIATION


@dataclass(slots=True)
class _Inputs:
    target: Series
    target_short: Series
    component: Series
    component_short: Series
    references: list[Series]
    reference_short: list[Series]
    five_minute_from: float


def _shape(
    inputs: _Inputs,
    windows: list[Window],
    window: Window,
    profile_weeks: int | None,
    total: float,
) -> tuple[dict[float, float], dict[str, int], list[str]]:
    """{hour: new component change} for a window, the hours per source, and
    warnings - references, then the profile, then even (module docstring)."""
    weights: dict[float, float] = {}
    sources: dict[float, str] = {}
    for hour in window.hours:
        for reference in inputs.references:
            change = reference.measured(hour)
            if change is not None and change >= 0:
                weights[hour] = change
                sources[hour] = f"reference:{reference.statistic_id}"
                break
    profile = (
        _profile(inputs.component, windows, window, profile_weeks)
        if profile_weeks
        else {}
    )
    for hour in window.hours:
        if hour not in weights and (key := _local_hour_of_week(hour)) in profile:
            weights[hour] = profile[key]
            sources[hour] = "profile"
    warnings = []
    shaped = list(weights.values())
    fallback = sum(shaped) / len(shaped) if shaped else 1.0
    if fallback <= _EPSILON:
        fallback = 1.0
    even = [hour for hour in window.hours if hour not in weights]
    for hour in even:
        weights[hour] = fallback
        sources[hour] = "even"
    if even:
        warnings.append(
            f"{len(even)} hour(s) spread evenly - no reference or profile "
            "value for them"
        )
    weight_total = sum(weights.values())
    if weight_total <= _EPSILON:
        weights = dict.fromkeys(window.hours, 1.0)
        sources = dict.fromkeys(window.hours, "even")
        weight_total = float(len(window.hours))
        warnings.append(
            "the shape is zero over the whole window - spread evenly instead"
        )
    counts: dict[str, int] = {}
    for source in sources.values():
        counts[source] = counts.get(source, 0) + 1
    return (
        {hour: total * weight / weight_total for hour, weight in weights.items()},
        counts,
        warnings,
    )


def _slot_shape(inputs: _Inputs, hour: float) -> list[float] | None:
    """The first reference's 5-minute changes in `hour`, if it has all 12."""
    for reference in inputs.reference_short:
        changes = [
            reference.measured(hour + index * SLOT) for index in range(SLOTS_PER_HOUR)
        ]
        if all(change is not None and change >= 0 for change in changes):
            return cast(list[float], changes)
    return None


def _split(total: float, weights: list[float] | None) -> list[float]:
    if not weights or sum(weights) <= _EPSILON:
        return [total / SLOTS_PER_HOUR] * SLOTS_PER_HOUR
    weight_total = sum(weights)
    return [total * weight / weight_total for weight in weights]


def _parse_values(
    values: list[Any], window: Window, five_minute_from: float
) -> dict[float, float]:
    """{period start: value} from values - a list, one per period oldest
    first (hours before five_minute_from, 5-minute slots from it on), or
    [{start, value}, ...] with every other period 0."""
    periods = [hour for hour in window.hours if hour < five_minute_from] + [
        hour + index * SLOT
        for hour in window.hours
        if hour >= five_minute_from
        for index in range(SLOTS_PER_HOUR)
    ]
    hourly = sum(1 for hour in window.hours if hour < five_minute_from)
    needed = (
        f"{len(periods)} values: {hourly} hourly and "
        f"{len(periods) - hourly} 5-minute"
    )
    if values and all(isinstance(value, dict) for value in values):
        allowed = set(periods)
        parsed: dict[float, float] = dict.fromkeys(periods, 0.0)
        for item in values:
            start = dt_util.parse_datetime(str(item.get("start", "")))
            value = item.get("value")
            if (
                start is None
                or not isinstance(value, int | float)
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                raise sm.StatisticsChangeRefusedError(
                    f"each value is {{'start': ISO 8601 time, 'value': number}}, "
                    f"got {item!r}"
                )
            timestamp = dt_util.as_utc(start).timestamp()
            if timestamp not in allowed:
                raise sm.StatisticsChangeRefusedError(
                    f"{start.isoformat()} isn't one of the window's periods ({needed})"
                )
            parsed[timestamp] = float(value)
        return parsed
    if not all(
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        for value in values
    ):
        raise sm.StatisticsChangeRefusedError(
            "each value is a finite number (or all are {start, value})"
        )
    if len(values) != len(periods):
        raise sm.StatisticsChangeRefusedError(
            f"this window needs {needed} (5-minute from "
            f"{_iso(five_minute_from) if hourly < len(window.hours) else 'nowhere'})"
            f", got {len(values)}"
        )
    return {start: float(value) for start, value in zip(periods, values, strict=True)}


def _by_period(changes: dict[float, float], fmt: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for hour, change in sorted(changes.items()):
        key = dt_util.as_local(dt_util.utc_from_timestamp(hour)).strftime(fmt)
        totals[key] = totals.get(key, 0.0) + change
    return totals


def _before_after(
    before: dict[float, float], after: dict[float, float], fmt: str, label: str
) -> list[dict[str, Any]]:
    old, new = _by_period(before, fmt), _by_period(after, fmt)
    return [
        {label: key, "before": round(old[key], 6), "after": round(new[key], 6)}
        for key in old
    ]


async def _derived_from(hass: HomeAssistant, statistic_id: str) -> list[str]:
    """ha_dev_tools: statistics derive_statistics built from this one (by
    their default name, '<source> × <factor>')."""
    listed = await sm._instance(hass).async_add_executor_job(
        recorder_statistics.list_statistic_ids, hass, None, None
    )
    return sorted(
        item["statistic_id"]
        for item in listed
        if item["source"] == DOMAIN
        and (item.get("name") or "").startswith(f"{statistic_id} ×")
        and not backups.is_backup(item["statistic_id"])
    )


async def plan_redistribute(
    hass: HomeAssistant,
    statistic_id: str,
    *,
    windows: list[dict[str, datetime]] | None = None,
    detect: dict[str, Any] | None = None,
    detect_on: str | None = None,
    reference_ids: list[str] | None = None,
    profile_weeks: int | None = None,
    values: list[Any] | None = None,
    normalize: bool = False,
    max_per_hour: float | None = None,
    can_back_up: bool,
    allow_no_backup: bool = False,
) -> RedistributePlan:
    """Work out a redistribution without writing anything."""
    component_id = detect_on or statistic_id
    reference_ids = list(dict.fromkeys(reference_ids or []))
    ids = list(dict.fromkeys([statistic_id, component_id, *reference_ids]))
    metadata = await sm.read_metadata(hass, ids)
    problems = [sm._unknown(each) for each in ids if each not in metadata]
    if (windows is None) == (detect is None):
        problems.append("pass exactly one of windows or detect")
    if values is not None and (windows is None or len(windows) != 1):
        problems.append("values go with exactly one explicit window")
    if values is not None and profile_weeks:
        problems.append("values replace the shape - no profile with them")
    if overlap := {statistic_id, component_id} & set(reference_ids):
        problems.append(
            f"'{sorted(overlap)[0]}' can't be its own reference - a reference is "
            "another meter that kept recording"
        )
    if backups.is_backup(statistic_id):
        problems.append("a backup can't be edited - restore_statistics restores one")
    if problems:
        raise sm.StatisticsChangeRefusedError("; ".join(problems))
    target_meta = metadata[statistic_id]
    unit = target_meta.get("unit_of_measurement")
    for each in ids:
        if not metadata[each]["has_sum"]:
            problems.append(f"'{each}' isn't a meter (sum) statistic")
    if problems:
        raise sm.StatisticsChangeRefusedError("; ".join(problems))
    converters = {}
    for each in ids:
        converters[each], unit_problem = backups.unit_converter(
            dict(metadata[each]), unit
        )
        if unit_problem:
            problems.append(unit_problem)
    if not can_back_up and not allow_no_backup:
        problems.append(sm._no_backup())
    if problems:
        raise sm.StatisticsChangeRefusedError("; ".join(problems))

    hourly = await sm.read_rows(hass, ids)
    short = await sm.read_rows(hass, ids, StatisticsShortTerm)

    def series(each: str, rows: dict[str, Any], step: float) -> Series:
        return Series(
            each, backups.convert_rows(rows.get(each, []), converters[each]), step
        )

    target = series(statistic_id, hourly, HOUR)
    component = series(component_id, hourly, HOUR)
    target_short = series(statistic_id, short, SLOT)
    # Hours from the one with the oldest 5-minute row on have 5-minute rows
    # to rewrite; none (inf) without any.
    oldest = target_short.starts[0] if target_short.starts else None
    inputs = _Inputs(
        target=target,
        target_short=target_short,
        component=component,
        component_short=series(component_id, short, SLOT),
        references=[series(each, hourly, HOUR) for each in reference_ids],
        reference_short=[series(each, short, SLOT) for each in reference_ids],
        five_minute_from=(
            oldest - oldest % HOUR if oldest is not None else float("inf")
        ),
    )

    now = dt_util.utcnow().timestamp()
    current_hour = now - now % HOUR
    found: list[Window]
    if detect is not None:
        low = detect.get("start")
        high = detect.get("end")
        found = detect_windows(
            component,
            min_silent_hours=int(detect["min_silent_hours"]),
            min_catchup=float(detect["min_catchup"]),
            low=low.timestamp() if low else float("-inf"),
            high=min(high.timestamp() if high else current_hour, current_hour),
        )
        if not found:
            raise sm.StatisticsChangeRefusedError(
                f"no window found on '{component_id}' with those thresholds"
            )
    else:
        found = []
        for item in windows or []:
            start = dt_util.as_utc(item["start"]).timestamp()
            catchup = dt_util.as_utc(item["catchup_hour"]).timestamp()
            if start % HOUR or catchup % HOUR:
                problems.append(
                    f"window {_iso(start)} -> {_iso(catchup)}: start and "
                    "catchup_hour are the start of an hour"
                )
            elif catchup <= start:
                problems.append(
                    f"window {_iso(start)}: catchup_hour must come after start"
                )
            elif catchup >= current_hour:
                problems.append(
                    f"window {_iso(start)}: the catch-up hour {_iso(catchup)} "
                    "isn't complete yet"
                )
            elif catchup not in component.by_start:
                problems.append(
                    f"'{component_id}' has no row for the catch-up hour "
                    f"{_iso(catchup)}"
                )
            else:
                found.append(Window(start, catchup))
    found.sort(key=lambda window: window.start)
    for earlier, later in zip(found, found[1:]):
        if later.start <= earlier.catchup:
            problems.append(
                f"windows overlap: {_iso(earlier.start)} -> {_iso(earlier.catchup)} "
                f"and {_iso(later.start)} -> {_iso(later.catchup)}"
            )
    for window in found:
        problems += _check_window(target, window, f"'{statistic_id}'")
        if component_id != statistic_id:
            problems += _check_window(component, window, f"'{component_id}'")
    if problems:
        raise sm.StatisticsChangeRefusedError("; ".join(dict.fromkeys(problems)))

    rows: list[dict[str, Any]] = []
    short_rows: list[dict[str, Any]] = []
    reports = []
    for window in found:
        report, window_rows, window_short = _redistribute(
            inputs,
            found,
            window,
            profile_weeks=profile_weeks,
            values=values,
            normalize=normalize,
            max_per_hour=max_per_hour,
        )
        reports.append(report)
        rows += window_rows
        short_rows += window_short

    report_all: dict[str, Any] = {
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
        "windows": reports,
        "rows_written": len(rows),
        "five_minute_rows_written": len(short_rows),
    }
    if component_id != statistic_id:
        report_all["detect_on"] = component_id
    if derived := await _derived_from(hass, statistic_id):
        report_all["derived_series"] = {
            "statistics": derived,
            "note": "built from this meter by derive_statistics - run it again "
            "for the redistributed hours to be priced the same way",
        }
    return RedistributePlan(
        target=(await sm.describe_statistics(hass, [statistic_id]))[statistic_id],
        metadata=dict(target_meta),
        rows=rows,
        short_term_rows=short_rows,
        report=report_all,
    )


def _redistribute(
    inputs: _Inputs,
    windows: list[Window],
    window: Window,
    *,
    profile_weeks: int | None,
    values: list[Any] | None,
    normalize: bool,
    max_per_hour: float | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """One window: its report, its new hourly rows and 5-minute rows."""
    target, component = inputs.target, inputs.component
    hours = window.hours
    five_from = inputs.five_minute_from
    component_before = cast(float, component.sum_end(window.start - HOUR))
    total = cast(float, component.sum_end(window.catchup)) - component_before
    current = {hour: component.change(hour) or 0.0 for hour in hours}
    target_current = {hour: target.change(hour) or 0.0 for hour in hours}
    warnings: list[str] = []
    report: dict[str, Any] = {
        "start": _iso(window.start),
        "catchup_hour": _iso(window.catchup),
        "hours": len(hours),
        "moved": round(total, 6),
    }
    if five_from <= window.catchup:
        five_hours = sum(1 for hour in hours if hour >= five_from)
        report["values_needed"] = {
            "hourly": len(hours) - five_hours,
            "five_minute": five_hours * SLOTS_PER_HOUR,
            "five_minute_from": _iso(max(five_from, window.start)),
        }
    else:
        report["values_needed"] = {"hourly": len(hours), "five_minute": 0}

    slot_values: dict[float, list[float]] = {}
    if values is not None:
        given = _parse_values(values, window, five_from)
        if any(value < 0 for value in given.values()):
            raise sm.StatisticsChangeRefusedError("values can't be negative")
        given_total = sum(given.values())
        if abs(given_total - total) > SUM_TOLERANCE:
            off = given_total / total - 1 if total > _EPSILON else float("inf")
            if not normalize or abs(off) > NORMALIZE_LIMIT:
                raise sm.StatisticsChangeRefusedError(
                    f"the values add up to {round(given_total, 6)}, the window "
                    f"holds {round(total, 6)} - they must match (within "
                    f"{SUM_TOLERANCE}){'' if normalize else ', or pass normalize'}"
                    f"{' - normalize scales at most 2 %' if normalize else ''}"
                )
            factor = total / given_total
            given = {start: value * factor for start, value in given.items()}
            report["normalized_by"] = round(factor, 6)
        new_component: dict[float, float] = {}
        for hour in hours:
            if hour < five_from:
                new_component[hour] = given[hour]
            else:
                slots = [given[hour + index * SLOT] for index in range(SLOTS_PER_HOUR)]
                slot_values[hour] = slots
                new_component[hour] = sum(slots)
        report["methods"] = {"values": len(hours)}
    else:
        new_component, counts, shape_warnings = _shape(
            inputs, windows, window, profile_weeks, total
        )
        warnings += shape_warnings
        report["methods"] = counts
    if inputs.references:
        # With values, only compared against - see _parse_values.
        report["references"] = []
        for reference in inputs.references:
            measured = [
                change
                for hour in hours
                if (change := reference.measured(hour)) is not None
            ]
            ratio = sum(measured) / total if total > _EPSILON else None
            entry: dict[str, Any] = {
                "statistic_id": reference.statistic_id,
                "hours_with_data": len(measured),
                "total": round(sum(measured), 6),
                "ratio_to_moved": round(ratio, 4) if ratio is not None else None,
            }
            report["references"].append(entry)
            if (
                ratio is not None
                and len(measured) == len(hours)
                and abs(ratio - 1) > RATIO_WARNING
            ):
                warnings.append(
                    f"'{reference.statistic_id}' measured {round(ratio * 100)} % of "
                    "the moved amount over the window - the wrong reference, or a "
                    "real event (a reset?) in it"
                )
            if _flat(measured):
                warnings.append(
                    f"'{reference.statistic_id}' is suspiciously constant over the "
                    "window - an estimate (e.g. a cloud fill-in) rather than a "
                    "measurement? Its shape would be wrong."
                )
        report["quality"] = [
            _quality(component, reference, window) for reference in inputs.references
        ]
        if values is not None:
            report["reference_by_hour"] = {
                reference.statistic_id: [
                    (
                        round(change, 6)
                        if (change := reference.measured(hour)) is not None
                        else None
                    )
                    for hour in hours
                ]
                for reference in inputs.references
            }

    new_target = {
        hour: target_current[hour] - current[hour] + new_component[hour]
        for hour in hours
    }
    if negative := [hour for hour, change in new_target.items() if change < -1e-6]:
        raise sm.StatisticsChangeRefusedError(
            f"'{target.statistic_id}' would go down at {_iso(negative[0])} - the "
            "component's hours don't fit the target's"
        )
    if max_per_hour is not None and (
        over := [hour for hour, change in new_target.items() if change > max_per_hour]
    ):
        raise sm.StatisticsChangeRefusedError(
            f"{len(over)} hour(s) would be above max_per_hour={max_per_hour}, the "
            f"first {_iso(over[0])} with {round(new_target[over[0]], 6)}"
        )
    report["max_hour_before"] = round(max(target_current.values()), 6)
    report["max_hour_after"] = round(max(new_target.values()), 6)
    if values is not None:
        report["by_hour"] = [
            {
                "hour": _iso(hour),
                "before": round(target_current[hour], 6),
                "after": round(new_target[hour], 6),
            }
            for hour in hours
        ]
    report["by_day"] = _before_after(target_current, new_target, "%Y-%m-%d", "day")
    report["by_month"] = _before_after(target_current, new_target, "%Y-%m", "month")
    if warnings:
        report["warnings"] = warnings

    # The rows: sums (and the reading, `state`) carried on from the hour
    # before the window; the catch-up hour ends exactly where it did.
    before = cast(dict[str, Any], target.last_row(window.start - HOUR))
    base_sum, base_state = before["sum"], before.get("state")
    rows: list[dict[str, Any]] = []
    short_rows: list[dict[str, Any]] = []
    running = base_sum
    for hour in hours:
        hour_start_sum = running
        running += new_target[hour]
        own = target.by_start.get(hour)
        if hour == window.catchup and own is not None:
            # Exactly where it was - values within SUM_TOLERANCE of the
            # total mustn't shift every later hour by the difference.
            running = own["sum"]
            new_target[hour] = running - hour_start_sum
        state = (
            own.get("state")
            if hour == window.catchup and own is not None
            else (base_state + running - base_sum if base_state is not None else None)
        )
        rows.append(
            {
                "start_ts": hour,
                "sum": running,
                "state": state,
                "last_reset_ts": before.get("last_reset_ts"),
            }
        )
        if hour < five_from:
            continue
        if hour in slot_values:
            raw = [
                (
                    (inputs.target_short.change(slot) or 0.0)
                    - (inputs.component_short.change(slot) or 0.0)
                    + value
                    if component.statistic_id != target.statistic_id
                    else value
                )
                for slot, value in zip(
                    [hour + index * SLOT for index in range(SLOTS_PER_HOUR)],
                    slot_values[hour],
                    strict=True,
                )
            ]
            slots = _split(new_target[hour], [max(0.0, value) for value in raw])
        else:
            slots = _split(new_target[hour], _slot_shape(inputs, hour))
        slot_sum = hour_start_sum
        for index, value in enumerate(slots):
            slot_sum += value
            last = index == SLOTS_PER_HOUR - 1
            short_rows.append(
                {
                    "start_ts": hour + index * SLOT,
                    "sum": running if last else slot_sum,
                    "state": (
                        state
                        if last
                        else (
                            base_state + slot_sum - base_sum
                            if base_state is not None
                            else None
                        )
                    ),
                    "last_reset_ts": before.get("last_reset_ts"),
                }
            )
    return report, rows, short_rows


async def redistribute_statistics(
    hass: HomeAssistant, plan: RedistributePlan
) -> dict[str, Any]:
    """Write a plan's hourly and 5-minute rows, checked against the
    recorder. The caller backs the statistic up first."""
    statistic_id = plan.metadata["statistic_id"]
    metadata = cast(StatisticMetaData, plan.metadata)
    sm.check_importable(metadata)

    def queue() -> None:
        if plan.rows:
            sm.queue_import(hass, metadata, plan.rows)
        if plan.short_term_rows:
            sm.queue_short_term_rows(hass, metadata, plan.short_term_rows)

    async def written() -> list[str]:
        return await sm.row_mismatches(
            hass, statistic_id, plan.rows
        ) + await sm.row_mismatches(
            hass, statistic_id, plan.short_term_rows, StatisticsShortTerm
        )

    await sm.on_recorder_verified(hass, queue, written, what="the redistribution")
    after = (await sm.describe_statistics(hass, [statistic_id]))[statistic_id]
    result: dict[str, Any] = {"redistributed": {**plan.preview(), "target": after}}
    result["next_compile"] = await sm.continuity_check(hass, statistic_id)
    return result
