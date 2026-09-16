"""Tests for the automation static-analysis audit (audit_manager.py)."""

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools.audit_manager import (
    audit_automations,
    find_automation_state,
)
from custom_components.ha_dev_tools.automation_manager import AutomationManager
from custom_components.ha_dev_tools.file_manager import FileManager
from custom_components.ha_dev_tools.security import SecurityManager


@pytest.fixture
def security_manager(hass: HomeAssistant):
    return SecurityManager(
        hass,
        {
            "read_paths": ["automations.yaml", "packages/**/*.yaml"],
            "write_paths": [],
            "denied_paths": [],
        },
    )


@pytest.fixture
def automation_manager(hass: HomeAssistant, security_manager, tmp_path):
    hass.config.config_dir = str(tmp_path)
    file_manager = FileManager(hass, security_manager)
    return AutomationManager(hass, file_manager)


def _write(tmp_path: Path, rel_path: str, content: str) -> None:
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)


@pytest.mark.asyncio
async def test_audit_finds_duplicate_ids_across_files(automation_manager, tmp_path):
    _write(tmp_path, "automations.yaml", "- id: dup\n  trigger: []\n  action: []\n")
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "automation:\n  - id: dup\n    trigger: []\n    action: []\n",
    )

    result = await audit_automations(automation_manager.hass, automation_manager)

    assert result["automations_checked"] == 2
    assert len(result["duplicate_ids"]) == 1
    finding = result["duplicate_ids"][0]
    assert finding["automation_id"] == "dup"
    assert set(finding["files"]) == {"automations.yaml", "packages/emhas.yaml"}


@pytest.mark.asyncio
async def test_audit_no_duplicates_when_ids_unique(automation_manager, tmp_path):
    _write(tmp_path, "automations.yaml", "- id: a\n  trigger: []\n  action: []\n")
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "automation:\n  - id: b\n    trigger: []\n    action: []\n",
    )

    result = await audit_automations(automation_manager.hass, automation_manager)

    assert result["duplicate_ids"] == []


@pytest.mark.asyncio
async def test_audit_finds_unavailable_entity_references(automation_manager, tmp_path):
    automation_manager.hass.states.async_set("light.broken", "unavailable")
    automation_manager.hass.states.async_set("light.fine", "on")
    _write(
        tmp_path,
        "automations.yaml",
        "- id: uses_broken\n"
        "  trigger:\n"
        "    - platform: state\n"
        "      entity_id: light.broken\n"
        "  action:\n"
        "    - service: light.turn_on\n"
        "      target:\n"
        "        entity_id: light.fine\n",
    )

    result = await audit_automations(automation_manager.hass, automation_manager)

    assert len(result["references_unavailable_entities"]) == 1
    finding = result["references_unavailable_entities"][0]
    assert finding["automation_id"] == "uses_broken"
    assert finding["unavailable_entities"] == ["light.broken"]


@pytest.mark.asyncio
async def test_audit_ignores_available_and_unknown_entities_not_referenced(
    automation_manager, tmp_path
):
    automation_manager.hass.states.async_set("light.fine", "on")
    _write(
        tmp_path,
        "automations.yaml",
        "- id: clean\n"
        "  trigger:\n"
        "    - platform: state\n"
        "      entity_id: light.fine\n"
        "  action: []\n",
    )

    result = await audit_automations(automation_manager.hass, automation_manager)

    assert result["references_unavailable_entities"] == []


@pytest.mark.asyncio
async def test_audit_handles_no_automations_at_all(automation_manager):
    result = await audit_automations(automation_manager.hass, automation_manager)

    assert result["automations_checked"] == 0
    assert result["duplicate_ids"] == []
    assert result["references_unavailable_entities"] == []
    assert result["currently_disabled"] == []


def test_find_automation_state_matches_by_id_attribute_not_entity_id(hass: HomeAssistant):
    # entity_id is derived from `alias` (slugified) - deliberately not
    # "automation.some_id" here, to prove the lookup can't be relying on
    # entity_id matching the config id.
    hass.states.async_set(
        "automation.turn_on_lights_at_sunset", "on", {"id": "some_id"}
    )

    state = find_automation_state(hass, "some_id")

    assert state is not None
    assert state.entity_id == "automation.turn_on_lights_at_sunset"


def test_find_automation_state_returns_none_when_no_matching_entity(
    hass: HomeAssistant,
):
    hass.states.async_set("automation.unrelated", "on", {"id": "other_id"})

    assert find_automation_state(hass, "some_id") is None


@pytest.mark.asyncio
async def test_audit_reports_currently_disabled_automations(
    automation_manager, tmp_path
):
    hass = automation_manager.hass
    hass.states.async_set("automation.off_one", "off", {"id": "off_id"})
    hass.states.async_set("automation.on_one", "on", {"id": "on_id"})
    _write(
        tmp_path,
        "automations.yaml",
        "- id: off_id\n  trigger: []\n  action: []\n"
        "- id: on_id\n  trigger: []\n  action: []\n"
        "- id: never_loaded\n  trigger: []\n  action: []\n",
    )

    result = await audit_automations(hass, automation_manager)

    assert result["currently_disabled"] == ["off_id"]


@pytest.mark.asyncio
async def test_audit_tags_unavailable_findings_with_enabled_state(
    automation_manager, tmp_path
):
    hass = automation_manager.hass
    hass.states.async_set("light.broken", "unavailable")
    hass.states.async_set("automation.off_one", "off", {"id": "off_and_broken"})
    _write(
        tmp_path,
        "automations.yaml",
        "- id: off_and_broken\n"
        "  trigger:\n"
        "    - platform: state\n"
        "      entity_id: light.broken\n"
        "  action: []\n",
    )

    result = await audit_automations(hass, automation_manager)

    assert len(result["references_unavailable_entities"]) == 1
    finding = result["references_unavailable_entities"][0]
    assert finding["currently_enabled"] is False
