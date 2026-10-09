"""Tests for derive_statistics (issue #143), against a real recorder -
fixture pattern as in test_statistics_merge.py."""

from datetime import timedelta

import pytest
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_import_statistics,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ha_dev_tools import statistics_manager as sm
from custom_components.ha_dev_tools.statistics_derive import (
    Rate,
    compute,
    derive_statistics,
    parse_factor,
    plan_derive,
)
from tests.test_statistics_manager import (
    START,
    _metadata,
    _seed,
)

H = 3600.0
T0 = START.timestamp()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Shadow conftest.py's fixture - see test_history_manager.py."""
    yield


def _changes(first_hour: int, deltas: list[float]) -> dict[float, float]:
    return {T0 + (first_hour + index) * H: delta for index, delta in enumerate(deltas)}


def _old(first_hour: int, sums: list[float], states: list[float] | None = None):
    return {
        T0
        + (first_hour + index)
        * H: {
            "sum": value,
            "state": (states or sums)[index],
            "last_reset_ts": None,
        }
        for index, value in enumerate(sums)
    }


# --- the factor and rate resolution ------------------------------------------


def test_factor_kinds():
    fixed = parse_factor(0.0794)
    assert fixed.kind == "fixed"
    assert fixed.at(T0) == 0.0794
    assert fixed.at(T0 + 900 * H) == 0.0794

    periods = parse_factor(
        [(START + timedelta(hours=3), 0.1), (START, 0.2)],
    )
    assert [value for _, value in periods.periods] == [0.2, 0.1]
    assert periods.at(T0) == 0.2
    assert periods.at(T0 + 3 * H) == 0.1
    # Before the first period the rate doesn't cover the hour.
    assert periods.at(T0 - H) is None

    hourly = Rate("statistic", {"kind": "statistic"}, hourly={T0: 0.3})
    assert hourly.at(T0) == 0.3
    assert hourly.at(T0 + H) is None

    with pytest.raises(sm.StatisticsChangeRefusedError, match="negative"):
        parse_factor(-1)
    with pytest.raises(sm.StatisticsChangeRefusedError, match="distinct"):
        parse_factor([(START, 0.1), (START, 0.2)])
    with pytest.raises(sm.StatisticsChangeRefusedError, match="number"):
        parse_factor(True)


# --- the derivation math ------------------------------------------------------


def test_fixed_rate_accumulates_from_the_anchor():
    rows, report = compute(
        _changes(0, [2, 3, 1]),
        parse_factor(0.5),
        {},
        anchor=10.0,
        low=T0,
        high=T0 + 3 * H,
    )
    assert [row["sum"] for row in rows] == [11.0, 12.5, 13.0]
    # A new target's state is its own running sum.
    assert [row["state"] for row in rows] == [11.0, 12.5, 13.0]
    assert report["total"] == 3.0
    assert report["old_total"] == 0.0
    assert report["replaced_rows"] == 0
    assert report["months"][0]["derived"] == 3.0


def test_old_rows_in_the_range_are_replaced_and_carry_the_sum():
    old = _old(0, [10, 20], states=[10, 20])
    old[T0 + 4 * H] = {"sum": 25, "state": 25, "last_reset_ts": None}
    rows, report = compute(
        _changes(2, [2, 4]),
        parse_factor(1.0),
        old,
        anchor=5.0,
        low=T0,
        high=T0 + 5 * H,
    )
    # The sum continues the anchor, not the old rows' basis: hours 0-1
    # carry (replacing the old rows), hours 2-3 are priced, hour 4's old
    # row is replaced by a carry too.
    assert [row["sum"] for row in rows] == [5.0, 5.0, 7.0, 11.0, 11.0]
    # The target keeps its own state where it had a row.
    assert rows[0]["state"] == 10
    assert rows[4]["state"] == 25
    assert report["replaced_rows"] == 3
    assert report["total"] == 6.0
    assert report["old_total"] == 20.0  # 10 -> 20 -> 25 across the range


def test_hours_without_a_rate_are_refused():
    rate = Rate("statistic", {"kind": "statistic"}, hourly={T0: 0.5})
    with pytest.raises(sm.StatisticsChangeRefusedError, match="doesn't cover 2"):
        compute(_changes(0, [1, 2, 3]), rate, {}, 0.0, T0, T0 + 3 * H)
    with pytest.raises(sm.StatisticsChangeRefusedError, match="no hourly rows"):
        compute(_changes(0, [1]), parse_factor(1.0), {}, 0.0, T0 + 5 * H, T0 + 9 * H)


def test_monthly_totals_split_the_range():
    late = T0 + 40 * 24 * H  # over a month after START
    rows, report = compute(
        {T0: 1.0, late: 2.0},
        parse_factor(1.0),
        {},
        0.0,
        T0,
        late + H,
    )
    assert len(report["months"]) == 2
    assert report["months"][0]["derived"] == 1.0
    assert report["months"][1]["derived"] == 2.0


# --- planning and writing against the recorder ---------------------------------


async def _price(hass: HomeAssistant, statistic_id: str, first_hour: int, means):
    async_import_statistics(
        hass,
        {
            **_metadata(statistic_id, "recorder", statistic_id),
            "has_sum": False,
            "mean_type": StatisticMeanType.ARITHMETIC,
        },
        [
            {
                "start": START + timedelta(hours=first_hour + index),
                "mean": value,
                "min": value,
                "max": value,
            }
            for index, value in enumerate(means)
        ],
    )
    await async_wait_recording_done(hass)


async def _compensation(hass: HomeAssistant, statistic_id: str, sums, unit="EUR"):
    async_import_statistics(
        hass,
        {
            **_metadata(statistic_id, "recorder", statistic_id),
            "unit_of_measurement": unit,
            "unit_class": None,
        },
        [
            {
                "start": START + timedelta(hours=index),
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
    ("source", "target", "factor", "kwargs", "expected"),
    [
        ("sensor.no_meter", "sensor.old_meter", 1, {}, "no statistic"),
        ("sensor.temperature", "sensor.old_meter", 1, {}, "isn't a meter"),
        ("sensor.old_meter", "ha_dev_tools:backup_x_20260101t000000", 1, {}, "backup"),
        ("sensor.old_meter", "sensor.temperature", 1, {}, "running sums"),
        ("sensor.old_meter", "sensor.old_meter", 1, {}, "can't also be the source"),
        ("sensor.old_meter", "sensor.gas", 1, {"unit": "kWh"}, "is in 'm³'"),
        ("sensor.old_meter", "sensor.brand_new", 1, {}, "'ha_dev_tools:"),
        ("sensor.old_meter", "ha_dev_tools:money", 1, {}, "needs its unit"),
        (
            "sensor.old_meter",
            "ha_dev_tools:money",
            1,
            {"source_unit": "m³"},
            "converted",
        ),
        (
            "sensor.old_meter",
            "sensor.old_meter",
            1,
            {"can_back_up": False},
            "allow_no_backup",
        ),
        ("sensor.old_meter", "sensor.old_meter", -1, {}, "negative"),
        ("sensor.old_meter", "sensor.old_meter", "sensor.gas", {}, "isn't a mean"),
        (
            "sensor.old_meter",
            "sensor.old_meter",
            "sensor.no_price",
            {},
            "no statistic 'sensor.no_price'",
        ),
    ],
)
async def test_plan_derive_refusals(
    hass: HomeAssistant, source, target, factor, kwargs, expected
):
    await _seed(hass)  # old_meter: kWh, hours 0-2, sums 0, 1.5, 3
    async_import_statistics(
        hass,
        {
            **_metadata("sensor.temperature", "recorder", "sensor.temperature"),
            "has_sum": False,
            "mean_type": StatisticMeanType.ARITHMETIC,
        },
        [{"start": START, "mean": 1, "min": 0, "max": 2}],
    )
    async_import_statistics(  # a gas meter in m³
        hass,
        {
            **_metadata("sensor.gas", "recorder", "sensor.gas"),
            "unit_of_measurement": "m³",
            "unit_class": "volume",
        },
        [{"start": START, "state": 1, "sum": 1}],
    )
    async_add_external_statistics(  # a backup the target can't be
        hass,
        _metadata("ha_dev_tools:backup_x_20260101t000000", "ha_dev_tools", "x"),
        [{"start": START, "state": 0, "sum": 0}],
    )
    await async_wait_recording_done(hass)
    arguments = {"can_back_up": True, **kwargs}
    with pytest.raises(sm.StatisticsChangeRefusedError, match=expected):
        await plan_derive(hass, source, target, factor, **arguments)


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_derive_into_a_new_external_target(hass: HomeAssistant):
    await _seed(hass)
    plan = await plan_derive(
        hass,
        "sensor.old_meter",
        "ha_dev_tools:export_compensation",
        0.5,
        unit="EUR",
        can_back_up=True,
    )
    assert plan.existing is False
    assert plan.seed is False
    assert plan.shift == (None, 0.0)
    # sums 0, 1.5, 3 -> changes 0, 1.5, 1.5 -> compensation 0, 0.75, 1.5
    assert [row["sum"] for row in plan.rows] == [0.0, 0.75, 1.5]
    preview = plan.preview()
    assert preview["rows_written"] == 3
    assert preview["total"] == 1.5
    assert preview["months"][0]["derived"] == 1.5

    result = await derive_statistics(hass, plan)
    assert result["derived"]["target"]["rows"] == 3
    rows = (await sm.read_rows(hass, ["ha_dev_tools:export_compensation"]))[
        "ha_dev_tools:export_compensation"
    ]
    assert [row["sum"] for row in rows] == [0.0, 0.75, 1.5]
    # An external statistic isn't compiled, so there's no check to run.
    assert "next_compile" not in result


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_derive_replaces_a_live_targets_range_and_shifts_after_it(
    hass: HomeAssistant,
):
    await _seed(hass)  # old_meter: hours 0-2, sums 0, 1.5, 3
    await _compensation(hass, "sensor.grid_compensation", [0, 9, 9])  # wrong prices
    plan = await plan_derive(
        hass,
        "sensor.old_meter",
        "sensor.grid_compensation",
        0.5,
        start=START,
        end=START + timedelta(hours=2),  # replaces hours 0-1
        can_back_up=True,
    )
    assert plan.existing is True
    # Anchor 0 (nothing before the range), derived 0, 0.75 over hours 0-1.
    assert [row["sum"] for row in plan.rows] == [0.0, 0.75]
    # Old basis at the range end was 9 - the hour-2 row (9) and anything
    # after it must move onto the derived basis: 0.75 - 9 = -8.25.
    assert plan.shift[1] == pytest.approx(-8.25)
    assert plan.shift[0] == (START + timedelta(hours=2)).timestamp()

    result = await derive_statistics(hass, plan)
    rows = (await sm.read_rows(hass, ["sensor.grid_compensation"]))[
        "sensor.grid_compensation"
    ]
    assert [row["sum"] for row in rows] == [0.0, 0.75, 0.75]
    # No 5-minute rows exist, so the target got one seeded.
    short = (
        await sm.read_rows(hass, ["sensor.grid_compensation"], StatisticsShortTerm)
    )["sensor.grid_compensation"]
    assert short[-1]["sum"] == pytest.approx(0.75)
    # And the result says the next compile will continue from it.
    assert result["next_compile"]["ok"] is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_derive_from_a_price_statistic_hour_by_hour(hass: HomeAssistant):
    await _seed(hass)
    await _price(hass, "sensor.price", 0, [0.1, 0.2, 0.3])
    plan = await plan_derive(
        hass,
        "sensor.old_meter",
        "ha_dev_tools:import_cost",
        "sensor.price",
        unit="EUR",
        can_back_up=True,
    )
    # changes 0, 1.5, 1.5 priced 0.1, 0.2, 0.3 -> 0, 0.3, 0.75
    assert [row["sum"] for row in plan.rows] == pytest.approx([0.0, 0.3, 0.75])
    assert plan.rate["kind"] == "statistic"
    assert plan.rate["hours"] == 3
    await derive_statistics(hass, plan)
    rows = (await sm.read_rows(hass, ["ha_dev_tools:import_cost"]))[
        "ha_dev_tools:import_cost"
    ]
    assert [row["sum"] for row in rows] == pytest.approx([0.0, 0.3, 0.75])


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_derive_converts_the_source_unit(hass: HomeAssistant):
    await _seed(hass)  # kWh
    plan = await plan_derive(
        hass,
        "sensor.old_meter",
        "ha_dev_tools:money",
        100.0,  # per Wh, not per kWh
        unit="EUR",
        source_unit="Wh",
        can_back_up=True,
    )
    # changes in Wh: 0, 1500, 1500 -> 0, 150000, 300000
    assert [row["sum"] for row in plan.rows] == [0.0, 150000.0, 300000.0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_derive_refuses_when_the_price_has_gaps(hass: HomeAssistant):
    await _seed(hass)
    await _price(hass, "sensor.price", 1, [0.2, 0.3])  # hour 0 uncovered
    with pytest.raises(sm.StatisticsChangeRefusedError, match="doesn't cover 1"):
        await plan_derive(
            hass,
            "sensor.old_meter",
            "ha_dev_tools:import_cost",
            "sensor.price",
            unit="EUR",
            can_back_up=True,
        )
