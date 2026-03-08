"""Property-based tests for SecurityManager read-write access control.

This module contains property-based tests that validate the correctness
of read-only vs read-write access control in the SecurityManager.
"""
import pytest
from hypothesis import given, strategies as st, assume, settings, HealthCheck
from unittest.mock import Mock
import string

from custom_components.ha_config_manager.security import SecurityManager
from custom_components.ha_config_manager.const import (
    OPERATION_READ,
    OPERATION_WRITE,
)


# Strategy for generating valid path strings (no .. or leading /)
def valid_path_string():
    """Generate valid path strings without path traversal or absolute paths."""
    # Generate path components (alphanumeric + underscore + hyphen)
    # Avoid dots in path components to prevent extension issues
    path_chars = string.ascii_letters + string.digits + "_-"
    
    # Valid extensions that will pass security validation
    valid_extensions = [".yaml", ".yml", ".json", ".txt", ".py", ".jinja2", ""]
    
    # Generate 1-3 path components separated by /
    def make_path(parts, ext):
        # Join parts and add extension to the last component
        if len(parts) == 1:
            path = parts[0] + ext
        else:
            path = "/".join(parts[:-1]) + "/" + parts[-1] + ext
        
        # Filter out paths containing ".." (path traversal)
        if ".." in path:
            assume(False)
        return path
    
    return st.builds(
        make_path,
        parts=st.lists(
            st.text(alphabet=path_chars, min_size=1, max_size=20).filter(
                lambda s: ".." not in s  # Exclude ".." in individual components
            ),
            min_size=1,
            max_size=3
        ),
        ext=st.sampled_from(valid_extensions)
    )


@given(
    read_only_paths=st.lists(valid_path_string(), min_size=1, max_size=10)
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=100,
    deadline=5000
)
def test_property_20_read_only_path_enforcement(read_only_paths):
    """
    Feature: configurable-security-allowlist, Property 20: Read-Only Path Enforcement
    
    For any path in read_paths configuration, read operations should succeed 
    and write operations should be denied.
    
    **Validates: Requirements 11.1, 11.6**
    """
    # Create mock Home Assistant instance
    mock_hass = Mock()
    mock_hass.config.config_dir = "/config"
    
    # Create configuration with read-only paths
    config = {
        "read_paths": read_only_paths,
        "write_paths": [],  # No write paths
        "denied_paths": [],  # No denied paths
    }
    
    # Initialize SecurityManager
    security_manager = SecurityManager(mock_hass, config)
    
    # Test each read-only path
    for path in read_only_paths:
        # Read operations should succeed
        is_valid_read, error_message_read = security_manager.validate_file_path(
            path, OPERATION_READ
        )
        assert is_valid_read is True, \
            f"Read operation on read-only path '{path}' was incorrectly denied"
        
        # Write operations should be denied
        is_valid_write, error_message_write = security_manager.validate_file_path(
            path, OPERATION_WRITE
        )
        assert is_valid_write is False, \
            f"Write operation on read-only path '{path}' was incorrectly allowed"
        assert error_message_write is not None, \
            f"Denied write operation should have an error message"


@given(
    write_paths=st.lists(valid_path_string(), min_size=1, max_size=10)
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=100,
    deadline=5000
)
def test_property_21_write_path_permissions(write_paths):
    """
    Feature: configurable-security-allowlist, Property 21: Write Path Permissions
    
    For any path in write_paths configuration, both read and write operations 
    should succeed.
    
    **Validates: Requirements 11.2**
    """
    # Create mock Home Assistant instance
    mock_hass = Mock()
    mock_hass.config.config_dir = "/config"
    
    # Create configuration with write-enabled paths
    config = {
        "read_paths": [],  # No read-only paths
        "write_paths": write_paths,
        "denied_paths": [],  # No denied paths
    }
    
    # Initialize SecurityManager
    security_manager = SecurityManager(mock_hass, config)
    
    # Test each write-enabled path
    for path in write_paths:
        # Read operations should succeed
        is_valid_read, error_message_read = security_manager.validate_file_path(
            path, OPERATION_READ
        )
        assert is_valid_read is True, \
            f"Read operation on write-enabled path '{path}' was incorrectly denied"
        
        # Write operations should succeed
        is_valid_write, error_message_write = security_manager.validate_file_path(
            path, OPERATION_WRITE
        )
        assert is_valid_write is True, \
            f"Write operation on write-enabled path '{path}' was incorrectly denied"


@given(
    common_paths=st.lists(valid_path_string(), min_size=1, max_size=10)
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=100,
    deadline=5000
)
def test_property_22_write_path_precedence(common_paths):
    """
    Feature: configurable-security-allowlist, Property 22: Write Path Precedence
    
    For any path that appears in both read_paths and write_paths, write operations 
    should succeed (write_paths takes precedence).
    
    **Validates: Requirements 11.3**
    """
    # Create mock Home Assistant instance
    mock_hass = Mock()
    mock_hass.config.config_dir = "/config"
    
    # Create configuration with paths in both read_paths and write_paths
    config = {
        "read_paths": common_paths,
        "write_paths": common_paths,  # Same paths in both lists
        "denied_paths": [],  # No denied paths
    }
    
    # Initialize SecurityManager
    security_manager = SecurityManager(mock_hass, config)
    
    # Test each common path
    for path in common_paths:
        # Read operations should succeed
        is_valid_read, error_message_read = security_manager.validate_file_path(
            path, OPERATION_READ
        )
        assert is_valid_read is True, \
            f"Read operation on path '{path}' in both lists was incorrectly denied"
        
        # Write operations should succeed (write_paths takes precedence)
        is_valid_write, error_message_write = security_manager.validate_file_path(
            path, OPERATION_WRITE
        )
        assert is_valid_write is True, \
            f"Write operation on path '{path}' in both read_paths and write_paths was incorrectly denied"


@given(
    common_paths=st.lists(valid_path_string(), min_size=1, max_size=10)
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=100,
    deadline=5000
)
def test_property_23_denied_path_supremacy(common_paths):
    """
    Feature: configurable-security-allowlist, Property 23: Denied Path Supremacy
    
    For any path in denied_paths, all operations (read and write) should be denied 
    regardless of whether the path is also in read_paths or write_paths.
    
    **Validates: Requirements 11.4**
    """
    # Create mock Home Assistant instance
    mock_hass = Mock()
    mock_hass.config.config_dir = "/config"
    
    # Create configuration with paths in denied_paths AND write_paths
    config = {
        "read_paths": common_paths,
        "write_paths": common_paths,
        "denied_paths": common_paths,  # Same paths in all lists
    }
    
    # Initialize SecurityManager
    security_manager = SecurityManager(mock_hass, config)
    
    # Test each common path
    for path in common_paths:
        # Read operations should be denied (denied_paths takes precedence)
        is_valid_read, error_message_read = security_manager.validate_file_path(
            path, OPERATION_READ
        )
        assert is_valid_read is False, \
            f"Read operation on denied path '{path}' was incorrectly allowed"
        assert error_message_read is not None, \
            f"Denied read operation should have an error message"
        
        # Write operations should be denied (denied_paths takes precedence)
        is_valid_write, error_message_write = security_manager.validate_file_path(
            path, OPERATION_WRITE
        )
        assert is_valid_write is False, \
            f"Write operation on denied path '{path}' was incorrectly allowed"
        assert error_message_write is not None, \
            f"Denied write operation should have an error message"
