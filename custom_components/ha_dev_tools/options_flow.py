"""Options flow: dry-run, the arm window's limits, what control_entities
may never touch, git mirroring (GitHub or GitLab)."""

from __future__ import annotations

import re
from typing import Any

# HA validates with probatio and types its schemas (llm.Tool's parameters,
# a flow's data_schema) as probatio's - the voluptuous it installs is a
# shim handing out the same objects (issue #138).
import probatio as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.helpers import selector

from .const import (
    ARM_IDLE_MINUTES_DEFAULT,
    ARM_IDLE_MINUTES_RANGE,
    ARM_MAX_MINUTES_DEFAULT,
    ARM_MAX_MINUTES_RANGE,
    GITLAB_DEFAULT_SERVER,
    MIRROR_PROVIDERS,
    OPT_ARM_IDLE_MINUTES,
    OPT_ARM_MAX_MINUTES,
    OPT_CONTROL_EXCLUDED_DEVICES,
    OPT_CONTROL_EXCLUDED_ENTITIES,
    OPT_DRY_RUN,
    OPT_MIRROR_ENABLED,
    OPT_MIRROR_PROVIDER,
    OPT_MIRROR_REPO,
    OPT_MIRROR_SERVER,
    OPT_MIRROR_TOKEN,
    OPT_MIRROR_VERIFY_SSL,
)
from .mirror import refresh_tls_issue
from .service_call_manager import CONTROL_SERVICES


def _minutes(bounds: tuple[int, int]) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=bounds[0],
            max=bounds[1],
            step=1,
            unit_of_measurement="min",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


# Only what control_entities could act on is offered.
_CONTROLLABLE_DOMAINS = sorted(CONTROL_SERVICES)


# GitHub's owner/repo: what the mirror's API URLs are built from.
_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
# A GitLab project path: group/project, subgroups allowed (issue #182).
_PROJECT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)+")
# A GitLab server: https only - the token goes in every request - with an
# optional port and path (GitLab served under a sub-path).
_SERVER = re.compile(r"https://[A-Za-z0-9.-]+(?::\d{1,5})?(?:/[A-Za-z0-9._~-]+)*")


def _mirror_problem(data: dict[str, Any]) -> tuple[str, str] | None:
    """(field, error key) for a mirror repo/server that can't be right."""
    repo = data[OPT_MIRROR_REPO]
    if data.get(OPT_MIRROR_PROVIDER, "github") == "gitlab":
        if not _SERVER.fullmatch(data[OPT_MIRROR_SERVER]):
            return OPT_MIRROR_SERVER, "invalid_server"
        if repo and not _PROJECT.fullmatch(repo):
            return OPT_MIRROR_REPO, "invalid_project"
    elif repo and not _REPO.fullmatch(repo):
        return OPT_MIRROR_REPO, "invalid_repo"
    return None


class HADevToolsOptionsFlow(OptionsFlow):
    """Manage dry-run, the arm window, control exclusions and git
    mirroring after setup."""

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
            data[OPT_MIRROR_REPO] = (data.get(OPT_MIRROR_REPO) or "").strip()
            data[OPT_MIRROR_SERVER] = (
                (data.get(OPT_MIRROR_SERVER) or GITLAB_DEFAULT_SERVER)
                .strip()
                .rstrip("/")
            )
            for key in (OPT_ARM_IDLE_MINUTES, OPT_ARM_MAX_MINUTES):
                if key in data:
                    data[key] = int(data[key])
            if problem := _mirror_problem(data):
                errors[problem[0]] = problem[1]
            elif data.get(OPT_ARM_IDLE_MINUTES, ARM_IDLE_MINUTES_DEFAULT) > data.get(
                OPT_ARM_MAX_MINUTES, ARM_MAX_MINUTES_DEFAULT
            ):
                errors[OPT_ARM_IDLE_MINUTES] = "idle_above_cap"
            else:
                refresh_tls_issue(self.hass, data)
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
                        OPT_ARM_IDLE_MINUTES,
                        default=options.get(
                            OPT_ARM_IDLE_MINUTES, ARM_IDLE_MINUTES_DEFAULT
                        ),
                    ): _minutes(ARM_IDLE_MINUTES_RANGE),
                    vol.Required(
                        OPT_ARM_MAX_MINUTES,
                        default=options.get(
                            OPT_ARM_MAX_MINUTES, ARM_MAX_MINUTES_DEFAULT
                        ),
                    ): _minutes(ARM_MAX_MINUTES_RANGE),
                    vol.Optional(
                        OPT_CONTROL_EXCLUDED_DEVICES,
                        default=options.get(OPT_CONTROL_EXCLUDED_DEVICES, []),
                    ): selector.DeviceSelector(
                        selector.DeviceSelectorConfig(
                            entity=selector.EntityFilterSelectorConfig(
                                domain=_CONTROLLABLE_DOMAINS
                            ),
                            multiple=True,
                        )
                    ),
                    vol.Optional(
                        OPT_CONTROL_EXCLUDED_ENTITIES,
                        default=options.get(OPT_CONTROL_EXCLUDED_ENTITIES, []),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            filter=selector.EntityWithDeviceFilterSelectorConfig(
                                domain=_CONTROLLABLE_DOMAINS
                            ),
                            multiple=True,
                        )
                    ),
                    vol.Required(
                        OPT_MIRROR_ENABLED,
                        default=options.get(OPT_MIRROR_ENABLED, False),
                    ): bool,
                    vol.Required(
                        OPT_MIRROR_PROVIDER,
                        default=options.get(OPT_MIRROR_PROVIDER, "github"),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(MIRROR_PROVIDERS),
                            translation_key=OPT_MIRROR_PROVIDER,
                        )
                    ),
                    vol.Optional(
                        OPT_MIRROR_REPO, default=options.get(OPT_MIRROR_REPO, "")
                    ): str,
                    vol.Optional(
                        OPT_MIRROR_SERVER,
                        default=options.get(OPT_MIRROR_SERVER, GITLAB_DEFAULT_SERVER),
                    ): str,
                    vol.Required(
                        OPT_MIRROR_VERIFY_SSL,
                        default=options.get(OPT_MIRROR_VERIFY_SSL, True),
                    ): bool,
                    vol.Optional(OPT_MIRROR_TOKEN): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
        )
