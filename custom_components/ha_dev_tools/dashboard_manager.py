"""Dashboard (Lovelace) read/write, storage mode only.

Storage-mode dashboards (the default; YAML mode is legacy and being
removed per home-assistant/core's dev branch - see docs/ARCHITECTURE.md)
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


async def list_dashboards(hass: HomeAssistant, user: User) -> list[dict[str, Any]]:
    """List every configured Lovelace dashboard, storage- and YAML-mode alike.

    Goes through the frontend's own `get_panels` WS command rather than
    `lovelace/dashboards/list` - that collection only covers storage-mode
    dashboards, since a YAML-mode one registered via `lovelace: dashboards:`
    in configuration.yaml never enters it at all (confirmed by reading
    home-assistant/core's lovelace/__init__.py: YAML dashboards are merged
    straight into hass.data[LOVELACE_DATA].dashboards, bypassing
    DashboardsCollection entirely). Every Lovelace dashboard of either mode
    does register a frontend panel with component_name == "lovelace"
    though - that's the one place both kinds show up together, matching
    get_dashboard's own "works in both modes" coverage (issue #77).
    """
    panels = await call_ws_command(hass, user, "get_panels")
    return [
        {
            "url_path": panel["url_path"],
            "title": panel.get("title"),
            "icon": panel.get("icon"),
            "mode": (panel.get("config") or {}).get("mode"),
            "require_admin": panel.get("require_admin", False),
            "show_in_sidebar": panel.get("show_in_sidebar", True),
        }
        for panel in panels.values()
        if panel.get("component_name") == "lovelace"
    ]


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
