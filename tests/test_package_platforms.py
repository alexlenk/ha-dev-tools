"""YAML platform entries in packages (issue #164) and rest_command writes
(issue #168), against real HA platform schemas."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml as pyyaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools.file_manager import FileManager
from custom_components.ha_dev_tools.platform_entry_manager import (
    PlatformEntryError,
    PlatformEntryManager,
    validation_error,
)
from custom_components.ha_dev_tools.rest_command_manager import RestCommandManager
from custom_components.ha_dev_tools.rest_command_write import (
    RestCommandWriteError,
    RestCommandWriter,
    host_class,
    target_host,
)
from custom_components.ha_dev_tools.security import SecurityManager
from tests.test_llm_api import _arm, _llm_context

PACKAGE = """\
sensor:
  - platform: statistics
    name: "Kitchen distance avg"
    unique_id: kitchen_distance_avg
    entity_id: sensor.kitchen_distance
    state_characteristic: mean
    max_age:
      minutes: 5
  - platform: statistics
    name: "Hall distance avg"
    entity_id: sensor.hall_distance
    state_characteristic: mean
    max_age:
      minutes: 5
  - platform: rest
    name: "Router status"
    resource: http://192.168.1.1/status
    password: !secret router_password
binary_sensor:
  platform: statistics
  name: "Door"
  entity_id: binary_sensor.door
  state_characteristic: count
  max_age:
    minutes: 5
automation:
  - id: keep_me
    triggers: []
    actions: []
rest_command:
  doorbird_restart:
    url: http://doorbird.local/bha-api/restart.cgi
    username: !secret doorbird_user
    password: !secret doorbird_password
"""
PATH = "packages/ibeacon.yaml"


@pytest.fixture
def files(hass: HomeAssistant, tmp_path: Path) -> FileManager:
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "packages").mkdir()
    (tmp_path / PATH).write_text(PACKAGE)
    (tmp_path / "packages" / "other.yaml").write_text("sensor: []\n")
    (tmp_path / "configuration.yaml").write_text(
        "rest_command:\n  in_config:\n    url: https://example.com/x\n"
    )
    security = SecurityManager(
        hass,
        {
            "read_paths": ["configuration.yaml", "packages/**/*.yaml"],
            "write_paths": ["packages/**/*.yaml"],
            "denied_paths": [],
        },
    )
    return FileManager(hass, security)


@pytest.fixture
def platforms(hass: HomeAssistant, files: FileManager) -> PlatformEntryManager:
    return PlatformEntryManager(hass, files)


@pytest.fixture
def writer(hass: HomeAssistant, files: FileManager) -> RestCommandWriter:
    return RestCommandWriter(hass, RestCommandManager(hass, files))


def _document(tmp_path: Path, path: str = PATH) -> dict:
    class _Loader(pyyaml.SafeLoader):
        pass

    _Loader.add_constructor("!secret", lambda loader, node: f"!secret {node.value}")
    return pyyaml.load((tmp_path / path).read_text(), Loader=_Loader)


STATS = {
    "platform": "statistics",
    "name": "Bathroom distance avg",
    "unique_id": "bathroom_distance_avg",
    "entity_id": "sensor.bathroom_distance",
    "state_characteristic": "mean",
    "max_age": {"minutes": 5},
}


# --- platform entries -----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_entries(hass: HomeAssistant, platforms):
    entries = await platforms.list_entries()
    assert [(e["domain"], e["index"], e["unique_id"], e["name"]) for e in entries] == [
        ("sensor", 0, "kitchen_distance_avg", "Kitchen distance avg"),
        ("sensor", 1, None, "Hall distance avg"),
        ("sensor", 2, None, "Router status"),
        ("binary_sensor", 0, None, "Door"),  # the short form
    ]
    assert entries[2]["config"]["password"] == "!secret router_password"
    assert [e["index"] for e in await platforms.list_entries(platform="rest")] == [2]
    assert len(await platforms.list_entries(domain="binary_sensor")) == 1


@pytest.mark.asyncio
async def test_validation_uses_the_platforms_own_schema(hass: HomeAssistant):
    assert await validation_error(hass, "sensor", STATS) is None
    missing = await validation_error(
        hass, "sensor", {**STATS, "state_characteristic": "nope"}
    )
    assert missing and "state_characteristic" in missing
    unknown = await validation_error(hass, "sensor", {"platform": "no_such_thing"})
    assert unknown and "can't be loaded" in unknown
    assert await validation_error(hass, "sensor", {}) == "an entry needs its platform"


@pytest.mark.asyncio
async def test_create_replace_and_delete(hass: HomeAssistant, platforms, tmp_path):
    original = _document(tmp_path)
    location, path, before, after = await platforms.plan_write(
        "sensor", STATS, package="ibeacon.yaml"
    )
    assert location is None and path == PATH
    await platforms.write_file(path, after)
    document = _document(tmp_path)
    assert document["sensor"][3]["unique_id"] == "bathroom_distance_avg"
    assert document["sensor"][:3] == original["sensor"]
    assert document["automation"] == original["automation"]

    # Replace, by unique_id - the rest of the file as it was.
    location, path, _, after = await platforms.plan_write(
        "sensor", {**STATS, "state_characteristic": "median"}
    )
    assert location is not None and location.index == 3
    await platforms.write_file(path, after)
    assert _document(tmp_path)["sensor"][3]["state_characteristic"] == "median"
    assert "password: !secret router_password" in (tmp_path / PATH).read_text()

    # Delete the name-only one and the short-form one: its key goes.
    hall = await platforms.resolve("sensor", "statistics", name="Hall distance avg")
    door = await platforms.resolve("binary_sensor", "statistics", name="Door")
    for path, entries, _, after in await platforms.plan_delete([hall, door]):
        await platforms.write_file(path, after)
    document = _document(tmp_path)
    assert "binary_sensor" not in document
    assert [entry.get("name") for entry in document["sensor"]] == [
        "Kitchen distance avg",
        "Router status",
        "Bathroom distance avg",
    ]


@pytest.mark.asyncio
async def test_finding_refuses_to_guess(hass: HomeAssistant, platforms, tmp_path):
    (tmp_path / "packages" / "other.yaml").write_text(
        "sensor:\n  - platform: statistics\n    name: Hall distance avg\n"
        "    entity_id: sensor.x\n    state_characteristic: mean\n"
        "    max_age: {minutes: 5}\n"
    )
    with pytest.raises(PlatformEntryError, match="2 of a sensor statistics entry"):
        await platforms.resolve("sensor", "statistics", name="Hall distance avg")
    narrowed = await platforms.resolve(
        "sensor", "statistics", name="Hall distance avg", file="other.yaml"
    )
    assert narrowed.file_path == "packages/other.yaml"
    with pytest.raises(PlatformEntryError, match="no sensor statistics entry"):
        await platforms.resolve("sensor", "statistics", unique_id="nope")
    with pytest.raises(PlatformEntryError, match="exactly one of unique_id or name"):
        await platforms.find("sensor", "statistics")
    with pytest.raises(PlatformEntryError, match="needs a unique_id or a name"):
        await platforms.plan_write("sensor", {"platform": "statistics"})
    with pytest.raises(PlatformEntryError, match="pass package"):
        await platforms.plan_write("sensor", {**STATS, "unique_id": "fresh"})


# --- rest_command ---------------------------------------------------------------


def test_target_host_and_class():
    assert target_host("https://Example.com:8443/a/{{ x }}") == "example.com"
    for url in (
        "http://{{ host }}/x",
        "{{ url }}",
        "http://{% if a %}b{% endif %}/",
        "",
    ):
        with pytest.raises(RestCommandWriteError):
            target_host(url)
    assert host_class("192.168.1.10") == "private"
    assert host_class("8.8.8.8") == "public"
    assert host_class("doorbird.local") == "lan (by name)"
    assert host_class("octoprint") == "lan (by name)"
    assert host_class("api.smard.de") == "public (by name)"


@pytest.mark.asyncio
async def test_rest_command_refusals(hass: HomeAssistant, writer):
    base = {"url": "http://doorbird.local/bha-api/x.cgi"}
    for config, kwargs, expected in (
        (base, {}, "pass package"),
        (
            {"url": "http://{{ states('input_text.host') }}/x"},
            {"package": "ibeacon.yaml"},
            "can't be a template",
        ),
        (
            {"url": "https://api.smard.de/x"},
            {"package": "ibeacon.yaml"},
            "public \\(by name\\)\\) is a host no rest_command uses",
        ),
        (
            {**base, "password": "hunter2"},
            {"package": "ibeacon.yaml"},
            "literal credential\\(s\\) at password",
        ),
        (
            {**base, "headers": {"Authorization": "Bearer abcdefghijkl"}},
            {"package": "ibeacon.yaml"},
            "headers.Authorization",
        ),
        (
            {**base, "verify_ssl": False},
            {"package": "ibeacon.yaml"},
            "allow_insecure_tls",
        ),
    ):
        with pytest.raises(RestCommandWriteError, match=expected):
            await writer.plan_write("new_one", config, **kwargs)
    with pytest.raises(RestCommandWriteError, match="configuration.yaml, which stays"):
        await writer.plan_write("in_config", {"url": "https://example.com/y"})
    with pytest.raises(RestCommandWriteError, match="read-only"):
        await writer.plan_delete("in_config")


@pytest.mark.asyncio
async def test_rest_command_create_replace_delete(
    hass: HomeAssistant, writer, tmp_path
):
    reload = AsyncMock()
    hass.services.async_register("rest_command", "reload", reload)
    plan = await writer.plan_write(
        "emhass_mpc",
        {
            "url": "https://emhass.lan:5000/action/naive-mpc-optim",
            "method": "POST",
            "headers": {
                "Authorization": "!secret emhass_token",
                "Accept": "application/json",
            },
            "payload": '{"prices": {{ states("sensor.price_curve") }}}',
        },
        package="ibeacon.yaml",
        allow_new_host=True,
    )
    assert plan.report["host"] == "emhass.lan"
    assert plan.report["host_class"] == "lan (by name)"
    assert plan.report["new_host"] is True
    assert (plan.report["method"], plan.report["timeout"]) == ("post", 10)
    assert await writer.write(plan) == "reloaded (rest_command.reload)"
    reload.assert_awaited_once()
    text = (tmp_path / PATH).read_text()
    # A real tag, not the quoted text '!secret emhass_token'.
    assert "Authorization: !secret emhass_token" in text
    assert _document(tmp_path)["rest_command"]["doorbird_restart"]["username"] == (
        "!secret doorbird_user"
    )

    # Its own host is known now - a replace needs no allow_new_host.
    plan = await writer.plan_write(
        "emhass_mpc",
        {"url": "https://emhass.lan:5000/action/dayahead-optim", "method": "POST"},
    )
    assert plan.report["action"] == "replace" and plan.report["new_host"] is False
    await writer.write(plan)
    assert _document(tmp_path)["rest_command"]["emhass_mpc"]["url"].endswith(
        "dayahead-optim"
    )
    await writer.write(await writer.plan_delete("emhass_mpc"))
    assert list(_document(tmp_path)["rest_command"]) == ["doorbird_restart"]
    await writer.write(await writer.plan_delete("doorbird_restart"))
    assert "rest_command" not in _document(tmp_path)


@pytest.mark.asyncio
async def test_an_entry_that_already_skips_tls_checks_keeps_it(
    hass: HomeAssistant, writer, tmp_path
):
    (tmp_path / "packages" / "other.yaml").write_text(
        "rest_command:\n  octo:\n    url: https://octoprint/api/system\n"
        "    verify_ssl: false\n"
    )
    plan = await writer.plan_write(
        "octo", {"url": "https://octoprint/api/system/commands", "verify_ssl": False}
    )
    assert plan.report["verify_ssl"] is False
    assert "restart Home Assistant" in await writer.write(plan)


# --- the tools --------------------------------------------------------------------


def _input(name: str, **args) -> llm.ToolInput:
    return llm.ToolInput(tool_name=name, tool_args=args)


@pytest.fixture
async def admin(hass: HomeAssistant):
    return MockUser(is_owner=True).add_to_hass(hass)


@pytest.mark.asyncio
async def test_platform_entry_tools(
    hass: HomeAssistant,
    setup_integration_with_entry,
    admin,
    platforms,
    writer,
    tmp_path,
):
    from custom_components.ha_dev_tools.llm_api import (
        DeletePackagePlatformEntitiesTool,
        ListPackagePlatformEntitiesTool,
        WritePackagePlatformEntityTool,
    )

    _arm(hass)
    context = _llm_context(admin.id)
    hass.services.async_register("statistics", "reload", AsyncMock())
    listed = await ListPackagePlatformEntitiesTool(platforms)._run(
        hass, _input("list_package_platform_entities", platform="rest"), context
    )
    assert listed["entries"][0]["name"] == "Router status"

    tool = WritePackagePlatformEntityTool(platforms, writer)
    real_validation = validation_error

    async def validation(hass, domain, config):
        # rest's own module needs xmltodict, not installed here - its
        # schema step is stubbed so the egress rules are what's tested.
        if config.get("platform") == "rest":
            return None
        return await real_validation(hass, domain, config)

    stub = patch(
        "custom_components.ha_dev_tools.llm_api.platform_entry_manager.validation_error",
        validation,
    )
    stub.start()
    for config, expected in (
        (
            {"platform": "command_line", "name": "x", "command": "rm -rf /"},
            "runs shell commands",
        ),
        ({**STATS, "state_characteristic": "nope"}, "state_characteristic"),
        ({**STATS, "password": "hunter2"}, "literal credential"),
        (
            {"platform": "rest", "name": "Smard", "resource": "https://api.smard.de/x"},
            "allow_new_host",
        ),
        (
            {
                "platform": "rest",
                "name": "Tpl",
                "resource_template": "http://{{ x }}/y",
            },
            "can't be a template",
        ),
    ):
        preview = await tool._preview_context(
            hass,
            _input(tool.name, domain="sensor", config=config, package="ibeacon.yaml"),
            context,
        )
        assert expected in preview["problems"], (config, preview)
    stub.stop()

    preview = await tool._preview_context(
        hass,
        _input(tool.name, domain="sensor", config=STATS, package="ibeacon.yaml"),
        context,
    )
    assert preview["would_write"]["action"] == "create"
    result = await tool._write(
        hass,
        _input(tool.name, domain="sensor", config=STATS, package="ibeacon.yaml"),
        context,
    )
    assert result["written"]["reload"] == "reloaded (statistics.reload)"

    replaced = await tool._preview_context(
        hass,
        _input(tool.name, domain="sensor", config={**STATS, "precision": 1}),
        context,
    )
    assert replaced["would_write"]["action"] == "replace"
    assert replaced["would_write"]["references"] is None  # no live entity here

    delete = DeletePackagePlatformEntitiesTool(platforms)
    entries = [
        {
            "domain": "sensor",
            "platform": "statistics",
            "unique_id": "bathroom_distance_avg",
        },
        {"domain": "binary_sensor", "platform": "statistics", "name": "Door"},
    ]
    preview = await delete._preview_context(
        hass, _input(delete.name, entries=entries), context
    )
    assert [entry["domain"] for entry in preview["would_delete"]] == [
        "sensor",
        "binary_sensor",
    ]
    twice = await delete._preview_context(
        hass, _input(delete.name, entries=[entries[0], entries[0]]), context
    )
    assert "listed twice" in twice["problems"]
    result = await delete._write(hass, _input(delete.name, entries=entries), context)
    assert result["deleted"][0]["deleted"] == 2
    assert result["reload"] == {"statistics": "reloaded (statistics.reload)"}
    assert "binary_sensor" not in _document(tmp_path)


@pytest.mark.asyncio
async def test_rest_command_tools(
    hass: HomeAssistant, setup_integration_with_entry, admin, writer, tmp_path
):
    from custom_components.ha_dev_tools.llm_api import (
        DeleteRestCommandTool,
        WriteRestCommandTool,
    )

    assert await async_setup_component(hass, "websocket_api", {})
    assert await async_setup_component(hass, "lovelace", {})
    _arm(hass)
    context = _llm_context(admin.id)
    hass.services.async_register("rest_command", "reload", AsyncMock())
    (tmp_path / "automations.yaml").write_text(
        "- id: ring\n  triggers: []\n  actions:\n"
        "    - action: rest_command.doorbird_restart\n"
    )
    tool = WriteRestCommandTool(writer)
    config = {
        "url": "http://doorbird.local/bha-api/restart.cgi",
        "username": "!secret doorbird_user",
        "password": "!secret doorbird_password",
        "timeout": 20,
    }
    preview = await tool._preview_context(
        hass,
        _input(tool.name, rest_command_id="doorbird_restart", config=config),
        context,
    )
    would = preview["would_write"]
    assert would["action"] == "replace" and would["timeout"] == 20
    assert would["references"]["entity_id"] == "rest_command.doorbird_restart"
    refused = await tool._preview_context(
        hass,
        _input(
            tool.name, rest_command_id="doorbird_restart", config={"url": "{{ u }}"}
        ),
        context,
    )
    assert "scheme and host" in refused["problems"]
    result = await tool._write(
        hass,
        _input(tool.name, rest_command_id="doorbird_restart", config=config),
        context,
    )
    assert result["written"]["reload"] == "reloaded (rest_command.reload)"

    delete = DeleteRestCommandTool(writer)
    missing = await delete._preview_context(
        hass, _input(delete.name, rest_command_id="nope"), context
    )
    assert "nope" in missing["problems"]
    preview = await delete._preview_context(
        hass, _input(delete.name, rest_command_id="doorbird_restart"), context
    )
    assert preview["would_delete"]["before"]["password"] == "!secret doorbird_password"
    result = await delete._write(
        hass, _input(delete.name, rest_command_id="doorbird_restart"), context
    )
    assert result["deleted"]["action"] == "delete"
    assert "rest_command" not in _document(tmp_path)


# --- edges --------------------------------------------------------------------------


def test_helpers_walk_lists():
    from custom_components.ha_dev_tools import mirror_secrets
    from custom_components.ha_dev_tools.yaml_style import tag_references

    tagged = tag_references({"a": ["!secret x", "plain", "!env_var Y"]})
    assert [getattr(item, "tag", None) for item in tagged["a"]][0].value == "!secret"
    assert tagged["a"][1] == "plain"
    assert mirror_secrets.literal_credentials(
        {"urls": ["https://u:pw@host/x", "https://ok/x"], "username": "!secret u"}
    ) == ["urls[0]"]


@pytest.mark.asyncio
async def test_entry_manager_edges(hass: HomeAssistant, platforms, tmp_path):
    from custom_components.ha_dev_tools import platform_entry_manager as pem

    # A package without the domain, an empty one, and a short-form one
    # getting a second entry.
    (tmp_path / "packages" / "other.yaml").write_text("homeassistant: {}\n")
    (tmp_path / "packages" / "empty.yaml").write_text("")
    for package, unique_id in (("other.yaml", "one"), ("empty.yaml", "two")):
        config = {**STATS, "unique_id": unique_id}
        _, path, _, after = await platforms.plan_write(
            "sensor", config, package=package
        )
        await platforms.write_file(path, after)
        assert _document(tmp_path, path)["sensor"][0]["unique_id"] == unique_id
    _, path, _, after = await platforms.plan_write(
        "binary_sensor",
        {**STATS, "unique_id": "second_door", "entity_id": "binary_sensor.d2"},
        package="ibeacon.yaml",
    )
    await platforms.write_file(path, after)
    assert [entry["name"] for entry in _document(tmp_path)["binary_sensor"]] == [
        "Door",
        "Bathroom distance avg",
    ]
    # The same entry in two files: a replace has to be told which.
    (tmp_path / "packages" / "copy.yaml").write_text(
        (tmp_path / "packages" / "other.yaml").read_text()
    )
    with pytest.raises(PlatformEntryError, match="2 matching entries"):
        await platforms.plan_write("sensor", {**STATS, "unique_id": "one"})
    with pytest.raises(PlatformEntryError, match="doesn't exist - create it"):
        await platforms.plan_write(
            "sensor", {**STATS, "unique_id": "x"}, package="missing.yaml"
        )
    assert pem._platform_lists(["not", "a", "mapping"]) == {}
    assert "restart Home Assistant" in await platforms.reload("statistics")

    class NoSchema:
        pass

    with patch.object(
        pem,
        "async_get_integration",
        AsyncMock(
            return_value=AsyncMock(
                async_get_platform=AsyncMock(return_value=NoSchema())
            )
        ),
    ):
        assert "no YAML platform schema" in await pem.validation_error(
            hass, "sensor", {"platform": "odd"}
        )
    import shutil

    shutil.rmtree(tmp_path / "packages")
    assert await platforms.list_entries() == []


@pytest.mark.asyncio
async def test_rest_command_edges(hass: HomeAssistant, writer, tmp_path):
    with pytest.raises(RestCommandWriteError, match="has no host"):
        target_host("http:///x")
    # An existing entry with a templated host is skipped as a known host,
    # and an old url that's no longer valid doesn't count either.
    (tmp_path / "packages" / "other.yaml").write_text(
        'rest_command:\n  dynamic:\n    url: "http://{{ host }}/x"\n'
    )
    plan = await writer.plan_write("dynamic", {"url": "http://doorbird.local/y"})
    assert plan.report["new_host"] is False  # doorbird.local is known
    with pytest.raises(RestCommandWriteError, match="doesn't exist - create it"):
        await writer.plan_write(
            "fresh", {"url": "http://doorbird.local/z"}, package="missing.yaml"
        )
    (tmp_path / "packages" / "empty.yaml").write_text("homeassistant: {}\n")
    plan = await writer.plan_write(
        "fresh", {"url": "http://doorbird.local/z"}, package="empty.yaml"
    )
    await writer.write(plan)
    assert _document(tmp_path, "packages/empty.yaml")["rest_command"]["fresh"]["url"]
    (tmp_path / "packages" / "blank.yaml").write_text("")
    await writer.write(
        await writer.plan_write(
            "blank_one", {"url": "http://doorbird.local/b"}, package="blank.yaml"
        )
    )
    # The same id in two files: refused.
    (tmp_path / "packages" / "dupe.yaml").write_text(
        "rest_command:\n  fresh:\n    url: http://doorbird.local/q\n"
    )
    with pytest.raises(RestCommandWriteError, match="more than one file"):
        await writer.plan_write("fresh", {"url": "http://doorbird.local/z"})


@pytest.mark.asyncio
async def test_tool_edges(
    hass: HomeAssistant,
    setup_integration_with_entry,
    admin,
    platforms,
    writer,
    tmp_path,
):
    from homeassistant.helpers import entity_registry as er

    from custom_components.ha_dev_tools.llm_api import (
        DeletePackagePlatformEntitiesTool,
        DeleteRestCommandTool,
        WritePackagePlatformEntityTool,
        WriteRestCommandTool,
        _entry_references,
    )

    for component in ("websocket_api", "lovelace", "person"):
        assert await async_setup_component(hass, component, {})
    _arm(hass)
    context = _llm_context(admin.id)
    # A live entity for the kitchen entry: its references are listed.
    er.async_get(hass).async_get_or_create(
        "sensor",
        "statistics",
        "kitchen_distance_avg",
        suggested_object_id="kitchen_avg",
    )
    tool = WritePackagePlatformEntityTool(platforms, writer)
    kitchen = {
        **STATS,
        "unique_id": "kitchen_distance_avg",
        "name": "Kitchen distance avg",
        "entity_id": "sensor.kitchen_distance",
    }
    preview = await tool._preview_context(
        hass, _input(tool.name, domain="sensor", config=kitchen), context
    )
    # Without the frontend loaded the lookup can't list dashboards - the
    # preview says so rather than failing.
    assert preview["would_write"]["references"] == {
        "entity_id": "sensor.kitchen_avg",
        "error": "unknown_command: Unknown command.",
    }
    with patch(
        "custom_components.ha_dev_tools.llm_api.references.find_references",
        AsyncMock(return_value={"yaml": [{"file": "automations.yaml"}]}),
    ):
        preview = await tool._preview_context(
            hass, _input(tool.name, domain="sensor", config=kitchen), context
        )
    assert preview["would_write"]["references"] == {
        "entity_id": "sensor.kitchen_avg",
        "yaml": [{"file": "automations.yaml"}],
    }
    assert await _entry_references(hass, _llm_context(), "sensor.kitchen_avg") is None
    bad = await tool._write(
        hass,
        _input(tool.name, domain="sensor", config={"platform": "command_line"}),
        context,
    )
    assert "runs shell commands" in bad["error"]

    async def no_validation(hass, domain, config):
        return None

    with patch(
        "custom_components.ha_dev_tools.llm_api.platform_entry_manager.validation_error",
        no_validation,
    ):
        insecure = await tool._preview_context(
            hass,
            _input(
                tool.name,
                domain="sensor",
                config={
                    "platform": "rest",
                    "name": "Router status",
                    "resource": "http://192.168.1.1/status",
                    "verify_ssl": False,
                },
            ),
            context,
        )
        assert "allow_insecure_tls" in insecure["problems"]
        allowed = await tool._preview_context(
            hass,
            _input(
                tool.name,
                domain="sensor",
                config={
                    "platform": "rest",
                    "name": "Smard",
                    "resource": "https://api.smard.de/x",
                },
                package="ibeacon.yaml",
                allow_new_host=True,
            ),
            context,
        )
        assert allowed["would_write"]["host_class"] == "public (by name)"
        (tmp_path / PATH).write_text(
            PACKAGE.replace(
                "resource: http://192.168.1.1/status",
                'resource: "http://{{ x }}/s"',
            )
        )
        fixed = await tool._preview_context(
            hass,
            _input(
                tool.name,
                domain="sensor",
                config={
                    "platform": "rest",
                    "name": "Router status",
                    "resource": "http://192.168.1.1/status",
                },
                allow_new_host=True,
            ),
            context,
        )
        assert fixed["would_write"]["action"] == "replace"

    delete = DeletePackagePlatformEntitiesTool(platforms)
    missing = await delete._write(
        hass,
        _input(
            delete.name, entries=[{"domain": "sensor", "platform": "x", "name": "y"}]
        ),
        context,
    )
    assert "no sensor x entry" in missing["error"]
    with patch.object(
        platforms, "write_file", AsyncMock(side_effect=RuntimeError("disk"))
    ):
        failed = await delete._write(
            hass,
            _input(
                delete.name,
                entries=[
                    {
                        "domain": "binary_sensor",
                        "platform": "statistics",
                        "name": "Door",
                    }
                ],
            ),
            context,
        )
    assert failed["error"] == "disk"

    refused = await WriteRestCommandTool(writer)._write(
        hass,
        _input("write_rest_command", rest_command_id="x", config={"url": "{{ u }}"}),
        context,
    )
    assert "pass package" in refused["error"]
    gone = await DeleteRestCommandTool(writer)._write(
        hass, _input("delete_rest_command", rest_command_id="nope"), context
    )
    assert "nope" in gone["error"]
