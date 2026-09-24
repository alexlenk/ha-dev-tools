"""Tests for the layout-aware, package-safe AutomationManager.

These are the highest-value tests in the redesign: they verify the "hard
safety rule" from docs/ARCHITECTURE.md actually holds in code - a
package-defined automation is found and edited through its real file, never
silently duplicated into automations.yaml or missed entirely.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml as pyyaml
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools.automation_manager import (
    AutomationManager,
    AutomationNotFoundError,
    DuplicateAutomationIdError,
)
from custom_components.ha_dev_tools.file_manager import FileManager
from custom_components.ha_dev_tools.security import SecurityManager


@pytest.fixture
def security_manager(hass: HomeAssistant):
    """SecurityManager configured to allow reading and writing automation files."""
    return SecurityManager(
        hass,
        {
            "read_paths": ["automations.yaml", "packages/**/*.yaml"],
            "write_paths": ["automations.yaml", "packages/**/*.yaml"],
            "denied_paths": [],
        },
    )


@pytest.fixture
def file_manager(hass: HomeAssistant, security_manager, tmp_path):
    """FileManager pointed at a real temp config directory."""
    hass.config.config_dir = str(tmp_path)
    return FileManager(hass, security_manager)


@pytest.fixture
def automation_manager(hass: HomeAssistant, file_manager):
    """AutomationManager under test."""
    return AutomationManager(hass, file_manager)


@pytest.fixture(autouse=True)
def mock_reload_service(hass: HomeAssistant):
    """Register a fake automation.reload service so write_automation's real
    hass.services.async_call succeeds without loading the whole automation
    integration, while still exercising the real service-call machinery."""
    mock = AsyncMock()
    hass.services.async_register("automation", "reload", mock)
    return mock


def _write(tmp_path: Path, rel_path: str, content: str) -> None:
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)


@pytest.mark.asyncio
async def test_find_automation_in_default_file(automation_manager, tmp_path):
    _write(tmp_path, "automations.yaml", "- id: abc\n  trigger: []\n  action: []\n")

    location = await automation_manager.find_automation("abc")

    assert location.file_path == "automations.yaml"
    assert location.is_package is False


@pytest.mark.asyncio
async def test_find_automation_in_package(automation_manager, tmp_path):
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "automation:\n  - id: solar_charge\n    trigger: []\n    action: []\n"
        "script:\n  unrelated_script: {}\n",
    )

    location = await automation_manager.find_automation("solar_charge")

    assert location.file_path == "packages/emhas.yaml"
    assert location.is_package is True


@pytest.mark.asyncio
async def test_find_automation_not_found_raises(automation_manager, tmp_path):
    _write(tmp_path, "automations.yaml", "- id: abc\n  trigger: []\n  action: []\n")

    with pytest.raises(AutomationNotFoundError):
        await automation_manager.find_automation("does_not_exist")


@pytest.mark.asyncio
async def test_find_automation_duplicate_across_files_raises(
    automation_manager, tmp_path
):
    _write(tmp_path, "automations.yaml", "- id: dup\n  trigger: []\n  action: []\n")
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "automation:\n  - id: dup\n    trigger: []\n    action: []\n",
    )

    with pytest.raises(DuplicateAutomationIdError) as exc_info:
        await automation_manager.find_automation("dup")

    file_paths = {loc.file_path for loc in exc_info.value.locations}
    assert file_paths == {"automations.yaml", "packages/emhas.yaml"}


@pytest.mark.asyncio
async def test_get_automation_returns_config(automation_manager, tmp_path):
    _write(
        tmp_path,
        "automations.yaml",
        "- id: abc\n  alias: My automation\n  trigger: []\n  action: []\n",
    )

    location, config = await automation_manager.get_automation("abc")

    assert location.file_path == "automations.yaml"
    assert config["alias"] == "My automation"


@pytest.mark.asyncio
async def test_write_automation_updates_existing_in_package_preserves_other_content(
    automation_manager, tmp_path, mock_reload_service
):
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "# EMHAS package - hand maintained, do not reformat\n"
        "automation:\n"
        "  - id: solar_charge\n"
        "    alias: Old alias\n"
        "    trigger: []\n"
        "    action: []\n"
        "script:\n"
        "  emhas_helper_script:\n"
        "    sequence: []\n"
        "input_boolean:\n"
        "  emhas_enabled:\n"
        "    name: EMHAS enabled\n",
    )

    result = await automation_manager.write_automation(
        "solar_charge",
        {"alias": "New alias", "trigger": [], "action": [], "mode": "single"},
    )

    assert "Old alias" in result.content_before
    assert "New alias" in result.content_after
    assert result.content_before != result.content_after
    mock_reload_service.assert_called_once()

    raw = (tmp_path / "packages/emhas.yaml").read_text()
    assert "# EMHAS package - hand maintained, do not reformat" in raw
    assert "emhas_helper_script" in raw
    assert "emhas_enabled" in raw

    parsed = pyyaml.safe_load(raw)
    automations = parsed["automation"]
    assert len(automations) == 1
    assert automations[0]["alias"] == "New alias"
    assert automations[0]["mode"] == "single"
    assert parsed["script"]["emhas_helper_script"] == {"sequence": []}


@pytest.mark.asyncio
async def test_write_automation_creates_new_in_default_file(
    automation_manager, tmp_path, mock_reload_service
):
    assert not (tmp_path / "automations.yaml").exists()

    result = await automation_manager.write_automation(
        "brand_new", {"alias": "Brand new", "trigger": [], "action": []}
    )

    assert result.location.file_path == "automations.yaml"
    assert result.content_before is None
    assert "brand_new" in result.content_after
    mock_reload_service.assert_called_once()

    parsed = pyyaml.safe_load((tmp_path / "automations.yaml").read_text())
    assert parsed[0]["id"] == "brand_new"
    assert parsed[0]["alias"] == "Brand new"


@pytest.mark.asyncio
async def test_write_automation_creates_new_with_explicit_package(
    automation_manager, tmp_path, mock_reload_service
):
    _write(tmp_path, "packages/emhas.yaml", "automation: []\n")

    result = await automation_manager.write_automation(
        "new_in_package",
        {"alias": "New in package", "trigger": [], "action": []},
        package="emhas.yaml",
    )

    assert result.location.file_path == "packages/emhas.yaml"
    assert result.content_before == "automation: []\n"
    parsed = pyyaml.safe_load((tmp_path / "packages/emhas.yaml").read_text())
    assert parsed["automation"][0]["id"] == "new_in_package"


@pytest.mark.asyncio
async def test_write_automation_dry_run_computes_content_without_writing(
    automation_manager, tmp_path, mock_reload_service
):
    """dry_run=True resolves the same location and builds the same
    content_after a real write would, but never touches the file or
    reloads - the foundation of issue #35's dry-run + mirroring support."""
    _write(
        tmp_path,
        "automations.yaml",
        "- id: existing\n  alias: Old\n  trigger: []\n  action: []\n",
    )

    result = await automation_manager.write_automation(
        "existing",
        {"alias": "New", "trigger": [], "action": []},
        dry_run=True,
    )

    assert result.location.file_path == "automations.yaml"
    assert "Old" in result.content_before
    assert "New" in result.content_after
    # Nothing live actually changed:
    assert "Old" in (tmp_path / "automations.yaml").read_text()
    assert "New" not in (tmp_path / "automations.yaml").read_text()
    mock_reload_service.assert_not_called()


@pytest.mark.asyncio
async def test_write_automation_quotes_ambiguous_state_scalars(
    automation_manager, tmp_path, mock_reload_service
):
    """A live-tested regression: writing a condition with a bare on/off/
    yes/no value used to reload as a bool, not a str - ruamel.yaml's own
    resolver (YAML 1.2) sees "off" as a perfectly safe unquoted plain
    scalar and doesn't quote it when dumping brand-new config, but Home
    Assistant's own YAML loader (PyYAML's default resolver, YAML 1.1
    semantics) reads an unquoted `off` back as `False`. That then fails
    schema validation ("expected str, got False") and HA auto-disables
    the whole automation - exactly what happened live with a
    state-condition-gated safety automation."""
    result = await automation_manager.write_automation(
        "state_condition_automation",
        {
            "alias": "Uses on/off state conditions",
            "trigger": [],
            "condition": [
                {"condition": "state", "entity_id": "switch.x", "state": "off"}
            ],
            "action": [
                {
                    "repeat": {
                        "until": [
                            {
                                "condition": "state",
                                "entity_id": "switch.y",
                                "state": "on",
                            }
                        ]
                    }
                }
            ],
        },
    )

    assert 'state: "off"' in result.content_after
    assert 'state: "on"' in result.content_after

    parsed = pyyaml.safe_load((tmp_path / "automations.yaml").read_text())
    assert parsed[0]["condition"][0]["state"] == "off"
    assert parsed[0]["action"][0]["repeat"]["until"][0]["state"] == "on"


@pytest.mark.asyncio
async def test_write_automation_quotes_base60_time_strings(
    automation_manager, tmp_path, mock_reload_service
):
    """Issue #91's root cause: ruamel.yaml (YAML 1.2) dumps a new "17:00:00"
    unquoted, but HA's PyYAML loader (YAML 1.1) reads an unquoted
    `17:00:00` as the base-60 int 61200 - HA then rejects the time
    condition ("Invalid time specified: 61200") and disables the
    automation."""
    result = await automation_manager.write_automation(
        "time_window",
        {
            "alias": "Time window",
            "triggers": [],
            "conditions": [
                {"condition": "time", "after": "07:00:00", "before": "17:00:00"},
                {"condition": "time", "after": "9:30"},
            ],
            "actions": [],
        },
    )

    assert 'before: "17:00:00"' in result.content_after
    assert 'after: "9:30"' in result.content_after
    parsed = pyyaml.safe_load((tmp_path / "automations.yaml").read_text())
    assert parsed[0]["conditions"][0]["after"] == "07:00:00"
    assert parsed[0]["conditions"][0]["before"] == "17:00:00"
    assert parsed[0]["conditions"][1]["after"] == "9:30"


@pytest.mark.asyncio
async def test_write_automation_preserves_formatting_of_unchanged_fields(
    automation_manager, tmp_path, mock_reload_service
):
    """Issue #91: editing one field used to swap the whole automation for
    the caller's plain dict, so every untouched field in it lost its
    original formatting too - e.g. `state: 'off'` two lines away from the
    edited `before:` came back as `state: "off"`."""
    _write(
        tmp_path,
        "automations.yaml",
        "- id: '1760425761699'\n"
        "  alias: Solar Heating\n"
        "  triggers:\n"
        "  - trigger: state\n"
        "    entity_id: sensor.excess  # grid export\n"
        "  conditions:\n"
        "  - condition: state\n"
        "    entity_id: switch.heater\n"
        "    state: 'off'\n"
        "  - condition: time\n"
        "    after: '07:00:00'\n"
        "    before: 61200\n"
        "  - condition: numeric_state\n"
        "    entity_id: sensor.excess\n"
        "    above: 0x10\n"
        "  actions: []\n"
        "  mode: single\n",
    )
    before = (tmp_path / "automations.yaml").read_text()

    result = await automation_manager.write_automation(
        "1760425761699",
        {
            "alias": "Solar Heating",
            "triggers": [{"trigger": "state", "entity_id": "sensor.excess"}],
            "conditions": [
                {"condition": "state", "entity_id": "switch.heater", "state": "off"},
                {"condition": "time", "after": "07:00:00", "before": "17:00:00"},
                {
                    "condition": "numeric_state",
                    "entity_id": "sensor.excess",
                    "above": 16,
                },
            ],
            "actions": [],
            "mode": "single",
        },
    )

    # Exactly one line changed; every other line (quote style, the hex
    # int, the comment) is byte-for-byte what it was.
    assert result.content_after == before.replace(
        "    before: 61200\n", '    before: "17:00:00"\n'
    )
    parsed = pyyaml.safe_load(result.content_after)
    assert parsed[0]["conditions"][1]["before"] == "17:00:00"


@pytest.mark.asyncio
async def test_write_automation_patch_adds_and_removes_keys_and_list_items(
    automation_manager, tmp_path, mock_reload_service
):
    _write(
        tmp_path,
        "automations.yaml",
        "- id: patched\n"
        "  alias: 'Patched'\n"
        "  description: Goes away\n"
        "  triggers: []\n"
        "  conditions:\n"
        "  - condition: state\n"
        "    entity_id: switch.a\n"
        "    state: 'on'\n"
        "  - condition: state\n"
        "    entity_id: switch.b\n"
        "    state: 'on'\n"
        "  actions: []\n",
    )

    await automation_manager.write_automation(
        "patched",
        {
            "alias": "Patched",
            "triggers": [],
            "conditions": [
                {"condition": "state", "entity_id": "switch.a", "state": "on"}
            ],
            "actions": [],
            "mode": "restart",
        },
    )

    raw = (tmp_path / "automations.yaml").read_text()
    assert "alias: 'Patched'" in raw
    assert "state: 'on'" in raw
    assert "description" not in raw
    assert "switch.b" not in raw
    parsed = pyyaml.safe_load(raw)
    assert parsed == [
        {
            "id": "patched",
            "alias": "Patched",
            "triggers": [],
            "conditions": [
                {"condition": "state", "entity_id": "switch.a", "state": "on"}
            ],
            "actions": [],
            "mode": "restart",
        }
    ]


@pytest.mark.asyncio
async def test_write_automation_does_not_patch_shared_anchor_in_place(
    automation_manager, tmp_path, mock_reload_service
):
    """Patching an anchored node in place would silently change every other
    automation aliasing it - so it's replaced instead, like before #91."""
    _write(
        tmp_path,
        "automations.yaml",
        "- id: first\n"
        "  alias: First\n"
        "  triggers: []\n"
        "  conditions: &shared\n"
        "  - condition: state\n"
        "    entity_id: switch.a\n"
        "    state: 'on'\n"
        "  actions: []\n"
        "- id: second\n"
        "  alias: Second\n"
        "  triggers: []\n"
        "  conditions: *shared\n"
        "  actions: []\n",
    )

    await automation_manager.write_automation(
        "first",
        {
            "alias": "First",
            "triggers": [],
            "conditions": [
                {"condition": "state", "entity_id": "switch.z", "state": "on"}
            ],
            "actions": [],
        },
    )

    parsed = pyyaml.safe_load((tmp_path / "automations.yaml").read_text())
    assert parsed[0]["conditions"][0]["entity_id"] == "switch.z"
    assert parsed[1]["conditions"][0]["entity_id"] == "switch.a"


@pytest.mark.asyncio
async def test_write_automation_requotes_unchanged_but_misread_plain_scalar(
    automation_manager, tmp_path, mock_reload_service
):
    """An unchanged value is only kept as-is if HA reads it back correctly -
    an existing unquoted `state: off` (which HA reads as False) gets quoted
    when the caller writes the string "off" for it."""
    _write(
        tmp_path,
        "automations.yaml",
        "- id: bare_off\n"
        "  triggers: []\n"
        "  conditions:\n"
        "  - condition: state\n"
        "    entity_id: switch.a\n"
        "    state: off\n"
        "  actions: []\n",
    )

    result = await automation_manager.write_automation(
        "bare_off",
        {
            "triggers": [],
            "conditions": [
                {"condition": "state", "entity_id": "switch.a", "state": "off"}
            ],
            "actions": [],
        },
    )

    assert 'state: "off"' in result.content_after
    parsed = pyyaml.safe_load(result.content_after)
    assert parsed[0]["conditions"][0]["state"] == "off"


@pytest.mark.asyncio
async def test_write_automation_missing_package_raises(automation_manager, tmp_path):
    with pytest.raises(AutomationNotFoundError):
        await automation_manager.write_automation(
            "id1", {"trigger": [], "action": []}, package="does_not_exist.yaml"
        )


@pytest.mark.asyncio
async def test_write_automation_duplicate_id_refuses_to_guess(
    automation_manager, tmp_path
):
    _write(tmp_path, "automations.yaml", "- id: dup\n  trigger: []\n  action: []\n")
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "automation:\n  - id: dup\n    trigger: []\n    action: []\n",
    )

    with pytest.raises(DuplicateAutomationIdError):
        await automation_manager.write_automation("dup", {"trigger": [], "action": []})


@pytest.mark.asyncio
async def test_write_automation_hash_conflict_raises(automation_manager, tmp_path):
    _write(tmp_path, "automations.yaml", "- id: abc\n  trigger: []\n  action: []\n")

    with pytest.raises(ValueError, match="Hash conflict"):
        await automation_manager.write_automation(
            "abc",
            {"alias": "changed", "trigger": [], "action": []},
            expected_hash="0000000000000000000000000000000000000000000000000000000000000000",
        )


@pytest.mark.asyncio
async def test_delete_automation_removes_from_default_file(
    automation_manager, tmp_path, mock_reload_service
):
    _write(
        tmp_path,
        "automations.yaml",
        "- id: keep\n  alias: Keep\n  trigger: []\n  action: []\n"
        "- id: gone\n  alias: Gone\n  trigger: []\n  action: []\n",
    )

    result = await automation_manager.delete_automation("gone")

    assert result.location.file_path == "automations.yaml"
    assert "gone" not in result.content_after
    mock_reload_service.assert_called_once()

    parsed = pyyaml.safe_load((tmp_path / "automations.yaml").read_text())
    ids = {entry["id"] for entry in parsed}
    assert ids == {"keep"}


@pytest.mark.asyncio
async def test_delete_automation_removes_from_package_preserves_other_content(
    automation_manager, tmp_path, mock_reload_service
):
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "# EMHAS package - hand maintained, do not reformat\n"
        "automation:\n"
        "  - id: keep\n    trigger: []\n    action: []\n"
        "  - id: gone\n    trigger: []\n    action: []\n"
        "input_boolean:\n  unrelated_helper: {}\n",
    )

    result = await automation_manager.delete_automation("gone")

    assert result.location.file_path == "packages/emhas.yaml"
    assert result.location.is_package is True

    raw = (tmp_path / "packages/emhas.yaml").read_text()
    assert "# EMHAS package - hand maintained, do not reformat" in raw
    assert "unrelated_helper" in raw

    parsed = pyyaml.safe_load(raw)
    ids = {entry["id"] for entry in parsed["automation"]}
    assert ids == {"keep"}
    assert parsed["input_boolean"]["unrelated_helper"] == {}


@pytest.mark.asyncio
async def test_delete_automation_removes_single_mapping_automation_from_package(
    automation_manager, tmp_path, mock_reload_service
):
    """A package's `automation:` key can be a single mapping instead of a
    list (HA allows both) - deleting the only automation there should
    normalize it to a list the same way write_automation's _build_content
    does, leaving an empty list rather than raising."""
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "automation:\n  id: gone\n  trigger: []\n  action: []\n"
        "input_boolean:\n  unrelated_helper: {}\n",
    )

    result = await automation_manager.delete_automation("gone")

    assert result.location.file_path == "packages/emhas.yaml"
    parsed = pyyaml.safe_load((tmp_path / "packages/emhas.yaml").read_text())
    assert parsed["automation"] == []
    assert parsed["input_boolean"]["unrelated_helper"] == {}


@pytest.mark.asyncio
async def test_delete_automation_dry_run_computes_content_without_writing(
    automation_manager, tmp_path, mock_reload_service
):
    _write(
        tmp_path,
        "automations.yaml",
        "- id: target\n  alias: Target\n  trigger: []\n  action: []\n",
    )

    result = await automation_manager.delete_automation("target", dry_run=True)

    assert "target" not in result.content_after
    # Nothing live actually changed:
    assert "target" in (tmp_path / "automations.yaml").read_text()
    mock_reload_service.assert_not_called()


@pytest.mark.asyncio
async def test_delete_automation_not_found_raises(automation_manager, tmp_path):
    _write(tmp_path, "automations.yaml", "- id: abc\n  trigger: []\n  action: []\n")

    with pytest.raises(AutomationNotFoundError):
        await automation_manager.delete_automation("does_not_exist")


@pytest.mark.asyncio
async def test_delete_automation_duplicate_id_refuses_to_guess(
    automation_manager, tmp_path
):
    _write(tmp_path, "automations.yaml", "- id: dup\n  trigger: []\n  action: []\n")
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "automation:\n  - id: dup\n    trigger: []\n    action: []\n",
    )

    with pytest.raises(DuplicateAutomationIdError):
        await automation_manager.delete_automation("dup")


@pytest.mark.asyncio
async def test_delete_automation_hash_conflict_raises(automation_manager, tmp_path):
    _write(tmp_path, "automations.yaml", "- id: abc\n  trigger: []\n  action: []\n")

    with pytest.raises(ValueError, match="Hash conflict"):
        await automation_manager.delete_automation(
            "abc",
            expected_hash="0000000000000000000000000000000000000000000000000000000000000000",
        )


# --- _automation_list edge cases (via all_automations) ---------------------------


@pytest.mark.asyncio
async def test_all_automations_empty_default_file_yields_nothing(
    automation_manager, tmp_path
):
    """An empty automations.yaml parses to None, not []: _automation_list
    must treat that as an empty list rather than raising or crashing."""
    _write(tmp_path, "automations.yaml", "")

    results = await automation_manager.all_automations()

    assert results == []


@pytest.mark.asyncio
async def test_all_automations_default_file_not_a_list_raises(
    automation_manager, tmp_path
):
    """automations.yaml must be a list at its root - a mapping there is an
    invalid configuration, and should be reported rather than silently
    treated as having zero automations."""
    _write(tmp_path, "automations.yaml", "homeassistant:\n  name: Test\n")

    with pytest.raises(ValueError, match="does not contain a YAML list"):
        await automation_manager.all_automations()


@pytest.mark.asyncio
async def test_all_automations_package_without_automation_key_yields_nothing(
    automation_manager, tmp_path
):
    """A package file that doesn't define the `automation:` domain at all
    (e.g. only helpers) contributes nothing, rather than erroring."""
    _write(tmp_path, "packages/helpers_only.yaml", "input_boolean:\n  foo: {}\n")

    results = await automation_manager.all_automations()

    assert results == []


@pytest.mark.asyncio
async def test_all_automations_package_automation_key_wrong_type_raises(
    automation_manager, tmp_path
):
    """A package's `automation:` key must be a list or single mapping - any
    other type (here, a bare scalar) is an invalid configuration."""
    _write(tmp_path, "packages/broken.yaml", "automation: not_a_list_or_mapping\n")

    with pytest.raises(ValueError, match="is not a list or mapping"):
        await automation_manager.all_automations()


# --- _load_document direct coverage -----------------------------------------------


@pytest.mark.asyncio
async def test_load_document_returns_none_for_missing_file(
    automation_manager, tmp_path
):
    """_load_document's FileNotFoundError->None fallback exists for callers
    reading a candidate file that may not have been created yet - exercised
    directly since candidate_files() itself never returns a nonexistent path."""
    document = await automation_manager._load_document("automations.yaml")

    assert document is None


# --- _build_content edge cases (via write_automation) -----------------------------


@pytest.mark.asyncio
async def test_write_automation_into_empty_package_file(
    automation_manager, tmp_path, mock_reload_service
):
    """Writing a new automation into a package file that exists but is
    empty (no parsed document at all yet) must build a fresh document
    rather than crashing on a None document."""
    _write(tmp_path, "packages/empty.yaml", "")

    result = await automation_manager.write_automation(
        "first_one",
        {"alias": "First", "trigger": [], "action": []},
        package="empty.yaml",
    )

    assert result.location.file_path == "packages/empty.yaml"
    parsed = pyyaml.safe_load((tmp_path / "packages/empty.yaml").read_text())
    assert parsed["automation"][0]["id"] == "first_one"


@pytest.mark.asyncio
async def test_write_automation_appends_to_package_with_single_mapping_automation(
    automation_manager, tmp_path, mock_reload_service
):
    """A package whose `automation:` key is still a single mapping (not yet
    a list) must be normalized to a list before appending the new automation,
    rather than the new entry clobbering or corrupting the existing one."""
    _write(
        tmp_path,
        "packages/single.yaml",
        "automation:\n  id: existing_single\n  trigger: []\n  action: []\n",
    )

    result = await automation_manager.write_automation(
        "new_id",
        {"alias": "New", "trigger": [], "action": []},
        package="single.yaml",
    )

    assert result.location.file_path == "packages/single.yaml"
    parsed = pyyaml.safe_load((tmp_path / "packages/single.yaml").read_text())
    ids = {entry["id"] for entry in parsed["automation"]}
    assert ids == {"existing_single", "new_id"}


@pytest.mark.asyncio
async def test_write_automation_into_package_missing_automation_key(
    automation_manager, tmp_path, mock_reload_service
):
    """A package file with content but no `automation:` key at all must get
    a fresh automation list built for it, alongside its existing content."""
    _write(tmp_path, "packages/other_domain.yaml", "input_boolean:\n  foo: {}\n")

    result = await automation_manager.write_automation(
        "brand_new",
        {"alias": "Brand new", "trigger": [], "action": []},
        package="other_domain.yaml",
    )

    assert result.location.file_path == "packages/other_domain.yaml"
    parsed = pyyaml.safe_load((tmp_path / "packages/other_domain.yaml").read_text())
    assert parsed["automation"][0]["id"] == "brand_new"
    assert parsed["input_boolean"]["foo"] == {}
