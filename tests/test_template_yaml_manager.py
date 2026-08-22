"""Tests for the layout-aware, package-safe TemplateYamlManager."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml as pyyaml
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools.file_manager import FileManager
from custom_components.ha_dev_tools.security import SecurityManager
from custom_components.ha_dev_tools.template_yaml_manager import (
    DuplicateTemplateUniqueIdError,
    TemplateEntityNotFoundError,
    TemplateYamlManager,
)


@pytest.fixture
def security_manager(hass: HomeAssistant):
    """SecurityManager configured to allow reading and writing template files."""
    return SecurityManager(
        hass,
        {
            "read_paths": ["configuration.yaml", "packages/**/*.yaml"],
            "write_paths": ["configuration.yaml", "packages/**/*.yaml"],
            "denied_paths": [],
        },
    )


@pytest.fixture
def file_manager(hass: HomeAssistant, security_manager, tmp_path):
    """FileManager pointed at a real temp config directory."""
    hass.config.config_dir = str(tmp_path)
    return FileManager(hass, security_manager)


@pytest.fixture
def template_manager(hass: HomeAssistant, file_manager):
    """TemplateYamlManager under test."""
    return TemplateYamlManager(hass, file_manager)


@pytest.fixture(autouse=True)
def mock_reload_service(hass: HomeAssistant):
    """Register a fake template.reload service - same pattern
    test_automation_manager.py uses for automation.reload, for the same
    reason: exercise the real hass.services.async_call machinery without
    loading the whole template integration."""
    mock = AsyncMock()
    hass.services.async_register("template", "reload", mock)
    return mock


def _write(tmp_path: Path, rel_path: str, content: str) -> None:
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)


# --- list/find/get -----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_entities_across_config_and_packages(template_manager, tmp_path):
    _write(
        tmp_path,
        "configuration.yaml",
        "template:\n"
        "  - sensor:\n"
        "      - name: Inline Sensor\n"
        "        unique_id: inline_sensor\n"
        '        state: "{{ 1 }}"\n',
    )
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        "  - sensor:\n"
        "      - name: Package Sensor\n"
        "        unique_id: package_sensor\n"
        '        state: "{{ 2 }}"\n',
    )

    entities = await template_manager.list_entities()

    unique_ids = {e["unique_id"] for e in entities}
    assert unique_ids == {"inline_sensor", "package_sensor"}
    by_id = {e["unique_id"]: e for e in entities}
    assert by_id["inline_sensor"]["file_path"] == "configuration.yaml"
    assert by_id["inline_sensor"]["is_package"] is False
    assert by_id["package_sensor"]["file_path"] == "packages/emhas.yaml"
    assert by_id["package_sensor"]["is_package"] is True
    assert by_id["package_sensor"]["platform"] == "sensor"


@pytest.mark.asyncio
async def test_list_entities_includes_entities_without_unique_id(
    template_manager, tmp_path
):
    _write(
        tmp_path,
        "configuration.yaml",
        'template:\n  - sensor:\n      - name: No ID\n        state: "{{ 1 }}"\n',
    )

    entities = await template_manager.list_entities()

    assert len(entities) == 1
    assert entities[0]["unique_id"] is None
    assert entities[0]["name"] == "No ID"


@pytest.mark.asyncio
async def test_list_entities_sanitizes_custom_yaml_tags(template_manager, tmp_path):
    """A !secret (or similar) value inside a template entity must not break JSON output."""
    _write(
        tmp_path,
        "configuration.yaml",
        "template:\n"
        "  - sensor:\n"
        "      - name: Secretive\n"
        "        unique_id: secretive\n"
        "        state: !secret my_secret_template\n"
        "automation: !include automations.yaml\n",
    )

    entities = await template_manager.list_entities()

    assert entities[0]["config"]["state"] == "!secret my_secret_template"


@pytest.mark.asyncio
async def test_find_entity_not_found_raises(template_manager, tmp_path):
    _write(tmp_path, "configuration.yaml", "homeassistant: {}\n")

    with pytest.raises(TemplateEntityNotFoundError):
        await template_manager.find_entity("does_not_exist")


@pytest.mark.asyncio
async def test_find_entity_duplicate_across_files_raises(template_manager, tmp_path):
    _write(
        tmp_path,
        "configuration.yaml",
        "template:\n"
        '  - sensor:\n      - name: A\n        unique_id: dup\n        state: "{{ 1 }}"\n',
    )
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        '  - sensor:\n      - name: B\n        unique_id: dup\n        state: "{{ 2 }}"\n',
    )

    with pytest.raises(DuplicateTemplateUniqueIdError) as exc_info:
        await template_manager.find_entity("dup")

    file_paths = {loc.file_path for loc in exc_info.value.locations}
    assert file_paths == {"configuration.yaml", "packages/emhas.yaml"}


@pytest.mark.asyncio
async def test_get_entity_returns_config(template_manager, tmp_path):
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        "  - sensor:\n"
        "      - name: Real Name\n"
        "        unique_id: real\n"
        '        state: "{{ 3 }}"\n',
    )

    location, config = await template_manager.get_entity("real")

    assert location.file_path == "packages/emhas.yaml"
    assert location.platform == "sensor"
    assert config["name"] == "Real Name"


# --- create_entity -------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entity_requires_unique_id(template_manager, tmp_path):
    _write(tmp_path, "packages/emhas.yaml", "")

    with pytest.raises(ValueError, match="unique_id"):
        await template_manager.create_entity(
            "sensor", {"name": "No ID", "state": "{{ 1 }}"}, package="emhas.yaml"
        )


@pytest.mark.asyncio
async def test_create_entity_requires_existing_package(template_manager, tmp_path):
    with pytest.raises(TemplateEntityNotFoundError):
        await template_manager.create_entity(
            "sensor",
            {"name": "New", "unique_id": "new_one", "state": "{{ 1 }}"},
            package="does_not_exist.yaml",
        )


@pytest.mark.asyncio
async def test_create_entity_refuses_duplicate_unique_id(template_manager, tmp_path):
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        '  - sensor:\n      - name: A\n        unique_id: taken\n        state: "{{ 1 }}"\n',
    )

    with pytest.raises(DuplicateTemplateUniqueIdError):
        await template_manager.create_entity(
            "sensor",
            {"name": "B", "unique_id": "taken", "state": "{{ 2 }}"},
            package="emhas.yaml",
        )


@pytest.mark.asyncio
async def test_create_entity_new_block_preserves_other_content(
    template_manager, tmp_path, mock_reload_service
):
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "# EMHAS package - hand maintained, do not reformat\n"
        "input_boolean:\n"
        "  emhas_enabled:\n"
        "    name: EMHAS enabled\n",
    )

    location, reloaded = await template_manager.create_entity(
        "sensor",
        {"name": "New Sensor", "unique_id": "new_sensor", "state": "{{ 1 }}"},
        package="emhas.yaml",
        triggers=[{"trigger": "state", "entity_id": "sensor.source"}],
    )

    assert location.file_path == "packages/emhas.yaml"
    assert location.platform == "sensor"
    assert reloaded is True
    mock_reload_service.assert_called_once()

    raw = (tmp_path / "packages/emhas.yaml").read_text()
    assert "# EMHAS package - hand maintained, do not reformat" in raw
    assert "emhas_enabled" in raw

    parsed = pyyaml.safe_load(raw)
    assert parsed["input_boolean"]["emhas_enabled"] == {"name": "EMHAS enabled"}
    new_block = parsed["template"][-1]
    assert new_block["sensor"][0]["unique_id"] == "new_sensor"
    assert new_block["triggers"] == [{"trigger": "state", "entity_id": "sensor.source"}]


@pytest.mark.asyncio
async def test_create_entity_appends_new_block_alongside_existing(
    template_manager, tmp_path, mock_reload_service
):
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        "  - sensor:\n"
        '      - name: Existing\n        unique_id: existing\n        state: "{{ 1 }}"\n',
    )

    await template_manager.create_entity(
        "binary_sensor",
        {"name": "New", "unique_id": "new_one", "state": "{{ true }}"},
        package="emhas.yaml",
    )

    entities = await template_manager.list_entities()
    unique_ids = {e["unique_id"] for e in entities}
    assert unique_ids == {"existing", "new_one"}


# --- update_entity ---------------------------------------------------------


@pytest.mark.asyncio
async def test_update_entity_in_place_preserves_siblings(
    template_manager, tmp_path, mock_reload_service
):
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        "  - triggers:\n"
        "      - trigger: state\n"
        "        entity_id: sensor.source\n"
        "    sensor:\n"
        "      - name: Target\n"
        "        unique_id: target\n"
        '        state: "{{ 1 }}"\n'
        "      - name: Sibling\n"
        "        unique_id: sibling\n"
        '        state: "{{ 2 }}"\n',
    )

    location, reloaded = await template_manager.update_entity(
        "target", {"name": "Target Renamed", "state": "{{ 3 }}"}
    )

    assert reloaded is True
    _, config = await template_manager.get_entity("target")
    assert config["name"] == "Target Renamed"
    assert config["state"] == "{{ 3 }}"

    _, sibling_config = await template_manager.get_entity("sibling")
    assert sibling_config["name"] == "Sibling"

    raw = (tmp_path / "packages/emhas.yaml").read_text()
    parsed = pyyaml.safe_load(raw)
    assert parsed["template"][0]["triggers"] == [
        {"trigger": "state", "entity_id": "sensor.source"}
    ]


@pytest.mark.asyncio
async def test_update_entity_rejects_mismatched_unique_id(template_manager, tmp_path):
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        '  - sensor:\n      - name: A\n        unique_id: real\n        state: "{{ 1 }}"\n',
    )

    with pytest.raises(ValueError, match="does not match"):
        await template_manager.update_entity(
            "real", {"name": "B", "unique_id": "different", "state": "{{ 2 }}"}
        )


@pytest.mark.asyncio
async def test_update_entity_not_found_raises(template_manager, tmp_path):
    _write(tmp_path, "configuration.yaml", "homeassistant: {}\n")

    with pytest.raises(TemplateEntityNotFoundError):
        await template_manager.update_entity("nonexistent", {"name": "X"})


# --- delete_entity -----------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_entity_removes_only_that_entity(
    template_manager, tmp_path, mock_reload_service
):
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        "  - sensor:\n"
        '      - name: A\n        unique_id: keep_me\n        state: "{{ 1 }}"\n'
        '      - name: B\n        unique_id: delete_me\n        state: "{{ 2 }}"\n',
    )

    location, reloaded = await template_manager.delete_entity("delete_me")

    assert reloaded is True
    entities = await template_manager.list_entities()
    assert {e["unique_id"] for e in entities} == {"keep_me"}


@pytest.mark.asyncio
async def test_delete_entity_removes_empty_platform_and_block(
    template_manager, tmp_path, mock_reload_service
):
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        "  - sensor:\n"
        '      - name: Only\n        unique_id: only_one\n        state: "{{ 1 }}"\n'
        "input_boolean:\n"
        "  unrelated:\n"
        "    name: Unrelated\n",
    )

    await template_manager.delete_entity("only_one")

    raw = (tmp_path / "packages/emhas.yaml").read_text()
    parsed = pyyaml.safe_load(raw)
    assert parsed.get("template") in (None, [])
    assert parsed["input_boolean"]["unrelated"] == {"name": "Unrelated"}


@pytest.mark.asyncio
async def test_delete_entity_not_found_raises(template_manager, tmp_path):
    _write(tmp_path, "configuration.yaml", "homeassistant: {}\n")

    with pytest.raises(TemplateEntityNotFoundError):
        await template_manager.delete_entity("nonexistent")


# --- configuration.yaml is read-only by this integration's real default policy --


@pytest.fixture
def default_file_manager(hass: HomeAssistant, tmp_path):
    """FileManager backed by the real default (non-permissive) security config."""
    hass.config.config_dir = str(tmp_path)
    return FileManager(hass, SecurityManager(hass))


@pytest.fixture
def default_template_manager(hass: HomeAssistant, default_file_manager):
    return TemplateYamlManager(hass, default_file_manager)


@pytest.mark.asyncio
async def test_entity_in_configuration_yaml_readable_but_not_writable(
    default_template_manager, tmp_path
):
    """Confirms the module docstring's core design claim against the real
    default security policy (configuration.yaml is in DEFAULT_READ_ONLY_PATHS,
    not DEFAULT_WRITE_PATHS - see file_manager.py's write_file/delete_file
    operation-check fix, which is what makes this actually true)."""
    _write(
        tmp_path,
        "configuration.yaml",
        "template:\n"
        '  - sensor:\n      - name: Inline\n        unique_id: inline\n        state: "{{ 1 }}"\n',
    )

    # Readable.
    location, config = await default_template_manager.get_entity("inline")
    assert location.file_path == "configuration.yaml"
    assert config["name"] == "Inline"

    # Not writable.
    with pytest.raises(PermissionError):
        await default_template_manager.update_entity("inline", {"name": "Changed"})
    with pytest.raises(PermissionError):
        await default_template_manager.delete_entity("inline")
