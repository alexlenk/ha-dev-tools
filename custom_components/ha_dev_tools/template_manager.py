"""Jinja2 template rendering and validation against live state.

Runs entirely in-process via `homeassistant.helpers.template.Template` -
no HTTP/WS round-trip needed at all, unlike the file/helper/dashboard
tools. This is the tight "draft, render against live state, adjust,
re-render" loop the restart plan calls out as the core of the author/
iterate workflow (see docs/RESTART_PLAN.md) - the agent should never need
to ask the user to paste a template into the Developer Tools UI.
"""
from __future__ import annotations

import inspect
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import TemplateError
from homeassistant.helpers.template import Template


async def _maybe_await(value: Any) -> Any:
    """Await value if it's awaitable, else return it as-is.

    Despite the `async_` naming convention, `Template.async_render`/
    `async_render_to_info` are plain synchronous functions in some HA
    versions (confirmed: 2025.1.4) and real coroutines in others - HA's
    `async_` prefix means "callback-safe", not "always a coroutine".
    Checked directly rather than assumed after this broke on the pinned
    test version despite matching the usual async calling convention.
    """
    if inspect.isawaitable(value):
        return await value
    return value


async def render_template(
    hass: HomeAssistant, template_str: str, *, variables: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Render a template against live state. Never raises - errors come back in the result."""
    tpl = Template(template_str, hass)
    try:
        result = await _maybe_await(tpl.async_render(variables=variables, parse_result=True))
    except TemplateError as err:
        return {"success": False, "error": str(err)}
    return {"success": True, "result": result}


async def validate_template(hass: HomeAssistant, template_str: str) -> dict[str, Any]:
    """Validate a template's syntax, and report which entities it references.

    Distinguishes a syntax error (never even attempted to render) from a
    render error (syntactically valid, failed against live state - e.g. a
    filter applied to an entity that doesn't exist) and from success.
    `unknown_entities` flags referenced entity_ids that don't currently
    exist, whether or not the render itself succeeded (a template can
    render successfully while silently treating a typo'd entity_id as
    always-unavailable, which validate_template surfaces explicitly).
    """
    tpl = Template(template_str, hass)
    try:
        tpl.ensure_valid()
    except TemplateError as err:
        return {
            "valid": False,
            "syntax_error": str(err),
            "referenced_entities": [],
            "unknown_entities": [],
        }

    info = await _maybe_await(tpl.async_render_to_info())
    referenced = sorted(info.entities)
    unknown = sorted(e for e in referenced if hass.states.get(e) is None)

    if info.exception is not None:
        return {
            "valid": False,
            "render_error": str(info.exception),
            "referenced_entities": referenced,
            "unknown_entities": unknown,
        }

    return {
        "valid": True,
        "referenced_entities": referenced,
        "unknown_entities": unknown,
    }
