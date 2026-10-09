"""Options flow: the dry-run toggle."""

from __future__ import annotations

import re
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

# GitHub's owner/repo: what the mirror's API URLs are built from.
_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")


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
        options = self.config_entry.options
        errors: dict[str, str] = {}
        if user_input is not None:
            data = dict(user_input)
            # The token field is never pre-filled (a form's defaults are
            # sent to the browser, password field or not), so left empty
            # it keeps the stored token.
            if not data.get(OPT_MIRROR_TOKEN) and options.get(OPT_MIRROR_TOKEN):
                data[OPT_MIRROR_TOKEN] = options[OPT_MIRROR_TOKEN]
            repo = (data.get(OPT_MIRROR_REPO) or "").strip()
            data[OPT_MIRROR_REPO] = repo
            if repo and not _REPO.fullmatch(repo):
                errors[OPT_MIRROR_REPO] = "invalid_repo"
            else:
                return self.async_create_entry(data=data)

        return self.async_show_form(
            step_id="init",
            errors=errors,
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
                    vol.Optional(OPT_MIRROR_TOKEN): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
        )
