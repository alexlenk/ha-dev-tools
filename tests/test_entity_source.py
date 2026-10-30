"""describe_entity: where an entity is defined and which tools own it
(entity_source.py, issue #185)."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.ha_dev_tools.automation_manager import AutomationManager
from custom_components.ha_dev_tools.entity_source import EntitySourceResolver
from custom_components.ha_dev_tools.file_manager import FileManager
from custom_components.ha_dev_tools.llm_api import DescribeEntityTool
from custom_components.ha_dev_tools.platform_entry_manager import PlatformEntryManager
from custom_components.ha_dev_tools.script_manager import ScriptManager
from custom_components.ha_dev_tools.security import SecurityManager
from custom_components.ha_dev_tools.template_yaml_manager import TemplateYamlManager
from custom_components.ha_dev_tools.ws_call import WebSocketCommandError
from tests.test_llm_api import _arm, _llm_context

PACKAGE = """\
template:
  - unique_id: block
    sensor:
      - name: Occupancy score
        unique_id: occupancy_score
        state: "{{ 1 }}"
  - binary_sensor:
      - name: House occupied
        unique_id: house_occupied
        state: "{{ true }}"
      - name: No id here
        state: "{{ true }}"
sensor:
  - platform: statistics
    name: Kitchen distance avg
    unique_id: kitchen_distance_avg
    entity_id: sensor.kitchen_distance
    state_characteristic: mean
    max_age:
      minutes: 5
  - platform: statistics
    name: Hall distance avg
    entity_id: sensor.hall_distance
    state_characteristic: mean
    max_age:
      minutes: 5
"""


@pytest.fixture
def resolver(hass: HomeAssistant, tmp_path: Path) -> EntitySourceResolver:
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "house.yaml").write_text(PACKAGE)
    (tmp_path / "configuration.yaml").write_text(
        "template:\n  - sensor:\n      - name: Core temp\n"
        "        unique_id: core_temp\n        state: '{{ 1 }}'\n"
    )
    (tmp_path / "automations.yaml").write_text(
        "- id: lights_on\n  alias: Lights on\n  triggers: []\n  actions: []\n"
    )
    (tmp_path / "scripts.yaml").write_text("bedtime:\n  sequence: []\n")
    files = FileManager(
        hass,
        SecurityManager(
            hass,
            {
                "read_paths": [
                    "configuration.yaml",
                    "automations.yaml",
                    "scripts.yaml",
                    "packages/**/*.yaml",
                ],
                "write_paths": ["packages/**/*.yaml"],
                "denied_paths": [],
            },
        ),
    )
    return EntitySourceResolver(
        hass,
        AutomationManager(hass, files),
        ScriptManager(hass, files),
        TemplateYamlManager(hass, files),
        PlatformEntryManager(hass, files),
    )


@pytest.fixture
async def user(hass: HomeAssistant):
    return MockUser(is_owner=True).add_to_hass(hass)


def _register(
    hass: HomeAssistant,
    domain: str,
    platform: str,
    unique_id: str,
    object_id: str,
    *,
    config_entry=None,
    attributes=None,
) -> str:
    entry = er.async_get(hass).async_get_or_create(
        domain,
        platform,
        unique_id,
        suggested_object_id=object_id,
        config_entry=config_entry,
    )
    hass.states.async_set(entry.entity_id, "on", attributes or {})
    return entry.entity_id


def _tools(found: dict) -> list[str]:
    return [tool["tool"] for tool in found["defined_in"]["tools"]]


async def test_ui_template_helper(hass: HomeAssistant, resolver, user):
    helper = MockConfigEntry(domain="template", title="House occupancy")
    helper.add_to_hass(hass)
    entity_id = _register(
        hass, "binary_sensor", "template", helper.entry_id, "house", config_entry=helper
    )

    found = await resolver.describe(entity_id, user)

    assert found["registry"]["config_entry_id"] == helper.entry_id
    assert found["defined_in"]["kind"] == "ui_helper"
    assert found["defined_in"]["helper_type"] == "template"
    assert _tools(found) == [
        "get_derived_sensor",
        "update_derived_sensor",
        "delete_derived_sensor",
        "update_entities",
    ]
    assert found["defined_in"]["tools"][0]["args"] == {"entry_id": helper.entry_id}


async def test_integration_entity(hass: HomeAssistant, resolver, user):
    shelly = MockConfigEntry(domain="shelly", title="Water meter")
    shelly.add_to_hass(hass)
    entity_id = _register(hass, "sensor", "shelly", "abc", "water", config_entry=shelly)

    found = await resolver.describe(entity_id, user)

    assert found["defined_in"]["kind"] == "integration"
    assert found["defined_in"]["integration"] == "shelly"
    assert _tools(found) == ["write_customize", "update_entities"]
    assert found["defined_in"]["tools"][0]["args"] == {"entities": {entity_id: {}}}


async def test_automation_and_script(hass: HomeAssistant, resolver, user):
    automation = _register(
        hass,
        "automation",
        "automation",
        "lights_on",
        "lights_on",
        attributes={"id": "lights_on"},
    )
    found = await resolver.describe(automation, user)
    assert found["defined_in"]["automation_id"] == "lights_on"
    assert found["defined_in"]["files"] == ["automations.yaml"]
    assert "set_automation_enabled" in _tools(found)

    no_id = _register(hass, "automation", "automation", "x", "legacy")
    assert (await resolver.describe(no_id, user))["defined_in"]["note"].startswith(
        "no config id"
    )

    script = _register(hass, "script", "script", "bedtime", "bedtime")
    found = await resolver.describe(script, user)
    assert found["defined_in"]["script_id"] == "bedtime"
    assert found["defined_in"]["files"] == ["scripts.yaml"]


async def test_helpers_storage_or_yaml(hass: HomeAssistant, resolver, user):
    stored = _register(hass, "input_boolean", "input_boolean", "guest", "guest")
    yaml = _register(hass, "input_boolean", "input_boolean", "vacation", "vacation")
    lister = AsyncMock(return_value=[{"id": "guest"}])
    with patch(
        "custom_components.ha_dev_tools.entity_source.helper_manager.list_helpers",
        lister,
    ):
        found = await resolver.describe(stored, user)
        assert found["defined_in"]["kind"] == "storage_helper"
        assert found["defined_in"]["tools"][1]["args"] == {
            "domain": "input_boolean",
            "item_id": "guest",
        }
        assert (await resolver.describe(yaml, user))["defined_in"][
            "kind"
        ] == "yaml_helper"
        lister.side_effect = WebSocketCommandError("not_loaded", "nope")
        failed = await resolver.describe(stored, user)
        assert "nope" in failed["defined_in"]["error"]


async def test_yaml_template_entities(hass: HomeAssistant, resolver, user):
    in_block = _register(hass, "sensor", "template", "block-occupancy_score", "score")
    found = await resolver.describe(in_block, user)
    assert found["defined_in"]["kind"] == "yaml_template"
    assert found["defined_in"]["locations"] == [
        {
            "file_path": "packages/house.yaml",
            "block_index": 0,
            "platform": "sensor",
            "entity_index": 0,
            "unique_id": "occupancy_score",
        }
    ]
    assert found["defined_in"]["tools"][0] == {
        "tool": "get_template_entity",
        "args": {"unique_id": "occupancy_score"},
    }
    assert "note" not in found["defined_in"]

    plain = _register(hass, "binary_sensor", "template", "house_occupied", "occ")
    assert (await resolver.describe(plain, user))["defined_in"]["locations"][0][
        "block_index"
    ] == 1

    core = _register(hass, "sensor", "template", "core_temp", "core")
    assert "read-only" in (await resolver.describe(core, user))["defined_in"]["note"]

    gone = _register(hass, "sensor", "template", "elsewhere", "elsewhere")
    assert "not found" in (await resolver.describe(gone, user))["defined_in"]["note"]


async def test_yaml_platform_entries(hass: HomeAssistant, resolver, user):
    kitchen = _register(hass, "sensor", "statistics", "kitchen_distance_avg", "k")
    found = await resolver.describe(kitchen, user)
    assert found["defined_in"]["kind"] == "package_platform_entry"
    assert found["defined_in"]["file_path"] == "packages/house.yaml"
    assert found["defined_in"]["index"] == 0

    outside = _register(hass, "sensor", "statistics", "in_configuration", "o")
    found = await resolver.describe(outside, user)
    assert found["defined_in"]["kind"] == "yaml_platform"
    assert found["defined_in"]["tools"][0] == {
        "tool": "get_config_file",
        "args": {"key": "sensor"},
    }


async def test_entities_without_unique_id(hass: HomeAssistant, resolver, user):
    hass.states.async_set("binary_sensor.no_id", "on", {"friendly_name": "No id here"})
    hass.states.async_set("sensor.hall", "3", {"friendly_name": "Hall distance avg"})
    hass.states.async_set("sensor.runtime", "1")

    found = await resolver.describe("binary_sensor.no_id", user)
    assert found["registry"] is None
    assert found["defined_in"]["kind"] == "no_unique_id"
    assert found["defined_in"]["candidates"] == [
        {
            "file_path": "packages/house.yaml",
            "block_index": 1,
            "platform": "binary_sensor",
            "entity_index": 1,
            "unique_id": None,
        }
    ]
    hall = await resolver.describe("sensor.hall", user)
    assert hall["defined_in"]["candidates"] == [
        {
            "file_path": "packages/house.yaml",
            "domain": "sensor",
            "index": 1,
            "platform": "statistics",
        }
    ]
    runtime = await resolver.describe("sensor.runtime", user)
    assert runtime["defined_in"]["candidates"] == []

    with pytest.raises(LookupError):
        await resolver.describe("sensor.nothing", user)


async def test_describe_entity_tool(hass: HomeAssistant, resolver, user):
    _arm(hass)
    tool = DescribeEntityTool(resolver)
    hass.states.async_set("sensor.runtime", "1")

    found = await tool.async_call(
        hass,
        llm.ToolInput(
            tool_name="describe_entity", tool_args={"entity_id": "sensor.runtime"}
        ),
        _llm_context(user.id),
    )
    assert found.error is False
    assert found.data["defined_in"]["kind"] == "no_unique_id"

    missing = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="describe_entity", tool_args={"entity_id": "sensor.x"}),
        _llm_context(user.id),
    )
    assert missing.error is True and missing.data["error_type"] == "LookupError"
