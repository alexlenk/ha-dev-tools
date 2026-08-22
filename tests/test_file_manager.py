"""Test FileManager functionality with Home Assistant fixtures."""

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools.file_manager import FileManager
from custom_components.ha_dev_tools.security import SecurityManager


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
        ],
        "write_paths": [
            "/config/*.yaml",
            "/config/*.yml",
            "/config/*.json",
            "/config/*.txt",
            "/config/**/*.yaml",
            "/config/**/*.yml",
            "/config/**/*.json",
            "/config/**/*.txt",
        ],
    }
    return SecurityManager(hass, config)


@pytest.fixture
def file_manager(hass: HomeAssistant, security_manager):
    """Create a FileManager instance for testing."""
    return FileManager(hass, security_manager)


@pytest.fixture
def mock_config_file(hass: HomeAssistant):
    """Create a mock configuration.yaml file for testing."""
    config_content = """homeassistant:
  name: Test Home
  latitude: 32.87336
  longitude: 117.22743
  elevation: 430
  unit_system: metric
  time_zone: America/Los_Angeles

logger:
  default: info
"""

    # Use the hass config directory (provided by pytest-homeassistant-custom-component)
    config_file = Path(hass.config.config_dir) / "configuration.yaml"
    config_file.write_text(config_content)

    return config_file


async def test_read_configuration_file(
    hass: HomeAssistant, file_manager, mock_config_file
):
    """Test reading configuration.yaml through FileManager."""
    content = await file_manager.read_file("configuration.yaml")

    assert content is not None
    assert "homeassistant:" in content
    assert "name: Test Home" in content


async def test_file_exists_check(hass: HomeAssistant, file_manager, mock_config_file):
    """Test file existence checking."""
    exists = await file_manager.file_exists("configuration.yaml")

    assert exists is True

    # Test non-existent file
    exists = await file_manager.file_exists("nonexistent.yaml")

    assert exists is False


async def test_read_nonexistent_file(
    hass: HomeAssistant, file_manager, mock_config_file
):
    """Test reading a non-existent file."""
    with pytest.raises(FileNotFoundError, match="File not found: nonexistent.yaml"):
        await file_manager.read_file("nonexistent.yaml")


async def test_security_integration_blacklisted_file(
    hass: HomeAssistant, file_manager, mock_config_file
):
    """Test that SecurityManager integration blocks blacklisted files."""
    with pytest.raises(
        PermissionError, match="Access to blacklisted file denied: secrets.yaml"
    ):
        await file_manager.read_file("secrets.yaml")


async def test_security_integration_path_traversal(
    hass: HomeAssistant, file_manager, mock_config_file
):
    """Test that SecurityManager integration blocks path traversal."""
    with pytest.raises(ValueError, match="Invalid file path"):
        await file_manager.read_file("../etc/passwd")


async def test_write_and_read_file(hass: HomeAssistant, file_manager, mock_config_file):
    """Test writing a file and then reading it back."""
    test_content = """automation:
  - alias: Test Automation
    trigger:
      platform: time
      at: "12:00:00"
    action:
      service: light.turn_on
      entity_id: light.living_room
"""

    # Write the file
    result = await file_manager.write_file("automations.yaml", test_content)

    # write_file returns metadata dictionary on success
    assert isinstance(result, dict)
    assert result.get("accessible") is True

    # Read it back
    content = await file_manager.read_file("automations.yaml")

    assert content == test_content
    assert "automation:" in content


async def test_write_file_creates_directories(
    hass: HomeAssistant, file_manager, mock_config_file
):
    """Test that writing a file creates necessary parent directories."""
    test_content = "test: value"

    # Write to a nested path
    result = await file_manager.write_file("subdir/test.yaml", test_content)

    # write_file returns metadata dictionary on success
    assert isinstance(result, dict)
    assert result.get("accessible") is True

    # Verify the file was created
    content = await file_manager.read_file("subdir/test.yaml")

    assert content == test_content


async def test_read_directory_as_file(
    hass: HomeAssistant, file_manager, mock_config_file
):
    """Test that trying to read a directory is blocked by security."""
    # Create a directory with a yaml extension to pass security check
    test_dir = Path(hass.config.config_dir) / "test_directory.yaml"
    test_dir.mkdir()

    try:
        # Try to read the directory as a file
        # Should fail because it's a directory, not a file
        with pytest.raises(
            FileNotFoundError, match="Path is not a file: test_directory.yaml"
        ):
            await file_manager.read_file("test_directory.yaml")
    finally:
        # testing_config is a shared, non-per-test directory - clean up so a
        # second suite run doesn't hit FileExistsError on this same path.
        test_dir.rmdir()


async def test_file_encoding_handling(
    hass: HomeAssistant, file_manager, mock_config_file
):
    """Test handling of files with special characters."""
    special_content = "test: 'special chars: åäö 中文 🎉'"

    # Write file with special characters
    result = await file_manager.write_file("special.yaml", special_content)

    # write_file returns metadata dictionary on success
    assert isinstance(result, dict)
    assert result.get("accessible") is True

    # Read it back
    content = await file_manager.read_file("special.yaml")

    assert content == special_content


async def test_empty_file_handling(hass: HomeAssistant, file_manager, mock_config_file):
    """Test handling of empty files."""
    # Create an empty file
    empty_file = Path(hass.config.config_dir) / "empty.yaml"
    empty_file.write_text("")

    # Read the empty file - should succeed and return empty string
    content = await file_manager.read_file("empty.yaml")

    assert content == ""


async def test_round_trip_consistency(
    hass: HomeAssistant, file_manager, mock_config_file
):
    """Test that writing then reading returns the same content (Property 1)."""
    original_content = """# Test configuration
test:
  key1: value1
  key2: value2
  list:
    - item1
    - item2
"""

    # Write the content
    result = await file_manager.write_file("test_roundtrip.yaml", original_content)

    # write_file returns metadata dictionary on success
    assert isinstance(result, dict)
    assert result.get("accessible") is True

    # Read it back
    read_content = await file_manager.read_file("test_roundtrip.yaml")

    assert read_content == original_content


# --- write_file/delete_file honoring read-only-only paths --------------------
#
# The fixtures above use a deliberately permissive SecurityManager config
# (identical read_paths/write_paths globs), so none of the tests above ever
# exercise a path that's readable but NOT writable - the exact case that
# matters here. These use the real default SecurityManager config instead
# (DEFAULT_READ_ONLY_PATHS vs DEFAULT_WRITE_PATHS - see const.py), which is
# what a real install actually runs with.


@pytest.fixture
def default_file_manager(hass: HomeAssistant):
    """FileManager backed by the real default (non-permissive) security config."""
    return FileManager(hass, SecurityManager(hass))


async def test_write_file_denied_on_read_only_only_path(
    hass: HomeAssistant, default_file_manager
):
    """configuration.yaml is in DEFAULT_READ_ONLY_PATHS but not DEFAULT_WRITE_PATHS.

    Regression test: write_file's validate_file_path call previously omitted
    operation=OPERATION_WRITE, silently defaulting to the read check - which
    is a superset of the write check - so this write incorrectly succeeded.
    """
    with pytest.raises(PermissionError, match="Write access to file denied"):
        await default_file_manager.write_file(
            "configuration.yaml", "homeassistant: {}\n", validate_before_write=False
        )


async def test_read_file_still_allowed_on_read_only_only_path(
    hass: HomeAssistant, default_file_manager
):
    """The fix must not also break reading a legitimately read-only path."""
    config_file = Path(hass.config.config_dir) / "configuration.yaml"
    config_file.write_text("homeassistant: {}\n")

    content = await default_file_manager.read_file("configuration.yaml")

    assert content == "homeassistant: {}\n"


async def test_delete_file_denied_on_read_only_only_path(
    hass: HomeAssistant, default_file_manager
):
    """scripts.yaml is in DEFAULT_READ_ONLY_PATHS but not DEFAULT_WRITE_PATHS.

    Same regression as write_file's test above, for delete_file's identical
    missing operation=OPERATION_WRITE.
    """
    scripts_file = Path(hass.config.config_dir) / "scripts.yaml"
    scripts_file.write_text("{}\n")
    try:
        with pytest.raises(PermissionError, match="Write access to file denied"):
            await default_file_manager.delete_file("scripts.yaml")

        assert scripts_file.exists()
    finally:
        # testing_config is a shared, non-per-test directory - clean up so a
        # second suite run doesn't see a stale file (see
        # test_read_directory_as_file's identical reasoning above).
        scripts_file.unlink(missing_ok=True)


async def test_write_file_still_allowed_on_write_permitted_path(
    hass: HomeAssistant, default_file_manager
):
    """packages/**/*.yaml is in DEFAULT_WRITE_PATHS - the fix must not over-block."""
    package_file = Path(hass.config.config_dir) / "packages" / "test.yaml"
    try:
        result = await default_file_manager.write_file(
            "packages/test.yaml", "sensor: []\n", validate_before_write=False
        )

        assert isinstance(result, dict)
        assert result.get("accessible") is True
    finally:
        package_file.unlink(missing_ok=True)


async def test_delete_file_still_allowed_on_write_permitted_path(
    hass: HomeAssistant, default_file_manager
):
    """Same as above, for delete_file."""
    package_file = Path(hass.config.config_dir) / "packages" / "test_delete.yaml"
    package_file.parent.mkdir(parents=True, exist_ok=True)
    package_file.write_text("sensor: []\n")

    deleted = await default_file_manager.delete_file("packages/test_delete.yaml")

    assert deleted is True
    assert not package_file.exists()
