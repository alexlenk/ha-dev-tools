"""Home Assistant Supervisor add-on info and logs - HA OS/Supervised only.

The Supervisor is a separate subsystem, only present on Home Assistant OS
and Supervised installs, reached through the `hassio` integration
(`hass.data[hassio.const.DATA_COMPONENT]`) - never on Core-only installs,
which is how the guard below distinguishes them.

Add-on logs specifically aren't exposed by the typed `aiohasupervisor`
client this HA version ships (its addons client has no `logs` method -
checked directly), so this goes through the same lower-level `HassIO`
REST wrapper HA's own frontend log viewer proxies through instead (see
`homeassistant/components/hassio/http.py`'s forwarded `/addons/{slug}/
logs` paths, and `handler.py`'s `HassIO.send_command`).

Can't be verified end-to-end in this environment - there's no way to
stand up a real Supervisor in a test sandbox, only under actual HA OS/
Supervised. Tests here check our own logic (the not-available guard, and
the shape built from a client response) against mocks, not a real round-
trip.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.hassio import const as hassio_const
from homeassistant.components.hassio.handler import (
    HassioAPIError,
    get_supervisor_client,
)
from homeassistant.core import HomeAssistant

# The hass.data key HassIO is stored under has drifted across HA versions:
# a HassKey named DATA_COMPONENT in newer ones, plain DOMAIN ("hassio") in
# older ones (confirmed: 2025.1.4) - checked directly rather than assumed,
# same pattern as ws_call.py's ActiveConnection signature handling.
_HASSIO_DATA_KEY = getattr(hassio_const, "DATA_COMPONENT", hassio_const.DOMAIN)


class SupervisorNotAvailableError(Exception):
    """Raised when this isn't a Home Assistant OS/Supervised install."""


def _get_hassio(hass: HomeAssistant) -> Any:
    hassio = hass.data.get(_HASSIO_DATA_KEY)
    if hassio is None:
        raise SupervisorNotAvailableError(
            "Supervisor isn't available - add-ons only exist on Home "
            "Assistant OS or Supervised installs"
        )
    return hassio


async def list_addons(hass: HomeAssistant) -> list[dict[str, Any]]:
    """List installed add-ons."""
    _get_hassio(hass)  # raises SupervisorNotAvailableError early if not applicable
    client = get_supervisor_client(hass)
    addons = await client.addons.list()
    return [
        {
            "slug": addon.slug,
            "name": addon.name,
            "state": addon.state.value,
            "version": addon.version,
            "version_latest": addon.version_latest,
            "update_available": addon.update_available,
        }
        for addon in addons
    ]


async def get_addon_logs(
    hass: HomeAssistant, slug: str, *, lines: int | None = None
) -> dict[str, Any]:
    """Get an add-on's raw log output (plain text, oldest first - same as HA's own log viewer).

    `lines`, if given, keeps only the most recent N lines client-side -
    the Supervisor's plain-text log endpoint has no server-side line
    limit or filtering of its own.
    """
    hassio = _get_hassio(hass)
    try:
        text = await hassio.send_command(
            f"/addons/{slug}/logs", method="get", return_text=True
        )
    except HassioAPIError as err:
        return {"error": str(err)}

    log_lines = text.splitlines()
    if lines is not None:
        log_lines = log_lines[-lines:]
    return {"slug": slug, "lines": log_lines}
