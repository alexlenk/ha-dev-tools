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
