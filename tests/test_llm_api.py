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

import base64
import inspect
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools import access_control, helper_manager
from custom_components.ha_dev_tools.access_control import NotAdminError, NotArmedError
from custom_components.ha_dev_tools.automation_manager import AutomationManager
from custom_components.ha_dev_tools.script_manager import ScriptManager
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
    CreateDerivedSensorTool,
    CreateHelperTool,
    CreateTemplateEntityTool,
    DeleteDerivedSensorTool,
    DeleteHelperTool,
    DeleteTemplateEntityTool,
    DevToolsPingTool,
    FindEntitiesTool,
    GetAutomationTool,
    GetDerivedSensorTool,
    GetEntityHistoryTool,
    GetLogbookTool,
    GetScriptTool,
    ListDerivedSensorsTool,
    ListScriptsTool,
    ReloadDerivedSensorTool,
    UpdateDerivedSensorTool,
    UpdateHelperTool,
    UpdateTemplateEntityTool,
    WriteAutomationTool,
    WriteDashboardTool,
    WriteGatedTool,
    WriteScriptTool,
)
from custom_components.ha_dev_tools.security import SecurityManager
from custom_components.ha_dev_tools.template_yaml_manager import TemplateYamlManager


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
    assert result == {"status": "ok", "domain": DOMAIN}


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
        "render_template",
        "validate_template",
        "get_logs",
        "get_entity_history",
        "get_logbook",
        "list_addons",
        "get_addon_logs",
        "check_config",
        "reload_domain",
        "get_automation",
        "write_automation",
        "audit_automations",
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
        "get_dashboard",
        "write_dashboard",
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
    reload_mock.assert_called_once()
    assert "new_script" in (tmp_path / "scripts.yaml").read_text()


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
    assert result == {"wrote": True}


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
    assert first == {"wrote": True}
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
    mock_update.assert_called_once_with(hass, "abc", {"init": {"type": "min"}})


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

    def get(self, url, **kwargs):
        return _FakeMirrorRequestContext(self._responses.pop(0))

    def put(self, url, **kwargs):
        return _FakeMirrorRequestContext(self._responses.pop(0))

    def post(self, url, **kwargs):
        return _FakeMirrorRequestContext(self._responses.pop(0))

    def patch(self, url, **kwargs):
        return _FakeMirrorRequestContext(self._responses.pop(0))


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
    assert result["mirror"]["commits"] == ["after"]
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
