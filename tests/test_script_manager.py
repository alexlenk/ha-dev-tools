"""Tests for the layout-aware, package-safe ScriptManager.

Mirrors test_automation_manager.py's structure and fixtures - see that
file's module docstring for why these are the highest-value tests in
this family: they verify a package-defined script is found and edited
through its real file, never silently duplicated into scripts.yaml or
missed entirely.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml as pyyaml
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools.file_manager import FileManager
from custom_components.ha_dev_tools.script_manager import (
    DuplicateScriptIdError,
    ScriptManager,
    ScriptNotFoundError,
)
from custom_components.ha_dev_tools.security import SecurityManager


@pytest.fixture
def security_manager(hass: HomeAssistant):
    """SecurityManager configured to allow reading and writing script files."""
    return SecurityManager(
        hass,
        {
            "read_paths": ["scripts.yaml", "packages/**/*.yaml"],
            "write_paths": ["scripts.yaml", "packages/**/*.yaml"],
            "denied_paths": [],
        },
    )


@pytest.fixture
def file_manager(hass: HomeAssistant, security_manager, tmp_path):
    """FileManager pointed at a real temp config directory."""
    hass.config.config_dir = str(tmp_path)
    return FileManager(hass, security_manager)


@pytest.fixture
def script_manager(hass: HomeAssistant, file_manager):
    """ScriptManager under test."""
    return ScriptManager(hass, file_manager)


@pytest.fixture(autouse=True)
def mock_reload_service(hass: HomeAssistant):
    """Register a fake script.reload service so write_script's real
    hass.services.async_call succeeds without loading the whole script
    integration, while still exercising the real service-call machinery."""
    mock = AsyncMock()
    hass.services.async_register("script", "reload", mock)
    return mock


def _write(tmp_path: Path, rel_path: str, content: str) -> None:
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)


@pytest.mark.asyncio
async def test_find_script_in_default_file(script_manager, tmp_path):
    _write(tmp_path, "scripts.yaml", "abc:\n  sequence: []\n")

    location = await script_manager.find_script("abc")

    assert location.file_path == "scripts.yaml"
    assert location.is_package is False


@pytest.mark.asyncio
async def test_find_script_in_package(script_manager, tmp_path):
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "script:\n  solar_charge:\n    sequence: []\n"
        "input_boolean:\n  unrelated_helper: {}\n",
    )

    location = await script_manager.find_script("solar_charge")

    assert location.file_path == "packages/emhas.yaml"
    assert location.is_package is True


@pytest.mark.asyncio
async def test_find_script_not_found_raises(script_manager, tmp_path):
    _write(tmp_path, "scripts.yaml", "abc:\n  sequence: []\n")

    with pytest.raises(ScriptNotFoundError):
        await script_manager.find_script("does_not_exist")


@pytest.mark.asyncio
async def test_find_script_duplicate_across_files_raises(script_manager, tmp_path):
    _write(tmp_path, "scripts.yaml", "dup:\n  sequence: []\n")
    _write(tmp_path, "packages/emhas.yaml", "script:\n  dup:\n    sequence: []\n")

    with pytest.raises(DuplicateScriptIdError) as exc_info:
        await script_manager.find_script("dup")

    file_paths = {loc.file_path for loc in exc_info.value.locations}
    assert file_paths == {"scripts.yaml", "packages/emhas.yaml"}


@pytest.mark.asyncio
async def test_get_script_returns_config(script_manager, tmp_path):
    _write(tmp_path, "scripts.yaml", "abc:\n  alias: My Script\n  sequence: []\n")

    location, config = await script_manager.get_script("abc")

    assert location.file_path == "scripts.yaml"
    assert config["alias"] == "My Script"


@pytest.mark.asyncio
async def test_all_scripts_across_files(script_manager, tmp_path):
    _write(tmp_path, "scripts.yaml", "abc:\n  alias: Default\n  sequence: []\n")
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "script:\n  pkg_script:\n    alias: Pkg\n    sequence: []\n",
    )

    results = await script_manager.all_scripts()

    by_id = {script_id: (location, config) for location, script_id, config in results}
    assert by_id["abc"][0].file_path == "scripts.yaml"
    assert by_id["pkg_script"][0].file_path == "packages/emhas.yaml"
    assert by_id["pkg_script"][1]["alias"] == "Pkg"


@pytest.mark.asyncio
async def test_write_script_updates_existing_in_package_preserves_other_content(
    script_manager, tmp_path, mock_reload_service
):
    _write(
        tmp_path,
        "packages/emhas.yaml",
        "# EMHAS package - hand maintained, do not reformat\n"
        "script:\n"
        "  solar_charge:\n"
        "    alias: Old alias\n"
        "    sequence: []\n"
        "input_boolean:\n"
        "  unrelated_helper: {}\n",
    )

    result = await script_manager.write_script(
        "solar_charge", {"alias": "New alias", "sequence": []}
    )

    assert result.location.file_path == "packages/emhas.yaml"
    assert result.location.is_package is True
    mock_reload_service.assert_called_once()

    raw = (tmp_path / "packages/emhas.yaml").read_text()
    assert "# EMHAS package - hand maintained, do not reformat" in raw
    assert "unrelated_helper" in raw

    parsed = pyyaml.safe_load(raw)
    assert parsed["input_boolean"]["unrelated_helper"] == {}
    assert parsed["script"]["solar_charge"]["alias"] == "New alias"


@pytest.mark.asyncio
async def test_write_script_creates_new_in_default_file(
    script_manager, tmp_path, mock_reload_service
):
    assert not (tmp_path / "scripts.yaml").exists()

    result = await script_manager.write_script(
        "brand_new", {"alias": "Brand new", "sequence": []}
    )

    assert result.location.file_path == "scripts.yaml"
    assert result.content_before is None
    assert "brand_new" in result.content_after
    mock_reload_service.assert_called_once()

    parsed = pyyaml.safe_load((tmp_path / "scripts.yaml").read_text())
    assert parsed["brand_new"]["alias"] == "Brand new"


@pytest.mark.asyncio
async def test_write_script_creates_new_with_explicit_package(
    script_manager, tmp_path, mock_reload_service
):
    _write(tmp_path, "packages/emhas.yaml", "script: {}\n")

    result = await script_manager.write_script(
        "new_in_package",
        {"alias": "New in package", "sequence": []},
        package="emhas.yaml",
    )

    assert result.location.file_path == "packages/emhas.yaml"
    parsed = pyyaml.safe_load((tmp_path / "packages/emhas.yaml").read_text())
    assert parsed["script"]["new_in_package"]["alias"] == "New in package"


@pytest.mark.asyncio
async def test_write_script_dry_run_computes_content_without_writing(
    script_manager, tmp_path, mock_reload_service
):
    """dry_run=True resolves the same location and builds the same
    content_after a real write would, but never touches the file or
    reloads."""
    _write(tmp_path, "scripts.yaml", "existing:\n  alias: Old\n  sequence: []\n")

    result = await script_manager.write_script(
        "existing", {"alias": "New", "sequence": []}, dry_run=True
    )

    assert result.location.file_path == "scripts.yaml"
    assert "Old" in result.content_before
    assert "New" in result.content_after
    assert "Old" in (tmp_path / "scripts.yaml").read_text()
    assert "New" not in (tmp_path / "scripts.yaml").read_text()
    mock_reload_service.assert_not_called()


@pytest.mark.asyncio
async def test_write_script_quotes_ambiguous_scalars(
    script_manager, tmp_path, mock_reload_service
):
    """Same live-tested regression as write_automation's equivalent test
    (issue found via a real safety automation's state conditions) -
    scripts splice new config the same way, so they're equally exposed."""
    result = await script_manager.write_script(
        "state_check",
        {
            "alias": "Uses on/off",
            "sequence": [
                {
                    "condition": "state",
                    "entity_id": "switch.x",
                    "state": "off",
                }
            ],
        },
    )

    assert 'state: "off"' in result.content_after

    parsed = pyyaml.safe_load((tmp_path / "scripts.yaml").read_text())
    assert parsed["state_check"]["sequence"][0]["state"] == "off"


@pytest.mark.asyncio
async def test_write_script_missing_package_raises(script_manager, tmp_path):
    with pytest.raises(ScriptNotFoundError):
        await script_manager.write_script(
            "id1", {"sequence": []}, package="does_not_exist.yaml"
        )


@pytest.mark.asyncio
async def test_write_script_duplicate_id_refuses_to_guess(script_manager, tmp_path):
    _write(tmp_path, "scripts.yaml", "dup:\n  sequence: []\n")
    _write(tmp_path, "packages/emhas.yaml", "script:\n  dup:\n    sequence: []\n")

    with pytest.raises(DuplicateScriptIdError):
        await script_manager.write_script("dup", {"sequence": []})


@pytest.mark.asyncio
async def test_write_script_hash_conflict_raises(script_manager, tmp_path):
    _write(tmp_path, "scripts.yaml", "abc:\n  sequence: []\n")

    with pytest.raises(ValueError, match="Hash conflict"):
        await script_manager.write_script(
            "abc", {"alias": "changed", "sequence": []}, expected_hash="wrong-hash"
        )


# --- _script_map edge cases (via all_scripts) -------------------------------------


@pytest.mark.asyncio
async def test_all_scripts_empty_default_file_yields_nothing(script_manager, tmp_path):
    """An empty scripts.yaml parses to None, not {}: _script_map must treat
    that as an empty mapping rather than raising or crashing."""
    _write(tmp_path, "scripts.yaml", "")

    results = await script_manager.all_scripts()

    assert results == []


@pytest.mark.asyncio
async def test_all_scripts_default_file_not_a_mapping_raises(script_manager, tmp_path):
    """scripts.yaml must be a mapping at its root - a list there is an
    invalid configuration, and should be reported rather than silently
    treated as having zero scripts."""
    _write(tmp_path, "scripts.yaml", "- not\n- a\n- mapping\n")

    with pytest.raises(ValueError, match="does not contain a YAML mapping"):
        await script_manager.all_scripts()


@pytest.mark.asyncio
async def test_all_scripts_package_without_script_key_yields_nothing(
    script_manager, tmp_path
):
    """A package file that doesn't define the `script:` domain at all
    (e.g. only helpers) contributes nothing, rather than erroring."""
    _write(tmp_path, "packages/helpers_only.yaml", "input_boolean:\n  foo: {}\n")

    results = await script_manager.all_scripts()

    assert results == []


@pytest.mark.asyncio
async def test_all_scripts_package_script_key_wrong_type_raises(
    script_manager, tmp_path
):
    """A package's `script:` key must be a mapping - any other type (here,
    a bare scalar) is an invalid configuration."""
    _write(tmp_path, "packages/broken.yaml", "script: not_a_mapping\n")

    with pytest.raises(ValueError, match="is not a mapping"):
        await script_manager.all_scripts()


# --- _load_document direct coverage -----------------------------------------------


@pytest.mark.asyncio
async def test_load_document_returns_none_for_missing_file(script_manager, tmp_path):
    """_load_document's FileNotFoundError->None fallback exists for callers
    reading a candidate file that may not have been created yet - exercised
    directly since candidate_files() itself never returns a nonexistent path."""
    document = await script_manager._load_document("scripts.yaml")

    assert document is None


# --- _build_content edge cases (via write_script) ---------------------------------


@pytest.mark.asyncio
async def test_write_script_into_empty_package_file(
    script_manager, tmp_path, mock_reload_service
):
    """Writing a new script into a package file that exists but is empty
    (no parsed document at all yet) must build a fresh document rather
    than crashing on a None document."""
    _write(tmp_path, "packages/empty.yaml", "")

    result = await script_manager.write_script(
        "first_one", {"alias": "First", "sequence": []}, package="empty.yaml"
    )

    assert result.location.file_path == "packages/empty.yaml"
    parsed = pyyaml.safe_load((tmp_path / "packages/empty.yaml").read_text())
    assert parsed["script"]["first_one"]["alias"] == "First"


@pytest.mark.asyncio
async def test_write_script_into_package_missing_script_key(
    script_manager, tmp_path, mock_reload_service
):
    """A package file with content but no `script:` key at all must get a
    fresh script mapping built for it, alongside its existing content."""
    _write(tmp_path, "packages/other_domain.yaml", "input_boolean:\n  foo: {}\n")

    result = await script_manager.write_script(
        "brand_new",
        {"alias": "Brand new", "sequence": []},
        package="other_domain.yaml",
    )

    assert result.location.file_path == "packages/other_domain.yaml"
    parsed = pyyaml.safe_load((tmp_path / "packages/other_domain.yaml").read_text())
    assert parsed["script"]["brand_new"]["alias"] == "Brand new"
    assert parsed["input_boolean"]["foo"] == {}
