"""Creating and deleting package files (issue #189).

Every package write tool (write_automation, write_script,
create_template_entity, write_package_platform_entity, write_rest_command,
write_customize) creates the `packages/<name>.yaml` its new content goes
into when it doesn't exist yet - after new_package_problem() finds nothing
wrong with it - so a new topic gets its own package without anyone
creating the file on the host. The new entry still goes through the
writing tool's own checks; there are no empty package files.

A new file is only of use if Home Assistant loads it. Package files are
loaded by `homeassistant: packages: !include_dir_named packages`, which
names each package after its file - so the name must not repeat one
already there, in any subfolder. `!include_dir_merge_named` expects each
file to hold `<package name>: {...}` mappings, which these tools don't
write; anything else (a list of explicit !includes) wouldn't pick a new
file up at all. Both are refused with the reason.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

from homeassistant.core import HomeAssistant
from ruamel.yaml import YAML

from .file_manager import FileManager

PACKAGES_DIR = "packages"
CONFIG_FILE = "configuration.yaml"
# packages/<name>.yaml, subfolders allowed: lowercase, digits, underscores.
_NEW_PACKAGE = re.compile(r"packages/(?:[a-z0-9_]+/)*[a-z0-9_]+\.yaml")


def _tag_and_value(node: Any) -> tuple[str | None, str | None]:
    tag = getattr(node, "tag", None)
    tag_value = getattr(tag, "value", tag)
    value = getattr(node, "value", node)
    return (
        str(tag_value) if tag_value else None,
        str(value) if isinstance(value, str) else None,
    )


def packages_loader(configuration: str) -> tuple[str | None, str | None]:
    """(how configuration.yaml loads packages, from which folder) - the
    `!include_dir_...` tag and its path, or (None, None)."""
    yaml = YAML(typ="rt")
    document = yaml.load(configuration) if configuration.strip() else None
    core = document.get("homeassistant") if isinstance(document, dict) else None
    packages = core.get("packages") if isinstance(core, dict) else None
    if packages is None:
        return None, None
    tag, folder = _tag_and_value(packages)
    if folder is not None:
        folder = folder.strip().removeprefix("./").rstrip("/")
    return tag, folder


def _stems(config_dir: Path) -> set[str]:
    folder = config_dir / PACKAGES_DIR
    if not folder.is_dir():
        return set()
    return {
        path.stem for pattern in ("*.yaml", "*.yml") for path in folder.rglob(pattern)
    }


async def new_package_problem(
    hass: HomeAssistant, file_manager: FileManager, file_path: str
) -> str | None:
    """Why `file_path` (a packages/ path that doesn't exist yet) can't be
    created, or None."""
    if not _NEW_PACKAGE.fullmatch(file_path):
        return (
            f"a new package's name is lowercase letters, digits and "
            f"underscores (e.g. house_state.yaml, heating/rooms.yaml), not "
            f"'{PurePosixPath(file_path).relative_to(PACKAGES_DIR)}'"
        )
    config_dir = Path(hass.config.config_dir)
    stem = PurePosixPath(file_path).stem
    if stem in await hass.async_add_executor_job(_stems, config_dir):
        return (
            f"a package named '{stem}' already exists in packages/ - "
            "!include_dir_named names each package after its file, so the "
            "two would clash; pick another name"
        )
    try:
        configuration = await file_manager.read_file(CONFIG_FILE)
    except (FileNotFoundError, PermissionError, ValueError) as err:
        return f"can't check how configuration.yaml loads packages: {err}"
    tag, folder = await hass.async_add_executor_job(packages_loader, configuration)
    if tag == "!include_dir_named" and folder == PACKAGES_DIR:
        return None
    if tag == "!include_dir_merge_named":
        return (
            "configuration.yaml loads packages with !include_dir_merge_named, "
            "where each file holds '<package name>: {...}' - a new file "
            "written here wouldn't fit that layout; create it on the host"
        )
    return (
        "configuration.yaml doesn't load packages/ with "
        "'homeassistant: packages: !include_dir_named packages', so Home "
        "Assistant wouldn't pick a new file up - add it there, or use an "
        "existing package"
    )


def is_empty(content: str) -> bool:
    """True for a package file with no configuration in it - only
    comments, blank lines, or `{}`."""
    document = YAML(typ="rt").load(content) if content.strip() else None
    return document is None or document == {}


def top_level_keys(content: str) -> list[str]:
    """A package's top-level keys - what keeps it from being empty."""
    document = YAML(typ="rt").load(content)
    return [str(key) for key in document] if isinstance(document, dict) else ["?"]
