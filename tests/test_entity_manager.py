"""Tests for area/domain-scoped entity lookup (entity_manager.py)."""
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_dev_tools.entity_manager import find_entities


@pytest.fixture
def bedroom_area(hass: HomeAssistant):
    """A real area entry: 'Daughter's Bedroom'."""
    return ar.async_get(hass).async_get_or_create("Daughter's Bedroom")


@pytest.fixture
def bedroom_device(hass: HomeAssistant, bedroom_area):
    """A device placed in the bedroom area."""
    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    device_reg = dr.async_get(hass)
    entry = device_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "shutter_device")},
    )
    return device_reg.async_update_device(entry.id, area_id=bedroom_area.id)


def _register_entity(
    hass: HomeAssistant,
    entity_id: str,
    *,
    device_id: str | None = None,
    area_id: str | None = None,
    disabled: bool = False,
    state: str | None = "on",
) -> None:
    domain, object_id = entity_id.split(".", 1)
    entity_reg = er.async_get(hass)
    entity_reg.async_get_or_create(
        domain,
        "test",
        object_id,
        suggested_object_id=object_id,
        device_id=device_id,
        disabled_by=er.RegistryEntryDisabler.USER if disabled else None,
    )
    if area_id:
        entity_reg.async_update_entity(entity_id, area_id=area_id)
    if state is not None:
        hass.states.async_set(entity_id, state)


@pytest.mark.asyncio
async def test_find_entities_by_area_inherits_from_device(
    hass: HomeAssistant, bedroom_area, bedroom_device
):
    _register_entity(hass, "cover.shutter", device_id=bedroom_device.id)
    _register_entity(hass, "light.kitchen")  # no area - must not match

    result = find_entities(hass, area="Daughter's Bedroom")

    entity_ids = {e["entity_id"] for e in result["entities"]}
    assert entity_ids == {"cover.shutter"}
    assert result["entities"][0]["area_id"] == bedroom_area.id


@pytest.mark.asyncio
async def test_find_entities_area_name_is_case_insensitive_substring(
    hass: HomeAssistant, bedroom_area, bedroom_device
):
    _register_entity(hass, "cover.shutter", device_id=bedroom_device.id)

    result = find_entities(hass, area="bedroom")

    assert {e["entity_id"] for e in result["entities"]} == {"cover.shutter"}


@pytest.mark.asyncio
async def test_find_entities_unknown_area_returns_error(hass: HomeAssistant):
    result = find_entities(hass, area="Nonexistent Room")

    assert result["entities"] == []
    assert "error" in result


@pytest.mark.asyncio
async def test_find_entities_domain_filter(hass: HomeAssistant):
    _register_entity(hass, "light.kitchen")
    _register_entity(hass, "switch.kitchen")

    result = find_entities(hass, domain="light")

    assert {e["entity_id"] for e in result["entities"]} == {"light.kitchen"}


@pytest.mark.asyncio
async def test_find_entities_excludes_disabled_by_default(hass: HomeAssistant):
    _register_entity(hass, "light.broken", disabled=True, state=None)
    _register_entity(hass, "light.working")

    default_result = find_entities(hass, domain="light")
    assert {e["entity_id"] for e in default_result["entities"]} == {"light.working"}

    with_disabled = find_entities(hass, domain="light", include_disabled=True)
    entity_ids = {e["entity_id"] for e in with_disabled["entities"]}
    assert entity_ids == {"light.broken", "light.working"}
    broken = next(e for e in with_disabled["entities"] if e["entity_id"] == "light.broken")
    assert broken["disabled"] is True


@pytest.mark.asyncio
async def test_find_entities_reports_live_state(hass: HomeAssistant):
    _register_entity(hass, "light.kitchen", state="on")

    result = find_entities(hass, domain="light")

    assert result["entities"][0]["state"] == "on"


@pytest.mark.asyncio
async def test_find_entities_truncates_and_flags_it(hass: HomeAssistant):
    for i in range(5):
        _register_entity(hass, f"light.bulb_{i}")

    result = find_entities(hass, domain="light", limit=2)

    assert len(result["entities"]) == 2
    assert result["truncated"] is True
