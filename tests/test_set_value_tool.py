"""The set_value tool (issue #194)."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools.llm_api import SetValueTool
from tests.test_llm_api import _arm, _llm_context, _write_automation_manager


async def test_set_value_flow(hass: HomeAssistant, tmp_path):
    manager = _write_automation_manager(hass, tmp_path)
    (tmp_path / "automations.yaml").write_text(
        "- id: notify_state\n  alias: Notify state\n"
        "  triggers:\n    - trigger: state\n      entity_id: input_select.house\n"
        "  actions: []\n"
        "- id: unrelated\n  alias: Unrelated\n"
        "  triggers:\n    - trigger: state\n      entity_id: input_select.house_mode\n"
        "  actions: []\n"
        "- alias: No id\n"
        "  trigger:\n    - platform: state\n      entity_id: input_select.house\n"
        "  actions: []\n"
    )
    hass.states.async_set("automation.notify_state", "on", {"id": "notify_state"})
    hass.states.async_set(
        "input_select.house", "home", {"options": ["home", "away", "vacation"]}
    )

    async def select(call):
        hass.states.async_set(
            "input_select.house", call.data["option"], {"options": ["home", "away"]}
        )

    hass.services.async_register("input_select", "select_option", select)
    admin = MockUser(is_owner=True).add_to_hass(hass)
    _arm(hass)
    tool = SetValueTool(manager)

    async def call(args):
        return (
            await tool.async_call(
                hass,
                llm.ToolInput(tool_name="set_value", tool_args=args),
                _llm_context(admin.id),
            )
        ).data

    refused = await call({"entity_id": "input_select.house", "value": "moon"})
    assert "not one of its options" in refused["error"]
    assert "confirm_token" not in refused

    args = {"entity_id": "input_select.house", "value": "away"}
    proposal = await call(args)
    assert proposal["would_set"]["service"] == "input_select.select_option"
    assert proposal["triggers_automations"] == [
        {"automation_id": "notify_state", "alias": "Notify state", "state": "on"},
        {"automation_id": None, "alias": "No id", "state": None},
    ]
    assert hass.states.get("input_select.house").state == "home"

    done = await call({**args, "confirm_token": proposal["confirm_token"]})
    assert done["state_before"] == "home" and done["state_after"] == "away"
