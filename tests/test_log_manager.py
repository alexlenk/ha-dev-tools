"""Test LogManager functionality with Home Assistant fixtures."""

import logging

from datetime import datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ha_dev_tools.log_manager import LogEntry, LogFilters, LogManager
from custom_components.ha_dev_tools.security import SecurityManager

_TEST_LOGGER = logging.getLogger("tests.test_log_manager.fixture_logger")


@pytest.fixture
def security_manager(hass: HomeAssistant):
    """Create a SecurityManager instance for testing."""
    return SecurityManager(hass)


@pytest.fixture
def log_manager(hass: HomeAssistant, security_manager):
    """Create a LogManager instance for testing."""
    return LogManager(hass, security_manager)


async def _setup_system_log(hass: HomeAssistant) -> None:
    """Set up the real system_log integration backing get_core_logs."""
    assert await async_setup_component(hass, "system_log", {})


async def _seed_log_entries(hass: HomeAssistant) -> None:
    """Emit real WARNING/ERROR log records for system_log to capture.

    system_log's handler is installed at logging.WARNING - DEBUG/INFO
    records are never captured, matching the native Logs page.
    """
    _TEST_LOGGER.warning("Sensor unavailable")
    _TEST_LOGGER.error("Failed to turn on light")
    await hass.async_block_till_done()


async def test_get_core_logs_basic(hass: HomeAssistant, log_manager):
    """Test retrieving core logs without filters."""
    await _setup_system_log(hass)
    await _seed_log_entries(hass)

    filters = LogFilters()
    logs = await log_manager.get_core_logs(filters)

    assert len(logs) > 0
    assert all(isinstance(log, LogEntry) for log in logs)


async def test_get_core_logs_no_system_log(hass: HomeAssistant, log_manager):
    """Test retrieving logs when system_log hasn't been set up."""
    filters = LogFilters()
    logs = await log_manager.get_core_logs(filters)

    assert logs == []


async def test_get_core_logs_empty_buffer(hass: HomeAssistant, log_manager):
    """Test retrieving logs when system_log has no records yet."""
    await _setup_system_log(hass)

    filters = LogFilters()
    logs = await log_manager.get_core_logs(filters)

    assert logs == []


async def test_log_filtering_by_lines(hass: HomeAssistant, log_manager):
    """Test filtering logs by number of lines."""
    await _setup_system_log(hass)
    _TEST_LOGGER.warning("First warning")
    _TEST_LOGGER.error("First error")
    _TEST_LOGGER.critical("First critical")
    await hass.async_block_till_done()

    filters = LogFilters(lines=2)

    logs = await log_manager.get_core_logs(filters)

    assert len(logs) <= 2


async def test_log_filtering_by_level(hass: HomeAssistant, log_manager):
    """Test filtering logs by level."""
    await _setup_system_log(hass)
    await _seed_log_entries(hass)

    filters = LogFilters(level="ERROR")

    logs = await log_manager.get_core_logs(filters)

    assert len(logs) > 0
    assert all(log.level == "ERROR" for log in logs)


async def test_log_filtering_by_search(hass: HomeAssistant, log_manager):
    """Test filtering logs by search term."""
    await _setup_system_log(hass)
    await _seed_log_entries(hass)

    filters = LogFilters(search="light")

    logs = await log_manager.get_core_logs(filters)

    assert len(logs) > 0
    assert all(
        "light" in log.message.lower() or "light" in log.component.lower()
        for log in logs
    )


async def test_log_filtering_by_time_range(hass: HomeAssistant, log_manager):
    """Test filtering logs by time range."""
    await _setup_system_log(hass)
    await _seed_log_entries(hass)

    now = datetime.now()
    since = now - timedelta(hours=1)
    until = now + timedelta(hours=1)

    filters = LogFilters(since=since, until=until)

    logs = await log_manager.get_core_logs(filters)

    assert len(logs) > 0
    # All logs should be within the time range
    for log in logs:
        assert since <= log.timestamp <= until


async def test_log_entry_to_dict(hass: HomeAssistant):
    """Test LogEntry serialization to dictionary."""
    timestamp = datetime(2024, 2, 8, 10, 0, 0)
    entry = LogEntry(
        timestamp=timestamp,
        level="INFO",
        source="core",
        message="Test message",
        component="test.component",
    )

    result = entry.to_dict()

    assert result["timestamp"] == timestamp.isoformat()
    assert result["level"] == "INFO"
    assert result["source"] == "core"
    assert result["message"] == "Test message"
    assert result["component"] == "test.component"


async def test_log_ordering(hass: HomeAssistant, log_manager):
    """Test that logs are ordered by timestamp (newest first)."""
    await _setup_system_log(hass)
    for i in range(5):
        logging.getLogger(f"tests.test_log_manager.ordering.{i}").warning(
            "Warning %d", i
        )
    await hass.async_block_till_done()

    filters = LogFilters()

    logs = await log_manager.get_core_logs(filters)

    # Check that logs are in descending order by timestamp
    for i in range(len(logs) - 1):
        assert logs[i].timestamp >= logs[i + 1].timestamp


async def test_log_filters_offset_and_limit(hass: HomeAssistant, log_manager):
    """Test pagination with offset and limit."""
    await _setup_system_log(hass)
    for i in range(5):
        logging.getLogger(f"tests.test_log_manager.pagination.{i}").warning(
            "Warning %d", i
        )
    await hass.async_block_till_done()

    # Get all logs first
    all_filters = LogFilters(limit=100)
    all_logs = await log_manager.get_core_logs(all_filters)

    # Get first page
    page1_filters = LogFilters(offset=0, limit=2)
    page1_logs = await log_manager.get_core_logs(page1_filters)

    # Get second page
    page2_filters = LogFilters(offset=2, limit=2)
    page2_logs = await log_manager.get_core_logs(page2_filters)

    assert len(page1_logs) <= 2
    assert len(page2_logs) <= 2

    # Paginated pages can never return more entries than exist in total.
    assert len(page1_logs) + len(page2_logs) <= len(all_logs)

    # Pages should not overlap
    if len(page1_logs) > 0 and len(page2_logs) > 0:
        assert page1_logs[0].timestamp != page2_logs[0].timestamp
