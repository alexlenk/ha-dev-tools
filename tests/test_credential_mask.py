"""Literal credentials masked on read, put back on write (issue #151)."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools import mirror_secrets
from custom_components.ha_dev_tools.llm_api import (
    GetAutomationTool,
    GetScriptTool,
    GetTemplateEntityTool,
    ListRestCommandsTool,
    ListScriptsTool,
    ListTemplateEntitiesTool,
    UpdateTemplateEntityTool,
    WriteAutomationTool,
    WriteRestCommandTool,
    WriteScriptTool,
)
from custom_components.ha_dev_tools.mirror_secrets import (
    WITHHELD,
    contains_withheld,
    mask_credentials,
    mask_misread,
    restore_withheld,
    scrub,
)
from tests.test_llm_api import (
    _arm,
    _llm_context,
    _write_automation_manager,
    _write_script_manager,
)

URL = "http://admin:hunter2@192.168.1.5/api"
AUTOMATION = """\
- id: door
  alias: Door
  triggers: []
  actions:
    - action: rest_command.ping
      data:
        password: s3cr3t-pw
        url: http://admin:hunter2@192.168.1.5/api
        token: !secret door_token
        alarm_password: 0123
"""


# --- the helpers ---------------------------------------------------------------


def test_restore_same_place_moved_and_refused():
    current = {"a": {"password": "pw-1", "url": URL}, "b": [{"api_key": "k-9"}]}
    sent = mask_credentials(current)
    assert contains_withheld(sent) and not contains_withheld(current)
    assert restore_withheld(sent, current) == (current, [], ["pw-1", URL, "k-9"])

    # Moved: the one value under the same key that masks to it.
    moved = {"c": {"api_key": WITHHELD}, "url": sent["a"]["url"]}
    restored, problems, found = restore_withheld(moved, current)
    assert restored == {"c": {"api_key": "k-9"}, "url": URL}
    assert problems == [] and found == ["k-9", URL]

    # The text around a masked part edited; two values that fit; no original.
    two = {"x": {"password": "one"}, "y": {"password": "two"}}
    edited = {
        "url": sent["a"]["url"].replace("/api", "/v2"),
        "z": {"password": WITHHELD},
    }
    _, problems, _ = restore_withheld(edited, two)
    assert problems == ["url", "z.password"]
    assert restore_withheld({"l": [{"token": WITHHELD}]}, None)[1] == ["l[0].token"]


def test_scrub_and_misread():
    answer = {
        "error": "expected int for dictionary value @ data['password']. Got 's3cr3t'",
        "url": "GET http://admin:hunter2@host failed",
        "pin": 123,
        "flag": True,
    }
    clean = scrub(answer, ["s3cr3t", "http://admin:hunter2@host/x", 123])
    assert "s3cr3t" not in clean["error"] and "hunter2" not in clean["url"]
    assert clean["pin"] == WITHHELD and clean["flag"] is True

    findings = [
        {
            "path": "actions[0].data.alarm_password",
            "written": "0123",
            "ha_reads_as": 83,
        },
        {"path": "mode", "written": "on", "ha_reads_as": True},
    ]
    masked = mask_misread(findings)
    assert masked[0]["written"] == masked[0]["ha_reads_as"] == WITHHELD
    assert masked[1] == findings[1]


# --- the tools -----------------------------------------------------------------


@pytest.fixture
async def admin(hass: HomeAssistant):
    return MockUser(is_owner=True).add_to_hass(hass)


async def _call(hass, tool, args, user):
    return (
        await tool.async_call(
            hass,
            llm.ToolInput(tool_name=tool.name, tool_args=args),
            _llm_context(user.id),
        )
    ).data


async def _confirm(hass, tool, args, user):
    proposal = await _call(hass, tool, args, user)
    assert "confirm_token" in proposal, proposal
    return proposal, await _call(
        hass, tool, {**args, "confirm_token": proposal["confirm_token"]}, user
    )


async def test_automation_round_trip(hass: HomeAssistant, tmp_path, admin):
    from unittest.mock import AsyncMock

    manager = _write_automation_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "automations.yaml").write_text(AUTOMATION)
    hass.services.async_register("automation", "reload", AsyncMock())

    read = await _call(
        hass, GetAutomationTool(manager), {"automation_id": "door"}, admin
    )
    data = read["config"]["actions"][0]["data"]
    assert data["password"] == WITHHELD and data["alarm_password"] == WITHHELD
    assert data["url"] == f"http://{WITHHELD}@192.168.1.5/api"
    assert data["token"] == "!secret door_token"
    assert all(f["written"] == WITHHELD for f in read["misread_values"])

    # One field changed, the masked ones sent back as they came.
    config = read["config"]
    config["alias"] = "Front door"
    proposal, done = await _confirm(
        hass,
        WriteAutomationTool(manager),
        {"automation_id": "door", "config": config},
        admin,
    )
    assert "s3cr3t-pw" not in str(proposal) and "hunter2" not in str(done)
    written = (tmp_path / "automations.yaml").read_text()
    assert "alias: Front door" in written
    assert "password: s3cr3t-pw" in written and "hunter2@" in written
    assert "!secret door_token" in written and "alarm_password: 0123" in written
    assert WITHHELD not in written

    # A masked value that no longer fits: refused before any token.
    config["actions"][0]["data"]["url"] = f"https://{WITHHELD}@other.host/api"
    refused = await _call(
        hass,
        WriteAutomationTool(manager),
        {"automation_id": "door", "config": config},
        admin,
    )
    assert "confirm_token" not in refused
    assert "config.actions[0].data.url" in refused["error"]


async def test_scripts_templates_and_rest_commands(
    hass: HomeAssistant, tmp_path, admin
):
    from unittest.mock import AsyncMock

    from custom_components.ha_dev_tools.file_manager import FileManager
    from custom_components.ha_dev_tools.rest_command_manager import (
        RestCommandManager,
    )
    from custom_components.ha_dev_tools.rest_command_write import RestCommandWriter
    from custom_components.ha_dev_tools.security import SecurityManager
    from custom_components.ha_dev_tools.template_yaml_manager import (
        TemplateYamlManager,
    )

    scripts = _write_script_manager(hass, tmp_path)
    _arm(hass)
    (tmp_path / "scripts.yaml").write_text(
        "ping:\n  sequence:\n    - action: notify.x\n      data:\n"
        "        api_key: k-123456\n"
    )
    hass.services.async_register("script", "reload", AsyncMock())
    listed = await _call(hass, ListScriptsTool(scripts), {}, admin)
    assert "k-123456" not in str(listed)
    read = await _call(hass, GetScriptTool(scripts), {"script_id": "ping"}, admin)
    await _confirm(
        hass,
        WriteScriptTool(scripts),
        {"script_id": "ping", "config": read["config"]},
        admin,
    )
    assert "api_key: k-123456" in (tmp_path / "scripts.yaml").read_text()

    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "t.yaml").write_text(
        "template:\n  - sensor:\n      - name: Feed\n        unique_id: feed\n"
        "        state: '{{ 1 }}'\n        attributes:\n"
        f"          source: {URL}\n"
        "rest_command:\n  ping:\n    url: http://host/x\n    password: pw-777\n"
    )
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
    templates = TemplateYamlManager(hass, files)
    hass.services.async_register("template", "reload", AsyncMock())
    assert "hunter2" not in str(
        await _call(hass, ListTemplateEntitiesTool(templates), {}, admin)
    )
    read = await _call(
        hass, GetTemplateEntityTool(templates), {"unique_id": "feed"}, admin
    )
    assert "hunter2" not in str(read)
    await _confirm(
        hass,
        UpdateTemplateEntityTool(templates),
        {"unique_id": "feed", "config": read["config"]},
        admin,
    )
    assert URL in (tmp_path / "packages" / "t.yaml").read_text()

    rest = RestCommandManager(hass, files)
    listed = await _call(hass, ListRestCommandsTool(rest), {}, admin)
    assert "pw-777" not in str(listed)
    # No original to put back for this tool: the placeholder is refused.
    refused = await _call(
        hass,
        WriteRestCommandTool(RestCommandWriter(hass, rest)),
        {"rest_command_id": "ping", "config": listed["items"][0]["config"]},
        admin,
    )
    assert "can't be put back" in refused["error"]
    assert "pw-777" in (tmp_path / "packages" / "t.yaml").read_text()


async def test_package_platform_entry_refused_as_a_literal(
    hass: HomeAssistant, admin, tmp_path
):
    from custom_components.ha_dev_tools.file_manager import FileManager
    from custom_components.ha_dev_tools.llm_api import WritePackagePlatformEntityTool
    from custom_components.ha_dev_tools.platform_entry_manager import (
        PlatformEntryManager,
    )
    from custom_components.ha_dev_tools.rest_command_manager import (
        RestCommandManager,
    )
    from custom_components.ha_dev_tools.rest_command_write import RestCommandWriter
    from custom_components.ha_dev_tools.security import SecurityManager

    hass.config.config_dir = str(tmp_path)
    _arm(hass)
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "r.yaml").write_text(
        "sensor:\n  - platform: rest\n    name: Router\n"
        "    resource: http://192.168.1.1/status\n    password: pw-555\n"
    )
    files = FileManager(
        hass,
        SecurityManager(
            hass,
            {
                "read_paths": [],
                "write_paths": ["packages/**/*.yaml"],
                "denied_paths": [],
            },
        ),
    )
    tool = WritePackagePlatformEntityTool(
        PlatformEntryManager(hass, files),
        RestCommandWriter(hass, RestCommandManager(hass, files)),
    )
    sent = mirror_secrets.mask_credentials(
        {
            "platform": "rest",
            "name": "Router",
            "resource": "http://192.168.1.1/status",
            "password": "pw-555",
            "scan_interval": 60,
        }
    )
    refused = await _call(hass, tool, {"domain": "sensor", "config": sent}, admin)
    # Put back, then flagged for what it really is - and not echoed.
    assert "literal credential(s) at password" in refused["problems"]
    assert "pw-555" not in str(refused)

    gone = {**sent, "name": "Nobody"}
    refused = await _call(hass, tool, {"domain": "sensor", "config": gone}, admin)
    assert "can't be put back" in refused["error"]
    nameless = {key: value for key, value in sent.items() if key != "name"}
    refused = await _call(hass, tool, {"domain": "sensor", "config": nameless}, admin)
    assert "can't be put back" in refused["error"]


async def test_a_mask_for_an_item_that_doesnt_exist(
    hass: HomeAssistant, tmp_path, admin
):
    from custom_components.ha_dev_tools.file_manager import FileManager
    from custom_components.ha_dev_tools.llm_api import WritePackagePlatformEntityTool
    from custom_components.ha_dev_tools.platform_entry_manager import (
        PlatformEntryManager,
    )
    from custom_components.ha_dev_tools.rest_command_manager import (
        RestCommandManager,
    )
    from custom_components.ha_dev_tools.rest_command_write import RestCommandWriter
    from custom_components.ha_dev_tools.security import SecurityManager
    from custom_components.ha_dev_tools.template_yaml_manager import (
        TemplateYamlManager,
    )

    automations = _write_automation_manager(hass, tmp_path)
    scripts = _write_script_manager(hass, tmp_path)
    (tmp_path / "packages").mkdir(exist_ok=True)
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
    _arm(hass)
    masked = {"alias": "x", "actions": [{"data": {"password": WITHHELD}}]}
    for tool, args in (
        (WriteAutomationTool(automations), {"automation_id": "nope", "config": masked}),
        (WriteScriptTool(scripts), {"script_id": "nope", "config": masked}),
        (
            UpdateTemplateEntityTool(TemplateYamlManager(hass, files)),
            {"unique_id": "nope", "config": {"password": WITHHELD}},
        ),
        (
            WritePackagePlatformEntityTool(
                PlatformEntryManager(hass, files),
                RestCommandWriter(hass, RestCommandManager(hass, files)),
            ),
            {
                "domain": "sensor",
                "file": "../outside.yaml",
                "config": {"platform": "rest", "name": "R", "password": WITHHELD},
            },
        ),
    ):
        refused = await _call(hass, tool, args, admin)
        assert "can't be put back" in refused["error"], (tool.name, refused)
