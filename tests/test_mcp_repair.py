"""Tests for the mcp_server-exposure repair issue (mcp_repair.py).

Covers the check that flags Settings -> System -> Repairs when Home
Assistant's own mcp_server integration isn't loaded, or is loaded but not
configured to expose the dev_tools API - see mcp_repair.py's module
docstring for why this exists and why it's a repair issue rather than a
config-flow blocker.
"""

from homeassistant.config_entries import ConfigEntryChange, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_dev_tools import mcp_repair
from custom_components.ha_dev_tools.const import DOMAIN


def _issue(hass: HomeAssistant):
    return ir.async_get(hass).async_get_issue(DOMAIN, mcp_repair.ISSUE_ID)


def _add_mcp_server_entry(
    hass: HomeAssistant, *, exposed_apis: list[str], loaded: bool = True
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=mcp_repair.MCP_SERVER_DOMAIN,
        data={mcp_repair.CONF_LLM_HASS_API: exposed_apis},
        state=ConfigEntryState.LOADED if loaded else ConfigEntryState.NOT_LOADED,
    )
    entry.add_to_hass(hass)
    return entry


def test_issue_created_when_mcp_server_missing(hass: HomeAssistant):
    unsub = mcp_repair.async_setup_repair(hass)

    assert _issue(hass) is not None
    unsub()


def test_no_issue_when_mcp_server_exposes_dev_tools(hass: HomeAssistant):
    _add_mcp_server_entry(hass, exposed_apis=["assist", DOMAIN])

    unsub = mcp_repair.async_setup_repair(hass)

    assert _issue(hass) is None
    unsub()


def test_issue_when_mcp_server_loaded_but_not_exposing_dev_tools(hass: HomeAssistant):
    _add_mcp_server_entry(hass, exposed_apis=["assist"])

    unsub = mcp_repair.async_setup_repair(hass)

    assert _issue(hass) is not None
    unsub()


def test_issue_when_mcp_server_entry_not_loaded(hass: HomeAssistant):
    """A config entry can list dev_tools in its data without actually
    being loaded (e.g. mcp_server failed to start) - only a LOADED entry
    is actually serving anything."""
    _add_mcp_server_entry(hass, exposed_apis=[DOMAIN], loaded=False)

    unsub = mcp_repair.async_setup_repair(hass)

    assert _issue(hass) is not None
    unsub()


def test_issue_clears_when_mcp_server_added_after_setup(hass: HomeAssistant):
    unsub = mcp_repair.async_setup_repair(hass)
    assert _issue(hass) is not None

    entry = _add_mcp_server_entry(hass, exposed_apis=[DOMAIN])
    async_dispatcher_send(
        hass, mcp_repair.SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntryChange.ADDED, entry
    )

    assert _issue(hass) is None
    unsub()


def test_issue_reappears_when_mcp_server_reconfigured_away_from_dev_tools(
    hass: HomeAssistant,
):
    entry = _add_mcp_server_entry(hass, exposed_apis=[DOMAIN])
    unsub = mcp_repair.async_setup_repair(hass)
    assert _issue(hass) is None

    hass.config_entries.async_update_entry(
        entry, data={mcp_repair.CONF_LLM_HASS_API: ["assist"]}
    )
    async_dispatcher_send(
        hass, mcp_repair.SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntryChange.UPDATED, entry
    )

    assert _issue(hass) is not None
    unsub()


def test_unsub_removes_issue(hass: HomeAssistant):
    unsub = mcp_repair.async_setup_repair(hass)
    assert _issue(hass) is not None

    unsub()

    assert _issue(hass) is None
