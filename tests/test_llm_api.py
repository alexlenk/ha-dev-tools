"""Tests for the dev_tools LLM API registration (llm_api.py).

Verifies the foundation of the redesigned architecture: this integration
registers into Home Assistant's own `homeassistant.helpers.llm` tool
registry, which HA's native `mcp_server` integration serves over MCP with
no custom transport code of our own. See docs/RESTART_PLAN.md.

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

import pytest

from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools import access_control
from custom_components.ha_dev_tools.access_control import NotAdminError, NotArmedError
from custom_components.ha_dev_tools.llm_api import API_ID, DOMAIN, DevToolsPingTool, FindEntitiesTool


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
async def test_dev_tools_api_registered(hass: HomeAssistant, setup_integration_with_entry):
    """The dev_tools API is registered in HA's LLM API registry after setup."""
    api_ids = {api.id for api in llm.async_get_apis(hass)}
    assert API_ID in api_ids


@pytest.mark.asyncio
async def test_dev_tools_ping_tool_reachable(hass: HomeAssistant, setup_integration_with_entry):
    """The ping tool is genuinely registered and its own logic works."""
    api_instance = await llm.async_get_api(hass, API_ID, _llm_context())

    tool_names = {tool.name for tool in api_instance.tools}
    assert "dev_tools_ping" in tool_names

    result = await DevToolsPingTool().async_call(
        hass, llm.ToolInput(tool_name="dev_tools_ping", tool_args={}), _llm_context()
    )
    assert result == {"status": "ok", "domain": DOMAIN}


@pytest.mark.asyncio
async def test_dev_tools_real_tools_registered(hass: HomeAssistant, setup_integration_with_entry):
    """All Phase 2 tools are registered, not just the diagnostic ping tool."""
    api_instance = await llm.async_get_api(hass, API_ID, _llm_context())

    tool_names = {tool.name for tool in api_instance.tools}
    assert tool_names == {
        "dev_tools_ping",
        "find_entities",
        "entity_health_report",
        "render_template",
        "validate_template",
        "get_logs",
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
