"""Tests for area/domain-scoped entity lookup (entity_manager.py)."""
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_dev_tools.entity_manager import entity_health_report, find_entities


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
    platform: str = "test",
) -> None:
    domain, object_id = entity_id.split(".", 1)
    entity_reg = er.async_get(hass)
    entity_reg.async_get_or_create(
        domain,
        platform,
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


@pytest.mark.asyncio
async def test_entity_health_report_buckets_by_integration_and_status(hass: HomeAssistant):
    _register_entity(hass, "light.hue_ok", platform="hue", state="on")
    _register_entity(hass, "light.hue_broken", platform="hue", state="unavailable")
    _register_entity(hass, "sensor.zwave_unknown", platform="zwave_js", state="unknown")

    report = entity_health_report(hass)

    assert report["by_integration"]["hue"] == {
        "total": 2,
        "disabled": 0,
        "hidden": 0,
        "unavailable": 1,
        "unknown": 0,
        "missing": 0,
        "ok": 1,
    }
    assert report["by_integration"]["zwave_js"]["unknown"] == 1
    problem_ids = {p["entity_id"] for p in report["problem_entities"]}
    assert problem_ids == {"light.hue_broken", "sensor.zwave_unknown"}


@pytest.mark.asyncio
async def test_entity_health_report_missing_state_vs_unavailable(hass: HomeAssistant):
    """An enabled entity with no state at all (integration not loaded) is 'missing', not 'unavailable'."""
    _register_entity(hass, "light.no_state_yet", platform="hue", state=None)

    report = entity_health_report(hass)

    assert report["by_integration"]["hue"]["missing"] == 1
    assert report["problem_entities"][0]["status"] == "missing"


@pytest.mark.asyncio
async def test_entity_health_report_disabled_entities_counted_and_flagged(hass: HomeAssistant):
    _register_entity(hass, "light.disabled_one", platform="hue", disabled=True, state=None)

    report = entity_health_report(hass)

    assert report["by_integration"]["hue"]["disabled"] == 1
    assert report["problem_entities"][0]["status"] == "disabled"


@pytest.mark.asyncio
async def test_entity_health_report_filters_by_integration(hass: HomeAssistant):
    _register_entity(hass, "light.hue_one", platform="hue", state="unavailable")
    _register_entity(hass, "sensor.zwave_one", platform="zwave_js", state="unavailable")

    report = entity_health_report(hass, integration="hue")

    assert set(report["by_integration"].keys()) == {"hue"}


@pytest.mark.asyncio
async def test_entity_health_report_filters_by_area(
    hass: HomeAssistant, bedroom_area, bedroom_device
):
    _register_entity(
        hass, "light.in_bedroom", platform="hue", device_id=bedroom_device.id, state="unavailable"
    )
    _register_entity(hass, "light.elsewhere", platform="hue", state="unavailable")

    report = entity_health_report(hass, area="Daughter's Bedroom")

    problem_ids = {p["entity_id"] for p in report["problem_entities"]}
    assert problem_ids == {"light.in_bedroom"}


@pytest.mark.asyncio
async def test_entity_health_report_unknown_area_returns_error(hass: HomeAssistant):
    report = entity_health_report(hass, area="Nonexistent Room")

    assert report["by_integration"] == {}
    assert "error" in report


@pytest.mark.asyncio
async def test_entity_health_report_truncates_problem_entities(hass: HomeAssistant):
    for i in range(5):
        _register_entity(hass, f"light.broken_{i}", platform="hue", state="unavailable")

    report = entity_health_report(hass, limit=2)

    assert len(report["problem_entities"]) == 2
    assert report["truncated"] is True
    # Counts in by_integration still reflect everything, only the sample list is capped.
    assert report["by_integration"]["hue"]["unavailable"] == 5
