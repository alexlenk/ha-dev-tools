"""Test file write API endpoint functionality."""
import pytest
from homeassistant.core import HomeAssistant
from aiohttp import web
from unittest.mock import Mock, AsyncMock, patch
from pathlib import Path

from custom_components.ha_dev_tools.api import ManagementAPIHandler, FileAPIView
from custom_components.ha_dev_tools.security import SecurityManager
from custom_components.ha_dev_tools.const import (
    HTTP_OK,
    HTTP_BAD_REQUEST,
    HTTP_FORBIDDEN,
    HTTP_UNAUTHORIZED,
    HTTP_UNPROCESSABLE_ENTITY,
)


@pytest.fixture
def security_manager(hass: HomeAssistant):
    """Create a SecurityManager instance for testing."""
    # Configure with read-write paths for testing
    config = {
        "write_paths": ["*.yaml", "*.yml", "packages/*.yaml"],
        "read_paths": [],
        "denied_paths": []
    }
    return SecurityManager(hass, config)


@pytest.fixture
def api_handler(hass: HomeAssistant, security_manager):
    """Create an API handler instance for testing."""
    return ManagementAPIHandler(hass, security_manager)


@pytest.fixture
def file_api_view(api_handler):
    """Create a FileAPIView instance for testing."""
    return FileAPIView(api_handler)


@pytest.fixture
def mock_admin_user():
    """Create a mock admin user."""
    user = Mock()
    user.is_admin = True
    user.id = "test_admin"
    user.name = "Test Admin"
    return user


@pytest.fixture
def mock_request(mock_admin_user):
    """Create a mock request with admin user."""
    request = Mock(spec=web.Request)
    request.get = Mock(return_value=mock_admin_user)
    return request


@pytest.fixture
def setup_test_file(hass: HomeAssistant, tmp_path):
    """Set up a test configuration directory with files."""
    hass.config.config_dir = str(tmp_path)
    
    # Create a test file
    test_file = tmp_path / "test.yaml"
    test_file.write_text("test: value")
    
    return tmp_path


async def test_write_valid_yaml_file(hass: HomeAssistant, file_api_view, mock_request, setup_test_file):
    """Test writing a valid YAML file."""
    # Prepare request data
    valid_yaml = """automation:
  - alias: Test
    trigger:
      platform: state
    action:
      service: light.turn_on
"""
    
    mock_request.json = AsyncMock(return_value={
        "content": valid_yaml,
        "validate_before_write": True
    })
    
    # Call the PUT endpoint
    response = await file_api_view.put(mock_request, "automations.yaml")
    
    # Verify response
    assert response.status == HTTP_OK
    
    # Parse response data
    import json
    response_data = json.loads(response.body)
    
    assert response_data["success"] is True
    assert "metadata" in response_data
    assert response_data["metadata"]["path"] == "automations.yaml"
    assert "content_hash" in response_data["metadata"]


async def test_write_invalid_yaml(hass: HomeAssistant, file_api_view, mock_request, setup_test_file):
    """Test writing invalid YAML (should fail)."""
    # Prepare request data with invalid YAML
    invalid_yaml = """automation:
  - alias: Test
    [invalid: yaml: syntax
"""
    
    mock_request.json = AsyncMock(return_value={
        "content": invalid_yaml,
        "validate_before_write": True
    })
    
    # Call the PUT endpoint
    response = await file_api_view.put(mock_request, "automations.yaml")
    
    # Verify response - should fail validation
    assert response.status == HTTP_UNPROCESSABLE_ENTITY
    
    # Parse response data
    import json
    response_data = json.loads(response.body)
    
    assert response_data["success"] is False
    assert "validation failed" in response_data["error"].lower()


async def test_write_with_correct_expected_hash(hass: HomeAssistant, file_api_view, mock_request, setup_test_file):
    """Test writing with correct expected_hash."""
    # First, write initial content
    initial_content = "test: initial"
    
    mock_request.json = AsyncMock(return_value={
        "content": initial_content,
        "validate_before_write": True
    })
    
    response = await file_api_view.put(mock_request, "test.yaml")
    assert response.status == HTTP_OK
    
    # Get the hash from response
    import json
    response_data = json.loads(response.body)
    initial_hash = response_data["metadata"]["content_hash"]
    
    # Now update with correct expected_hash
    updated_content = "test: updated"
    
    mock_request.json = AsyncMock(return_value={
        "content": updated_content,
        "expected_hash": initial_hash,
        "validate_before_write": True
    })
    
    response = await file_api_view.put(mock_request, "test.yaml")
    
    # Should succeed
    assert response.status == HTTP_OK
    
    response_data = json.loads(response.body)
    assert response_data["success"] is True
    assert response_data["metadata"]["content_hash"] != initial_hash


async def test_write_with_incorrect_expected_hash(hass: HomeAssistant, file_api_view, mock_request, setup_test_file):
    """Test writing with incorrect expected_hash (conflict)."""
    # First, write initial content
    initial_content = "test: initial"
    
    mock_request.json = AsyncMock(return_value={
        "content": initial_content,
        "validate_before_write": True
    })
    
    response = await file_api_view.put(mock_request, "test.yaml")
    assert response.status == HTTP_OK
    
    # Now try to update with wrong expected_hash
    updated_content = "test: updated"
    wrong_hash = "0" * 64  # Invalid hash
    
    mock_request.json = AsyncMock(return_value={
        "content": updated_content,
        "expected_hash": wrong_hash,
        "validate_before_write": True
    })
    
    response = await file_api_view.put(mock_request, "test.yaml")
    
    # Should fail with conflict
    assert response.status == 409  # HTTP 409 Conflict
    
    import json
    response_data = json.loads(response.body)
    assert response_data["success"] is False
    assert "hash conflict" in response_data["error"].lower()


async def test_write_to_blocked_path(hass: HomeAssistant, file_api_view, mock_request, setup_test_file):
    """Test writing to blocked path (403)."""
    # Try to write to a blacklisted file
    mock_request.json = AsyncMock(return_value={
        "content": "test: value",
        "validate_before_write": True
    })
    
    response = await file_api_view.put(mock_request, "secrets.yaml")
    
    # Should be forbidden
    assert response.status == HTTP_FORBIDDEN
    
    import json
    response_data = json.loads(response.body)
    assert response_data["success"] is False
    assert "blacklisted" in response_data["error"].lower()


async def test_write_without_content_parameter(hass: HomeAssistant, file_api_view, mock_request, setup_test_file):
    """Test writing without required content parameter."""
    # Missing content parameter
    mock_request.json = AsyncMock(return_value={
        "validate_before_write": True
    })
    
    response = await file_api_view.put(mock_request, "test.yaml")
    
    # Should fail with bad request
    assert response.status == HTTP_BAD_REQUEST
    
    import json
    response_data = json.loads(response.body)
    assert response_data["success"] is False
    assert "missing" in response_data["error"].lower()


async def test_write_with_invalid_json(hass: HomeAssistant, file_api_view, mock_request, setup_test_file):
    """Test writing with invalid JSON in request body."""
    import json as json_module
    # Mock invalid JSON with JSONDecodeError
    mock_request.json = AsyncMock(side_effect=json_module.JSONDecodeError("Invalid JSON", "", 0))
    
    response = await file_api_view.put(mock_request, "test.yaml")
    
    # Should fail with bad request
    assert response.status == HTTP_BAD_REQUEST
    
    import json
    response_data = json.loads(response.body)
    assert response_data["success"] is False


async def test_write_without_authentication(hass: HomeAssistant, file_api_view, setup_test_file):
    """Test writing without authentication."""
    # Create request without user
    request = Mock(spec=web.Request)
    request.get = Mock(return_value=None)
    request.json = AsyncMock(return_value={
        "content": "test: value"
    })
    
    response = await file_api_view.put(request, "test.yaml")
    
    # Should be unauthorized
    assert response.status == HTTP_UNAUTHORIZED
    
    import json
    response_data = json.loads(response.body)
    assert response_data["success"] is False


async def test_write_as_non_admin_user(hass: HomeAssistant, file_api_view, setup_test_file):
    """Test writing as non-admin user."""
    # Create non-admin user
    non_admin_user = Mock()
    non_admin_user.is_admin = False
    non_admin_user.id = "test_user"
    non_admin_user.name = "Test User"
    
    request = Mock(spec=web.Request)
    request.get = Mock(return_value=non_admin_user)
    request.json = AsyncMock(return_value={
        "content": "test: value"
    })
    
    response = await file_api_view.put(request, "test.yaml")
    
    # Should be forbidden
    assert response.status == HTTP_FORBIDDEN
    
    import json
    response_data = json.loads(response.body)
    assert response_data["success"] is False


async def test_write_creates_backup(hass: HomeAssistant, file_api_view, mock_request, setup_test_file):
    """Test that writing creates a backup of existing file."""
    # First, create an initial file
    initial_content = "test: initial"
    
    mock_request.json = AsyncMock(return_value={
        "content": initial_content,
        "validate_before_write": True
    })
    
    response = await file_api_view.put(mock_request, "test.yaml")
    assert response.status == HTTP_OK
    
    # Now update the file
    updated_content = "test: updated"
    
    mock_request.json = AsyncMock(return_value={
        "content": updated_content,
        "validate_before_write": True
    })
    
    response = await file_api_view.put(mock_request, "test.yaml")
    assert response.status == HTTP_OK
    
    # Check that backup was created - FileManager uses hass.config.config_dir
    # which is set by the pytest-homeassistant-custom-component framework
    from pathlib import Path
    # The FileManager is initialized with hass, so it uses hass.config.config_dir
    # We need to check the actual directory where FileManager creates backups
    file_manager = file_api_view.api_handler.file_manager
    config_dir = file_manager._config_path
    backup_dir = config_dir / ".ha_config_manager_backups"
    assert backup_dir.exists(), f"Backup directory not found at {backup_dir}"
    
    # Check that there's at least one backup file
    backup_files = list(backup_dir.glob("test.yaml.*.backup"))
    assert len(backup_files) > 0, f"No backup files found in {backup_dir}"


@pytest.mark.skip(reason="ServiceRegistry.async_call is read-only and cannot be mocked")
async def test_config_check_trigger_for_config_files(hass: HomeAssistant, file_api_view, mock_request, setup_test_file):
    """Test that config check is triggered for configuration files."""
    # Mock the check_config service using patch
    with patch.object(hass.services, 'async_call', new=AsyncMock(return_value={"valid": True})):
        config_content = """homeassistant:
  name: Test
"""
        
        mock_request.json = AsyncMock(return_value={
            "content": config_content,
            "validate_before_write": True
        })
        
        response = await file_api_view.put(mock_request, "configuration.yaml")
        
        # Should succeed
        assert response.status == HTTP_OK
        
        import json
        response_data = json.loads(response.body)
        assert response_data["success"] is True
        
        # Verify config check was triggered
        assert "config_check" in response_data


async def test_write_to_packages_directory(hass: HomeAssistant, file_api_view, mock_request, setup_test_file):
    """Test writing to packages directory."""
    # Create packages directory
    packages_dir = setup_test_file / "packages"
    packages_dir.mkdir(exist_ok=True)
    
    package_content = """automation:
  - alias: Package Test
    trigger:
      platform: state
    action:
      service: light.turn_on
"""
    
    mock_request.json = AsyncMock(return_value={
        "content": package_content,
        "validate_before_write": True
    })
    
    response = await file_api_view.put(mock_request, "packages/lighting.yaml")
    
    # Should succeed
    assert response.status == HTTP_OK
    
    import json
    response_data = json.loads(response.body)
    assert response_data["success"] is True
    assert response_data["metadata"]["path"] == "packages/lighting.yaml"
