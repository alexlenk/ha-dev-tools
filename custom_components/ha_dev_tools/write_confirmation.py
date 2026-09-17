"""Two-call propose/confirm gate for every write tool.

See docs/AUTOMATION_TESTING_DESIGN.md's "Per-write confirmation" section
for the design and why this is friction/UX, not a hard guarantee: it
stops an agent from writing on its first call, and stops a token issued
for one specific tool call from silently authorizing a different one, but
it does not stop an agent that calls propose and confirm back to back
with no real human in between. An HA-native out-of-band approval gate was
considered and explicitly rejected in that same document - see its
"Explicitly rejected" section.

State lives in hass.data[DOMAIN], the same place __init__.py already
keeps this integration's other per-instance state, not a module-level
global - keeps tests isolated and matches the rest of this codebase.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass

from homeassistant.core import HomeAssistant

from .const import DOMAIN

TOKEN_TTL_SECONDS = 600  # 10 minutes
_STORE_KEY = "pending_confirmations"


@dataclass(slots=True)
class _PendingConfirmation:
    args_hash: str
    expires_at: float


def _args_hash(tool_name: str, args: dict) -> str:
    """Bind a token to one specific tool call's arguments.

    confirm_token itself is excluded so hashing the confirm call's own
    args (which include the token) still matches the hash computed at
    propose time (which didn't have it yet).
    """
    normalized = {key: value for key, value in args.items() if key != "confirm_token"}
    canonical = json.dumps(normalized, sort_keys=True, default=str)
    return hashlib.sha256(f"{tool_name}:{canonical}".encode()).hexdigest()


def _store(hass: HomeAssistant) -> dict[str, _PendingConfirmation]:
    return hass.data.setdefault(DOMAIN, {}).setdefault(_STORE_KEY, {})


def _prune_expired(store: dict[str, _PendingConfirmation], *, now: float) -> None:
    for token in [tok for tok, pending in store.items() if pending.expires_at <= now]:
        del store[token]


def create_pending(hass: HomeAssistant, tool_name: str, args: dict) -> str:
    """Register a new pending confirmation for this exact tool call, return its token."""
    store = _store(hass)
    now = time.time()
    _prune_expired(store, now=now)
    token = secrets.token_urlsafe(24)
    store[token] = _PendingConfirmation(
        args_hash=_args_hash(tool_name, args), expires_at=now + TOKEN_TTL_SECONDS
    )
    return token


def consume_pending(
    hass: HomeAssistant, tool_name: str, args: dict, token: str
) -> bool:
    """Validate and one-time-consume a token for this exact tool call.

    True only if the token exists, hasn't expired, and matches a hash of
    these same arguments. Consumed (deleted) on success so a token can't
    be replayed for a second write.
    """
    store = _store(hass)
    now = time.time()
    _prune_expired(store, now=now)
    pending = store.get(token)
    if pending is None:
        return False
    if pending.args_hash != _args_hash(tool_name, args):
        return False
    del store[token]
    return True
