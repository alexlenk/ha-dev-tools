"""Tests for the layout-aware, package-safe read-only RestCommandManager.

Mirrors test_script_manager.py's structure - see that file's module
docstring for why these are the highest-value tests in this family: they
verify a package-defined rest_command is found through its real file,
never missed entirely. No write tests here - rest_command_manager.py is
read-only by design (issue #73).
"""

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools.file_manager import FileManager
from custom_components.ha_dev_tools.rest_command_manager import (
    DuplicateRestCommandIdError,
    RestCommandManager,
    RestCommandNotFoundError,
)
from custom_components.ha_dev_tools.security import SecurityManager


@pytest.fixture
def security_manager(hass: HomeAssistant):
    """SecurityManager configured to allow reading configuration.yaml and packages."""
    return SecurityManager(
        hass,
        {
            "read_paths": ["configuration.yaml", "packages/**/*.yaml"],
            "write_paths": [],
            "denied_paths": [],
        },
    )


@pytest.fixture
def file_manager(hass: HomeAssistant, security_manager, tmp_path):
    """FileManager pointed at a real temp config directory."""
    hass.config.config_dir = str(tmp_path)
    return FileManager(hass, security_manager)


@pytest.fixture
def rest_command_manager(hass: HomeAssistant, file_manager):
    """RestCommandManager under test."""
    return RestCommandManager(hass, file_manager)


def _write(tmp_path: Path, rel_path: str, content: str) -> None:
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)


@pytest.mark.asyncio
async def test_find_rest_command_in_configuration_yaml(rest_command_manager, tmp_path):
    _write(
        tmp_path,
        "configuration.yaml",
        "rest_command:\n  ping_host:\n    url: http://example.com\n",
    )

    location = await rest_command_manager.find_rest_command("ping_host")

    assert location.file_path == "configuration.yaml"
    assert location.is_package is False


@pytest.mark.asyncio
async def test_find_rest_command_in_package(rest_command_manager, tmp_path):
    _write(tmp_path, "configuration.yaml", "homeassistant:\n  name: Test\n")
    _write(
        tmp_path,
        "packages/emhass.yaml",
        "rest_command:\n  emhass_mpc_optim:\n    url: http://emhass/action\n"
        "input_boolean:\n  unrelated_helper: {}\n",
    )

    location = await rest_command_manager.find_rest_command("emhass_mpc_optim")

    assert location.file_path == "packages/emhass.yaml"
    assert location.is_package is True


@pytest.mark.asyncio
async def test_find_rest_command_not_found_raises(rest_command_manager, tmp_path):
    _write(
        tmp_path,
        "configuration.yaml",
        "rest_command:\n  abc:\n    url: http://example.com\n",
    )

    with pytest.raises(RestCommandNotFoundError):
        await rest_command_manager.find_rest_command("does_not_exist")


@pytest.mark.asyncio
async def test_find_rest_command_duplicate_across_files_raises(
    rest_command_manager, tmp_path
):
    _write(
        tmp_path,
        "configuration.yaml",
        "rest_command:\n  dup:\n    url: http://a\n",
    )
    _write(
        tmp_path,
        "packages/emhass.yaml",
        "rest_command:\n  dup:\n    url: http://b\n",
    )

    with pytest.raises(DuplicateRestCommandIdError) as exc_info:
        await rest_command_manager.find_rest_command("dup")

    file_paths = {loc.file_path for loc in exc_info.value.locations}
    assert file_paths == {"configuration.yaml", "packages/emhass.yaml"}


@pytest.mark.asyncio
async def test_get_rest_command_returns_config(rest_command_manager, tmp_path):
    _write(
        tmp_path,
        "configuration.yaml",
        "rest_command:\n"
        "  emhass_publish_data:\n"
        "    url: http://emhass:5000/action/publish-data\n"
        "    method: post\n"
        "    payload: '{{ payload }}'\n",
    )

    location, config = await rest_command_manager.get_rest_command(
        "emhass_publish_data"
    )

    assert location.file_path == "configuration.yaml"
    assert config["url"] == "http://emhass:5000/action/publish-data"
    assert config["method"] == "post"


@pytest.mark.asyncio
async def test_all_rest_commands_across_files(rest_command_manager, tmp_path):
    _write(
        tmp_path,
        "configuration.yaml",
        "rest_command:\n  default_cmd:\n    url: http://a\n",
    )
    _write(
        tmp_path,
        "packages/emhass.yaml",
        "rest_command:\n  pkg_cmd:\n    url: http://b\n",
    )

    results = await rest_command_manager.all_rest_commands()

    by_id = {
        rest_command_id: (location, config)
        for location, rest_command_id, config in results
    }
    assert by_id["default_cmd"][0].file_path == "configuration.yaml"
    assert by_id["pkg_cmd"][0].file_path == "packages/emhass.yaml"
    assert by_id["pkg_cmd"][1]["url"] == "http://b"


@pytest.mark.asyncio
async def test_all_rest_commands_no_rest_command_key_yields_nothing(
    rest_command_manager, tmp_path
):
    """configuration.yaml with no rest_command: key at all contributes nothing."""
    _write(tmp_path, "configuration.yaml", "homeassistant:\n  name: Test\n")

    results = await rest_command_manager.all_rest_commands()

    assert results == []


@pytest.mark.asyncio
async def test_all_rest_commands_package_without_rest_command_key_yields_nothing(
    rest_command_manager, tmp_path
):
    """A package file that doesn't define rest_command: at all (e.g. only
    helpers) contributes nothing, rather than erroring."""
    _write(tmp_path, "configuration.yaml", "homeassistant:\n  name: Test\n")
    _write(tmp_path, "packages/helpers_only.yaml", "input_boolean:\n  foo: {}\n")

    results = await rest_command_manager.all_rest_commands()

    assert results == []


@pytest.mark.asyncio
async def test_all_rest_commands_key_wrong_type_raises(rest_command_manager, tmp_path):
    """rest_command: must be a mapping - any other type is an invalid config."""
    _write(tmp_path, "configuration.yaml", "rest_command: not_a_mapping\n")

    with pytest.raises(ValueError, match="is not a mapping"):
        await rest_command_manager.all_rest_commands()


@pytest.mark.asyncio
async def test_load_document_returns_none_for_missing_package_file(
    rest_command_manager, tmp_path
):
    """_load_document's FileNotFoundError->None fallback - exercised
    directly since candidate_files() itself never returns a nonexistent
    path (packages are globbed, only existing files returned)."""
    document = await rest_command_manager._load_document("packages/gone.yaml")

    assert document is None
