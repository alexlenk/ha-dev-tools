"""write_automation / write_script refuse a config HA would refuse to set up
(issue #165) - checked with HA's own validation before a token is issued or
anything is written."""

import re
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools import config_tools, write_confirmation
from custom_components.ha_dev_tools.llm_api import (
    WriteAutomationTool,
    WriteScriptTool,
)
from tests.test_llm_api import (
    _arm,
    _llm_context,
    _write_automation_manager,
    _write_script_manager,
)

# The issue's case: `for:` is a trigger option, not a condition one.
BAD_CONDITION = {
    "condition": "numeric_state",
    "entity_id": "sensor.grid_power",
    "below": -1500,
    "for": {"minutes": 10},
}
GOOD_CONDITION = {**BAD_CONDITION}
del GOOD_CONDITION["for"]


def _names_the_bad_key(error) -> bool:
    """HA's own text - its wording differs by version (voluptuous' "extra
    keys not allowed" on 2026.8, probatio's "not a valid option" on
    2026.10), the offending key and value don't."""
    return (
        bool(error)
        # voluptuous: data[...]['for'], probatio: conditions[0].for
        and re.search(r"(\['for'\]|\.for')", error) is not None
        and "'minutes': 10" in error
    )


def _automation(condition: dict) -> dict:
    return {
        "alias": "Excess on",
        "triggers": [{"trigger": "state", "entity_id": "sensor.grid_power"}],
        "conditions": [{"condition": "and", "conditions": [condition]}],
        "actions": [],
    }


@pytest.fixture
async def admin_user(hass: HomeAssistant):
    return MockUser(is_owner=True).add_to_hass(hass)


@pytest.mark.asyncio
async def test_validation_error_is_has_own_text(hass: HomeAssistant):
    error = await config_tools.validation_error(
        hass, "automation", "excess", _automation(BAD_CONDITION)
    )
    assert _names_the_bad_key(error)
    assert (
        await config_tools.validation_error(
            hass, "automation", "excess", _automation(GOOD_CONDITION)
        )
        is None
    )
    script_error = await config_tools.validation_error(
        hass, "script", "excess", {"sequence": [BAD_CONDITION]}
    )
    assert _names_the_bad_key(script_error)
    assert (
        await config_tools.validation_error(
            hass, "script", "excess", {"sequence": [GOOD_CONDITION]}
        )
        is None
    )
    # A blueprint that doesn't exist: HA raises its own error, not Invalid.
    blueprint = await config_tools.validation_error(
        hass,
        "automation",
        "from_blueprint",
        {"use_blueprint": {"path": "nope/missing.yaml", "input": {}}},
    )
    assert blueprint and "missing.yaml" in blueprint


async def _call(hass, tool, user, **args):
    return (
        await tool.async_call(
            hass,
            llm.ToolInput(tool_name=tool.name, tool_args=args),
            _llm_context(user.id),
        )
    ).data


@pytest.mark.asyncio
async def test_write_automation_refuses_before_a_token_or_a_write(
    hass: HomeAssistant, setup_integration_with_entry, admin_user, tmp_path
):
    manager = _write_automation_manager(hass, tmp_path)
    hass.services.async_register("automation", "reload", AsyncMock())
    _arm(hass)
    old = "- id: excess\n  alias: Old\n  triggers: []\n  actions: []\n"
    (tmp_path / "automations.yaml").write_text(old)
    tool = WriteAutomationTool(manager)
    bad = {"automation_id": "excess", "config": _automation(BAD_CONDITION)}

    proposal = await _call(hass, tool, admin_user, **bad)
    assert "confirm_token" not in proposal
    assert _names_the_bad_key(proposal["error"])
    assert proposal["error_type"] == "Invalid"

    # Even with a token (e.g. from an earlier, valid-looking proposal for
    # these exact arguments), the confirm call writes nothing.
    token = write_confirmation.create_pending(hass, tool.name, bad)
    confirmed = await _call(hass, tool, admin_user, **bad, confirm_token=token)
    assert _names_the_bad_key(confirmed["error"])
    assert (tmp_path / "automations.yaml").read_text() == old

    # The corrected config goes through as before.
    good = {"automation_id": "excess", "config": _automation(GOOD_CONDITION)}
    proposal = await _call(hass, tool, admin_user, **good)
    assert proposal["confirmation_required"] is True
    result = await _call(
        hass, tool, admin_user, **good, confirm_token=proposal["confirm_token"]
    )
    assert "error" not in result
    assert "Excess on" in (tmp_path / "automations.yaml").read_text()


@pytest.mark.asyncio
async def test_write_script_refuses_before_a_token_or_a_write(
    hass: HomeAssistant, setup_integration_with_entry, admin_user, tmp_path
):
    manager = _write_script_manager(hass, tmp_path)
    _arm(hass)
    old = "morning:\n  sequence: []\n"
    (tmp_path / "scripts.yaml").write_text(old)
    tool = WriteScriptTool(manager)
    bad = {"script_id": "morning", "config": {"sequence": [BAD_CONDITION]}}

    proposal = await _call(hass, tool, admin_user, **bad)
    assert "confirm_token" not in proposal
    assert _names_the_bad_key(proposal["error"])
    assert (tmp_path / "scripts.yaml").read_text() == old

    good = {"script_id": "morning", "config": {"sequence": [GOOD_CONDITION]}}
    proposal = await _call(hass, tool, admin_user, **good)
    assert proposal["confirmation_required"] is True


@pytest.mark.asyncio
async def test_an_unexpected_validator_failure_isnt_swallowed(hass: HomeAssistant):
    """Only HA's validation errors are a refusal; anything else surfaces
    (as a tool error through GatedTool), rather than passing as valid."""
    from unittest.mock import patch

    with (
        patch(
            "homeassistant.components.automation.config.async_validate_config_item",
            AsyncMock(side_effect=RuntimeError("validator broke")),
        ),
        pytest.raises(RuntimeError, match="validator broke"),
    ):
        await config_tools.validation_error(hass, "automation", "x", {})
