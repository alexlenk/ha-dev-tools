"""Security manager for the Home Assistant Management Integration.

This module provides comprehensive security controls for file access validation,
including support for allowlist/denylist modes, glob pattern matching, and
read-only vs read-write access control.

Key Features:
    - Strict allowlist mode (deny by default)
    - Glob pattern support (*, ?, **) for flexible path matching
    - Read-only vs read-write access distinction
    - Path traversal protection
    - Sensitive file protection (auth, credentials, system files)
    - Operation-specific validation (read vs write)

Example Configuration:
    ```yaml
    ha_dev_tools:
      security:
        # Read-only paths
        read_paths:
          - "/config/.storage/lovelace*"  # All dashboards
          - "/config/.storage/input_*"    # All input helpers
          - "/config/configuration.yaml"
        
        # Read-write paths
        write_paths:
          - "/config/packages/generated/*.yaml"
        
        # Always denied
        denied_paths:
          - "/config/.storage/auth*"
          - "/config/secrets.yaml"
    ```

Security Model:
    1. Denied paths are checked first (highest priority)
    2. Write operations require explicit write_paths permission
    3. Read operations check allowlist in strict mode
    4. Default deny - only explicitly permitted paths are accessible

Path Matching Rules:
    - Exact match: "/config/configuration.yaml"
    - Directory match: "/config/packages/*"
    - Glob patterns: "/config/.storage/lovelace*" matches all dashboards
    - Recursive: "/config/packages/**/*.yaml" matches all nested YAML files
"""
from __future__ import annotations

import fnmatch
import logging
import os
from pathlib import Path
from typing import Any, Optional, Tuple

from homeassistant.core import HomeAssistant
from homeassistant.auth.models import User
from homeassistant.components.http import HomeAssistantView

from .const import (
    ALLOWED_DIRECTORIES,
    ALLOWED_EXTENSIONS,
    DEFAULT_ALLOWLIST,
    DEFAULT_BLACKLIST,
    DEFAULT_DENYLIST,
    DEFAULT_READ_ONLY_PATHS,
    DEFAULT_WRITE_PATHS,
    ERROR_BLACKLISTED_FILE,
    ERROR_INVALID_PATH,
    ERROR_PERMISSION_DENIED,
    ERROR_WRITE_NOT_PERMITTED,
    OPERATION_READ,
    OPERATION_WRITE,
    RECOMMENDED_SAFE_STORAGE_PATTERNS,
)

_LOGGER = logging.getLogger(__name__)


class SecurityManager:
    """Manages security controls and file access validation.
    
    The SecurityManager enforces security policies for file access, supporting
    both allowlist and denylist modes with glob pattern matching and operation-specific
    permissions (read vs write).
    
    Security Model:
        The system operates in strict allowlist mode where only explicitly permitted
        paths are accessible. All access is denied by default unless explicitly allowed.
    
    Configuration Parameters:
        read_paths (list): Paths that allow read-only operations
        write_paths (list): Paths that allow both read and write operations
        denied_paths (list): Paths that are always blocked (highest priority)
    
    Path Matching:
        The manager supports three types of path matching:
        1. Exact match: "/config/configuration.yaml"
        2. Directory match: "/config/packages/" matches all files in packages/
        3. Glob patterns: 
           - "*" matches any characters: "/config/.storage/lovelace*"
           - "?" matches single character: "config.yam?"
           - "**" matches directories recursively: "/config/packages/**/*.yaml"
    
    Operation Types:
        - OPERATION_READ: Read-only operations (view file content)
        - OPERATION_WRITE: Write operations (create, modify, delete files)
    
    Access Control Rules:
        1. Denied paths always block access (highest priority)
        2. Write operations require explicit write_paths permission
        3. Read operations check read_paths or write_paths
        4. Write paths implicitly grant read access
        5. Default deny in allowlist mode
    
    Example Usage:
        ```python
        # Initialize with configuration
        config = {
            "read_paths": ["/config/configuration.yaml"],
            "write_paths": ["/config/packages/*.yaml"],
            "denied_paths": ["/config/secrets.yaml"],
            "mode": "allowlist"
        }
        security_manager = SecurityManager(hass, config)
        
        # Validate read operation
        is_valid, error = security_manager.validate_file_path(
            "/config/configuration.yaml",
            operation=OPERATION_READ
        )
        
        # Validate write operation
        is_valid, error = security_manager.validate_file_path(
            "/config/packages/lights.yaml",
            operation=OPERATION_WRITE
        )
        
        # Check specific permissions
        if security_manager.is_readable("/config/configuration.yaml"):
            # File can be read
            pass
        
        if security_manager.is_writable("/config/packages/lights.yaml"):
            # File can be written
            pass
        ```
    
    Attributes:
        hass: Home Assistant instance
        read_paths: Set of read-only paths/patterns
        write_paths: Set of read-write paths/patterns
        allowlist: Unified set of allowed paths (read + write)
        denylist: Set of denied paths/patterns
        allowed_extensions: Set of allowed file extensions
        allowed_directories: Set of allowed directories
    """

    def __init__(self, hass: HomeAssistant, config: Optional[dict[str, Any]] = None) -> None:
        """Initialize the security manager with optional configuration.
        
        Sets up security controls based on provided configuration or defaults.
        Validates all configured paths and logs the security configuration.
        
        The system operates in strict allowlist mode - only explicitly permitted
        paths are accessible. All access is denied by default.
        
        Args:
            hass: Home Assistant instance for accessing config directory and logging
            config: Security configuration dictionary with the following optional keys:
                - read_paths (list): Paths allowing read-only operations
                - write_paths (list): Paths allowing read and write operations
                - denied_paths (list): Paths that are always blocked
                - allowed_paths (list): Legacy - paths to allow (deprecated)
                - allowed_storage_files (list): Legacy - storage files to allow (deprecated)
        
        Configuration Example:
            ```python
            config = {
                "read_paths": [
                    "/config/.storage/lovelace*",  # All dashboards
                    "/config/configuration.yaml"
                ],
                "write_paths": [
                    "/config/packages/generated/*.yaml"
                ],
                "denied_paths": [
                    "/config/.storage/auth*",
                    "/config/secrets.yaml"
                ],
                "mode": "allowlist"
            }
            ```
        
        Raises:
            No exceptions - invalid paths are logged and excluded from security lists
        
        Side Effects:
            - Logs security configuration summary
            - Logs warnings for invalid paths
            - Initializes allowlist, denylist, read_paths, and write_paths
        """
        self.hass = hass
        self._config = config or {}
        
        # Initialize denylist with defaults + configured denied_paths
        self.denylist = self._build_denylist()
        self.blacklist = self.denylist  # Alias for backward compatibility
        
        # Initialize read_paths and write_paths sets
        self.read_paths = self._build_read_paths()
        self.write_paths = self._build_write_paths()
        
        # Initialize allowlist using _build_allowlist method (merges read/write paths)
        self.allowlist = self._build_allowlist()
        
        self.allowed_extensions = ALLOWED_EXTENSIONS
        self.allowed_directories = ALLOWED_DIRECTORIES
        
        # Validate all configured paths
        self._validate_configured_paths()
        
        # Log security configuration
        self._log_security_config()

    def _build_read_paths(self) -> set[str]:
        """Build the read-only paths from configuration and defaults.
        
        Extracts read_paths from config. If no configuration provided,
        uses DEFAULT_READ_ONLY_PATHS which includes safe configuration files
        and storage files.
        
        Read-only paths allow viewing file content but prevent modifications.
        This is the recommended setting for most configuration files.
        
        Returns:
            Set of read-only paths/patterns
        
        Example:
            ```python
            # With configuration
            config = {"read_paths": ["/config/configuration.yaml"]}
            read_paths = self._build_read_paths()
            # Returns: {"/config/configuration.yaml"}
            
            # Without configuration (uses defaults)
            config = {}
            read_paths = self._build_read_paths()
            # Returns: {"/config/.storage/lovelace*", "/config/configuration.yaml", ...}
            ```
        """
        read_paths = set()
        
        # Add configured read paths
        configured_read = self._config.get("read_paths", [])
        read_paths.update(configured_read)
        
        # If no configuration provided, use safe defaults
        if not configured_read and not self._config.get("write_paths"):
            read_paths.update(DEFAULT_READ_ONLY_PATHS)
        
        return read_paths
    
    def _build_write_paths(self) -> set[str]:
        """Build the read-write paths from configuration.
        
        Extracts write_paths from config. Empty by default for security.
        Write paths allow both reading and writing files.
        
        Write access should only be granted when necessary, as it allows
        file creation, modification, and deletion.
        
        Returns:
            Set of read-write paths/patterns
        
        Example:
            ```python
            # With write paths configured
            config = {"write_paths": ["/config/packages/generated/*.yaml"]}
            write_paths = self._build_write_paths()
            # Returns: {"/config/packages/generated/*.yaml"}
            
            # Without configuration (empty by default)
            config = {}
            write_paths = self._build_write_paths()
            # Returns: set()
            ```
        """
        write_paths = set()
        
        # Add configured write paths
        configured_write = self._config.get("write_paths", [])
        write_paths.update(configured_write)
        
        return write_paths

    def _build_allowlist(self) -> set[str]:
        """
        Build the allowlist from configuration.
        
        Merges read_paths and write_paths into unified allowlist.
        Maintains backward compatibility with allowed_paths.
        Adds configured allowed_paths to allowlist (paths can include glob patterns).
        If allowlist mode but empty, adds RECOMMENDED_SAFE_STORAGE_PATTERNS.
        
        Returns:
            Set of allowlist paths/patterns
        """
        allowlist = set()
        
        # Add configured allowed_paths (legacy - can include glob patterns)
        configured_paths = self._config.get("allowed_paths", [])
        allowlist.update(configured_paths)
        
        # Add legacy allowed_storage_files with .storage/ prefix
        storage_files = self._config.get("allowed_storage_files", [])
        for storage_file in storage_files:
            allowlist.add(f".storage/{storage_file}")
        
        # Merge read_paths and write_paths into unified allowlist
        allowlist.update(self.read_paths)
        allowlist.update(self.write_paths)
        
        # If empty configuration, use recommended safe storage patterns
        if not allowlist:
            allowlist.update(RECOMMENDED_SAFE_STORAGE_PATTERNS)
            _LOGGER.info("Using recommended safe storage patterns (no paths configured)")
        
        # If no configuration at all, use DEFAULT_ALLOWLIST for backward compatibility
        if not configured_paths and not storage_files and not self.read_paths and not self.write_paths:
            if DEFAULT_ALLOWLIST:
                allowlist.update(DEFAULT_ALLOWLIST)
        
        return allowlist

    def _build_denylist(self) -> set[str]:
        """
        Build the denylist from configuration.
        
        Starts with DEFAULT_DENYLIST, adds configured denied_paths (can include glob patterns),
        and ensures sensitive storage files are always included.
        
        Returns:
            Set of denylist paths/patterns
        """
        denylist = set(DEFAULT_DENYLIST)
        
        # Add configured denied_paths (can include glob patterns)
        configured_denied = self._config.get("denied_paths", [])
        denylist.update(configured_denied)
        
        # Ensure sensitive storage files are always included (defense in depth)
        # These patterns are already in DEFAULT_DENYLIST, but we ensure they're present
        sensitive_patterns = [
            ".storage/auth",
            ".storage/auth*",
            ".storage/core.*",
            ".storage/onboarding",
            ".storage/hassio",
        ]
        denylist.update(sensitive_patterns)
        
        return denylist

    def _validate_configured_paths(self) -> None:
        """
        Validate all configured paths for security issues.
        
        Validates allowed_paths for path traversal (..) and absolute paths (/).
        Logs warnings for invalid paths and removes them from allowlist.
        """
        invalid_paths = []
        
        # Validate allowed_paths
        for path in self._config.get("allowed_paths", []):
            if not self._is_valid_path_config(path):
                invalid_paths.append(path)
                _LOGGER.warning(
                    "Invalid path in allowed_paths: %s (contains path traversal or absolute path)",
                    path
                )
        
        # Validate storage files
        for storage_file in self._config.get("allowed_storage_files", []):
            if "/" in storage_file or "\\" in storage_file:
                invalid_paths.append(storage_file)
                _LOGGER.warning(
                    "Invalid storage file name: %s (contains directory separators)",
                    storage_file
                )
        
        # Validate denied_paths
        for path in self._config.get("denied_paths", []):
            if not self._is_valid_path_config(path):
                _LOGGER.warning(
                    "Invalid path in denied_paths: %s (contains path traversal or absolute path)",
                    path
                )
        
        # Remove invalid paths from allowlist
        for invalid_path in invalid_paths:
            self.allowlist.discard(invalid_path)
            self.allowlist.discard(f".storage/{invalid_path}")

    def _is_valid_path_config(self, path: str) -> bool:
        """
        Check if a configured path is valid (no traversal, must start with /config/ or /addon_configs/).
        
        Args:
            path: The path to validate
            
        Returns:
            True if path is valid, False otherwise
        """
        # Check for path traversal (..)
        # Note: We allow ".." in glob patterns if needed, but for now we're strict
        if ".." in path:
            return False
        
        # Absolute paths must start with /config/ or /addon_configs/
        if path.startswith("/"):
            if not (path.startswith("/config/") or path.startswith("/addon_configs/")):
                return False
        
        return True

    def _log_security_config(self) -> None:
        """
        Log the active security configuration.
        
        Logs count of read-only paths, read-write paths, and denied paths.
        Also logs when using default safe storage patterns.
        """
        _LOGGER.info(
            "SecurityManager initialized in strict allowlist mode: "
            "%d read-only paths, %d read-write paths, %d denied paths",
            len(self.read_paths),
            len(self.write_paths),
            len(self.denylist)
        )
        
        # Log when using default safe storage files
        configured_paths = self._config.get("allowed_paths", [])
        storage_files = self._config.get("allowed_storage_files", [])
        configured_read = self._config.get("read_paths", [])
        configured_write = self._config.get("write_paths", [])
        
        if not configured_paths and not storage_files and not configured_read and not configured_write:
            _LOGGER.info(
                "Using default safe storage patterns: %d patterns",
                len(RECOMMENDED_SAFE_STORAGE_PATTERNS)
            )
        
        if not configured_read and not configured_write:
            _LOGGER.info(
                "Using default read-only paths: %d paths",
                len(DEFAULT_READ_ONLY_PATHS)
            )

    def _matches_glob_pattern(self, path: str, pattern: str) -> bool:
        """Check if path matches a glob pattern.
        
        Uses fnmatch.fnmatch for glob matching with support for wildcards.
        Handles both absolute paths (starting with /) and relative paths.
        For patterns without leading /, checks if the pattern matches the path
        or any suffix with /config/ or /addon_configs/ prefix.
        
        Supported Wildcards:
            - * : Matches any characters (including none)
            - ? : Matches exactly one character
            - [seq] : Matches any character in seq
            - [!seq] : Matches any character not in seq
        
        Args:
            path: The file path to check (normalized)
            pattern: The glob pattern to match against
        
        Returns:
            True if path matches the pattern, False otherwise
        
        Examples:
            ```python
            # Asterisk wildcard - matches any characters
            _matches_glob_pattern(
                "/config/.storage/lovelace",
                ".storage/lovelace*"
            )
            # Returns: True
            
            _matches_glob_pattern(
                "/config/.storage/lovelace.dashboard_main",
                ".storage/lovelace*"
            )
            # Returns: True
            
            # Question mark wildcard - matches single character
            _matches_glob_pattern(
                "/config/config.yaml",
                "config.yam?"
            )
            # Returns: True
            
            _matches_glob_pattern(
                "/config/config.yml",
                "config.yam?"
            )
            # Returns: True
            
            # Character class
            _matches_glob_pattern(
                "/config/backup_1.yaml",
                "backup_[0-9].yaml"
            )
            # Returns: True
            
            # Recursive pattern (handled by fnmatch)
            _matches_glob_pattern(
                "/config/packages/lights/bedroom.yaml",
                "/config/packages/**/*.yaml"
            )
            # Note: ** requires special handling, not directly supported by fnmatch
            
            # Pattern without leading / (checks with prefix)
            _matches_glob_pattern(
                "/config/.storage/auth",
                ".storage/auth*"
            )
            # Returns: True (matches with /config/ prefix)
            ```
        
        Note:
            fnmatch has no native concept of "zero or more directories" for
            `**` - `fnmatch.fnmatch("packages/x.yaml", "packages/**/*.yaml")`
            is False, because the pattern's literal `/` between the two `*`
            groups requires an intermediate path segment to actually be
            present. That silently broke the documented recommended pattern
            `packages/**/*.yaml` for files placed directly in `packages/`
            (e.g. `packages/emhas.yaml`) rather than in a subdirectory - found
            via a real test against that exact layout. `_fnmatch_globstar`
            below also tries the pattern with `**/` collapsed to nothing, so
            `**` behaves as "zero or more directories" as documented/intended.
        """
        return (
            self._fnmatch_globstar(path, pattern)
            or (
                not pattern.startswith("/")
                and (
                    self._fnmatch_globstar(path, f"/config/{pattern}")
                    or self._fnmatch_globstar(path, f"/addon_configs/{pattern}")
                )
            )
        )

    @staticmethod
    def _fnmatch_globstar(path: str, pattern: str) -> bool:
        """fnmatch.fnmatch, but `**` also matches zero intermediate directories."""
        if fnmatch.fnmatch(path, pattern):
            return True
        if "**/" in pattern:
            return fnmatch.fnmatch(path, pattern.replace("**/", ""))
        if "/**" in pattern:
            return fnmatch.fnmatch(path, pattern.replace("/**", ""))
        return False

    def validate_file_path(self, file_path: str, operation: str = OPERATION_READ) -> Tuple[bool, Optional[str]]:
        """Validate file path against security rules with operation type.
        
        Performs comprehensive security validation including:
        - Path normalization for consistent handling
        - Path traversal detection (..)
        - Denylist checking (highest priority)
        - Operation-specific permission checking (read vs write)
        - Allowlist checking in strict mode
        - Directory boundary validation
        - File extension validation
        
        Validation Order:
            1. Normalize path (handle ./, //, etc.)
            2. Check for path traversal attempts (..)
            3. Check denylist (always enforced, highest priority)
            4. Check operation permissions:
               - Write: Requires write_paths permission
               - Read: Requires read_paths or write_paths in allowlist mode
            5. Validate path is within config directory
            6. Validate file extension (except .storage files)
        
        Args:
            file_path: The file path to validate (relative or absolute)
            operation: The operation type, one of:
                - OPERATION_READ (default): Read-only operation
                - OPERATION_WRITE: Write operation (create, modify, delete)
        
        Returns:
            Tuple of (is_valid, error_message):
                - (True, None): Path is valid for the operation
                - (False, error_message): Path is invalid, with reason
        
        Error Messages:
            - ERROR_INVALID_PATH: Path traversal or invalid format
            - ERROR_BLACKLISTED_FILE: File is in denylist
            - ERROR_WRITE_NOT_PERMITTED: Write operation not allowed
            - ERROR_PERMISSION_DENIED: File not in allowlist (strict mode)
        
        Example Usage:
            ```python
            # Validate read operation (default)
            is_valid, error = security_manager.validate_file_path(
                "/config/configuration.yaml"
            )
            if is_valid:
                # Read the file
                pass
            
            # Validate write operation
            is_valid, error = security_manager.validate_file_path(
                "/config/packages/lights.yaml",
                operation=OPERATION_WRITE
            )
            if is_valid:
                # Write to the file
                pass
            else:
                # Handle error
                print(f"Access denied: {error}")
            
            # Path traversal attempt (blocked)
            is_valid, error = security_manager.validate_file_path(
                "../secrets.yaml"
            )
            # Returns: (False, ERROR_INVALID_PATH)
            
            # Sensitive file (blocked)
            is_valid, error = security_manager.validate_file_path(
                "/config/.storage/auth"
            )
            # Returns: (False, ERROR_BLACKLISTED_FILE)
            ```
        
        Security Notes:
            - Denylist always takes precedence over allowlist
            - Write paths implicitly grant read access
            - Path normalization prevents bypass attempts
            - Glob patterns are supported in configuration
        """
        try:
            # Normalize the path using os.path.normpath for consistent handling
            normalized_path = os.path.normpath(file_path)
            
            # Check for path traversal attempts (but allow absolute paths starting with /config/ or /addon_configs/)
            if ".." in normalized_path:
                self.log_security_event("path_traversal_attempt", {"path": file_path, "operation": operation})
                return False, ERROR_INVALID_PATH
            
            # If path doesn't start with /, prepend /config/ for matching against configured paths
            if not normalized_path.startswith("/"):
                normalized_path_with_prefix = f"/config/{normalized_path}"
            else:
                normalized_path_with_prefix = normalized_path
            
            # Check denylist FIRST (always enforced, takes precedence)
            # Check both with and without /config/ prefix
            if self.is_denylisted(normalized_path) or self.is_denylisted(normalized_path_with_prefix):
                self.log_security_event("denylist_access_attempt", {"path": file_path, "operation": operation})
                return False, ERROR_BLACKLISTED_FILE
            
            # Check operation-specific permissions
            if operation == OPERATION_WRITE:
                # Write operations require explicit write_paths permission
                # Check both with and without /config/ prefix
                if not (self.is_writable(normalized_path) or self.is_writable(normalized_path_with_prefix)):
                    self.log_security_event("write_access_denied", {
                        "path": file_path,
                        "reason": "path_not_in_write_paths"
                    })
                    return False, ERROR_WRITE_NOT_PERMITTED
            else:
                # Read operations check allowlist (always enforced - strict allowlist mode)
                # Check both with and without /config/ prefix
                if not (self.is_allowlisted(normalized_path) or self.is_allowlisted(normalized_path_with_prefix)):
                    self.log_security_event("allowlist_access_denied", {"path": file_path, "operation": operation})
                    return False, ERROR_PERMISSION_DENIED
            
            # Convert to absolute path relative to config directory
            config_path = Path(self.hass.config.config_dir)
            full_path = config_path / normalized_path
            
            # Ensure the resolved path is within allowed directories
            try:
                resolved_path = full_path.resolve()
                config_path_resolved = config_path.resolve()
                
                # Check if path is within config directory
                if not str(resolved_path).startswith(str(config_path_resolved)):
                    self.log_security_event("path_outside_config", {"path": file_path, "operation": operation})
                    return False, ERROR_INVALID_PATH
                    
            except (OSError, ValueError) as e:
                _LOGGER.warning("Path resolution failed for %s: %s", file_path, e)
                return False, ERROR_INVALID_PATH
            
            # Check file extension (skip for .storage files and explicitly allowlisted paths)
            # If a path is explicitly in the allowlist, trust it regardless of extension
            is_explicitly_allowlisted = (
                normalized_path in self.allowlist or 
                normalized_path_with_prefix in self.allowlist
            )
            
            if not is_explicitly_allowlisted and not normalized_path.startswith(".storage/") and not normalized_path.startswith("/config/.storage/"):
                file_extension = Path(normalized_path).suffix.lower()
                if file_extension and file_extension not in self.allowed_extensions:
                    self.log_security_event("invalid_extension", {
                        "path": file_path,
                        "extension": file_extension,
                        "operation": operation
                    })
                    return False, ERROR_INVALID_PATH
            
            return True, None
            
        except Exception as e:
            _LOGGER.error("Unexpected error validating path %s: %s", file_path, e)
            return False, ERROR_INVALID_PATH

    def is_denylisted(self, file_path: str) -> bool:
        """Check if file is in security denylist.
        
        Supports exact path matches, directory prefix matches (path/*),
        and glob pattern matches using fnmatch (* and ? wildcards).
        Also handles patterns without leading / by checking with /config/
        and /addon_configs/ prefixes.
        
        Denylist always takes precedence over allowlist - denied files are
        blocked regardless of other configuration.
        
        Matching Rules:
            1. Exact match: Path exactly matches a denylist entry
            2. Directory match: Path is within a denylisted directory
            3. Glob match: Path matches a glob pattern in denylist
        
        Args:
            file_path: The file path to check (will be normalized)
        
        Returns:
            True if file is denylisted, False otherwise
        
        Examples:
            ```python
            # Exact match
            denylist = {"/config/secrets.yaml"}
            is_denylisted("/config/secrets.yaml")
            # Returns: True
            
            # Directory match
            denylist = {"/config/.storage/auth"}
            is_denylisted("/config/.storage/auth/provider.json")
            # Returns: True
            
            # Glob pattern match
            denylist = {"/config/.storage/auth*"}
            is_denylisted("/config/.storage/auth_provider.homeassistant")
            # Returns: True
            
            # Pattern without leading / (checks with prefix)
            denylist = {".storage/core.*"}
            is_denylisted("/config/.storage/core.config_entries")
            # Returns: True
            
            # Not in denylist
            denylist = {"/config/secrets.yaml"}
            is_denylisted("/config/configuration.yaml")
            # Returns: False
            ```
        
        Note:
            Sensitive files like authentication data and system files are
            always in the default denylist and cannot be removed.
        """
        normalized_path = os.path.normpath(file_path)
        
        # Check exact matches
        if normalized_path in self.denylist:
            return True
        
        # Check directory matches and patterns
        for denied in self.denylist:
            # Check if file is in a subdirectory of a denied path
            if normalized_path.startswith(denied + "/") or normalized_path.startswith(denied + os.sep):
                return True
            
            # Check glob patterns (if pattern contains * or ?)
            if "*" in denied or "?" in denied:
                if self._matches_glob_pattern(normalized_path, denied):
                    return True
            else:
                # For non-wildcard patterns, also check with prefix matching
                # This allows patterns like ".storage/onboarding" to match "/config/.storage/onboarding"
                if self._matches_glob_pattern(normalized_path, denied):
                    return True
                
        return False

    def is_blacklisted(self, file_path: str) -> bool:
        """
        Check if file is in security blacklist.
        
        Deprecated: Use is_denylisted() instead. This method is kept for backward compatibility.
        
        Args:
            file_path: The file path to check
            
        Returns:
            True if file is blacklisted
        """
        return self.is_denylisted(file_path)

    def is_allowlisted(self, file_path: str) -> bool:
        """Check if file is in security allowlist (whitelist).
        
        Only used when allowlist mode is enabled. Supports exact path matches,
        directory prefix matches (path/*), and glob pattern matches using fnmatch.
        
        Matching Rules:
            1. Exact match: Path exactly matches an allowlist entry
            2. Directory match: Path is within an allowlisted directory
            3. Glob match: Path matches a glob pattern in allowlist
        
        Args:
            file_path: The file path to check (will be normalized)
        
        Returns:
            True if file is allowlisted or allowlist is empty (disabled),
            False otherwise
        
        Examples:
            ```python
            # Exact match
            allowlist = {"/config/configuration.yaml"}
            is_allowlisted("/config/configuration.yaml")
            # Returns: True
            
            # Directory match
            allowlist = {"/config/packages"}
            is_allowlisted("/config/packages/lights.yaml")
            # Returns: True
            
            # Glob pattern match
            allowlist = {"/config/.storage/lovelace*"}
            is_allowlisted("/config/.storage/lovelace.dashboard_main")
            # Returns: True
            
            # Not in allowlist
            allowlist = {"/config/configuration.yaml"}
            is_allowlisted("/config/secrets.yaml")
            # Returns: False
            
            # Allowlist disabled (empty)
            allowlist = set()
            is_allowlisted("/config/any_file.yaml")
            # Returns: True (allowlist mode disabled)
            ```
        
        Note:
            This method only checks the allowlist. It does NOT check the denylist.
            Use validate_file_path() for comprehensive security validation that
            checks both allowlist and denylist.
        """
        if not self.allowlist:
            # Allowlist mode disabled, allow all (subject to denylist)
            return True
            
        normalized_path = os.path.normpath(file_path)
        
        # Check exact matches
        if normalized_path in self.allowlist:
            return True
        
        # Check directory matches and glob patterns
        for allowed in self.allowlist:
            # Check if the file is in a subdirectory of an allowed path
            if normalized_path.startswith(allowed + "/") or normalized_path.startswith(allowed + os.sep):
                return True
            
            # Check glob patterns (if pattern contains * or ?)
            if "*" in allowed or "?" in allowed:
                if self._matches_glob_pattern(normalized_path, allowed):
                    return True
                
        return False

    def is_readable(self, file_path: str) -> bool:
        """Check if file is readable (in read_paths or write_paths).
        
        A file is readable if it appears in either read_paths or write_paths.
        Write paths implicitly grant read access.
        
        Args:
            file_path: The file path to check (relative or absolute)
        
        Returns:
            True if file is readable, False otherwise
        
        Example:
            ```python
            # File in read_paths
            config = {"read_paths": ["/config/configuration.yaml"]}
            security_manager = SecurityManager(hass, config)
            
            is_readable = security_manager.is_readable("/config/configuration.yaml")
            # Returns: True
            
            # File in write_paths (write implies read)
            config = {"write_paths": ["/config/packages/*.yaml"]}
            security_manager = SecurityManager(hass, config)
            
            is_readable = security_manager.is_readable("/config/packages/lights.yaml")
            # Returns: True
            
            # File not in any list
            is_readable = security_manager.is_readable("/config/secrets.yaml")
            # Returns: False
            ```
        
        Note:
            This method only checks if the path is in read_paths or write_paths.
            It does NOT check the denylist. Use validate_file_path() for
            comprehensive security validation.
        """
        normalized_path = os.path.normpath(file_path)
        
        # Check read_paths
        if self._path_matches_set(normalized_path, self.read_paths):
            return True
        
        # Check write_paths (write implies read)
        if self._path_matches_set(normalized_path, self.write_paths):
            return True
        
        return False
    
    def is_writable(self, file_path: str) -> bool:
        """Check if file is writable (in write_paths only).
        
        A file is writable only if it appears in write_paths.
        Being in read_paths does NOT grant write access.
        
        Args:
            file_path: The file path to check (relative or absolute)
        
        Returns:
            True if file is writable, False otherwise
        
        Example:
            ```python
            # File in write_paths
            config = {"write_paths": ["/config/packages/*.yaml"]}
            security_manager = SecurityManager(hass, config)
            
            is_writable = security_manager.is_writable("/config/packages/lights.yaml")
            # Returns: True
            
            # File in read_paths only (not writable)
            config = {"read_paths": ["/config/configuration.yaml"]}
            security_manager = SecurityManager(hass, config)
            
            is_writable = security_manager.is_writable("/config/configuration.yaml")
            # Returns: False
            
            # File not in any list
            is_writable = security_manager.is_writable("/config/secrets.yaml")
            # Returns: False
            ```
        
        Note:
            This method only checks if the path is in write_paths.
            It does NOT check the denylist. Use validate_file_path() with
            operation=OPERATION_WRITE for comprehensive security validation.
        """
        normalized_path = os.path.normpath(file_path)
        return self._path_matches_set(normalized_path, self.write_paths)
    
    def _path_matches_set(self, path: str, path_set: set[str]) -> bool:
        """
        Check if a path matches any entry in a path set.
        Supports exact matches, directory matches, and glob patterns.
        
        Args:
            path: The normalized path to check
            path_set: The set of paths/patterns to match against
            
        Returns:
            True if path matches any entry in the set
        """
        if not path_set:
            return False
        
        # Check exact matches
        if path in path_set:
            return True
        
        # Check directory matches
        for pattern in path_set:
            if path.startswith(pattern + "/") or path.startswith(pattern + os.sep):
                return True
        
        # Check glob patterns
        for pattern in path_set:
            if "*" in pattern or "?" in pattern:
                if self._matches_glob_pattern(path, pattern):
                    return True
        
        return False

    def validate_user_permissions(self, user: Optional[User]) -> Tuple[bool, Optional[str]]:
        """
        Validate user has required permissions.
        
        Args:
            user: The authenticated user
            
        Returns:
            Tuple of (is_authorized, error_message)
        """
        if not user:
            return False, "Authentication required"
        
        if not user.is_admin:
            self.log_security_event("non_admin_access_attempt", {
                "user_id": user.id,
                "user_name": user.name
            })
            return False, ERROR_PERMISSION_DENIED
        
        return True, None

    def get_security_mode(self) -> str:
        """
        Get the current security mode.
        
        Returns:
            Always returns "allowlist" as the system operates in strict allowlist mode
        """
        return "allowlist"

    def get_allowlist(self) -> set[str]:
        """
        Get the current allowlist.
        
        Returns:
            A copy of the current allowlist
        """
        return self.allowlist.copy()

    def get_denylist(self) -> set[str]:
        """
        Get the current denylist.
        
        Returns:
            A copy of the current denylist
        """
        return self.denylist.copy()

    def log_security_event(self, event_type: str, details: dict[str, Any]) -> None:
        """
        Log security-related events for monitoring.
        
        Args:
            event_type: Type of security event
            details: Additional event details
        """
        _LOGGER.warning(
            "Security event: %s - %s",
            event_type,
            details
        )

    def add_to_blacklist(self, file_path: str) -> None:
        """
        Add a file to the security blacklist.
        
        Deprecated: Use add_to_denylist() instead. This method is kept for backward compatibility.
        
        Args:
            file_path: File path to blacklist
        """
        self.add_to_denylist(file_path)

    def remove_from_blacklist(self, file_path: str) -> None:
        """
        Remove a file from the security blacklist.
        
        Deprecated: Use remove_from_denylist() instead. This method is kept for backward compatibility.
        
        Args:
            file_path: File path to remove from blacklist
        """
        self.remove_from_denylist(file_path)

    def add_to_denylist(self, file_path: str) -> None:
        """
        Add a file to the security denylist.
        
        Validates path before adding and logs the addition.
        
        Args:
            file_path: File path to denylist
        """
        # Validate path before adding
        if not self._is_valid_path_config(file_path):
            _LOGGER.warning(
                "Cannot add invalid path to denylist: %s (contains path traversal or absolute path)",
                file_path
            )
            return
        
        self.denylist.add(file_path)
        self.blacklist.add(file_path)  # Keep blacklist in sync for backward compatibility
        _LOGGER.info("Added %s to security denylist", file_path)

    def remove_from_denylist(self, file_path: str) -> None:
        """
        Remove a file from the security denylist.
        
        Args:
            file_path: File path to remove from denylist
        """
        self.denylist.discard(file_path)
        self.blacklist.discard(file_path)  # Keep blacklist in sync for backward compatibility
        _LOGGER.info("Removed %s from security denylist", file_path)

    def add_to_allowlist(self, file_path: str) -> None:
        """
        Add a file to the security allowlist.
        
        Validates path before adding and logs the addition.
        
        Args:
            file_path: File path to allowlist
        """
        # Validate path before adding
        if not self._is_valid_path_config(file_path):
            _LOGGER.warning(
                "Cannot add invalid path to allowlist: %s (contains path traversal or absolute path)",
                file_path
            )
            return
        
        self.allowlist.add(file_path)
        _LOGGER.info("Added %s to security allowlist", file_path)

    def remove_from_allowlist(self, file_path: str) -> None:
        """
        Remove a file from the security allowlist.
        
        Args:
            file_path: File path to remove from allowlist
        """
        self.allowlist.discard(file_path)
        _LOGGER.info("Removed %s from security allowlist", file_path)