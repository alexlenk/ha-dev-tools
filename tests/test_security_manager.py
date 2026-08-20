"""Test SecurityManager functionality with Home Assistant fixtures."""
import pytest
from pathlib import Path
from unittest.mock import Mock
from homeassistant.core import HomeAssistant
from homeassistant.auth.models import User

from custom_components.ha_dev_tools.security import SecurityManager
from custom_components.ha_dev_tools.const import (
    ERROR_BLACKLISTED_FILE,
    ERROR_INVALID_PATH,
    ERROR_PERMISSION_DENIED,
)


@pytest.fixture
def security_manager(hass: HomeAssistant):
    """Create a SecurityManager instance for testing with permissive config."""
    # Configure with permissive paths for testing
    config = {
        "read_paths": [
            "/config/*.yaml",
            "/config/*.yml",
            "/config/*.json",
            "/config/*.txt",
            "/config/**/*.yaml",
            "/config/**/*.yml",
            "/config/**/*.json",
            "/config/**/*.txt",
        ]
    }
    return SecurityManager(hass, config)


@pytest.fixture
def admin_user():
    """Create a mock admin user."""
    user = Mock()
    user.is_admin = True
    user.id = "admin_user_id"
    user.name = "Admin User"
    return user


@pytest.fixture
def regular_user():
    """Create a mock regular (non-admin) user."""
    user = Mock()
    user.is_admin = False
    user.id = "regular_user_id"
    user.name = "Regular User"
    return user


def test_validate_file_path_valid(hass: HomeAssistant, security_manager):
    """Test validation of valid file paths."""
    valid_paths = [
        "configuration.yaml",
        "automations.yaml",
        "scripts.yaml",
        "subdir/test.yaml",
        "test.json",
        "test.txt",
    ]
    
    for path in valid_paths:
        is_valid, error = security_manager.validate_file_path(path)
        assert is_valid is True, f"Path {path} should be valid"
        assert error is None


def test_validate_file_path_traversal(hass: HomeAssistant, security_manager):
    """Test rejection of path traversal attempts."""
    malicious_paths = [
        "../etc/passwd",
        "../../etc/shadow",
        "../../../etc/hosts",
        "subdir/../../etc/passwd",
    ]
    
    for path in malicious_paths:
        is_valid, error = security_manager.validate_file_path(path)
        assert is_valid is False, f"Path {path} should be rejected"
        assert error == ERROR_INVALID_PATH


def test_validate_file_path_absolute(hass: HomeAssistant, security_manager):
    """Test rejection of absolute paths outside allowed directories."""
    absolute_paths = [
        "/etc/passwd",
        "/var/log/syslog",
        "/home/user/.ssh/id_rsa",
    ]
    
    for path in absolute_paths:
        is_valid, error = security_manager.validate_file_path(path)
        assert is_valid is False, f"Path {path} should be rejected"
        # In strict allowlist mode, these paths are not in allowlist
        # so they return PERMISSION_DENIED
        assert error == ERROR_PERMISSION_DENIED


def test_validate_file_path_blacklisted(hass: HomeAssistant, security_manager):
    """Test rejection of blacklisted files."""
    blacklisted_paths = [
        "secrets.yaml",
        ".HA_VERSION",
        "home-assistant.log",
        ".storage/core.config_entries",
    ]
    
    for path in blacklisted_paths:
        is_valid, error = security_manager.validate_file_path(path)
        assert is_valid is False, f"Path {path} should be blacklisted"
        assert error == ERROR_BLACKLISTED_FILE


def test_validate_file_path_invalid_extension(hass: HomeAssistant, security_manager):
    """Test rejection of files with invalid extensions."""
    invalid_extensions = [
        "test.exe",
        "test.sh",
        "test.bat",
        "test.dll",
    ]
    
    for path in invalid_extensions:
        is_valid, error = security_manager.validate_file_path(path)
        assert is_valid is False, f"Path {path} should be rejected (invalid extension)"
        # In strict allowlist mode, these are rejected because they're not in allowlist
        assert error == ERROR_PERMISSION_DENIED


def test_is_blacklisted_exact_match(hass: HomeAssistant, security_manager):
    """Test blacklist checking with exact matches."""
    assert security_manager.is_blacklisted("secrets.yaml") is True
    assert security_manager.is_blacklisted(".HA_VERSION") is True
    assert security_manager.is_blacklisted("configuration.yaml") is False


def test_is_blacklisted_directory_match(hass: HomeAssistant, security_manager):
    """Test blacklist checking with directory patterns."""
    # Specific .storage files are blacklisted
    assert security_manager.is_blacklisted(".storage/core.config_entries") is True
    assert security_manager.is_blacklisted(".storage/core.entity_registry") is True
    assert security_manager.is_blacklisted(".storage/auth") is True
    
    # Regular directories should not be blacklisted
    assert security_manager.is_blacklisted("custom_components/test.py") is False


def test_validate_user_permissions_admin(hass: HomeAssistant, security_manager, admin_user):
    """Test that admin users are authorized."""
    is_authorized, error = security_manager.validate_user_permissions(admin_user)
    
    assert is_authorized is True
    assert error is None


def test_validate_user_permissions_regular_user(hass: HomeAssistant, security_manager, regular_user):
    """Test that regular users are not authorized."""
    is_authorized, error = security_manager.validate_user_permissions(regular_user)
    
    assert is_authorized is False
    assert error == ERROR_PERMISSION_DENIED


def test_validate_user_permissions_no_user(hass: HomeAssistant, security_manager):
    """Test that missing user is not authorized."""
    is_authorized, error = security_manager.validate_user_permissions(None)
    
    assert is_authorized is False
    assert error == "Authentication required"


def test_add_to_blacklist(hass: HomeAssistant, security_manager):
    """Test adding files to blacklist."""
    test_file = "test_sensitive.yaml"
    
    # Initially not blacklisted
    assert security_manager.is_blacklisted(test_file) is False
    
    # Add to blacklist
    security_manager.add_to_blacklist(test_file)
    
    # Now should be blacklisted
    assert security_manager.is_blacklisted(test_file) is True


def test_remove_from_blacklist(hass: HomeAssistant, security_manager):
    """Test removing files from blacklist."""
    test_file = "test_file.yaml"
    
    # Add to blacklist first
    security_manager.add_to_blacklist(test_file)
    assert security_manager.is_blacklisted(test_file) is True
    
    # Remove from blacklist
    security_manager.remove_from_blacklist(test_file)
    
    # Should no longer be blacklisted
    assert security_manager.is_blacklisted(test_file) is False


def test_log_security_event(hass: HomeAssistant, security_manager, caplog):
    """Test security event logging."""
    import logging
    
    with caplog.at_level(logging.WARNING):
        security_manager.log_security_event("test_event", {"detail": "test"})
    
    assert "Security event: test_event" in caplog.text
    assert "test" in caplog.text


def test_validate_file_path_normalization(hass: HomeAssistant, security_manager):
    """Test that paths are properly normalized.
    
    Note: os.path.normpath() resolves relative paths, so 'subdir/../configuration.yaml'
    becomes 'configuration.yaml', which is valid. This is correct behavior - benign
    relative paths are allowed after normalization.
    """
    # Simple path should be valid
    is_valid, error = security_manager.validate_file_path("configuration.yaml")
    assert is_valid is True
    
    # Path that normalizes to a valid path should be accepted
    # (subdir/../configuration.yaml normalizes to configuration.yaml)
    is_valid, error = security_manager.validate_file_path("subdir/../configuration.yaml")
    assert is_valid is True  # This is valid after normalization
    
    # But paths that try to escape the config directory should be rejected
    is_valid, error = security_manager.validate_file_path("../etc/passwd")
    assert is_valid is False
    assert error == ERROR_INVALID_PATH


def test_security_manager_initialization(hass: HomeAssistant):
    """Test SecurityManager initialization."""
    manager = SecurityManager(hass)
    
    assert manager.hass == hass
    assert len(manager.blacklist) > 0
    assert len(manager.allowed_extensions) > 0
    assert len(manager.allowed_directories) > 0


def test_allowed_extensions(hass: HomeAssistant, security_manager):
    """Test that allowed extensions are properly configured."""
    allowed_files = [
        "test.yaml",
        "test.yml",
        "test.json",
        "test.txt",
    ]
    
    for file in allowed_files:
        is_valid, error = security_manager.validate_file_path(file)
        # Should be valid (assuming not blacklisted and matches allowlist pattern)
        if not security_manager.is_blacklisted(file):
            assert is_valid is True, f"File {file} should have allowed extension"
    
    # test.py and test.jinja2 are not in the /config/*.{yaml,yml,json,txt} pattern
    # so they will be rejected in strict allowlist mode even though .py is an allowed extension


# ============================================================================
# Configuration Loading Tests (Task 9)
# ============================================================================

def test_default_configuration_with_none(hass: HomeAssistant):
    """Test SecurityManager with None config (Task 9.1)."""
    manager = SecurityManager(hass, config=None)
    
    # Should initialize successfully
    assert manager.hass == hass
    assert manager._config == {}
    
    # Should have default denylist loaded
    assert len(manager.denylist) > 0
    assert "secrets.yaml" in manager.denylist
    assert ".HA_VERSION" in manager.denylist
    
    # System now always operates in strict allowlist mode
    # No _mode attribute needed


def test_default_configuration_with_empty_dict(hass: HomeAssistant):
    """Test SecurityManager with empty dict config (Task 9.1)."""
    manager = SecurityManager(hass, config={})
    
    # Should initialize successfully
    assert manager.hass == hass
    assert manager._config == {}
    
    # Should have default denylist loaded
    assert len(manager.denylist) > 0
    assert "secrets.yaml" in manager.denylist
    assert ".HA_VERSION" in manager.denylist
    
    # System now always operates in strict allowlist mode
    # No _mode attribute needed


def test_default_denylist_loaded(hass: HomeAssistant):
    """Test that default denylist is loaded (Task 9.1)."""
    manager = SecurityManager(hass, config=None)
    
    # Check that sensitive files are in denylist
    sensitive_files = [
        "secrets.yaml",
        ".HA_VERSION",
        "home-assistant.log",
        ".storage/auth",
        ".storage/core.config_entries",
        ".storage/core.device_registry",
        ".storage/core.entity_registry",
        ".storage/onboarding",
        ".storage/hassio",
    ]
    
    for file in sensitive_files:
        assert manager.is_denylisted(file), f"{file} should be in default denylist"


def test_configuration_parsing_allowed_paths(hass: HomeAssistant):
    """Test configuration parsing with allowed_paths list (Task 9.2)."""
    config = {
        "allowed_paths": [
            "/config/configuration.yaml",
            "/config/automations.yaml",
            "/config/packages/*.yaml",  # Glob pattern
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # All configured paths should be in allowlist
    assert "/config/configuration.yaml" in manager.allowlist
    assert "/config/automations.yaml" in manager.allowlist
    assert "/config/packages/*.yaml" in manager.allowlist


def test_configuration_parsing_read_write_paths(hass: HomeAssistant):
    """Test configuration parsing with read_paths and write_paths (Task 9.2)."""
    config = {
        "read_paths": [
            "/config/configuration.yaml",
            "/config/.storage/lovelace*",  # Glob pattern
        ],
        "write_paths": [
            "/config/packages/generated/*.yaml",  # Glob pattern
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Read paths should be in read_paths set
    assert "/config/configuration.yaml" in manager.read_paths
    assert "/config/.storage/lovelace*" in manager.read_paths
    
    # Write paths should be in write_paths set
    assert "/config/packages/generated/*.yaml" in manager.write_paths
    
    # Both should be merged into allowlist
    assert "/config/configuration.yaml" in manager.allowlist
    assert "/config/.storage/lovelace*" in manager.allowlist
    assert "/config/packages/generated/*.yaml" in manager.allowlist


def test_configuration_parsing_denied_paths(hass: HomeAssistant):
    """Test configuration parsing with denied_paths list (Task 9.2)."""
    config = {
        "denied_paths": [
            "/config/custom_file.yaml",
            "/config/temp/*.log",  # Glob pattern
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Configured denied paths should be in denylist
    assert "/config/custom_file.yaml" in manager.denylist
    assert "/config/temp/*.log" in manager.denylist
    
    # Default denylist should still be present
    assert "secrets.yaml" in manager.denylist


def test_configuration_parsing_glob_patterns(hass: HomeAssistant):
    """Test that glob patterns are added correctly (Task 9.2)."""
    config = {
        "allowed_paths": [
            "/config/.storage/lovelace*",
            "/config/.storage/input_*",
            "/config/packages/**/*.yaml",
        ],
        "denied_paths": [
            "/config/.storage/auth*",
            "/config/temp/*.log",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Glob patterns should be in allowlist
    assert "/config/.storage/lovelace*" in manager.allowlist
    assert "/config/.storage/input_*" in manager.allowlist
    assert "/config/packages/**/*.yaml" in manager.allowlist
    
    # Glob patterns should be in denylist
    assert "/config/.storage/auth*" in manager.denylist
    assert "/config/temp/*.log" in manager.denylist


def test_configuration_parsing_legacy_storage_files(hass: HomeAssistant):
    """Test legacy allowed_storage_files configuration (Task 9.2)."""
    config = {
        "allowed_storage_files": [
            "lovelace",
            "input_boolean",
            "script",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Storage files should be prefixed with .storage/
    assert ".storage/lovelace" in manager.allowlist
    assert ".storage/input_boolean" in manager.allowlist
    assert ".storage/script" in manager.allowlist


def test_invalid_configuration_path_traversal(hass: HomeAssistant, caplog):
    """Test handling of paths with path traversal (..) (Task 9.3)."""
    import logging
    
    config = {
        "allowed_paths": [
            "/config/configuration.yaml",  # Valid
            "/config/../etc/passwd",  # Invalid - path traversal
            "/config/packages/../../../etc/shadow",  # Invalid - path traversal
        ]
    }
    
    with caplog.at_level(logging.WARNING):
        manager = SecurityManager(hass, config=config)
    
    # Valid path should be in allowlist
    assert "/config/configuration.yaml" in manager.allowlist
    
    # Invalid paths should NOT be in allowlist
    assert "/config/../etc/passwd" not in manager.allowlist
    assert "/config/packages/../../../etc/shadow" not in manager.allowlist
    
    # Should log warnings for invalid paths
    assert "Invalid path in allowed_paths" in caplog.text
    assert "path traversal" in caplog.text.lower()


def test_invalid_configuration_absolute_paths(hass: HomeAssistant, caplog):
    """Test handling of absolute paths not starting with /config/ or /addon_configs/ (Task 9.3)."""
    import logging
    
    config = {
        "allowed_paths": [
            "/config/configuration.yaml",  # Valid
            "/etc/passwd",  # Invalid - absolute path outside allowed directories
            "/var/log/syslog",  # Invalid - absolute path outside allowed directories
        ]
    }
    
    with caplog.at_level(logging.WARNING):
        manager = SecurityManager(hass, config=config)
    
    # Valid path should be in allowlist
    assert "/config/configuration.yaml" in manager.allowlist
    
    # Invalid paths should NOT be in allowlist
    assert "/etc/passwd" not in manager.allowlist
    assert "/var/log/syslog" not in manager.allowlist
    
    # Should log warnings for invalid paths
    assert "Invalid path in allowed_paths" in caplog.text


def test_invalid_configuration_storage_file_with_separators(hass: HomeAssistant, caplog):
    """Test handling of storage file names with directory separators (Task 9.3)."""
    import logging
    
    config = {
        "allowed_storage_files": [
            "lovelace",  # Valid
            "../auth",  # Invalid - contains separator
            "subdir/file",  # Invalid - contains separator
        ]
    }
    
    with caplog.at_level(logging.WARNING):
        manager = SecurityManager(hass, config=config)
    
    # Valid storage file should be in allowlist
    assert ".storage/lovelace" in manager.allowlist
    
    # Invalid storage files should NOT be in allowlist
    assert ".storage/../auth" not in manager.allowlist
    assert ".storage/subdir/file" not in manager.allowlist
    
    # Should log warnings for invalid storage file names
    assert "Invalid storage file name" in caplog.text
    assert "directory separators" in caplog.text.lower()


def test_invalid_configuration_fallback_to_defaults(hass: HomeAssistant):
    """Test that system falls back to defaults with invalid config (Task 9.3)."""
    config = {
        "allowed_paths": [
            "../etc/passwd",  # All invalid
            "/etc/shadow",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should still initialize successfully
    assert manager.hass == hass
    
    # Should have default denylist even with invalid config
    assert len(manager.denylist) > 0
    assert "secrets.yaml" in manager.denylist
    
    # Invalid paths should not be in allowlist
    assert "../etc/passwd" not in manager.allowlist
    assert "/etc/shadow" not in manager.allowlist


def test_invalid_configuration_warning_logs(hass: HomeAssistant, caplog):
    """Test that warning logs are generated for invalid configuration (Task 9.3)."""
    import logging
    
    config = {
        "allowed_paths": [
            "/config/../etc/passwd",
            "/etc/shadow",
        ],
        "denied_paths": [
            "../sensitive.yaml",
        ],
        "allowed_storage_files": [
            "subdir/file",
        ]
    }
    
    with caplog.at_level(logging.WARNING):
        manager = SecurityManager(hass, config=config)
    
    # Should log warnings for all invalid configurations
    log_text = caplog.text.lower()
    assert "invalid path" in log_text
    assert "path traversal" in log_text or "absolute path" in log_text
    assert "invalid storage file name" in log_text


def test_allowlist_mode_with_explicit_paths(hass: HomeAssistant):
    """Test allowlist with explicit read_paths (Task 9.4)."""
    config = {
        "read_paths": [
            "/config/configuration.yaml",
            "/config/automations.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # System always operates in strict allowlist mode
    # Configured paths should be in allowlist
    assert "/config/configuration.yaml" in manager.allowlist
    assert "/config/automations.yaml" in manager.allowlist
    
    # Should allow access to allowlisted files
    assert manager.is_allowlisted("/config/configuration.yaml") is True
    assert manager.is_allowlisted("/config/automations.yaml") is True
    
    # Should deny access to non-allowlisted files
    assert manager.is_allowlisted("/config/secrets.yaml") is False


def test_allowlist_mode_default_behavior(hass: HomeAssistant, caplog):
    """Test default behavior with no read_paths specified (Task 9.4)."""
    import logging
    
    config = {
        # No read_paths specified
    }
    
    with caplog.at_level(logging.INFO):
        manager = SecurityManager(hass, config=config)
    
    # System always operates in strict allowlist mode
    # Should use recommended safe storage patterns
    assert len(manager.allowlist) > 0
    
    # Should log that it's using default safe storage patterns
    assert "default safe storage patterns" in caplog.text.lower()
    
    # Should have safe storage patterns in allowlist
    from custom_components.ha_dev_tools.const import RECOMMENDED_SAFE_STORAGE_PATTERNS
    for pattern in RECOMMENDED_SAFE_STORAGE_PATTERNS:
        assert pattern in manager.allowlist


def test_denylist_mode_operation(hass: HomeAssistant):
    """Test denylist operation with custom denied_paths (Task 9.4)."""
    config = {
        "denied_paths": [
            "/config/custom_blocked.yaml",
        ],
        "read_paths": [
            "/config/*.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Custom denied path should be in denylist
    assert "/config/custom_blocked.yaml" in manager.denylist
    
    # Should deny access to denylisted files
    assert manager.is_denylisted("/config/custom_blocked.yaml") is True
    
    # Should allow access to non-denylisted files that are in allowlist
    assert manager.is_denylisted("/config/configuration.yaml") is False


def test_allowlist_mode_denylist_precedence(hass: HomeAssistant):
    """Test that denylist takes precedence in allowlist mode (Task 9.4)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "allowed_paths": [
            "/config/configuration.yaml",
            "/config/secrets.yaml",  # Also in denylist
        ],
        "denied_paths": [
            "/config/secrets.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Both should be in their respective lists
    assert "/config/configuration.yaml" in manager.allowlist
    assert "/config/secrets.yaml" in manager.allowlist
    assert "/config/secrets.yaml" in manager.denylist
    
    # Denylist should take precedence in validation
    is_valid, error = manager.validate_file_path("/config/secrets.yaml")
    assert is_valid is False
    from custom_components.ha_dev_tools.const import ERROR_BLACKLISTED_FILE
    assert error == ERROR_BLACKLISTED_FILE


# ============================================================================
# Path Validation Tests (Task 10)
# ============================================================================

def test_allowlist_mode_access_to_allowlisted_files(hass: HomeAssistant):
    """Test access to allowlisted files in allowlist mode (Task 10.1)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/configuration.yaml",
            "/config/automations.yaml",
            "/config/scripts.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should allow access to allowlisted files
    is_valid, error = manager.validate_file_path("configuration.yaml")
    assert is_valid is True
    assert error is None
    
    is_valid, error = manager.validate_file_path("automations.yaml")
    assert is_valid is True
    assert error is None
    
    is_valid, error = manager.validate_file_path("scripts.yaml")
    assert is_valid is True
    assert error is None


def test_allowlist_mode_denial_of_non_allowlisted_files(hass: HomeAssistant):
    """Test denial of non-allowlisted files in allowlist mode (Task 10.1)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/configuration.yaml",
            "/config/automations.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should deny access to non-allowlisted files
    is_valid, error = manager.validate_file_path("scripts.yaml")
    assert is_valid is False
    assert error == ERROR_PERMISSION_DENIED
    
    is_valid, error = manager.validate_file_path("scenes.yaml")
    assert is_valid is False
    assert error == ERROR_PERMISSION_DENIED
    
    is_valid, error = manager.validate_file_path("custom_file.yaml")
    assert is_valid is False
    assert error == ERROR_PERMISSION_DENIED


def test_allowlist_mode_directory_matching(hass: HomeAssistant):
    """Test directory matching in allowlist mode (Task 10.1)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/packages",  # Allow entire directory
            "/config/blueprints",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should allow access to files within allowlisted directories
    is_valid, error = manager.validate_file_path("packages/lights.yaml")
    assert is_valid is True
    assert error is None
    
    is_valid, error = manager.validate_file_path("packages/sensors/temperature.yaml")
    assert is_valid is True
    assert error is None
    
    is_valid, error = manager.validate_file_path("blueprints/automation/motion_light.yaml")
    assert is_valid is True
    assert error is None
    
    # Should deny access to files outside allowlisted directories
    is_valid, error = manager.validate_file_path("custom_components/test.py")
    assert is_valid is False
    assert error == ERROR_PERMISSION_DENIED


def test_denylist_precedence_over_allowlist(hass: HomeAssistant):
    """Test that denylist takes precedence over allowlist (Task 10.2)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/configuration.yaml",
            "/config/secrets.yaml",  # Also in denylist
            "/config/packages",
        ],
        "denied_paths": [
            "/config/secrets.yaml",
            "/config/packages/blocked.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # File in both allowlist and denylist should be denied
    is_valid, error = manager.validate_file_path("secrets.yaml")
    assert is_valid is False
    assert error == ERROR_BLACKLISTED_FILE
    
    # File in allowlisted directory but explicitly denied should be denied
    is_valid, error = manager.validate_file_path("packages/blocked.yaml")
    assert is_valid is False
    assert error == ERROR_BLACKLISTED_FILE
    
    # File only in allowlist should be allowed
    is_valid, error = manager.validate_file_path("configuration.yaml")
    assert is_valid is True
    assert error is None
    
    # File in allowlisted directory and not denied should be allowed
    is_valid, error = manager.validate_file_path("packages/lights.yaml")
    assert is_valid is True
    assert error is None


def test_path_normalization_with_dot_slash(hass: HomeAssistant):
    """Test path normalization with ./ components (Task 10.3)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/configuration.yaml",
            "/config/packages",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Paths with ./ should be normalized and treated the same
    is_valid1, error1 = manager.validate_file_path("./configuration.yaml")
    is_valid2, error2 = manager.validate_file_path("configuration.yaml")
    
    assert is_valid1 == is_valid2
    assert error1 == error2
    
    # Paths with ./ in subdirectories
    is_valid1, error1 = manager.validate_file_path("./packages/lights.yaml")
    is_valid2, error2 = manager.validate_file_path("packages/lights.yaml")
    
    assert is_valid1 == is_valid2
    assert error1 == error2


def test_path_normalization_with_redundant_separators(hass: HomeAssistant):
    """Test path normalization with redundant separators (Task 10.3)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/packages",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Paths with redundant separators should be normalized
    is_valid1, error1 = manager.validate_file_path("packages//lights.yaml")
    is_valid2, error2 = manager.validate_file_path("packages/lights.yaml")
    
    assert is_valid1 == is_valid2
    assert error1 == error2
    
    # Multiple redundant separators
    is_valid1, error1 = manager.validate_file_path("packages///sensors//temperature.yaml")
    is_valid2, error2 = manager.validate_file_path("packages/sensors/temperature.yaml")
    
    assert is_valid1 == is_valid2
    assert error1 == error2


def test_path_normalization_consistency(hass: HomeAssistant):
    """Test that equivalent paths are treated consistently (Task 10.3)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/configuration.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # All these paths should be treated identically
    paths = [
        "configuration.yaml",
        "./configuration.yaml",
        "././configuration.yaml",
    ]
    
    results = [manager.validate_file_path(path) for path in paths]
    
    # All should have the same result
    assert all(r[0] == results[0][0] for r in results)
    assert all(r[1] == results[0][1] for r in results)


def test_glob_pattern_asterisk_wildcard(hass: HomeAssistant):
    """Test * wildcard patterns in allowlist (Task 10.4)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/.storage/lovelace*",  # Matches lovelace and lovelace.dashboard_*
            "/config/.storage/input_*",    # Matches all input helpers
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should match .storage/lovelace
    is_valid, error = manager.validate_file_path(".storage/lovelace")
    assert is_valid is True
    assert error is None
    
    # Should match .storage/lovelace.dashboard_main
    is_valid, error = manager.validate_file_path(".storage/lovelace.dashboard_main")
    assert is_valid is True
    assert error is None
    
    # Should match all input helpers
    is_valid, error = manager.validate_file_path(".storage/input_boolean")
    assert is_valid is True
    assert error is None
    
    is_valid, error = manager.validate_file_path(".storage/input_number")
    assert is_valid is True
    assert error is None
    
    is_valid, error = manager.validate_file_path(".storage/input_text")
    assert is_valid is True
    assert error is None
    
    # Should NOT match files that don't match the pattern
    is_valid, error = manager.validate_file_path(".storage/script")
    assert is_valid is False
    assert error == ERROR_PERMISSION_DENIED


def test_glob_pattern_question_mark_wildcard(hass: HomeAssistant):
    """Test ? wildcard patterns in allowlist (Task 10.4)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/configuration.ya??",  # Matches configuration.yaml (4 chars) and configuration.yamlx (if it existed)
            "/config/automations.ym?",     # Matches automations.yml (3 chars)
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should match configuration.yaml (ya?? matches yaml)
    is_valid, error = manager.validate_file_path("configuration.yaml")
    assert is_valid is True
    assert error is None
    
    # Should match automations.yml (ym? matches yml)
    is_valid, error = manager.validate_file_path("automations.yml")
    assert is_valid is True
    assert error is None
    
    # Should NOT match configuration.yml (ya?? doesn't match yml - only 3 chars)
    is_valid, error = manager.validate_file_path("configuration.yml")
    assert is_valid is False
    assert error == ERROR_PERMISSION_DENIED


def test_glob_pattern_directory_wildcards(hass: HomeAssistant):
    """Test directory wildcards in allowlist (Task 10.4)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/packages/*",  # Matches all files directly in packages/
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should match files directly in packages/
    is_valid, error = manager.validate_file_path("packages/lights.yaml")
    assert is_valid is True
    assert error is None
    
    is_valid, error = manager.validate_file_path("packages/sensors.yaml")
    assert is_valid is True
    assert error is None
    
    # Note: packages/* pattern matches files in packages/ directory
    # For subdirectories, we need packages/** or specific patterns


def test_glob_pattern_storage_files(hass: HomeAssistant):
    """Test .storage/* patterns for storage files (Task 10.4)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/.storage/*",  # Matches all storage files
        ],
        "denied_paths": [
            "/config/.storage/auth*",  # But block auth files
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should match storage files
    is_valid, error = manager.validate_file_path(".storage/lovelace")
    assert is_valid is True
    assert error is None
    
    is_valid, error = manager.validate_file_path(".storage/script")
    assert is_valid is True
    assert error is None
    
    # Should block auth files (denylist precedence)
    is_valid, error = manager.validate_file_path(".storage/auth")
    assert is_valid is False
    assert error == ERROR_BLACKLISTED_FILE
    
    is_valid, error = manager.validate_file_path(".storage/auth_provider.homeassistant")
    assert is_valid is False
    assert error == ERROR_BLACKLISTED_FILE


def test_glob_pattern_multiple_wildcards(hass: HomeAssistant):
    """Test patterns with multiple wildcards (Task 10.4)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/*ations.ya??",  # Matches *ations.yaml in root (using ?? for 'ml')
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should match files with both wildcards
    is_valid, error = manager.validate_file_path("automations.yaml")
    assert is_valid is True
    assert error is None
    
    is_valid, error = manager.validate_file_path("integrations.yaml")
    assert is_valid is True
    assert error is None
    
    is_valid, error = manager.validate_file_path("configurations.yaml")
    assert is_valid is True
    assert error is None
    
    # Should NOT match files that don't match the pattern
    is_valid, error = manager.validate_file_path("configuration.yaml")  # Doesn't end with 'ations'
    assert is_valid is False
    assert error == ERROR_PERMISSION_DENIED
    
    is_valid, error = manager.validate_file_path("automations.yml")  # Only 3 chars after 'ya', not 4
    assert is_valid is False
    assert error == ERROR_PERMISSION_DENIED
    
    is_valid, error = manager.validate_file_path("test.json")
    assert is_valid is False
    assert error == ERROR_PERMISSION_DENIED


# ============================================================================
# Storage File Access Tests (Task 11)
# ============================================================================

def test_safe_storage_lovelace_pattern_matches(hass: HomeAssistant):
    """Test .storage/lovelace* pattern matches lovelace files (Task 11.1)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/.storage/lovelace*",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should match .storage/lovelace
    is_valid, error = manager.validate_file_path(".storage/lovelace")
    assert is_valid is True, ".storage/lovelace should be accessible"
    assert error is None
    
    # Should match .storage/lovelace.dashboard_main
    is_valid, error = manager.validate_file_path(".storage/lovelace.dashboard_main")
    assert is_valid is True, ".storage/lovelace.dashboard_main should be accessible"
    assert error is None
    
    # Should match .storage/lovelace.dashboard_admin
    is_valid, error = manager.validate_file_path(".storage/lovelace.dashboard_admin")
    assert is_valid is True, ".storage/lovelace.dashboard_admin should be accessible"
    assert error is None
    
    # Should match .storage/lovelace_anything
    is_valid, error = manager.validate_file_path(".storage/lovelace_anything")
    assert is_valid is True, ".storage/lovelace_anything should be accessible"
    assert error is None


def test_safe_storage_input_helpers_pattern_matches(hass: HomeAssistant):
    """Test .storage/input_* pattern matches all input helpers (Task 11.1)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/.storage/input_*",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should match all input helper types
    input_helpers = [
        ".storage/input_boolean",
        ".storage/input_number",
        ".storage/input_text",
        ".storage/input_select",
        ".storage/input_datetime",
    ]
    
    for helper in input_helpers:
        is_valid, error = manager.validate_file_path(helper)
        assert is_valid is True, f"{helper} should be accessible"
        assert error is None


def test_safe_storage_script_access(hass: HomeAssistant):
    """Test .storage/script access (Task 11.1)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/.storage/script",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should allow access to script storage file
    is_valid, error = manager.validate_file_path(".storage/script")
    assert is_valid is True, ".storage/script should be accessible"
    assert error is None


def test_safe_storage_automation_access(hass: HomeAssistant):
    """Test .storage/automation access (Task 11.1)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/.storage/automation",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should allow access to automation storage file
    is_valid, error = manager.validate_file_path(".storage/automation")
    assert is_valid is True, ".storage/automation should be accessible"
    assert error is None


def test_safe_storage_timer_counter_access(hass: HomeAssistant):
    """Test .storage/timer and .storage/counter access (Task 11.1)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/.storage/timer",
            "/config/.storage/counter",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should allow access to timer storage file
    is_valid, error = manager.validate_file_path(".storage/timer")
    assert is_valid is True, ".storage/timer should be accessible"
    assert error is None
    
    # Should allow access to counter storage file
    is_valid, error = manager.validate_file_path(".storage/counter")
    assert is_valid is True, ".storage/counter should be accessible"
    assert error is None


def test_safe_storage_scene_access(hass: HomeAssistant):
    """Test .storage/scene access (Task 11.1)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/.storage/scene",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should allow access to scene storage file
    is_valid, error = manager.validate_file_path(".storage/scene")
    assert is_valid is True, ".storage/scene should be accessible"
    assert error is None


def test_all_safe_storage_files_with_recommended_patterns(hass: HomeAssistant):
    """Test all safe files accessible with recommended patterns (Task 11.1)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    # Use recommended configuration
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/.storage/lovelace*",
            "/config/.storage/input_*",
            "/config/.storage/timer",
            "/config/.storage/counter",
            "/config/.storage/script",
            "/config/.storage/scene",
            "/config/.storage/automation",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Test all safe storage files
    safe_files = [
        ".storage/lovelace",
        ".storage/lovelace.dashboard_main",
        ".storage/input_boolean",
        ".storage/input_number",
        ".storage/input_text",
        ".storage/input_select",
        ".storage/input_datetime",
        ".storage/timer",
        ".storage/counter",
        ".storage/script",
        ".storage/scene",
        ".storage/automation",
    ]
    
    for file in safe_files:
        is_valid, error = manager.validate_file_path(file)
        assert is_valid is True, f"{file} should be accessible with recommended patterns"
        assert error is None


def test_sensitive_storage_auth_pattern_blocks_all_auth_files(hass: HomeAssistant):
    """Test .storage/auth* pattern blocks all auth files (Task 11.2)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/.storage/*",  # Allow all storage files
        ],
        "denied_paths": [
            "/config/.storage/auth*",  # But block auth files
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should block all auth-related files
    auth_files = [
        ".storage/auth",
        ".storage/auth_provider.homeassistant",
        ".storage/auth_module.totp",
        ".storage/auth_anything",
    ]
    
    for file in auth_files:
        is_valid, error = manager.validate_file_path(file)
        assert is_valid is False, f"{file} should be blocked"
        assert error == ERROR_BLACKLISTED_FILE


def test_sensitive_storage_core_pattern_blocks_registry_files(hass: HomeAssistant):
    """Test .storage/core.* pattern blocks all core registry files (Task 11.2)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/.storage/*",  # Allow all storage files
        ],
        "denied_paths": [
            "/config/.storage/core.*",  # But block core registry files
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should block all core registry files
    core_files = [
        ".storage/core.config_entries",
        ".storage/core.device_registry",
        ".storage/core.entity_registry",
        ".storage/core.area_registry",
        ".storage/core.restore_state",
    ]
    
    for file in core_files:
        is_valid, error = manager.validate_file_path(file)
        assert is_valid is False, f"{file} should be blocked"
        assert error == ERROR_BLACKLISTED_FILE


def test_sensitive_storage_onboarding_blocking(hass: HomeAssistant):
    """Test .storage/onboarding blocking (Task 11.2)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/.storage/*",  # Allow all storage files
        ]
        # onboarding is in DEFAULT_DENYLIST
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should block onboarding file
    is_valid, error = manager.validate_file_path(".storage/onboarding")
    assert is_valid is False, ".storage/onboarding should be blocked"
    assert error == ERROR_BLACKLISTED_FILE


def test_sensitive_storage_hassio_blocking(hass: HomeAssistant):
    """Test .storage/hassio blocking (Task 11.2)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/.storage/*",  # Allow all storage files
        ]
        # hassio is in DEFAULT_DENYLIST
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should block hassio file
    is_valid, error = manager.validate_file_path(".storage/hassio")
    assert is_valid is False, ".storage/hassio should be blocked"
    assert error == ERROR_BLACKLISTED_FILE


def test_all_sensitive_storage_files_blocked(hass: HomeAssistant):
    """Test all sensitive files blocked regardless of allowlist (Task 11.2)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/.storage/*",  # Allow all storage files
        ]
        # Sensitive files are in DEFAULT_DENYLIST
    }
    
    manager = SecurityManager(hass, config=config)
    
    # All sensitive files should be blocked
    sensitive_files = [
        ".storage/auth",
        ".storage/auth_provider.homeassistant",
        ".storage/core.config_entries",
        ".storage/core.device_registry",
        ".storage/core.entity_registry",
        ".storage/onboarding",
        ".storage/hassio",
        ".storage/person",
        ".storage/zone",
    ]
    
    for file in sensitive_files:
        is_valid, error = manager.validate_file_path(file)
        assert is_valid is False, f"{file} should be blocked"
        assert error == ERROR_BLACKLISTED_FILE


def test_sensitive_files_blocked_even_with_explicit_allowlist(hass: HomeAssistant):
    """Test sensitive files blocked even when explicitly in allowlist (Task 11.2)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/.storage/auth",  # Try to explicitly allow
            "/config/.storage/core.config_entries",  # Try to explicitly allow
        ]
        # These are in DEFAULT_DENYLIST, which takes precedence
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Should still be blocked (denylist precedence)
    is_valid, error = manager.validate_file_path(".storage/auth")
    assert is_valid is False, ".storage/auth should be blocked despite being in allowlist"
    assert error == ERROR_BLACKLISTED_FILE
    
    is_valid, error = manager.validate_file_path(".storage/core.config_entries")
    assert is_valid is False, ".storage/core.config_entries should be blocked despite being in allowlist"
    assert error == ERROR_BLACKLISTED_FILE


def test_safe_and_sensitive_storage_separation(hass: HomeAssistant):
    """Test that safe files are accessible while sensitive files are blocked (Task 11.1, 11.2)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    # Use recommended configuration
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/.storage/lovelace*",
            "/config/.storage/input_*",
            "/config/.storage/script",
            "/config/.storage/automation",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Safe files should be accessible
    safe_files = [
        ".storage/lovelace",
        ".storage/input_boolean",
        ".storage/script",
        ".storage/automation",
    ]
    
    for file in safe_files:
        is_valid, error = manager.validate_file_path(file)
        assert is_valid is True, f"Safe file {file} should be accessible"
        assert error is None
    
    # Sensitive files should be blocked
    sensitive_files = [
        ".storage/auth",
        ".storage/core.config_entries",
        ".storage/onboarding",
        ".storage/hassio",
    ]
    
    for file in sensitive_files:
        is_valid, error = manager.validate_file_path(file)
        assert is_valid is False, f"Sensitive file {file} should be blocked"
        assert error == ERROR_BLACKLISTED_FILE


# ============================================================================
# Read-Only vs Read-Write Access Control Tests (Task 12)
# ============================================================================

def test_read_operations_on_read_only_paths(hass: HomeAssistant):
    """Test read operations succeed on read-only paths (Task 12.1)."""
    from custom_components.ha_dev_tools.const import (
        SECURITY_MODE_ALLOWLIST,
        OPERATION_READ,
        OPERATION_WRITE,
        ERROR_WRITE_NOT_PERMITTED,
    )
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/configuration.yaml",
            "/config/automations.yaml",
            "/config/scripts.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Test read operations on read-only paths (should succeed)
    read_only_paths = [
        "configuration.yaml",
        "automations.yaml",
        "scripts.yaml",
    ]
    
    for path in read_only_paths:
        is_valid, error = manager.validate_file_path(path, OPERATION_READ)
        assert is_valid is True, f"Read operation on read-only path {path} should succeed"
        assert error is None


def test_write_operations_denied_on_read_only_paths(hass: HomeAssistant):
    """Test write operations are denied on read-only paths (Task 12.1)."""
    from custom_components.ha_dev_tools.const import (
        SECURITY_MODE_ALLOWLIST,
        OPERATION_READ,
        OPERATION_WRITE,
        ERROR_WRITE_NOT_PERMITTED,
    )
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/configuration.yaml",
            "/config/automations.yaml",
            "/config/scripts.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Test write operations on read-only paths (should be denied)
    read_only_paths = [
        "configuration.yaml",
        "automations.yaml",
        "scripts.yaml",
    ]
    
    for path in read_only_paths:
        is_valid, error = manager.validate_file_path(path, OPERATION_WRITE)
        assert is_valid is False, f"Write operation on read-only path {path} should be denied"
        assert error == ERROR_WRITE_NOT_PERMITTED, f"Expected ERROR_WRITE_NOT_PERMITTED, got {error}"


def test_error_message_for_write_on_read_only_path(hass: HomeAssistant):
    """Test error message is ERROR_WRITE_NOT_PERMITTED for write on read-only path (Task 12.1)."""
    from custom_components.ha_dev_tools.const import (
        SECURITY_MODE_ALLOWLIST,
        OPERATION_WRITE,
        ERROR_WRITE_NOT_PERMITTED,
    )
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/configuration.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Verify specific error message
    is_valid, error = manager.validate_file_path("configuration.yaml", OPERATION_WRITE)
    assert is_valid is False
    assert error == ERROR_WRITE_NOT_PERMITTED, f"Expected ERROR_WRITE_NOT_PERMITTED, got {error}"


def test_read_operations_on_write_enabled_paths(hass: HomeAssistant):
    """Test read operations succeed on write-enabled paths (Task 12.2)."""
    from custom_components.ha_dev_tools.const import (
        SECURITY_MODE_ALLOWLIST,
        OPERATION_READ,
    )
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "write_paths": [
            "/config/packages/generated/test.yaml",
            "/config/custom_data.json",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Test read operations on write-enabled paths (should succeed)
    write_enabled_paths = [
        "packages/generated/test.yaml",
        "custom_data.json",
    ]
    
    for path in write_enabled_paths:
        is_valid, error = manager.validate_file_path(path, OPERATION_READ)
        assert is_valid is True, f"Read operation on write-enabled path {path} should succeed"
        assert error is None


def test_write_operations_on_write_enabled_paths(hass: HomeAssistant):
    """Test write operations succeed on write-enabled paths (Task 12.2)."""
    from custom_components.ha_dev_tools.const import (
        SECURITY_MODE_ALLOWLIST,
        OPERATION_WRITE,
    )
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "write_paths": [
            "/config/packages/generated/test.yaml",
            "/config/custom_data.json",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Test write operations on write-enabled paths (should succeed)
    write_enabled_paths = [
        "packages/generated/test.yaml",
        "custom_data.json",
    ]
    
    for path in write_enabled_paths:
        is_valid, error = manager.validate_file_path(path, OPERATION_WRITE)
        assert is_valid is True, f"Write operation on write-enabled path {path} should succeed"
        assert error is None


def test_write_paths_precedence_over_read_paths(hass: HomeAssistant):
    """Test write_paths takes precedence over read_paths (Task 12.3)."""
    from custom_components.ha_dev_tools.const import (
        SECURITY_MODE_ALLOWLIST,
        OPERATION_READ,
        OPERATION_WRITE,
    )
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/test.yaml",
        ],
        "write_paths": [
            "/config/test.yaml",  # Same path in both
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Both read and write operations should succeed (write_paths takes precedence)
    is_valid, error = manager.validate_file_path("test.yaml", OPERATION_READ)
    assert is_valid is True, "Read operation should succeed when path is in write_paths"
    assert error is None
    
    is_valid, error = manager.validate_file_path("test.yaml", OPERATION_WRITE)
    assert is_valid is True, "Write operation should succeed when path is in write_paths"
    assert error is None


def test_denied_paths_precedence_over_write_paths(hass: HomeAssistant):
    """Test denied_paths takes precedence over write_paths (Task 12.4)."""
    from custom_components.ha_dev_tools.const import (
        SECURITY_MODE_ALLOWLIST,
        OPERATION_READ,
        OPERATION_WRITE,
        ERROR_BLACKLISTED_FILE,
    )
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "write_paths": [
            "/config/secrets.yaml",  # Try to allow writes
        ],
        "denied_paths": [
            "/config/secrets.yaml",  # But also deny it
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Both read and write operations should be denied (denied_paths takes precedence)
    is_valid, error = manager.validate_file_path("secrets.yaml", OPERATION_READ)
    assert is_valid is False, "Read operation should be denied when path is in denied_paths"
    assert error == ERROR_BLACKLISTED_FILE
    
    is_valid, error = manager.validate_file_path("secrets.yaml", OPERATION_WRITE)
    assert is_valid is False, "Write operation should be denied when path is in denied_paths"
    assert error == ERROR_BLACKLISTED_FILE


def test_default_operation_type_is_read(hass: HomeAssistant):
    """Test validate_file_path defaults to read operation (Task 12.5)."""
    from custom_components.ha_dev_tools.const import (
        SECURITY_MODE_ALLOWLIST,
    )
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/configuration.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Call without operation parameter (should default to read)
    is_valid, error = manager.validate_file_path("configuration.yaml")
    assert is_valid is True, "Default operation should be read, which should succeed on read_paths"
    assert error is None


def test_recommended_read_only_configuration_yaml(hass: HomeAssistant):
    """Test configuration.yaml is read-only in recommended config (Task 12.6)."""
    from custom_components.ha_dev_tools.const import (
        SECURITY_MODE_ALLOWLIST,
        OPERATION_READ,
        OPERATION_WRITE,
        ERROR_WRITE_NOT_PERMITTED,
    )
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/configuration.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Read should succeed
    is_valid, error = manager.validate_file_path("configuration.yaml", OPERATION_READ)
    assert is_valid is True, "configuration.yaml should be readable"
    assert error is None
    
    # Write should be denied
    is_valid, error = manager.validate_file_path("configuration.yaml", OPERATION_WRITE)
    assert is_valid is False, "configuration.yaml should be read-only"
    assert error == ERROR_WRITE_NOT_PERMITTED


def test_recommended_read_only_automations_yaml(hass: HomeAssistant):
    """Test automations.yaml is read-only in recommended config (Task 12.6)."""
    from custom_components.ha_dev_tools.const import (
        SECURITY_MODE_ALLOWLIST,
        OPERATION_READ,
        OPERATION_WRITE,
        ERROR_WRITE_NOT_PERMITTED,
    )
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/automations.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Read should succeed
    is_valid, error = manager.validate_file_path("automations.yaml", OPERATION_READ)
    assert is_valid is True, "automations.yaml should be readable"
    assert error is None
    
    # Write should be denied
    is_valid, error = manager.validate_file_path("automations.yaml", OPERATION_WRITE)
    assert is_valid is False, "automations.yaml should be read-only"
    assert error == ERROR_WRITE_NOT_PERMITTED


def test_recommended_read_only_scripts_yaml(hass: HomeAssistant):
    """Test scripts.yaml is read-only in recommended config (Task 12.6)."""
    from custom_components.ha_dev_tools.const import (
        SECURITY_MODE_ALLOWLIST,
        OPERATION_READ,
        OPERATION_WRITE,
        ERROR_WRITE_NOT_PERMITTED,
    )
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/scripts.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Read should succeed
    is_valid, error = manager.validate_file_path("scripts.yaml", OPERATION_READ)
    assert is_valid is True, "scripts.yaml should be readable"
    assert error is None
    
    # Write should be denied
    is_valid, error = manager.validate_file_path("scripts.yaml", OPERATION_WRITE)
    assert is_valid is False, "scripts.yaml should be read-only"
    assert error == ERROR_WRITE_NOT_PERMITTED


def test_recommended_read_only_scenes_yaml(hass: HomeAssistant):
    """Test scenes.yaml is read-only in recommended config (Task 12.6)."""
    from custom_components.ha_dev_tools.const import (
        SECURITY_MODE_ALLOWLIST,
        OPERATION_READ,
        OPERATION_WRITE,
        ERROR_WRITE_NOT_PERMITTED,
    )
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/scenes.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Read should succeed
    is_valid, error = manager.validate_file_path("scenes.yaml", OPERATION_READ)
    assert is_valid is True, "scenes.yaml should be readable"
    assert error is None
    
    # Write should be denied
    is_valid, error = manager.validate_file_path("scenes.yaml", OPERATION_WRITE)
    assert is_valid is False, "scenes.yaml should be read-only"
    assert error == ERROR_WRITE_NOT_PERMITTED


def test_recommended_read_only_storage_files(hass: HomeAssistant):
    """Test .storage/* files are read-only in recommended config (Task 12.6)."""
    from custom_components.ha_dev_tools.const import (
        SECURITY_MODE_ALLOWLIST,
        OPERATION_READ,
        OPERATION_WRITE,
        ERROR_WRITE_NOT_PERMITTED,
    )
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/.storage/lovelace*",
            "/config/.storage/input_*",
            "/config/.storage/script",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Test various storage files
    storage_files = [
        ".storage/lovelace",
        ".storage/lovelace.dashboard_main",
        ".storage/input_boolean",
        ".storage/input_number",
        ".storage/script",
    ]
    
    for path in storage_files:
        # Read should succeed
        is_valid, error = manager.validate_file_path(path, OPERATION_READ)
        assert is_valid is True, f"{path} should be readable"
        assert error is None
        
        # Write should be denied
        is_valid, error = manager.validate_file_path(path, OPERATION_WRITE)
        assert is_valid is False, f"{path} should be read-only"
        assert error == ERROR_WRITE_NOT_PERMITTED


def test_recommended_read_only_packages_yaml_files(hass: HomeAssistant):
    """Test packages/**/*.yaml files are read-only in recommended config (Task 12.6)."""
    from custom_components.ha_dev_tools.const import (
        SECURITY_MODE_ALLOWLIST,
        OPERATION_READ,
        OPERATION_WRITE,
        ERROR_WRITE_NOT_PERMITTED,
    )
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/packages",  # Allow entire packages directory
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Test various package files
    package_files = [
        "packages/lights.yaml",
        "packages/sensors/temperature.yaml",
        "packages/automations/motion.yaml",
    ]
    
    for path in package_files:
        # Read should succeed
        is_valid, error = manager.validate_file_path(path, OPERATION_READ)
        assert is_valid is True, f"{path} should be readable"
        assert error is None
        
        # Write should be denied
        is_valid, error = manager.validate_file_path(path, OPERATION_WRITE)
        assert is_valid is False, f"{path} should be read-only"
        assert error == ERROR_WRITE_NOT_PERMITTED


def test_globstar_pattern_matches_direct_children_too(hass: HomeAssistant):
    """packages/**/*.yaml must match packages/x.yaml, not just packages/sub/x.yaml.

    Found via a real test against the actual recommended default pattern
    (DEFAULT_READ_ONLY_PATHS uses "/config/packages/**/*.yaml") against a
    package file placed directly in packages/ - e.g. packages/emhas.yaml,
    a real user's layout. Plain fnmatch.fnmatch requires the literal '/'
    between the two '*' groups to be present in the path, so it silently
    rejected exactly the common case the pattern's own docstring claims to
    support.
    """
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST

    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": ["/config/packages/**/*.yaml"],
    }
    manager = SecurityManager(hass, config=config)

    # Direct child of packages/ - the case that was broken.
    is_valid, error = manager.validate_file_path("packages/emhas.yaml")
    assert is_valid is True, f"packages/emhas.yaml should match **, got error={error}"

    # Nested child - already worked, must keep working.
    is_valid, error = manager.validate_file_path("packages/sub/emhas.yaml")
    assert is_valid is True

    # Outside packages/ entirely - must still be denied.
    is_valid, error = manager.validate_file_path("not_packages/emhas.yaml")
    assert is_valid is False


# ============================================================================
# Dynamic Modifications Tests (Task 13)
# ============================================================================

def test_add_to_allowlist_runtime(hass: HomeAssistant):
    """Test adding path to allowlist at runtime (Task 13.1)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/configuration.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Initially, test.yaml should not be in allowlist
    assert "/config/test.yaml" not in manager.allowlist
    
    # Add path at runtime
    manager.add_to_allowlist("/config/test.yaml")
    
    # Verify path is in allowlist
    assert "/config/test.yaml" in manager.allowlist
    
    # Verify subsequent validation allows access
    is_valid, error = manager.validate_file_path("test.yaml")
    assert is_valid is True, "Path added to allowlist should be accessible"
    assert error is None


def test_add_to_allowlist_with_glob_pattern(hass: HomeAssistant):
    """Test adding glob pattern to allowlist at runtime (Task 13.1)."""
    from custom_components.ha_dev_tools.const import SECURITY_MODE_ALLOWLIST
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/configuration.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Add glob pattern at runtime
    manager.add_to_allowlist("/config/custom_*.yaml")
    
    # Verify pattern is in allowlist
    assert "/config/custom_*.yaml" in manager.allowlist
    
    # Verify files matching the pattern are accessible
    is_valid, error = manager.validate_file_path("custom_lights.yaml")
    assert is_valid is True, "Files matching added glob pattern should be accessible"
    assert error is None
    
    is_valid, error = manager.validate_file_path("custom_sensors.yaml")
    assert is_valid is True, "Files matching added glob pattern should be accessible"
    assert error is None


def test_add_to_allowlist_invalid_path_rejected(hass: HomeAssistant, caplog):
    """Test that invalid paths are rejected when adding to allowlist (Task 13.1)."""
    import logging
    
    manager = SecurityManager(hass)
    
    # Try to add invalid path with path traversal
    with caplog.at_level(logging.WARNING):
        manager.add_to_allowlist("/config/../etc/passwd")
    
    # Should not be added to allowlist
    assert "/config/../etc/passwd" not in manager.allowlist
    
    # Should log warning
    assert "Cannot add invalid path to allowlist" in caplog.text
    assert "path traversal" in caplog.text.lower()


def test_add_to_allowlist_logging(hass: HomeAssistant, caplog):
    """Test that adding to allowlist is logged (Task 13.1)."""
    import logging
    
    manager = SecurityManager(hass)
    
    with caplog.at_level(logging.INFO):
        manager.add_to_allowlist("/config/test.yaml")
    
    # Should log the addition
    assert "Added /config/test.yaml to security allowlist" in caplog.text


def test_add_to_denylist_runtime(hass: HomeAssistant):
    """Test adding path to denylist at runtime (Task 13.2)."""
    from custom_components.ha_dev_tools.const import ERROR_BLACKLISTED_FILE
    
    manager = SecurityManager(hass)
    
    # Initially, test.yaml should not be in denylist
    assert "/config/test.yaml" not in manager.denylist
    
    # Add path at runtime
    manager.add_to_denylist("/config/test.yaml")
    
    # Verify path is in denylist
    assert "/config/test.yaml" in manager.denylist
    
    # Verify subsequent validation denies access
    is_valid, error = manager.validate_file_path("test.yaml")
    assert is_valid is False, "Path added to denylist should be denied"
    assert error == ERROR_BLACKLISTED_FILE


def test_add_to_denylist_with_glob_pattern(hass: HomeAssistant):
    """Test adding glob pattern to denylist at runtime (Task 13.2)."""
    from custom_components.ha_dev_tools.const import ERROR_BLACKLISTED_FILE
    
    manager = SecurityManager(hass)
    
    # Add glob pattern at runtime
    manager.add_to_denylist("/config/temp_*.log")
    
    # Verify pattern is in denylist
    assert "/config/temp_*.log" in manager.denylist
    
    # Verify files matching the pattern are denied
    is_valid, error = manager.validate_file_path("temp_debug.log")
    assert is_valid is False, "Files matching added glob pattern should be denied"
    assert error == ERROR_BLACKLISTED_FILE
    
    is_valid, error = manager.validate_file_path("temp_error.log")
    assert is_valid is False, "Files matching added glob pattern should be denied"
    assert error == ERROR_BLACKLISTED_FILE


def test_add_to_denylist_invalid_path_rejected(hass: HomeAssistant, caplog):
    """Test that invalid paths are rejected when adding to denylist (Task 13.2)."""
    import logging
    
    manager = SecurityManager(hass)
    
    # Try to add invalid path with path traversal
    with caplog.at_level(logging.WARNING):
        manager.add_to_denylist("/config/../etc/passwd")
    
    # Should not be added to denylist
    assert "/config/../etc/passwd" not in manager.denylist
    
    # Should log warning
    assert "Cannot add invalid path to denylist" in caplog.text
    assert "path traversal" in caplog.text.lower()


def test_add_to_denylist_logging(hass: HomeAssistant, caplog):
    """Test that adding to denylist is logged (Task 13.2)."""
    import logging
    
    manager = SecurityManager(hass)
    
    with caplog.at_level(logging.INFO):
        manager.add_to_denylist("/config/test.yaml")
    
    # Should log the addition
    assert "Added /config/test.yaml to security denylist" in caplog.text


def test_add_to_denylist_overrides_allowlist(hass: HomeAssistant):
    """Test that adding to denylist overrides allowlist (Task 13.2)."""
    from custom_components.ha_dev_tools.const import (
        SECURITY_MODE_ALLOWLIST,
        ERROR_BLACKLISTED_FILE,
    )
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/test.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Initially accessible (in allowlist)
    is_valid, error = manager.validate_file_path("test.yaml")
    assert is_valid is True
    assert error is None
    
    # Add to denylist at runtime
    manager.add_to_denylist("/config/test.yaml")
    
    # Now should be denied (denylist takes precedence)
    is_valid, error = manager.validate_file_path("test.yaml")
    assert is_valid is False, "Denylist should take precedence over allowlist"
    assert error == ERROR_BLACKLISTED_FILE


def test_remove_from_allowlist_runtime(hass: HomeAssistant):
    """Test removing path from allowlist at runtime (Task 13.3)."""
    from custom_components.ha_dev_tools.const import (
        SECURITY_MODE_ALLOWLIST,
        ERROR_PERMISSION_DENIED,
    )
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/test.yaml",
            "/config/configuration.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Initially, test.yaml should be in allowlist
    assert "/config/test.yaml" in manager.allowlist
    
    # Initially accessible
    is_valid, error = manager.validate_file_path("test.yaml")
    assert is_valid is True
    assert error is None
    
    # Remove path at runtime
    manager.remove_from_allowlist("/config/test.yaml")
    
    # Verify path is not in allowlist
    assert "/config/test.yaml" not in manager.allowlist
    
    # Verify subsequent validation denies access (in allowlist mode)
    is_valid, error = manager.validate_file_path("test.yaml")
    assert is_valid is False, "Path removed from allowlist should be denied in allowlist mode"
    assert error == ERROR_PERMISSION_DENIED


def test_remove_from_allowlist_logging(hass: HomeAssistant, caplog):
    """Test that removing from allowlist is logged (Task 13.3)."""
    import logging
    
    config = {
        "read_paths": [
            "/config/test.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    with caplog.at_level(logging.INFO):
        manager.remove_from_allowlist("/config/test.yaml")
    
    # Should log the removal
    assert "Removed /config/test.yaml from security allowlist" in caplog.text


def test_remove_from_allowlist_nonexistent_path(hass: HomeAssistant):
    """Test removing nonexistent path from allowlist (Task 13.3)."""
    manager = SecurityManager(hass)
    
    # Remove path that was never in allowlist (should not raise error)
    manager.remove_from_allowlist("/config/nonexistent.yaml")
    
    # Should complete without error
    assert "/config/nonexistent.yaml" not in manager.allowlist


def test_remove_from_denylist_runtime(hass: HomeAssistant):
    """Test removing path from denylist at runtime (Task 13.4)."""
    manager = SecurityManager(hass)
    
    # Add path to denylist first
    manager.add_to_denylist("/config/test.yaml")
    assert "/config/test.yaml" in manager.denylist
    
    # Remove path at runtime
    manager.remove_from_denylist("/config/test.yaml")
    
    # Verify path is not in denylist
    assert "/config/test.yaml" not in manager.denylist
    
    # In strict allowlist mode, removing from denylist is not enough
    # The path must also be in the allowlist to be accessible
    # Add to allowlist to make it accessible
    manager.add_to_allowlist("/config/test.yaml")
    
    # Now it should be accessible
    is_valid, error = manager.validate_file_path("test.yaml")
    assert is_valid is True, "Path removed from denylist and added to allowlist should be accessible"
    assert error is None


def test_remove_from_denylist_logging(hass: HomeAssistant, caplog):
    """Test that removing from denylist is logged (Task 13.4)."""
    import logging
    
    manager = SecurityManager(hass)
    
    # Add first
    manager.add_to_denylist("/config/test.yaml")
    
    with caplog.at_level(logging.INFO):
        manager.remove_from_denylist("/config/test.yaml")
    
    # Should log the removal
    assert "Removed /config/test.yaml from security denylist" in caplog.text


def test_remove_from_denylist_nonexistent_path(hass: HomeAssistant):
    """Test removing nonexistent path from denylist (Task 13.4)."""
    manager = SecurityManager(hass)
    
    # Remove path that was never in denylist (should not raise error)
    manager.remove_from_denylist("/config/nonexistent.yaml")
    
    # Should complete without error
    assert "/config/nonexistent.yaml" not in manager.denylist


def test_remove_from_denylist_default_sensitive_file(hass: HomeAssistant):
    """Test removing default sensitive file from denylist (Task 13.4)."""
    config = {
        "read_paths": ["/config/*.yaml"]  # Add allowlist config
    }
    manager = SecurityManager(hass, config=config)
    
    # secrets.yaml is in default denylist
    assert manager.is_denylisted("secrets.yaml") is True
    
    # Remove it from denylist
    manager.remove_from_denylist("secrets.yaml")
    
    # Should no longer be in denylist
    assert "secrets.yaml" not in manager.denylist
    
    # In strict allowlist mode, it's now accessible because:
    # 1. It's no longer in denylist
    # 2. It matches the /config/*.yaml pattern in read_paths
    is_valid, error = manager.validate_file_path("secrets.yaml")
    assert is_valid is True, "Removed default sensitive file should be accessible after removal when in allowlist"
    assert error is None


def test_get_allowlist_returns_current_allowlist(hass: HomeAssistant):
    """Test get_allowlist returns current allowlist (Task 13.5)."""
    config = {
        "read_paths": [
            "/config/configuration.yaml",
            "/config/automations.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Get allowlist
    allowlist = manager.get_allowlist()
    
    # Should contain configured paths
    assert "/config/configuration.yaml" in allowlist
    assert "/config/automations.yaml" in allowlist


def test_get_denylist_returns_current_denylist(hass: HomeAssistant):
    """Test get_denylist returns current denylist (Task 13.5)."""
    config = {
        "denied_paths": [
            "/config/custom_blocked.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Get denylist
    denylist = manager.get_denylist()
    
    # Should contain configured paths
    assert "/config/custom_blocked.yaml" in denylist
    
    # Should also contain default denylist items
    assert "secrets.yaml" in denylist
    assert ".HA_VERSION" in denylist


def test_get_allowlist_returns_copy_not_reference(hass: HomeAssistant):
    """Test get_allowlist returns a copy, not a reference (Task 13.5)."""
    config = {
        "read_paths": [
            "/config/configuration.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Get allowlist
    allowlist1 = manager.get_allowlist()
    
    # Modify the returned allowlist
    allowlist1.add("/config/test.yaml")
    
    # Get allowlist again
    allowlist2 = manager.get_allowlist()
    
    # Original allowlist should not be modified
    assert "/config/test.yaml" not in allowlist2, "Modifying returned allowlist should not affect original"
    assert "/config/test.yaml" not in manager.allowlist, "Modifying returned allowlist should not affect original"


def test_get_denylist_returns_copy_not_reference(hass: HomeAssistant):
    """Test get_denylist returns a copy, not a reference (Task 13.5)."""
    manager = SecurityManager(hass)
    
    # Get denylist
    denylist1 = manager.get_denylist()
    
    # Modify the returned denylist
    denylist1.add("/config/test.yaml")
    
    # Get denylist again
    denylist2 = manager.get_denylist()
    
    # Original denylist should not be modified
    assert "/config/test.yaml" not in denylist2, "Modifying returned denylist should not affect original"
    assert "/config/test.yaml" not in manager.denylist, "Modifying returned denylist should not affect original"


def test_query_methods_reflect_runtime_modifications(hass: HomeAssistant):
    """Test query methods reflect runtime modifications (Task 13.5)."""
    manager = SecurityManager(hass)
    
    # Add to allowlist
    manager.add_to_allowlist("/config/test1.yaml")
    
    # Add to denylist
    manager.add_to_denylist("/config/test2.yaml")
    
    # Query methods should reflect the changes
    allowlist = manager.get_allowlist()
    denylist = manager.get_denylist()
    
    assert "/config/test1.yaml" in allowlist, "get_allowlist should reflect runtime additions"
    assert "/config/test2.yaml" in denylist, "get_denylist should reflect runtime additions"
    
    # Remove from allowlist
    manager.remove_from_allowlist("/config/test1.yaml")
    
    # Remove from denylist
    manager.remove_from_denylist("/config/test2.yaml")
    
    # Query methods should reflect the removals
    allowlist = manager.get_allowlist()
    denylist = manager.get_denylist()
    
    assert "/config/test1.yaml" not in allowlist, "get_allowlist should reflect runtime removals"
    assert "/config/test2.yaml" not in denylist, "get_denylist should reflect runtime removals"


def test_dynamic_modifications_with_validation(hass: HomeAssistant):
    """Test dynamic modifications affect validation immediately (Task 13.1, 13.2)."""
    from custom_components.ha_dev_tools.const import (
        SECURITY_MODE_ALLOWLIST,
        ERROR_PERMISSION_DENIED,
        ERROR_BLACKLISTED_FILE,
    )
    
    config = {
        "mode": SECURITY_MODE_ALLOWLIST,
        "read_paths": [
            "/config/configuration.yaml",
        ]
    }
    
    manager = SecurityManager(hass, config=config)
    
    # Initially, test.yaml should be denied (not in allowlist)
    is_valid, error = manager.validate_file_path("test.yaml")
    assert is_valid is False
    assert error == ERROR_PERMISSION_DENIED
    
    # Add to allowlist
    manager.add_to_allowlist("/config/test.yaml")
    
    # Now should be allowed
    is_valid, error = manager.validate_file_path("test.yaml")
    assert is_valid is True
    assert error is None
    
    # Add to denylist (should override allowlist)
    manager.add_to_denylist("/config/test.yaml")
    
    # Now should be denied (denylist precedence)
    is_valid, error = manager.validate_file_path("test.yaml")
    assert is_valid is False
    assert error == ERROR_BLACKLISTED_FILE
    
    # Remove from denylist
    manager.remove_from_denylist("/config/test.yaml")
    
    # Should be allowed again (still in allowlist)
    is_valid, error = manager.validate_file_path("test.yaml")
    assert is_valid is True
    assert error is None
    
    # Remove from allowlist
    manager.remove_from_allowlist("/config/test.yaml")
    
    # Should be denied again (not in allowlist)
    is_valid, error = manager.validate_file_path("test.yaml")
    assert is_valid is False
    assert error == ERROR_PERMISSION_DENIED
