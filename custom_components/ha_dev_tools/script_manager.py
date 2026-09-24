"""Layout-aware, package-safe script config access.

Same "hard safety rule: package provenance" reasoning as
automation_manager.py (see its module docstring and
docs/ARCHITECTURE.md) - this module resolves, for a given script id,
which file actually defines it - the default `scripts.yaml`, or a
specific `packages/*.yaml` file - before any read or write happens, and
always writes through the same file it found.

Structurally different from automations in one key way: `script:` is a
*mapping* of script id -> config (the id is the dict key itself), not a
list of dicts each carrying their own `id:` field the way `automation:`
is. `scripts.yaml` (the default file, substituted in via
`script: !include scripts.yaml`) is a bare mapping at its document root;
a package file has it under a `script:` key whose value is also a
mapping. Splicing one script in is therefore a single dict-key
assignment, not a list search-and-replace.

Scope: only the default file and `packages/**/*.yaml` are resolved, same
as automation_manager.py - `find_script` simply won't find scripts
defined via some other `!include_dir_merge_named`-style layout, which is
safer than guessing wrong.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from .file_manager import FileManager
from .yaml_style import merge_preserving_style, quote_ambiguous_scalars

_LOGGER = logging.getLogger(__name__)

DEFAULT_SCRIPTS_FILE = "scripts.yaml"
PACKAGES_DIR = "packages"


def _new_yaml() -> YAML:
    """Return a ruamel.yaml instance configured for round-trip editing.

    Same config as automation_manager.py's helper of the same name -
    duplicated rather than imported since it's five lines and these
    modules aren't otherwise coupled.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096  # avoid re-wrapping long lines
    return yaml


def _load_yaml(content: str) -> Any:
    """Synchronous: build a fresh YAML() and load content with it, for
    hass.async_add_executor_job() - same reasoning as automation_manager.py's
    helper of the same name: YAML(typ="rt")'s own __init__ does a blocking
    scandir, so a bare `_new_yaml().load` passed as the executor job's
    callable only defers load() to the executor - `_new_yaml()` itself is
    evaluated eagerly on the event loop before being passed in."""
    return _new_yaml().load(content)


@dataclass(frozen=True)
class ScriptLocation:
    """Where a given script id is defined."""

    file_path: str  # relative to the HA config dir
    is_package: bool


@dataclass(frozen=True)
class ScriptWriteResult:
    """What write_script() actually wrote - location plus before/after file
    content, for mirror.py to push. content_before is None for a brand new
    file (nothing existed there to capture)."""

    location: ScriptLocation
    content_before: str | None
    content_after: str


class ScriptNotFoundError(Exception):
    """Raised when a script id can't be resolved to any known file."""


class DuplicateScriptIdError(Exception):
    """Raised when a script id is defined in more than one file.

    Same "refuse to guess" reasoning as DuplicateAutomationIdError -
    rather than assume how HA's package merge resolves a dict key
    collision across files, this repo just never picks one on the
    caller's behalf.
    """

    def __init__(self, script_id: str, locations: list[ScriptLocation]) -> None:
        self.script_id = script_id
        self.locations = locations
        super().__init__(
            f"Script id '{script_id}' is defined in more than one file: "
            f"{[loc.file_path for loc in locations]}"
        )


class ScriptManager:
    """Layout-aware, package-safe read/write access to script config."""

    def __init__(self, hass: HomeAssistant, file_manager: FileManager) -> None:
        """Initialize the script manager."""
        self.hass = hass
        self.file_manager = file_manager
        self._config_dir = Path(hass.config.config_dir)

    async def candidate_files(self) -> list[str]:
        """Return every file that may define scripts, default file first."""
        candidates: list[str] = []
        if (self._config_dir / DEFAULT_SCRIPTS_FILE).is_file():
            candidates.append(DEFAULT_SCRIPTS_FILE)
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
        """Load a candidate file's parsed YAML document (not just the script map).

        Returns None if the file doesn't exist yet - callers writing a
        brand new script to a not-yet-created default file rely on this
        rather than a FileNotFoundError; find_all_locations never hits
        this case since candidate_files() only returns files that already
        exist.
        """
        try:
            content = await self.file_manager.read_file(file_path)
        except FileNotFoundError:
            return None
        return await self.hass.async_add_executor_job(_load_yaml, content)

    def _script_map(self, file_path: str, document: Any) -> CommentedMap | dict | None:
        """Return the script id -> config mapping within a loaded document,
        or None if it has none.

        The default scripts.yaml is itself that mapping at the document
        root (that's what `script: !include scripts.yaml` substitutes). A
        package file is a dict with a `script:` key whose value is that
        mapping.
        """
        if file_path == DEFAULT_SCRIPTS_FILE:
            if document is None:
                return CommentedMap()
            if not isinstance(document, dict):
                raise ValueError(
                    f"{file_path} does not contain a YAML mapping at its root"
                )
            return document

        if document is None or "script" not in document:
            return None
        scripts = document["script"]
        if not isinstance(scripts, dict):
            raise ValueError(f"{file_path}'s 'script:' key is not a mapping")
        return scripts

    async def find_all_locations(self, script_id: str) -> list[ScriptLocation]:
        """Find every file that defines the given script id."""
        locations: list[ScriptLocation] = []
        for file_path in await self.candidate_files():
            document = await self._load_document(file_path)
            scripts = self._script_map(file_path, document)
            if scripts and script_id in scripts:
                locations.append(
                    ScriptLocation(
                        file_path=file_path,
                        is_package=(file_path != DEFAULT_SCRIPTS_FILE),
                    )
                )
        return locations

    async def find_script(self, script_id: str) -> ScriptLocation:
        """Resolve exactly one location for a script id.

        Raises ScriptNotFoundError if it's defined nowhere, and
        DuplicateScriptIdError if it's defined in more than one file
        (rather than silently picking one).
        """
        locations = await self.find_all_locations(script_id)
        if not locations:
            raise ScriptNotFoundError(f"No script with id '{script_id}' found")
        if len(locations) > 1:
            raise DuplicateScriptIdError(script_id, locations)
        return locations[0]

    async def all_scripts(self) -> list[tuple[ScriptLocation, str, dict[str, Any]]]:
        """Return every script across every candidate file, for listing.

        Unlike get_script, this doesn't resolve/refuse on duplicate ids -
        a listing needs to see every definition, duplicates included.
        """
        results: list[tuple[ScriptLocation, str, dict[str, Any]]] = []
        for file_path in await self.candidate_files():
            document = await self._load_document(file_path)
            scripts = self._script_map(file_path, document)
            if not scripts:
                continue
            location = ScriptLocation(
                file_path=file_path, is_package=(file_path != DEFAULT_SCRIPTS_FILE)
            )
            for script_id, config in scripts.items():
                if isinstance(config, dict):
                    results.append((location, str(script_id), dict(config)))
        return results

    async def get_script(self, script_id: str) -> tuple[ScriptLocation, dict[str, Any]]:
        """Return the location and config dict for a script id."""
        location = await self.find_script(script_id)
        document = await self._load_document(location.file_path)
        scripts = self._script_map(location.file_path, document) or {}
        if script_id in scripts:
            return location, dict(scripts[script_id])
        # Shouldn't happen - find_script already confirmed presence.
        raise ScriptNotFoundError(f"No script with id '{script_id}' found")

    async def write_script(
        self,
        script_id: str,
        config: dict[str, Any],
        *,
        package: str | None = None,
        expected_hash: str | None = None,
        dry_run: bool = False,
    ) -> ScriptWriteResult:
        """Create or update a script, writing through the correct file.

        - If the id already exists, it's updated in place in whatever file
          defines it (package or default) - `package` is ignored in this case.
        - If the id is new, `package` selects the target
          (`packages/<package>` must already exist); omitting it targets
          the default `scripts.yaml`.
        - `expected_hash` is checked against the target file's current
          content hash before writing.

        Always calls `script.reload` after a successful write - never a
        full restart.

        `dry_run=True` resolves the location and computes content_after
        exactly the same way, but returns before ever calling
        file_manager.write_file() or reloading - nothing live changes.
        """
        config = dict(config)
        config = quote_ambiguous_scalars(config)

        locations = await self.find_all_locations(script_id)
        if len(locations) > 1:
            raise DuplicateScriptIdError(script_id, locations)

        if locations:
            location = locations[0]
        elif package:
            file_path = f"{PACKAGES_DIR}/{package}"
            if not (self._config_dir / file_path).is_file():
                raise ScriptNotFoundError(
                    f"Package file '{file_path}' does not exist - create it "
                    "first, this tool won't invent a new package file"
                )
            location = ScriptLocation(file_path=file_path, is_package=True)
        else:
            location = ScriptLocation(file_path=DEFAULT_SCRIPTS_FILE, is_package=False)

        try:
            content_before: str | None = await self.file_manager.read_file(
                location.file_path
            )
        except FileNotFoundError:
            content_before = None
        document = (
            await self.hass.async_add_executor_job(_load_yaml, content_before)
            if content_before is not None
            else None
        )
        content_after = await self.hass.async_add_executor_job(
            self._build_content, location, document, script_id, config
        )

        if dry_run:
            return ScriptWriteResult(
                location=location,
                content_before=content_before,
                content_after=content_after,
            )

        # Routes through FileManager for the same reasons write_automation
        # does: security allowlist enforcement, hash-conflict re-check
        # right before the write, backup, atomic write.
        await self.file_manager.write_file(
            location.file_path,
            content_after,
            expected_hash=expected_hash,
            validate_before_write=True,
        )

        await self.hass.services.async_call("script", "reload", blocking=True)
        _LOGGER.info(
            "Wrote script '%s' to %s and reloaded scripts",
            script_id,
            location.file_path,
        )
        return ScriptWriteResult(
            location=location,
            content_before=content_before,
            content_after=content_after,
        )

    def _build_content(
        self,
        location: ScriptLocation,
        document: Any,
        script_id: str,
        config: dict[str, Any],
    ) -> str:
        """Synchronous: splice the script into its document, return the new file content.

        Uses ruamel's round-trip dumper so everything else in the document
        (other scripts, comments, other domains in a package file) is
        preserved as-is - only the target script's key is set or patched. Unlike
        automation_manager's list splice, this is a plain dict-key
        assignment, since `script:` is a mapping keyed by id.
        """
        yaml = _new_yaml()

        if location.file_path == DEFAULT_SCRIPTS_FILE:
            scripts = document if isinstance(document, dict) else CommentedMap()
            document = scripts
        else:
            if document is None:
                document = CommentedMap()
            scripts = document.get("script")
            if not isinstance(scripts, dict):
                scripts = CommentedMap()
            document["script"] = scripts

        # Patch an existing script in place rather than swapping it for the
        # caller's plain dict, so its unchanged fields keep their original
        # formatting - see yaml_style.merge_preserving_style (issue #92).
        scripts[script_id] = (
            merge_preserving_style(scripts[script_id], config)
            if script_id in scripts
            else config
        )

        from io import StringIO

        buffer = StringIO()
        yaml.dump(document, buffer)
        return buffer.getvalue()
