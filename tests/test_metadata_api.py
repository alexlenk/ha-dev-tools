"""Unit tests for metadata API endpoints.

This module contains unit tests for the metadata API endpoints including:
- GET /api/ha_config_manager/metadata/{path} - Single file metadata
- POST /api/ha_config_manager/metadata/batch - Batch metadata retrieval

These tests validate the API layer behavior including error handling,
security constraints, and response formats.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch
from aiohttp import web
import json
import sys
import os

# Add the custom_components directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'custom_components'))

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

# Create a proper base class for views instead of mocking
class MockHomeAssistantView:
    """Mock base class for HomeAssistantView."""
    url = ""
    name = ""
    requires_auth = True

mock_http.HomeAssistantView = MockHomeAssistantView

# Now import our modules
from ha_config_manager.api import ManagementAPIHandler, MetadataAPIView, BatchMetadataAPIView
from ha_config_manager.security import SecurityManager
from ha_config_manager.file_manager import FileManager


class MockRequest:
    """Mock aiohttp request for testing."""
    
    def __init__(self, user=None, query_params=None, json_data=None):
        self.user = user
        self.query = query_params or {}
        self._json_data = json_data
        
    def get(self, key, default=None):
        """Mock request.get() for user access."""
        if key == "hass_user":
            return self.user
        return default
    
    async def json(self):
        """Mock request.json() for body parsing."""
        if self._json_data is None:
            raise json.JSONDecodeError("Invalid JSON", "", 0)
        return self._json_data


@pytest.mark.asyncio
async def test_metadata_endpoint_single_file_success():
    """
    Test single file metadata retrieval with valid file.
    **Validates: Requirements 7.1**
    """
    # Setup mocks
    mock_hass = Mock()
    mock_hass.config = Mock()
    mock_hass.config.config_dir = "/config"
    
    mock_security = Mock(spec=SecurityManager)
    mock_security.validate_user_permissions = Mock(return_value=(True, None))
    
    # Create API handler
    api_handler = ManagementAPIHandler(mock_hass, mock_security)
    
    # Mock file manager to return metadata
    expected_metadata = {
        "path": "configuration.yaml",
        "size": 1024,
        "modified_at": "2026-02-12T10:30:00Z",
        "content_hash": "a" * 64,
        "exists": True,
        "accessible": True
    }
    api_handler.file_manager.get_file_metadata = AsyncMock(return_value=expected_metadata)
    
    # Create view and request
    view = MetadataAPIView(api_handler)
    mock_user = Mock()
    request = MockRequest(user=mock_user)
    
    # Execute request
    response = await view.get(request, "configuration.yaml")
    
    # Verify response
    assert response.status == 200
    response_data = json.loads(response.body)
    assert response_data == expected_metadata
    assert response_data["path"] == "configuration.yaml"
    assert response_data["exists"] is True
    assert response_data["accessible"] is True


@pytest.mark.asyncio
async def test_metadata_endpoint_nonexistent_file():
    """
    Test metadata retrieval for non-existent file (404).
    **Validates: Requirements 7.5**
    """
    # Setup mocks
    mock_hass = Mock()
    mock_hass.config = Mock()
    mock_hass.config.config_dir = "/config"
    
    mock_security = Mock(spec=SecurityManager)
    mock_security.validate_user_permissions = Mock(return_value=(True, None))
    
    # Create API handler
    api_handler = ManagementAPIHandler(mock_hass, mock_security)
    
    # Mock file manager to raise FileNotFoundError
    api_handler.file_manager.get_file_metadata = AsyncMock(
        side_effect=FileNotFoundError("File not found: nonexistent.yaml")
    )
    
    # Create view and request
    view = MetadataAPIView(api_handler)
    mock_user = Mock()
    request = MockRequest(user=mock_user)
    
    # Execute request
    response = await view.get(request, "nonexistent.yaml")
    
    # Verify response
    assert response.status == 404
    response_data = json.loads(response.body)
    assert response_data["success"] is False
    assert "error" in response_data
    assert "not found" in response_data["error"].lower()


@pytest.mark.asyncio
async def test_metadata_endpoint_blocked_file():
    """
    Test metadata retrieval for blocked file (403).
    **Validates: Requirements 7.5**
    """
    # Setup mocks
    mock_hass = Mock()
    mock_hass.config = Mock()
    mock_hass.config.config_dir = "/config"
    
    mock_security = Mock(spec=SecurityManager)
    mock_security.validate_user_permissions = Mock(return_value=(True, None))
    
    # Create API handler
    api_handler = ManagementAPIHandler(mock_hass, mock_security)
    
    # Mock file manager to raise PermissionError
    api_handler.file_manager.get_file_metadata = AsyncMock(
        side_effect=PermissionError("Access to blacklisted file denied: secrets.yaml")
    )
    
    # Create view and request
    view = MetadataAPIView(api_handler)
    mock_user = Mock()
    request = MockRequest(user=mock_user)
    
    # Execute request
    response = await view.get(request, "secrets.yaml")
    
    # Verify response
    assert response.status == 403
    response_data = json.loads(response.body)
    assert response_data["success"] is False
    assert "error" in response_data
    assert "blacklisted" in response_data["error"].lower() or "denied" in response_data["error"].lower()


@pytest.mark.asyncio
async def test_metadata_endpoint_unauthorized():
    """
    Test metadata retrieval without authentication (401).
    **Validates: Requirements 7.1**
    """
    # Setup mocks
    mock_hass = Mock()
    mock_hass.config = Mock()
    mock_hass.config.config_dir = "/config"
    
    mock_security = Mock(spec=SecurityManager)
    mock_security.validate_user_permissions = Mock(return_value=(False, "Authentication required"))
    
    # Create API handler
    api_handler = ManagementAPIHandler(mock_hass, mock_security)
    
    # Create view and request (no user)
    view = MetadataAPIView(api_handler)
    request = MockRequest(user=None)
    
    # Execute request
    response = await view.get(request, "configuration.yaml")
    
    # Verify response
    assert response.status == 401
    response_data = json.loads(response.body)
    assert response_data["success"] is False
    assert "error" in response_data


@pytest.mark.asyncio
async def test_batch_metadata_endpoint_success():
    """
    Test batch metadata retrieval with multiple valid files.
    **Validates: Requirements 7.3**
    """
    # Setup mocks
    mock_hass = Mock()
    mock_hass.config = Mock()
    mock_hass.config.config_dir = "/config"
    
    mock_security = Mock(spec=SecurityManager)
    mock_security.validate_user_permissions = Mock(return_value=(True, None))
    
    # Create API handler
    api_handler = ManagementAPIHandler(mock_hass, mock_security)
    
    # Mock file manager to return metadata for each file
    async def mock_get_metadata(file_path):
        return {
            "path": file_path,
            "size": 1024,
            "modified_at": "2026-02-12T10:30:00Z",
            "content_hash": "a" * 64,
            "exists": True,
            "accessible": True
        }
    
    api_handler.file_manager.get_file_metadata = AsyncMock(side_effect=mock_get_metadata)
    
    # Create view and request
    view = BatchMetadataAPIView(api_handler)
    mock_user = Mock()
    request = MockRequest(
        user=mock_user,
        json_data={"file_paths": ["configuration.yaml", "automations.yaml", "scripts.yaml"]}
    )
    
    # Execute request
    response = await view.post(request)
    
    # Verify response
    assert response.status == 200
    response_data = json.loads(response.body)
    assert "metadata" in response_data
    assert "total" in response_data
    assert response_data["total"] == 3
    assert len(response_data["metadata"]) == 3
    
    # Verify each file's metadata
    paths = [m["path"] for m in response_data["metadata"]]
    assert "configuration.yaml" in paths
    assert "automations.yaml" in paths
    assert "scripts.yaml" in paths


@pytest.mark.asyncio
async def test_batch_metadata_endpoint_mixed_results():
    """
    Test batch metadata with mixed valid/invalid files.
    **Validates: Requirements 7.3**
    """
    # Setup mocks
    mock_hass = Mock()
    mock_hass.config = Mock()
    mock_hass.config.config_dir = "/config"
    
    mock_security = Mock(spec=SecurityManager)
    mock_security.validate_user_permissions = Mock(return_value=(True, None))
    
    # Create API handler
    api_handler = ManagementAPIHandler(mock_hass, mock_security)
    
    # Mock file manager to return metadata or error based on file
    async def mock_get_metadata(file_path):
        if file_path == "configuration.yaml":
            return {
                "path": file_path,
                "size": 1024,
                "modified_at": "2026-02-12T10:30:00Z",
                "content_hash": "a" * 64,
                "exists": True,
                "accessible": True
            }
        elif file_path == "nonexistent.yaml":
            raise FileNotFoundError(f"File not found: {file_path}")
        elif file_path == "secrets.yaml":
            raise PermissionError(f"Access denied: {file_path}")
        else:
            return {
                "path": file_path,
                "exists": False,
                "accessible": True
            }
    
    api_handler.file_manager.get_file_metadata = AsyncMock(side_effect=mock_get_metadata)
    
    # Create view and request
    view = BatchMetadataAPIView(api_handler)
    mock_user = Mock()
    request = MockRequest(
        user=mock_user,
        json_data={"file_paths": ["configuration.yaml", "nonexistent.yaml", "secrets.yaml"]}
    )
    
    # Execute request
    response = await view.post(request)
    
    # Verify response - batch operations should not fail entirely
    assert response.status == 200
    response_data = json.loads(response.body)
    assert "metadata" in response_data
    assert response_data["total"] == 3
    
    # Verify results include errors for failed files
    results = response_data["metadata"]
    assert len(results) == 3
    
    # Check that error files have error field
    error_files = [r for r in results if "error" in r]
    assert len(error_files) == 2  # nonexistent.yaml and secrets.yaml


@pytest.mark.asyncio
async def test_batch_metadata_endpoint_empty_list():
    """
    Test batch metadata with empty file list.
    **Validates: Requirements 7.3**
    """
    # Setup mocks
    mock_hass = Mock()
    mock_hass.config = Mock()
    mock_hass.config.config_dir = "/config"
    
    mock_security = Mock(spec=SecurityManager)
    mock_security.validate_user_permissions = Mock(return_value=(True, None))
    
    # Create API handler
    api_handler = ManagementAPIHandler(mock_hass, mock_security)
    
    # Create view and request
    view = BatchMetadataAPIView(api_handler)
    mock_user = Mock()
    request = MockRequest(
        user=mock_user,
        json_data={"file_paths": []}
    )
    
    # Execute request
    response = await view.post(request)
    
    # Verify response
    assert response.status == 200
    response_data = json.loads(response.body)
    assert "metadata" in response_data
    assert response_data["total"] == 0
    assert len(response_data["metadata"]) == 0


@pytest.mark.asyncio
async def test_batch_metadata_endpoint_size_limit():
    """
    Test batch metadata with more than 20 files (should fail).
    **Validates: Requirements 7.3**
    """
    # Setup mocks
    mock_hass = Mock()
    mock_hass.config = Mock()
    mock_hass.config.config_dir = "/config"
    
    mock_security = Mock(spec=SecurityManager)
    mock_security.validate_user_permissions = Mock(return_value=(True, None))
    
    # Create API handler
    api_handler = ManagementAPIHandler(mock_hass, mock_security)
    
    # Create view and request with 21 files
    view = BatchMetadataAPIView(api_handler)
    mock_user = Mock()
    file_paths = [f"file{i}.yaml" for i in range(21)]
    request = MockRequest(
        user=mock_user,
        json_data={"file_paths": file_paths}
    )
    
    # Execute request
    response = await view.post(request)
    
    # Verify response - should reject with 400
    assert response.status == 400
    response_data = json.loads(response.body)
    assert response_data["success"] is False
    assert "error" in response_data
    assert "20" in response_data["error"]  # Should mention the limit


@pytest.mark.asyncio
async def test_batch_metadata_endpoint_invalid_json():
    """
    Test batch metadata with invalid JSON body.
    **Validates: Requirements 7.3**
    """
    # Setup mocks
    mock_hass = Mock()
    mock_hass.config = Mock()
    mock_hass.config.config_dir = "/config"
    
    mock_security = Mock(spec=SecurityManager)
    mock_security.validate_user_permissions = Mock(return_value=(True, None))
    
    # Create API handler
    api_handler = ManagementAPIHandler(mock_hass, mock_security)
    
    # Create view and request with invalid JSON
    view = BatchMetadataAPIView(api_handler)
    mock_user = Mock()
    request = MockRequest(user=mock_user, json_data=None)  # Will raise JSONDecodeError
    
    # Execute request
    response = await view.post(request)
    
    # Verify response
    assert response.status == 400
    response_data = json.loads(response.body)
    assert response_data["success"] is False
    assert "error" in response_data
    assert "json" in response_data["error"].lower()


@pytest.mark.asyncio
async def test_batch_metadata_endpoint_invalid_parameter():
    """
    Test batch metadata with invalid file_paths parameter (not a list).
    **Validates: Requirements 7.3**
    """
    # Setup mocks
    mock_hass = Mock()
    mock_hass.config = Mock()
    mock_hass.config.config_dir = "/config"
    
    mock_security = Mock(spec=SecurityManager)
    mock_security.validate_user_permissions = Mock(return_value=(True, None))
    
    # Create API handler
    api_handler = ManagementAPIHandler(mock_hass, mock_security)
    
    # Create view and request with invalid parameter type
    view = BatchMetadataAPIView(api_handler)
    mock_user = Mock()
    request = MockRequest(
        user=mock_user,
        json_data={"file_paths": "not_a_list"}  # Should be array
    )
    
    # Execute request
    response = await view.post(request)
    
    # Verify response
    assert response.status == 400
    response_data = json.loads(response.body)
    assert response_data["success"] is False
    assert "error" in response_data
    assert "array" in response_data["error"].lower()
