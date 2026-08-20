"""CRUD for HA "helper" entities (input_boolean, counter, timer, etc.).

All nine helper domains share one generic storage-collection pattern in
HA core (confirmed by reading the source - see docs/RESTART_PLAN.md) with
no in-process access point, only WS commands. Built on `ws_call.py`'s
verified loopback mechanism - see tests/test_ws_call.py's real
`input_boolean` CRUD round-trip proving this actually works before this
module was written on top of it.
"""
from __future__ import annotations

from typing import Any

from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from .ws_call import call_ws_command

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
)


class UnresolvedUserError(Exception):
    """Raised when the calling context has no resolvable HA user.

    Helper create/update/delete are admin-gated at the real WS command
    level - refuse before even trying, rather than falling back to some
    synthetic bypass user.
    """


class InvalidHelperDomainError(Exception):
    """Raised for a domain that isn't one of the nine known helper domains."""


def _check_domain(domain: str) -> None:
    if domain not in HELPER_DOMAINS:
        raise InvalidHelperDomainError(
            f"'{domain}' is not a helper domain; must be one of {HELPER_DOMAINS}"
        )


async def resolve_user(hass: HomeAssistant, llm_context: llm.LLMContext) -> User:
    """Resolve the real HA user behind an MCP tool call.

    HA's native mcp_server integration builds LLMContext.context from the
    authenticated request, so this should be the real caller in
    production - not a synthetic admin bypass.
    """
    user_id = llm_context.context.user_id if llm_context.context else None
    user = await hass.auth.async_get_user(user_id) if user_id else None
    if user is None:
        raise UnresolvedUserError(
            "Could not resolve a real Home Assistant user from this request's "
            "context - refusing rather than acting with elevated/ambiguous "
            "permissions"
        )
    return user


async def list_helpers(
    hass: HomeAssistant, user: User, domain: str
) -> list[dict[str, Any]]:
    """List every storage-defined item in a helper domain."""
    _check_domain(domain)
    return await call_ws_command(hass, user, f"{domain}/list")


async def create_helper(
    hass: HomeAssistant, user: User, domain: str, config: dict[str, Any]
) -> dict[str, Any]:
    """Create a new helper item."""
    _check_domain(domain)
    return await call_ws_command(hass, user, f"{domain}/create", **config)


async def update_helper(
    hass: HomeAssistant, user: User, domain: str, item_id: str, config: dict[str, Any]
) -> dict[str, Any]:
    """Update an existing helper item by id."""
    _check_domain(domain)
    return await call_ws_command(
        hass, user, f"{domain}/update", **{f"{domain}_id": item_id}, **config
    )


async def delete_helper(hass: HomeAssistant, user: User, domain: str, item_id: str) -> None:
    """Delete a helper item by id."""
    _check_domain(domain)
    await call_ws_command(hass, user, f"{domain}/delete", **{f"{domain}_id": item_id})
