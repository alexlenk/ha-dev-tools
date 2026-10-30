"""New package files from the write tools, and delete_package (issue #189)."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools import packages
from custom_components.ha_dev_tools.automation_manager import AutomationManager
from custom_components.ha_dev_tools.customize_write import CustomizeWriter
from custom_components.ha_dev_tools.file_manager import FileManager
from custom_components.ha_dev_tools.llm_api import DeletePackageTool
from custom_components.ha_dev_tools.platform_entry_manager import (
    PlatformEntryError,
    PlatformEntryManager,
)
from custom_components.ha_dev_tools.rest_command_manager import RestCommandManager
from custom_components.ha_dev_tools.rest_command_write import RestCommandWriter
from custom_components.ha_dev_tools.script_manager import ScriptManager
from custom_components.ha_dev_tools.security import SecurityManager
from custom_components.ha_dev_tools.template_yaml_manager import (
    TemplateEntityNotFoundError,
    TemplateYamlManager,
)
from tests.test_llm_api import _arm, _llm_context

LOADED = "homeassistant:\n  packages: !include_dir_named packages\n"


@pytest.fixture
def files(hass: HomeAssistant, tmp_path: Path) -> FileManager:
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text(LOADED)
    (tmp_path / "packages" / "heating").mkdir(parents=True)
    (tmp_path / "packages" / "heating" / "rooms.yaml").write_text("sensor: []\n")
    for name in ("automations.yaml", "scripts.yaml"):
        (tmp_path / name).write_text("")
    return FileManager(
        hass,
        SecurityManager(
            hass,
            {
                "read_paths": ["configuration.yaml"],
                "write_paths": [
                    "automations.yaml",
                    "scripts.yaml",
                    "packages/**/*.yaml",
                ],
                "denied_paths": [],
            },
        ),
    )


def test_packages_loader():
    assert packages.packages_loader(LOADED) == ("!include_dir_named", "packages")
    assert packages.packages_loader(
        "homeassistant:\n  packages: !include_dir_named ./packages/\n"
    ) == ("!include_dir_named", "packages")
    assert (
        packages.packages_loader(
            "homeassistant:\n  packages: !include_dir_merge_named packages\n"
        )[0]
        == "!include_dir_merge_named"
    )
    assert packages.packages_loader(
        "homeassistant:\n  packages:\n    a: !include packages/a.yaml\n"
    ) == (None, None)
    assert packages.packages_loader("homeassistant:\n  name: Home\n") == (None, None)
    assert packages.packages_loader("") == (None, None)


def test_is_empty_and_keys():
    assert packages.is_empty("")
    assert packages.is_empty("# nothing left\n\n")
    assert packages.is_empty("{}\n")
    assert not packages.is_empty("sensor: []\n")
    assert packages.top_level_keys("sensor: []\ntemplate: []\n") == [
        "sensor",
        "template",
    ]
    assert packages.top_level_keys("- a\n") == ["?"]


async def test_new_package_problem(hass: HomeAssistant, files, tmp_path):
    check = packages.new_package_problem
    assert await check(hass, files, "packages/house_state.yaml") is None
    assert await check(hass, files, "packages/heating/boiler.yaml") is None
    for bad in ("packages/House State.yaml", "packages/x.yml", "packages/a-b.yaml"):
        assert "lowercase letters" in await check(hass, files, bad)
    assert "named 'rooms' already exists" in await check(
        hass, files, "packages/rooms.yaml"
    )

    config = tmp_path / "configuration.yaml"
    config.write_text("homeassistant:\n  packages: !include_dir_merge_named packages\n")
    assert "!include_dir_merge_named" in await check(hass, files, "packages/new.yaml")
    config.write_text("homeassistant:\n  name: Home\n")
    assert "doesn't load packages/" in await check(hass, files, "packages/new.yaml")
    config.unlink()
    assert "can't check" in await check(hass, files, "packages/new.yaml")


async def test_writers_create_the_package(hass: HomeAssistant, files, tmp_path):
    for domain in ("automation", "script", "template", "rest_command"):
        hass.services.async_register(domain, "reload", AsyncMock())
    automation = await AutomationManager(hass, files).write_automation(
        "lights",
        {"alias": "Lights", "triggers": [], "actions": []},
        package="lights.yaml",
    )
    assert automation.location.file_path == "packages/lights.yaml"
    assert "alias: Lights" in (tmp_path / "packages" / "lights.yaml").read_text()

    await ScriptManager(hass, files).write_script(
        "bedtime", {"sequence": []}, package="night/bedtime.yaml"
    )
    assert (tmp_path / "packages" / "night" / "bedtime.yaml").is_file()

    templates = TemplateYamlManager(hass, files)
    created = await templates.create_entity(
        "sensor",
        {"name": "Cleaner", "unique_id": "cleaner", "state": "{{ 1 }}"},
        package="house_state.yaml",
    )
    assert created.content_before is None
    assert (
        "unique_id: cleaner" in (tmp_path / "packages" / "house_state.yaml").read_text()
    )
    with pytest.raises(TemplateEntityNotFoundError, match="already exists"):
        await templates.create_entity(
            "sensor",
            {"name": "X", "unique_id": "x", "state": "1"},
            package="other/rooms.yaml",
        )

    platforms = PlatformEntryManager(hass, files)
    location, path, before, after = await platforms.plan_write(
        "sensor",
        {"platform": "history_stats", "name": "Hours", "entity_id": "x"},
        package="cleaning.yaml",
    )
    assert (location, path, before) == (None, "packages/cleaning.yaml", None)
    assert "platform: history_stats" in after
    with pytest.raises(PlatformEntryError, match="lowercase letters"):
        await platforms.plan_write(
            "sensor", {"platform": "x", "name": "Y"}, package="Bad Name.yaml"
        )

    writer = RestCommandWriter(hass, RestCommandManager(hass, files))
    plan = await writer.plan_write(
        "ping",
        {"url": "http://192.168.1.2/ping"},
        package="network.yaml",
        allow_new_host=True,
    )
    assert plan.content_before is None and "ping:" in plan.content_after

    customize = CustomizeWriter(hass, files)
    plan = await customize.plan(
        {"sensor.water": {"icon": "mdi:water"}}, package="looks.yaml"
    )
    assert plan.content_before is None
    with pytest.raises(ValueError, match="already exists"):
        await customize.plan(
            {"sensor.water": {"icon": "mdi:water"}}, package="rooms.yaml"
        )


async def test_delete_package(hass: HomeAssistant, files, tmp_path):
    admin = MockUser(is_owner=True).add_to_hass(hass)
    _arm(hass)
    tool = DeletePackageTool(files)

    async def call(args):
        return (
            await tool.async_call(
                hass,
                llm.ToolInput(tool_name="delete_package", tool_args=args),
                _llm_context(admin.id),
            )
        ).data

    full = await call({"package": "heating/rooms.yaml"})
    assert "still holds configuration (sensor)" in full["error"]
    assert "there is no" in (await call({"package": "gone.yaml"}))["error"]
    assert "not '../x.yaml'" in (await call({"package": "../x.yaml"}))["error"]

    empty = tmp_path / "packages" / "old.yaml"
    empty.write_text("# retired\n")
    proposal = await call({"package": "old.yaml"})
    assert proposal["would_delete"] == "packages/old.yaml"
    assert proposal["content"] == "# retired\n"
    done = await call(
        {"package": "old.yaml", "confirm_token": proposal["confirm_token"]}
    )
    assert done["deleted"] == "packages/old.yaml"
    assert not empty.exists()


def test_no_packages_folder_yet(tmp_path):
    assert packages._stems(tmp_path) == set()
