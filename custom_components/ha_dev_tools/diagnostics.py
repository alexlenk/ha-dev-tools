"""Diagnostics support for HA Dev Tools.

Implements Home Assistant's diagnostics platform (Settings -> Devices &
Services -> HA Dev Tools -> the three-dot menu -> Download diagnostics).
Signature and helper usage confirmed against the homeassistant package
actually installed in this repo's venv (2026.8.2, matching manifest.json's
minimum) - see homeassistant/components/diagnostics/__init__.py's
DiagnosticsProtocol and homeassistant/components/diagnostics/util.py's
async_redact_data, and e.g. homeassistant/components/aladdin_connect/
diagnostics.py and .../actron_air/diagnostics.py for the established
"redact a plain dict of config-entry values" pattern this file follows.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from . import access_control
from .const import DOMAIN, OPT_DRY_RUN, OPT_MIRROR_ENABLED, OPT_MIRROR_TOKEN

# The git-mirroring access token (options_flow.py, const.py) is a real
# GitHub credential and the only genuinely sensitive value this config
# entry can hold. Everything else in entry.options - dry_run, mirror_enabled,
# and the mirror_repo "owner/repo" name - is a plain toggle or a repo
# reference, not a secret (same "not everything here is sensitive" judgment
# docs/SECURITY.md applies to the arm-file timestamp).
TO_REDACT = {OPT_MIRROR_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Deliberately small: just enough to triage a bug report (which version
    is running, is dry-run on, is git mirroring on, is the arm gate
    currently open) plus the entry's own options with the mirroring token
    redacted - not a dump of this integration's full read/write surface,
    that's what the tools themselves are for.
    """
    integration = await async_get_integration(hass, DOMAIN)

    return {
        "integration_version": (
            str(integration.version) if integration.version is not None else None
        ),
        "dry_run_enabled": bool(entry.options.get(OPT_DRY_RUN, False)),
        "mirroring_enabled": bool(entry.options.get(OPT_MIRROR_ENABLED, False)),
        "armed": await _is_armed(hass),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
    }


async def _is_armed(hass: HomeAssistant) -> bool:
    """Read-only check of whether dev_tools is currently armed.

    Reuses access_control.check_armed() - the same function every gated
    tool call goes through - instead of re-deriving armed state from its
    private helpers, so this can never drift from what a real tool call
    would decide. This never calls touch_armed(), so downloading
    diagnostics can't itself extend an active arm window.
    """
    try:
        await access_control.check_armed(hass)
    except access_control.NotArmedError:
        return False
    return True
