"""In-process WebSocket API command invocation.

Some Home Assistant state (helpers like `input_boolean`/`counter`/`timer`,
and dashboards) is only reachable through the WebSocket API - the
component that owns it (e.g. `input_boolean/__init__.py`) keeps its
`StorageCollection` as a private local variable inside its own
`async_setup`, never exposed via `hass.data` (confirmed by reading the
actual home-assistant/core source - see docs/ARCHITECTURE.md's "WebSocket
loopback pattern" section). The WS API is genuinely the only public
interface for it.

Rather than open a real network loopback connection to ourselves, this
constructs a `websocket_api.ActiveConnection` with a fake `send_message`
callback that captures the result into a Future - reusing 100% of HA's
real command lookup, schema validation, admin-checking, and dispatch
logic (`ActiveConnection.async_handle`), faking only the transport, which
is the one part we genuinely don't have (or need) a real socket for.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from types import SimpleNamespace
from typing import Any

from homeassistant.auth.models import User
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10


class WebSocketCommandError(HomeAssistantError):
    """A WS command handler returned an error result."""

    def __init__(self, code: str, message: str) -> None:
        """Init with the WS error code/message."""
        self.code = code
        super().__init__(f"{code}: {message}")


class UnresolvedUserError(Exception):
    """Raised when the calling context has no resolvable HA user.

    Admin-gated WS commands (helper/dashboard create/update/delete etc.)
    should refuse before even trying, rather than falling back to some
    synthetic bypass user.
    """


async def resolve_user(hass: HomeAssistant, llm_context: llm.LLMContext) -> User:
    """Resolve the real HA user behind an MCP tool call.

    HA's native mcp_server integration builds LLMContext.context from the
    authenticated request, so this should be the real caller in
    production - not a synthetic admin bypass. Shared by any WS-backed
    tool (helpers, dashboards, ...), not helper-specific.
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


def _decode(raw: bytes | str | dict[str, Any]) -> dict[str, Any]:
    """Normalize whatever ActiveConnection.send_message was given into a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


async def call_ws_command(
    hass: HomeAssistant,
    user: User,
    command_type: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    **kwargs: Any,
) -> Any:
    """Invoke a registered WebSocket API command in-process and return its result.

    Runs as `user` - callers should resolve this to the real user behind
    the MCP request (e.g. via `llm_context.context.user_id` +
    `hass.auth.async_get_user`), not a synthetic admin, so admin-gated
    commands enforce the real caller's permissions.

    Raises WebSocketCommandError if the handler returns an error result,
    or TimeoutError if it never responds within `timeout` seconds (e.g. a
    subscription-style command that streams rather than resolving once -
    not supported here, only fire-once request/response commands are).
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()

    def _send_message(message: bytes | str | dict[str, Any]) -> None:
        if not future.done():
            future.set_result(_decode(message))

    # ActiveConnection's signature has drifted across HA versions: `remote`
    # is present in some, not others, and `refresh_token` is optional
    # (`RefreshToken | None`) in some but required-non-None in others (its
    # `.id` is accessed unconditionally there). A stub with `.id = None`
    # satisfies both - `if refresh_token else None` treats any truthy
    # object as present, and `.id` access finds None either way. We
    # generally have no real RefreshToken for an internal Tool call.
    all_kwargs = {
        "logger": _LOGGER,
        "hass": hass,
        "send_message": _send_message,
        "user": user,
        "refresh_token": SimpleNamespace(id=None),
        "remote": None,
    }
    # Under Python 3.14's deferred annotation evaluation (PEP 649/749),
    # plain inspect.signature() calls ActiveConnection.__init__'s
    # __annotate__ to resolve its parameter annotations - and raises
    # NameError there, because `WebSocketAdapter` isn't resolvable in that
    # scope at inspection time (confirmed: real failure on HA 2026.8.2 /
    # Python 3.14.7, not assumed). We only need parameter *names*, never
    # their annotations, so ask for best-effort ForwardRef placeholders
    # instead of raising. inspect.Format itself is 3.14+ only - checked
    # with getattr rather than try/except TypeError, since referencing
    # inspect.Format at all raises AttributeError on older interpreters
    # before the call even happens (confirmed: a bare `except TypeError`
    # here does not catch that - real failure on Python 3.12, not
    # assumed). Older interpreters don't defer annotation evaluation and
    # so don't hit the NameError this guards against either way.
    annotation_format = getattr(inspect, "Format", None)
    if annotation_format is not None:
        sig = inspect.signature(
            websocket_api.ActiveConnection.__init__,
            annotation_format=annotation_format.FORWARDREF,
        )
    else:
        sig = inspect.signature(websocket_api.ActiveConnection.__init__)
    accepted = set(sig.parameters)
    connection = websocket_api.ActiveConnection(
        **{k: v for k, v in all_kwargs.items() if k in accepted}
    )

    msg: dict[str, Any] = {"id": 1, "type": command_type, **kwargs}
    connection.async_handle(msg)

    try:
        result = await asyncio.wait_for(future, timeout=timeout)
    except TimeoutError as err:
        raise TimeoutError(
            f"WS command '{command_type}' did not respond within {timeout}s"
        ) from err

    if result.get("type") == "result" and result.get("success"):
        return result.get("result")

    raise WebSocketCommandError(
        result.get("error", {}).get("code", "unknown_error"),
        result.get("error", {}).get("message", "Unknown error"),
    )
