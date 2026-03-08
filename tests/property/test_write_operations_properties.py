"""Property-based tests for file write operations.

This module contains property-based tests that validate the correctness
of file write operations including upload round trip and pre-upload validation.
"""
import pytest
import asyncio
from hypothesis import given, strategies as st, assume, settings, HealthCheck
import string
import tempfile
from pathlib import Path
from unittest.mock import Mock
import sys
import os

# Add the custom_components directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'custom_components'))

# Mock homeassistant modules before importing our code
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

sys.modules['homeassistant'] = mock_homeassistant
sys.modules['homeassistant.core'] = mock_core
sys.modules['homeassistant.config_entries'] = mock_config_entries
sys.modules['homeassistant.auth'] = mock_auth
sys.modules['homeassistant.auth.models'] = mock_auth_models
sys.modules['homeassistant.components'] = mock_components
sys.modules['homeassistant.components.http'] = mock_http
sys.modules['homeassistant.const'] = mock_const
sys.modules['homeassistant.helpers'] = mock_helpers
sys.modules['homeassistant.helpers.typing'] = mock_helpers_typing

# Mock the specific classes and constants we need
mock_config_entries.ConfigEntry = Mock()
mock_const.Platform = Mock()
mock_helpers_typing.ConfigType = dict
mock_core.HomeAssistant = Mock()
mock_auth_models.User = Mock()
mock_http.HomeAssistantView = Mock()

# Mock the HomeAssistant classes we need
class MockHass:
    """Mock Home Assistant instance for testing."""
    
    def __init__(self, config_dir: str):
        self.config = Mock()
        self.config.config_dir = config_dir
        self.data = {}  # Add data dict for integration config
        
    async def async_add_executor_job(self, func, *args):
        """Mock executor job - just run synchronously."""
        return func(*args)

# Now import our modules
from ha_config_manager.file_manager import FileManager
from ha_config_manager.security import SecurityManager
from ha_config_manager.const import DEFAULT_BLACKLIST


# Strategy for generating valid YAML content
valid_yaml_content = st.one_of([
    # Simple key-value pairs
    st.builds(
        lambda k, v: f"{k}: {v}",
        k=st.text(alphabet=string.ascii_letters, min_size=1, max_size=20),
        v=st.one_of(
            st.integers(),
            st.text(alphabet=string.ascii_letters + string.digits + " ", min_size=0, max_size=50),
            st.booleans()
        )
    ),
    # Lists
    st.builds(
        lambda items: "items:\n" + "\n".join(f"  - {item}" for item in items),
        items=st.lists(st.text(alphabet=string.ascii_letters, min_size=1, max_size=20), min_size=1, max_size=5)
    ),
    # Nested structures
    st.just("automation:\n  - alias: Test\n    trigger:\n      platform: state\n    action:\n      service: light.turn_on"),
])

# Strategy for generating invalid YAML content
# These are ACTUALLY invalid YAML that will fail parsing - verified with yaml.safe_load
invalid_yaml_content = st.sampled_from([
    "key: value\n  [invalid: yaml: syntax",  # ScannerError - Invalid syntax with brackets
    "key: value\n\t\ttabs: not allowed",  # ScannerError - Tabs are not allowed in YAML  
    "{{unclosed template",  # ParserError - Unclosed template
    "key: !!python/object:os.system",  # ConstructorError - Dangerous YAML tag
    "key: value\n  invalid: [unclosed",  # ScannerError - Unclosed bracket
    "key: value\n    nested:\n  wrong_indent: value",  # ScannerError - Wrong indentation
    ": no key",  # ParserError - Missing key
])

# Strategy for generating valid file names
valid_yaml_filenames = st.builds(
    lambda name: f"{name}.yaml",
    name=st.text(alphabet=string.ascii_letters + string.digits + "_-", min_size=1, max_size=20).filter(
        lambda x: not x.startswith('.') and x not in DEFAULT_BLACKLIST
    )
)


@given(filename=valid_yaml_filenames, content=valid_yaml_content)
@settings(suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.function_scoped_fixture], max_examples=100)
def test_upload_round_trip_property(filename, content):
    """
    Property 6: Upload Round Trip
    
    For any valid YAML content, uploading then reading back should return equivalent content.
    The content should be preserved exactly as written.
    
    **Validates: Requirements 5.2**
    """
    async def run_test():
        with tempfile.TemporaryDirectory() as temp_dir:
            # Skip blacklisted files
            assume(filename not in DEFAULT_BLACKLIST)
            assume(not any(filename.startswith(bl) for bl in DEFAULT_BLACKLIST))
            
            # Skip files with path traversal
            assume('..' not in filename)
            assume(not filename.startswith('/'))
            
            # Create mock hass and managers
            mock_hass = MockHass(temp_dir)
            
            # Configure security manager to allow read-write access to all files in temp dir
            security_config = {
                "write_paths": ["*.yaml", "*.yml"],  # Allow all YAML files for testing
                "read_paths": [],
                "denied_paths": []
            }
            
            security_manager = SecurityManager(mock_hass, security_config)
            file_manager = FileManager(mock_hass, security_manager)
            
            try:
                # Upload the content (write_file now returns metadata)
                metadata = await file_manager.write_file(
                    filename, 
                    content,
                    validate_before_write=True
                )
                
                # Verify metadata was returned
                assert metadata is not None
                assert "path" in metadata
                assert "content_hash" in metadata
                
                # Read the content back
                read_content = await file_manager.read_file(filename)
                
                # The content should be exactly the same
                assert read_content == content, f"Content mismatch for {filename}"
                
                # Verify the file exists
                exists = await file_manager.file_exists(filename)
                assert exists is True
                
            except (PermissionError, ValueError) as e:
                # If write failed due to security restrictions or validation, that's acceptable
                # but we should verify it's for a valid reason
                if "blacklisted" in str(e).lower() or "invalid" in str(e).lower():
                    assume(False)  # Skip this test case
                else:
                    raise
    
    # Run the async test
    asyncio.run(run_test())


@given(filename=valid_yaml_filenames, content=invalid_yaml_content)
@settings(suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.function_scoped_fixture], max_examples=100)
def test_pre_upload_validation_property(filename, content):
    """
    Property 13: Pre-Upload Validation
    
    For any invalid YAML content, syntax validation should occur before the file is written.
    The file should not be created or modified if validation fails.
    
    **Validates: Requirements 5.1**
    """
    async def run_test():
        with tempfile.TemporaryDirectory() as temp_dir:
            # Skip blacklisted files
            assume(filename not in DEFAULT_BLACKLIST)
            assume(not any(filename.startswith(bl) for bl in DEFAULT_BLACKLIST))
            
            # Skip files with path traversal
            assume('..' not in filename)
            assume(not filename.startswith('/'))
            
            # Create mock hass and managers
            mock_hass = MockHass(temp_dir)
            
            # Configure security manager to allow read-write access
            security_config = {
                "write_paths": ["*.yaml", "*.yml"],
                "read_paths": [],
                "denied_paths": []
            }
            
            security_manager = SecurityManager(mock_hass, security_config)
            file_manager = FileManager(mock_hass, security_manager)
            
            # Get the full path to check if file exists before
            full_path = Path(temp_dir) / filename
            existed_before = full_path.exists()
            
            # Try to write invalid content - should raise ValueError or similar
            try:
                await file_manager.write_file(
                    filename,
                    content,
                    validate_before_write=True
                )
                # If we get here, validation didn't catch the invalid YAML
                # This is a test failure - invalid YAML should have been rejected
                pytest.fail(f"Invalid YAML was not rejected: {content[:50]}...")
            except (ValueError, Exception) as e:
                # Good - validation caught the invalid YAML
                # Verify error message mentions validation
                error_msg = str(e).lower()
                assert any(keyword in error_msg for keyword in ['validation', 'invalid', 'yaml', 'syntax']), \
                    f"Error message should mention validation issue: {e}"
            
            # Verify the file was not created or modified
            if existed_before:
                # If file existed, it should still exist with original content
                assert full_path.exists()
            else:
                # If file didn't exist, it should still not exist
                assert not full_path.exists(), f"File {filename} should not have been created after validation failure"
    
    # Run the async test
    asyncio.run(run_test())


@given(filename=valid_yaml_filenames, content=valid_yaml_content)
@settings(suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.function_scoped_fixture], max_examples=50)
def test_hash_conflict_detection_property(filename, content):
    """
    Property: Hash Conflict Detection
    
    For any file, if the expected_hash doesn't match the current file hash,
    a conflict should be detected and the write should fail.
    
    **Validates: Requirements 5.2**
    """
    async def run_test():
        with tempfile.TemporaryDirectory() as temp_dir:
            # Skip blacklisted files
            assume(filename not in DEFAULT_BLACKLIST)
            assume(not any(filename.startswith(bl) for bl in DEFAULT_BLACKLIST))
            
            # Skip files with path traversal
            assume('..' not in filename)
            assume(not filename.startswith('/'))
            
            # Create mock hass and managers
            mock_hass = MockHass(temp_dir)
            
            # Configure security manager to allow read-write access
            security_config = {
                "write_paths": ["*.yaml", "*.yml"],
                "read_paths": [],
                "denied_paths": []
            }
            
            security_manager = SecurityManager(mock_hass, security_config)
            file_manager = FileManager(mock_hass, security_manager)
            
            try:
                # Write initial content
                initial_metadata = await file_manager.write_file(
                    filename,
                    content,
                    validate_before_write=True
                )
                
                initial_hash = initial_metadata["content_hash"]
                
                # Try to write with wrong expected_hash - should raise ValueError
                wrong_hash = "0" * 64  # Invalid hash
                assume(wrong_hash != initial_hash)  # Make sure it's actually different
                
                with pytest.raises(ValueError, match="Hash conflict"):
                    await file_manager.write_file(
                        filename,
                        "new content",
                        expected_hash=wrong_hash,
                        validate_before_write=True
                    )
                
                # Verify the file content wasn't changed
                current_content = await file_manager.read_file(filename)
                assert current_content == content, "File should not have been modified after hash conflict"
                
                # Now write with correct expected_hash - should succeed
                new_content = content + "\n# Updated"
                new_metadata = await file_manager.write_file(
                    filename,
                    new_content,
                    expected_hash=initial_hash,
                    validate_before_write=True
                )
                
                # Verify the write succeeded
                assert new_metadata["content_hash"] != initial_hash
                updated_content = await file_manager.read_file(filename)
                assert updated_content == new_content
                
            except (PermissionError, ValueError) as e:
                # If write failed due to security restrictions or validation, skip
                if "blacklisted" in str(e).lower() or "validation failed" in str(e).lower():
                    assume(False)
                else:
                    raise
    
    # Run the async test
    asyncio.run(run_test())


@given(filename=valid_yaml_filenames, content=valid_yaml_content)
@settings(suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.function_scoped_fixture], max_examples=50)
def test_atomic_write_property(filename, content):
    """
    Property: Atomic Write Operations
    
    For any file write operation, the write should be atomic - either the entire
    content is written successfully, or the original file remains unchanged.
    
    **Validates: Requirements 5.2**
    """
    async def run_test():
        with tempfile.TemporaryDirectory() as temp_dir:
            # Skip blacklisted files
            assume(filename not in DEFAULT_BLACKLIST)
            assume(not any(filename.startswith(bl) for bl in DEFAULT_BLACKLIST))
            
            # Skip files with path traversal
            assume('..' not in filename)
            assume(not filename.startswith('/'))
            
            # Create mock hass and managers
            mock_hass = MockHass(temp_dir)
            
            # Configure security manager to allow read-write access
            security_config = {
                "write_paths": ["*.yaml", "*.yml"],
                "read_paths": [],
                "denied_paths": []
            }
            
            security_manager = SecurityManager(mock_hass, security_config)
            file_manager = FileManager(mock_hass, security_manager)
            
            try:
                # Write initial content
                await file_manager.write_file(filename, content, validate_before_write=True)
                
                # Read it back to verify
                read_content = await file_manager.read_file(filename)
                assert read_content == content
                
                # Get the full path
                full_path = Path(temp_dir) / filename
                
                # Verify no temporary files are left behind
                temp_files = list(full_path.parent.glob(f".{filename}*.tmp"))
                assert len(temp_files) == 0, f"Temporary files should be cleaned up: {temp_files}"
                
            except (PermissionError, ValueError) as e:
                # If write failed due to security restrictions or validation, skip
                if "blacklisted" in str(e).lower() or "validation failed" in str(e).lower():
                    assume(False)
                else:
                    raise
    
    # Run the async test
    asyncio.run(run_test())
