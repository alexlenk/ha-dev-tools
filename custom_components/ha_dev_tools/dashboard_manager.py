"""Dashboard (Lovelace) read/write, storage mode only.

Storage-mode dashboards (the default; YAML mode is legacy and being
removed per home-assistant/core's dev branch - see docs/RESTART_PLAN.md)
are fully reachable through `lovelace/config` and `lovelace/config/save`,
via the same `ws_call.py` loopback verified for helpers. YAML-mode
dashboards hard-reject `lovelace/config/save` at the HA level ("Not
supported") - that needs raw `ui-lovelace.yaml` file access instead, same
as the automation-YAML case, and isn't implemented yet.
"""
from __future__ import annotations

from typing import Any

from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant

from .ws_call import WebSocketCommandError, call_ws_command


class YamlModeDashboardError(Exception):
    """Raised when a write is attempted against a YAML-mode dashboard.

    lovelace/config/save hard-rejects these at the HA level rather than
    writing anything - this just gives that the same treatment early and
    with a clearer message, rather than a confusing WebSocketCommandError.
    Reads still work fine via lovelace/config in either mode.
    """


async def get_dashboard(
    hass: HomeAssistant, user: User, *, url_path: str | None = None
) -> dict[str, Any]:
    """Read a dashboard's config. Works in both storage and YAML mode."""
    kwargs: dict[str, Any] = {"force": False}
    if url_path is not None:
        kwargs["url_path"] = url_path
    return await call_ws_command(hass, user, "lovelace/config", **kwargs)


async def write_dashboard(
    hass: HomeAssistant,
    user: User,
    config: dict[str, Any] | str,
    *,
    url_path: str | None = None,
) -> None:
    """Save a dashboard's config. Storage mode only - see module docstring."""
    kwargs: dict[str, Any] = {"config": config}
    if url_path is not None:
        kwargs["url_path"] = url_path
    try:
        await call_ws_command(hass, user, "lovelace/config/save", **kwargs)
    except WebSocketCommandError as exc:
        if "not supported" in str(exc).lower():
            raise YamlModeDashboardError(
                "This dashboard is in YAML mode, which HA doesn't allow "
                "saving to via the API - edit ui-lovelace.yaml directly "
                "(not yet implemented in this tool)."
            ) from exc
        raise
