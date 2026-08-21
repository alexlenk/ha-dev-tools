"""Property-based tests for LogManager log retrieval operations.

This module contains property-based tests that validate the correctness
of log retrieval operations in the HA Dev Tools.
"""

import asyncio
import os
import string
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Add the custom_components directory to the path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "custom_components")
)

# Mock homeassistant modules before importing our code
# Snapshot real sys.modules entries so they can be restored after this
# file's own imports below - without this, replacing sys.modules['homeassistant']
# etc. leaks into every test file collected afterward in the same pytest
# process (breaks anything needing the real homeassistant package, e.g.
# tests/test_llm_api.py's pytest-homeassistant-custom-component fixtures).
_ORIGINAL_HA_MODULES = {
    key: sys.modules.get(key)
    for key in [
        "homeassistant",
        "homeassistant.core",
        "homeassistant.config_entries",
        "homeassistant.auth",
        "homeassistant.auth.models",
        "homeassistant.components",
        "homeassistant.components.http",
        "homeassistant.const",
        "homeassistant.helpers",
        "homeassistant.helpers.typing",
    ]
}
mock_homeassistant = Mock()
mock_core = Mock()
mock_config_entries = Mock()
mock_auth = Mock()
mock_auth_models = Mock()
mock_components = Mock()
mock_http = Mock()
mock_const = Mock()
mock_helpers = Mock()
mock_helpers_typing = Mock()

sys.modules["homeassistant"] = mock_homeassistant
sys.modules["homeassistant.core"] = mock_core
sys.modules["homeassistant.config_entries"] = mock_config_entries
sys.modules["homeassistant.auth"] = mock_auth
sys.modules["homeassistant.auth.models"] = mock_auth_models
sys.modules["homeassistant.components"] = mock_components
sys.modules["homeassistant.components.http"] = mock_http
sys.modules["homeassistant.const"] = mock_const
sys.modules["homeassistant.helpers"] = mock_helpers
sys.modules["homeassistant.helpers.typing"] = mock_helpers_typing

# Mock the specific classes and constants we need
mock_config_entries.ConfigEntry = Mock()
mock_const.Platform = Mock()
mock_helpers_typing.ConfigType = dict
mock_core.HomeAssistant = Mock()
mock_auth_models.User = Mock()
mock_http.HomeAssistantView = Mock()


class MockHass:
    """Mock Home Assistant instance for testing."""

    def __init__(self, config_dir: str):
        self.config = Mock()
        self.config.config_dir = config_dir

    async def async_add_executor_job(self, func, *args):
        """Mock executor job - just run synchronously."""
        return func(*args)


# Now import our modules - deliberately placed after the mock-pollution
# setup above, not sloppy ordering (see the sys.modules restore below).
from custom_components.ha_dev_tools.log_manager import (  # noqa: E402
    LogEntry,
    LogFilters,
    LogManager,
)
from custom_components.ha_dev_tools.security import SecurityManager  # noqa: E402

# Restore the real sys.modules entries now that the module(s) under test
# have finished importing against the mocks above - contains the mock
# pollution to this file instead of leaking into later-collected tests.
for _key, _mod in _ORIGINAL_HA_MODULES.items():
    if _mod is not None:
        sys.modules[_key] = _mod
    else:
        sys.modules.pop(_key, None)


# Strategy for generating valid log levels
valid_log_levels = st.sampled_from(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])

# Strategy for generating valid log messages
valid_log_messages = st.text(
    alphabet=string.ascii_letters + string.digits + " .,!?-_", min_size=10, max_size=200
)

# Strategy for generating valid component names
valid_component_names = st.text(
    alphabet=string.ascii_lowercase + string.digits + "_.", min_size=3, max_size=30
).filter(lambda x: not x.startswith(".") and not x.endswith("."))


# Strategy for generating timestamps
def generate_timestamp():
    """Generate a timestamp within the last 7 days."""
    now = datetime.now()
    days_ago = st.integers(min_value=0, max_value=7)
    hours = st.integers(min_value=0, max_value=23)
    minutes = st.integers(min_value=0, max_value=59)
    seconds = st.integers(min_value=0, max_value=59)

    return st.builds(
        lambda d, h, m, s: now - timedelta(days=d, hours=h, minutes=m, seconds=s),
        days_ago,
        hours,
        minutes,
        seconds,
    )


valid_timestamps = generate_timestamp()


def create_log_file(log_dir: Path, log_entries: list) -> None:
    """Create a log file with the given entries."""
    log_file = log_dir / "home-assistant.log"

    with open(log_file, "w", encoding="utf-8") as f:
        for entry in log_entries:
            # Format: YYYY-MM-DD HH:MM:SS LEVEL component: message
            timestamp_str = entry["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
            component_part = f"{entry['component']}: " if entry["component"] else ""
            log_line = (
                f"{timestamp_str} {entry['level']} {component_part}{entry['message']}\n"
            )
            f.write(log_line)


@given(
    log_entries=st.lists(
        st.fixed_dictionaries(
            {
                "timestamp": valid_timestamps,
                "level": valid_log_levels,
                "component": valid_component_names,
                "message": valid_log_messages,
            }
        ),
        min_size=1,
        max_size=50,
    )
)
@settings(suppress_health_check=[HealthCheck.filter_too_much], deadline=5000)
def test_log_retrieval_completeness_property(log_entries):
    """
    Feature: ha-config-manager-integration, Property 12: Log Retrieval Completeness

    For any log source (core, supervisor, addons), log requests should return
    properly formatted log entries from the specified source.
    **Validates: Requirements 10.1**
    """

    async def run_test():
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create mock hass and managers
            mock_hass = MockHass(temp_dir)
            security_manager = SecurityManager(mock_hass)
            log_manager = LogManager(mock_hass, security_manager)

            # Create log file with entries
            create_log_file(Path(temp_dir), log_entries)

            # Retrieve logs without filters
            filters = LogFilters()
            retrieved_logs = await log_manager.get_core_logs(filters)

            # Should succeed
            assert retrieved_logs is not None, "Retrieved logs should not be None"

            # Should return log entries
            assert len(retrieved_logs) > 0, "Should retrieve at least one log entry"

            # All retrieved entries should be properly formatted LogEntry objects
            for log_entry in retrieved_logs:
                assert isinstance(
                    log_entry, LogEntry
                ), f"Entry should be LogEntry, got {type(log_entry)}"
                assert hasattr(log_entry, "timestamp"), "Entry should have timestamp"
                assert hasattr(log_entry, "level"), "Entry should have level"
                assert hasattr(log_entry, "source"), "Entry should have source"
                assert hasattr(log_entry, "message"), "Entry should have message"
                assert hasattr(log_entry, "component"), "Entry should have component"

                # Validate timestamp is a datetime object
                assert isinstance(
                    log_entry.timestamp, datetime
                ), f"Timestamp should be datetime, got {type(log_entry.timestamp)}"

                # Validate level is a string
                assert isinstance(
                    log_entry.level, str
                ), f"Level should be string, got {type(log_entry.level)}"

                # Validate source is 'core' for core logs
                assert (
                    log_entry.source == "core"
                ), f"Source should be 'core', got {log_entry.source}"

                # Validate message is a string
                assert isinstance(
                    log_entry.message, str
                ), f"Message should be string, got {type(log_entry.message)}"

                # Validate component is a string
                assert isinstance(
                    log_entry.component, str
                ), f"Component should be string, got {type(log_entry.component)}"

            # Verify we can convert to dict (for JSON serialization)
            for log_entry in retrieved_logs:
                entry_dict = log_entry.to_dict()
                assert isinstance(
                    entry_dict, dict
                ), "to_dict() should return a dictionary"
                assert "timestamp" in entry_dict, "Dict should contain timestamp"
                assert "level" in entry_dict, "Dict should contain level"
                assert "source" in entry_dict, "Dict should contain source"
                assert "message" in entry_dict, "Dict should contain message"
                assert "component" in entry_dict, "Dict should contain component"

    # Run the async test
    asyncio.run(run_test())


@given(
    log_entries=st.lists(
        st.fixed_dictionaries(
            {
                "timestamp": valid_timestamps,
                "level": valid_log_levels,
                "component": valid_component_names,
                "message": valid_log_messages,
            }
        ),
        min_size=5,
        max_size=100,
    ),
    lines_limit=st.integers(min_value=1, max_value=20),
)
@settings(suppress_health_check=[HealthCheck.filter_too_much], deadline=5000)
def test_log_filtering_lines_property(log_entries, lines_limit):
    """
    Feature: ha-config-manager-integration, Property: Log Filtering by Lines

    For any log entries and lines limit, the returned logs should not exceed
    the specified number of lines.
    **Validates: Requirements 11.1**
    """

    async def run_test():
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create mock hass and managers
            mock_hass = MockHass(temp_dir)
            security_manager = SecurityManager(mock_hass)
            log_manager = LogManager(mock_hass, security_manager)

            # Create log file with entries
            create_log_file(Path(temp_dir), log_entries)

            # Retrieve logs with lines filter
            filters = LogFilters(lines=lines_limit)
            retrieved_logs = await log_manager.get_core_logs(filters)

            # Should succeed
            assert retrieved_logs is not None, "Retrieved logs should not be None"

            # Should not exceed the lines limit
            assert (
                len(retrieved_logs) <= lines_limit
            ), f"Retrieved {len(retrieved_logs)} logs, but limit was {lines_limit}"

    # Run the async test
    asyncio.run(run_test())


@given(
    log_entries=st.lists(
        st.fixed_dictionaries(
            {
                "timestamp": valid_timestamps,
                "level": valid_log_levels,
                "component": valid_component_names,
                "message": valid_log_messages,
            }
        ),
        min_size=10,
        max_size=50,
    ),
    filter_level=valid_log_levels,
)
@settings(suppress_health_check=[HealthCheck.filter_too_much], deadline=5000)
def test_log_filtering_level_property(log_entries, filter_level):
    """
    Feature: ha-config-manager-integration, Property: Log Filtering by Level

    For any log entries and level filter, all returned logs should match
    the specified log level.
    **Validates: Requirements 11.4**
    """

    async def run_test():
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create mock hass and managers
            mock_hass = MockHass(temp_dir)
            security_manager = SecurityManager(mock_hass)
            log_manager = LogManager(mock_hass, security_manager)

            # Create log file with entries
            create_log_file(Path(temp_dir), log_entries)

            # Retrieve logs with level filter
            filters = LogFilters(level=filter_level)
            retrieved_logs = await log_manager.get_core_logs(filters)

            # Should succeed
            assert retrieved_logs is not None, "Retrieved logs should not be None"

            # All returned logs should match the filter level
            for log_entry in retrieved_logs:
                assert (
                    log_entry.level == filter_level.upper()
                ), f"Log entry level {log_entry.level} doesn't match filter {filter_level}"

    # Run the async test
    asyncio.run(run_test())


@given(
    log_entries=st.lists(
        st.fixed_dictionaries(
            {
                "timestamp": valid_timestamps,
                "level": valid_log_levels,
                "component": valid_component_names,
                "message": valid_log_messages,
            }
        ),
        min_size=10,
        max_size=50,
    ),
    search_term=st.text(alphabet=string.ascii_lowercase, min_size=3, max_size=10),
)
@settings(suppress_health_check=[HealthCheck.filter_too_much], deadline=5000)
def test_log_filtering_search_property(log_entries, search_term):
    """
    Feature: ha-config-manager-integration, Property: Log Filtering by Search

    For any log entries and search term, all returned logs should contain
    the search term in either the message or component.
    **Validates: Requirements 11.5**
    """

    async def run_test():
        with tempfile.TemporaryDirectory() as temp_dir:
            # Ensure at least one log entry contains the search term
            # Add the search term to at least one entry
            if log_entries:
                log_entries[0]["message"] = f"Test message with {search_term} included"

            # Create mock hass and managers
            mock_hass = MockHass(temp_dir)
            security_manager = SecurityManager(mock_hass)
            log_manager = LogManager(mock_hass, security_manager)

            # Create log file with entries
            create_log_file(Path(temp_dir), log_entries)

            # Retrieve logs with search filter
            filters = LogFilters(search=search_term)
            retrieved_logs = await log_manager.get_core_logs(filters)

            # Should succeed
            assert retrieved_logs is not None, "Retrieved logs should not be None"

            # All returned logs should contain the search term
            search_lower = search_term.lower()
            for log_entry in retrieved_logs:
                message_match = search_lower in log_entry.message.lower()
                component_match = search_lower in log_entry.component.lower()
                assert (
                    message_match or component_match
                ), f"Log entry doesn't contain search term '{search_term}': {log_entry.message}"

    # Run the async test
    asyncio.run(run_test())


@given(
    log_entries=st.lists(
        st.fixed_dictionaries(
            {
                "timestamp": valid_timestamps,
                "level": valid_log_levels,
                "component": valid_component_names,
                "message": valid_log_messages,
            }
        ),
        min_size=20,
        max_size=100,
    )
)
@settings(suppress_health_check=[HealthCheck.filter_too_much], deadline=5000)
def test_log_time_ordering_property(log_entries):
    """
    Feature: ha-config-manager-integration, Property: Log Time Ordering

    For any log entries, retrieved logs should be ordered by timestamp
    (newest first) when no specific ordering is requested.
    **Validates: Requirements 10.1**
    """

    async def run_test():
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create mock hass and managers
            mock_hass = MockHass(temp_dir)
            security_manager = SecurityManager(mock_hass)
            log_manager = LogManager(mock_hass, security_manager)

            # Create log file with entries
            create_log_file(Path(temp_dir), log_entries)

            # Retrieve logs without filters
            filters = LogFilters()
            retrieved_logs = await log_manager.get_core_logs(filters)

            # Should succeed
            assert retrieved_logs is not None, "Retrieved logs should not be None"

            # Should have at least 2 entries to check ordering
            if len(retrieved_logs) >= 2:
                # Verify logs are ordered by timestamp (newest first)
                for i in range(len(retrieved_logs) - 1):
                    current_time = retrieved_logs[i].timestamp
                    next_time = retrieved_logs[i + 1].timestamp
                    assert (
                        current_time >= next_time
                    ), f"Logs not properly ordered: {current_time} should be >= {next_time}"

    # Run the async test
    asyncio.run(run_test())


def test_empty_log_file_property():
    """
    Feature: ha-config-manager-integration, Property: Empty Log File Handling

    When the log file is empty or doesn't exist, the system should handle
    it gracefully and return an empty list without errors.
    **Validates: Requirements 10.1**
    """

    async def run_test():
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create mock hass and managers
            mock_hass = MockHass(temp_dir)
            security_manager = SecurityManager(mock_hass)
            log_manager = LogManager(mock_hass, security_manager)

            # Don't create a log file - test missing file handling
            filters = LogFilters()
            retrieved_logs = await log_manager.get_core_logs(filters)

            # Should succeed with empty list
            assert retrieved_logs is not None, "Retrieved logs should not be None"
            assert (
                len(retrieved_logs) == 0
            ), "Should return empty list for missing log file"

            # Now test with empty log file
            log_file = Path(temp_dir) / "home-assistant.log"
            log_file.touch()

            retrieved_logs = await log_manager.get_core_logs(filters)

            # Should succeed with empty list
            assert retrieved_logs is not None, "Retrieved logs should not be None"
            assert (
                len(retrieved_logs) == 0
            ), "Should return empty list for empty log file"

    # Run the async test
    asyncio.run(run_test())
