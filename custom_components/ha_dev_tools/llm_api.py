"""LLM API for the Home Assistant Development Tools integration.

Registers a `dev_tools` API into Home Assistant's LLM tool registry
(`homeassistant.helpers.llm`). Home Assistant's own native `mcp_server`
integration exposes whatever is registered here over MCP (Streamable HTTP)
with no custom transport or auth code required on our side — see
docs/RESTART_PLAN.md for the full architecture.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, override

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from .const import DOMAIN

API_ID = "dev_tools"
API_NAME = "HA Dev Tools"
API_PROMPT = (
    "Tools for developing and maintaining this Home Assistant instance: "
    "authoring and validating automations/scripts, reading logs, and "
    "auditing configuration."
)


class DevToolsPingTool(llm.Tool):
    """Confirm the dev_tools API is registered and reachable.

    Not part of the real tool surface (see docs/RESTART_PLAN.md's "Concrete
    tool list") — this exists solely to prove the llm.API/mcp_server wiring
    end-to-end before real tools are built on top of it.
    """

    name = "dev_tools_ping"
    description = (
        "Check that the ha_dev_tools API is registered and reachable. "
        "Returns a static status payload; has no side effects."
    )
    parameters = vol.Schema({})

    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Return a static status payload."""
        return {"status": "ok", "domain": DOMAIN}


@dataclass(slots=True, kw_only=True)
class DevToolsAPI(llm.API):
    """The ha_dev_tools LLM API.

    Backing services (FileManager/LogManager/SecurityManager, etc.) will be
    added as fields here as real tools are built, following the same pattern
    Home Assistant's own `mcp` client component uses for its coordinator.
    """

    @override
    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        """Return the instance of the API."""
        return llm.APIInstance(
            self,
            API_PROMPT,
            llm_context,
            tools=[DevToolsPingTool()],
        )


def async_register(hass: HomeAssistant) -> Any:
    """Register the dev_tools API and return its unsubscribe callable."""
    return llm.async_register_api(
        hass,
        DevToolsAPI(hass=hass, id=API_ID, name=API_NAME),
    )
