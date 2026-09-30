"""CRUD for HA "helper" entities (input_boolean, counter, timer, etc.).

All nine helper domains share one generic storage-collection pattern in
HA core (confirmed by reading the source - see docs/ARCHITECTURE.md) with
no in-process access point, only WS commands. Built on `ws_call.py`'s
verified loopback mechanism - see tests/test_ws_call.py's real
`input_boolean` CRUD round-trip proving this actually works before this
module was written on top of it.

`person`, `area`, `label` and `category` aren't helpers, but have the
same list/create/update/delete shape over WS, so the helper tools cover
them too rather than adding tools (issues #117, #128): `person` is a
storage collection like the helpers (`person/*`, keyed `person_id`);
rooms, labels and categories are HA's area, label and category registries
(`config/<x>_registry/*`, keyed `<x>_id`; categories also by scope).
"""

from __future__ import annotations

from typing import Any

from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant

from .ws_call import UnresolvedUserError, call_ws_command, resolve_user

__all__ = [
    "HELPER_DOMAINS",
    "InvalidHelperDomainError",
    "UnresolvedUserError",
    "create_helper",
    "delete_helper",
    "list_helpers",
    "resolve_user",
    "update_helper",
]

HELPER_DOMAINS = (
    "input_boolean",
    "input_number",
    "input_text",
    "input_select",
    "input_datetime",
    "input_button",
    "counter",
    "timer",
    "schedule",
    "person",
    "area",
    "label",
    "category",
)

# Domains whose WS commands live under another prefix than `<domain>/`.
_COMMAND_PREFIX = {
    "area": "config/area_registry",
    "label": "config/label_registry",
    "category": "config/category_registry",
}
# Registries keyed `<domain>_id` rather than `id` (issues #117, #128).
_ID_KEY = {"area": "area_id", "label": "label_id", "category": "category_id"}
# The scopes HA's own UI files categories under (the Automations, Scripts,
# Scenes and Helpers pages). A category belongs to exactly one scope, so
# its id here is "<scope>/<category_id>" - everything update and delete need.
CATEGORY_SCOPES = ("automation", "script", "scene", "helpers")


class InvalidHelperDomainError(Exception):
    """Raised for a domain that isn't one of HELPER_DOMAINS."""


def _command(domain: str, action: str) -> str:
    return f"{_COMMAND_PREFIX.get(domain, domain)}/{action}"


def _check_domain(domain: str) -> None:
    if domain not in HELPER_DOMAINS:
        raise InvalidHelperDomainError(
            f"'{domain}' is not a helper domain; must be one of {HELPER_DOMAINS}"
        )


def _item_kwargs(domain: str, item_id: str) -> dict[str, Any]:
    """The WS arguments naming one item: `<domain>_id`, and for a category
    its scope, split from the "<scope>/<category_id>" id."""
    if domain != "category":
        return {f"{domain}_id": item_id}
    scope, _, category_id = item_id.partition("/")
    if not category_id or scope not in CATEGORY_SCOPES:
        raise ValueError(
            f"A category id is '<scope>/<category_id>' with scope one of "
            f"{CATEGORY_SCOPES} (as list_helpers returns it), not '{item_id}'"
        )
    return {"scope": scope, "category_id": category_id}


def _with_id(
    domain: str, item: dict[str, Any], scope: str | None = None
) -> dict[str, Any]:
    """Registry items get `id` too, like every other item here - for a
    category "<scope>/<category_id>", with its scope."""
    key = _ID_KEY.get(domain)
    if key is None:
        return item
    if domain == "category":
        return {"id": f"{scope}/{item[key]}", "scope": scope, **item}
    return {"id": item[key], **item}


async def list_helpers(
    hass: HomeAssistant, user: User, domain: str
) -> list[dict[str, Any]]:
    """List every storage-defined item in a helper domain."""
    _check_domain(domain)
    if domain == "category":
        return [
            _with_id(domain, item, scope)
            for scope in CATEGORY_SCOPES
            for item in await call_ws_command(
                hass, user, _command(domain, "list"), scope=scope
            )
        ]
    items = await call_ws_command(hass, user, _command(domain, "list"))
    if domain == "person":
        # person/list returns {"storage": [...], "config": [...]}; only the
        # storage (UI-made) persons are editable, like any helper.
        return list(items["storage"])
    return [_with_id(domain, item) for item in items]


async def create_helper(
    hass: HomeAssistant, user: User, domain: str, config: dict[str, Any]
) -> dict[str, Any]:
    """Create a new helper item."""
    _check_domain(domain)
    if domain == "category" and config.get("scope") not in CATEGORY_SCOPES:
        raise ValueError(
            f"A category needs 'scope' in config, one of {CATEGORY_SCOPES}"
        )
    created = await call_ws_command(hass, user, _command(domain, "create"), **config)
    return _with_id(domain, created, config.get("scope"))


async def update_helper(
    hass: HomeAssistant, user: User, domain: str, item_id: str, config: dict[str, Any]
) -> dict[str, Any]:
    """Update an existing helper item by id."""
    _check_domain(domain)
    command = _item_kwargs(domain, item_id)
    command.update(config)
    updated = await call_ws_command(hass, user, _command(domain, "update"), **command)
    return _with_id(domain, updated, command.get("scope"))


async def delete_helper(
    hass: HomeAssistant, user: User, domain: str, item_id: str
) -> None:
    """Delete a helper item by id."""
    _check_domain(domain)
    await call_ws_command(
        hass, user, _command(domain, "delete"), **_item_kwargs(domain, item_id)
    )
