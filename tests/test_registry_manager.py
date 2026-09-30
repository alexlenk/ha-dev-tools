"""Tests for registry_manager.py (update_entities, issue #117), against HA's
real entity/device/area registries and WS commands."""

import re

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.ha_dev_tools import registry_manager
from custom_components.ha_dev_tools.registry_manager import (
    RegistryUpdateError,
    apply_updates,
    plan_updates,
    snapshot,
)


@pytest.fixture
async def admin_user(hass: HomeAssistant):
    assert await async_setup_component(hass, "websocket_api", {})
    assert await async_setup_component(hass, "config", {})
    return MockUser(is_owner=True).add_to_hass(hass)


@pytest.fixture
def setup(hass: HomeAssistant):
    """Two phones (FRITZ!Box-style trackers) on one device, a switch, rooms."""
    config_entry = MockConfigEntry(domain="fritz")
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("fritz", "box")},
        name="FRITZ!Box",
    )
    entity_reg = er.async_get(hass)
    phone = entity_reg.async_get_or_create(
        "device_tracker", "fritz", "phone1", suggested_object_id="iphone"
    )
    phone_2 = entity_reg.async_get_or_create(
        "device_tracker", "fritz", "phone2", suggested_object_id="iphone_2"
    )
    relay = entity_reg.async_get_or_create(
        "switch",
        "fritz",
        "relay",
        suggested_object_id="lamp_relay",
        config_entry=config_entry,
        device_id=device.id,
    )
    area_reg = ar.async_get(hass)
    garage = area_reg.async_create("Garage")
    area_reg.async_create("Living Room")
    return {
        "device": device,
        "phone": phone.entity_id,
        "phone_2": phone_2.entity_id,
        "relay": relay.entity_id,
        "garage": garage.id,
    }


def test_plan_describes_every_change(hass: HomeAssistant, setup):
    plans = plan_updates(
        hass,
        [
            {
                "entity_id": setup["phone"],
                "new_entity_id": "device_tracker.alex_iphone_wifi",
                "name": "Alex iPhone",
                "area": "garage",  # a name, any case
                "disabled": False,  # unchanged: not reported as a change
            },
            {"device_id": setup["device"].id, "name": "Router", "area": None},
        ],
    )
    assert plans[0].ws_changes == {
        "new_entity_id": "device_tracker.alex_iphone_wifi",
        "name": "Alex iPhone",
        "area_id": setup["garage"],
        "disabled_by": None,
    }
    assert plans[0].changes == {
        "new_entity_id": {
            "from": "device_tracker.iphone",
            "to": "device_tracker.alex_iphone_wifi",
        },
        "name": {"from": None, "to": "Alex iPhone"},
        "area": {"from": None, "to": "Garage"},
    }
    assert plans[1].ws_changes == {"name_by_user": "Router", "area_id": None}
    assert plans[1].changes == {"name": {"from": None, "to": "Router"}}


def test_plan_refuses_the_whole_batch_and_lists_every_problem(
    hass: HomeAssistant, setup
):
    with pytest.raises(RegistryUpdateError) as err:
        plan_updates(
            hass,
            [
                {"entity_id": setup["phone"], "area": "Garrage"},
                {"entity_id": setup["phone_2"], "new_entity_id": setup["phone"]},
                {"entity_id": "light.nowhere", "name": "x"},
                {"device_id": "nope", "name": "x"},
                {"device_id": setup["device"].id, "icon": "mdi:router"},
                {"entity_id": setup["relay"], "hidden": "yes"},
                {"entity_id": setup["phone"], "name": "again"},
                {"name": "no target"},
            ],
        )
    message = str(err.value)
    assert message.startswith("Nothing was changed")
    for expected in (
        "no room 'Garrage' (rooms: Garage, Living Room)",
        "'device_tracker.iphone' is already taken",
        "not in the entity registry",
        "no such device",
        "icon can't be set on a device",
        "hidden must be true or false",
        "listed more than once",
        "exactly one of entity_id or device_id",
    ):
        assert expected in message


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"new_entity_id": "sensor.phone"}, r"same domain \(device_tracker\)"),
        ({}, "nothing to change"),
        ({"new_entity_id": "not valid"}, "must be a valid entity_id"),
        ({"show_as": "light"}, "show_as only applies to a switch"),
    ],
)
def test_plan_checks_renames_and_show_as(hass: HomeAssistant, setup, item, expected):
    with pytest.raises(RegistryUpdateError, match=expected):
        plan_updates(hass, [{"entity_id": setup["phone"], **item}])


def test_plan_checks_show_as_targets(hass: HomeAssistant, setup):
    relay = setup["relay"]
    with pytest.raises(RegistryUpdateError, match="must be one of cover, fan"):
        plan_updates(hass, [{"entity_id": relay, "show_as": "sensor"}])
    with pytest.raises(RegistryUpdateError, match="can't be combined"):
        plan_updates(
            hass,
            [{"entity_id": relay, "show_as": "light", "new_entity_id": "switch.x"}],
        )
    MockConfigEntry(domain="switch_as_x", options={"entity_id": relay}).add_to_hass(
        hass
    )
    with pytest.raises(RegistryUpdateError, match="already shown as another type"):
        plan_updates(hass, [{"entity_id": relay, "show_as": "light"}])


def test_two_renames_to_the_same_id_are_refused(hass: HomeAssistant, setup):
    with pytest.raises(RegistryUpdateError, match="already taken"):
        plan_updates(
            hass,
            [
                {"entity_id": setup["phone"], "new_entity_id": "device_tracker.x"},
                {"entity_id": setup["phone_2"], "new_entity_id": "device_tracker.x"},
            ],
        )


@pytest.mark.asyncio
async def test_apply_goes_through_has_own_registry_commands(
    hass: HomeAssistant, setup, admin_user
):
    entity_reg = er.async_get(hass)
    plans = plan_updates(
        hass,
        [
            {
                "entity_id": setup["phone"],
                "new_entity_id": "device_tracker.alex_iphone_wifi",
                "area": "Garage",
                "hidden": True,
            },
            {"entity_id": setup["phone_2"], "disabled": True},
            {"device_id": setup["device"].id, "name": "Router", "area": "Living Room"},
        ],
    )
    results = await apply_updates(hass, admin_user, plans)

    assert results[0]["entity_id"] == "device_tracker.alex_iphone_wifi"
    renamed = entity_reg.async_get("device_tracker.alex_iphone_wifi")
    assert renamed.area_id == setup["garage"]
    assert renamed.hidden_by is er.RegistryEntryHider.USER
    assert entity_reg.async_get(setup["phone_2"]).disabled_by is (
        er.RegistryEntryDisabler.USER
    )
    device = dr.async_get(hass).async_get(setup["device"].id)
    assert device.name_by_user == "Router"
    assert device.area_id == "living_room"

    # Enabling again: HA reports the reload it schedules (or needs a restart).
    results = await apply_updates(
        hass,
        admin_user,
        plan_updates(hass, [{"entity_id": setup["phone_2"], "disabled": False}]),
    )
    assert "require_restart" in results[0] or "reload_delay" in results[0]


@pytest.mark.asyncio
async def test_show_as_runs_switch_as_x_and_hides_the_switch(
    hass: HomeAssistant, setup, admin_user
):
    assert await async_setup_component(hass, "homeassistant", {})
    plans = plan_updates(hass, [{"entity_id": setup["relay"], "show_as": "light"}])
    results = await apply_updates(hass, admin_user, plans)

    # Named the way HA names it (after the relay's device here).
    shown_as = results[0]["shown_as"]
    assert shown_as.startswith("light.")
    entity_reg = er.async_get(hass)
    assert entity_reg.async_get(shown_as).platform == "switch_as_x"
    assert entity_reg.async_get(setup["relay"]).hidden_by is (
        er.RegistryEntryHider.INTEGRATION
    )
    assert registry_manager._is_wrapped(hass, setup["relay"])


def test_snapshot_of_entities_and_devices(hass: HomeAssistant, setup):
    plans = plan_updates(
        hass,
        [
            {"entity_id": setup["phone"], "name": "x"},
            {"device_id": setup["device"].id, "disabled": True},
        ],
    )
    assert snapshot(hass, plans[0], setup["phone"])["entity_id"] == setup["phone"]
    assert snapshot(hass, plans[1], setup["device"].id) == {
        "device_id": setup["device"].id,
        "name": "FRITZ!Box",
        "name_by_user": None,
        "area_id": None,
        "disabled_by": None,
    }
    assert snapshot(hass, plans[0], "device_tracker.gone") is None
    assert snapshot(hass, plans[1], "gone") is None


@pytest.mark.asyncio
async def test_apply_stops_at_a_refused_item_and_marks_the_rest(
    hass: HomeAssistant, setup, admin_user
):
    """HA refuses enabling an entity of a disabled device - only at write
    time, so plan_updates can't catch it."""
    entity_reg = er.async_get(hass)
    entity_reg.async_update_entity(
        setup["relay"], disabled_by=er.RegistryEntryDisabler.USER
    )
    dr.async_get(hass).async_update_device(
        setup["device"].id, disabled_by=dr.DeviceEntryDisabler.USER
    )
    plans = plan_updates(
        hass,
        [
            {"entity_id": setup["phone"], "name": "First"},
            {"entity_id": setup["relay"], "disabled": False},
            {"entity_id": setup["phone_2"], "name": "Never"},
        ],
    )
    results = await apply_updates(hass, admin_user, plans)

    assert results[0]["changed"] == {"name": {"from": None, "to": "First"}}
    assert "Device is disabled" in results[1]["error"]
    assert results[2] == {
        "entity_id": setup["phone_2"],
        "not_applied": "an earlier item failed",
    }
    assert entity_reg.async_get(setup["phone_2"]).name is None


# --- labels and categories (issue #128) --------------------------------------


@pytest.fixture
def tags(hass: HomeAssistant, setup):
    from homeassistant.helpers import category_registry as cr
    from homeassistant.helpers import label_registry as lr

    labels = lr.async_get(hass)
    energy = labels.async_create("Energy")
    deye = labels.async_create("Deye")
    categories = cr.async_get(hass)
    modbus = categories.async_create(scope="helpers", name="Deye 12K Modbus")
    doors = categories.async_create(scope="automation", name="Doors")
    return {
        "energy": energy.label_id,
        "deye": deye.label_id,
        "modbus": modbus.category_id,
        "doors": doors.category_id,
    }


def test_plan_labels_and_categories_by_name(hass: HomeAssistant, setup, tags):
    er.async_get(hass).async_update_entity(setup["phone"], labels={tags["deye"]})
    plans = plan_updates(
        hass,
        [
            {
                "entity_id": setup["phone"],
                "add_labels": ["energy"],  # a name, any case
                "categories": {"helpers": "deye 12k modbus", "automation": None},
            },
            {"entity_id": setup["phone_2"], "labels": [tags["energy"], "Deye"]},
            {"device_id": setup["device"].id, "labels": ["Energy"]},
        ],
    )
    assert plans[0].ws_changes == {
        "labels": sorted({tags["deye"], tags["energy"]}),
        "categories": {"helpers": tags["modbus"], "automation": None},
    }
    assert plans[0].changes == {
        "labels": {"from": ["Deye"], "to": ["Deye", "Energy"]},
        "categories": {"helpers": {"from": None, "to": "Deye 12K Modbus"}},
    }
    assert plans[1].ws_changes["labels"] == sorted({tags["energy"], tags["deye"]})
    assert plans[2].ws_changes == {"labels": [tags["energy"]]}


def test_plan_remove_labels_and_unchanged_sets(hass: HomeAssistant, setup, tags):
    er.async_get(hass).async_update_entity(
        setup["phone"], labels={tags["deye"], tags["energy"]}
    )
    plans = plan_updates(
        hass,
        [
            {"entity_id": setup["phone"], "remove_labels": ["Deye"]},
            {"entity_id": setup["phone_2"], "labels": []},  # already none
        ],
    )
    assert plans[0].ws_changes == {"labels": [tags["energy"]]}
    assert plans[0].changes == {
        "labels": {"from": ["Deye", "Energy"], "to": ["Energy"]}
    }
    assert plans[1].changes == {}


def test_plan_category_by_id(hass: HomeAssistant, setup, tags):
    plans = plan_updates(
        hass,
        [{"entity_id": setup["phone"], "categories": {"automation": tags["doors"]}}],
    )
    assert plans[0].ws_changes == {"categories": {"automation": tags["doors"]}}
    assert plans[0].changes == {
        "categories": {"automation": {"from": None, "to": "Doors"}}
    }


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"labels": ["Solar"]}, "no label 'Solar' (labels: Deye, Energy)"),
        ({"labels": ["Deye"], "add_labels": ["Energy"]}, "use it alone"),
        ({"labels": "Deye"}, "must be a list of label names or ids"),
        ({"categories": {"helpers": "Nope"}}, "no helpers category 'Nope'"),
        ({"categories": {"garage": "x"}}, "no category scope 'garage'"),
        ({"categories": ["x"]}, "categories is {scope: category}"),
        ({"categories": {}}, "categories is {scope: category}"),
    ],
)
def test_plan_refuses_unknown_labels_and_categories(
    hass: HomeAssistant, setup, tags, item, expected
):
    with pytest.raises(RegistryUpdateError, match=re.escape(expected)):
        plan_updates(hass, [{"entity_id": setup["phone"], **item}])


def test_device_items_have_no_categories(hass: HomeAssistant, setup, tags):
    with pytest.raises(
        RegistryUpdateError, match="categories can't be set on a device"
    ):
        plan_updates(
            hass, [{"device_id": setup["device"].id, "categories": {"helpers": "x"}}]
        )


@pytest.mark.asyncio
async def test_apply_labels_and_categories(
    hass: HomeAssistant, setup, tags, admin_user
):
    plans = plan_updates(
        hass,
        [
            {
                "entity_id": setup["phone"],
                "labels": ["Energy"],
                "categories": {"helpers": "Deye 12K Modbus"},
            },
            {"device_id": setup["device"].id, "add_labels": ["Deye"]},
        ],
    )
    await apply_updates(hass, admin_user, plans)
    entry = er.async_get(hass).async_get(setup["phone"])
    assert entry.labels == {tags["energy"]}
    assert entry.categories == {"helpers": tags["modbus"]}
    assert dr.async_get(hass).async_get(setup["device"].id).labels == {tags["deye"]}

    # Clearing one scope leaves the others.
    er.async_get(hass).async_update_entity(
        setup["phone"],
        categories={"helpers": tags["modbus"], "automation": tags["doors"]},
    )
    await apply_updates(
        hass,
        admin_user,
        plan_updates(
            hass, [{"entity_id": setup["phone"], "categories": {"helpers": None}}]
        ),
    )
    assert er.async_get(hass).async_get(setup["phone"]).categories == {
        "automation": tags["doors"]
    }
