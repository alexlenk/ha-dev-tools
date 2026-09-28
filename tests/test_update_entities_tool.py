"""Tests for the update_entities tool (issue #117): preview, apply, rename
references and mirroring, against real HA registries, files and persons."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools import dashboard_manager, helper_manager
from custom_components.ha_dev_tools.const import DOMAIN
from custom_components.ha_dev_tools.llm_api import UpdateEntitiesTool
from custom_components.ha_dev_tools.mirror import MirrorResult
from custom_components.ha_dev_tools.security import SecurityManager
from tests.test_llm_api import _llm_context

OLD = "device_tracker.iphone"
NEW = "device_tracker.alex_iphone_wifi"


async def _no_dashboards(*_):
    return []


@pytest.fixture
async def admin(hass: HomeAssistant, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dashboard_manager, "list_dashboards", _no_dashboards)
    hass.config.config_dir = str(tmp_path)
    hass.data.setdefault(DOMAIN, {})["security_manager"] = SecurityManager(hass, {})
    (tmp_path / "automations.yaml").write_text(
        f"- id: arrive\n  triggers:\n    - trigger: state\n      entity_id: {OLD}\n"
    )
    (tmp_path / "configuration.yaml").write_text(
        "automation: !include automations.yaml\n"
    )
    for component in ("websocket_api", "config", "person"):
        assert await async_setup_component(hass, component, {})
    er.async_get(hass).async_get_or_create(
        "device_tracker", "fritz", "phone1", suggested_object_id="iphone"
    )
    ar.async_get(hass).async_create("Garage")
    return MockUser(is_owner=True).add_to_hass(hass)


def _input(**args) -> llm.ToolInput:
    return llm.ToolInput(tool_name="update_entities", tool_args=args)


@pytest.mark.asyncio
async def test_preview_shows_changes_and_a_renames_references(
    hass: HomeAssistant, admin
):
    await helper_manager.create_helper(
        hass, admin, "person", {"name": "Alex", "device_trackers": [OLD]}
    )
    context = await UpdateEntitiesTool()._preview_context(
        hass,
        _input(items=[{"entity_id": OLD, "new_entity_id": NEW, "area": "garage"}]),
        _llm_context(admin.id),
    )

    assert context["would_change"] == [
        {
            "entity_id": OLD,
            "changes": {
                "new_entity_id": {"from": OLD, "to": NEW},
                "area": {"from": None, "to": "Garage"},
            },
        }
    ]
    refs = context["references"][OLD]
    assert refs["yaml"] == [
        {"path": "automations.yaml", "lines": [4], "writable": True}
    ]
    assert [person["name"] for person in refs["persons"]] == ["Alex"]
    assert "update_references=true" in context["references_note"]


@pytest.mark.asyncio
async def test_preview_reports_problems_and_skips_references_without_a_user(
    hass: HomeAssistant, admin
):
    tool = UpdateEntitiesTool()
    problems = await tool._preview_context(
        hass,
        _input(items=[{"entity_id": OLD, "area": "Attic"}]),
        _llm_context(admin.id),
    )
    assert "no room 'Attic' (rooms: Garage)" in problems["problems"]

    no_user = await tool._preview_context(
        hass,
        _input(items=[{"entity_id": OLD, "new_entity_id": NEW}]),
        _llm_context(None),
    )
    assert no_user["would_change"][0]["entity_id"] == OLD
    assert "references" not in no_user


@pytest.mark.asyncio
async def test_rename_with_update_references_rewrites_and_mirrors(
    hass: HomeAssistant, admin, tmp_path
):
    person = await helper_manager.create_helper(
        hass, admin, "person", {"name": "Alex", "device_trackers": [OLD]}
    )
    hass.services.async_register("automation", "reload", AsyncMock())
    mirror_write = AsyncMock(
        return_value=MirrorResult(mirrored=True, commits=("after",))
    )
    with (
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.is_mirror_enabled",
            return_value=True,
        ),
        patch(
            "custom_components.ha_dev_tools.llm_api.mirror.mirror_write", mirror_write
        ),
    ):
        result = await UpdateEntitiesTool()._write(
            hass,
            _input(
                items=[{"entity_id": OLD, "new_entity_id": NEW, "hidden": True}],
                update_references=True,
            ),
            _llm_context(admin.id),
        )

    assert result["results"][0]["entity_id"] == NEW
    assert er.async_get(hass).async_get(NEW).hidden_by is er.RegistryEntryHider.USER
    refs = result["references"][OLD]
    assert refs["rewritten"] == {"yaml": ["automations.yaml"], "persons": ["Alex"]}
    assert refs["reloaded"] == ["automation"]
    assert refs["still_referenced"]["yaml"] == []
    assert NEW in (tmp_path / "automations.yaml").read_text()
    persons = await helper_manager.list_helpers(hass, admin, "person")
    assert persons[0]["id"] == person["id"]
    assert persons[0]["device_trackers"] == [NEW]

    paths = [call.kwargs["path"] for call in mirror_write.await_args_list]
    assert paths[0] == "automations.yaml"
    assert paths[1].startswith("entities/update-")
    registry = mirror_write.await_args_list[1].kwargs
    assert json.loads(registry["content_before"])[0]["entity_id"] == OLD
    assert json.loads(registry["content_after"])[0]["entity_id"] == NEW
    assert result["mirror"]["mirrored"] is True


@pytest.mark.asyncio
async def test_rename_without_update_references_reports_what_still_points_at_it(
    hass: HomeAssistant, admin, tmp_path
):
    rewrite = AsyncMock()
    with patch(
        "custom_components.ha_dev_tools.llm_api.references.rewrite_references",
        rewrite,
    ):
        result = await UpdateEntitiesTool()._write(
            hass,
            _input(items=[{"entity_id": OLD, "new_entity_id": NEW}]),
            _llm_context(admin.id),
        )
    rewrite.assert_not_called()
    still = result["references"][OLD]["still_referenced"]
    assert still["yaml"][0]["path"] == "automations.yaml"
    assert OLD in (tmp_path / "automations.yaml").read_text()
    assert "mirror" not in result


@pytest.mark.asyncio
async def test_rewrite_errors_and_failed_renames_are_reported(
    hass: HomeAssistant, admin
):
    rewrite = AsyncMock(
        return_value=type(
            "R",
            (),
            {"rewritten": {}, "errors": ["x: no"], "reloaded": [], "mirror": []},
        )()
    )
    with patch(
        "custom_components.ha_dev_tools.llm_api.references.rewrite_references",
        rewrite,
    ):
        ok = await UpdateEntitiesTool()._write(
            hass,
            _input(
                items=[{"entity_id": OLD, "new_entity_id": NEW}], update_references=True
            ),
            _llm_context(admin.id),
        )
    assert ok["references"][OLD]["errors"] == ["x: no"]
    assert "reloaded" not in ok["references"][OLD]

    # A rename HA refused at write time: no reference handling for it.
    with patch(
        "custom_components.ha_dev_tools.llm_api.registry_manager.apply_updates",
        AsyncMock(return_value=[{"entity_id": NEW, "error": "refused"}]),
    ):
        failed = await UpdateEntitiesTool()._write(
            hass,
            _input(
                items=[{"entity_id": NEW, "new_entity_id": OLD}], update_references=True
            ),
            _llm_context(admin.id),
        )
    assert failed["results"] == [{"entity_id": NEW, "error": "refused"}]
    assert "references" not in failed


@pytest.mark.asyncio
async def test_write_refuses_an_invalid_batch_or_unknown_user(
    hass: HomeAssistant, admin
):
    tool = UpdateEntitiesTool()
    invalid = await tool._write(
        hass,
        _input(items=[{"entity_id": "light.nope", "name": "x"}]),
        _llm_context(admin.id),
    )
    assert invalid["error_type"] == "RegistryUpdateError"
    no_user = await tool._write(
        hass, _input(items=[{"entity_id": OLD, "name": "x"}]), _llm_context(None)
    )
    assert no_user["error_type"] == "UnresolvedUserError"


@pytest.mark.asyncio
async def test_a_failing_reference_lookup_never_hides_the_rename(
    hass: HomeAssistant, admin
):
    from custom_components.ha_dev_tools.ws_call import WebSocketCommandError

    with patch(
        "custom_components.ha_dev_tools.llm_api.references.find_references",
        AsyncMock(side_effect=WebSocketCommandError("unknown_command", "get_panels")),
    ):
        result = await UpdateEntitiesTool()._write(
            hass,
            _input(items=[{"entity_id": OLD, "new_entity_id": NEW}]),
            _llm_context(admin.id),
        )
    assert result["results"][0]["entity_id"] == NEW
    assert result["references"][OLD] == {
        "error": "references not checked: unknown_command: get_panels"
    }
