"""Tests for references.py: finding and rewriting references to a renamed
entity_id (issue #117), against real HA dashboards, persons and files."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.ha_dev_tools import dashboard_manager, helper_manager
from custom_components.ha_dev_tools.const import DOMAIN
from custom_components.ha_dev_tools.references import (
    _replace,
    find_references,
    pattern,
    rewrite_references,
)
from custom_components.ha_dev_tools.security import SecurityManager

OLD = "device_tracker.iphone"
NEW = "device_tracker.alex_iphone_wifi"

_AUTOMATIONS = (
    "- id: arrive  # home\n"
    "  triggers:\n"
    "    - trigger: state\n"
    f"      entity_id: {OLD}\n"
    "      to: home\n"
    "  actions:\n"
    "    - action: notify.x\n"
    "      data:\n"
    f"        message: \"{{{{ states('{OLD}') }}}} / {OLD}_2\"\n"
)
_CONFIGURATION = (
    "automation: !include automations.yaml\n"
    "homeassistant:\n"
    "  packages: !include_dir_named packages\n"
    "template:\n"
    "  - sensor:\n"
    f'      - state: "{{{{ states.{OLD}.state }}}}"\n'
)


def test_pattern_matches_whole_tokens_only():
    regex = pattern("sensor.power")
    assert regex.search("states('sensor.power')")
    assert regex.search("{{ states.sensor.power.state }}")
    assert regex.search("entity_id: sensor.power")
    assert not regex.search("sensor.power_total")
    assert not regex.search("binary_sensor.power")
    assert not regex.search("sensor.powerwall")


async def _default_dashboard_only(*_):
    """get_panels comes from the frontend, not loaded in tests (see
    test_dashboard_manager.py): one storage-mode default dashboard."""
    return [{"url_path": "lovelace", "mode": "storage"}]


def test_replace_walks_a_dashboard_config_leaving_other_values():
    config = {"views": [{"columns": 2, "show": True, "icon": None, "e": [OLD]}]}
    replaced, count = _replace(config, pattern(OLD), NEW)
    assert count == 1
    assert replaced == {
        "views": [{"columns": 2, "show": True, "icon": None, "e": [NEW]}]
    }


@pytest.fixture
async def user(hass: HomeAssistant, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dashboard_manager, "list_dashboards", _default_dashboard_only)
    hass.config.config_dir = str(tmp_path)
    hass.data.setdefault(DOMAIN, {})["security_manager"] = SecurityManager(hass, {})
    (tmp_path / "configuration.yaml").write_text(_CONFIGURATION)
    (tmp_path / "automations.yaml").write_text(_AUTOMATIONS)
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages/presence.yaml").write_text(
        f"script:\n  x:\n    sequence:\n      - condition: state\n"
        f"        entity_id: {OLD}\n        state: home\n"
    )
    (tmp_path / "packages/unrelated.yaml").write_text("input_boolean: {}\n")
    for component in ("websocket_api", "config", "person", "lovelace"):
        assert await async_setup_component(hass, component, {})
    return MockUser(is_owner=True).add_to_hass(hass)


async def _with_dashboard_and_person(hass: HomeAssistant, user):
    await dashboard_manager.write_dashboard(
        hass,
        user,
        {"views": [{"cards": [{"type": "entities", "entities": [OLD, "sun.sun"]}]}]},
    )
    person = await helper_manager.create_helper(
        hass,
        user,
        "person",
        {"name": "Alex", "device_trackers": [OLD, "device_tracker.ipad"]},
    )
    MockConfigEntry(
        domain="group", title="Phones", options={"entities": [OLD, "b.c"]}
    ).add_to_hass(hass)
    return person


@pytest.mark.asyncio
async def test_find_references_everywhere(hass: HomeAssistant, user):
    person = await _with_dashboard_and_person(hass, user)

    refs = await find_references(hass, user, OLD)

    assert refs["yaml"] == [
        {"path": "configuration.yaml", "lines": [6], "writable": False},
        {"path": "automations.yaml", "lines": [4, 9], "writable": True},
        {"path": "packages/presence.yaml", "lines": [5], "writable": True},
    ]
    assert refs["dashboards"] == [{"url_path": None, "mode": "storage", "count": 1}]
    assert refs["persons"] == [{"id": person["id"], "name": "Alex"}]
    assert [entry["domain"] for entry in refs["config_entries"]] == ["group"]


@pytest.mark.asyncio
async def test_rewrite_changes_only_the_id_and_reloads(
    hass: HomeAssistant, user, tmp_path
):
    person = await _with_dashboard_and_person(hass, user)
    reload = AsyncMock()
    hass.services.async_register("automation", "reload", reload)

    result = await rewrite_references(hass, user, OLD, NEW)

    assert result.errors == []
    assert result.rewritten == {
        "yaml": ["automations.yaml", "packages/presence.yaml"],
        "dashboards": ["default"],
        "persons": ["Alex"],
    }
    # Byte-for-byte the same file, only the id replaced - `_2` untouched.
    assert (tmp_path / "automations.yaml").read_text() == _AUTOMATIONS.replace(
        f"{OLD}\n", f"{NEW}\n"
    ).replace(f"states('{OLD}')", f"states('{NEW}')")
    assert f"{OLD}_2" in (tmp_path / "automations.yaml").read_text()
    # Read-only: listed, never written.
    assert (tmp_path / "configuration.yaml").read_text() == _CONFIGURATION
    config = await dashboard_manager.get_dashboard(hass, user)
    assert config["views"][0]["cards"][0]["entities"] == [NEW, "sun.sun"]
    persons = await helper_manager.list_helpers(hass, user, "person")
    assert persons[0]["id"] == person["id"]
    assert persons[0]["device_trackers"] == [NEW, "device_tracker.ipad"]
    assert result.reloaded == ["automation"]
    reload.assert_awaited_once()
    assert [(item.path, item.content_type) for item in result.mirror] == [
        ("automations.yaml", "yaml"),
        ("packages/presence.yaml", "yaml"),
        (".storage/lovelace", "json"),
    ]

    # What's left: only what can't be rewritten here.
    left = await find_references(hass, user, OLD)
    assert [ref["path"] for ref in left["yaml"]] == ["configuration.yaml"]
    assert left["dashboards"] == left["persons"] == []


@pytest.mark.asyncio
async def test_rewrite_reports_failures_and_skips_yaml_dashboards(
    hass: HomeAssistant, user, tmp_path, monkeypatch
):
    await _with_dashboard_and_person(hass, user)
    # An automations.yaml the rewrite would break: refused, reported.
    (tmp_path / "automations.yaml").write_text(f"- id: a\n  x: [{OLD}\n")

    async def dashboards(*_):
        return [
            {"url_path": "lovelace", "mode": "storage"},
            {"url_path": "yaml-one", "mode": "yaml"},
            {"url_path": "never-saved", "mode": "storage"},
        ]

    async def get(hass, user, *, url_path=None):
        if url_path == "never-saved":
            raise dashboard_manager.WebSocketCommandError("config_not_found", "x")
        return {"views": [{"entities": [OLD]}]}

    async def fail(*_args, **_kwargs):
        raise dashboard_manager.WebSocketCommandError("home_assistant_error", "no")

    monkeypatch.setattr(dashboard_manager, "list_dashboards", dashboards)
    monkeypatch.setattr(dashboard_manager, "get_dashboard", get)
    monkeypatch.setattr(dashboard_manager, "write_dashboard", fail)
    monkeypatch.setattr(helper_manager, "update_helper", fail)

    result = await rewrite_references(hass, user, OLD, NEW)

    assert result.rewritten.get("yaml") == ["packages/presence.yaml"]
    assert any(error.startswith("automations.yaml:") for error in result.errors)
    assert "dashboard default: home_assistant_error: no" in result.errors
    assert "person Alex: home_assistant_error: no" in result.errors
    assert "dashboards" not in result.rewritten  # yaml-one never attempted


@pytest.mark.asyncio
async def test_no_person_component_means_no_person_references(
    hass: HomeAssistant, tmp_path, monkeypatch
):
    monkeypatch.setattr(dashboard_manager, "list_dashboards", _default_dashboard_only)
    hass.config.config_dir = str(tmp_path)
    hass.data.setdefault(DOMAIN, {})["security_manager"] = SecurityManager(hass, {})
    for component in ("websocket_api", "config", "lovelace"):
        assert await async_setup_component(hass, component, {})
    user = MockUser(is_owner=True).add_to_hass(hass)
    assert await find_references(hass, user, OLD) == {
        "yaml": [],
        "dashboards": [],
        "persons": [],
        "config_entries": [],
    }


@pytest.mark.asyncio
async def test_unreadable_yaml_is_skipped(hass: HomeAssistant, user, tmp_path):
    (tmp_path / "packages/binary.yaml").write_bytes(b"\xff\xfe")
    refs = await find_references(hass, user, OLD)
    assert "packages/binary.yaml" not in [ref["path"] for ref in refs["yaml"]]
