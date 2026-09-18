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
async def test_list_entities_ignores_non_dict_blocks_and_entities(
    template_manager, tmp_path
):
    """A malformed template: block (a bare string instead of a mapping) or a
    malformed entity entry (a bare string instead of a mapping) must be
    skipped, not crash the whole listing - real config can be malformed
    without HA itself having rejected it yet."""
    _write(
        tmp_path,
        "configuration.yaml",
        "template:\n"
        "  - sensor:\n"
        "      - name: Good\n"
        '        unique_id: good\n        state: "{{ 1 }}"\n'
        "      - not_a_dict_entity\n"
        "  - not_a_dict_block\n",
    )

    entities = await template_manager.list_entities()

    assert [e["unique_id"] for e in entities] == ["good"]


@pytest.mark.asyncio
async def test_find_entity_ignores_non_dict_blocks(template_manager, tmp_path):
    """Same malformed-block tolerance as list_entities, for the
    find_all_locations path used by find_entity/create_entity's duplicate check."""
    _write(
        tmp_path,
        "configuration.yaml",
        "template:\n"
        "  - sensor:\n"
        '      - name: Good\n        unique_id: good\n        state: "{{ 1 }}"\n'
        "  - not_a_dict_block\n",
    )

    location = await template_manager.find_entity("good")

    assert location.file_path == "configuration.yaml"


@pytest.mark.asyncio
async def test_template_blocks_normalizes_single_mapping(template_manager, tmp_path):
    """A `template:` key that's a single mapping (not yet a list) must be
    normalized the same way automation_manager normalizes a single-mapping
    `automation:` key."""
    _write(
        tmp_path,
        "configuration.yaml",
        "template:\n"
        "  sensor:\n"
        '    - name: Solo\n      unique_id: solo\n      state: "{{ 1 }}"\n',
    )

    entities = await template_manager.list_entities()

    assert [e["unique_id"] for e in entities] == ["solo"]


@pytest.mark.asyncio
async def test_template_blocks_wrong_type_raises(template_manager, tmp_path):
    """A `template:` key that's neither a list nor a mapping is an invalid
    configuration, and should be reported rather than silently ignored."""
    _write(tmp_path, "configuration.yaml", "template: not_a_list_or_mapping\n")

    with pytest.raises(ValueError, match="is not a list or mapping"):
        await template_manager.list_entities()


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
async def test_list_entities_sanitizes_custom_yaml_tags_inside_a_list(
    template_manager, tmp_path
):
    """_to_plain must recurse into list values too, not just dict values -
    a custom tag nested inside a list-typed config field (e.g. attributes)
    must be sanitized the same way as one at the top level."""
    _write(
        tmp_path,
        "configuration.yaml",
        "template:\n"
        "  - sensor:\n"
        "      - name: Listy\n"
        "        unique_id: listy\n"
        '        state: "{{ 1 }}"\n'
        "        attributes:\n"
        "          - !secret my_secret_template\n"
        "          - plain_value\n",
    )

    entities = await template_manager.list_entities()

    assert entities[0]["config"]["attributes"] == [
        "!secret my_secret_template",
        "plain_value",
    ]


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

    result = await template_manager.create_entity(
        "sensor",
        {"name": "New Sensor", "unique_id": "new_sensor", "state": "{{ 1 }}"},
        package="emhas.yaml",
        triggers=[{"trigger": "state", "entity_id": "sensor.source"}],
    )

    assert result.location.file_path == "packages/emhas.yaml"
    assert result.location.platform == "sensor"
    assert result.reloaded is True
    assert "emhas_enabled" in result.content_before
    assert "new_sensor" in result.content_after
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


@pytest.mark.asyncio
async def test_create_entity_dry_run_computes_content_without_writing(
    template_manager, tmp_path, mock_reload_service
):
    """dry_run=True (issue #35) resolves the same location and builds the
    same content_after a real create would, but never touches the file or
    reloads."""
    _write(tmp_path, "packages/emhas.yaml", "template: []\n")

    result = await template_manager.create_entity(
        "sensor",
        {"name": "New", "unique_id": "new_one", "state": "{{ 1 }}"},
        package="emhas.yaml",
        dry_run=True,
    )

    assert result.location.file_path == "packages/emhas.yaml"
    assert result.reloaded is False
    assert "new_one" in result.content_after
    assert "new_one" not in (tmp_path / "packages/emhas.yaml").read_text()
    mock_reload_service.assert_not_called()


@pytest.mark.asyncio
async def test_create_entity_quotes_ambiguous_scalars(
    template_manager, tmp_path, mock_reload_service
):
    """Same live-tested regression as automation_manager.py's equivalent
    test: a bare on/off/yes/no value in a trigger/condition dict used to
    round-trip through ruamel.yaml unquoted, then reload via HA's own
    (PyYAML-based) loader as a bool instead of a str."""
    _write(tmp_path, "packages/emhas.yaml", "template: []\n")

    result = await template_manager.create_entity(
        "binary_sensor",
        {
            "name": "Uses state condition",
            "unique_id": "uses_state_condition",
            "state": "{{ true }}",
        },
        package="emhas.yaml",
        triggers=[
            {
                "trigger": "state",
                "entity_id": "switch.x",
                "to": "off",
            }
        ],
    )

    assert 'to: "off"' in result.content_after

    parsed = pyyaml.safe_load((tmp_path / "packages/emhas.yaml").read_text())
    assert parsed["template"][-1]["triggers"][0]["to"] == "off"


@pytest.mark.asyncio
async def test_create_entity_into_empty_package_file(
    template_manager, tmp_path, mock_reload_service
):
    """Creating the first entity in a package file that exists but is
    empty (no parsed document at all yet) must build a fresh document
    rather than crashing on a None document."""
    _write(tmp_path, "packages/empty.yaml", "")

    result = await template_manager.create_entity(
        "sensor",
        {"name": "First", "unique_id": "first_one", "state": "{{ 1 }}"},
        package="empty.yaml",
    )

    assert result.location.file_path == "packages/empty.yaml"
    parsed = pyyaml.safe_load((tmp_path / "packages/empty.yaml").read_text())
    assert parsed["template"][0]["sensor"][0]["unique_id"] == "first_one"


@pytest.mark.asyncio
async def test_create_entity_appends_to_package_with_single_mapping_template(
    template_manager, tmp_path, mock_reload_service
):
    """A package whose `template:` key is still a single mapping (not yet
    a list) must be normalized to a list before appending the new block,
    same as _template_blocks does for reads."""
    _write(
        tmp_path,
        "packages/single.yaml",
        "template:\n"
        "  sensor:\n"
        '    - name: Existing\n      unique_id: existing\n      state: "{{ 1 }}"\n',
    )

    await template_manager.create_entity(
        "sensor",
        {"name": "New", "unique_id": "new_one", "state": "{{ 2 }}"},
        package="single.yaml",
    )

    entities = await template_manager.list_entities()
    unique_ids = {e["unique_id"] for e in entities}
    assert unique_ids == {"existing", "new_one"}


@pytest.mark.asyncio
async def test_create_entity_reload_not_registered_returns_false(
    template_manager, tmp_path, hass: HomeAssistant
):
    """When nothing has loaded the template integration yet (no prior
    template: config or Template helper), the write must still succeed but
    report reloaded=False rather than raising - see _reload_template's
    docstring for why this is the expected first-ever-entity edge case."""
    hass.services.async_remove("template", "reload")
    _write(tmp_path, "packages/emhas.yaml", "template: []\n")

    result = await template_manager.create_entity(
        "sensor",
        {"name": "New", "unique_id": "new_one", "state": "{{ 1 }}"},
        package="emhas.yaml",
    )

    assert result.reloaded is False


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

    result = await template_manager.update_entity(
        "target", {"name": "Target Renamed", "state": "{{ 3 }}"}
    )

    assert result.reloaded is True
    assert "Target" in result.content_before and "Renamed" not in result.content_before
    assert "Target Renamed" in result.content_after
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
async def test_update_entity_dry_run_computes_content_without_writing(
    template_manager, tmp_path, mock_reload_service
):
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        "  - sensor:\n"
        "      - name: Old\n"
        "        unique_id: target\n"
        '        state: "{{ 1 }}"\n',
    )

    result = await template_manager.update_entity(
        "target", {"name": "New", "state": "{{ 2 }}"}, dry_run=True
    )

    assert result.reloaded is False
    assert "Old" in result.content_before
    assert "New" in result.content_after
    raw = (tmp_path / "packages/emhas.yaml").read_text()
    assert "Old" in raw
    assert "New" not in raw
    mock_reload_service.assert_not_called()


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

    result = await template_manager.delete_entity("delete_me")

    assert result.reloaded is True
    assert "delete_me" in result.content_before
    assert "delete_me" not in result.content_after
    entities = await template_manager.list_entities()
    assert {e["unique_id"] for e in entities} == {"keep_me"}


@pytest.mark.asyncio
async def test_delete_entity_dry_run_computes_content_without_writing(
    template_manager, tmp_path, mock_reload_service
):
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        "  - sensor:\n"
        '      - name: A\n        unique_id: delete_me\n        state: "{{ 1 }}"\n',
    )

    result = await template_manager.delete_entity("delete_me", dry_run=True)

    assert result.reloaded is False
    assert "delete_me" in result.content_before
    assert "delete_me" not in result.content_after
    raw = (tmp_path / "packages/emhas.yaml").read_text()
    assert "delete_me" in raw
    mock_reload_service.assert_not_called()


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
