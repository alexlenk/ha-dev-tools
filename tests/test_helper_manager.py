"""Tests for helper CRUD (helper_manager.py), against real HA components."""

import inspect

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools.helper_manager import (
    InvalidHelperDomainError,
    UnresolvedUserError,
    create_helper,
    delete_helper,
    list_helpers,
    resolve_user,
    update_helper,
)


def _llm_context(context: Context | None) -> llm.LLMContext:
    """Build an LLMContext tolerant of field changes across HA versions (see test_llm_api.py)."""
    fields = {
        "platform": "test",
        "context": context,
        "user_prompt": None,
        "language": "en",
        "assistant": "test",
        "device_id": None,
    }
    accepted = set(inspect.signature(llm.LLMContext.__init__).parameters)
    return llm.LLMContext(**{k: v for k, v in fields.items() if k in accepted})


@pytest.fixture(autouse=True)
async def setup_websocket_api(hass: HomeAssistant):
    assert await async_setup_component(hass, "websocket_api", {})


@pytest.fixture
async def admin_user(hass: HomeAssistant):
    return MockUser(is_owner=True).add_to_hass(hass)


@pytest.mark.asyncio
async def test_helper_crud_round_trip_input_boolean(hass: HomeAssistant, admin_user):
    assert await async_setup_component(hass, "input_boolean", {})

    created = await create_helper(hass, admin_user, "input_boolean", {"name": "Test"})
    item_id = created["id"]

    items = await list_helpers(hass, admin_user, "input_boolean")
    assert any(item["id"] == item_id for item in items)

    updated = await update_helper(
        hass, admin_user, "input_boolean", item_id, {"name": "Renamed"}
    )
    assert updated["name"] == "Renamed"

    await delete_helper(hass, admin_user, "input_boolean", item_id)

    items_after = await list_helpers(hass, admin_user, "input_boolean")
    assert not any(item["id"] == item_id for item in items_after)


@pytest.mark.asyncio
async def test_helper_crud_round_trip_counter(hass: HomeAssistant, admin_user):
    """Prove this generalizes beyond input_* domains, not just input_boolean."""
    assert await async_setup_component(hass, "counter", {})

    created = await create_helper(hass, admin_user, "counter", {"name": "Test Counter"})
    item_id = created["id"]

    items = await list_helpers(hass, admin_user, "counter")
    assert any(item["id"] == item_id for item in items)

    await delete_helper(hass, admin_user, "counter", item_id)


@pytest.mark.asyncio
async def test_area_crud_round_trip(hass: HomeAssistant, admin_user):
    """Issue #117: rooms through the helper functions, via HA's own
    config/area_registry commands; items carry `id` like every helper."""
    assert await async_setup_component(hass, "config", {})

    created = await create_helper(hass, admin_user, "area", {"name": "Garage"})
    assert created["id"] == created["area_id"]

    items = await list_helpers(hass, admin_user, "area")
    assert {"id": created["id"], "name": "Garage"}.items() <= next(
        item for item in items if item["id"] == created["id"]
    ).items()

    updated = await update_helper(
        hass, admin_user, "area", created["id"], {"name": "Workshop"}
    )
    assert (updated["id"], updated["name"]) == (created["id"], "Workshop")

    await delete_helper(hass, admin_user, "area", created["id"])
    items_after = await list_helpers(hass, admin_user, "area")
    assert not any(item["id"] == created["id"] for item in items_after)


@pytest.mark.asyncio
async def test_person_crud_round_trip_links_trackers(hass: HomeAssistant, admin_user):
    """Issue #117: link device trackers to a UI-made person."""
    assert await async_setup_component(hass, "person", {})

    created = await create_helper(hass, admin_user, "person", {"name": "Alex"})
    updated = await update_helper(
        hass,
        admin_user,
        "person",
        created["id"],
        {"device_trackers": ["device_tracker.alex_iphone_wifi"]},
    )
    assert updated["name"] == "Alex"  # only the given fields change
    assert updated["device_trackers"] == ["device_tracker.alex_iphone_wifi"]

    items = await list_helpers(hass, admin_user, "person")
    assert [item["id"] for item in items] == [created["id"]]

    await delete_helper(hass, admin_user, "person", created["id"])
    assert await list_helpers(hass, admin_user, "person") == []


@pytest.mark.asyncio
async def test_invalid_domain_rejected(hass: HomeAssistant, admin_user):
    with pytest.raises(InvalidHelperDomainError):
        await list_helpers(hass, admin_user, "not_a_helper_domain")


@pytest.mark.asyncio
async def test_resolve_user_from_context(hass: HomeAssistant, admin_user):
    llm_context = _llm_context(Context(user_id=admin_user.id))

    resolved = await resolve_user(hass, llm_context)

    assert resolved.id == admin_user.id


@pytest.mark.asyncio
async def test_resolve_user_refuses_when_unresolvable(hass: HomeAssistant):
    llm_context = _llm_context(None)

    with pytest.raises(UnresolvedUserError):
        await resolve_user(hass, llm_context)
