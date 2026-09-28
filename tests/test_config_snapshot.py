"""Tests for config_snapshot.py: hand-edited config discovery, get_config_file,
and snapshots to the mirror repo (issue #105)."""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.ha_dev_tools import config_snapshot, mirror
from custom_components.ha_dev_tools.config_snapshot import ConfigFileError
from custom_components.ha_dev_tools.const import DOMAIN
from custom_components.ha_dev_tools.security import SecurityManager

_CONFIGURATION = (
    "homeassistant:\n"
    "  packages: !include_dir_named packages\n"
    "sensor: !include sensors.yaml\n"
    "template: !include_dir_merge_list templates/\n"
    "# retired: !include retired.yaml\n"
    "group: !include missing.yaml\n"
    'script: !include "scripts.yaml"\n'
)


def _write(root: Path, rel_path: str, content: str) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    hass.config.config_dir = str(tmp_path)
    hass.data.setdefault(DOMAIN, {})["security_manager"] = SecurityManager(hass, {})
    _write(tmp_path, "configuration.yaml", _CONFIGURATION)
    _write(tmp_path, "sensors.yaml", "- platform: time_date\n")
    _write(tmp_path, "scripts.yaml", "hello:\n  sequence: []\n")
    _write(tmp_path, "retired.yaml", "old: 1\n")
    _write(tmp_path, "templates/lights.yaml", "- sensor: []\n")
    _write(tmp_path, "templates/.hidden.yaml", "- sensor: []\n")
    _write(tmp_path, "packages/doorbird.yaml", "extra: !include ../extra.yaml\n")
    _write(tmp_path, "packages/sub/heating.yaml", "input_boolean: {}\n")
    _write(tmp_path, "packages/secrets.yaml", "door_password: hunter2\n")
    _write(tmp_path, "extra.yaml", "a: 1\n")
    _write(tmp_path, "custom_templates/macros.jinja", "{% macro x() %}{% endmacro %}\n")
    _write(tmp_path, "custom_templates/notes.txt", "not a template\n")
    _write(tmp_path, "secrets.yaml", "door_password: hunter2\n")
    return tmp_path


def _discover(hass: HomeAssistant, config_dir: Path) -> dict[str, str]:
    return config_snapshot.discover_files(
        str(config_dir), hass.data[DOMAIN]["security_manager"]
    )


def test_discover_files_follows_includes_like_ha(hass: HomeAssistant, config_dir):
    assert _discover(hass, config_dir) == {
        "configuration.yaml": "configuration.yaml",
        "packages/doorbird.yaml": "!include_dir_named in configuration.yaml",
        "extra.yaml": "!include in packages/doorbird.yaml",
        "packages/sub/heating.yaml": "!include_dir_named in configuration.yaml",
        "sensors.yaml": "!include in configuration.yaml",
        "templates/lights.yaml": "!include_dir_merge_list in configuration.yaml",
        "scripts.yaml": "!include in configuration.yaml",
        "custom_templates/macros.jinja": "custom_templates",
    }


def test_discover_files_never_leaves_config_dir(
    hass: HomeAssistant, config_dir, tmp_path_factory
):
    outside = tmp_path_factory.mktemp("outside") / "shadow.yaml"
    outside.write_text("a: 1\n")
    (config_dir / "packages/link.yaml").symlink_to(outside)
    _write(config_dir, "packages/up.yaml", f"x: !include {outside}\n")

    _write(config_dir, "packages/cert.yaml", "key: !include ../ssl/key.pem\n")
    _write(config_dir, "ssl/key.pem", "-----BEGIN PRIVATE KEY-----\n")
    found = _discover(hass, config_dir)
    assert "ssl/key.pem" not in found  # only YAML/JSON can be !included
    assert "packages/link.yaml" not in found
    assert not any("shadow" in path for path in found)
    assert "packages/up.yaml" in found


def test_discover_files_respects_the_read_allowlist(hass: HomeAssistant, config_dir):
    """configuration.yaml not readable: neither it nor its includes are."""
    security = SecurityManager(hass, {"read_paths": ["/config/packages/**/*.yaml"]})
    found = config_snapshot.discover_files(str(config_dir), security)
    assert found == {
        "packages/doorbird.yaml": "packages",
        "extra.yaml": "!include in packages/doorbird.yaml",
        "packages/sub/heating.yaml": "packages",
    }


def test_discover_files_tolerates_an_unreadable_include_source(
    hass: HomeAssistant, config_dir
):
    with patch.object(Path, "read_text", side_effect=OSError("denied")):
        found = _discover(hass, config_dir)
    # Still listed; its includes just can't be followed.
    assert "configuration.yaml" in found
    assert "sensors.yaml" not in found


# --- get_config_file ----------------------------------------------------------


@pytest.mark.asyncio
async def test_get_config_file_lists_covered_files(hass: HomeAssistant, config_dir):
    result = await config_snapshot.get_config_file(hass)
    assert {"path": "sensors.yaml", "via": "!include in configuration.yaml"} in (
        result["files"]
    )
    assert not any("secrets" in item["path"] for item in result["files"])


@pytest.mark.asyncio
async def test_get_config_file_returns_raw_text_with_tags(
    hass: HomeAssistant, config_dir
):
    result = await config_snapshot.get_config_file(hass, "/config/configuration.yaml")
    assert result == {
        "path": "configuration.yaml",
        "source": "live",
        "via": "configuration.yaml",
        "content": _CONFIGURATION,
    }


@pytest.mark.asyncio
async def test_get_config_file_reads_a_broken_file(hass: HomeAssistant, config_dir):
    """The incident behind #105: a truncated paste broke the file. It still
    reads, so the broken block can be seen and repaired."""
    broken = 'rest_command:\n  door:\n    payload: \'{"a": [1, 2>\n'
    _write(config_dir, "packages/doorbird.yaml", broken)
    result = await config_snapshot.get_config_file(hass, "packages/doorbird.yaml")
    assert result["content"] == broken


@pytest.mark.asyncio
async def test_get_config_file_withholds_a_file_with_a_credential(
    hass: HomeAssistant, config_dir
):
    _write(config_dir, "sensors.yaml", "- platform: rest\n  password: hunter2\n")
    with pytest.raises(ConfigFileError) as err:
        await config_snapshot.get_config_file(hass, "sensors.yaml")
    assert "line 2: password" in str(err.value)
    assert "hunter2" not in str(err.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["secrets.yaml", "packages/secrets.yaml", "retired.yaml", "../etc/passwd"]
)
async def test_get_config_file_refuses_files_it_does_not_cover(
    hass: HomeAssistant, config_dir, path
):
    with pytest.raises(ConfigFileError, match="isn't a hand-edited config file"):
        await config_snapshot.get_config_file(hass, path)


@pytest.mark.asyncio
async def test_get_config_file_refuses_oversized_and_undecodable_files(
    hass: HomeAssistant, config_dir
):
    with patch.object(config_snapshot, "MAX_FILE_BYTES", 5):
        with pytest.raises(ConfigFileError, match="larger than 5 bytes"):
            await config_snapshot.get_config_file(hass, "sensors.yaml")
    (config_dir / "extra.yaml").write_bytes(b"a: \xff\n")
    with pytest.raises(ConfigFileError, match="Reading 'extra.yaml' failed"):
        await config_snapshot.get_config_file(hass, "extra.yaml")


@pytest.mark.asyncio
async def test_get_config_file_from_the_mirror(hass: HomeAssistant, config_dir):
    with pytest.raises(ConfigFileError, match="Mirroring isn't set up"):
        await config_snapshot.get_config_file(hass, "sensors.yaml", "mirror")

    with patch.object(mirror, "is_mirror_enabled", return_value=True):
        with patch.object(
            mirror, "read_mirrored", AsyncMock(return_value="- good: 1\n")
        ):
            result = await config_snapshot.get_config_file(
                hass, "sensors.yaml", "mirror"
            )
        assert result["source"] == "mirror"
        assert result["content"] == "- good: 1\n"

        with patch.object(mirror, "read_mirrored", AsyncMock(return_value=None)):
            with pytest.raises(ConfigFileError, match="has no copy"):
                await config_snapshot.get_config_file(hass, "sensors.yaml", "mirror")

        with patch.object(
            mirror, "read_mirrored", AsyncMock(side_effect=RuntimeError("HTTP 500"))
        ):
            with pytest.raises(ConfigFileError, match="HTTP 500"):
                await config_snapshot.get_config_file(hass, "sensors.yaml", "mirror")

        with patch.object(
            mirror, "read_mirrored", AsyncMock(return_value="token: abc\n")
        ):
            with pytest.raises(ConfigFileError, match="withheld"):
                await config_snapshot.get_config_file(hass, "sensors.yaml", "mirror")


# --- snapshots ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_snapshot_pushes_every_readable_file(
    hass: HomeAssistant, config_dir
):
    (config_dir / "extra.yaml").write_bytes(b"a: \xff\n")
    pushed = AsyncMock(
        return_value=mirror.SnapshotResult(
            committed=("configuration.yaml",),
            unchanged=("sensors.yaml",),
            skipped=(("scripts.yaml", "credential"),),
        )
    )
    with patch.object(mirror, "mirror_snapshots", pushed):
        result = await config_snapshot.async_snapshot(hass)

    files = dict(pushed.call_args.args[1])
    assert files["configuration.yaml"] == _CONFIGURATION
    assert "extra.yaml" not in files
    assert not any("secrets" in path for path in files)
    assert result["committed"] == ["configuration.yaml"]
    assert result["unchanged"] == 1
    assert result["skipped"][0]["path"] == "extra.yaml"
    assert result["skipped"][0]["reason"].startswith("not read:")
    assert result["skipped"][1] == {"path": "scripts.yaml", "reason": "credential"}


@pytest.mark.asyncio
async def test_async_snapshot_if_valid_only_snapshots_a_passing_config(
    hass: HomeAssistant, config_dir
):
    assert await config_snapshot.async_snapshot_if_valid(hass) is None

    snapshot = AsyncMock(return_value={"committed": []})
    with (
        patch.object(mirror, "is_mirror_enabled", return_value=True),
        patch.object(config_snapshot, "async_snapshot", snapshot),
        patch.object(
            config_snapshot,
            "async_check_ha_config_file",
            AsyncMock(return_value=SimpleNamespace(errors=["bad"])),
        ),
    ):
        result = await config_snapshot.async_snapshot_if_valid(hass)
    assert "configuration check failed" in result["skipped_all"]
    snapshot.assert_not_called()

    with (
        patch.object(mirror, "is_mirror_enabled", return_value=True),
        patch.object(config_snapshot, "async_snapshot", snapshot),
        patch.object(
            config_snapshot,
            "async_check_ha_config_file",
            AsyncMock(return_value=SimpleNamespace(errors=[])),
        ),
    ):
        assert await config_snapshot.async_snapshot_if_valid(hass) == {"committed": []}


@pytest.mark.asyncio
async def test_snapshots_run_at_start_and_daily(hass: HomeAssistant, config_dir):
    run = AsyncMock(side_effect=[None, RuntimeError("boom"), None])
    with patch.object(config_snapshot, "async_snapshot_if_valid", run):
        hass.set_state(CoreState.starting)
        unsub = config_snapshot.async_setup_snapshots(hass)
        await hass.async_block_till_done()
        run.assert_not_called()

        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        assert run.call_count == 1

        # A failing run is logged, not raised, and the schedule keeps going.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(days=1, seconds=1))
        await hass.async_block_till_done()
        assert run.call_count == 2

        unsub()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(days=3))
        await hass.async_block_till_done()
        assert run.call_count == 2


@pytest.mark.asyncio
async def test_snapshots_start_right_away_when_ha_is_running(
    hass: HomeAssistant, config_dir
):
    run = AsyncMock()
    with patch.object(config_snapshot, "async_snapshot_if_valid", run):
        unsub = config_snapshot.async_setup_snapshots(hass)
        await hass.async_block_till_done()
        unsub()
    run.assert_called_once_with(hass)


@pytest.mark.asyncio
async def test_unsubscribing_before_start_removes_the_start_listener(
    hass: HomeAssistant, config_dir
):
    run = AsyncMock()
    with patch.object(config_snapshot, "async_snapshot_if_valid", run):
        hass.set_state(CoreState.starting)
        config_snapshot.async_setup_snapshots(hass)()
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
    run.assert_not_called()


@pytest.mark.asyncio
async def test_async_snapshot_if_valid_uses_ha_real_config_check(
    hass: HomeAssistant, config_dir
):
    """Not mocked: HA's own check decides, on real files."""
    snapshot = AsyncMock(return_value={"committed": []})
    _write(config_dir, "configuration.yaml", "homeassistant:\n")
    with (
        patch.object(mirror, "is_mirror_enabled", return_value=True),
        patch.object(config_snapshot, "async_snapshot", snapshot),
    ):
        assert await config_snapshot.async_snapshot_if_valid(hass) == {"committed": []}
        _write(config_dir, "configuration.yaml", "homeassistant:\n  bad: [\n")
        result = await config_snapshot.async_snapshot_if_valid(hass)
    assert "configuration check failed" in result["skipped_all"]
    snapshot.assert_called_once()
