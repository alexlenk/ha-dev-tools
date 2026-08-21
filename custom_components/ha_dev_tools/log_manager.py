"""Log manager for the HA Dev Tools."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from homeassistant.core import HomeAssistant

from .security import SecurityManager

_LOGGER = logging.getLogger(__name__)

# The system_log integration's own DOMAIN constant - hardcoded rather than
# imported (`from homeassistant.components.system_log import DOMAIN`) to
# avoid pulling in that submodule's own import chain here. The string is
# part of system_log's stable public surface already relied on elsewhere
# (the `system_log:` YAML config key, the frontend's `system_log/list`
# websocket command), so it isn't expected to change.
_SYSTEM_LOG_DOMAIN = "system_log"


class LogEntry:
    """Represents a single log entry."""

    def __init__(
        self,
        timestamp: datetime,
        level: str,
        source: str,
        message: str,
        component: str = "",
    ):
        """Initialize log entry."""
        self.timestamp = timestamp
        self.level = level
        self.source = source
        self.message = message
        self.component = component

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "level": self.level,
            "source": self.source,
            "message": self.message,
            "component": self.component,
        }


class LogFilters:
    """Log filtering parameters."""

    def __init__(
        self,
        lines: Optional[int] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        level: Optional[str] = None,
        search: Optional[str] = None,
        offset: int = 0,
        limit: int = 100,
    ):
        """Initialize log filters."""
        self.lines = lines
        self.since = since
        self.until = until
        self.level = level
        self.search = search
        self.offset = offset
        self.limit = limit


class LogManager:
    """Provides access to Home Assistant logs with filtering capabilities."""

    def __init__(self, hass: HomeAssistant, security_manager: SecurityManager) -> None:
        """Initialize the log manager."""
        self.hass = hass
        self.security_manager = security_manager

        _LOGGER.info("LogManager initialized")

    async def get_core_logs(self, filters: LogFilters) -> List[LogEntry]:
        """
        Retrieve Home Assistant core logs with filtering.

        Reads from the `system_log` integration's in-memory WARNING+ record
        buffer (`hass.data["system_log"].records`) - the same source that
        backs the native Settings -> System -> Logs page - rather than a
        static log file, which may not exist, may be rotated, or may not be
        the file the running instance is actually writing to. `system_log`
        is set up unconditionally during HA's startup (it's part of
        bootstrap's always-on `LOGGING_AND_HTTP_DEPS_INTEGRATIONS`), so its
        buffer is present on every real instance.

        Args:
            filters: Log filtering parameters

        Returns:
            List of log entries

        Raises:
            RuntimeError: If log retrieval fails
        """
        try:
            handler = self.hass.data.get(_SYSTEM_LOG_DOMAIN)
            if handler is None:
                _LOGGER.warning(
                    "system_log integration not loaded - no log buffer to read from"
                )
                return []

            log_entries = [
                self._to_log_entry(record) for record in handler.records.to_list()
            ]

            filtered_entries = self._apply_filters(log_entries, filters)

            _LOGGER.debug("Retrieved %d core log entries", len(filtered_entries))
            return filtered_entries

        except Exception as e:
            _LOGGER.error("Error retrieving core logs: %s", e)
            raise RuntimeError(f"Error retrieving logs: {str(e)}")

    @staticmethod
    def _to_log_entry(record: Dict[str, Any]) -> LogEntry:
        """
        Convert one system_log record dict into a LogEntry.

        system_log dedupes repeated identical log lines and keeps up to the
        last 5 distinct messages per (logger, source, root_cause) key - the
        newest one (record["message"][-1]) is what to surface here.
        """
        messages = record.get("message") or [""]
        return LogEntry(
            timestamp=datetime.fromtimestamp(record["timestamp"]),
            level=record["level"],
            source="core",
            message=messages[-1],
            component=record.get("name", ""),
        )

    def _apply_filters(
        self, entries: List[LogEntry], filters: LogFilters
    ) -> List[LogEntry]:
        """
        Apply filtering to log entries.

        Args:
            entries: List of log entries to filter
            filters: Filtering parameters

        Returns:
            Filtered list of log entries
        """
        filtered = entries

        # Filter by time range
        if filters.since:
            filtered = [e for e in filtered if e.timestamp >= filters.since]

        if filters.until:
            filtered = [e for e in filtered if e.timestamp <= filters.until]

        # Filter by log level
        if filters.level:
            level_upper = filters.level.upper()
            filtered = [e for e in filtered if e.level == level_upper]

        # Filter by search term
        if filters.search:
            search_lower = filters.search.lower()
            filtered = [
                e
                for e in filtered
                if search_lower in e.message.lower()
                or search_lower in e.component.lower()
            ]

        # Sort by timestamp (newest first)
        filtered.sort(key=lambda x: x.timestamp, reverse=True)

        # Apply lines limit (if specified, overrides offset/limit)
        if filters.lines:
            filtered = filtered[: filters.lines]
        else:
            # Apply offset and limit
            start_idx = filters.offset
            end_idx = start_idx + filters.limit
            filtered = filtered[start_idx:end_idx]

        return filtered
