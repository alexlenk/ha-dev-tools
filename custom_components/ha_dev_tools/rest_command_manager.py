"""Layout-aware, package-safe read-only `rest_command:` config access.

Issue #73: `get_automation`/`get_script` already resolve which file
actually defines an entry (the default file, or a `packages/*.yaml`) and
return its full config - `rest_command:` had no equivalent, so a
package-defined `rest_command` was only readable by having the owner
paste the file's contents manually.

Structurally, `rest_command:` is a mapping of command id -> config, same
shape as `script:` (see script_manager.py's module docstring for that
comparison) - one key difference from both `script:` and `automation:`
though: neither has a dedicated default include-file convention the way
`script: !include scripts.yaml` does. A `rest_command:` block can live
directly in `configuration.yaml`, or inside a `packages/*.yaml` file's own
`rest_command:` key - so `configuration.yaml` itself is always a read
candidate, not just packages (same reasoning as
template_yaml_manager.py's `candidate_files`, and it's already
`DEFAULT_READ_ONLY_PATHS` in security.py, so no security config change
was needed for this).

Read-only by design (see the issue) - no write/splice logic, no
ruamel round-trip dumper, no confirm-token write path. A write tool would
need its own safety review and is explicitly out of scope here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from ruamel.yaml import YAML

from .file_manager import FileManager

REST_COMMAND_KEY = "rest_command"
DEFAULT_CONFIG_FILE = "configuration.yaml"
PACKAGES_DIR = "packages"


def _new_yaml() -> YAML:
    """Return a ruamel.yaml instance for parsing.

    Round-trip type (not safe/plain PyYAML) purely for consistency with
    every other manager in this codebase reading the same files - nothing
    here re-dumps, so the round-trip-specific features (preserved
    quotes/comments) are unused, but a second YAML dialect reading the
    same files isn't worth the inconsistency.
    """
    return YAML(typ="rt")


def _load_yaml(content: str) -> Any:
    """Synchronous: build a fresh YAML() and load content with it, for
    hass.async_add_executor_job() - same reasoning as script_manager.py's
    helper of the same name: YAML(typ="rt")'s own __init__ does a blocking
    scandir, so a bare `_new_yaml().load` passed as the executor job's
    callable only defers load() to the executor - `_new_yaml()` itself is
    evaluated eagerly on the event loop before being passed in."""
    return _new_yaml().load(content)


@dataclass(frozen=True)
class RestCommandLocation:
    """Where a given rest_command id is defined."""

    file_path: str  # relative to the HA config dir
    is_package: bool


class RestCommandNotFoundError(Exception):
    """Raised when a rest_command id can't be resolved to any known file."""


class DuplicateRestCommandIdError(Exception):
    """Raised when a rest_command id is defined in more than one file.

    Same "refuse to guess" reasoning as DuplicateScriptIdError - rather
    than assume how HA's package merge resolves a dict key collision
    across files, this repo just never picks one on the caller's behalf.
    """

    def __init__(self, rest_command_id: str, locations: list[RestCommandLocation]) -> None:
        self.rest_command_id = rest_command_id
        self.locations = locations
        super().__init__(
            f"rest_command id '{rest_command_id}' is defined in more than "
            f"one file: {[loc.file_path for loc in locations]}"
        )


class RestCommandManager:
    """Layout-aware, package-safe read-only access to rest_command config."""

    def __init__(self, hass: HomeAssistant, file_manager: FileManager) -> None:
        """Initialize the rest_command manager."""
        self.hass = hass
        self.file_manager = file_manager
        self._config_dir = Path(hass.config.config_dir)

    async def candidate_files(self) -> list[str]:
        """Return every file that may define rest_command entries,
        configuration.yaml first.

        Unlike script_manager.py's candidate_files, configuration.yaml
        itself is always a candidate (not just packages) -
        rest_command: has no default include-file convention to gate on,
        same as template_yaml_manager.py's template: entries.
        """
        candidates: list[str] = [DEFAULT_CONFIG_FILE]
        candidates.extend(await self.hass.async_add_executor_job(self._glob_packages))
        return candidates

    def _glob_packages(self) -> list[str]:
        """Synchronous glob of packages/**/*.yaml, relative to the config dir."""
        packages_dir = self._config_dir / PACKAGES_DIR
        if not packages_dir.is_dir():
            return []
        return sorted(
            str(p.relative_to(self._config_dir)) for p in packages_dir.rglob("*.yaml")
        )

    async def _load_document(self, file_path: str) -> Any:
        """Load a candidate file's parsed YAML document.

        Returns None if the file doesn't exist yet - only relevant for a
        package file listed by a glob race; configuration.yaml always
        exists on a running instance, and candidate_files() only globs
        packages that already exist.
        """
        try:
            content = await self.file_manager.read_file(file_path)
        except FileNotFoundError:
            return None
        return await self.hass.async_add_executor_job(_load_yaml, content)

    def _rest_command_map(self, file_path: str, document: Any) -> dict | None:
        """Return the rest_command id -> config mapping within a loaded
        document, or None if it has none.

        Always nested under a `rest_command:` key, in configuration.yaml
        as much as in a package file - unlike script_manager.py's
        DEFAULT_SCRIPTS_FILE case, rest_command has no "root of the file
        is the mapping" convention to special-case.
        """
        if document is None or REST_COMMAND_KEY not in document:
            return None
        commands = document[REST_COMMAND_KEY]
        if not isinstance(commands, dict):
            raise ValueError(f"{file_path}'s '{REST_COMMAND_KEY}:' key is not a mapping")
        return commands

    async def find_all_locations(self, rest_command_id: str) -> list[RestCommandLocation]:
        """Find every file that defines the given rest_command id."""
        locations: list[RestCommandLocation] = []
        for file_path in await self.candidate_files():
            document = await self._load_document(file_path)
            commands = self._rest_command_map(file_path, document)
            if commands and rest_command_id in commands:
                locations.append(
                    RestCommandLocation(
                        file_path=file_path,
                        is_package=(file_path != DEFAULT_CONFIG_FILE),
                    )
                )
        return locations

    async def find_rest_command(self, rest_command_id: str) -> RestCommandLocation:
        """Resolve exactly one location for a rest_command id.

        Raises RestCommandNotFoundError if it's defined nowhere, and
        DuplicateRestCommandIdError if it's defined in more than one file
        (rather than silently picking one).
        """
        locations = await self.find_all_locations(rest_command_id)
        if not locations:
            raise RestCommandNotFoundError(
                f"No rest_command with id '{rest_command_id}' found"
            )
        if len(locations) > 1:
            raise DuplicateRestCommandIdError(rest_command_id, locations)
        return locations[0]

    async def all_rest_commands(
        self,
    ) -> list[tuple[RestCommandLocation, str, dict[str, Any]]]:
        """Return every rest_command across every candidate file, for listing.

        Unlike get_rest_command, this doesn't resolve/refuse on duplicate
        ids - a listing needs to see every definition, duplicates
        included.
        """
        results: list[tuple[RestCommandLocation, str, dict[str, Any]]] = []
        for file_path in await self.candidate_files():
            document = await self._load_document(file_path)
            commands = self._rest_command_map(file_path, document)
            if not commands:
                continue
            location = RestCommandLocation(
                file_path=file_path, is_package=(file_path != DEFAULT_CONFIG_FILE)
            )
            for rest_command_id, config in commands.items():
                if isinstance(config, dict):
                    results.append((location, str(rest_command_id), dict(config)))
        return results

    async def get_rest_command(
        self, rest_command_id: str
    ) -> tuple[RestCommandLocation, dict[str, Any]]:
        """Return the location and config dict for a rest_command id."""
        location = await self.find_rest_command(rest_command_id)
        document = await self._load_document(location.file_path)
        commands = self._rest_command_map(location.file_path, document) or {}
        if rest_command_id in commands:
            return location, dict(commands[rest_command_id])
        # Shouldn't happen - find_rest_command already confirmed presence.
        raise RestCommandNotFoundError(
            f"No rest_command with id '{rest_command_id}' found"
        )
