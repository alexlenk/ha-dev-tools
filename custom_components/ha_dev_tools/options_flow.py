"""Options flow: the dry-run toggle."""

from __future__ import annotations

from typing import Any

try:
    # HA 2026.9+ validates with probatio and types its schemas (llm.Tool's
    # parameters, a flow's data_schema) as probatio's - the voluptuous it
    # installs is a shim handing out the same objects (issue #138).
    import probatio as vol
except ImportError:  # pragma: no cover - HA before 2026.9
    import voluptuous as vol  # type: ignore[no-redef]
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.helpers import selector

from .const import OPT_DRY_RUN, OPT_MIRROR_ENABLED, OPT_MIRROR_REPO, OPT_MIRROR_TOKEN


class HADevToolsOptionsFlow(OptionsFlow):
    """Manage the dry-run toggle and git-mirroring settings after setup."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Accept config_entry only because async_get_options_flow passes
        it - self.config_entry is a read-only property the flow framework
        populates itself; it must not be assigned here. This API has
        changed across Home Assistant releases (see ha-concierge-mcp's
        options_flow.py for the same note in more detail)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Toggle dry-run mode and configure git mirroring."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        OPT_DRY_RUN, default=options.get(OPT_DRY_RUN, False)
                    ): bool,
                    vol.Required(
                        OPT_MIRROR_ENABLED,
                        default=options.get(OPT_MIRROR_ENABLED, False),
                    ): bool,
                    vol.Optional(
                        OPT_MIRROR_REPO, default=options.get(OPT_MIRROR_REPO, "")
                    ): str,
                    vol.Optional(
                        OPT_MIRROR_TOKEN, default=options.get(OPT_MIRROR_TOKEN, "")
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
        )
