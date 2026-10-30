"""Dashboards in the mirror repo (issue #192): the .storage file a save
lands in, a save that can't be mirrored, and dashboards in snapshots."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools import config_snapshot, dashboard_manager, mirror
from custom_components.ha_dev_tools.const import (
    DOMAIN,
    OPT_MIRROR_ENABLED,
    OPT_MIRROR_REPO,
    OPT_MIRROR_TOKEN,
)
from custom_components.ha_dev_tools.llm_api import WriteDashboardTool
from custom_components.ha_dev_tools.security import SecurityManager
from custom_components.ha_dev_tools.ws_call import call_ws_command
from tests.test_llm_api import _llm_context


@pytest.fixture
async def admin(hass: HomeAssistant):
    assert await async_setup_component(hass, "websocket_api", {})
    assert await async_setup_component(hass, "lovelace", {})
    return MockUser(is_owner=True).add_to_hass(hass)


async def _create(hass, user, url_path: str) -> dict:
    return await call_ws_command(
        hass,
        user,
        "lovelace/dashboards/create",
        url_path=url_path,
        title=url_path,
        mode="storage",
    )


async def test_storage_path_follows_ha(hass: HomeAssistant, admin):
    # No "lovelace" dashboard: a save without url_path goes to the legacy
    # default store.
    assert (
        await dashboard_manager.storage_path(hass, admin, None) == ".storage/lovelace"
    )
    assert await dashboard_manager.storage_path(hass, admin, "nope-x") is None

    guest = await _create(hass, admin, "dashboard-guest")
    assert guest["id"] == "dashboard_guest"
    assert (
        await dashboard_manager.storage_path(hass, admin, "dashboard-guest")
        == ".storage/lovelace.dashboard_guest"
    )
    # Once HA's migration made the Overview a "lovelace" dashboard (it does
    # that at start-up, before the default panel exists - not reproducible
    # through the WS API here), a save without url_path goes there.
    migrated = [
        {**guest, "mode": "storage"},
        {"id": "lovelace", "url_path": "lovelace", "mode": "storage"},
    ]
    with patch.object(
        dashboard_manager, "call_ws_command", AsyncMock(return_value=migrated)
    ):
        assert (
            await dashboard_manager.storage_path(hass, admin, None)
            == ".storage/lovelace.lovelace"
        )


async def test_a_save_that_cant_be_mirrored_says_so(
    hass: HomeAssistant, setup_integration_with_entry, admin
):
    tool = WriteDashboardTool()
    args = {"url_path": "dashboard-guest", "config": {"views": []}}
    await _create(hass, admin, "dashboard-guest")

    # Mirroring off: saved, no storage lookups at all.
    lookups = AsyncMock()
    with patch("custom_components.ha_dev_tools.llm_api._read_storage_file", lookups):
        result = await tool._write(
            hass,
            llm.ToolInput(tool_name="write_dashboard", tool_args=args),
            _llm_context(admin.id),
        )
    assert "mirror" not in result and lookups.await_count == 0

    hass.config_entries.async_update_entry(
        setup_integration_with_entry,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    reads = AsyncMock(return_value=None)
    with patch("custom_components.ha_dev_tools.llm_api._read_storage_file", reads):
        result = await tool._write(
            hass,
            llm.ToolInput(tool_name="write_dashboard", tool_args=args),
            _llm_context(admin.id),
        )
    assert reads.await_args.args[1] == ".storage/lovelace.dashboard_guest"
    assert result["mirror"]["mirrored"] is False
    assert "lovelace.dashboard_guest" in result["mirror"]["reason"]

    # A dashboard HA lists no storage file for (YAML mode, gone).
    with (
        patch("custom_components.ha_dev_tools.llm_api._read_storage_file", reads),
        patch.object(dashboard_manager, "write_dashboard", AsyncMock()),
    ):
        result = await tool._write(
            hass,
            llm.ToolInput(
                tool_name="write_dashboard",
                tool_args={"url_path": "yaml-dash", "config": {"views": []}},
            ),
            _llm_context(admin.id),
        )
    assert "no storage file" in result["mirror"]["reason"]


def test_dashboard_files(hass: HomeAssistant, tmp_path: Path):
    storage = tmp_path / ".storage"
    storage.mkdir()
    for name in (
        "lovelace",
        "lovelace.lovelace",
        "lovelace.dashboard_guest",
        "lovelace_dashboards",
        "lovelace_resources",
        "core.config_entries",
    ):
        (storage / name).write_text("{}")
    security = SecurityManager(hass, {})
    assert config_snapshot.dashboard_files(str(tmp_path), security) == [
        ".storage/lovelace",
        ".storage/lovelace.dashboard_guest",
        ".storage/lovelace.lovelace",
    ]
    closed = SecurityManager(hass, {"read_paths": ["/config/configuration.yaml"]})
    assert config_snapshot.dashboard_files(str(tmp_path), closed) == []
    assert config_snapshot.dashboard_files(str(tmp_path / "none"), security) == []


async def test_snapshots_carry_dashboards(hass: HomeAssistant, tmp_path: Path):
    hass.config.config_dir = str(tmp_path)
    hass.data.setdefault(DOMAIN, {})["security_manager"] = SecurityManager(hass, {})
    (tmp_path / "configuration.yaml").write_text("homeassistant: {}\n")
    (tmp_path / ".storage").mkdir()
    (tmp_path / ".storage" / "lovelace.lovelace").write_text('{"data": {}}')
    pushed = AsyncMock(return_value=mirror.SnapshotResult())
    with patch.object(mirror, "mirror_snapshots", pushed):
        await config_snapshot.async_snapshot(hass)
    files = dict(pushed.call_args.args[1])
    assert files[".storage/lovelace.lovelace"] == '{"data": {}}'
    assert "configuration.yaml" in files
