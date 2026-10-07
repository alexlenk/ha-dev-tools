"""Tests for trace_manager.py, against real automation and script runs.

Each test sets up real automations/scripts, fires the events that trigger
them, and reads the traces Home Assistant actually recorded through the
same trace/list and trace/get WS commands the Traces view uses.
"""

import inspect

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools.const import DOMAIN
from custom_components.ha_dev_tools.llm_api import GetTraceTool, ListTracesTool
from custom_components.ha_dev_tools.trace_manager import (
    TraceNotFoundError,
    get_trace,
    list_traces,
    resolve_item,
)
from custom_components.ha_dev_tools.ws_call import WebSocketCommandError

AUTOMATIONS = [
    {
        "id": "porch_light",
        "alias": "Porch light",
        "triggers": [{"trigger": "event", "event_type": "porch"}],
        "conditions": [
            {"condition": "template", "value_template": "{{ trigger.event.data.go }}"}
        ],
        "actions": [{"action": "script.announce"}],
    },
    {
        "id": "broken",
        "alias": "Broken",
        "triggers": [{"trigger": "event", "event_type": "broken"}],
        "actions": [{"action": "nope.nope"}],
    },
]
SCRIPTS = {"announce": {"sequence": [{"event": "announced"}]}}


@pytest.fixture
async def admin_user(hass: HomeAssistant):
    return MockUser(is_owner=True).add_to_hass(hass)


async def _setup(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "websocket_api", {})
    assert await async_setup_component(hass, "script", {"script": SCRIPTS})
    assert await async_setup_component(hass, "automation", {"automation": AUTOMATIONS})
    await hass.async_block_till_done()


async def _fire(hass: HomeAssistant, event_type: str, **data) -> None:
    hass.bus.async_fire(event_type, data)
    await hass.async_block_till_done()


def _llm_context(user_id: str) -> llm.LLMContext:
    fields = {
        "platform": DOMAIN,
        "context": Context(user_id=user_id),
        "user_prompt": None,
        "language": "en",
        "assistant": "test",
        "device_id": None,
    }
    accepted = set(inspect.signature(llm.LLMContext.__init__).parameters)
    return llm.LLMContext(**{k: v for k, v in fields.items() if k in accepted})


@pytest.mark.asyncio
async def test_list_traces_newest_first_with_outcomes(hass: HomeAssistant, admin_user):
    await _setup(hass)
    await _fire(hass, "porch", go=False)
    await _fire(hass, "porch", go=True)
    await _fire(hass, "broken")

    listed = await list_traces(hass, admin_user, domain="automation")

    assert listed["total"] == 3
    assert listed["truncated"] is False
    rows = listed["traces"]
    assert [row["item_id"] for row in rows] == ["broken", "porch_light", "porch_light"]
    assert rows[0]["entity_id"] == "automation.broken"
    assert rows[0]["script_execution"] == "error"
    assert "nope.nope" in rows[0]["error"]
    assert rows[1]["script_execution"] == "finished"
    assert rows[2]["script_execution"] == "failed_conditions"
    assert rows[2]["trigger"] == "event 'porch'"

    errors = await list_traces(hass, admin_user, domain="automation", errors_only=True)
    assert [row["item_id"] for row in errors["traces"]] == ["broken"]

    one = await list_traces(
        hass, admin_user, domain="automation", item_id="porch_light", limit=1
    )
    assert one["total"] == 2
    assert one["truncated"] is True
    assert one["traces"][0]["script_execution"] == "finished"


@pytest.mark.asyncio
async def test_get_trace_defaults_to_latest_run_with_steps_in_order(
    hass: HomeAssistant, admin_user
):
    await _setup(hass)
    await _fire(hass, "porch", go=False)
    await _fire(hass, "porch", go=True)

    trace = await get_trace(
        hass, admin_user, domain="automation", item_id="porch_light"
    )

    assert trace["script_execution"] == "finished"
    paths = [step["path"] for step in trace["steps"]]
    assert paths == ["trigger/0", "condition/0", "action/0"]
    condition = trace["steps"][1]
    assert condition["result"]["result"] is True
    trigger_vars = trace["steps"][0]["changed_variables"]["trigger"]
    assert trigger_vars["event"]["data"] == {"go": True}
    assert trace["context"]["id"]
    assert "config" not in trace

    # The action ran a script: its child_id leads to the script's own trace.
    child = trace["steps"][2]["child_id"]
    assert child["domain"] == "script"
    assert child["item_id"] == "announce"
    script_trace = await get_trace(
        hass, admin_user, domain="script", item_id="announce", run_id=child["run_id"]
    )
    assert [step["path"] for step in script_trace["steps"]] == ["sequence/0"]


@pytest.mark.asyncio
async def test_get_trace_by_run_id_and_options(hass: HomeAssistant, admin_user):
    await _setup(hass)
    await _fire(hass, "porch", go=False)
    await _fire(hass, "porch", go=True)
    listed = await list_traces(
        hass, admin_user, domain="automation", item_id="porch_light"
    )
    failed = listed["traces"][1]

    trace = await get_trace(
        hass,
        admin_user,
        domain="automation",
        item_id="porch_light",
        run_id=failed["run_id"],
        include_variables=False,
        include_config=True,
    )

    assert trace["script_execution"] == "failed_conditions"
    assert trace["steps"][-1]["path"] == "condition/0"
    assert trace["steps"][-1]["result"]["result"] is False
    assert all("changed_variables" not in step for step in trace["steps"])
    assert trace["config"]["id"] == "porch_light"


@pytest.mark.asyncio
async def test_missing_traces_and_runs(hass: HomeAssistant, admin_user):
    await _setup(hass)

    with pytest.raises(TraceNotFoundError, match="No stored traces"):
        await get_trace(hass, admin_user, domain="automation", item_id="porch_light")

    await _fire(hass, "porch", go=True)
    with pytest.raises(WebSocketCommandError, match="not_found"):
        await get_trace(
            hass, admin_user, domain="automation", item_id="porch_light", run_id="nope"
        )


@pytest.mark.asyncio
async def test_traces_are_admin_only(hass: HomeAssistant):
    await _setup(hass)
    user = MockUser(is_owner=False).add_to_hass(hass)

    with pytest.raises(WebSocketCommandError, match="unauthorized"):
        await list_traces(hass, user, domain="automation")


@pytest.mark.asyncio
async def test_resolve_item(hass: HomeAssistant):
    await _setup(hass)

    assert resolve_item(hass) == ("automation", None)
    assert resolve_item(hass, domain="script", item_id="announce") == (
        "script",
        "announce",
    )
    assert resolve_item(hass, entity_id="automation.porch_light") == (
        "automation",
        "porch_light",
    )
    assert resolve_item(hass, entity_id="script.announce") == ("script", "announce")
    with pytest.raises(ValueError, match="not an automation"):
        resolve_item(hass, entity_id="light.porch")
    with pytest.raises(ValueError, match="not in domain"):
        resolve_item(hass, domain="script", entity_id="automation.porch_light")
    with pytest.raises(ValueError, match="must be one of"):
        resolve_item(hass, domain="scene")
    with pytest.raises(TraceNotFoundError, match="only keeps traces"):
        resolve_item(hass, entity_id="automation.no_such_thing")


@pytest.mark.asyncio
async def test_trace_tools(hass: HomeAssistant, admin_user):
    await _setup(hass)
    await _fire(hass, "porch", go=True)
    context = _llm_context(admin_user.id)

    listed = await ListTracesTool()._run(
        hass,
        llm.ToolInput(
            tool_name="list_traces", tool_args={"entity_id": "automation.porch_light"}
        ),
        context,
    )
    assert [row["entity_id"] for row in listed["traces"]] == ["automation.porch_light"]

    trace = await GetTraceTool()._run(
        hass,
        llm.ToolInput(
            tool_name="get_trace", tool_args={"entity_id": "automation.porch_light"}
        ),
        context,
    )
    assert trace["run_id"] == listed["traces"][0]["run_id"]

    missing_id = await GetTraceTool()._run(
        hass,
        llm.ToolInput(tool_name="get_trace", tool_args={"domain": "automation"}),
        context,
    )
    assert missing_id["error_type"] == "ValueError"

    no_user = await ListTracesTool()._run(
        hass,
        llm.ToolInput(tool_name="list_traces", tool_args={}),
        _llm_context("not-a-user"),
    )
    assert no_user["error_type"] == "UnresolvedUserError"
