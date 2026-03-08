"""Test LogManager functionality with Home Assistant fixtures."""
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools.log_manager import (
    LogManager,
    LogEntry,
    LogFilters,
)
from custom_components.ha_dev_tools.security import SecurityManager


@pytest.fixture
def security_manager(hass: HomeAssistant):
    """Create a SecurityManager instance for testing."""
    return SecurityManager(hass)


@pytest.fixture
def log_manager(hass: HomeAssistant, security_manager):
    """Create a LogManager instance for testing."""
    return LogManager(hass, security_manager)


@pytest.fixture
def mock_log_file(hass: HomeAssistant):
    """Create a mock log file for testing."""
    log_content = """2024-02-08 10:00:00 INFO homeassistant.core: Starting Home Assistant
2024-02-08 10:00:01 DEBUG homeassistant.loader: Loading integration ha_config_manager
2024-02-08 10:00:02 WARNING homeassistant.components.sensor: Sensor unavailable
2024-02-08 10:00:03 ERROR homeassistant.components.light: Failed to turn on light
2024-02-08 10:00:04 INFO homeassistant.setup: Setup completed
"""
    
    # Use the hass config directory
    log_file = Path(hass.config.config_dir) / "home-assistant.log"
    log_file.write_text(log_content)
    
    return log_file


async def test_get_core_logs_basic(hass: HomeAssistant, log_manager, mock_log_file):
    """Test retrieving core logs without filters."""
    filters = LogFilters()
    
    logs = await log_manager.get_core_logs(filters)
    
    assert len(logs) > 0
    assert all(isinstance(log, LogEntry) for log in logs)


async def test_get_core_logs_empty_file(hass: HomeAssistant, log_manager):
    """Test retrieving logs from empty file."""
    # Create empty log file
    log_file = Path(hass.config.config_dir) / "home-assistant.log"
    log_file.write_text("")
    
    filters = LogFilters()
    logs = await log_manager.get_core_logs(filters)
    
    assert logs == []


async def test_get_core_logs_missing_file(hass: HomeAssistant, log_manager):
    """Test retrieving logs when file doesn't exist."""
    filters = LogFilters()
    logs = await log_manager.get_core_logs(filters)
    
    assert logs == []


async def test_log_filtering_by_lines(hass: HomeAssistant, log_manager, mock_log_file):
    """Test filtering logs by number of lines."""
    filters = LogFilters(lines=2)
    
    logs = await log_manager.get_core_logs(filters)
    
    assert len(logs) <= 2


async def test_log_filtering_by_level(hass: HomeAssistant, log_manager, mock_log_file):
    """Test filtering logs by level."""
    filters = LogFilters(level="ERROR")
    
    logs = await log_manager.get_core_logs(filters)
    
    assert all(log.level == "ERROR" for log in logs)


async def test_log_filtering_by_search(hass: HomeAssistant, log_manager, mock_log_file):
    """Test filtering logs by search term."""
    filters = LogFilters(search="light")
    
    logs = await log_manager.get_core_logs(filters)
    
    assert all("light" in log.message.lower() or "light" in log.component.lower() for log in logs)


async def test_log_filtering_by_time_range(hass: HomeAssistant, log_manager, mock_log_file):
    """Test filtering logs by time range."""
    now = datetime.now()
    since = now - timedelta(hours=1)
    until = now + timedelta(hours=1)
    
    filters = LogFilters(since=since, until=until)
    
    logs = await log_manager.get_core_logs(filters)
    
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
        component="test.component"
    )
    
    result = entry.to_dict()
    
    assert result["timestamp"] == timestamp.isoformat()
    assert result["level"] == "INFO"
    assert result["source"] == "core"
    assert result["message"] == "Test message"
    assert result["component"] == "test.component"


async def test_log_ordering(hass: HomeAssistant, log_manager, mock_log_file):
    """Test that logs are ordered by timestamp (newest first)."""
    filters = LogFilters()
    
    logs = await log_manager.get_core_logs(filters)
    
    # Check that logs are in descending order by timestamp
    for i in range(len(logs) - 1):
        assert logs[i].timestamp >= logs[i + 1].timestamp


async def test_log_filters_offset_and_limit(hass: HomeAssistant, log_manager, mock_log_file):
    """Test pagination with offset and limit."""
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
    
    # Pages should not overlap
    if len(page1_logs) > 0 and len(page2_logs) > 0:
        assert page1_logs[0].timestamp != page2_logs[0].timestamp
