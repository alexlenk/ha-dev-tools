"""Tests for the dev_tools access-control gate (access_control.py).

Covers the two independent checks every gated tool call must pass - see
access_control.py's module docstring for the full reasoning - plus the
best-effort cleanup task. Uses explicit past timestamps (os.utime /
file content) rather than freezegun or real sleeps, since the checks
are pure comparisons against time.time() and don't need a frozen clock.
"""

import time

import homeassistant.util.dt as dt_util
import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockUser,
    async_fire_time_changed,
)

from custom_components.ha_dev_tools import access_control
from custom_components.ha_dev_tools.access_control import NotAdminError, NotArmedError
from custom_components.ha_dev_tools.const import DOMAIN, OPT_DRY_RUN
from custom_components.ha_dev_tools.ws_call import UnresolvedUserError


def _arm_path(hass: HomeAssistant):
    path = access_control._arm_file_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_arm_file(hass: HomeAssistant, *, armed_at: float, mtime: float) -> None:
    path = _arm_path(hass)
    path.write_text(str(armed_at))
    import os

    os.utime(path, (mtime, mtime))


def _llm_context(user_id: str | None) -> llm.LLMContext:
    import inspect

    fields = {
        "platform": "test",
        "context": Context(user_id=user_id) if user_id else None,
        "user_prompt": None,
        "language": "en",
        "assistant": "test",
        "device_id": None,
    }
    accepted = set(inspect.signature(llm.LLMContext.__init__).parameters)
    return llm.LLMContext(**{k: v for k, v in fields.items() if k in accepted})


@pytest.fixture(autouse=True)
def _clean_arm_file(hass: HomeAssistant):
    """hass.config.config_dir is a shared, non-per-test-isolated directory
    (pytest_homeassistant_custom_component's default fixture reuses one
    physical testing_config dir across tests/runs) - without this, an arm
    file written by one test leaks into whichever test runs next."""
    path = access_control._arm_file_path(hass)
    path.unlink(missing_ok=True)
    yield
    path.unlink(missing_ok=True)


@pytest.fixture
async def admin_user(hass: HomeAssistant):
    return MockUser(is_owner=True).add_to_hass(hass)


@pytest.fixture
async def non_admin_user(hass: HomeAssistant):
    return MockUser(is_owner=False).add_to_hass(hass)


# --- check_armed --------------------------------------------------------


@pytest.mark.asyncio
async def test_check_armed_raises_when_file_missing(hass: HomeAssistant):
    with pytest.raises(NotArmedError):
        await access_control.check_armed(hass)


@pytest.mark.asyncio
async def test_check_armed_raises_when_idle_expired(hass: HomeAssistant):
    now = time.time()
    _write_arm_file(
        hass, armed_at=now, mtime=now - access_control.IDLE_TIMEOUT.total_seconds() - 1
    )

    with pytest.raises(NotArmedError):
        await access_control.check_armed(hass)


@pytest.mark.asyncio
async def test_check_armed_raises_when_hard_cap_exceeded(hass: HomeAssistant):
    now = time.time()
    # mtime is fresh (just used), but the original arm time is past the cap.
    _write_arm_file(
        hass, armed_at=now - access_control.MAX_SESSION.total_seconds() - 1, mtime=now
    )

    with pytest.raises(NotArmedError):
        await access_control.check_armed(hass)


@pytest.mark.asyncio
async def test_check_armed_raises_when_content_unparseable(hass: HomeAssistant):
    path = _arm_path(hass)
    path.write_text("not a timestamp")

    with pytest.raises(NotArmedError):
        await access_control.check_armed(hass)


@pytest.mark.asyncio
async def test_check_armed_passes_when_fresh_and_within_cap(hass: HomeAssistant):
    now = time.time()
    _write_arm_file(hass, armed_at=now, mtime=now)

    await access_control.check_armed(hass)  # does not raise


# --- touch_armed ---------------------------------------------------------


@pytest.mark.asyncio
async def test_touch_armed_bumps_mtime_not_content(hass: HomeAssistant):
    now = time.time()
    original_armed_at = now - 60
    _write_arm_file(hass, armed_at=original_armed_at, mtime=now - 60)

    await access_control.touch_armed(hass)

    path = access_control._arm_file_path(hass)
    assert access_control._read_armed_at(path) == original_armed_at
    assert path.stat().st_mtime == pytest.approx(time.time(), abs=5)


@pytest.mark.asyncio
async def test_touch_armed_is_best_effort_when_file_missing(hass: HomeAssistant):
    await access_control.touch_armed(hass)  # does not raise, just logs


# --- require_admin ---------------------------------------------------------


@pytest.mark.asyncio
async def test_require_admin_passes_for_admin(hass: HomeAssistant, admin_user):
    await access_control.require_admin(hass, _llm_context(admin_user.id))


@pytest.mark.asyncio
async def test_require_admin_raises_for_non_admin(hass: HomeAssistant, non_admin_user):
    with pytest.raises(NotAdminError):
        await access_control.require_admin(hass, _llm_context(non_admin_user.id))


@pytest.mark.asyncio
async def test_require_admin_raises_for_unresolvable_user(hass: HomeAssistant):
    with pytest.raises(UnresolvedUserError):
        await access_control.require_admin(hass, _llm_context(None))


# --- is_dry_run -------------------------------------------------------------


def test_is_dry_run_false_with_no_config_entry(hass: HomeAssistant):
    assert access_control.is_dry_run(hass) is False


def test_is_dry_run_false_by_default(hass: HomeAssistant):
    MockConfigEntry(domain=DOMAIN, options={}).add_to_hass(hass)

    assert access_control.is_dry_run(hass) is False


def test_is_dry_run_true_when_option_enabled(hass: HomeAssistant):
    MockConfigEntry(domain=DOMAIN, options={OPT_DRY_RUN: True}).add_to_hass(hass)

    assert access_control.is_dry_run(hass) is True


def test_is_dry_run_reflects_live_option_updates(hass: HomeAssistant):
    """No reload needed - toggling the option takes effect immediately,
    since is_dry_run() always re-reads the live config entry."""
    entry = MockConfigEntry(domain=DOMAIN, options={OPT_DRY_RUN: False})
    entry.add_to_hass(hass)
    assert access_control.is_dry_run(hass) is False

    hass.config_entries.async_update_entry(entry, options={OPT_DRY_RUN: True})

    assert access_control.is_dry_run(hass) is True


# --- cleanup ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_removes_expired_file(hass: HomeAssistant):
    now = time.time()
    _write_arm_file(
        hass, armed_at=now, mtime=now - access_control.IDLE_TIMEOUT.total_seconds() - 1
    )
    path = access_control._arm_file_path(hass)
    assert path.exists()

    unsub = await access_control.async_setup_cleanup(hass)
    try:
        assert not path.exists()
    finally:
        unsub()


@pytest.mark.asyncio
async def test_cleanup_leaves_valid_file_alone(hass: HomeAssistant):
    now = time.time()
    _write_arm_file(hass, armed_at=now, mtime=now)
    path = access_control._arm_file_path(hass)

    unsub = await access_control.async_setup_cleanup(hass)
    try:
        assert path.exists()
    finally:
        unsub()


@pytest.mark.asyncio
async def test_cleanup_tick_removes_expired_file(hass: HomeAssistant):
    """The periodic tick (not just the initial cleanup run at setup) also
    runs the cleanup - covers the fire-and-forget executor job in _tick."""
    path = access_control._arm_file_path(hass)

    unsub = await access_control.async_setup_cleanup(hass)
    try:
        # Nothing to clean up yet - write an expired file only after setup,
        # so the assertion below can only pass via the periodic tick.
        now = time.time()
        _write_arm_file(
            hass,
            armed_at=now,
            mtime=now - access_control.IDLE_TIMEOUT.total_seconds() - 1,
        )
        assert path.exists()

        async_fire_time_changed(
            hass, dt_util.utcnow() + access_control.CLEANUP_INTERVAL
        )
        # The tick's executor job is fire-and-forget from a @callback, so
        # it lands in hass's background tasks, not its tracked tasks.
        await hass.async_block_till_done(wait_background_tasks=True)

        assert not path.exists()
    finally:
        unsub()
