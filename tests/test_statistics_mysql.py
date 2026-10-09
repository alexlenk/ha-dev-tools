"""Statistics writes against a real MariaDB/MySQL recorder, with real lock
contention (issue #148).

On MySQL/MariaDB - and only there - HA's statistics import and sum
adjustment retry a lock-wait timeout or a deadlock (errors 1205/1206/1213,
recorder.util.retryable_database_job): the task sleeps db_retry_wait and
re-queues itself at the *end* of the recorder's queue, for as long as it
keeps failing. test_statistics_writes.py simulates that by patching HA's
functions; here a second connection holds the rows instead
(`SELECT ... FOR UPDATE` over the whole table, which under InnoDB's
REPEATABLE READ also locks the gaps new rows go into), so the recorder's
own statement times out.

Skipped on SQLite. Run with `pytest tests/test_statistics_mysql.py
--dburl "mysql://user:pw@host/ha_test?charset=utf8mb4" --drop-existing-db`
(the CI job `test (MariaDB)` does).
"""

import logging
import threading
import time
from datetime import timedelta

import pytest
import sqlalchemy as sa
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ha_dev_tools import statistics_manager as sm
from custom_components.ha_dev_tools.statistics_merge import (
    merge_statistics,
    plan_merge,
)
from tests.test_statistics_merge import _latest_short_term_sum, _meter
from tests.test_statistics_writes import _live_target, _sums

pytestmark = pytest.mark.skipif(
    "not config.getoption('dburl').startswith('mysql://')",
    reason="needs a MariaDB/MySQL recorder (--dburl mysql://...)",
)

# Seconds a statement waits for a row lock before error 1205.
LOCK_WAIT = 1


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Shadow conftest.py's fixture - see test_history_manager.py."""
    yield


@pytest.fixture(autouse=True)
def short_lock_waits(pytestconfig):
    """innodb_lock_wait_timeout for the connections the recorder opens -
    the 50 s default would make every timeout take that long."""
    server = sa.make_url(pytestconfig.getoption("dburl")).set(database="")
    engine = sa.create_engine(server)
    with engine.begin() as connection:
        before = connection.execute(
            sa.text("SELECT @@GLOBAL.innodb_lock_wait_timeout")
        ).scalar()
        connection.execute(
            sa.text(f"SET GLOBAL innodb_lock_wait_timeout = {LOCK_WAIT}")
        )
    yield
    with engine.begin() as connection:
        connection.execute(sa.text(f"SET GLOBAL innodb_lock_wait_timeout = {before}"))
    engine.dispose()


@pytest.fixture
def retries(caplog):
    """HA's 'not completed, retrying' log lines - proof a write really hit
    a lock-wait timeout and was re-queued."""
    caplog.set_level(logging.INFO, logger="homeassistant.components.recorder.util")

    def count(job: str) -> int:
        return sum(
            f"{job} not completed, retrying" in record.getMessage()
            for record in caplog.records
        )

    return count


class RowLock:
    """A second connection holding every row (and gap) of the hourly
    statistics table for `seconds`, as a long purge or a backup tool
    would."""

    def __init__(self, recorder_url: str, seconds: float) -> None:
        self.engine = sa.create_engine(recorder_url)
        self.seconds = seconds
        self.held = threading.Event()
        self.thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self) -> None:
        with self.engine.connect() as connection, connection.begin():
            connection.execute(sa.text("SELECT id FROM statistics FOR UPDATE"))
            self.held.set()
            time.sleep(self.seconds)

    def start(self) -> None:
        self.thread.start()
        assert self.held.wait(10), "couldn't lock the statistics table"

    def join(self) -> None:
        if self.thread.is_alive():
            self.thread.join()
        self.engine.dispose()


@pytest.fixture
def row_lock(recorder_db_url):
    """RowLock factory; every lock is released and its connection closed
    before the test database is dropped, even when the test fails."""
    made: list[RowLock] = []

    def make(seconds: float) -> RowLock:
        made.append(RowLock(recorder_db_url, seconds))
        return made[-1]

    yield make
    for lock in made:
        lock.join()


async def _plan(hass: HomeAssistant):
    return await plan_merge(
        hass,
        "sensor.new_meter",
        ["sensor.old_meter"],
        can_back_up=False,
        allow_no_backup=True,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_merge_import_hits_a_lock_wait_timeout(
    hass: HomeAssistant, freezer, row_lock, retries
):
    """The #146 ordering bug with the real engine: the import times out,
    re-queues behind what was queued after it, and the adjustment must
    still come after it."""
    await _live_target(hass, freezer)
    plan = await _plan(hass)
    lock = row_lock(2)
    lock.start()
    result = await merge_statistics(hass, plan)
    lock.join()

    assert retries("statistics") >= 1
    assert await _sums(hass, "sensor.new_meter") == [0, 1.5, 3, 4, 5]
    assert await _latest_short_term_sum(hass, "sensor.new_meter") == 5.0
    assert result["next_compile"]["ok"] is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_merge_adjustment_hits_a_lock_wait_timeout(
    hass: HomeAssistant, freezer, row_lock, retries, monkeypatch
):
    await _live_target(hass, freezer)
    plan = await _plan(hass)
    adjust = sm.queue_adjust
    lock = row_lock(2)

    def locked_adjust(*args):
        lock.start()  # only the adjustment meets the lock
        adjust(*args)

    monkeypatch.setattr(sm, "queue_adjust", locked_adjust)
    result = await merge_statistics(hass, plan)
    lock.join()

    assert retries("ha_dev_tools sum adjustment") >= 1
    # Applied exactly once, to the hourly and the 5-minute rows alike, and
    # reported only once it was. HA's own adjustment swallowed the timeout:
    # 5-minute rows shifted (5.0), hourly ones not ([..., 1, 2]), committed.
    assert await _sums(hass, "sensor.new_meter") == [0, 1.5, 3, 4, 5]
    assert await _latest_short_term_sum(hass, "sensor.new_meter") == 5.0
    assert result["next_compile"]["ok"] is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_long_lock_is_waited_out_not_reported_as_failed(
    hass: HomeAssistant, freezer, row_lock, retries
):
    """HA keeps retrying for as long as the lock is held. Giving up after a
    few rounds (2.26.4) reported the write as not applied while it was
    still pending - and once the lock went, it applied anyway: a retry by
    then would have been applied twice."""
    await _live_target(hass, freezer)
    plan = await _plan(hass)
    lock = row_lock(6 * (LOCK_WAIT + 3))  # ~6 retries
    lock.start()
    result = await merge_statistics(hass, plan)
    lock.join()

    assert retries("statistics") >= 5
    assert await _sums(hass, "sensor.new_meter") == [0, 1.5, 3, 4, 5]
    assert result["next_compile"]["ok"] is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_clear_that_hits_a_lock_is_reported_at_once(
    hass: HomeAssistant, row_lock
):
    """HA doesn't retry a clear: it fails, and is reported as not applied
    without waiting - with the series still all there."""
    from tests.test_statistics_manager import _seed

    await _seed(hass)
    lock = row_lock(3 * LOCK_WAIT)
    lock.start()
    started = time.monotonic()
    with pytest.raises(sm.StatisticsNotAppliedError, match="still has its rows"):
        await sm.clear_statistics(hass, ["sensor.old_meter"])
    assert time.monotonic() - started < 30
    lock.join()
    await async_wait_recording_done(hass)
    assert await _sums(hass, "sensor.old_meter") == [0, 1.5, 3]


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_long_series_on_mariadb(hass: HomeAssistant, freezer):
    """Issue #145's size against the real engine: HA's import checks every
    row on its own, which is what made the backup outlast the old wait."""
    from custom_components.ha_dev_tools import statistics_backup as backups
    from tests.test_statistics_manager import START

    hours = 14_600
    freezer.move_to(START + timedelta(hours=hours + 2))
    await _meter(hass, "sensor.old_meter", 0, [index * 0.5 for index in range(hours)])

    made = await backups.create_backups(hass, ["sensor.old_meter"], "clear_statistics")
    backup = made["sensor.old_meter"]
    assert len(await _sums(hass, backup)) == hours


@pytest.mark.asyncio
@pytest.mark.usefixtures("recorder_mock")
async def test_a_restore_whose_clear_hits_a_lock_changes_nothing(
    hass: HomeAssistant, row_lock
):
    """The clear fails (HA doesn't retry it); the backup must not then be
    imported over the target's own series."""
    from custom_components.ha_dev_tools import statistics_backup as backups
    from tests.test_statistics_manager import _seed

    await _seed(hass)
    made = await backups.create_backups(hass, ["sensor.old_meter"], "clear_statistics")
    await _meter(hass, "sensor.wh_meter", 10, [1000, 2000], unit="Wh")
    plan = await backups.plan_restore(
        hass, made["sensor.old_meter"], "sensor.wh_meter", can_back_up=True
    )
    lock = row_lock(3 * LOCK_WAIT)
    lock.start()
    with pytest.raises(sm.StatisticsNotAppliedError, match="restore"):
        await backups.restore_statistics(hass, plan)
    lock.join()
    await async_wait_recording_done(hass)
    assert await _sums(hass, "sensor.wh_meter") == [1000, 2000]
