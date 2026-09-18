"""Tests for the integration's config-entry setup/unload/reload (__init__.py).

Setup and unload are already exercised indirectly through
setup_integration_with_entry (conftest.py) and test_llm_api.py's unload
test - this covers the one path neither touches: async_reload_entry,
which is just unload-then-setup but wired up as HA's own reload hook.
"""

from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools import async_reload_entry
from custom_components.ha_dev_tools.const import DOMAIN


async def test_reload_entry_unloads_then_sets_up_again(
    hass: HomeAssistant, setup_integration_with_entry
):
    """A reload must leave the integration in a working setup state, not a
    torn-down one - the same hass.data population async_setup_entry does
    on a first run."""
    await async_reload_entry(hass, setup_integration_with_entry)

    assert DOMAIN in hass.data
    assert "security_manager" in hass.data[DOMAIN]
