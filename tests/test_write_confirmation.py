"""Tests for write_confirmation.py's propose/confirm token store.

llm_api.py's WriteGatedTool tests (test_llm_api.py) already exercise this
through a real tool; these tests cover the token store's own logic
directly - hashing/binding, expiry, and single-use consumption - without
needing a Tool subclass in the way.
"""

import time

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools import write_confirmation


@pytest.mark.asyncio
async def test_create_pending_returns_usable_token(hass: HomeAssistant):
    token = write_confirmation.create_pending(hass, "some_tool", {"a": 1})
    assert isinstance(token, str) and token

    assert (
        write_confirmation.consume_pending(hass, "some_tool", {"a": 1}, token) is True
    )


@pytest.mark.asyncio
async def test_consume_pending_rejects_unknown_token(hass: HomeAssistant):
    assert (
        write_confirmation.consume_pending(hass, "some_tool", {"a": 1}, "bogus")
        is False
    )


@pytest.mark.asyncio
async def test_consume_pending_rejects_mismatched_args(hass: HomeAssistant):
    token = write_confirmation.create_pending(hass, "some_tool", {"a": 1})

    assert (
        write_confirmation.consume_pending(hass, "some_tool", {"a": 2}, token) is False
    )


@pytest.mark.asyncio
async def test_consume_pending_rejects_mismatched_tool_name(hass: HomeAssistant):
    token = write_confirmation.create_pending(hass, "tool_a", {"a": 1})

    assert write_confirmation.consume_pending(hass, "tool_b", {"a": 1}, token) is False


@pytest.mark.asyncio
async def test_consume_pending_ignores_confirm_token_itself_in_the_hash(
    hass: HomeAssistant,
):
    """The args dict passed to consume_pending includes confirm_token (it's part of
    the real call's tool_args); it must not be hashed into args_hash, or every
    confirm call would fail to match its own propose call's hash."""
    token = write_confirmation.create_pending(hass, "some_tool", {"a": 1})

    args_with_token = {"a": 1, "confirm_token": token}
    assert (
        write_confirmation.consume_pending(hass, "some_tool", args_with_token, token)
        is True
    )


@pytest.mark.asyncio
async def test_consume_pending_is_single_use(hass: HomeAssistant):
    token = write_confirmation.create_pending(hass, "some_tool", {"a": 1})

    assert (
        write_confirmation.consume_pending(hass, "some_tool", {"a": 1}, token) is True
    )
    assert (
        write_confirmation.consume_pending(hass, "some_tool", {"a": 1}, token) is False
    )


@pytest.mark.asyncio
async def test_consume_pending_rejects_expired_token(hass: HomeAssistant, monkeypatch):
    real_time = time.time
    monkeypatch.setattr(write_confirmation.time, "time", lambda: real_time())
    token = write_confirmation.create_pending(hass, "some_tool", {"a": 1})

    future = real_time() + write_confirmation.TOKEN_TTL_SECONDS + 1
    monkeypatch.setattr(write_confirmation.time, "time", lambda: future)

    assert (
        write_confirmation.consume_pending(hass, "some_tool", {"a": 1}, token) is False
    )


@pytest.mark.asyncio
async def test_create_pending_prunes_expired_entries(hass: HomeAssistant, monkeypatch):
    """Expired entries don't accumulate forever - each create_pending call prunes them."""
    real_time = time.time
    monkeypatch.setattr(write_confirmation.time, "time", lambda: real_time())
    stale_token = write_confirmation.create_pending(hass, "some_tool", {"a": 1})
    store = write_confirmation._store(hass)
    assert stale_token in store

    future = real_time() + write_confirmation.TOKEN_TTL_SECONDS + 1
    monkeypatch.setattr(write_confirmation.time, "time", lambda: future)
    write_confirmation.create_pending(hass, "some_tool", {"a": 2})

    assert stale_token not in store
