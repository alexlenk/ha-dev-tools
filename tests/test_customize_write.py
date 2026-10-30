"""write_customize: `homeassistant: customize:` in packages (issue #183),
against HA's own core config reload and config check."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools.config_tools import reload_domain
from custom_components.ha_dev_tools.customize_write import (
    CustomizeError,
    CustomizeWriter,
    check_value,
    combination_warnings,
)
from custom_components.ha_dev_tools.file_manager import FileManager
from custom_components.ha_dev_tools.llm_api import WriteCustomizeTool
from custom_components.ha_dev_tools.security import SecurityManager
from tests.test_llm_api import _arm, _llm_context

CONFIG = "homeassistant:\n  packages: !include_dir_named packages\n"
STATS = """\
# Long-term statistics for integration sensors.
homeassistant:
  customize:
    sensor.pressure:  # companion app
      state_class: measurement
"""


@pytest.fixture
async def writer(hass: HomeAssistant, tmp_path: Path) -> CustomizeWriter:
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text(CONFIG)
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "stats.yaml").write_text(STATS)
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(
        hass,
        "template",
        {"template": [{"sensor": [{"name": "Water", "state": "{{ 12 }}"}]}]},
    )
    await hass.async_block_till_done()
    files = FileManager(
        hass,
        SecurityManager(
            hass,
            {
                "read_paths": ["configuration.yaml"],
                "write_paths": ["packages/**/*.yaml"],
                "denied_paths": [],
            },
        ),
    )
    return CustomizeWriter(hass, files)


def test_check_value():
    assert check_value("sensor.x", "state_class", "total_increasing") is None
    assert check_value("sensor.x", "state_class", None) is None
    assert "one of" in check_value("sensor.x", "state_class", "sum")
    assert "sensors only" in check_value("binary_sensor.x", "state_class", "total")
    assert check_value("binary_sensor.x", "device_class", "door") is None
    assert "isn't a sensor device_class" in check_value(
        "sensor.x", "device_class", "door"
    )
    assert "no device classes" in check_value("light.x", "device_class", "x")
    assert "can't be customized" in check_value("sensor.x", "hidden", True)
    assert check_value("switch.x", "assumed_state", True) is None
    assert "true or false" in check_value("switch.x", "assumed_state", "yes")
    assert check_value("sensor.x", "icon", "mdi:water") is None
    assert "prefix:name" in check_value("sensor.x", "icon", "water")
    assert "non-empty string" in check_value("sensor.x", "friendly_name", "")


def test_combination_warnings():
    assert combination_warnings("switch.x", {"device_class": "outlet"}) == []
    assert (
        combination_warnings(
            "sensor.x", {"device_class": "energy", "state_class": "total_increasing"}
        )
        == []
    )
    [warning] = combination_warnings(
        "sensor.x", {"device_class": "energy", "state_class": "measurement"}
    )
    assert "doesn't take state_class measurement" in warning
    [unit] = combination_warnings(
        "sensor.x", {"device_class": "water", "unit_of_measurement": "kWh"}
    )
    assert "doesn't take unit 'kWh'" in unit
    assert combination_warnings("sensor.x", {"device_class": "nonsense"}) == []


async def test_plan_refusals(hass: HomeAssistant, writer, tmp_path):
    (tmp_path / "packages" / "other.yaml").write_text(
        "homeassistant:\n  customize:\n    sensor.pressure:\n      icon: mdi:gauge\n"
        "    sensor.solo:\n      icon: mdi:gauge\n"
    )
    with pytest.raises(CustomizeError) as err:
        await writer.plan(
            {
                "nonsense": {"icon": "mdi:x"},
                "sensor.empty": {},
                "sensor.bad": {"state_class": "sum", "hidden": True},
                "sensor.pressure": {"icon": "mdi:x"},
            },
            package="stats.yaml",
        )
    message = str(err.value)
    assert "'nonsense' isn't an entity_id" in message
    assert "sensor.empty: give at least one attribute" in message
    assert "sensor.bad: state_class is one of" in message
    assert "sensor.bad: 'hidden' can't be customized" in message
    assert "sensor.pressure is customized in 2 files" in message

    with pytest.raises(CustomizeError, match="one file per call"):
        await writer.plan({"sensor.solo": {"icon": "mdi:x"}}, package="stats.yaml")
    with pytest.raises(CustomizeError, match="pass package"):
        await writer.plan({"sensor.new": {"icon": "mdi:x"}})
    with pytest.raises(CustomizeError, match="entities is empty"):
        await writer.plan({})
    with pytest.raises(CustomizeError, match="isn't customized in"):
        await writer.plan({"sensor.solo": {"state_class": None}})

    (tmp_path / "configuration.yaml").write_text(
        CONFIG + "  customize:\n    sensor.core:\n      icon: mdi:x\n"
    )
    with pytest.raises(CustomizeError, match="configuration.yaml, which stays"):
        await writer.plan({"sensor.core": {"icon": "mdi:y"}})


async def test_create_change_and_remove(hass: HomeAssistant, writer, tmp_path):
    new_file = tmp_path / "packages" / "water.yaml"

    plan = await writer.plan(
        {
            "sensor.water": {
                "state_class": "total_increasing",
                "device_class": "water",
                "unit_of_measurement": "L",
            },
            "sensor.garden": {"device_class": "energy", "state_class": "measurement"},
        },
        package="water.yaml",
    )
    preview = plan.preview()
    assert (
        preview["creates_file"] is True
        and preview["file_path"] == "packages/water.yaml"
    )
    water = preview["entities"][0]
    assert water["state"] == "12" and water["attributes_now"]["state_class"] is None
    assert water["customize_before"] is None
    assert any("sensor.garden doesn't exist now" in w for w in preview["warnings"])
    assert any("doesn't take state_class measurement" in w for w in preview["warnings"])

    applied = await writer.apply(plan)
    assert applied["created_file"] is True
    entry = applied["entities"][0]
    assert entry["applied"] is True
    assert entry["attributes_now"]["state_class"] == "total_increasing"
    assert hass.states.get("sensor.water").attributes["unit_of_measurement"] == "L"
    assert applied["entities"][1] == {
        "entity_id": "sensor.garden",
        "attributes_now": None,
        "applied": False,
    }
    assert "homeassistant:\n  customize:\n    sensor.water:" in new_file.read_text()

    # Changed where it is (no package needed); the comment survives.
    hass.states.async_set("sensor.pressure", "1013.2")
    change = await writer.plan({"sensor.pressure": {"icon": "mdi:gauge"}})
    assert change.file_path == "packages/stats.yaml"
    await writer.apply(change)
    content = (tmp_path / "packages" / "stats.yaml").read_text()
    assert "# companion app" in content and "icon: mdi:gauge" in content

    # Removing a key, then the last key - the entry and its block go.
    removed = await writer.apply(await writer.plan({"sensor.pressure": {"icon": None}}))
    assert removed["entities"][0]["removed"] == ["icon"]
    await writer.apply(await writer.plan({"sensor.pressure": {"state_class": None}}))
    assert (tmp_path / "packages" / "stats.yaml").read_text() == (
        "# Long-term statistics for integration sensors.\n"
    )
    await writer.apply(
        await writer.plan(
            {"sensor.garden": {"device_class": None, "state_class": None}}
        )
    )
    assert "sensor.garden" not in new_file.read_text()


async def test_warns_on_a_non_numeric_state(hass: HomeAssistant, writer):
    hass.states.async_set("sensor.mode", "eco")
    plan = await writer.plan(
        {"sensor.mode": {"state_class": "measurement"}}, package="m.yaml"
    )
    assert any("isn't a number" in warning for warning in plan.warnings)


async def test_a_new_config_error_rolls_back(hass: HomeAssistant, writer, tmp_path):
    stats = tmp_path / "packages" / "stats.yaml"
    checks = [
        SimpleNamespace(errors=[]),
        SimpleNamespace(errors=[SimpleNamespace(message="bad")]),
    ]

    with patch.object(writer, "_check", AsyncMock(side_effect=checks * 2)):
        with pytest.raises(CustomizeError, match="rolled back: bad"):
            await writer.apply(
                await writer.plan({"sensor.pressure": {"icon": "mdi:gauge"}})
            )
        assert stats.read_text() == STATS
        with pytest.raises(CustomizeError, match="rolled back"):
            await writer.apply(
                await writer.plan(
                    {"sensor.water": {"icon": "mdi:x"}}, package="new.yaml"
                )
            )
    assert not (tmp_path / "packages" / "new.yaml").exists()


async def test_update_entity_failure_is_reported(hass: HomeAssistant, writer):
    from homeassistant.exceptions import HomeAssistantError

    async def update_entity(call):
        raise HomeAssistantError("gone")

    hass.services.async_register("homeassistant", "update_entity", update_entity)
    hass.states.async_set("sensor.pressure", "1013")
    applied = await writer.apply(
        await writer.plan({"sensor.pressure": {"icon": "mdi:gauge"}})
    )
    assert applied["entities"][0]["update_error"] == "gone"


async def test_customized_without_packages_or_configuration(
    hass: HomeAssistant, tmp_path
):
    hass.config.config_dir = str(tmp_path)
    files = FileManager(
        hass,
        SecurityManager(
            hass,
            {
                "read_paths": ["configuration.yaml"],
                "write_paths": ["packages/**/*.yaml"],
                "denied_paths": [],
            },
        ),
    )
    assert await CustomizeWriter(hass, files).customized() == {}


def test_build_keeps_other_core_keys():
    from custom_components.ha_dev_tools.customize_write import _build

    content = (
        "homeassistant:\n  customize_glob:\n    'sensor.*_x':\n      icon: mdi:x\n"
        "  customize:\n    sensor.a:\n      icon: mdi:a\n"
    )
    assert _build(content, "sensor.other", None) == content
    assert _build(content, "sensor.a", None) == (
        "homeassistant:\n  customize_glob:\n    'sensor.*_x':\n      icon: mdi:x\n"
    )


async def test_write_customize_tool(hass: HomeAssistant, writer, tmp_path):
    user = MockUser(is_owner=True).add_to_hass(hass)
    _arm(hass)
    tool = WriteCustomizeTool(writer)

    refused = await tool.async_call(
        hass,
        llm.ToolInput(
            tool_name="write_customize",
            tool_args={"entities": {"sensor.water": {"hidden": True}}},
        ),
        _llm_context(user.id),
    )
    assert refused.error is True and "confirm_token" not in refused.data

    args = {
        "entities": {"sensor.water": {"state_class": "total_increasing"}},
        "package": "water.yaml",
    }
    proposal = (
        await tool.async_call(
            hass,
            llm.ToolInput(tool_name="write_customize", tool_args=args),
            _llm_context(user.id),
        )
    ).data
    assert proposal["would_write"]["creates_file"] is True
    done = await tool.async_call(
        hass,
        llm.ToolInput(
            tool_name="write_customize",
            tool_args={**args, "confirm_token": proposal["confirm_token"]},
        ),
        _llm_context(user.id),
    )
    assert done.error is False
    assert done.data["customized"]["entities"][0]["applied"] is True

    failed = await tool._write(
        hass,
        llm.ToolInput(
            tool_name="write_customize",
            tool_args={"entities": {"sensor.water": {"icon": "nope"}}},
        ),
        _llm_context(user.id),
    )
    assert failed["error_type"] == "CustomizeError"


async def test_reload_domain_homeassistant(hass: HomeAssistant, writer):
    result = await reload_domain(hass, "homeassistant")
    assert result["service"] == "homeassistant.reload_core_config"
