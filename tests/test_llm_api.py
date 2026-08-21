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

import inspect
import time
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools import access_control
from custom_components.ha_dev_tools.access_control import NotAdminError, NotArmedError
from custom_components.ha_dev_tools.const import OPT_DRY_RUN
from custom_components.ha_dev_tools.history_manager import RecorderNotAvailableError
from custom_components.ha_dev_tools.llm_api import (
    API_ID,
    DOMAIN,
    DevToolsPingTool,
    FindEntitiesTool,
    GetEntityHistoryTool,
    GetLogbookTool,
    WriteGatedTool,
)


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
        "list_helpers",
        "create_helper",
        "update_helper",
        "delete_helper",
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


# --- WriteGatedTool / dry-run ------------------------------------------------


class _StubWriteTool(WriteGatedTool):
    """Minimal WriteGatedTool subclass so these tests exercise only the
    dry-run gating itself, decoupled from any specific manager's setup."""

    name = "stub_write"
    description = "stub"
    parameters = vol.Schema({})

    def __init__(self) -> None:
        self.write_called = False

    async def _write(self, hass, tool_input, llm_context):
        self.write_called = True
        return {"wrote": True}


@pytest.mark.asyncio
async def test_write_gated_tool_performs_write_when_dry_run_disabled(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    _arm(hass)
    tool = _StubWriteTool()

    result = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="stub_write", tool_args={"foo": "bar"}),
        _llm_context(admin_user.id),
    )

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

    result = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="stub_write", tool_args={"foo": "bar"}),
        _llm_context(admin_user.id),
    )

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
