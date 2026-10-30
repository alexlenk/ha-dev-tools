"""set_automation_enabled and write_automation's `enabled` (issue #184)."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools.llm_api import (
    SetAutomationEnabledTool,
    WriteAutomationTool,
)
from tests.test_llm_api import _arm, _llm_context, _write_automation_manager


@pytest.fixture
async def admin(hass: HomeAssistant):
    return MockUser(is_owner=True).add_to_hass(hass)


@pytest.fixture
def automations(hass: HomeAssistant, tmp_path):
    manager = _write_automation_manager(hass, tmp_path)
    (tmp_path / "automations.yaml").write_text(
        "- id: lights\n  alias: Lights\n"
        "  triggers:\n    - trigger: sun\n      event: sunset\n  actions: []\n"
        "- id: twin\n  alias: Twin\n  triggers: []\n  actions: []\n"
    )
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "twin.yaml").write_text(
        "automation:\n  - id: twin\n    alias: Twin\n    triggers: []\n"
        "    actions: []\n"
    )
    hass.states.async_set(
        "automation.lights", "off", {"id": "lights", "friendly_name": "Lights"}
    )
    hass.states.async_set("automation.twin", "on", {"id": "twin"})

    async def switch(call):
        state = "on" if call.service == "turn_on" else "off"
        entity_id = call.data["entity_id"]
        attributes = dict(hass.states.get(entity_id).attributes)
        hass.states.async_set(entity_id, state, attributes)

    hass.services.async_register("automation", "turn_on", switch)
    hass.services.async_register("automation", "turn_off", switch)
    hass.services.async_register("automation", "reload", AsyncMock())
    return manager


async def _call(hass, tool, args, user):
    return await tool.async_call(
        hass,
        llm.ToolInput(tool_name=tool.name, tool_args=args),
        _llm_context(user.id),
    )


async def test_set_automation_enabled_flow(hass: HomeAssistant, automations, admin):
    _arm(hass)
    tool = SetAutomationEnabledTool(automations)

    refused = await _call(hass, tool, {"automation_id": "gone", "enabled": True}, admin)
    assert refused.error is True and "confirm_token" not in refused.data
    both = await _call(
        hass,
        tool,
        {"automation_id": "lights", "automation_ids": ["twin"], "enabled": True},
        admin,
    )
    assert both.error is True

    args = {"automation_ids": ["lights", "twin"], "enabled": True}
    proposal = (await _call(hass, tool, args, admin)).data
    lights, twin = proposal["would_set"]
    assert lights["triggers"] == ["sun: event=sunset"]
    assert lights["change"] == "off -> on"
    # Defined twice: no triggers to show, but still switchable.
    assert twin["triggers"] == [] and twin["change"] == "already on"
    assert hass.states.get("automation.lights").state == "off"

    done = await _call(
        hass, tool, {**args, "confirm_token": proposal["confirm_token"]}, admin
    )
    assert done.error is False
    assert [r["state_after"] for r in done.data["results"]] == ["on", "on"]
    assert hass.states.get("automation.lights").state == "on"


async def test_write_automation_enabled(hass: HomeAssistant, automations, admin):
    tool = WriteAutomationTool(automations)
    config = {"alias": "Lights", "triggers": [], "actions": []}

    result = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="write_automation",
            tool_args={"automation_id": "lights", "config": config, "enabled": True},
        ),
        _llm_context(admin.id),
    )
    assert result["enabled"] == {
        "automation_id": "lights",
        "entity_id": "automation.lights",
        "state_before": "off",
        "state_after": "on",
    }

    # A new automation the reload didn't bring up: reported, not raised.
    missing = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="write_automation",
            tool_args={
                "automation_id": "brand_new",
                "config": config,
                "enabled": False,
            },
        ),
        _llm_context(),
    )
    assert missing["file_path"] == "automations.yaml"
    assert "no automation.* entity" in missing["enabled"]["error"]
