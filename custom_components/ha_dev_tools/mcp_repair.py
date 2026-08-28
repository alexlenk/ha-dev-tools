"""Repair issue for a silently-unreachable dev_tools MCP endpoint.

Why this exists: ha_dev_tools only registers into Home Assistant's internal
LLM API registry (see __init__.py) - it has no HTTP transport of its own,
so it loads cleanly with zero log errors even when nothing outside Home
Assistant can actually reach it. That combination - a visible, healthy-
looking integration plus a fully silent failure - is exactly what happens
when Home Assistant's own `mcp_server` integration isn't installed, isn't
loaded, or isn't configured to expose the `dev_tools` API: an MCP client
gets a bare 404 from `/api/mcp/dev_tools`, and there is nothing in
ha_dev_tools' own logs to point at why.

This can't be enforced at config-flow time - the documented setup order
(README's Setup section) is ha_dev_tools first, then mcp_server second, so
refusing to install ha_dev_tools until mcp_server already exists would
contradict the instructions telling people to do it in that order. A
self-clearing repair issue (Settings -> System -> Repairs) fits instead:
it appears when the check fails and disappears on its own once mcp_server
is added and configured to expose `dev_tools`, no extra step required.
"""

from __future__ import annotations

import logging
from typing import Callable

from homeassistant.config_entries import ConfigEntryState, SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_ID = "mcp_server_not_exposing_dev_tools"
MCP_SERVER_DOMAIN = "mcp_server"
CONF_LLM_HASS_API = "llm_hass_api"


def _dev_tools_exposed(hass: HomeAssistant) -> bool:
    """True if a loaded mcp_server config entry exposes the dev_tools API."""
    for entry in hass.config_entries.async_entries(MCP_SERVER_DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        if DOMAIN in entry.data.get(CONF_LLM_HASS_API, []):
            return True
    return False


@callback
def _async_refresh_issue(hass: HomeAssistant) -> None:
    if _dev_tools_exposed(hass):
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ID)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_ID,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_ID,
        learn_more_url="https://github.com/alexlenk/ha-dev-tools#connecting-an-mcp-client",
    )


def async_setup_repair(hass: HomeAssistant) -> Callable[[], None]:
    """Create/clear the repair issue now, and again on every config entry
    change - mcp_server's own config entry can be added, removed, or have
    its exposed APIs edited independently of ha_dev_tools' lifecycle at any
    time, so a one-shot check at setup isn't enough.
    """
    _async_refresh_issue(hass)

    @callback
    def _on_entry_changed(*_args: object) -> None:
        _async_refresh_issue(hass)

    unsub_dispatcher = async_dispatcher_connect(
        hass, SIGNAL_CONFIG_ENTRY_CHANGED, _on_entry_changed
    )

    @callback
    def _unsub() -> None:
        unsub_dispatcher()
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ID)

    return _unsub
