"""ESPHome configs in /config/esphome (issue #197)."""

from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools import esphome_manager
from custom_components.ha_dev_tools.esphome_manager import (
    PLACEHOLDER,
    EsphomeConfigError,
    EsphomeManager,
    esphome_path,
    list_configs,
    mask,
    restore,
)
from custom_components.ha_dev_tools.file_manager import FileManager
from custom_components.ha_dev_tools.llm_api import (
    DeleteEsphomeConfigTool,
    GetEsphomeConfigTool,
    ListEsphomeConfigsTool,
    WriteEsphomeConfigTool,
)
from custom_components.ha_dev_tools.security import SecurityManager
from tests.test_llm_api import _arm, _llm_context

KITCHEN = """\
esphome:
  name: kitchen
wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password
  ap:
    password: "fallback123"  # captive portal
api:
  encryption:
    key: "abcDEF123+/xyz=="
ota:
  - platform: esphome
    password: 'ota-pass'
web_server: {port: 80, auth: {username: admin, password: webpw}}
sensor:
  - platform: template
    name: Kitchen fake
    lambda: !lambda |-
      return 1.0;
"""
SECRETS = ("fallback123", "abcDEF123+/xyz==", "ota-pass", "webpw")


@pytest.fixture
def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    hass.config.config_dir = str(tmp_path)
    esphome = tmp_path / "esphome"
    (esphome / "common").mkdir(parents=True)
    (esphome / ".esphome" / "build").mkdir(parents=True)
    (esphome / "kitchen.yaml").write_text(KITCHEN)
    (esphome / "common" / "wifi.yaml").write_text("wifi:\n  ssid: !secret x\n")
    (esphome / "secrets.yaml").write_text("wifi_password: hunter2\n")
    (esphome / ".esphome" / "build" / "kitchen.yaml").write_text("x: 1\n")
    (esphome / "notes.txt").write_text("not yaml\n")
    return tmp_path


@pytest.fixture
def manager(hass: HomeAssistant, config_dir) -> EsphomeManager:
    # The defaults: esphome/**/*.yaml writable, its secrets and build denied.
    return EsphomeManager(hass, FileManager(hass, SecurityManager(hass, {})))


def test_esphome_path():
    assert esphome_path("kitchen.yaml") == "esphome/kitchen.yaml"
    assert esphome_path("common/wifi.yml") == "esphome/common/wifi.yml"
    for bad in ("../configuration.yaml", ".esphome/x.yaml", "/x.yaml", "a.txt", " "):
        with pytest.raises(EsphomeConfigError, match="inside esphome/"):
            esphome_path(bad)
    with pytest.raises(EsphomeConfigError, match="secrets.yaml is never"):
        esphome_path("common/secrets.yaml")


def test_list_configs(config_dir):
    configs = list_configs(str(config_dir))
    assert [(c["name"], c["kind"]) for c in configs] == [
        ("common/wifi.yaml", "include"),
        ("kitchen.yaml", "device"),
    ]
    assert configs[1]["size"] == len(KITCHEN)
    assert list_configs(str(config_dir / "nowhere")) == []


def test_mask_and_restore_round_trip():
    masked, where = mask(KITCHEN)
    assert where == [
        "wifi.ap.password",
        "api.encryption.key",
        "ota[0].password",
        "web_server.auth.password",
    ]
    assert not any(secret in masked for secret in SECRETS)
    assert "!secret wifi_password" in masked and "!lambda |-" in masked
    assert f'"{PLACEHOLDER}"  # captive portal' in masked
    assert 'auth: {username: admin, password: "<withheld>"}}' in masked
    # Sent back unchanged: byte for byte the original.
    assert restore(masked, KITCHEN) == KITCHEN
    # One field edited: only that line differs.
    edited = masked.replace("name: kitchen", "name: kitchen2")
    assert restore(edited, KITCHEN) == KITCHEN.replace(
        "name: kitchen", "name: kitchen2"
    )


def test_restore_refusals_and_moves():
    masked, _ = mask(KITCHEN)
    # A new literal credential: !secret only.
    with pytest.raises(EsphomeConfigError, match="mqtt.password: a new literal"):
        restore(masked + "mqtt:\n  password: newpw\n", KITCHEN)
    # A mask moved under another parent, its key unique: put back.
    moved = masked.replace("api:\n  encryption:\n", "api:\n  crypt:\n")
    assert "abcDEF123+/xyz==" in restore(moved, KITCHEN)
    # Two passwords could fit: refused, never guessed.
    two = masked + f'mqtt:\n  password: "{PLACEHOLDER}"\n'
    with pytest.raises(EsphomeConfigError, match="mqtt.password: a masked credential"):
        restore(two, KITCHEN)
    # A new file has nothing to put back.
    with pytest.raises(EsphomeConfigError, match="matches nothing"):
        restore(f'api:\n  encryption:\n    key: "{PLACEHOLDER}"\n', None)
    with pytest.raises(EsphomeConfigError, match="not valid YAML"):
        restore("a: [\n", KITCHEN)
    with pytest.raises(EsphomeConfigError, match="multi-line"):
        mask("ota:\n  password: |\n    secret\n")
    assert mask("wifi:\n  password: !secret pw\n") == (
        "wifi:\n  password: !secret pw\n",
        [],
    )
    assert esphome_manager._token_end('k: "a\\"b" # c', 3) == 9
    assert esphome_manager._token_end("k: 'it''s'", 3) == 10
    assert esphome_manager._token_end('k: "open', 3) == 8


async def test_security_keeps_secrets_and_builds_out(hass: HomeAssistant, manager):
    files = manager.file_manager
    await files.write_file("esphome/new.yaml", "esphome:\n  name: new\n")
    for denied in ("esphome/secrets.yaml", "esphome/.esphome/build/kitchen.yaml"):
        with pytest.raises(PermissionError):
            await files.write_file(denied, "x: 1\n")


async def test_tools(hass: HomeAssistant, manager, config_dir):
    admin = MockUser(is_owner=True).add_to_hass(hass)
    _arm(hass)

    async def call(tool, args):
        return (
            await tool.async_call(
                hass,
                llm.ToolInput(tool_name=tool.name, tool_args=args),
                _llm_context(admin.id),
            )
        ).data

    listed = await call(ListEsphomeConfigsTool(manager), {})
    assert [c["name"] for c in listed["configs"]] == [
        "common/wifi.yaml",
        "kitchen.yaml",
    ]

    get = GetEsphomeConfigTool(manager)
    read = await call(get, {"name": "kitchen.yaml"})
    assert not any(secret in read["content"] for secret in SECRETS)
    assert (
        "secrets.yaml is never" in (await call(get, {"name": "secrets.yaml"}))["error"]
    )
    assert "there is no" in (await call(get, {"name": "gone.yaml"}))["error"]

    write = WriteEsphomeConfigTool(manager)
    edited = read["content"].replace("name: kitchen", "name: kitchen2")
    args = {
        "name": "kitchen.yaml",
        "content": edited,
        "expected_hash": read["content_hash"],
    }
    proposal = await call(write, args)
    assert proposal["creates_file"] is False
    assert "-  name: kitchen\n+  name: kitchen2" in proposal["diff"]
    assert not any(secret in str(proposal) for secret in SECRETS)
    done = await call(write, {**args, "confirm_token": proposal["confirm_token"]})
    assert done["written"] == "esphome/kitchen.yaml"
    assert not any(secret in str(done) for secret in SECRETS)
    on_disk = (config_dir / "esphome" / "kitchen.yaml").read_text()
    assert on_disk == KITCHEN.replace("name: kitchen", "name: kitchen2")

    refused = await call(
        write, {"name": "kitchen.yaml", "content": edited + "mqtt:\n  password: x1\n"}
    )
    assert "a new literal credential" in refused["error"]
    # A stale expected_hash: refused at write time.
    stale = {**args, "content": edited + "# more\n"}
    proposal = await call(write, stale)
    failed = await call(write, {**stale, "confirm_token": proposal["confirm_token"]})
    assert "Hash conflict" in failed["error"]
    new = await call(
        write, {"name": "garage.yaml", "content": "esphome:\n  name: garage\n"}
    )
    assert new["creates_file"] is True and new["diff"].startswith("--- /dev/null")

    delete = DeleteEsphomeConfigTool(manager)
    assert "there is no" in (await call(delete, {"name": "gone.yaml"}))["error"]
    assert "inside esphome/" in (await call(delete, {"name": "../x.yaml"}))["error"]
    proposal = await call(delete, {"name": "kitchen.yaml"})
    assert proposal["would_delete"] == "esphome/kitchen.yaml"
    assert not any(secret in proposal["content"] for secret in SECRETS)
    gone = await call(
        delete, {"name": "kitchen.yaml", "confirm_token": proposal["confirm_token"]}
    )
    assert not (config_dir / "esphome" / "kitchen.yaml").exists()
    assert (config_dir / gone["backup"]).read_text() == on_disk
    # Deleted meanwhile: the write path reports it, nothing raised.
    proposal = await call(delete, {"name": "common/wifi.yaml"})
    (config_dir / "esphome" / "common" / "wifi.yaml").unlink()
    late = await call(
        delete, {"name": "common/wifi.yaml", "confirm_token": proposal["confirm_token"]}
    )
    assert "there is no" in late["error"]
    with pytest.raises(EsphomeConfigError, match="there is no"):
        await manager.delete("gone.yaml")


async def test_delete_without_backup_deletes_nothing(
    hass: HomeAssistant, manager, config_dir
):
    admin = MockUser(is_owner=True).add_to_hass(hass)
    _arm(hass)
    tool = DeleteEsphomeConfigTool(manager)

    async def call(args):
        return (
            await tool.async_call(
                hass,
                llm.ToolInput(tool_name=tool.name, tool_args=args),
                _llm_context(admin.id),
            )
        ).data

    proposal = await call({"name": "kitchen.yaml"})
    with patch.object(
        manager.file_manager, "_backup_sync", side_effect=OSError("disk full")
    ):
        failed = await call(
            {"name": "kitchen.yaml", "confirm_token": proposal["confirm_token"]}
        )
    assert "disk full" in failed["error"]
    assert (config_dir / "esphome" / "kitchen.yaml").read_text() == KITCHEN
