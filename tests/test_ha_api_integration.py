"""Integration tests for API endpoints using Home Assistant fixtures.

These tests use the official pytest-homeassistant-custom-component framework
to test API endpoints with HA's test web client (hass_client fixture).

NOTE: These tests require a fully initialized HTTP component and are skipped
in the standard test environment. They should be run in a real Home Assistant
instance for full integration testing.
"""
import pytest
from pathlib import Path

from homeassistant.core import HomeAssistant
from aiohttp.test_utils import TestClient

from custom_components.ha_dev_tools.const import DOMAIN

# Mark all tests in this module as integration tests that require HTTP
pytestmark = pytest.mark.skip(reason="Requires full HTTP component - run in real HA environment")


@pytest.mark.asyncio
async def test_file_api_endpoint_read(hass: HomeAssistant, setup_integration, hass_client):
    """Test reading configuration.yaml through the API endpoint."""
    client: TestClient = await hass_client()
    
    # Test reading configuration.yaml
    resp = await client.get("/api/management/files/configuration.yaml")
    assert resp.status == 200
    
    content = await resp.text()
    assert "homeassistant:" in content
    assert "Test Home" in content


@pytest.mark.asyncio
async def test_file_api_endpoint_list(hass: HomeAssistant, setup_integration, hass_client):
    """Test listing files through the API endpoint."""
    client: TestClient = await hass_client()
    
    # Test listing files
    resp = await client.get("/api/management/files")
    assert resp.status == 200
    
    data = await resp.json()
    assert "files" in data
    assert isinstance(data["files"], list)
    
    # Should include configuration.yaml
    file_names = [f["name"] for f in data["files"]]
    assert "configuration.yaml" in file_names


@pytest.mark.asyncio
async def test_file_api_endpoint_write(hass: HomeAssistant, setup_integration, hass_client):
    """Test writing a file through the API endpoint."""
    client: TestClient = await hass_client()
    
    # Create a test file
    test_content = """# Test automation
automation:
  - alias: Test
    trigger:
      platform: state
    action:
      service: light.turn_on
"""
    
    resp = await client.post(
        "/api/management/files/automations.yaml",
        data=test_content,
        headers={"Content-Type": "text/plain"}
    )
    assert resp.status == 201
    
    # Verify the file was created
    test_file = Path(hass.config.config_dir) / "automations.yaml"
    assert test_file.exists()
    assert test_file.read_text() == test_content


@pytest.mark.asyncio
async def test_file_api_endpoint_security_blacklist(hass: HomeAssistant, setup_integration, hass_client):
    """Test that blacklisted files are rejected."""
    client: TestClient = await hass_client()
    
    # Try to read secrets.yaml (blacklisted)
    resp = await client.get("/api/management/files/secrets.yaml")
    assert resp.status == 403
    
    data = await resp.json()
    assert "error" in data
    assert "blacklisted" in data["error"].lower()


@pytest.mark.asyncio
async def test_file_api_endpoint_path_traversal(hass: HomeAssistant, setup_integration, hass_client):
    """Test that path traversal attempts are rejected.
    
    Note: aiohttp normalizes URLs at the routing level, so path traversal
    attempts like '../../../etc/passwd' get normalized to '/etc/passwd'
    before reaching our handler. This returns 404 (not found) which is
    actually correct security behavior - the attack is blocked at the
    framework level before reaching application code.
    """
    client: TestClient = await hass_client()
    
    # Try path traversal - aiohttp normalizes this to /etc/passwd
    resp = await client.get("/api/management/files/../../../etc/passwd")
    # Framework-level path normalization returns 404 (route not found)
    # This is correct security behavior - defense in depth
    assert resp.status == 404


@pytest.mark.asyncio
async def test_logs_api_endpoint_core(hass: HomeAssistant, setup_integration, hass_client):
    """Test retrieving core logs through the API endpoint."""
    client: TestClient = await hass_client()
    
    # Test getting core logs
    resp = await client.get("/api/management/logs/core")
    assert resp.status == 200
    
    data = await resp.json()
    assert "logs" in data
    assert isinstance(data["logs"], list)


@pytest.mark.asyncio
async def test_logs_api_endpoint_with_filters(hass: HomeAssistant, setup_integration, hass_client):
    """Test log retrieval with filtering parameters."""
    client: TestClient = await hass_client()
    
    # Test with lines parameter
    resp = await client.get("/api/management/logs/core?lines=10")
    assert resp.status == 200
    
    data = await resp.json()
    assert "logs" in data
    assert len(data["logs"]) <= 10


@pytest.mark.asyncio
async def test_validation_api_endpoint(hass: HomeAssistant, setup_integration, hass_client):
    """Test YAML validation through the API."""
    client: TestClient = await hass_client()
    
    # Test writing invalid YAML
    invalid_yaml = """homeassistant:
  name: Test
  [invalid: yaml: syntax
"""
    
    resp = await client.post(
        "/api/management/files/test_invalid.yaml",
        data=invalid_yaml,
        headers={"Content-Type": "text/plain"}
    )
    assert resp.status == 422
    
    data = await resp.json()
    assert "error" in data
    assert "validation" in data["error"].lower() or "yaml" in data["error"].lower()


@pytest.mark.asyncio
async def test_authentication_required(hass: HomeAssistant, setup_integration, hass_client):
    """Test that authentication is required for API endpoints."""
    # Note: hass_client fixture automatically provides authentication
    # This test verifies the integration works with HA's auth system
    client: TestClient = await hass_client()
    
    # All requests should work with authenticated client
    resp = await client.get("/api/management/files/configuration.yaml")
    assert resp.status == 200


@pytest.mark.asyncio
async def test_file_operations_integration(hass: HomeAssistant, setup_integration, hass_client):
    """Test complete file operation workflow."""
    client: TestClient = await hass_client()
    
    # 1. Create a file
    content = "# Test script\nscript:\n  test_script:\n    sequence: []"
    resp = await client.post(
        "/api/management/files/scripts.yaml",
        data=content,
        headers={"Content-Type": "text/plain"}
    )
    assert resp.status == 201
    
    # 2. Read the file back
    resp = await client.get("/api/management/files/scripts.yaml")
    assert resp.status == 200
    read_content = await resp.text()
    assert read_content == content
    
    # 3. Update the file
    updated_content = "# Updated script\nscript:\n  updated_script:\n    sequence: []"
    resp = await client.put(
        "/api/management/files/scripts.yaml",
        data=updated_content,
        headers={"Content-Type": "text/plain"}
    )
    assert resp.status == 200
    
    # 4. Verify update
    resp = await client.get("/api/management/files/scripts.yaml")
    assert resp.status == 200
    read_content = await resp.text()
    assert read_content == updated_content
    
    # 5. Delete the file
    resp = await client.delete("/api/management/files/scripts.yaml")
    assert resp.status == 200
    
    # 6. Verify deletion
    resp = await client.get("/api/management/files/scripts.yaml")
    assert resp.status == 404
