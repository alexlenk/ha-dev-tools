"""Tests for the dev_tools LLM API registration (llm_api.py).

Verifies the foundation of the redesigned architecture: this integration
registers into Home Assistant's own `homeassistant.helpers.llm` tool
registry, which HA's native `mcp_server` integration serves over MCP with
no custom transport code of our own. See docs/RESTART_PLAN.md.

NOTE on version skew: the real minimum supported HA version is 2026.8.2
(when `mcp_server` shipped - see hacs.json), but this sandbox's package
mirror is capped at 2025.1.4, over a year behind. `llm.LLMContext` gained/
lost fields and `llm.async_register_api`'s return value changed between
those versions, so this file builds LLMContext dynamically from whatever
fields the installed version actually has, and treats the unsub-callable
behavior as best-effort rather than asserting it unconditionally. Full
verification of the unsub path (and of `mcp_server` itself) happens in
CI against a current homeassistant release, not in this sandbox.
"""
import inspect

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from custom_components.ha_dev_tools.llm_api import API_ID, DOMAIN


def _llm_context() -> llm.LLMContext:
    """Build an LLMContext for calling the API, tolerant of field changes across HA versions."""
    fields = {
        "platform": DOMAIN,
        "context": None,
        "user_prompt": None,
        "language": "en",
        "assistant": "test",
        "device_id": None,
    }
    accepted = set(inspect.signature(llm.LLMContext.__init__).parameters)
    return llm.LLMContext(**{k: v for k, v in fields.items() if k in accepted})


@pytest.mark.asyncio
async def test_dev_tools_api_registered(hass: HomeAssistant, setup_integration_with_entry):
    """The dev_tools API is registered in HA's LLM API registry after setup."""
    api_ids = {api.id for api in llm.async_get_apis(hass)}
    assert API_ID in api_ids


@pytest.mark.asyncio
async def test_dev_tools_ping_tool_reachable(hass: HomeAssistant, setup_integration_with_entry):
    """The ping tool is exposed and callable through the registered API instance."""
    api_instance = await llm.async_get_api(hass, API_ID, _llm_context())

    tool_names = {tool.name for tool in api_instance.tools}
    assert "dev_tools_ping" in tool_names

    result = await api_instance.async_call_tool(
        llm.ToolInput(tool_name="dev_tools_ping", tool_args={})
    )
    assert result == {"status": "ok", "domain": DOMAIN}


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
