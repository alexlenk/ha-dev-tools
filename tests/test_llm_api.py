"""Tests for the dev_tools LLM API registration (llm_api.py).

Verifies the foundation of the redesigned architecture: this integration
registers into Home Assistant's own `homeassistant.helpers.llm` tool
registry, which HA's native `mcp_server` integration serves over MCP with
no custom transport code of our own. See docs/ARCHITECTURE.md.

NOTE on `llm.APIInstance.async_call_tool`: that method does its own
deferred `from homeassistant.components.conversation import (...)` purely
for conversation trace logging, unrelated to anything our tools need. In
a manually-pinned test environment (as opposed to a real HA install, where
the `homeassistant`/`hassil`/`home-assistant-intents` versions are
guaranteed compatible by HA's own release process) that import can fail or
succeed depending on unrelated factors - reproduced this directly: it
failed deterministically on Python 3.13 with a real, fresh
`pip install -r requirements-test.txt`, passed on 3.12 with the identical
versions. So tests here call `Tool.async_call()` directly to verify our
own logic, rather than going through that wrapper and becoming hostage to
HA's voice/NLU dependency chain. `test_dev_tools_ping_tool_reachable`
still proves the tool is genuinely registered and discoverable through the
real API instance - just not by making a full traced tool call.

NOTE on version skew: the real minimum supported HA version is 2026.8.2
(when `mcp_server` shipped), but this sandbox's package mirror - and, as
of this writing, a real GitHub Actions runner's real PyPI resolution too -
only has up to 2025.1.4. `llm.LLMContext` gained/lost fields and
`llm.async_register_api`'s return value changed between those versions, so
this file builds LLMContext dynamically from whatever fields the
installed version actually has, and treats the unsub-callable behavior as
best-effort rather than asserting it unconditionally.
"""

import asyncio
import base64
import inspect
import json
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest
import voluptuous as vol
import yaml
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools import (
    access_control,
    config_snapshot,
    helper_manager,
)
from custom_components.ha_dev_tools.access_control import NotAdminError, NotArmedError
from custom_components.ha_dev_tools.automation_manager import AutomationManager
from custom_components.ha_dev_tools.const import (
    OPT_DRY_RUN,
    OPT_MIRROR_ENABLED,
    OPT_MIRROR_REPO,
    OPT_MIRROR_TOKEN,
)
from custom_components.ha_dev_tools.derived_sensor_manager import (
    DerivedSensorNotFoundError,
    FlowStepRequiredError,
    InvalidDerivedSensorDomainError,
)
from custom_components.ha_dev_tools.file_manager import FileManager
from custom_components.ha_dev_tools.history_manager import RecorderNotAvailableError
from custom_components.ha_dev_tools.llm_api import (
    API_ID,
    DOMAIN,
    AuditAutomationsTool,
    CheckConfigTool,
    CreateDerivedSensorTool,
    CreateHelperTool,
    CreateTemplateEntityTool,
    DeleteAutomationTool,
    DeleteDerivedSensorTool,
    DeleteEntitiesTool,
    DeleteEntityTool,
    DeleteHelperTool,
    DeleteTemplateEntityTool,
    DevToolsPingTool,
    EntityHealthReportTool,
    FindEntitiesTool,
    GetAddonLogsTool,
    GetAutomationTool,
    GetConfigFileTool,
    GetDashboardTool,
    GetDerivedSensorTool,
    GetEnergyConfigTool,
    GetEntityHistoryTool,
    GetLogbookTool,
    GetLogsTool,
    GetRestCommandTool,
    GetScriptTool,
    GetTemplateEntityTool,
    ListAddonsTool,
    ListDashboardsTool,
    ListDerivedSensorsTool,
    ListHelpersTool,
    ListMqttTopicsTool,
    ListRestCommandsTool,
    ListScriptsTool,
    ListTemplateEntitiesTool,
    ReloadDerivedSensorTool,
    ReloadDomainTool,
    RenderTemplateTool,
    SetBooleanValueTool,
    SetNumberValueTool,
    TriggerAutomationTool,
    UpdateDerivedSensorTool,
    UpdateHelperTool,
    UpdateTemplateEntityTool,
    ValidateTemplateTool,
    WriteAutomationTool,
    WriteDashboardTool,
    WriteEnergyConfigTool,
    WriteGatedTool,
    WriteScriptTool,
    _mirror_result_payload,
    _one_or_many,
)
from custom_components.ha_dev_tools.log_manager import LogManager
from custom_components.ha_dev_tools.mirror import MirrorResult
from custom_components.ha_dev_tools.mqtt_manager import MqttNotAvailableError
from custom_components.ha_dev_tools.rest_command_manager import RestCommandManager
from custom_components.ha_dev_tools.script_manager import ScriptManager
from custom_components.ha_dev_tools.security import SecurityManager
from custom_components.ha_dev_tools.template_yaml_manager import TemplateYamlManager
from custom_components.ha_dev_tools.ws_call import WebSocketCommandError


def _without_arm(result: dict) -> dict:
    """A tool result minus the `arm` status every write tool (and the ping)
    now adds (issue #63) - for exact-payload asserts that predate it."""
    return {k: v for k, v in result.items() if k != "arm"}


def _llm_context(user_id: str | None = None) -> llm.LLMContext:
    """Build an LLMContext for calling the API, tolerant of field changes across HA versions."""
    fields = {
        "platform": DOMAIN,
        "context": Context(user_id=user_id) if user_id else None,
        "user_prompt": None,
        "language": "en",
        "assistant": "test",
        "device_id": None,
    }
    accepted = set(inspect.signature(llm.LLMContext.__init__).parameters)
    return llm.LLMContext(**{k: v for k, v in fields.items() if k in accepted})


def _arm(hass: HomeAssistant) -> None:
    """Arm dev_tools as a human would (out-of-band), for tests of gated tools."""
    path = access_control._arm_file_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()))


@pytest.fixture(autouse=True)
def _clean_arm_file(hass: HomeAssistant):
    """See test_access_control.py's identical fixture for why this is needed -
    hass.config.config_dir is a shared, non-per-test-isolated directory."""
    path = access_control._arm_file_path(hass)
    path.unlink(missing_ok=True)
    yield
    path.unlink(missing_ok=True)


@pytest.fixture
async def admin_user(hass: HomeAssistant):
    return MockUser(is_owner=True).add_to_hass(hass)


@pytest.fixture
async def non_admin_user(hass: HomeAssistant):
    return MockUser(is_owner=False).add_to_hass(hass)


@pytest.mark.asyncio
async def test_dev_tools_api_registered(
    hass: HomeAssistant, setup_integration_with_entry
):
    """The dev_tools API is registered in HA's LLM API registry after setup."""
    api_ids = {api.id for api in llm.async_get_apis(hass)}
    assert API_ID in api_ids


@pytest.mark.asyncio
async def test_dev_tools_ping_tool_reachable(
    hass: HomeAssistant, setup_integration_with_entry
):
    """The ping tool is genuinely registered and its own logic works."""
    api_instance = await llm.async_get_api(hass, API_ID, _llm_context())

    tool_names = {tool.name for tool in api_instance.tools}
    assert "dev_tools_ping" in tool_names

    result = await DevToolsPingTool().async_call(
        hass, llm.ToolInput(tool_name="dev_tools_ping", tool_args={}), _llm_context()
    )
    assert _without_arm(result) == {"status": "ok", "domain": DOMAIN}
    # Works while not armed, and says how to arm (issue #63).
    assert result["arm"]["armed"] is False
    assert "arm_command" in result["arm"]


@pytest.mark.asyncio
async def test_dev_tools_real_tools_registered(
    hass: HomeAssistant, setup_integration_with_entry
):
    """The real tool surface is registered, not just the diagnostic ping tool."""
    api_instance = await llm.async_get_api(hass, API_ID, _llm_context())

    tool_names = {tool.name for tool in api_instance.tools}
    assert tool_names == {
        "dev_tools_ping",
        "find_entities",
        "entity_health_report",
        "delete_entity",
        "delete_entities",
        "update_entities",
        "list_mqtt_topics",
        "render_template",
        "validate_template",
        "get_logs",
        "get_entity_history",
        "get_logbook",
        "list_statistics",
        "get_statistics",
        "list_addons",
        "get_addon_logs",
        "check_config",
        "get_config_file",
        "reload_domain",
        "get_automation",
        "write_automation",
        "delete_automation",
        "audit_automations",
        "trigger_automation",
        "set_number_value",
        "set_boolean_value",
        "list_scripts",
        "get_script",
        "write_script",
        "list_helpers",
        "create_helper",
        "update_helper",
        "delete_helper",
        "list_derived_sensors",
        "get_derived_sensor",
        "create_derived_sensor",
        "update_derived_sensor",
        "delete_derived_sensor",
        "reload_derived_sensor",
        "list_template_entities",
        "get_template_entity",
        "create_template_entity",
        "update_template_entity",
        "delete_template_entity",
        "list_dashboards",
        "get_dashboard",
        "write_dashboard",
        "get_energy_config",
        "write_energy_config",
        "list_rest_commands",
        "get_rest_command",
    }


@pytest.mark.asyncio
async def test_dev_tools_api_unregistered_on_unload(
    hass: HomeAssistant, setup_integration_with_entry
):
    """Unloading the config entry unregisters the dev_tools API.

    `llm.async_register_api` only started returning an unsub callable in
    newer HA versions than this sandbox can install (see module docstring);
    older versions leak the registration on unload, which is a real gap in
    those versions, not in this integration. Assert the real behavior when
    the HA version we're running against supports it; otherwise just prove
    unload doesn't crash.
    """
    from custom_components.ha_dev_tools import async_unload_entry

    unsub_supported = hass.data[DOMAIN].get("unsub_llm_api") is not None

    assert await async_unload_entry(hass, setup_integration_with_entry)

    api_ids = {api.id for api in llm.async_get_apis(hass)}
    if unsub_supported:
        assert API_ID not in api_ids


@pytest.mark.asyncio
async def test_gated_tool_refuses_when_not_armed(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    """A real gated tool (not just access_control's own unit tests) refuses when unarmed."""
    with pytest.raises(NotArmedError):
        await FindEntitiesTool().async_call(
            hass,
            llm.ToolInput(tool_name="find_entities", tool_args={}),
            _llm_context(admin_user.id),
        )


@pytest.mark.asyncio
async def test_gated_tool_refuses_non_admin_even_when_armed(
    hass: HomeAssistant, setup_integration_with_entry, non_admin_user
):
    _arm(hass)

    with pytest.raises(NotAdminError):
        await FindEntitiesTool().async_call(
            hass,
            llm.ToolInput(tool_name="find_entities", tool_args={}),
            _llm_context(non_admin_user.id),
        )


@pytest.mark.asyncio
async def test_gated_tool_succeeds_when_armed_and_admin(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    _arm(hass)
    path = access_control._arm_file_path(hass)
    mtime_before = path.stat().st_mtime

    result = await FindEntitiesTool().async_call(
        hass,
        llm.ToolInput(tool_name="find_entities", tool_args={}),
        _llm_context(admin_user.id),
    )

    assert isinstance(result, dict)
    # A successful call extends the idle window (touch_armed).
    assert path.stat().st_mtime >= mtime_before


@pytest.mark.asyncio
async def test_entity_health_report_tool_calls_manager(hass: HomeAssistant):
    """Never exercised anywhere else - a thin argument-plumbing wrapper
    around entity_manager.entity_health_report, same shape as
    FindEntitiesTool above."""
    result = await EntityHealthReportTool()._run(
        hass,
        llm.ToolInput(tool_name="entity_health_report", tool_args={}),
        _llm_context(),
    )

    assert isinstance(result, dict)
    assert "by_integration" in result


# --- DeleteEntityTool --------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_entity_tool_confirm_flow_removes_from_registry(
    hass: HomeAssistant, admin_user
):
    _arm(hass)
    entity_reg = er.async_get(hass)
    entity_reg.async_get_or_create(
        "light", "test", "kitchen_light", suggested_object_id="kitchen_light"
    )
    tool = DeleteEntityTool()
    args = {"entity_id": "light.kitchen_light"}

    proposal = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="delete_entity", tool_args=args),
        _llm_context(admin_user.id),
    )
    assert proposal["confirmation_required"] is True

    confirmed_args = {**args, "confirm_token": proposal["confirm_token"]}
    result = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="delete_entity", tool_args=confirmed_args),
        _llm_context(admin_user.id),
    )

    assert _without_arm(result) == {"deleted": True, "entity_id": "light.kitchen_light"}
    # Write tools report the arm window this call just extended (issue #63).
    assert result["arm"]["armed"] is True
    assert 29 <= result["arm"]["minutes_left"] <= 30
    assert entity_reg.async_get("light.kitchen_light") is None


@pytest.mark.asyncio
async def test_delete_entity_tool_not_found_returns_tool_error(hass: HomeAssistant):
    tool = DeleteEntityTool()

    result = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="delete_entity", tool_args={"entity_id": "light.does_not_exist"}
        ),
        _llm_context(),
    )

    assert result["error_type"] == "EntityNotFoundError"


@pytest.mark.asyncio
async def test_delete_entity_tool_no_mirror_key_when_disabled(hass: HomeAssistant):
    entity_reg = er.async_get(hass)
    entity_reg.async_get_or_create(
        "light", "test", "kitchen_light", suggested_object_id="kitchen_light"
    )
    tool = DeleteEntityTool()

    result = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="delete_entity", tool_args={"entity_id": "light.kitchen_light"}
        ),
        _llm_context(),
    )

    assert result["deleted"] is True
    assert "mirror" not in result
    assert entity_reg.async_get("light.kitchen_light") is None


@pytest.mark.asyncio
async def test_delete_entity_tool_mirrors_registry_snapshot_then_tombstone(
    hass: HomeAssistant, setup_integration_with_entry
):
    """No real file to remove - same tombstone-marker pattern as
    delete_derived_sensor: the mirrored "before" state is this entity's
    full registry snapshot, "after" is a small deletion marker."""
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    entity_reg = er.async_get(hass)
    entity_reg.async_get_or_create(
        "light", "test", "kitchen_light", suggested_object_id="kitchen_light"
    )
    entity_reg.async_update_entity("light.kitchen_light", name="Kitchen Light")
    tool = DeleteEntityTool()
    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # GET current - not mirrored yet
            _FakeMirrorResponse(200, {"content": {"sha": "sha-before"}}),  # PUT before
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT after
        ]
    )

    with patch(
        "custom_components.ha_dev_tools.mirror.async_get_clientsession",
        return_value=fake_session,
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="delete_entity",
                tool_args={"entity_id": "light.kitchen_light"},
            ),
            _llm_context(),
        )

    assert result["mirror"]["mirrored"] is True
    assert result["mirror"]["commits"] == ["before", "after"]

    put_before_json = fake_session.calls[2][2]["json"]["content"]
    before_content = json.loads(base64.b64decode(put_before_json))
    assert before_content["entity_id"] == "light.kitchen_light"
    assert before_content["name"] == "Kitchen Light"

    put_after_json = fake_session.calls[-1][2]["json"]["content"]
    after_content = json.loads(base64.b64decode(put_after_json))
    assert after_content == {"deleted": True, "entity_id": "light.kitchen_light"}


# --- DeleteEntitiesTool -------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_entities_tool_confirm_flow_removes_all_from_registry(
    hass: HomeAssistant, admin_user
):
    _arm(hass)
    entity_reg = er.async_get(hass)
    entity_reg.async_get_or_create(
        "light", "test", "kitchen_light", suggested_object_id="kitchen_light"
    )
    entity_reg.async_get_or_create(
        "light", "test", "bedroom_light", suggested_object_id="bedroom_light"
    )
    tool = DeleteEntitiesTool()
    args = {"entity_ids": ["light.kitchen_light", "light.bedroom_light"]}

    proposal = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="delete_entities", tool_args=args),
        _llm_context(admin_user.id),
    )
    assert proposal["confirmation_required"] is True

    confirmed_args = {**args, "confirm_token": proposal["confirm_token"]}
    result = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="delete_entities", tool_args=confirmed_args),
        _llm_context(admin_user.id),
    )

    assert _without_arm(result) == {
        "deleted": True,
        "entity_ids": ["light.kitchen_light", "light.bedroom_light"],
    }
    assert entity_reg.async_get("light.kitchen_light") is None
    assert entity_reg.async_get("light.bedroom_light") is None


@pytest.mark.asyncio
async def test_delete_entities_tool_not_found_returns_tool_error_deletes_none(
    hass: HomeAssistant,
):
    entity_reg = er.async_get(hass)
    entity_reg.async_get_or_create(
        "light", "test", "kitchen_light", suggested_object_id="kitchen_light"
    )
    tool = DeleteEntitiesTool()

    result = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="delete_entities",
            tool_args={"entity_ids": ["light.kitchen_light", "light.does_not_exist"]},
        ),
        _llm_context(),
    )

    assert result["error_type"] == "EntityNotFoundError"
    # Refused as a whole - the one valid id wasn't deleted either.
    assert entity_reg.async_get("light.kitchen_light") is not None


@pytest.mark.asyncio
async def test_delete_entities_tool_no_mirror_key_when_disabled(hass: HomeAssistant):
    entity_reg = er.async_get(hass)
    entity_reg.async_get_or_create(
        "light", "test", "kitchen_light", suggested_object_id="kitchen_light"
    )
    tool = DeleteEntitiesTool()

    result = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="delete_entities",
            tool_args={"entity_ids": ["light.kitchen_light"]},
        ),
        _llm_context(),
    )

    assert result["deleted"] is True
    assert "mirror" not in result
    assert entity_reg.async_get("light.kitchen_light") is None


@pytest.mark.asyncio
async def test_delete_entities_tool_mirrors_one_combined_commit_pair(
    hass: HomeAssistant, setup_integration_with_entry
):
    """The whole point of this tool over calling delete_entity in a loop:
    N entities still only ever produce one before/after commit pair, not
    one pair per entity."""
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    entity_reg = er.async_get(hass)
    entity_reg.async_get_or_create(
        "light", "test", "kitchen_light", suggested_object_id="kitchen_light"
    )
    entity_reg.async_update_entity("light.kitchen_light", name="Kitchen Light")
    entity_reg.async_get_or_create(
        "light", "test", "bedroom_light", suggested_object_id="bedroom_light"
    )
    entity_reg.async_update_entity("light.bedroom_light", name="Bedroom Light")
    tool = DeleteEntitiesTool()
    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # GET current - not mirrored yet
            _FakeMirrorResponse(200, {"content": {"sha": "sha-before"}}),  # PUT before
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT after
        ]
    )

    with patch(
        "custom_components.ha_dev_tools.mirror.async_get_clientsession",
        return_value=fake_session,
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="delete_entities",
                tool_args={
                    "entity_ids": ["light.kitchen_light", "light.bedroom_light"]
                },
            ),
            _llm_context(),
        )

    assert result["mirror"]["mirrored"] is True
    assert result["mirror"]["commits"] == ["before", "after"]
    # Exactly 4 calls total (GET repo, GET current, PUT before, PUT after) -
    # not 8, proving this was one combined push, not one per entity.
    assert len(fake_session.calls) == 4

    put_before_json = fake_session.calls[2][2]["json"]["content"]
    before_content = json.loads(base64.b64decode(put_before_json))
    assert isinstance(before_content, list)
    assert {e["entity_id"] for e in before_content} == {
        "light.kitchen_light",
        "light.bedroom_light",
    }
    names = {e["entity_id"]: e["name"] for e in before_content}
    assert names["light.kitchen_light"] == "Kitchen Light"
    assert names["light.bedroom_light"] == "Bedroom Light"

    put_after_json = fake_session.calls[-1][2]["json"]["content"]
    after_content = json.loads(base64.b64decode(put_after_json))
    assert after_content == [
        {"deleted": True, "entity_id": "light.kitchen_light"},
        {"deleted": True, "entity_id": "light.bedroom_light"},
    ]


# --- ListMqttTopicsTool -------------------------------------------------------


@pytest.mark.asyncio
async def test_list_mqtt_topics_tool_calls_manager(hass: HomeAssistant):
    tool = ListMqttTopicsTool()
    mock_list_topics = AsyncMock(
        return_value={
            "topic_filter": "watermeter/#",
            "topics": {
                "watermeter/uptime": {"payload": "1234", "retain": True, "qos": 0}
            },
            "count": 1,
            "truncated": False,
        }
    )
    with patch(
        "custom_components.ha_dev_tools.llm_api.mqtt_manager.list_topics",
        mock_list_topics,
    ):
        result = await tool._run(
            hass,
            llm.ToolInput(
                tool_name="list_mqtt_topics",
                tool_args={"topic": "watermeter/#", "timeout": 3.0, "limit": 200},
            ),
            _llm_context(),
        )

    assert result["count"] == 1
    mock_list_topics.assert_called_once_with(
        hass, topic="watermeter/#", timeout=3.0, limit=200
    )


@pytest.mark.asyncio
async def test_list_mqtt_topics_tool_not_available_returns_tool_error(
    hass: HomeAssistant,
):
    tool = ListMqttTopicsTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.mqtt_manager.list_topics",
        AsyncMock(side_effect=MqttNotAvailableError("mqtt isn't configured")),
    ):
        result = await tool._run(
            hass,
            llm.ToolInput(tool_name="list_mqtt_topics", tool_args={}),
            _llm_context(),
        )

    assert result["error_type"] == "MqttNotAvailableError"


# --- RenderTemplateTool / ValidateTemplateTool -------------------------------
#
# template_manager.py's own tests cover the actual Jinja2 rendering/
# validation logic - these just prove the tool wrapper plumbs arguments
# through and returns the manager's result, real end to end (no mocking
# needed - both run entirely in-process against live state).


@pytest.mark.asyncio
async def test_render_template_tool_renders_against_live_state(hass: HomeAssistant):
    result = await RenderTemplateTool()._run(
        hass,
        llm.ToolInput(
            tool_name="render_template",
            tool_args={"template": "{{ 1 + 1 }}"},
        ),
        _llm_context(),
    )

    assert result == {"success": True, "result": 2}


@pytest.mark.asyncio
async def test_validate_template_tool_reports_syntax_error(hass: HomeAssistant):
    """Never exercised anywhere else - covers the tool wrapper, not the
    already-tested validation logic itself."""
    result = await ValidateTemplateTool()._run(
        hass,
        llm.ToolInput(
            tool_name="validate_template", tool_args={"template": "{{ unterminated"}
        ),
        _llm_context(),
    )

    assert result["valid"] is False


# --- GetLogsTool --------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_logs_tool_returns_filtered_entries(hass: HomeAssistant):
    """Never imported by any existing test - covers both the LogFilters
    argument plumbing and the entries/count response shaping."""
    log_manager = LogManager(hass, SecurityManager(hass, {}))
    fake_entry = Mock()
    fake_entry.to_dict.return_value = {"message": "hello"}
    tool = GetLogsTool(log_manager)

    with patch.object(
        log_manager, "get_core_logs", AsyncMock(return_value=[fake_entry])
    ) as mock_get_logs:
        result = await tool._run(
            hass,
            llm.ToolInput(
                tool_name="get_logs", tool_args={"lines": 50, "level": "ERROR"}
            ),
            _llm_context(),
        )

    assert result == {"entries": [{"message": "hello"}], "count": 1}
    filters = mock_get_logs.call_args.args[0]
    assert filters.lines == 50
    assert filters.level == "ERROR"


# --- ListAddonsTool / GetAddonLogsTool (Supervisor-only tools) ---------------
#
# Can't stand up a real Supervisor in this sandbox (see
# supervisor_manager.py's module docstring) - "not available" is exercised
# for real (no hassio component registered at all here), "calls manager"
# mocks the module function the same way the derived-sensor tool tests do.


@pytest.mark.asyncio
async def test_list_addons_tool_surfaces_not_available(hass: HomeAssistant):
    result = await ListAddonsTool()._run(
        hass, llm.ToolInput(tool_name="list_addons", tool_args={}), _llm_context()
    )

    assert result["error_type"] == "SupervisorNotAvailableError"


@pytest.mark.asyncio
async def test_list_addons_tool_calls_manager(hass: HomeAssistant):
    with patch(
        "custom_components.ha_dev_tools.llm_api.supervisor_manager.list_addons",
        AsyncMock(return_value=[{"slug": "core_mosquitto"}]),
    ):
        result = await ListAddonsTool()._run(
            hass, llm.ToolInput(tool_name="list_addons", tool_args={}), _llm_context()
        )

    assert result == {"addons": [{"slug": "core_mosquitto"}]}


@pytest.mark.asyncio
async def test_get_addon_logs_tool_surfaces_not_available(hass: HomeAssistant):
    result = await GetAddonLogsTool()._run(
        hass,
        llm.ToolInput(tool_name="get_addon_logs", tool_args={"slug": "core_mosquitto"}),
        _llm_context(),
    )

    assert result["error_type"] == "SupervisorNotAvailableError"


@pytest.mark.asyncio
async def test_get_addon_logs_tool_calls_manager(hass: HomeAssistant):
    with patch(
        "custom_components.ha_dev_tools.llm_api.supervisor_manager.get_addon_logs",
        AsyncMock(return_value={"logs": "hello\n"}),
    ) as mock_get_addon_logs:
        result = await GetAddonLogsTool()._run(
            hass,
            llm.ToolInput(
                tool_name="get_addon_logs",
                tool_args={"slug": "core_mosquitto", "lines": 10},
            ),
            _llm_context(),
        )

    assert result == {"logs": "hello\n"}
    mock_get_addon_logs.assert_called_once_with(hass, "core_mosquitto", lines=10)


# --- CheckConfigTool / ReloadDomainTool ---------------------------------------


@pytest.mark.asyncio
async def test_check_config_tool_calls_manager(hass: HomeAssistant):
    with patch(
        "custom_components.ha_dev_tools.llm_api.config_tools.check_ha_config",
        AsyncMock(return_value={"valid": True}),
    ) as mock_check:
        result = await CheckConfigTool()._run(
            hass, llm.ToolInput(tool_name="check_config", tool_args={}), _llm_context()
        )

    assert result == {"valid": True}
    mock_check.assert_called_once_with(hass)


@pytest.mark.asyncio
async def test_check_config_tool_snapshots_only_a_passing_config(hass: HomeAssistant):
    """Issue #105: with mirroring on, a passing check snapshots hand-edited
    config; a failing one keeps the mirror's last good copy."""
    snapshot = AsyncMock(return_value={"committed": ["configuration.yaml"]})
    with (
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.is_mirror_enabled",
            return_value=True,
        ),
        patch(
            "custom_components.ha_dev_tools.llm_api.config_snapshot.async_snapshot",
            snapshot,
        ),
        patch(
            "custom_components.ha_dev_tools.llm_api.config_tools.check_ha_config",
            AsyncMock(side_effect=[{"errors": []}, {"errors": [{"message": "x"}]}]),
        ),
    ):
        passing = await CheckConfigTool()._run(
            hass, llm.ToolInput(tool_name="check_config", tool_args={}), _llm_context()
        )
        failing = await CheckConfigTool()._run(
            hass, llm.ToolInput(tool_name="check_config", tool_args={}), _llm_context()
        )

    assert passing["config_snapshot"] == {"committed": ["configuration.yaml"]}
    assert "configuration check failed" in failing["config_snapshot"]["skipped_all"]
    snapshot.assert_called_once_with(hass)


@pytest.mark.asyncio
async def test_get_config_file_tool_returns_file_or_error(hass: HomeAssistant):
    target = "custom_components.ha_dev_tools.llm_api.config_snapshot.get_config_file"
    with patch(target, AsyncMock(return_value={"path": "a.yaml"})) as mock_get:
        result = await GetConfigFileTool()._run(
            hass,
            llm.ToolInput(
                tool_name="get_config_file",
                tool_args={"path": "a.yaml", "source": "mirror"},
            ),
            _llm_context(),
        )
    assert result == {"path": "a.yaml"}
    mock_get.assert_called_once_with(hass, "a.yaml", "mirror", None)

    error = config_snapshot.ConfigFileError("withheld")
    with patch(target, AsyncMock(side_effect=error)):
        result = await GetConfigFileTool()._run(
            hass,
            llm.ToolInput(tool_name="get_config_file", tool_args={}),
            _llm_context(),
        )
    assert result == {"error": "withheld", "error_type": "ConfigFileError"}


@pytest.mark.asyncio
async def test_reload_domain_tool_calls_manager(hass: HomeAssistant):
    with patch(
        "custom_components.ha_dev_tools.llm_api.config_tools.reload_domain",
        AsyncMock(return_value={"reloaded": True}),
    ) as mock_reload:
        result = await ReloadDomainTool()._run(
            hass,
            llm.ToolInput(
                tool_name="reload_domain", tool_args={"domain": "automation"}
            ),
            _llm_context(),
        )

    assert result == {"reloaded": True}
    mock_reload.assert_called_once_with(hass, "automation", None)


# --- GetAutomationTool / currently_enabled ----------------------------------


def _automation_manager(hass: HomeAssistant, tmp_path) -> AutomationManager:
    hass.config.config_dir = str(tmp_path)
    security_manager = SecurityManager(
        hass,
        {
            "read_paths": ["automations.yaml", "packages/**/*.yaml"],
            "write_paths": [],
            "denied_paths": [],
        },
    )
    file_manager = FileManager(hass, security_manager)
    return AutomationManager(hass, file_manager)


@pytest.mark.asyncio
async def test_get_automation_reports_currently_enabled_true(
    hass: HomeAssistant, admin_user, tmp_path
):
    manager = _automation_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "automations.yaml").write_text(
        "- id: my_automation\n  trigger: []\n  action: []\n"
    )
    hass.states.async_set("automation.my_automation", "on", {"id": "my_automation"})

    result = await GetAutomationTool(manager).async_call(
        hass,
        llm.ToolInput(
            tool_name="get_automation", tool_args={"automation_id": "my_automation"}
        ),
        _llm_context(admin_user.id),
    )

    assert result["currently_enabled"] is True
    assert result["runtime_state_note"] is None


@pytest.mark.asyncio
async def test_get_automation_reports_misread_values(
    hass: HomeAssistant, admin_user, tmp_path
):
    """Issue #97: 'config' shows the intended text, so a bare
    `before: 17:00:00` looks fine - misread_values says HA reads 61200."""
    manager = _automation_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "automations.yaml").write_text(
        "- id: my_automation\n"
        "  trigger: []\n"
        "  condition:\n"
        "  - condition: time\n"
        "    after: '07:00:00'\n"
        "    before: 17:00:00\n"
        "  action: []\n"
    )

    result = await GetAutomationTool(manager).async_call(
        hass,
        llm.ToolInput(
            tool_name="get_automation", tool_args={"automation_id": "my_automation"}
        ),
        _llm_context(admin_user.id),
    )

    assert result["config"]["condition"][0]["before"] == "17:00:00"
    assert result["misread_values"] == [
        {
            "path": "condition[0].before",
            "line": 6,
            "written": "17:00:00",
            "ha_reads_as": 61200,
        }
    ]


@pytest.mark.asyncio
async def test_get_automation_reports_currently_enabled_false(
    hass: HomeAssistant, admin_user, tmp_path
):
    manager = _automation_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "automations.yaml").write_text(
        "- id: my_automation\n  trigger: []\n  action: []\n"
    )
    hass.states.async_set("automation.my_automation", "off", {"id": "my_automation"})

    result = await GetAutomationTool(manager).async_call(
        hass,
        llm.ToolInput(
            tool_name="get_automation", tool_args={"automation_id": "my_automation"}
        ),
        _llm_context(admin_user.id),
    )

    assert result["currently_enabled"] is False


@pytest.mark.asyncio
async def test_get_automation_reports_unknown_when_never_reloaded(
    hass: HomeAssistant, admin_user, tmp_path
):
    """No automation.* entity exists yet - currently_enabled is None, with a note."""
    manager = _automation_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "automations.yaml").write_text(
        "- id: my_automation\n  trigger: []\n  action: []\n"
    )

    result = await GetAutomationTool(manager).async_call(
        hass,
        llm.ToolInput(
            tool_name="get_automation", tool_args={"automation_id": "my_automation"}
        ),
        _llm_context(admin_user.id),
    )

    assert result["currently_enabled"] is None
    assert result["runtime_state_note"] is not None


@pytest.mark.asyncio
async def test_get_automation_returns_error_for_missing_id(
    hass: HomeAssistant, admin_user, tmp_path
):
    manager = _automation_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "automations.yaml").write_text(
        "- id: some_other_id\n  trigger: []\n  action: []\n"
    )

    result = await GetAutomationTool(manager).async_call(
        hass,
        llm.ToolInput(
            tool_name="get_automation", tool_args={"automation_id": "missing"}
        ),
        _llm_context(admin_user.id),
    )

    assert "error" in result


# --- GetScriptTool / WriteScriptTool / ListScriptsTool (issue #42) ----------


def _script_manager(hass: HomeAssistant, tmp_path) -> ScriptManager:
    hass.config.config_dir = str(tmp_path)
    security_manager = SecurityManager(
        hass,
        {
            "read_paths": ["scripts.yaml", "packages/**/*.yaml"],
            "write_paths": ["scripts.yaml", "packages/**/*.yaml"],
            "denied_paths": [],
        },
    )
    file_manager = FileManager(hass, security_manager)
    return ScriptManager(hass, file_manager)


@pytest.mark.asyncio
async def test_get_script_returns_config(hass: HomeAssistant, admin_user, tmp_path):
    manager = _script_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "scripts.yaml").write_text(
        "my_script:\n  alias: Mine\n  sequence: []\n"
    )

    result = await GetScriptTool(manager).async_call(
        hass,
        llm.ToolInput(tool_name="get_script", tool_args={"script_id": "my_script"}),
        _llm_context(admin_user.id),
    )

    assert result["file_path"] == "scripts.yaml"
    assert result["config"]["alias"] == "Mine"
    assert result["misread_values"] == []


@pytest.mark.asyncio
async def test_get_script_reports_misread_values(
    hass: HomeAssistant, admin_user, tmp_path
):
    """Issue #97: an unquoted `delay: 1:30` is read by HA as 90 seconds,
    not 1h30m - silently, with no Repairs entry."""
    manager = _script_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "scripts.yaml").write_text("my_script:\n  sequence:\n  - delay: 1:30\n")

    result = await GetScriptTool(manager).async_call(
        hass,
        llm.ToolInput(tool_name="get_script", tool_args={"script_id": "my_script"}),
        _llm_context(admin_user.id),
    )

    assert result["misread_values"] == [
        {"path": "sequence[0].delay", "line": 3, "written": "1:30", "ha_reads_as": 90}
    ]


@pytest.mark.asyncio
async def test_get_script_returns_error_for_missing_id(
    hass: HomeAssistant, admin_user, tmp_path
):
    manager = _script_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "scripts.yaml").write_text("other_id:\n  sequence: []\n")

    result = await GetScriptTool(manager).async_call(
        hass,
        llm.ToolInput(tool_name="get_script", tool_args={"script_id": "missing"}),
        _llm_context(admin_user.id),
    )

    assert "error" in result


@pytest.mark.asyncio
async def test_list_scripts_returns_every_script(
    hass: HomeAssistant, admin_user, tmp_path
):
    manager = _script_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "scripts.yaml").write_text("a:\n  sequence: []\n")
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "emhas.yaml").write_text(
        "script:\n  b:\n    sequence: []\n"
    )

    result = await ListScriptsTool(manager).async_call(
        hass,
        llm.ToolInput(tool_name="list_scripts", tool_args={}),
        _llm_context(admin_user.id),
    )

    ids = {item["script_id"] for item in result["items"]}
    assert ids == {"a", "b"}


@pytest.mark.asyncio
async def test_write_script_tool_confirm_flow_writes_and_reloads(
    hass: HomeAssistant, admin_user, tmp_path
):
    manager = _script_manager(hass, tmp_path)
    _arm(hass)
    reload_mock = AsyncMock()
    hass.services.async_register("script", "reload", reload_mock)
    tool = WriteScriptTool(manager)
    args = {"script_id": "new_script", "config": {"alias": "New", "sequence": []}}

    proposal = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="write_script", tool_args=args),
        _llm_context(admin_user.id),
    )
    assert proposal["confirmation_required"] is True

    confirmed_args = {**args, "confirm_token": proposal["confirm_token"]}
    result = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="write_script", tool_args=confirmed_args),
        _llm_context(admin_user.id),
    )

    assert result["file_path"] == "scripts.yaml"
    assert result["setup_error"] is None
    reload_mock.assert_called_once()
    assert "new_script" in (tmp_path / "scripts.yaml").read_text()


def _reload_raising_setup_repair(hass: HomeAssistant, domain: str, item_id: str):
    """A stand-in `<domain>.reload` that does what HA's does for an item it
    can't set up: create the validation_failed repair while reloading."""
    from homeassistant.helpers import issue_registry as ir

    async def _reload(call):
        ir.async_create_issue(
            hass,
            domain,
            f"{domain}.{item_id}_validation_failed_triggers",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="validation_failed_triggers",
            translation_placeholders={
                "edit": f"/config/{domain}/edit/{item_id}",
                "entity_id": f"{domain}.{item_id}",
                "error": "invalid time_pattern value at 'minutes'. Got None",
                "name": item_id,
            },
        )

    hass.services.async_register(domain, "reload", _reload)


@pytest.mark.asyncio
async def test_delete_automation_tool_batch(hass: HomeAssistant, admin_user, tmp_path):
    """Issue #66: automation_ids deletes several under one confirmation."""
    manager = _write_automation_manager(hass, tmp_path)
    _arm(hass)
    hass.services.async_register("automation", "reload", AsyncMock())
    (tmp_path / "automations.yaml").write_text(
        "- id: a\n  actions: []\n- id: b\n  actions: []\n- id: keep\n  actions: []\n"
    )
    tool = DeleteAutomationTool(manager)

    result = await _confirm(hass, tool, admin_user, {"automation_ids": ["a", "b"]})
    both = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="delete_automation",
            tool_args={"automation_id": "keep", "automation_ids": ["keep"]},
        ),
        _llm_context(admin_user.id),
    )
    batch_dry_run = await tool._dry_run_mirror(
        hass,
        llm.ToolInput(
            tool_name="delete_automation", tool_args={"automation_ids": ["keep"]}
        ),
        _llm_context(admin_user.id),
    )

    assert result["deleted"] == ["a", "b"]
    assert result["files"] == [{"file_path": "automations.yaml", "is_package": False}]
    assert (tmp_path / "automations.yaml").read_text() == "- id: keep\n  actions: []\n"
    assert "exactly one of automation_id or automation_ids" in both["error"]
    assert batch_dry_run.mirrored is False


@pytest.mark.asyncio
async def test_delete_automation_tool_batch_mirrors_each_file(
    hass: HomeAssistant, admin_user, tmp_path
):
    manager = _write_automation_manager(hass, tmp_path)
    _arm(hass)
    hass.services.async_register("automation", "reload", AsyncMock())
    (tmp_path / "automations.yaml").write_text("- id: a\n  actions: []\n")
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "p.yaml").write_text(
        "automation:\n  - id: b\n    actions: []\n"
    )
    mirror_write = AsyncMock(
        return_value=MirrorResult(mirrored=True, commits=("before", "after"))
    )

    with (
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.is_mirror_enabled",
            return_value=True,
        ),
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.mirror_write", mirror_write
        ),
    ):
        result = await DeleteAutomationTool(manager)._write(
            hass,
            llm.ToolInput(
                tool_name="delete_automation", tool_args={"automation_ids": ["a", "b"]}
            ),
            _llm_context(admin_user.id),
        )

    assert [call.kwargs["path"] for call in mirror_write.await_args_list] == [
        "automations.yaml",
        "packages/p.yaml",
    ]
    assert len(result["mirror"]) == 2
    assert all(entry["mirrored"] for entry in result["mirror"])


@pytest.mark.asyncio
async def test_write_tools_return_setup_error_from_reload(
    hass: HomeAssistant, admin_user, tmp_path
):
    """Issue #102: HA's refusal to set up a just-written item used to be
    visible only on the Repairs page - the write reported plain success."""
    automation_manager = _write_automation_manager(hass, tmp_path)
    script_manager = _write_script_manager(hass, tmp_path)
    _arm(hass)
    _reload_raising_setup_repair(hass, "automation", "fault_detection")
    _reload_raising_setup_repair(hass, "script", "broken_script")

    automation = await _confirm(
        hass,
        WriteAutomationTool(automation_manager),
        admin_user,
        {
            "automation_id": "fault_detection",
            "config": {
                "triggers": [{"trigger": "time_pattern", "minutes": "2,17,32,47"}],
                "actions": [],
            },
        },
    )
    script = await _confirm(
        hass,
        WriteScriptTool(script_manager),
        admin_user,
        {"script_id": "broken_script", "config": {"sequence": []}},
    )

    assert "invalid time_pattern value" in automation["setup_error"]
    assert "invalid time_pattern value" in script["setup_error"]


# --- GetRestCommandTool / ListRestCommandsTool (issue #73) -------------------


def _rest_command_manager(hass: HomeAssistant, tmp_path) -> RestCommandManager:
    hass.config.config_dir = str(tmp_path)
    security_manager = SecurityManager(
        hass,
        {
            "read_paths": ["configuration.yaml", "packages/**/*.yaml"],
            "write_paths": [],
            "denied_paths": [],
        },
    )
    file_manager = FileManager(hass, security_manager)
    return RestCommandManager(hass, file_manager)


@pytest.mark.asyncio
async def test_get_rest_command_returns_config(
    hass: HomeAssistant, admin_user, tmp_path
):
    manager = _rest_command_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "configuration.yaml").write_text(
        "rest_command:\n  my_cmd:\n    url: http://example.com\n"
    )

    result = await GetRestCommandTool(manager).async_call(
        hass,
        llm.ToolInput(
            tool_name="get_rest_command", tool_args={"rest_command_id": "my_cmd"}
        ),
        _llm_context(admin_user.id),
    )

    assert result["file_path"] == "configuration.yaml"
    assert result["config"]["url"] == "http://example.com"


@pytest.mark.asyncio
async def test_get_rest_command_returns_error_for_missing_id(
    hass: HomeAssistant, admin_user, tmp_path
):
    manager = _rest_command_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "configuration.yaml").write_text(
        "rest_command:\n  other_id:\n    url: http://example.com\n"
    )

    result = await GetRestCommandTool(manager).async_call(
        hass,
        llm.ToolInput(
            tool_name="get_rest_command", tool_args={"rest_command_id": "missing"}
        ),
        _llm_context(admin_user.id),
    )

    assert "error" in result


@pytest.mark.asyncio
async def test_list_rest_commands_returns_every_command(
    hass: HomeAssistant, admin_user, tmp_path
):
    manager = _rest_command_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "configuration.yaml").write_text(
        "rest_command:\n  a:\n    url: http://a\n"
    )
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "emhass.yaml").write_text(
        "rest_command:\n  b:\n    url: http://b\n"
    )

    result = await ListRestCommandsTool(manager).async_call(
        hass,
        llm.ToolInput(tool_name="list_rest_commands", tool_args={}),
        _llm_context(admin_user.id),
    )

    ids = {item["rest_command_id"] for item in result["items"]}
    assert ids == {"a", "b"}


@pytest.mark.asyncio
async def test_rest_command_tools_read_shell_commands_via_domain(
    hass: HomeAssistant, admin_user, tmp_path
):
    """Issue #100: shell_command is read through the existing rest_command
    tools' optional `domain`, not a second pair of tools."""
    manager = _rest_command_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "configuration.yaml").write_text(
        "shell_command:\n  cache_prices: 'curl -s http://x'\n"
        "rest_command:\n  my_cmd:\n    url: http://example.com\n"
    )

    got = await GetRestCommandTool(manager).async_call(
        hass,
        llm.ToolInput(
            tool_name="get_rest_command",
            tool_args={"rest_command_id": "cache_prices", "domain": "shell_command"},
        ),
        _llm_context(admin_user.id),
    )
    listed = await ListRestCommandsTool(manager).async_call(
        hass,
        llm.ToolInput(
            tool_name="list_rest_commands", tool_args={"domain": "shell_command"}
        ),
        _llm_context(admin_user.id),
    )

    assert got["config"] == "curl -s http://x"
    assert [item["rest_command_id"] for item in listed["items"]] == ["cache_prices"]
    with pytest.raises(vol.Invalid):
        GetRestCommandTool(manager).parameters(
            {"rest_command_id": "x", "domain": "switch"}
        )


@pytest.mark.asyncio
async def test_rest_command_reads_with_secret_tag_are_json_safe(
    hass: HomeAssistant, admin_user, tmp_path
):
    """Issue #90: a `!secret` value loads as a ruamel TaggedScalar, which
    isn't JSON serializable - one such rest_command crashed both
    get_rest_command and the whole list_rest_commands listing. The tag is
    reported as its literal `!secret name` text, never resolved."""
    manager = _rest_command_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "configuration.yaml").write_text(
        "rest_command:\n"
        "  doorbird:\n"
        "    url: http://door/rfid\n"
        "    headers:\n"
        "      authorization: !secret doorbird_auth\n"
        "  plain:\n"
        "    url: http://plain\n"
    )

    got = await GetRestCommandTool(manager).async_call(
        hass,
        llm.ToolInput(
            tool_name="get_rest_command", tool_args={"rest_command_id": "doorbird"}
        ),
        _llm_context(admin_user.id),
    )
    listed = await ListRestCommandsTool(manager).async_call(
        hass,
        llm.ToolInput(tool_name="list_rest_commands", tool_args={}),
        _llm_context(admin_user.id),
    )

    json.dumps(got)
    json.dumps(listed)
    assert got["config"]["headers"]["authorization"] == "!secret doorbird_auth"
    assert {item["rest_command_id"] for item in listed["items"]} == {
        "doorbird",
        "plain",
    }


@pytest.mark.asyncio
async def test_automation_and_script_reads_with_secret_tag_are_json_safe(
    hass: HomeAssistant, admin_user, tmp_path
):
    """Same crash as #90, in get_automation/get_script/list_scripts - they
    return loaded ruamel nodes too."""
    automation_manager = _automation_manager(hass, tmp_path)
    script_manager = _script_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "automations.yaml").write_text(
        "- id: notify\n"
        "  trigger: []\n"
        "  action:\n"
        "  - action: notify.x\n"
        "    data:\n"
        "      target: !secret my_phone\n"
    )
    (tmp_path / "scripts.yaml").write_text(
        "call:\n  sequence:\n  - action: notify.x\n    data:\n"
        "      target: !secret my_phone\n"
    )

    automation = await GetAutomationTool(automation_manager).async_call(
        hass,
        llm.ToolInput(
            tool_name="get_automation", tool_args={"automation_id": "notify"}
        ),
        _llm_context(admin_user.id),
    )
    script = await GetScriptTool(script_manager).async_call(
        hass,
        llm.ToolInput(tool_name="get_script", tool_args={"script_id": "call"}),
        _llm_context(admin_user.id),
    )
    scripts = await ListScriptsTool(script_manager).async_call(
        hass,
        llm.ToolInput(tool_name="list_scripts", tool_args={}),
        _llm_context(admin_user.id),
    )

    for result in (automation, script, scripts):
        json.dumps(result)
    assert automation["config"]["action"][0]["data"]["target"] == "!secret my_phone"
    assert script["config"]["sequence"][0]["data"]["target"] == "!secret my_phone"


# --- WriteGatedTool / dry-run ------------------------------------------------


class _StubWriteTool(WriteGatedTool):
    """Minimal WriteGatedTool subclass so these tests exercise only the
    confirmation/dry-run gating itself, decoupled from any specific
    manager's setup."""

    name = "stub_write"
    description = "stub"
    parameters = vol.Schema({vol.Optional("confirm_token"): str})

    def __init__(self) -> None:
        self.write_called = False

    async def _write(self, hass, tool_input, llm_context):
        self.write_called = True
        return {"wrote": True}


async def _confirm(hass, tool, admin_user, args: dict) -> dict:
    """Drive a WriteGatedTool through its propose call, then confirm with the returned token."""
    proposal = await tool.async_call(
        hass,
        llm.ToolInput(tool_name=tool.name, tool_args=args),
        _llm_context(admin_user.id),
    )
    assert proposal["confirmation_required"] is True
    confirmed_args = {**args, "confirm_token": proposal["confirm_token"]}
    return await tool.async_call(
        hass,
        llm.ToolInput(tool_name=tool.name, tool_args=confirmed_args),
        _llm_context(admin_user.id),
    )


@pytest.mark.asyncio
async def test_write_gated_tool_first_call_requires_confirmation(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    """The first call to any write tool never writes - it proposes instead."""
    _arm(hass)
    tool = _StubWriteTool()

    result = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="stub_write", tool_args={"foo": "bar"}),
        _llm_context(admin_user.id),
    )

    assert tool.write_called is False
    assert result["confirmation_required"] is True
    assert result["action"] == "stub_write"
    assert result["would_apply"] == {"foo": "bar"}
    assert isinstance(result["confirm_token"], str) and result["confirm_token"]


@pytest.mark.asyncio
async def test_write_gated_tool_performs_write_when_dry_run_disabled(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    _arm(hass)
    tool = _StubWriteTool()

    result = await _confirm(hass, tool, admin_user, {"foo": "bar"})

    assert tool.write_called is True
    assert _without_arm(result) == {"wrote": True}


@pytest.mark.asyncio
async def test_write_gated_tool_blocks_write_when_dry_run_enabled(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    hass.config_entries.async_update_entry(
        setup_integration_with_entry, options={OPT_DRY_RUN: True}
    )
    _arm(hass)
    tool = _StubWriteTool()

    result = await _confirm(hass, tool, admin_user, {"foo": "bar"})

    assert tool.write_called is False
    assert result["dry_run"] is True
    assert result["action"] == "stub_write"
    assert result["would_apply"] == {"foo": "bar"}


@pytest.mark.asyncio
async def test_write_gated_tool_dry_run_mirror_default_hook_returns_none(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    """Most write tools (helpers, derived sensors, dashboards) never
    override _dry_run_mirror - dry-run + mirroring enabled together must
    still come back with no 'mirror' key for them, via the base class's
    default None, rather than only ever being exercised by the tools that
    do override it."""
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_DRY_RUN: True,
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    _arm(hass)
    tool = _StubWriteTool()

    result = await _confirm(hass, tool, admin_user, {"foo": "bar"})

    assert result["dry_run"] is True
    assert "mirror" not in result


@pytest.mark.asyncio
async def test_write_gated_tool_dry_run_still_requires_armed_and_admin(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    """Dry-run mode previews a write, it doesn't bypass the access gate."""
    hass.config_entries.async_update_entry(
        setup_integration_with_entry, options={OPT_DRY_RUN: True}
    )
    tool = _StubWriteTool()

    with pytest.raises(NotArmedError):
        await tool.async_call(
            hass,
            llm.ToolInput(tool_name="stub_write", tool_args={}),
            _llm_context(admin_user.id),
        )
    assert tool.write_called is False


@pytest.mark.asyncio
async def test_write_gated_tool_rejects_unknown_token(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    """A garbage/unrecognized token is treated as a fresh propose, not an error."""
    _arm(hass)
    tool = _StubWriteTool()

    result = await tool.async_call(
        hass,
        llm.ToolInput(
            tool_name="stub_write", tool_args={"foo": "bar", "confirm_token": "nope"}
        ),
        _llm_context(admin_user.id),
    )

    assert tool.write_called is False
    assert result["confirmation_required"] is True
    assert result["confirm_token"] != "nope"


@pytest.mark.asyncio
async def test_write_gated_tool_token_does_not_authorize_different_args(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    """A token issued for one set of arguments doesn't confirm a call with different ones."""
    _arm(hass)
    tool = _StubWriteTool()

    proposal = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="stub_write", tool_args={"foo": "bar"}),
        _llm_context(admin_user.id),
    )

    drifted = await tool.async_call(
        hass,
        llm.ToolInput(
            tool_name="stub_write",
            tool_args={"foo": "different", "confirm_token": proposal["confirm_token"]},
        ),
        _llm_context(admin_user.id),
    )

    assert tool.write_called is False
    assert drifted["confirmation_required"] is True


@pytest.mark.asyncio
async def test_write_gated_tool_token_is_single_use(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    """Replaying an already-confirmed token doesn't authorize a second write."""
    _arm(hass)
    tool = _StubWriteTool()
    args = {"foo": "bar"}

    proposal = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="stub_write", tool_args=args),
        _llm_context(admin_user.id),
    )
    confirmed_args = {**args, "confirm_token": proposal["confirm_token"]}

    first = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="stub_write", tool_args=confirmed_args),
        _llm_context(admin_user.id),
    )
    assert _without_arm(first) == {"wrote": True}
    assert tool.write_called is True

    tool.write_called = False
    replay = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="stub_write", tool_args=confirmed_args),
        _llm_context(admin_user.id),
    )
    assert replay["confirmation_required"] is True
    assert tool.write_called is False


# --- GetEntityHistoryTool / GetLogbookTool -----------------------------------
#
# _run() is exercised directly against a mocked history_manager rather than a
# real recorder here - test_history_manager.py already covers the real
# recorder/logbook query logic end to end. What's specific to these two
# Tool classes and not covered there is the thin adapter layer: parsing
# start_time/end_time, applying argument defaults, and turning
# RecorderNotAvailableError/ValueError into a _tool_error() payload instead
# of letting them escape.


@pytest.mark.asyncio
async def test_get_entity_history_tool_calls_manager(hass: HomeAssistant):
    tool = GetEntityHistoryTool()
    mock_get_history = AsyncMock(
        return_value={"entities": {"sensor.x": {"states": []}}}
    )
    with patch(
        "custom_components.ha_dev_tools.llm_api.history_manager.get_entity_history",
        mock_get_history,
    ):
        result = await tool._run(
            hass,
            llm.ToolInput(
                tool_name="get_entity_history",
                tool_args={
                    "entity_ids": ["sensor.x"],
                    "start_time": "2026-08-10T00:00:00+00:00",
                    "end_time": "2026-08-11T00:00:00+00:00",
                },
            ),
            _llm_context(),
        )

    assert result == {"entities": {"sensor.x": {"states": []}}}
    mock_get_history.assert_called_once()


@pytest.mark.asyncio
async def test_get_entity_history_tool_rejects_invalid_start_time(hass: HomeAssistant):
    tool = GetEntityHistoryTool()

    result = await tool._run(
        hass,
        llm.ToolInput(
            tool_name="get_entity_history",
            tool_args={"entity_ids": ["sensor.x"], "start_time": "not-a-date"},
        ),
        _llm_context(),
    )

    assert result["error_type"] == "ValueError"


@pytest.mark.asyncio
async def test_get_entity_history_tool_rejects_missing_start_time(
    hass: HomeAssistant,
):
    """A caller omitting start_time gets a clean ValueError, not a raw
    KeyError - see issue #45: the schema already marks it vol.Required, but
    that schema is never actually invoked to validate tool_args."""
    tool = GetEntityHistoryTool()

    result = await tool._run(
        hass,
        llm.ToolInput(
            tool_name="get_entity_history",
            tool_args={"entity_ids": ["sensor.x"]},
        ),
        _llm_context(),
    )

    assert result == {
        "error": "'start_time' is required",
        "error_type": "ValueError",
    }


@pytest.mark.asyncio
async def test_get_entity_history_tool_rejects_missing_entity_ids(
    hass: HomeAssistant,
):
    tool = GetEntityHistoryTool()

    result = await tool._run(
        hass,
        llm.ToolInput(
            tool_name="get_entity_history",
            tool_args={"start_time": "2026-08-10T00:00:00+00:00"},
        ),
        _llm_context(),
    )

    assert result == {
        "error": "'entity_ids' is required",
        "error_type": "ValueError",
    }


@pytest.mark.asyncio
async def test_get_entity_history_tool_surfaces_recorder_not_available(
    hass: HomeAssistant,
):
    tool = GetEntityHistoryTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.history_manager.get_entity_history",
        AsyncMock(side_effect=RecorderNotAvailableError()),
    ):
        result = await tool._run(
            hass,
            llm.ToolInput(
                tool_name="get_entity_history",
                tool_args={
                    "entity_ids": ["sensor.x"],
                    "start_time": "2026-08-10T00:00:00+00:00",
                },
            ),
            _llm_context(),
        )

    assert result["error_type"] == "RecorderNotAvailableError"


@pytest.mark.asyncio
async def test_get_logbook_tool_rejects_missing_start_time(hass: HomeAssistant):
    """Same fix, same crash class as get_entity_history's start_time - see
    issue #45."""
    tool = GetLogbookTool()

    result = await tool._run(
        hass,
        llm.ToolInput(tool_name="get_logbook", tool_args={}),
        _llm_context(),
    )

    assert result == {
        "error": "'start_time' is required",
        "error_type": "ValueError",
    }


@pytest.mark.asyncio
async def test_get_logbook_tool_calls_manager(hass: HomeAssistant):
    tool = GetLogbookTool()
    mock_get_logbook = AsyncMock(
        return_value={"entries": [], "count": 0, "truncated": False}
    )
    with patch(
        "custom_components.ha_dev_tools.llm_api.history_manager.get_logbook_entries",
        mock_get_logbook,
    ):
        result = await tool._run(
            hass,
            llm.ToolInput(
                tool_name="get_logbook",
                tool_args={"start_time": "2026-08-10T00:00:00+00:00"},
            ),
            _llm_context(),
        )

    assert result == {"entries": [], "count": 0, "truncated": False}
    mock_get_logbook.assert_called_once()


@pytest.mark.asyncio
async def test_get_logbook_tool_rejects_invalid_end_time(hass: HomeAssistant):
    tool = GetLogbookTool()

    result = await tool._run(
        hass,
        llm.ToolInput(
            tool_name="get_logbook",
            tool_args={
                "start_time": "2026-08-10T00:00:00+00:00",
                "end_time": "not-a-date",
            },
        ),
        _llm_context(),
    )

    assert result["error_type"] == "ValueError"


# --- Derived-sensor tools -----------------------------------------------
#
# The real config/options-flow driving is already covered end to end by
# test_derived_sensor_manager.py (and test_derived_sensor_manager_recorder.py
# for the one domain that needs a real recorder) against real HA components.
# What's specific to these Tool classes and not covered there is the thin
# adapter layer: argument plumbing, and turning FlowStepRequiredError into
# the needs_input payload (not a _tool_error) versus the other exceptions
# into a _tool_error.


@pytest.mark.asyncio
async def test_list_derived_sensors_tool_calls_manager(hass: HomeAssistant):
    tool = ListDerivedSensorsTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.list_derived_sensors",
        return_value=[{"entry_id": "abc", "domain": "min_max"}],
    ) as mock_list:
        result = await tool._run(
            hass,
            llm.ToolInput(
                tool_name="list_derived_sensors", tool_args={"domain": "min_max"}
            ),
            _llm_context(),
        )

    assert result == {"items": [{"entry_id": "abc", "domain": "min_max"}]}
    mock_list.assert_called_once_with(hass, "min_max")


@pytest.mark.asyncio
async def test_list_derived_sensors_tool_rejects_invalid_domain(hass: HomeAssistant):
    tool = ListDerivedSensorsTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.list_derived_sensors",
        side_effect=InvalidDerivedSensorDomainError("bad domain"),
    ):
        result = await tool._run(
            hass,
            llm.ToolInput(
                tool_name="list_derived_sensors", tool_args={"domain": "min_max"}
            ),
            _llm_context(),
        )

    assert result["error_type"] == "InvalidDerivedSensorDomainError"


@pytest.mark.asyncio
async def test_get_derived_sensor_tool_calls_manager(hass: HomeAssistant):
    tool = GetDerivedSensorTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.get_derived_sensor",
        return_value={"entry_id": "abc"},
    ) as mock_get:
        result = await tool._run(
            hass,
            llm.ToolInput(
                tool_name="get_derived_sensor", tool_args={"entry_id": "abc"}
            ),
            _llm_context(),
        )

    assert result == {"entry_id": "abc"}
    mock_get.assert_called_once_with(hass, "abc")


@pytest.mark.asyncio
async def test_get_derived_sensor_tool_surfaces_not_found(hass: HomeAssistant):
    tool = GetDerivedSensorTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.get_derived_sensor",
        side_effect=DerivedSensorNotFoundError("nope"),
    ):
        result = await tool._run(
            hass,
            llm.ToolInput(
                tool_name="get_derived_sensor", tool_args={"entry_id": "abc"}
            ),
            _llm_context(),
        )

    assert result["error_type"] == "DerivedSensorNotFoundError"


@pytest.mark.asyncio
async def test_create_derived_sensor_tool_calls_manager(hass: HomeAssistant):
    tool = CreateDerivedSensorTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.create_derived_sensor",
        AsyncMock(return_value={"entry_id": "abc", "domain": "min_max"}),
    ) as mock_create:
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="create_derived_sensor",
                tool_args={"domain": "min_max", "steps": {"user": {"name": "x"}}},
            ),
            _llm_context(),
        )

    assert result == {"entry_id": "abc", "domain": "min_max"}
    mock_create.assert_called_once_with(hass, "min_max", {"user": {"name": "x"}})


@pytest.mark.asyncio
async def test_create_derived_sensor_tool_needs_input_payload(hass: HomeAssistant):
    """A FlowStepRequiredError becomes a structured needs_input payload, not a _tool_error."""
    tool = CreateDerivedSensorTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.create_derived_sensor",
        AsyncMock(
            side_effect=FlowStepRequiredError(
                "user", [{"name": "entity_id", "type": "string"}], None
            )
        ),
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="create_derived_sensor", tool_args={"domain": "min_max"}
            ),
            _llm_context(),
        )

    assert result["needs_input"] is True
    assert result["step_id"] == "user"
    assert result["schema"] == [{"name": "entity_id", "type": "string"}]
    assert result["errors"] == {}
    assert "error" not in result


@pytest.mark.asyncio
async def test_create_derived_sensor_tool_surfaces_invalid_domain(hass: HomeAssistant):
    """The other exception branch alongside FlowStepRequiredError above -
    never exercised anywhere else."""
    tool = CreateDerivedSensorTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.create_derived_sensor",
        AsyncMock(side_effect=InvalidDerivedSensorDomainError("bad domain")),
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="create_derived_sensor", tool_args={"domain": "min_max"}
            ),
            _llm_context(),
        )

    assert result["error_type"] == "InvalidDerivedSensorDomainError"


@pytest.mark.asyncio
async def test_update_derived_sensor_tool_calls_manager(hass: HomeAssistant):
    tool = UpdateDerivedSensorTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.update_derived_sensor",
        AsyncMock(return_value={"entry_id": "abc"}),
    ) as mock_update:
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="update_derived_sensor",
                tool_args={"entry_id": "abc", "steps": {"init": {"type": "min"}}},
            ),
            _llm_context(),
        )

    assert result == {"entry_id": "abc"}
    mock_update.assert_called_once_with(hass, "abc", {"init": {"type": "min"}}, None)


@pytest.mark.asyncio
async def test_update_derived_sensor_propose_shows_current_and_changes(
    hass: HomeAssistant,
):
    """Issue #86: the propose step used to echo only the caller's own
    arguments, so the user confirming never saw what the entry holds or
    what actually changes. Unknown entry ids fall back to the plain
    preview - the real write reports the not-found error."""
    tool = UpdateDerivedSensorTool()
    preview = {
        "current_options": {"state": "{{ 1 }}", "name": "X"},
        "would_change": [{"field": "state", "from": "{{ 1 }}", "to": "{{ 2 }}"}],
    }
    args = {"entry_id": "abc", "options": {"state": "{{ 2 }}"}}
    with patch(
        "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.preview_update",
        return_value=preview,
    ) as mock_preview:
        proposal = await tool._run(
            hass,
            llm.ToolInput(tool_name="update_derived_sensor", tool_args=args),
            _llm_context(),
        )
    missing = await tool._run(
        hass,
        llm.ToolInput(
            tool_name="update_derived_sensor",
            tool_args={"entry_id": "nope", "options": {"state": "x"}},
        ),
        _llm_context(),
    )

    mock_preview.assert_called_once_with(hass, "abc", None, {"state": "{{ 2 }}"})
    assert proposal["confirmation_required"] is True
    assert proposal["would_apply"] == args
    assert proposal["current_options"] == preview["current_options"]
    assert proposal["would_change"] == preview["would_change"]
    assert missing["confirmation_required"] is True
    assert "would_change" not in missing


@pytest.mark.asyncio
async def test_update_derived_sensor_tool_passes_options_patch(hass: HomeAssistant):
    """The step-id-free `options` patch (issue #82) reaches the manager."""
    tool = UpdateDerivedSensorTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.update_derived_sensor",
        AsyncMock(return_value={"entry_id": "abc"}),
    ) as mock_update:
        await tool._write(
            hass,
            llm.ToolInput(
                tool_name="update_derived_sensor",
                tool_args={"entry_id": "abc", "options": {"type": "min"}},
            ),
            _llm_context(),
        )

    mock_update.assert_called_once_with(hass, "abc", {}, {"type": "min"})


@pytest.mark.asyncio
async def test_update_derived_sensor_tool_surfaces_not_found(hass: HomeAssistant):
    tool = UpdateDerivedSensorTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.update_derived_sensor",
        AsyncMock(side_effect=DerivedSensorNotFoundError("nope")),
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="update_derived_sensor", tool_args={"entry_id": "abc"}
            ),
            _llm_context(),
        )

    assert result["error_type"] == "DerivedSensorNotFoundError"


@pytest.mark.asyncio
async def test_update_derived_sensor_tool_needs_input_payload(hass: HomeAssistant):
    """update_derived_sensor's own FlowStepRequiredError path - only
    create_derived_sensor's equivalent (above) was covered before."""
    tool = UpdateDerivedSensorTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.update_derived_sensor",
        AsyncMock(
            side_effect=FlowStepRequiredError(
                "init", [{"name": "max", "type": "float"}], None
            )
        ),
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="update_derived_sensor", tool_args={"entry_id": "abc"}
            ),
            _llm_context(),
        )

    assert result["needs_input"] is True
    assert result["step_id"] == "init"


@pytest.mark.asyncio
async def test_delete_derived_sensor_tool_calls_manager(hass: HomeAssistant):
    tool = DeleteDerivedSensorTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.delete_derived_sensor",
        AsyncMock(return_value={"deleted": True, "entry_id": "abc"}),
    ) as mock_delete:
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="delete_derived_sensor", tool_args={"entry_id": "abc"}
            ),
            _llm_context(),
        )

    assert result == {"deleted": True, "entry_id": "abc"}
    mock_delete.assert_called_once_with(hass, "abc")


@pytest.mark.asyncio
async def test_delete_derived_sensor_tool_surfaces_not_found(hass: HomeAssistant):
    tool = DeleteDerivedSensorTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.delete_derived_sensor",
        AsyncMock(side_effect=DerivedSensorNotFoundError("nope")),
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="delete_derived_sensor", tool_args={"entry_id": "abc"}
            ),
            _llm_context(),
        )

    assert result["error_type"] == "DerivedSensorNotFoundError"


@pytest.mark.asyncio
async def test_reload_derived_sensor_tool_calls_manager(hass: HomeAssistant):
    tool = ReloadDerivedSensorTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.reload_derived_sensor",
        AsyncMock(return_value={"reloaded": True, "entry_id": "abc"}),
    ) as mock_reload:
        result = await tool._run(
            hass,
            llm.ToolInput(
                tool_name="reload_derived_sensor", tool_args={"entry_id": "abc"}
            ),
            _llm_context(),
        )

    assert result == {"reloaded": True, "entry_id": "abc"}
    mock_reload.assert_called_once_with(hass, "abc")


@pytest.mark.asyncio
async def test_reload_derived_sensor_tool_surfaces_not_found(hass: HomeAssistant):
    tool = ReloadDerivedSensorTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.reload_derived_sensor",
        AsyncMock(side_effect=DerivedSensorNotFoundError("nope")),
    ):
        result = await tool._run(
            hass,
            llm.ToolInput(
                tool_name="reload_derived_sensor", tool_args={"entry_id": "abc"}
            ),
            _llm_context(),
        )

    assert result["error_type"] == "DerivedSensorNotFoundError"


# --- Template entity write tools (mirroring wiring) --------------------------
#
# These exercise the real llm_api.py <-> template_yaml_manager.py integration
# (a real TemplateYamlManager against a temp config dir, not a mocked one) -
# specifically the _mirror_file_write() wiring _write() added, both with
# mirroring off (the default/common case) and on (to cover the actual push
# call site, not just mirror.mirror_write() in isolation - see test_mirror.py
# for that).


@pytest.fixture
def _template_security_manager(hass: HomeAssistant):
    return SecurityManager(
        hass,
        {
            "read_paths": ["configuration.yaml", "packages/**/*.yaml"],
            "write_paths": ["configuration.yaml", "packages/**/*.yaml"],
            "denied_paths": [],
        },
    )


@pytest.fixture
def _template_file_manager(hass: HomeAssistant, _template_security_manager, tmp_path):
    hass.config.config_dir = str(tmp_path)
    return FileManager(hass, _template_security_manager)


@pytest.fixture
def template_yaml_manager(hass: HomeAssistant, _template_file_manager):
    return TemplateYamlManager(hass, _template_file_manager)


@pytest.fixture(autouse=True)
def _mock_template_reload_service(hass: HomeAssistant):
    hass.services.async_register("template", "reload", AsyncMock())


def _write_package(tmp_path, rel_path: str, content: str) -> None:
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)


# --- ListTemplateEntitiesTool / GetTemplateEntityTool (reads) ----------------
#
# Neither tool was ever imported by any existing test - both are thin
# read-side wrappers around the same real TemplateYamlManager the write
# tools above use.


@pytest.mark.asyncio
async def test_list_template_entities_tool_calls_manager(
    hass: HomeAssistant, template_yaml_manager, tmp_path
):
    _write_package(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        "  - sensor:\n"
        "      - name: One\n"
        "        unique_id: one\n"
        '        state: "{{ 1 }}"\n',
    )

    result = await ListTemplateEntitiesTool(template_yaml_manager)._run(
        hass,
        llm.ToolInput(tool_name="list_template_entities", tool_args={}),
        _llm_context(),
    )

    assert [item["unique_id"] for item in result["items"]] == ["one"]


@pytest.mark.asyncio
async def test_get_template_entity_tool_returns_config(
    hass: HomeAssistant, template_yaml_manager, tmp_path
):
    _write_package(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        "  - sensor:\n"
        "      - name: Target\n"
        "        unique_id: target\n"
        '        state: "{{ 1 }}"\n',
    )

    result = await GetTemplateEntityTool(template_yaml_manager)._run(
        hass,
        llm.ToolInput(
            tool_name="get_template_entity", tool_args={"unique_id": "target"}
        ),
        _llm_context(),
    )

    assert result["file_path"] == "packages/emhas.yaml"
    assert result["config"]["name"] == "Target"
    assert result["misread_values"] == []


@pytest.mark.asyncio
async def test_get_template_entity_tool_reports_misread_values(
    hass: HomeAssistant, template_yaml_manager, tmp_path
):
    """Issue #97. The template read path converts to plain dicts for
    !secret/!include tags, so there's no line number - the path still
    locates the value."""
    _write_package(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        "  - binary_sensor:\n"
        "      - name: Target\n"
        "        unique_id: target\n"
        "        delay_on: 1:30\n"
        '        state: "{{ true }}"\n',
    )

    result = await GetTemplateEntityTool(template_yaml_manager)._run(
        hass,
        llm.ToolInput(
            tool_name="get_template_entity", tool_args={"unique_id": "target"}
        ),
        _llm_context(),
    )

    assert result["misread_values"] == [
        {"path": "delay_on", "line": None, "written": "1:30", "ha_reads_as": 90}
    ]


@pytest.mark.asyncio
async def test_get_template_entity_tool_surfaces_not_found(
    hass: HomeAssistant, template_yaml_manager, tmp_path
):
    _write_package(tmp_path, "packages/emhas.yaml", "template: []\n")

    result = await GetTemplateEntityTool(template_yaml_manager)._run(
        hass,
        llm.ToolInput(
            tool_name="get_template_entity", tool_args={"unique_id": "missing"}
        ),
        _llm_context(),
    )

    assert result["error_type"] == "TemplateEntityNotFoundError"


@pytest.mark.asyncio
async def test_create_template_entity_tool_writes_and_reports_location(
    hass: HomeAssistant, setup_integration_with_entry, template_yaml_manager, tmp_path
):
    _write_package(tmp_path, "packages/emhas.yaml", "template: []\n")
    tool = CreateTemplateEntityTool(template_yaml_manager)

    result = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="create_template_entity",
            tool_args={
                "platform": "sensor",
                "config": {"name": "New", "unique_id": "new_one", "state": "{{ 1 }}"},
                "package": "emhas.yaml",
            },
        ),
        _llm_context(),
    )

    assert result["file_path"] == "packages/emhas.yaml"
    assert result["platform"] == "sensor"
    assert result["reloaded"] is True
    assert "mirror" not in result  # mirroring not configured on this entry


@pytest.mark.asyncio
async def test_update_template_entity_tool_writes_and_reports_location(
    hass: HomeAssistant, setup_integration_with_entry, template_yaml_manager, tmp_path
):
    _write_package(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        "  - sensor:\n"
        "      - name: Old\n"
        "        unique_id: target\n"
        '        state: "{{ 1 }}"\n',
    )
    tool = UpdateTemplateEntityTool(template_yaml_manager)

    result = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="update_template_entity",
            tool_args={
                "unique_id": "target",
                "config": {"name": "New", "state": "{{ 2 }}"},
            },
        ),
        _llm_context(),
    )

    assert result["file_path"] == "packages/emhas.yaml"
    assert result["reloaded"] is True
    assert "mirror" not in result


@pytest.mark.asyncio
async def test_delete_template_entity_tool_writes_and_reports_location(
    hass: HomeAssistant, setup_integration_with_entry, template_yaml_manager, tmp_path
):
    _write_package(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        "  - sensor:\n"
        "      - name: Gone\n"
        "        unique_id: gone\n"
        '        state: "{{ 1 }}"\n',
    )
    tool = DeleteTemplateEntityTool(template_yaml_manager)

    result = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="delete_template_entity", tool_args={"unique_id": "gone"}
        ),
        _llm_context(),
    )

    assert result["file_path"] == "packages/emhas.yaml"
    assert result["reloaded"] is True
    assert "mirror" not in result


# --- Template entity write tools: exception branches -------------------------
#
# The happy paths above never exercise these tools' except clauses (or
# their _dry_run_mirror equivalents) - a missing 'unique_id'/nonexistent
# unique_id triggers the same real errors template_yaml_manager.py's own
# tests already verify, no mocking needed.


@pytest.mark.asyncio
async def test_create_template_entity_tool_write_rejects_missing_unique_id(
    hass: HomeAssistant, template_yaml_manager, tmp_path
):
    _write_package(tmp_path, "packages/emhas.yaml", "template: []\n")
    tool = CreateTemplateEntityTool(template_yaml_manager)

    result = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="create_template_entity",
            tool_args={
                "platform": "sensor",
                "config": {"name": "New", "state": "{{ 1 }}"},
                "package": "emhas.yaml",
            },
        ),
        _llm_context(),
    )

    assert result["error_type"] == "ValueError"


@pytest.mark.asyncio
async def test_create_template_entity_tool_dry_run_mirror_skips_on_error(
    hass: HomeAssistant, template_yaml_manager, tmp_path
):
    _write_package(tmp_path, "packages/emhas.yaml", "template: []\n")
    tool = CreateTemplateEntityTool(template_yaml_manager)

    result = await tool._dry_run_mirror(
        hass,
        llm.ToolInput(
            tool_name="create_template_entity",
            tool_args={
                "platform": "sensor",
                "config": {"name": "New", "state": "{{ 1 }}"},
                "package": "emhas.yaml",
            },
        ),
        _llm_context(),
    )

    assert result.mirrored is False


@pytest.mark.asyncio
async def test_update_template_entity_tool_write_surfaces_not_found(
    hass: HomeAssistant, template_yaml_manager, tmp_path
):
    _write_package(tmp_path, "packages/emhas.yaml", "template: []\n")
    tool = UpdateTemplateEntityTool(template_yaml_manager)

    result = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="update_template_entity",
            tool_args={"unique_id": "missing", "config": {"name": "New"}},
        ),
        _llm_context(),
    )

    assert result["error_type"] == "TemplateEntityNotFoundError"


@pytest.mark.asyncio
async def test_update_template_entity_tool_dry_run_mirror_skips_when_not_found(
    hass: HomeAssistant, template_yaml_manager, tmp_path
):
    _write_package(tmp_path, "packages/emhas.yaml", "template: []\n")
    tool = UpdateTemplateEntityTool(template_yaml_manager)

    result = await tool._dry_run_mirror(
        hass,
        llm.ToolInput(
            tool_name="update_template_entity",
            tool_args={"unique_id": "missing", "config": {"name": "New"}},
        ),
        _llm_context(),
    )

    assert result.mirrored is False


@pytest.mark.asyncio
async def test_delete_template_entity_tool_write_surfaces_not_found(
    hass: HomeAssistant, template_yaml_manager, tmp_path
):
    _write_package(tmp_path, "packages/emhas.yaml", "template: []\n")
    tool = DeleteTemplateEntityTool(template_yaml_manager)

    result = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="delete_template_entity", tool_args={"unique_id": "missing"}
        ),
        _llm_context(),
    )

    assert result["error_type"] == "TemplateEntityNotFoundError"


@pytest.mark.asyncio
async def test_delete_template_entity_tool_dry_run_mirror_skips_when_not_found(
    hass: HomeAssistant, template_yaml_manager, tmp_path
):
    _write_package(tmp_path, "packages/emhas.yaml", "template: []\n")
    tool = DeleteTemplateEntityTool(template_yaml_manager)

    result = await tool._dry_run_mirror(
        hass,
        llm.ToolInput(
            tool_name="delete_template_entity", tool_args={"unique_id": "missing"}
        ),
        _llm_context(),
    )

    assert result.mirrored is False


class _FakeMirrorResponse:
    def __init__(self, status: int, payload: object = None):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _FakeMirrorRequestContext:
    def __init__(self, response: _FakeMirrorResponse):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc_info):
        return False


class _FakeMirrorSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def _next(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        return _FakeMirrorRequestContext(self._responses.pop(0))

    def get(self, url, **kwargs):
        return self._next("GET", url, kwargs)

    def put(self, url, **kwargs):
        return self._next("PUT", url, kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, kwargs)

    def patch(self, url, **kwargs):
        return self._next("PATCH", url, kwargs)


@pytest.mark.asyncio
async def test_update_template_entity_tool_mirrors_when_enabled(
    hass: HomeAssistant, setup_integration_with_entry, template_yaml_manager, tmp_path
):
    """Same write as above, but with mirroring configured - covers _mirror_file_write's
    actual push call site (mirror.mirror_write's own logic is covered by test_mirror.py).
    """
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    _write_package(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        "  - sensor:\n"
        "      - name: Old\n"
        "        unique_id: target\n"
        '        state: "{{ 1 }}"\n',
    )
    tool = UpdateTemplateEntityTool(template_yaml_manager)
    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # GET current - not mirrored yet
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT before
            _FakeMirrorResponse(200, {"content": {"sha": "sha-2"}}),  # PUT after
        ]
    )

    with patch(
        "custom_components.ha_dev_tools.mirror.async_get_clientsession",
        return_value=fake_session,
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="update_template_entity",
                tool_args={
                    "unique_id": "target",
                    "config": {"name": "New", "state": "{{ 2 }}"},
                },
            ),
            _llm_context(),
        )

    assert result["mirror"]["mirrored"] is True
    assert result["mirror"]["commits"] == ["before", "after"]


@pytest.mark.asyncio
async def test_create_template_entity_tool_mirrors_when_enabled(
    hass: HomeAssistant, setup_integration_with_entry, template_yaml_manager, tmp_path
):
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    _write_package(tmp_path, "packages/emhas.yaml", "template: []\n")
    tool = CreateTemplateEntityTool(template_yaml_manager)
    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # GET current - not mirrored yet
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT before
            _FakeMirrorResponse(200, {"content": {"sha": "sha-2"}}),  # PUT after
        ]
    )

    with patch(
        "custom_components.ha_dev_tools.mirror.async_get_clientsession",
        return_value=fake_session,
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="create_template_entity",
                tool_args={
                    "platform": "sensor",
                    "config": {
                        "name": "New",
                        "unique_id": "new_one",
                        "state": "{{ 1 }}",
                    },
                    "package": "emhas.yaml",
                },
            ),
            _llm_context(),
        )

    assert result["mirror"]["mirrored"] is True
    assert result["mirror"]["commits"] == ["before", "after"]


@pytest.mark.asyncio
async def test_delete_template_entity_tool_mirrors_when_enabled(
    hass: HomeAssistant, setup_integration_with_entry, template_yaml_manager, tmp_path
):
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    _write_package(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        "  - sensor:\n"
        "      - name: Gone\n"
        "        unique_id: gone\n"
        '        state: "{{ 1 }}"\n',
    )
    tool = DeleteTemplateEntityTool(template_yaml_manager)
    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # GET current - not mirrored yet
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT before
            _FakeMirrorResponse(200, {"content": {"sha": "sha-2"}}),  # PUT after
        ]
    )

    with patch(
        "custom_components.ha_dev_tools.mirror.async_get_clientsession",
        return_value=fake_session,
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="delete_template_entity", tool_args={"unique_id": "gone"}
            ),
            _llm_context(),
        )

    assert result["mirror"]["mirrored"] is True
    assert result["mirror"]["commits"] == ["before", "after"]


# --- ListDashboardsTool (issue #77) -------------------------------------------
#
# Never imported by any existing test. dashboard_manager.py's own tests
# (test_dashboard_manager.py) cover list_dashboards' real filtering/mapping
# logic against real HA lovelace - this only proves the tool wires args/
# errors through correctly, same pattern as GetDashboardTool's tests below.


@pytest.mark.asyncio
async def test_list_dashboards_tool_calls_manager(hass: HomeAssistant, admin_user):
    with patch(
        "custom_components.ha_dev_tools.llm_api.dashboard_manager.list_dashboards",
        AsyncMock(return_value=[{"url_path": "lovelace", "title": "Overview"}]),
    ) as mock_list:
        result = await ListDashboardsTool()._run(
            hass,
            llm.ToolInput(tool_name="list_dashboards", tool_args={}),
            _llm_context(admin_user.id),
        )

    assert result == {"dashboards": [{"url_path": "lovelace", "title": "Overview"}]}
    mock_list.assert_called_once_with(hass, admin_user)


@pytest.mark.asyncio
async def test_list_dashboards_tool_surfaces_unresolved_user(hass: HomeAssistant):
    """No user in context - resolve_user must refuse before dashboard_manager
    is ever called."""
    result = await ListDashboardsTool()._run(
        hass, llm.ToolInput(tool_name="list_dashboards", tool_args={}), _llm_context()
    )

    assert result["error_type"] == "UnresolvedUserError"


# --- GetDashboardTool ---------------------------------------------------------
#
# Never imported by any existing test.


@pytest.mark.asyncio
async def test_get_dashboard_tool_calls_manager(hass: HomeAssistant, admin_user):
    with patch(
        "custom_components.ha_dev_tools.llm_api.dashboard_manager.get_dashboard",
        AsyncMock(return_value={"views": []}),
    ) as mock_get:
        result = await GetDashboardTool()._run(
            hass,
            llm.ToolInput(tool_name="get_dashboard", tool_args={"url_path": "x"}),
            _llm_context(admin_user.id),
        )

    assert result == {"views": []}
    mock_get.assert_called_once_with(hass, admin_user, url_path="x")


@pytest.mark.asyncio
async def test_get_dashboard_tool_surfaces_unresolved_user(hass: HomeAssistant):
    """No user in context - resolve_user must refuse before dashboard_manager
    is ever called."""
    result = await GetDashboardTool()._run(
        hass, llm.ToolInput(tool_name="get_dashboard", tool_args={}), _llm_context()
    )

    assert result["error_type"] == "UnresolvedUserError"


# --- GetEnergyConfigTool / WriteEnergyConfigTool (issue #74) -----------------
#
# Never imported by any existing test. Mocking energy_manager directly
# (same pattern as ListHelpersTool's tests) rather than standing up the
# real `energy` component (which needs recorder/history, see
# test_energy_manager.py) - what needs the real component is already
# covered there; this only needs to prove the tool wires its args/errors
# through correctly.


@pytest.mark.asyncio
async def test_get_energy_config_tool_calls_manager(hass: HomeAssistant, admin_user):
    with patch(
        "custom_components.ha_dev_tools.llm_api.energy_manager.get_energy_config",
        AsyncMock(return_value={"energy_sources": []}),
    ) as mock_get:
        result = await GetEnergyConfigTool()._run(
            hass,
            llm.ToolInput(tool_name="get_energy_config", tool_args={}),
            _llm_context(admin_user.id),
        )

    assert result == {"energy_sources": []}
    mock_get.assert_called_once_with(hass, admin_user)


@pytest.mark.asyncio
async def test_get_energy_config_tool_surfaces_unresolved_user(hass: HomeAssistant):
    """No user in context - resolve_user must refuse before energy_manager
    is ever called."""
    result = await GetEnergyConfigTool()._run(
        hass, llm.ToolInput(tool_name="get_energy_config", tool_args={}), _llm_context()
    )

    assert result["error_type"] == "UnresolvedUserError"


@pytest.mark.asyncio
async def test_get_energy_config_tool_surfaces_not_configured(
    hass: HomeAssistant, admin_user
):
    """energy/get_prefs's real not_found error (never configured) must
    come back as a tool error, not propagate as a raw exception."""
    with patch(
        "custom_components.ha_dev_tools.llm_api.energy_manager.get_energy_config",
        AsyncMock(side_effect=WebSocketCommandError("not_found", "No prefs")),
    ):
        result = await GetEnergyConfigTool()._run(
            hass,
            llm.ToolInput(tool_name="get_energy_config", tool_args={}),
            _llm_context(admin_user.id),
        )

    assert result["error_type"] == "WebSocketCommandError"


@pytest.mark.asyncio
async def test_write_energy_config_tool_writes_and_reports_saved(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    """Direct _write() call (bypassing the confirm-token gate), same
    pattern as write_dashboard's equivalent test - the confirm-token flow
    itself is generic WriteGatedTool behavior, already covered by
    write_script's confirm-flow test."""
    with patch(
        "custom_components.ha_dev_tools.llm_api.energy_manager.write_energy_config",
        AsyncMock(return_value={"energy_sources": []}),
    ) as mock_write:
        result = await WriteEnergyConfigTool()._write(
            hass,
            llm.ToolInput(
                tool_name="write_energy_config",
                tool_args={"energy_sources": []},
            ),
            _llm_context(admin_user.id),
        )

    assert result["saved"] is True
    assert result["config"] == {"energy_sources": []}
    mock_write.assert_called_once_with(
        hass,
        admin_user,
        energy_sources=[],
        device_consumption=None,
        device_consumption_water=None,
    )


@pytest.mark.asyncio
async def test_write_energy_config_tool_surfaces_unresolved_user(
    hass: HomeAssistant, setup_integration_with_entry
):
    """No user in context - resolve_user must refuse before
    energy_manager.write_energy_config is ever called."""
    result = await WriteEnergyConfigTool()._write(
        hass,
        llm.ToolInput(
            tool_name="write_energy_config", tool_args={"energy_sources": []}
        ),
        _llm_context(),
    )

    assert result["error_type"] == "UnresolvedUserError"


@pytest.mark.asyncio
async def test_write_energy_config_tool_confirm_flow(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    """The propose/confirm gate every write tool needs - proposing first
    never calls energy_manager at all, confirming does."""
    _arm(hass)
    tool = WriteEnergyConfigTool()
    args = {"energy_sources": []}

    with patch(
        "custom_components.ha_dev_tools.llm_api.energy_manager.write_energy_config",
        AsyncMock(return_value={"energy_sources": []}),
    ) as mock_write:
        proposal = await tool.async_call(
            hass,
            llm.ToolInput(tool_name="write_energy_config", tool_args=args),
            _llm_context(admin_user.id),
        )
        assert proposal["confirmation_required"] is True
        mock_write.assert_not_called()

        confirmed_args = {**args, "confirm_token": proposal["confirm_token"]}
        result = await tool.async_call(
            hass,
            llm.ToolInput(tool_name="write_energy_config", tool_args=confirmed_args),
            _llm_context(admin_user.id),
        )

    assert result["saved"] is True
    mock_write.assert_called_once()


@pytest.mark.asyncio
async def test_write_energy_config_tool_passes_device_consumption_water(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    """The third optional field (device_consumption_water) must reach
    energy_manager too, not just energy_sources/device_consumption."""
    with patch(
        "custom_components.ha_dev_tools.llm_api.energy_manager.write_energy_config",
        AsyncMock(return_value={"device_consumption_water": []}),
    ) as mock_write:
        await WriteEnergyConfigTool()._write(
            hass,
            llm.ToolInput(
                tool_name="write_energy_config",
                tool_args={"device_consumption_water": []},
            ),
            _llm_context(admin_user.id),
        )

    mock_write.assert_called_once_with(
        hass,
        admin_user,
        energy_sources=None,
        device_consumption=None,
        device_consumption_water=[],
    )


@pytest.mark.asyncio
async def test_write_energy_config_tool_mirrors_when_enabled(
    hass: HomeAssistant,
    setup_integration_with_entry,
    admin_user,
):
    """Same mirroring wiring as write_dashboard - see that test's own
    comment for why _read_storage_file is patched directly (the test
    harness's in-memory Store means a real read-after-write here would
    never see energy_manager's write)."""
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    tool = WriteEnergyConfigTool()
    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # GET current - no energy config mirrored yet
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT after
        ]
    )

    with (
        patch(
            "custom_components.ha_dev_tools.llm_api.energy_manager.write_energy_config",
            AsyncMock(return_value={"energy_sources": []}),
        ),
        patch(
            "custom_components.ha_dev_tools.llm_api._read_storage_file",
            AsyncMock(side_effect=[None, '{"data": {"energy_sources": []}}']),
        ),
        patch(
            "custom_components.ha_dev_tools.mirror.async_get_clientsession",
            return_value=fake_session,
        ),
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="write_energy_config", tool_args={"energy_sources": []}
            ),
            _llm_context(admin_user.id),
        )

    assert result["mirror"]["mirrored"] is True
    assert result["mirror"]["commits"] == ["after"]


# --- AuditAutomationsTool ------------------------------------------------------
#
# audit_manager.py's own tests cover the actual audit logic - this is the
# thin tool wrapper, never imported by any existing test.


@pytest.mark.asyncio
async def test_audit_automations_tool_calls_manager(hass: HomeAssistant):
    manager = Mock()
    with patch(
        "custom_components.ha_dev_tools.llm_api.audit_manager.audit_automations",
        AsyncMock(return_value={"duplicates": []}),
    ) as mock_audit:
        result = await AuditAutomationsTool(manager)._run(
            hass,
            llm.ToolInput(tool_name="audit_automations", tool_args={}),
            _llm_context(),
        )

    assert result == {"duplicates": []}
    mock_audit.assert_called_once_with(hass, manager)


# --- TriggerAutomationTool / SetNumberValueTool / SetBooleanValueTool (issue #76) -
#
# Never imported by any existing test. service_call_manager.py's own tests
# (test_service_call_manager.py) cover the real service-call/validation
# logic - these only prove each tool's propose/confirm gate and error
# translation, same mocked-manager pattern as the tools above.


@pytest.mark.asyncio
async def test_trigger_automation_tool_confirm_flow_calls_manager(
    hass: HomeAssistant, admin_user
):
    _arm(hass)
    tool = TriggerAutomationTool()
    args = {"automation_id": "kitchen_id"}

    proposal = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="trigger_automation", tool_args=args),
        _llm_context(admin_user.id),
    )
    assert proposal["confirmation_required"] is True

    confirmed_args = {**args, "confirm_token": proposal["confirm_token"]}
    with patch(
        "custom_components.ha_dev_tools.llm_api.service_call_manager.trigger_automation",
        AsyncMock(return_value="automation.kitchen_lights"),
    ) as mock_trigger:
        result = await tool.async_call(
            hass,
            llm.ToolInput(tool_name="trigger_automation", tool_args=confirmed_args),
            _llm_context(admin_user.id),
        )

    assert _without_arm(result) == {
        "triggered": True,
        "entity_id": "automation.kitchen_lights",
    }
    mock_trigger.assert_called_once_with(hass, "kitchen_id", skip_condition=True)


@pytest.mark.asyncio
async def test_trigger_automation_tool_surfaces_not_running_error(hass: HomeAssistant):
    result = await TriggerAutomationTool()._write(
        hass,
        llm.ToolInput(
            tool_name="trigger_automation", tool_args={"automation_id": "unknown_id"}
        ),
        _llm_context(),
    )

    assert result["error_type"] == "AutomationNotRunningError"


@pytest.mark.asyncio
async def test_set_number_value_tool_confirm_flow_calls_manager(
    hass: HomeAssistant, admin_user
):
    _arm(hass)
    tool = SetNumberValueTool()
    args = {"entity_id": "number.battery_charge_slot", "value": 42.5}

    proposal = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="set_number_value", tool_args=args),
        _llm_context(admin_user.id),
    )
    assert proposal["confirmation_required"] is True

    confirmed_args = {**args, "confirm_token": proposal["confirm_token"]}
    with patch(
        "custom_components.ha_dev_tools.llm_api.service_call_manager.set_number_value",
        AsyncMock(return_value=None),
    ) as mock_set:
        result = await tool.async_call(
            hass,
            llm.ToolInput(tool_name="set_number_value", tool_args=confirmed_args),
            _llm_context(admin_user.id),
        )

    assert _without_arm(result) == {
        "entity_id": "number.battery_charge_slot",
        "value": 42.5,
    }
    mock_set.assert_called_once_with(hass, "number.battery_charge_slot", 42.5)


@pytest.mark.asyncio
async def test_set_number_value_tool_surfaces_invalid_domain_error(
    hass: HomeAssistant,
):
    result = await SetNumberValueTool()._write(
        hass,
        llm.ToolInput(
            tool_name="set_number_value",
            tool_args={"entity_id": "switch.garage_door", "value": 1},
        ),
        _llm_context(),
    )

    assert result["error_type"] == "InvalidEntityDomainError"


@pytest.mark.asyncio
async def test_set_boolean_value_tool_confirm_flow_calls_manager(
    hass: HomeAssistant, admin_user
):
    _arm(hass)
    tool = SetBooleanValueTool()
    args = {"entity_id": "input_boolean.vacation_mode", "state": True}

    proposal = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="set_boolean_value", tool_args=args),
        _llm_context(admin_user.id),
    )
    assert proposal["confirmation_required"] is True

    confirmed_args = {**args, "confirm_token": proposal["confirm_token"]}
    with patch(
        "custom_components.ha_dev_tools.llm_api.service_call_manager.set_boolean_value",
        AsyncMock(return_value=None),
    ) as mock_set:
        result = await tool.async_call(
            hass,
            llm.ToolInput(tool_name="set_boolean_value", tool_args=confirmed_args),
            _llm_context(admin_user.id),
        )

    assert _without_arm(result) == {
        "entity_id": "input_boolean.vacation_mode",
        "state": True,
    }
    mock_set.assert_called_once_with(hass, "input_boolean.vacation_mode", True)


@pytest.mark.asyncio
async def test_set_boolean_value_tool_surfaces_entity_not_found_error(
    hass: HomeAssistant,
):
    result = await SetBooleanValueTool()._write(
        hass,
        llm.ToolInput(
            tool_name="set_boolean_value",
            tool_args={"entity_id": "input_boolean.does_not_exist", "state": True},
        ),
        _llm_context(),
    )

    assert result["error_type"] == "EntityNotFoundError"


# --- write_dashboard tool (storage-file-based mirroring) ---------------------
#
# Unlike helpers (see llm_api.py's _mirror_file_write docstring for why
# those are deliberately NOT wired up), lovelace dashboards save
# immediately (LovelaceStorage.async_save -> self._store.async_save),
# confirmed against home-assistant/core source - so reading .storage/
# lovelace* right after write_dashboard() returns is safe.


@pytest.fixture
async def _setup_lovelace_components(hass: HomeAssistant):
    assert await async_setup_component(hass, "websocket_api", {})
    assert await async_setup_component(hass, "lovelace", {})


@pytest.mark.asyncio
async def test_write_dashboard_tool_writes_and_reports_saved(
    hass: HomeAssistant,
    setup_integration_with_entry,
    admin_user,
    _setup_lovelace_components,
):
    tool = WriteDashboardTool()

    result = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="write_dashboard",
            tool_args={"config": {"views": [{"title": "Test View", "cards": []}]}},
        ),
        _llm_context(admin_user.id),
    )

    assert result["saved"] is True
    assert "mirror" not in result  # mirroring not configured on this entry


@pytest.mark.asyncio
async def test_write_dashboard_tool_mirrors_when_enabled(
    hass: HomeAssistant,
    setup_integration_with_entry,
    admin_user,
    _setup_lovelace_components,
):
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    tool = WriteDashboardTool()
    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # GET current - no dashboard mirrored yet
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT after
        ]
    )

    # pytest_homeassistant_custom_component's `hass` fixture always wraps
    # Store in mock_storage() (an in-memory dict, never real disk - see its
    # own hass_storage fixture) - so unlike write_dashboard's real target
    # (LovelaceStorage.async_save() -> real file write, confirmed against
    # home-assistant/core), _read_storage_file's FileManager.read_file()
    # would never see the write this call makes. Patch it directly to
    # supply what a real .storage/lovelace read would return, so this test
    # isolates the mirror wiring itself, not the test harness's storage mock.
    with (
        patch(
            "custom_components.ha_dev_tools.llm_api._read_storage_file",
            AsyncMock(
                side_effect=[
                    None,
                    '{"data": {"config": {"views": [{"title": "New", "cards": []}]}}}',
                ]
            ),
        ),
        patch(
            "custom_components.ha_dev_tools.mirror.async_get_clientsession",
            return_value=fake_session,
        ),
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="write_dashboard",
                tool_args={"config": {"views": [{"title": "New", "cards": []}]}},
            ),
            _llm_context(admin_user.id),
        )

    assert result["mirror"]["mirrored"] is True
    # content_before is None (no dashboard saved before this call in this
    # test) - only the after-commit pushes, matching mirror.py's own
    # "content_before is None -> no before-commit" rule.
    assert result["mirror"]["commits"] == ["after"]


@pytest.mark.asyncio
async def test_write_dashboard_tool_surfaces_unresolved_user(
    hass: HomeAssistant, setup_integration_with_entry
):
    """No user in context - resolve_user must refuse before
    dashboard_manager.write_dashboard is ever called."""
    tool = WriteDashboardTool()

    result = await tool._write(
        hass,
        llm.ToolInput(tool_name="write_dashboard", tool_args={"config": {"views": []}}),
        _llm_context(),
    )

    assert result["error_type"] == "UnresolvedUserError"


# --- ListHelpersTool / helper tools' invalid-domain exception branches ------
#
# Each create/update/delete_helper tool already has success-path coverage
# below (via the mirroring tests) - what's missing is their shared
# except clause. Bypassing schema validation by calling _run()/_write()
# directly (as the rest of this file already does) lets a bad domain
# reach helper_manager's own real _check_domain() check, no mocking
# needed.


@pytest.mark.asyncio
async def test_list_helpers_tool_calls_manager(hass: HomeAssistant, admin_user):
    with patch(
        "custom_components.ha_dev_tools.llm_api.helper_manager.list_helpers",
        AsyncMock(return_value=[{"id": "abc"}]),
    ) as mock_list:
        result = await ListHelpersTool()._run(
            hass,
            llm.ToolInput(
                tool_name="list_helpers", tool_args={"domain": "input_boolean"}
            ),
            _llm_context(admin_user.id),
        )

    assert result == {"items": [{"id": "abc"}]}
    mock_list.assert_called_once_with(hass, admin_user, "input_boolean")


@pytest.mark.asyncio
async def test_list_helpers_tool_invalid_domain_returns_tool_error(
    hass: HomeAssistant, admin_user
):
    result = await ListHelpersTool()._run(
        hass,
        llm.ToolInput(tool_name="list_helpers", tool_args={"domain": "not_a_domain"}),
        _llm_context(admin_user.id),
    )

    assert result["error_type"] == "InvalidHelperDomainError"


@pytest.mark.asyncio
async def test_create_helper_tool_invalid_domain_returns_tool_error(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    # "input_bogus" (not a real HELPER_DOMAINS entry) still matches the
    # default security policy's ".storage/input_*" read allowlist pattern,
    # so _read_storage_file succeeds and _check_domain's rejection is what
    # actually gets exercised here, not an unrelated permission error.
    result = await CreateHelperTool()._write(
        hass,
        llm.ToolInput(
            tool_name="create_helper",
            tool_args={"domain": "input_bogus", "config": {"name": "x"}},
        ),
        _llm_context(admin_user.id),
    )

    assert result["error_type"] == "InvalidHelperDomainError"


@pytest.mark.asyncio
async def test_update_helper_tool_invalid_domain_returns_tool_error(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    result = await UpdateHelperTool()._write(
        hass,
        llm.ToolInput(
            tool_name="update_helper",
            tool_args={
                "domain": "input_bogus",
                "item_id": "x",
                "config": {"name": "x"},
            },
        ),
        _llm_context(admin_user.id),
    )

    assert result["error_type"] == "InvalidHelperDomainError"


@pytest.mark.asyncio
async def test_delete_helper_tool_invalid_domain_returns_tool_error(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    result = await DeleteHelperTool()._write(
        hass,
        llm.ToolInput(
            tool_name="delete_helper",
            tool_args={"domain": "input_bogus", "item_id": "x"},
        ),
        _llm_context(admin_user.id),
    )

    assert result["error_type"] == "InvalidHelperDomainError"


# --- helper mirroring: in-memory reconstruction, never re-reading the ------
# --- debounced-save storage file (issue #43) --------------------------------


@pytest.fixture
async def _setup_websocket_api_for_helpers(hass: HomeAssistant):
    """create/update/delete_helper go through the real WS command dispatch
    (ws_call.py) - needs websocket_api registered, same as
    test_helper_manager.py's identical fixture."""
    assert await async_setup_component(hass, "websocket_api", {})


@pytest.mark.asyncio
async def test_create_helper_tool_mirrors_reconstructed_content(
    hass: HomeAssistant,
    setup_integration_with_entry,
    admin_user,
    _setup_websocket_api_for_helpers,
):
    """create_helper's mirror push never re-reads .storage/input_boolean
    after the write (HA's StorageCollection debounces that save 10s) -
    the "after" content is spliced together in memory from the "before"
    content plus the WS command's own returned item."""
    assert await async_setup_component(hass, "input_boolean", {})
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    tool = CreateHelperTool()
    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # GET current - not mirrored yet
            # before_content is non-None but the mirror repo has nothing
            # recorded yet (previous GET was 404) - _sync_before treats
            # that as drift and pushes a before-commit first.
            _FakeMirrorResponse(200, {"content": {"sha": "sha-before"}}),  # PUT before
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT after
        ]
    )
    before_content = (
        '{"version": 1, "minor_version": 1, "key": "input_boolean", '
        '"data": {"items": [{"id": "existing", "name": "Existing"}]}}'
    )

    with (
        patch(
            "custom_components.ha_dev_tools.llm_api._read_storage_file",
            AsyncMock(return_value=before_content),
        ),
        patch(
            "custom_components.ha_dev_tools.mirror.async_get_clientsession",
            return_value=fake_session,
        ),
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="create_helper",
                tool_args={"domain": "input_boolean", "config": {"name": "New"}},
            ),
            _llm_context(admin_user.id),
        )

    assert result["mirror"]["mirrored"] is True
    assert result["mirror"]["commits"] == ["before", "after"]
    put_after_json = fake_session.calls[-1][2]["json"]["content"]

    after_content = json.loads(base64.b64decode(put_after_json))
    item_ids = {item["id"] for item in after_content["data"]["items"]}
    assert item_ids == {"existing", result["id"]}


@pytest.mark.asyncio
async def test_update_helper_tool_mirrors_reconstructed_content(
    hass: HomeAssistant,
    setup_integration_with_entry,
    admin_user,
    _setup_websocket_api_for_helpers,
):
    assert await async_setup_component(hass, "input_boolean", {})
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    created = await helper_manager.create_helper(
        hass, admin_user, "input_boolean", {"name": "Original"}
    )
    tool = UpdateHelperTool()
    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # GET current - not mirrored yet
            _FakeMirrorResponse(200, {"content": {"sha": "sha-before"}}),  # PUT before
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT after
        ]
    )
    before_content = json.dumps(
        {
            "version": 1,
            "minor_version": 1,
            "key": "input_boolean",
            "data": {"items": [created]},
        }
    )

    with (
        patch(
            "custom_components.ha_dev_tools.llm_api._read_storage_file",
            AsyncMock(return_value=before_content),
        ),
        patch(
            "custom_components.ha_dev_tools.mirror.async_get_clientsession",
            return_value=fake_session,
        ),
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="update_helper",
                tool_args={
                    "domain": "input_boolean",
                    "item_id": created["id"],
                    "config": {"name": "Renamed"},
                },
            ),
            _llm_context(admin_user.id),
        )

    assert result["mirror"]["mirrored"] is True
    put_after_json = fake_session.calls[-1][2]["json"]["content"]

    after_content = json.loads(base64.b64decode(put_after_json))
    assert after_content["data"]["items"] == [{"id": created["id"], "name": "Renamed"}]


@pytest.mark.asyncio
async def test_delete_helper_tool_mirrors_reconstructed_content(
    hass: HomeAssistant,
    setup_integration_with_entry,
    admin_user,
    _setup_websocket_api_for_helpers,
):
    assert await async_setup_component(hass, "input_boolean", {})
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    created = await helper_manager.create_helper(
        hass, admin_user, "input_boolean", {"name": "Doomed"}
    )
    tool = DeleteHelperTool()
    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # GET current - not mirrored yet
            _FakeMirrorResponse(200, {"content": {"sha": "sha-before"}}),  # PUT before
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT after
        ]
    )
    before_content = json.dumps(
        {
            "version": 1,
            "minor_version": 1,
            "key": "input_boolean",
            "data": {"items": [created]},
        }
    )

    with (
        patch(
            "custom_components.ha_dev_tools.llm_api._read_storage_file",
            AsyncMock(return_value=before_content),
        ),
        patch(
            "custom_components.ha_dev_tools.mirror.async_get_clientsession",
            return_value=fake_session,
        ),
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="delete_helper",
                tool_args={"domain": "input_boolean", "item_id": created["id"]},
            ),
            _llm_context(admin_user.id),
        )

    assert result["mirror"]["mirrored"] is True
    put_after_json = fake_session.calls[-1][2]["json"]["content"]

    after_content = json.loads(base64.b64decode(put_after_json))
    assert after_content["data"]["items"] == []


@pytest.mark.asyncio
async def test_create_helper_tool_no_mirror_key_when_disabled(
    hass: HomeAssistant,
    setup_integration_with_entry,
    admin_user,
    _setup_websocket_api_for_helpers,
):
    """Mirroring off (the default) - no 'mirror' key at all, not even an attempt."""
    assert await async_setup_component(hass, "input_boolean", {})
    tool = CreateHelperTool()

    result = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="create_helper",
            tool_args={"domain": "input_boolean", "config": {"name": "New"}},
        ),
        _llm_context(admin_user.id),
    )

    assert "mirror" not in result


# --- derived-sensor mirroring: synthetic per-entry JSON path (issue #34) ----


@pytest.mark.asyncio
async def test_create_derived_sensor_tool_mirrors_when_enabled(
    hass: HomeAssistant, setup_integration_with_entry
):
    """create_derived_sensor has no real file - the resolved ConfigEntry's
    own .data/.options (get_derived_sensor's shape) is pushed to a
    synthetic derived_sensors/<domain>/<entry_id>.json path."""
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    tool = CreateDerivedSensorTool()
    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # GET current - not mirrored yet
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT after
        ]
    )

    with (
        patch(
            "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.create_derived_sensor",
            AsyncMock(
                return_value={
                    "entry_id": "abc",
                    "domain": "min_max",
                    "data": {},
                    "options": {"entity_ids": ["sensor.x"], "type": "max"},
                }
            ),
        ),
        patch(
            "custom_components.ha_dev_tools.mirror.async_get_clientsession",
            return_value=fake_session,
        ),
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="create_derived_sensor",
                tool_args={"domain": "min_max", "steps": {"user": {}}},
            ),
            _llm_context(),
        )

    assert result["mirror"]["mirrored"] is True
    assert result["mirror"]["commits"] == ["after"]
    put_call = fake_session.calls[-1]
    assert put_call[1].endswith("/contents/derived_sensors/min_max/abc.json")
    after_content = json.loads(base64.b64decode(put_call[2]["json"]["content"]))
    assert after_content["options"] == {"entity_ids": ["sensor.x"], "type": "max"}


@pytest.mark.asyncio
async def test_update_derived_sensor_tool_mirrors_when_enabled(
    hass: HomeAssistant, setup_integration_with_entry
):
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    tool = UpdateDerivedSensorTool()
    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # GET current - not mirrored yet
            _FakeMirrorResponse(200, {"content": {"sha": "sha-before"}}),  # PUT before
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT after
        ]
    )

    with (
        patch(
            "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.get_derived_sensor",
            return_value={
                "entry_id": "abc",
                "domain": "min_max",
                "options": {"type": "min"},
            },
        ),
        patch(
            "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.update_derived_sensor",
            AsyncMock(
                return_value={
                    "entry_id": "abc",
                    "domain": "min_max",
                    "options": {"type": "max"},
                }
            ),
        ),
        patch(
            "custom_components.ha_dev_tools.mirror.async_get_clientsession",
            return_value=fake_session,
        ),
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="update_derived_sensor",
                tool_args={"entry_id": "abc", "steps": {"init": {"type": "max"}}},
            ),
            _llm_context(),
        )

    assert result["mirror"]["mirrored"] is True
    assert result["mirror"]["commits"] == ["before", "after"]
    put_before_json = fake_session.calls[2][2]["json"]["content"]
    put_after_json = fake_session.calls[-1][2]["json"]["content"]
    before_content = json.loads(base64.b64decode(put_before_json))
    after_content = json.loads(base64.b64decode(put_after_json))
    assert before_content["options"] == {"type": "min"}
    assert after_content["options"] == {"type": "max"}


@pytest.mark.asyncio
async def test_delete_derived_sensor_tool_mirrors_deletion_marker(
    hass: HomeAssistant, setup_integration_with_entry
):
    """No real file to remove - the mirrored "after" state is a small
    tombstone JSON marking the entry as deleted, preserving the entry's
    last real config in the mirror repo's own git history."""
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    tool = DeleteDerivedSensorTool()
    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # GET current - not mirrored yet
            _FakeMirrorResponse(200, {"content": {"sha": "sha-before"}}),  # PUT before
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT after
        ]
    )

    with (
        patch(
            "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.get_derived_sensor",
            return_value={"entry_id": "abc", "domain": "min_max"},
        ),
        patch(
            "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.delete_derived_sensor",
            AsyncMock(return_value={"deleted": True, "entry_id": "abc"}),
        ),
        patch(
            "custom_components.ha_dev_tools.mirror.async_get_clientsession",
            return_value=fake_session,
        ),
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="delete_derived_sensor", tool_args={"entry_id": "abc"}
            ),
            _llm_context(),
        )

    assert result["mirror"]["mirrored"] is True
    put_after_json = fake_session.calls[-1][2]["json"]["content"]
    after_content = json.loads(base64.b64decode(put_after_json))
    assert after_content == {"deleted": True, "entry_id": "abc"}


@pytest.mark.asyncio
async def test_update_derived_sensor_tool_no_mirror_key_when_disabled(
    hass: HomeAssistant,
):
    """Mirroring off (the default) - get_derived_sensor is never even
    called, matching the pre-existing (no-mirroring) test's mocking."""
    tool = UpdateDerivedSensorTool()
    with patch(
        "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.update_derived_sensor",
        AsyncMock(return_value={"entry_id": "abc", "domain": "min_max"}),
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="update_derived_sensor", tool_args={"entry_id": "abc"}
            ),
            _llm_context(),
        )

    assert "mirror" not in result


# --- dry-run + mirroring: proposed/<kind>-<id> branches (issue #35) ---------


def _write_automation_manager(hass: HomeAssistant, tmp_path) -> AutomationManager:
    hass.config.config_dir = str(tmp_path)
    security_manager = SecurityManager(
        hass,
        {
            "read_paths": ["automations.yaml", "packages/**/*.yaml"],
            "write_paths": ["automations.yaml", "packages/**/*.yaml"],
            "denied_paths": [],
        },
    )
    file_manager = FileManager(hass, security_manager)
    return AutomationManager(hass, file_manager)


@pytest.mark.asyncio
async def test_write_automation_tool_dry_run_mirrors_to_proposed_branch(
    hass: HomeAssistant, setup_integration_with_entry, admin_user, tmp_path
):
    """Dry-run mode + mirroring enabled together push the resolved would-be
    content to a proposed/automation-<id> branch instead of mirroring
    nothing at all - the gap this session's earlier code left (issue #35)."""
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_DRY_RUN: True,
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    manager = _write_automation_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "automations.yaml").write_text(
        "- id: my_automation\n  alias: Old\n  trigger: []\n  action: []\n"
    )
    tool = WriteAutomationTool(manager)
    args = {
        "automation_id": "my_automation",
        "config": {"alias": "New", "trigger": [], "action": []},
    }

    proposal = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="write_automation", tool_args=args),
        _llm_context(admin_user.id),
    )
    assert proposal["confirmation_required"] is True

    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # _sync_before GET current -> none
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT before
            _FakeMirrorResponse(200, {"object": {"sha": "main-sha"}}),  # GET ref/main
            _FakeMirrorResponse(404),  # GET ref/proposed -> doesn't exist
            _FakeMirrorResponse(201),  # POST create ref
            _FakeMirrorResponse(404),  # GET current on proposed branch
            _FakeMirrorResponse(201, {"content": {"sha": "sha-2"}}),  # PUT proposed
        ]
    )
    confirmed_args = {**args, "confirm_token": proposal["confirm_token"]}
    with patch(
        "custom_components.ha_dev_tools.mirror.async_get_clientsession",
        return_value=fake_session,
    ):
        result = await tool.async_call(
            hass,
            llm.ToolInput(tool_name="write_automation", tool_args=confirmed_args),
            _llm_context(admin_user.id),
        )

    assert result["dry_run"] is True
    assert result["mirror"]["mirrored"] is True
    assert result["mirror"]["branch"] == "proposed/automation-my_automation"
    assert result["mirror"]["commits"] == ["before", "proposed"]
    # Nothing live actually changed:
    assert "Old" in (tmp_path / "automations.yaml").read_text()
    assert "New" not in (tmp_path / "automations.yaml").read_text()


@pytest.mark.asyncio
async def test_write_automation_tool_dry_run_no_mirror_key_when_mirroring_disabled(
    hass: HomeAssistant, setup_integration_with_entry, admin_user, tmp_path
):
    """Dry-run alone (mirroring off) behaves exactly as before this feature -
    no 'mirror' key at all, not even an attempt."""
    hass.config_entries.async_update_entry(
        setup_integration_with_entry, options={OPT_DRY_RUN: True}
    )
    manager = _write_automation_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "automations.yaml").write_text(
        "- id: my_automation\n  trigger: []\n  action: []\n"
    )
    tool = WriteAutomationTool(manager)
    args = {"automation_id": "my_automation", "config": {"trigger": [], "action": []}}

    proposal = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="write_automation", tool_args=args),
        _llm_context(admin_user.id),
    )
    confirmed_args = {**args, "confirm_token": proposal["confirm_token"]}
    result = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="write_automation", tool_args=confirmed_args),
        _llm_context(admin_user.id),
    )

    assert result["dry_run"] is True
    assert "mirror" not in result


@pytest.mark.asyncio
async def test_write_automation_tool_write_performs_real_write_when_dry_run_disabled(
    hass: HomeAssistant, tmp_path
):
    """Every other write_automation test above drives dry-run mode - this
    is the only one exercising the real _write() path (dry-run disabled,
    the default), calling _write() directly to isolate it from the
    confirm-flow/gating already covered elsewhere."""
    manager = _write_automation_manager(hass, tmp_path)
    (tmp_path / "automations.yaml").write_text(
        "- id: my_automation\n  alias: Old\n  trigger: []\n  action: []\n"
    )
    hass.services.async_register("automation", "reload", AsyncMock())
    tool = WriteAutomationTool(manager)

    result = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="write_automation",
            tool_args={
                "automation_id": "my_automation",
                "config": {"alias": "New", "trigger": [], "action": []},
            },
        ),
        _llm_context(),
    )

    assert result["file_path"] == "automations.yaml"
    assert result["is_package"] is False
    assert "mirror" not in result
    assert "New" in (tmp_path / "automations.yaml").read_text()


@pytest.mark.asyncio
async def test_write_automation_tool_write_surfaces_not_found(
    hass: HomeAssistant, tmp_path
):
    """write_automation's own _write() exception handling - a nonexistent
    target package must come back as a _tool_error, not raise
    (_dry_run_mirror's identical except clause is covered separately
    below - the real _write() path here was never exercised by anything)."""
    manager = _write_automation_manager(hass, tmp_path)
    tool = WriteAutomationTool(manager)

    result = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="write_automation",
            tool_args={
                "automation_id": "brand_new",
                "config": {"trigger": [], "action": []},
                "package": "does_not_exist.yaml",
            },
        ),
        _llm_context(),
    )

    assert result["error_type"] == "AutomationNotFoundError"


@pytest.mark.asyncio
async def test_write_automation_tool_dry_run_mirror_skips_when_package_missing(
    hass: HomeAssistant, tmp_path
):
    """_dry_run_mirror's own exception handling - a dry-run write targeting
    a nonexistent package file must skip mirroring with a reason, not raise
    AutomationNotFoundError uncaught."""
    manager = _write_automation_manager(hass, tmp_path)
    tool = WriteAutomationTool(manager)

    result = await tool._dry_run_mirror(
        hass,
        llm.ToolInput(
            tool_name="write_automation",
            tool_args={
                "automation_id": "brand_new",
                "config": {"trigger": [], "action": []},
                "package": "does_not_exist.yaml",
            },
        ),
        _llm_context(),
    )

    assert result.mirrored is False
    assert "does_not_exist.yaml" in result.reason


@pytest.mark.asyncio
async def test_delete_automation_tool_confirm_flow_deletes_and_reloads(
    hass: HomeAssistant, admin_user, tmp_path
):
    manager = _write_automation_manager(hass, tmp_path)
    _arm(hass)
    reload_mock = AsyncMock()
    hass.services.async_register("automation", "reload", reload_mock)
    (tmp_path / "automations.yaml").write_text(
        "- id: gone\n  alias: Gone\n  trigger: []\n  action: []\n"
    )
    tool = DeleteAutomationTool(manager)
    args = {"automation_id": "gone"}

    proposal = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="delete_automation", tool_args=args),
        _llm_context(admin_user.id),
    )
    assert proposal["confirmation_required"] is True

    confirmed_args = {**args, "confirm_token": proposal["confirm_token"]}
    result = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="delete_automation", tool_args=confirmed_args),
        _llm_context(admin_user.id),
    )

    assert result["deleted"] is True
    assert result["file_path"] == "automations.yaml"
    reload_mock.assert_called_once()
    assert "gone" not in (tmp_path / "automations.yaml").read_text()


@pytest.mark.asyncio
async def test_delete_automation_tool_not_found_returns_tool_error(
    hass: HomeAssistant, admin_user, tmp_path
):
    """AutomationNotFoundError from the manager becomes a _tool_error()
    payload, not a raw exception escaping the tool."""
    manager = _write_automation_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "automations.yaml").write_text(
        "- id: keep\n  alias: Keep\n  trigger: []\n  action: []\n"
    )
    tool = DeleteAutomationTool(manager)
    args = {"automation_id": "does_not_exist"}

    proposal = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="delete_automation", tool_args=args),
        _llm_context(admin_user.id),
    )
    confirmed_args = {**args, "confirm_token": proposal["confirm_token"]}
    result = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="delete_automation", tool_args=confirmed_args),
        _llm_context(admin_user.id),
    )

    assert result["error_type"] == "AutomationNotFoundError"
    assert "does_not_exist" in result["error"]


@pytest.mark.asyncio
async def test_delete_automation_tool_dry_run_not_found_skips_mirror(
    hass: HomeAssistant, setup_integration_with_entry, admin_user, tmp_path
):
    """Same not-found handling in the dry-run + mirroring hook: the error
    becomes a not-mirrored MirrorResult rather than escaping, and
    mirror.mirror_dry_run is never reached."""
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_DRY_RUN: True,
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    manager = _write_automation_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "automations.yaml").write_text(
        "- id: keep\n  alias: Keep\n  trigger: []\n  action: []\n"
    )
    tool = DeleteAutomationTool(manager)
    args = {"automation_id": "does_not_exist"}

    proposal = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="delete_automation", tool_args=args),
        _llm_context(admin_user.id),
    )
    confirmed_args = {**args, "confirm_token": proposal["confirm_token"]}
    result = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="delete_automation", tool_args=confirmed_args),
        _llm_context(admin_user.id),
    )

    assert result["dry_run"] is True
    assert result["mirror"]["mirrored"] is False
    assert "does_not_exist" in result["mirror"]["reason"]


@pytest.mark.asyncio
async def test_delete_automation_tool_mirrors_when_enabled(
    hass: HomeAssistant, setup_integration_with_entry, admin_user, tmp_path
):
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    manager = _write_automation_manager(hass, tmp_path)
    _arm(hass)
    hass.services.async_register("automation", "reload", AsyncMock())
    (tmp_path / "automations.yaml").write_text(
        "- id: gone\n  alias: Gone\n  trigger: []\n  action: []\n"
    )
    tool = DeleteAutomationTool(manager)
    args = {"automation_id": "gone"}

    proposal = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="delete_automation", tool_args=args),
        _llm_context(admin_user.id),
    )
    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # GET current - not mirrored yet
            # content_before is the real (non-None) file content, and the
            # mirror repo has nothing recorded yet (404 above) -
            # _sync_before treats that as drift and pushes a before-commit
            # first, same as any other mirrored write.
            _FakeMirrorResponse(200, {"content": {"sha": "sha-before"}}),  # PUT before
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT after
        ]
    )
    confirmed_args = {**args, "confirm_token": proposal["confirm_token"]}
    with patch(
        "custom_components.ha_dev_tools.mirror.async_get_clientsession",
        return_value=fake_session,
    ):
        result = await tool.async_call(
            hass,
            llm.ToolInput(tool_name="delete_automation", tool_args=confirmed_args),
            _llm_context(admin_user.id),
        )

    assert result["mirror"]["mirrored"] is True
    assert result["mirror"]["commits"] == ["before", "after"]


@pytest.mark.asyncio
async def test_delete_automation_tool_dry_run_mirrors_to_proposed_branch(
    hass: HomeAssistant, setup_integration_with_entry, admin_user, tmp_path
):
    """Same dry-run + mirroring behavior as write_automation's equivalent
    test (issue #35) - delete_automation's _dry_run_mirror hook reuses
    delete_automation's own resolve+build logic via dry_run=True."""
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_DRY_RUN: True,
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    manager = _write_automation_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "automations.yaml").write_text(
        "- id: my_automation\n  alias: Old\n  trigger: []\n  action: []\n"
    )
    tool = DeleteAutomationTool(manager)
    args = {"automation_id": "my_automation"}

    proposal = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="delete_automation", tool_args=args),
        _llm_context(admin_user.id),
    )
    assert proposal["confirmation_required"] is True

    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # _sync_before GET current -> none
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT before
            _FakeMirrorResponse(200, {"object": {"sha": "main-sha"}}),  # GET ref/main
            _FakeMirrorResponse(404),  # GET ref/proposed -> doesn't exist
            _FakeMirrorResponse(201),  # POST create ref
            _FakeMirrorResponse(404),  # GET current on proposed branch
            _FakeMirrorResponse(201, {"content": {"sha": "sha-2"}}),  # PUT proposed
        ]
    )
    confirmed_args = {**args, "confirm_token": proposal["confirm_token"]}
    with patch(
        "custom_components.ha_dev_tools.mirror.async_get_clientsession",
        return_value=fake_session,
    ):
        result = await tool.async_call(
            hass,
            llm.ToolInput(tool_name="delete_automation", tool_args=confirmed_args),
            _llm_context(admin_user.id),
        )

    assert result["dry_run"] is True
    assert result["mirror"]["mirrored"] is True
    assert result["mirror"]["branch"] == "proposed/automation-my_automation"
    assert result["mirror"]["commits"] == ["before", "proposed"]
    # Nothing live actually changed:
    assert "my_automation" in (tmp_path / "automations.yaml").read_text()


def _write_script_manager(hass: HomeAssistant, tmp_path) -> ScriptManager:
    hass.config.config_dir = str(tmp_path)
    security_manager = SecurityManager(
        hass,
        {
            "read_paths": ["scripts.yaml", "packages/**/*.yaml"],
            "write_paths": ["scripts.yaml", "packages/**/*.yaml"],
            "denied_paths": [],
        },
    )
    file_manager = FileManager(hass, security_manager)
    return ScriptManager(hass, file_manager)


@pytest.mark.asyncio
async def test_write_script_tool_dry_run_mirrors_to_proposed_branch(
    hass: HomeAssistant, setup_integration_with_entry, admin_user, tmp_path
):
    """Same dry-run + mirroring behavior as write_automation's equivalent
    test (issue #35) - write_script's _dry_run_mirror hook reuses
    write_script's own resolve+build logic via dry_run=True."""
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_DRY_RUN: True,
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    manager = _write_script_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "scripts.yaml").write_text("my_script:\n  alias: Old\n  sequence: []\n")
    tool = WriteScriptTool(manager)
    args = {
        "script_id": "my_script",
        "config": {"alias": "New", "sequence": []},
    }

    proposal = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="write_script", tool_args=args),
        _llm_context(admin_user.id),
    )
    assert proposal["confirmation_required"] is True

    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # _sync_before GET current -> none
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT before
            _FakeMirrorResponse(200, {"object": {"sha": "main-sha"}}),  # GET ref/main
            _FakeMirrorResponse(404),  # GET ref/proposed -> doesn't exist
            _FakeMirrorResponse(201),  # POST create ref
            _FakeMirrorResponse(404),  # GET current on proposed branch
            _FakeMirrorResponse(201, {"content": {"sha": "sha-2"}}),  # PUT proposed
        ]
    )
    confirmed_args = {**args, "confirm_token": proposal["confirm_token"]}
    with patch(
        "custom_components.ha_dev_tools.mirror.async_get_clientsession",
        return_value=fake_session,
    ):
        result = await tool.async_call(
            hass,
            llm.ToolInput(tool_name="write_script", tool_args=confirmed_args),
            _llm_context(admin_user.id),
        )

    assert result["dry_run"] is True
    assert result["mirror"]["mirrored"] is True
    assert result["mirror"]["branch"] == "proposed/script-my_script"
    assert result["mirror"]["commits"] == ["before", "proposed"]
    # Nothing live actually changed:
    assert "Old" in (tmp_path / "scripts.yaml").read_text()
    assert "New" not in (tmp_path / "scripts.yaml").read_text()


@pytest.mark.asyncio
async def test_write_script_tool_write_surfaces_not_found(
    hass: HomeAssistant, tmp_path
):
    """write_script's own _write() exception handling - a nonexistent
    target package must come back as a _tool_error, not raise."""
    manager = _write_script_manager(hass, tmp_path)
    tool = WriteScriptTool(manager)

    result = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="write_script",
            tool_args={
                "script_id": "brand_new",
                "config": {"sequence": []},
                "package": "does_not_exist.yaml",
            },
        ),
        _llm_context(),
    )

    assert result["error_type"] == "ScriptNotFoundError"


@pytest.mark.asyncio
async def test_write_script_tool_dry_run_mirror_skips_when_not_found(
    hass: HomeAssistant, tmp_path
):
    manager = _write_script_manager(hass, tmp_path)
    tool = WriteScriptTool(manager)

    result = await tool._dry_run_mirror(
        hass,
        llm.ToolInput(
            tool_name="write_script",
            tool_args={
                "script_id": "brand_new",
                "config": {"sequence": []},
                "package": "does_not_exist.yaml",
            },
        ),
        _llm_context(),
    )

    assert result.mirrored is False


@pytest.mark.asyncio
async def test_update_template_entity_tool_dry_run_mirrors_to_proposed_branch(
    hass: HomeAssistant, setup_integration_with_entry, template_yaml_manager, tmp_path
):
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    _write_package(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        "  - sensor:\n"
        "      - name: Old\n"
        "        unique_id: target\n"
        '        state: "{{ 1 }}"\n',
    )
    tool = UpdateTemplateEntityTool(template_yaml_manager)
    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # _sync_before GET current -> none
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT before
            _FakeMirrorResponse(200, {"object": {"sha": "main-sha"}}),  # GET ref/main
            _FakeMirrorResponse(404),  # GET ref/proposed -> doesn't exist
            _FakeMirrorResponse(201),  # POST create ref
            _FakeMirrorResponse(404),  # GET current on proposed branch
            _FakeMirrorResponse(201, {"content": {"sha": "sha-2"}}),  # PUT proposed
        ]
    )

    with patch(
        "custom_components.ha_dev_tools.mirror.async_get_clientsession",
        return_value=fake_session,
    ):
        result = await tool._dry_run_mirror(
            hass,
            llm.ToolInput(
                tool_name="update_template_entity",
                tool_args={
                    "unique_id": "target",
                    "config": {"name": "New", "state": "{{ 2 }}"},
                },
            ),
            _llm_context(),
        )

    assert result.mirrored is True
    assert result.branch == "proposed/template_entity-target"
    assert result.commits == ("before", "proposed")
    # Nothing live actually changed:
    raw = (tmp_path / "packages/emhas.yaml").read_text()
    assert "Old" in raw
    assert "New" not in raw


@pytest.mark.asyncio
async def test_create_template_entity_tool_dry_run_mirrors_to_proposed_branch(
    hass: HomeAssistant, setup_integration_with_entry, template_yaml_manager, tmp_path
):
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    _write_package(tmp_path, "packages/emhas.yaml", "template: []\n")
    tool = CreateTemplateEntityTool(template_yaml_manager)
    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # _sync_before GET current -> none
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT before
            _FakeMirrorResponse(200, {"object": {"sha": "main-sha"}}),  # GET ref/main
            _FakeMirrorResponse(404),  # GET ref/proposed -> doesn't exist
            _FakeMirrorResponse(201),  # POST create ref
            _FakeMirrorResponse(404),  # GET current on proposed branch
            _FakeMirrorResponse(201, {"content": {"sha": "sha-2"}}),  # PUT proposed
        ]
    )

    with patch(
        "custom_components.ha_dev_tools.mirror.async_get_clientsession",
        return_value=fake_session,
    ):
        result = await tool._dry_run_mirror(
            hass,
            llm.ToolInput(
                tool_name="create_template_entity",
                tool_args={
                    "platform": "sensor",
                    "config": {
                        "name": "New",
                        "unique_id": "new_one",
                        "state": "{{ 1 }}",
                    },
                    "package": "emhas.yaml",
                },
            ),
            _llm_context(),
        )

    assert result.mirrored is True
    assert result.branch == "proposed/template_entity-new_one"
    assert result.commits == ("before", "proposed")
    # Nothing live actually changed:
    raw = (tmp_path / "packages/emhas.yaml").read_text()
    assert "new_one" not in raw


@pytest.mark.asyncio
async def test_delete_template_entity_tool_dry_run_mirrors_to_proposed_branch(
    hass: HomeAssistant, setup_integration_with_entry, template_yaml_manager, tmp_path
):
    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    _write_package(
        tmp_path,
        "packages/emhas.yaml",
        "template:\n"
        "  - sensor:\n"
        "      - name: Gone\n"
        "        unique_id: gone\n"
        '        state: "{{ 1 }}"\n',
    )
    tool = DeleteTemplateEntityTool(template_yaml_manager)
    fake_session = _FakeMirrorSession(
        [
            _FakeMirrorResponse(200, {"default_branch": "main"}),  # GET repo info
            _FakeMirrorResponse(404),  # _sync_before GET current -> none
            _FakeMirrorResponse(200, {"content": {"sha": "sha-1"}}),  # PUT before
            _FakeMirrorResponse(200, {"object": {"sha": "main-sha"}}),  # GET ref/main
            _FakeMirrorResponse(404),  # GET ref/proposed -> doesn't exist
            _FakeMirrorResponse(201),  # POST create ref
            _FakeMirrorResponse(404),  # GET current on proposed branch
            _FakeMirrorResponse(201, {"content": {"sha": "sha-2"}}),  # PUT proposed
        ]
    )

    with patch(
        "custom_components.ha_dev_tools.mirror.async_get_clientsession",
        return_value=fake_session,
    ):
        result = await tool._dry_run_mirror(
            hass,
            llm.ToolInput(
                tool_name="delete_template_entity", tool_args={"unique_id": "gone"}
            ),
            _llm_context(),
        )

    assert result.mirrored is True
    assert result.branch == "proposed/template_entity-gone"
    assert result.commits == ("before", "proposed")
    # Nothing live actually changed:
    raw = (tmp_path / "packages/emhas.yaml").read_text()
    assert "gone" in raw


# --- Batch deletes for helpers, derived sensors, template entities (#66) -----


def test_one_or_many_rules():
    assert _one_or_many({"x": "a"}, "x", "xs") == ["a"]
    assert _one_or_many({"xs": ["a", "b"]}, "x", "xs") == ["a", "b"]
    for args, message in (
        ({}, "exactly one of x or xs"),
        ({"x": "a", "xs": ["a"]}, "exactly one of x or xs"),
        ({"xs": []}, "xs is empty"),
        ({"xs": ["a", "a"]}, "more than once"),
    ):
        with pytest.raises(ValueError, match=message):
            _one_or_many(args, "x", "xs")


@pytest.mark.asyncio
async def test_delete_helper_tool_batch_is_all_or_nothing(
    hass: HomeAssistant,
    setup_integration_with_entry,
    admin_user,
    _setup_websocket_api_for_helpers,
):
    assert await async_setup_component(hass, "input_boolean", {})
    one = await helper_manager.create_helper(
        hass, admin_user, "input_boolean", {"name": "One"}
    )
    two = await helper_manager.create_helper(
        hass, admin_user, "input_boolean", {"name": "Two"}
    )
    tool = DeleteHelperTool()

    async def delete(ids):
        with patch(
            "custom_components.ha_dev_tools.llm_api._read_storage_file",
            AsyncMock(return_value=None),
        ):
            return await tool._write(
                hass,
                llm.ToolInput(
                    tool_name="delete_helper",
                    tool_args={"domain": "input_boolean", "item_ids": ids},
                ),
                _llm_context(admin_user.id),
            )

    refused = await delete([one["id"], "missing"])
    remaining = await helper_manager.list_helpers(hass, admin_user, "input_boolean")
    assert "missing" in refused["error"] and "nothing was deleted" in refused["error"]
    assert {item["id"] for item in remaining} == {one["id"], two["id"]}

    deleted = await delete([one["id"], two["id"]])
    assert deleted == {"deleted": [one["id"], two["id"]], "domain": "input_boolean"}
    assert await helper_manager.list_helpers(hass, admin_user, "input_boolean") == []


@pytest.mark.asyncio
async def test_delete_helper_tool_batch_mirrors_all_removals_in_one_commit(
    hass: HomeAssistant,
    setup_integration_with_entry,
    admin_user,
    _setup_websocket_api_for_helpers,
):
    assert await async_setup_component(hass, "input_boolean", {})
    one = await helper_manager.create_helper(
        hass, admin_user, "input_boolean", {"name": "One"}
    )
    two = await helper_manager.create_helper(
        hass, admin_user, "input_boolean", {"name": "Two"}
    )
    before = json.dumps({"version": 1, "data": {"items": [one, two]}})
    mirror_write = AsyncMock(
        return_value=MirrorResult(mirrored=True, commits=("after",))
    )

    with (
        patch(
            "custom_components.ha_dev_tools.llm_api._read_storage_file",
            AsyncMock(return_value=before),
        ),
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.is_mirror_enabled",
            return_value=True,
        ),
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.mirror_write", mirror_write
        ),
    ):
        await DeleteHelperTool()._write(
            hass,
            llm.ToolInput(
                tool_name="delete_helper",
                tool_args={
                    "domain": "input_boolean",
                    "item_ids": [one["id"], two["id"]],
                },
            ),
            _llm_context(admin_user.id),
        )

    mirror_write.assert_awaited_once()
    after = json.loads(mirror_write.await_args.kwargs["content_after"])
    assert after["data"]["items"] == []


@pytest.mark.asyncio
async def test_delete_derived_sensor_tool_batch(hass: HomeAssistant):
    def get(hass, entry_id):
        if entry_id == "unknown":
            raise DerivedSensorNotFoundError(entry_id)
        return {"entry_id": entry_id, "domain": "min_max"}

    mirror_write = AsyncMock(
        return_value=MirrorResult(mirrored=True, commits=("after",))
    )
    with (
        patch(
            "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.get_derived_sensor",
            side_effect=get,
        ),
        patch(
            "custom_components.ha_dev_tools.llm_api.derived_sensor_manager.delete_derived_sensor",
            AsyncMock(
                side_effect=lambda hass, entry_id: {
                    "deleted": True,
                    "entry_id": entry_id,
                }
            ),
        ) as mock_delete,
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.is_mirror_enabled",
            return_value=True,
        ),
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.mirror_write", mirror_write
        ),
    ):
        tool = DeleteDerivedSensorTool()
        refused = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="delete_derived_sensor",
                tool_args={"entry_ids": ["a", "unknown"]},
            ),
            _llm_context(),
        )
        assert refused["error_type"] == "DerivedSensorNotFoundError"
        mock_delete.assert_not_called()

        deleted = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="delete_derived_sensor", tool_args={"entry_ids": ["a", "b"]}
            ),
            _llm_context(),
        )

    assert deleted["deleted"] == ["a", "b"]
    assert [c.kwargs["path"] for c in mirror_write.await_args_list] == [
        "derived_sensors/min_max/a.json",
        "derived_sensors/min_max/b.json",
    ]
    assert len(deleted["mirror"]) == 2


@pytest.mark.asyncio
async def test_delete_template_entity_tool_batch(
    hass: HomeAssistant, template_yaml_manager, tmp_path
):
    package = (
        "template:\n"
        "- sensor:\n"
        "  - name: A\n"
        "    unique_id: a\n"
        "    state: '{{ 1 }}'\n"
        "  - name: Keep\n"
        "    unique_id: keep\n"
        "    state: '{{ 2 }}'\n"
        "- sensor:\n"
        "  - name: B\n"
        "    unique_id: b\n"
        "    state: '{{ 3 }}'\n"
    )
    _write_package(tmp_path, "packages/emhas.yaml", package)
    tool = DeleteTemplateEntityTool(template_yaml_manager)
    mirror_write = AsyncMock(
        return_value=MirrorResult(mirrored=True, commits=("after",))
    )

    def call(args):
        return tool._write(
            hass,
            llm.ToolInput(tool_name="delete_template_entity", tool_args=args),
            _llm_context(),
        )

    refused = await call({"unique_ids": ["a", "nope"]})
    assert refused["error_type"] == "TemplateEntityNotFoundError"
    assert (tmp_path / "packages/emhas.yaml").read_text() == package

    with (
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.is_mirror_enabled",
            return_value=True,
        ),
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.mirror_write", mirror_write
        ),
    ):
        deleted = await call({"unique_ids": ["a", "b"]})

    expected = (
        "template:\n"
        "- sensor:\n"
        "  - name: Keep\n"
        "    unique_id: keep\n"
        "    state: '{{ 2 }}'\n"
    )
    assert deleted["deleted"] == ["a", "b"]
    assert deleted["files"] == ["packages/emhas.yaml"]
    assert (tmp_path / "packages/emhas.yaml").read_text() == expected
    # One mirror commit for the file: first delete's before, last delete's after.
    mirror_write.assert_awaited_once()
    assert mirror_write.await_args.kwargs["content_before"] == package
    assert mirror_write.await_args.kwargs["content_after"] == expected
    assert (
        await tool._dry_run_mirror(
            hass,
            llm.ToolInput(
                tool_name="delete_template_entity", tool_args={"unique_ids": ["keep"]}
            ),
            _llm_context(),
        )
    ).mirrored is False


# --- helper tools: area and person domains (issue #117) ---------------------


@pytest.mark.asyncio
async def test_helper_tools_mirror_areas_as_the_area_list(
    hass: HomeAssistant,
    setup_integration_with_entry,
    admin_user,
    _setup_websocket_api_for_helpers,
):
    """.storage/core.area_registry is denylisted, so area writes mirror the
    area list itself (areas.json), read before and right after each write."""
    assert await async_setup_component(hass, "config", {})
    mirror_write = AsyncMock(
        return_value=MirrorResult(mirrored=True, commits=("after",))
    )

    async def run(tool, args):
        with (
            patch(
                "custom_components.ha_dev_tools.llm_api.mirror.is_mirror_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.ha_dev_tools.llm_api.mirror.mirror_write",
                mirror_write,
            ),
        ):
            return await tool._write(
                hass,
                llm.ToolInput(tool_name=tool.name, tool_args=args),
                _llm_context(admin_user.id),
            )

    def names(content: str | None) -> list[str]:
        return sorted(area["name"] for area in json.loads(content or "[]"))

    created = await run(
        CreateHelperTool(), {"domain": "area", "config": {"name": "Garage"}}
    )
    call = mirror_write.await_args.kwargs
    assert call["path"] == "areas.json"
    assert names(call["content_before"]) == []
    assert names(call["content_after"]) == ["Garage"]
    assert created["mirror"]["mirrored"] is True

    await run(
        UpdateHelperTool(),
        {"domain": "area", "item_id": created["id"], "config": {"name": "Workshop"}},
    )
    call = mirror_write.await_args.kwargs
    assert (names(call["content_before"]), names(call["content_after"])) == (
        ["Garage"],
        ["Workshop"],
    )

    await run(DeleteHelperTool(), {"domain": "area", "item_ids": [created["id"]]})
    call = mirror_write.await_args.kwargs
    assert (names(call["content_before"]), names(call["content_after"])) == (
        ["Workshop"],
        [],
    )


@pytest.mark.asyncio
async def test_helper_tools_never_mirror_persons(
    hass: HomeAssistant,
    setup_integration_with_entry,
    admin_user,
    _setup_websocket_api_for_helpers,
):
    assert await async_setup_component(hass, "person", {})
    mirror_write = AsyncMock()
    with (
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.is_mirror_enabled",
            return_value=True,
        ),
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.mirror_write", mirror_write
        ),
    ):
        created = await CreateHelperTool()._write(
            hass,
            llm.ToolInput(
                tool_name="create_helper",
                tool_args={"domain": "person", "config": {"name": "Alex"}},
            ),
            _llm_context(admin_user.id),
        )
        updated = await UpdateHelperTool()._write(
            hass,
            llm.ToolInput(
                tool_name="update_helper",
                tool_args={
                    "domain": "person",
                    "item_id": created["id"],
                    "config": {"device_trackers": ["device_tracker.alex_iphone"]},
                },
            ),
            _llm_context(admin_user.id),
        )

    assert updated["device_trackers"] == ["device_tracker.alex_iphone"]
    assert created["mirror"]["mirrored"] is False
    assert "personal data" in updated["mirror"]["reason"]
    mirror_write.assert_not_called()


# --- create_template_entity triggers (issue #125) ---------------------------


def test_no_tool_parameter_is_an_untyped_list():
    """The root cause of #125: a bare `list` parameter reaches MCP clients
    as an array of strings, so structured items arrive JSON-encoded. Every
    list parameter of every tool must say what its items are."""
    try:  # HA 2026.9+ converts with probatio; older HA with voluptuous_openapi
        from probatio import to_openapi as convert
    except ImportError:
        from voluptuous_openapi import convert

    from custom_components.ha_dev_tools import llm_api as module

    def untyped(validator, path):
        if validator is list:
            yield path
        elif isinstance(validator, dict):
            for key, sub in validator.items():
                yield from untyped(sub, f"{path}.{getattr(key, 'schema', key)}")
        elif isinstance(validator, list) and validator:
            yield from untyped(validator[0], f"{path}[]")
        elif isinstance(validator, vol.Schema):
            yield from untyped(validator.schema, path)
        elif isinstance(validator, vol.All):
            for sub in validator.validators:
                yield from untyped(sub, path)

    tools = [
        cls
        for cls in vars(module).values()
        if isinstance(cls, type)
        and issubclass(cls, llm.Tool)
        and cls.__module__ == module.__name__
        and isinstance(getattr(cls, "name", None), str)
        and isinstance(getattr(cls, "parameters", None), vol.Schema)
    ]
    assert len(tools) > 40
    assert [path for cls in tools for path in untyped(cls.parameters, cls.name)] == []
    # ... and the ones #125 fixed are advertised as arrays of objects.
    for cls, keys in (
        (CreateTemplateEntityTool, ["triggers"]),
        (
            module.WriteEnergyConfigTool,
            ["energy_sources", "device_consumption", "device_consumption_water"],
        ),
    ):
        properties = convert(cls.parameters)["properties"]
        for key in keys:
            assert properties[key]["items"]["type"] == "object", key


@pytest.mark.asyncio
async def test_create_template_entity_refuses_invalid_triggers_before_writing(
    hass: HomeAssistant, setup_integration_with_entry, template_yaml_manager, tmp_path
):
    _write_package(tmp_path, "packages/energy.yaml", "template: []\n")
    tool = CreateTemplateEntityTool(template_yaml_manager)

    for triggers, expected in (
        # "expected a dictionary" / "expected a mapping", by HA version
        (['{"trigger": "state", "entity_id": "sensor.x"}'], "expected a "),
        ([{"trigger": "nope"}], "Invalid trigger 'nope'"),
        ([{"entity_id": "sensor.x"}], "required key not provided"),
    ):
        args = {
            "platform": "sensor",
            "config": {"name": "PV", "unique_id": "pv", "state": "{{ 1 }}"},
            "package": "energy.yaml",
            "triggers": triggers,
        }
        preview = await tool._preview_context(
            hass,
            llm.ToolInput(tool_name="create_template_entity", tool_args=args),
            _llm_context(),
        )
        assert expected in preview["problems"]
        result = await tool._write(
            hass,
            llm.ToolInput(tool_name="create_template_entity", tool_args=args),
            _llm_context(),
        )
        assert result["error_type"] == "ValueError"
        assert "nothing was written" in result["error"]
        assert (tmp_path / "packages/energy.yaml").read_text() == "template: []\n"


@pytest.mark.asyncio
async def test_create_template_entity_writes_trigger_objects_as_given(
    hass: HomeAssistant, setup_integration_with_entry, template_yaml_manager, tmp_path
):
    _write_package(tmp_path, "packages/energy.yaml", "template: []\n")
    tool = CreateTemplateEntityTool(template_yaml_manager)
    args = {
        "platform": "sensor",
        "config": {"name": "PV", "unique_id": "pv", "state": "{{ 1 }}"},
        "package": "energy.yaml",
        "triggers": [
            {"trigger": "state", "entity_id": "sensor.deye_total_pv"},
            {"trigger": "homeassistant", "event": "start"},
        ],
    }
    assert (
        await tool._preview_context(
            hass,
            llm.ToolInput(tool_name="create_template_entity", tool_args=args),
            _llm_context(),
        )
        == {}
    )
    result = await tool._write(
        hass,
        llm.ToolInput(tool_name="create_template_entity", tool_args=args),
        _llm_context(),
    )

    written = yaml.safe_load((tmp_path / "packages/energy.yaml").read_text())
    assert written["template"][0]["triggers"] == args["triggers"]
    # The reload is mocked here, so the entity never comes up: flagged.
    assert "didn't come up" in result["warning"]
    assert "entity_id" not in result


@pytest.mark.asyncio
async def test_create_template_entity_reports_the_entity_that_came_up(
    hass: HomeAssistant, setup_integration_with_entry, template_yaml_manager, tmp_path
):
    _write_package(tmp_path, "packages/energy.yaml", "template: []\n")
    er.async_get(hass).async_get_or_create(
        "sensor", "template", "pv", suggested_object_id="pv"
    )
    hass.states.async_set("sensor.pv", "1")
    result = await CreateTemplateEntityTool(template_yaml_manager)._write(
        hass,
        llm.ToolInput(
            tool_name="create_template_entity",
            tool_args={
                "platform": "sensor",
                "config": {"name": "PV", "unique_id": "pv", "state": "{{ 1 }}"},
                "package": "energy.yaml",
            },
        ),
        _llm_context(),
    )
    assert result["entity_id"] == "sensor.pv"
    assert "warning" not in result


@pytest.mark.asyncio
async def test_update_template_entity_reports_whether_the_entity_came_back(
    hass: HomeAssistant, setup_integration_with_entry, template_yaml_manager, tmp_path
):
    """An edit HA rejects on reload drops the entity as silently as a bad
    create did (#125). In a block with its own unique_id, the registry id is
    "<block>-<entity>" - the check must not warn falsely there."""
    _write_package(
        tmp_path,
        "packages/energy.yaml",
        "template:\n"
        "  - unique_id: energy\n"
        "    triggers:\n"
        "      - trigger: homeassistant\n"
        "        event: start\n"
        "    sensor:\n"
        "      - name: PV\n"
        "        unique_id: pv\n"
        '        state: "{{ 1 }}"\n',
    )
    tool = UpdateTemplateEntityTool(template_yaml_manager)
    args = {"unique_id": "pv", "config": {"name": "PV", "state": "{{ 2 }}"}}

    missing = await tool._write(
        hass,
        llm.ToolInput(tool_name="update_template_entity", tool_args=args),
        _llm_context(),
    )
    assert "didn't come up" in missing["warning"]

    er.async_get(hass).async_get_or_create(
        "sensor", "template", "energy-pv", suggested_object_id="pv"
    )
    hass.states.async_set("sensor.pv", "2")
    loaded = await tool._write(
        hass,
        llm.ToolInput(tool_name="update_template_entity", tool_args=args),
        _llm_context(),
    )
    assert loaded["entity_id"] == "sensor.pv"
    assert "warning" not in loaded


def test_block_unique_id_reads_the_written_block():
    from custom_components.ha_dev_tools.template_yaml_manager import (
        TemplateYamlManager,
    )

    read = TemplateYamlManager.block_unique_id
    assert read("template:\n  - unique_id: a\n    sensor: []\n", 0) == "a"
    assert read("template:\n  unique_id: 7\n  sensor: []\n", 0) == "7"
    assert read("template:\n  - sensor: []\n", 0) is None
    assert read("template:\n  - sensor: []\n", 3) is None
    assert read("template:\n  - just a string\n", 0) is None
    assert read("sensor: []\n", 0) is None
    assert read("", 0) is None


@pytest.mark.asyncio
async def test_template_entity_status_without_a_reload(
    hass: HomeAssistant, template_yaml_manager
):
    """No reload ran (e.g. the template integration isn't loaded): no
    warning - the result's reloaded: false already says it."""
    from types import SimpleNamespace

    from custom_components.ha_dev_tools.llm_api import _template_entity_status

    result = SimpleNamespace(
        content_after="template:\n  - sensor: []\n",
        location=SimpleNamespace(block_index=0, platform="sensor"),
        reloaded=False,
    )
    assert await _template_entity_status(hass, template_yaml_manager, result, "x") == {}


# --- atomic batch template delete (issue #127) ------------------------------

_ENERGY_MODEL = (
    "# energy model\n"
    "template:\n"
    "  - triggers:\n"
    "      - trigger: homeassistant\n"
    "        event: start\n"
    "    sensor:\n"
    + "".join(
        f"      - name: E{i}\n        unique_id: e{i}\n        state: '{{{{ {i} }}}}'\n"
        for i in range(7)
    )
    + "  - sensor:\n"
    "      - name: Keep\n"
    "        unique_id: keep\n"
    "        state: '{{ 1 }}'\n"
    "    binary_sensor:\n"
    "      - name: Gone\n"
    "        unique_id: gone\n"
    "        state: '{{ true }}'\n"
    "  - sensor:\n"
    "      - name: Solo\n"
    "        unique_id: solo\n"
    "        state: '{{ 2 }}'\n"
)
_BATCH = [f"e{i}" for i in range(7)] + ["gone", "solo"]


def _count_reloads(hass: HomeAssistant) -> AsyncMock:
    reload = AsyncMock()
    hass.services.async_register("template", "reload", reload)
    return reload


@pytest.mark.asyncio
async def test_batch_delete_writes_once_and_reloads_once(
    hass: HomeAssistant, template_yaml_manager, tmp_path
):
    """Ten ids used to mean ten writes and ten full template reloads -
    slow enough to time out partway (#127). Now one of each, with the same
    result as deleting them one by one."""
    path = tmp_path / "packages/energy_model.yaml"
    _write_package(tmp_path, "packages/energy_model.yaml", _ENERGY_MODEL)
    for unique_id in _BATCH:  # the old way, for comparison
        await template_yaml_manager.delete_entity(unique_id)
    expected = path.read_text()
    path.write_text(_ENERGY_MODEL)
    reload = _count_reloads(hass)
    write = AsyncMock(wraps=template_yaml_manager.file_manager.write_file)

    with patch.object(template_yaml_manager.file_manager, "write_file", write):
        results = await template_yaml_manager.delete_entities(_BATCH)

    assert write.await_count == 1
    assert reload.await_count == 1
    (result,) = results
    assert result.file_path == "packages/energy_model.yaml"
    assert result.unique_ids == _BATCH
    assert result.reloaded is True
    after = path.read_text()
    assert after == expected == result.content_after
    assert "unique_id: keep" in after and "# energy model" in after


@pytest.mark.asyncio
async def test_batch_delete_dry_run_writes_nothing(
    hass: HomeAssistant, template_yaml_manager, tmp_path
):
    _write_package(tmp_path, "packages/energy_model.yaml", _ENERGY_MODEL)
    reload = _count_reloads(hass)
    (result,) = await template_yaml_manager.delete_entities(_BATCH, dry_run=True)
    assert "unique_id: e0" not in result.content_after
    assert (tmp_path / "packages/energy_model.yaml").read_text() == _ENERGY_MODEL
    assert reload.await_count == 0


@pytest.mark.asyncio
async def test_batch_delete_tool_survives_a_cancelled_request(
    hass: HomeAssistant, template_yaml_manager, tmp_path
):
    """The #127 scenario: the client times out mid-call. The commit phase -
    write, reload, mirror - still runs to the end, so the change is whole
    and mirrored instead of half-applied and missing from git."""
    _write_package(tmp_path, "packages/energy_model.yaml", _ENERGY_MODEL)
    reload_started = asyncio.Event()
    release_reload = asyncio.Event()

    async def slow_reload(call):
        reload_started.set()
        await release_reload.wait()

    hass.services.async_register("template", "reload", slow_reload)
    mirror_write = AsyncMock(
        return_value=MirrorResult(mirrored=True, commits=("after",))
    )
    with (
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.is_mirror_enabled",
            return_value=True,
        ),
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.mirror_write", mirror_write
        ),
    ):
        call = hass.async_create_task(
            DeleteTemplateEntityTool(template_yaml_manager)._write(
                hass,
                llm.ToolInput(
                    tool_name="delete_template_entity",
                    tool_args={"unique_ids": _BATCH},
                ),
                _llm_context(),
            )
        )
        await reload_started.wait()
        call.cancel()  # the client gave up
        release_reload.set()
        await hass.async_block_till_done()

    assert call.cancelled()
    after = (tmp_path / "packages/energy_model.yaml").read_text()
    assert not any(f"unique_id: {i}\n" in after for i in _BATCH)
    mirror_write.assert_awaited_once()
    assert mirror_write.await_args.kwargs["content_before"] == _ENERGY_MODEL
    assert mirror_write.await_args.kwargs["content_after"] == after


@pytest.mark.asyncio
async def test_batch_delete_tool_reports_and_mirrors_a_partial_failure(
    hass: HomeAssistant, template_yaml_manager, tmp_path
):
    """Across two files, the second write fails: the first file's change is
    reported, reloaded and mirrored; the rest are listed as not deleted."""
    _write_package(tmp_path, "packages/a.yaml", _ENERGY_MODEL)
    _write_package(
        tmp_path,
        "packages/b.yaml",
        "template:\n  - sensor:\n      - name: B\n        unique_id: b\n"
        "        state: '{{ 1 }}'\n",
    )
    reload = _count_reloads(hass)
    real_write = template_yaml_manager.file_manager.write_file

    async def write(path, content, **kwargs):
        if path == "packages/b.yaml":
            raise ValueError("disk full")
        return await real_write(path, content, **kwargs)

    mirror_write = AsyncMock(
        return_value=MirrorResult(mirrored=True, commits=("after",))
    )
    with (
        patch.object(template_yaml_manager.file_manager, "write_file", write),
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.is_mirror_enabled",
            return_value=True,
        ),
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.mirror_write", mirror_write
        ),
    ):
        result = await DeleteTemplateEntityTool(template_yaml_manager)._write(
            hass,
            llm.ToolInput(
                tool_name="delete_template_entity",
                tool_args={"unique_ids": ["e0", "b", "e1"]},
            ),
            _llm_context(),
        )

    assert result["deleted"] == ["e0", "e1"]
    assert result["not_deleted"] == ["b"]
    assert "packages/b.yaml: disk full" in result["error"]
    assert result["files"] == ["packages/a.yaml"]
    assert reload.await_count == 1
    assert [c.kwargs["path"] for c in mirror_write.await_args_list] == [
        "packages/a.yaml"
    ]
    assert "unique_id: b\n" in (tmp_path / "packages/b.yaml").read_text()


@pytest.mark.asyncio
async def test_batch_delete_tool_nothing_written_when_first_write_fails(
    hass: HomeAssistant, template_yaml_manager, tmp_path
):
    _write_package(tmp_path, "packages/a.yaml", _ENERGY_MODEL)
    reload = _count_reloads(hass)
    with patch.object(
        template_yaml_manager.file_manager,
        "write_file",
        AsyncMock(side_effect=ValueError("read-only")),
    ):
        result = await DeleteTemplateEntityTool(template_yaml_manager)._write(
            hass,
            llm.ToolInput(
                tool_name="delete_template_entity",
                tool_args={"unique_ids": ["e0", "e1"]},
            ),
            _llm_context(),
        )
    assert result["deleted"] == []
    assert result["reloaded"] is False
    assert result["not_deleted"] == ["e0", "e1"]
    assert reload.await_count == 0


def test_mirror_payload_flags_drift():
    payload = _mirror_result_payload(
        MirrorResult(mirrored=True, commits=("before", "after"))
    )
    assert "changed since its last mirrored copy" in payload["drift"]
    assert "drift" not in _mirror_result_payload(
        MirrorResult(mirrored=True, commits=("after",))
    )


@pytest.mark.asyncio
async def test_template_delete_reports_an_entity_gone_since_it_was_resolved(
    hass: HomeAssistant, template_yaml_manager, tmp_path
):
    """Resolved up front, then gone by the time of the delete (the file was
    edited in between): reported as an error, not raised."""
    from custom_components.ha_dev_tools.template_yaml_manager import (
        TemplateEntityNotFoundError,
    )

    _write_package(tmp_path, "packages/a.yaml", _ENERGY_MODEL)
    tool = DeleteTemplateEntityTool(template_yaml_manager)
    gone = AsyncMock(side_effect=TemplateEntityNotFoundError("e0 is gone"))
    for method, args in (
        ("delete_entity", {"unique_id": "e0"}),
        ("delete_entities", {"unique_ids": ["e0", "e1"]}),
    ):
        with patch.object(template_yaml_manager, method, gone):
            result = await tool._write(
                hass,
                llm.ToolInput(tool_name="delete_template_entity", tool_args=args),
                _llm_context(),
            )
        assert result["error_type"] == "TemplateEntityNotFoundError"

    document = yaml.safe_load(_ENERGY_MODEL)
    with pytest.raises(TemplateEntityNotFoundError):
        template_yaml_manager._locate(document, "packages/a.yaml", "nope")
