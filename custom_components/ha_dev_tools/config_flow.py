"""Config flow for HA Dev Tools.

No user input needed at setup time - the security path allowlist has sane
defaults (see const.py), and there's no configuration.yaml import path (see
CHANGELOG's Removed entry for why that was dropped rather than fixed).
"""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN
from .options_flow import HADevToolsOptionsFlow


# mypy doesn't know HA's ConfigFlow.__init_subclass__ accepts `domain=` -
# that needs Home Assistant's own mypy plugin, not available to custom
# integrations; this exact pattern is standard across the entire HA
# ecosystem, not a real error.
class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle a config flow for HA Dev Tools."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> HADevToolsOptionsFlow:
        """Get the options flow (currently just the dry-run toggle)."""
        return HADevToolsOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Handle the initial (and only) step."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(title="HA Dev Tools", data={})

        return self.async_show_form(step_id="user")
