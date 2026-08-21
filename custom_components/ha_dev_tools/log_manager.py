"""Log manager for the HA Dev Tools."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from homeassistant.core import HomeAssistant

from .security import SecurityManager

_LOGGER = logging.getLogger(__name__)


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
        self.config_dir = Path(hass.config.config_dir)

        _LOGGER.info("LogManager initialized")

    async def get_core_logs(self, filters: LogFilters) -> List[LogEntry]:
        """
        Retrieve Home Assistant core logs with filtering.

        Args:
            filters: Log filtering parameters

        Returns:
            List of log entries

        Raises:
            PermissionError: If access is denied
            RuntimeError: If log retrieval fails
        """
        try:
            log_file_path = self.config_dir / "home-assistant.log"

            # Check if log file exists
            if not log_file_path.exists():
                _LOGGER.warning("Core log file not found: %s", log_file_path)
                return []

            # Read log file
            log_content = await self.hass.async_add_executor_job(
                self._read_log_file_sync, log_file_path
            )

            # Parse log entries
            log_entries = self._parse_log_content(log_content, "core")

            # Apply filters
            filtered_entries = self._apply_filters(log_entries, filters)

            _LOGGER.debug("Retrieved %d core log entries", len(filtered_entries))
            return filtered_entries

        except PermissionError:
            _LOGGER.error("Permission denied reading core logs")
            raise PermissionError("Permission denied reading core logs")
        except Exception as e:
            _LOGGER.error("Error retrieving core logs: %s", e)
            raise RuntimeError(f"Error retrieving logs: {str(e)}")

    def _read_log_file_sync(self, log_file_path: Path) -> str:
        """
        Synchronous log file reading helper.

        Args:
            log_file_path: Path to log file

        Returns:
            Log file contents
        """
        with open(log_file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    def _parse_log_content(self, content: str, source: str) -> List[LogEntry]:
        """
        Parse log content into LogEntry objects.

        Args:
            content: Raw log content
            source: Log source identifier

        Returns:
            List of parsed log entries
        """
        entries = []
        lines = content.strip().split("\n")

        for line in lines:
            if not line.strip():
                continue

            try:
                # Basic log parsing - this is a simplified version
                # Real implementation would need more sophisticated parsing
                # for Home Assistant's log format

                # Try to extract timestamp, level, and message
                parts = line.split(" ", 3)
                if len(parts) >= 4:
                    # Simplified parsing - assumes "YYYY-MM-DD HH:MM:SS LEVEL component: message"
                    date_part = parts[0]
                    time_part = parts[1]
                    level = parts[2]
                    message = parts[3] if len(parts) > 3 else ""

                    try:
                        timestamp = datetime.fromisoformat(f"{date_part} {time_part}")
                    except ValueError:
                        # If timestamp parsing fails, use current time
                        timestamp = datetime.now()

                    # Extract component if present
                    component = ""
                    if ":" in message:
                        component_part, message = message.split(":", 1)
                        component = component_part.strip()
                        message = message.strip()

                    entry = LogEntry(
                        timestamp=timestamp,
                        level=level.upper(),
                        source=source,
                        message=message,
                        component=component,
                    )
                    entries.append(entry)
                else:
                    # If parsing fails, create a basic entry
                    entry = LogEntry(
                        timestamp=datetime.now(),
                        level="INFO",
                        source=source,
                        message=line,
                        component="",
                    )
                    entries.append(entry)

            except Exception as e:
                _LOGGER.debug("Failed to parse log line: %s - %s", line, e)
                # Create a basic entry for unparseable lines
                entry = LogEntry(
                    timestamp=datetime.now(),
                    level="INFO",
                    source=source,
                    message=line,
                    component="",
                )
                entries.append(entry)

        return entries

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
