"""Findings of the full code audit, each against the real code path it
was found in. Every test here failed on the code before the fix."""

import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools import (
    access_control,
    helper_manager,
    mirror,
    mirror_secrets,
    supervisor_manager,
)
from custom_components.ha_dev_tools.automation_manager import AutomationManager
from custom_components.ha_dev_tools.file_manager import FileManager, package_file
from custom_components.ha_dev_tools.script_manager import ScriptManager
from custom_components.ha_dev_tools.security import SecurityManager
from custom_components.ha_dev_tools.template_yaml_manager import TemplateYamlManager
from custom_components.ha_dev_tools.ws_call import call_ws_command, resolve_user
from tests.test_llm_api import _arm, _llm_context


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Shadow conftest.py's fixture - see test_history_manager.py."""
    yield


@pytest.fixture(autouse=True)
def _clean_arm_file(hass: HomeAssistant):
    path = access_control._arm_file_path(hass)
    path.unlink(missing_ok=True)
    yield
    path.unlink(missing_ok=True)


@pytest.fixture
async def admin_user(hass: HomeAssistant):
    return MockUser(is_owner=True).add_to_hass(hass)


# --- a helper's config picked the WebSocket command ---------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", ["create", "update"], ids=["create_helper", "update_helper"]
)
async def test_a_helper_config_cant_choose_the_ws_command(
    hass: HomeAssistant, admin_user, action
):
    """`type` in a helper's config replaced the command - here minting a
    long-lived access token as the calling admin."""
    assert await async_setup_component(hass, "input_boolean", {})
    assert await async_setup_component(hass, "auth", {})
    sneaky = {"type": "auth/long_lived_access_token", "client_name": "x", "lifespan": 1}
    tokens = len(admin_user.refresh_tokens)

    with pytest.raises(ValueError, match="'type' can't be passed"):
        if action == "create":
            await helper_manager.create_helper(
                hass, admin_user, "input_boolean", sneaky
            )
        else:
            await helper_manager.update_helper(
                hass, admin_user, "input_boolean", "anything", sneaky
            )
    assert len(admin_user.refresh_tokens) == tokens


@pytest.mark.asyncio
async def test_call_ws_command_keeps_its_own_id_and_type(
    hass: HomeAssistant, admin_user
):
    with pytest.raises(ValueError, match="'id', 'type'"):
        await call_ws_command(hass, admin_user, "get_config", id=7, type="x")


@pytest.mark.asyncio
async def test_an_inactive_user_isnt_resolved(hass: HomeAssistant):
    from custom_components.ha_dev_tools.ws_call import UnresolvedUserError

    user = MockUser(is_owner=True, is_active=False).add_to_hass(hass)
    with pytest.raises(UnresolvedUserError):
        await resolve_user(hass, _llm_context(user.id))


# --- tool arguments were never checked against their schema -----------------------


async def _call(hass, tool, user, **args):
    return await tool.async_call(
        hass, llm.ToolInput(tool_name=tool.name, tool_args=args), _llm_context(user.id)
    )


@pytest.mark.asyncio
async def test_tool_arguments_are_checked_against_their_schema(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    """HA hands a tool the client's JSON as-is (llm.APIInstance.
    async_call_tool), so a schema's types, ranges and required fields were
    never enforced."""
    from custom_components.ha_dev_tools.llm_api import (
        GetLogsTool,
        GetStatisticsTool,
        ListMqttTopicsTool,
    )
    from custom_components.ha_dev_tools.log_manager import LogManager

    _arm(hass)
    logs = GetLogsTool(LogManager(hass, SecurityManager(hass, {})))
    too_many = await _call(hass, logs, admin_user, lines=5000)
    assert too_many["error_type"] == "Invalid"
    unknown = await _call(hass, logs, admin_user, nonsense=1)
    assert unknown["error_type"] == "Invalid"
    # A string where a list belongs, iterated as characters before.
    stats = await _call(
        hass, GetStatisticsTool(), admin_user, statistic_ids="sensor.x", start_time="x"
    )
    assert stats["error_type"] == "Invalid"
    mqtt = await _call(hass, ListMqttTopicsTool(), admin_user, timeout=3600)
    assert mqtt["error_type"] == "Invalid"
    # Within the schema, a call goes through.
    assert "entries" in await _call(hass, logs, admin_user, limit=5)


@pytest.mark.asyncio
async def test_an_unexpected_error_is_a_tool_error_not_an_escape(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    from custom_components.ha_dev_tools.llm_api import GatedTool, vol

    class Boom(GatedTool):
        name = "boom"
        parameters = vol.Schema({})

        async def _run(self, hass, tool_input, llm_context):
            raise RuntimeError("disk on fire")

    _arm(hass)
    result = await _call(hass, Boom(), admin_user)
    assert result == {"error": "disk on fire", "error_type": "RuntimeError"}


def test_tools_say_which_integration_provides_them():
    from custom_components.ha_dev_tools.llm_api import DevToolsPingTool, GatedTool

    assert GatedTool.integration == "ha_dev_tools"
    assert DevToolsPingTool.integration == "ha_dev_tools"


# --- get_logs: since/until were documented but ignored -----------------------------


@pytest.mark.asyncio
async def test_get_logs_filters_by_since_and_until(
    hass: HomeAssistant, setup_integration_with_entry, admin_user
):
    import logging

    from homeassistant.util import dt as dt_util

    from custom_components.ha_dev_tools.llm_api import GetLogsTool
    from custom_components.ha_dev_tools.log_manager import LogManager

    assert await async_setup_component(hass, "system_log", {})
    logging.getLogger("tests.audit").error("something broke")
    await hass.async_block_till_done()
    _arm(hass)
    tool = GetLogsTool(LogManager(hass, SecurityManager(hass, {})))
    later = (dt_util.utcnow() + timedelta(hours=1)).isoformat()
    earlier = (dt_util.utcnow() - timedelta(hours=1)).isoformat()

    assert (await _call(hass, tool, admin_user, since=later))["entries"] == []
    assert (await _call(hass, tool, admin_user, until=earlier))["entries"] == []
    found = await _call(hass, tool, admin_user, since=earlier)
    assert any("something broke" in entry["message"] for entry in found["entries"])
    bad = await _call(hass, tool, admin_user, since="yesterday")
    assert "since" in bad["error"]


# --- the arm file's hard cap ----------------------------------------------------------


@pytest.mark.parametrize(
    ("armed_at", "expired"),
    [(0, False), (30, False), (3600, True), (10**10, True)],
    ids=["now", "skewed", "an_hour_ahead", "far_future"],
)
def test_a_future_arm_time_doesnt_lift_the_hard_cap(tmp_path, armed_at, expired):
    """The 4-hour cap counts from the content's time; one in the future
    held it off indefinitely."""
    path = tmp_path / "armed"
    now = time.time()
    path.write_text(str(now + armed_at))
    assert access_control._is_expired(path, now=now) is expired


# --- `package` reached other allowlisted files ----------------------------------------


@pytest.fixture
def config(hass: HomeAssistant, tmp_path: Path) -> Path:
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "energy.yaml").write_text("homeassistant: {}\n")
    (tmp_path / "automations.yaml").write_text("[]\n")
    (tmp_path / "scripts.yaml").write_text("morning:\n  sequence: []\n")
    for domain in ("automation", "script", "template"):
        hass.services.async_register(domain, "reload", AsyncMock())
    return tmp_path


@pytest.mark.asyncio
async def test_package_cant_leave_packages(hass: HomeAssistant, config: Path):
    """package='../scripts.yaml' wrote an `automation:` key into
    scripts.yaml - a script named "automation" - and '../automations.yaml'
    crashed or corrupted the automations list."""
    files = FileManager(hass, SecurityManager(hass, {}))
    before = {
        name: (config / name).read_text()
        for name in ("scripts.yaml", "automations.yaml")
    }

    with pytest.raises(ValueError, match="inside packages/"):
        await AutomationManager(hass, files).write_automation(
            "new_one",
            {"alias": "x", "triggers": [], "actions": []},
            package="../scripts.yaml",
        )
    with pytest.raises(ValueError, match="inside packages/"):
        await ScriptManager(hass, files).write_script(
            "new_one", {"sequence": []}, package="../automations.yaml"
        )
    with pytest.raises(ValueError, match="inside packages/"):
        await TemplateYamlManager(hass, files).create_entity(
            "sensor",
            {"name": "x", "unique_id": "x", "state": "1"},
            package="../scripts.yaml",
        )
    assert {
        name: (config / name).read_text()
        for name in ("scripts.yaml", "automations.yaml")
    } == before

    # A real package still works.
    result = await AutomationManager(hass, files).write_automation(
        "in_package",
        {"alias": "y", "triggers": [], "actions": []},
        package="energy.yaml",
    )
    assert result.location.file_path == "packages/energy.yaml"


@pytest.mark.parametrize(
    "package",
    ["../scripts.yaml", "/config/x.yaml", "a/../../b.yaml", "notes.txt", "", "  "],
)
def test_package_file_refuses(package):
    with pytest.raises(ValueError):
        package_file(package)


def test_package_file_accepts_nested_packages():
    assert package_file("heating/rooms.yaml") == "packages/heating/rooms.yaml"


# --- the mirror's GitHub URLs -----------------------------------------------------------


def test_mirror_paths_cant_reach_other_api_endpoints():
    assert mirror._url_path(".storage/lovelace.my_dash") == ".storage/lovelace.my_dash"
    assert mirror._url_path("entities/a b#c?.json") == "entities/a%20b%23c%3F.json"
    for path in ("../../user", "a/../b", "/abs", "a//b", "."):
        with pytest.raises(ValueError):
            mirror._url_path(path)


# --- add-on slugs -------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["core_ssh/../../info", "a/b", "..", "x?y"])
async def test_an_addon_slug_is_a_slug(hass: HomeAssistant, slug):
    hassio = AsyncMock()
    hass.data[supervisor_manager._HASSIO_DATA_KEY] = hassio
    result = await supervisor_manager.get_addon_logs(hass, slug)
    assert "isn't an add-on slug" in result["error"]
    hassio.send_command.assert_not_called()


# --- credentials ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "wifi_passphrase: correct horse",
        "psk: abcdef123",
        "encryption_key: 0011aabb",
        "network_key: [1, 2, 3]",
    ],
)
def test_more_credential_names_are_recognized(line):
    assert mirror_secrets.find_text_credentials(line)


def test_rest_command_credentials_are_masked():
    config = {
        "url": "https://user:hunter2@example.com/api",
        "headers": {
            "Authorization": "Bearer abcdefghijklmnop",
            "X-Api-Key": "k-123",
            "Accept": "application/json",
        },
        "password": "!secret router_password",
        "payload": '{"on": true}',
        "verify_ssl": True,
        "urls": ["https://admin:pw123@router.local/"],
    }
    masked = mirror_secrets.mask_credentials(config)
    assert "hunter2" not in str(masked)
    assert "abcdefghijklmnop" not in str(masked)
    assert masked["headers"]["X-Api-Key"] == mirror_secrets.WITHHELD
    assert masked["headers"]["Accept"] == "application/json"
    assert masked["password"] == "!secret router_password"
    assert masked["verify_ssl"] is True
    assert masked["payload"] == '{"on": true}'
    assert "pw123" not in masked["urls"][0]
