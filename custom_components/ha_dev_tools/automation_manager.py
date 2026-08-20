"""Layout-aware, package-safe automation config access.

Home Assistant's `config/automation` REST API (and the UI editor built on
it) hard-codes `automations.yaml` and has zero awareness of packages.
Editing a package-defined automation through it doesn't fail loudly - a
write silently creates a diverging duplicate in `automations.yaml`, and a
delete silently no-ops while reporting success, leaving the real
package-defined automation untouched. See docs/RESTART_PLAN.md's "Hard
safety rule: package provenance" for the full finding.

This module resolves, for a given automation id, which file actually
defines it - the default `automations.yaml`, or a specific
`packages/*.yaml` file - before any read or write happens, and always
writes through the same file it found. It uses `ruamel.yaml`'s
round-trip loader/dumper rather than plain PyYAML so hand-maintained
package files (comments, key order, formatting) survive a surgical edit
to one automation instead of being silently reformatted.

Scope: only the default file and `packages/**/*.yaml` are resolved.
Arbitrary custom `!include_dir_merge_list` layouts outside `packages/`
aren't handled yet - `find_automation` simply won't find automations
defined that way, which is safer than guessing wrong.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .file_manager import FileManager

_LOGGER = logging.getLogger(__name__)

DEFAULT_AUTOMATIONS_FILE = "automations.yaml"
PACKAGES_DIR = "packages"


def _new_yaml() -> YAML:
    """Return a ruamel.yaml instance configured for round-trip editing."""
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096  # avoid re-wrapping long lines
    return yaml


@dataclass(frozen=True)
class AutomationLocation:
    """Where a given automation id is defined."""

    file_path: str  # relative to the HA config dir
    is_package: bool


class AutomationNotFoundError(Exception):
    """Raised when an automation id can't be resolved to any known file."""


class DuplicateAutomationIdError(Exception):
    """Raised when an automation id is defined in more than one file.

    HA's own package merge doesn't hard-error on this (see
    docs/RESTART_PLAN.md) - it silently concatenates. We refuse to guess
    which one the caller means.
    """

    def __init__(self, automation_id: str, locations: list[AutomationLocation]) -> None:
        self.automation_id = automation_id
        self.locations = locations
        super().__init__(
            f"Automation id '{automation_id}' is defined in more than one "
            f"file: {[loc.file_path for loc in locations]}"
        )


class AutomationManager:
    """Layout-aware, package-safe read/write access to automation config."""

    def __init__(self, hass: HomeAssistant, file_manager: FileManager) -> None:
        """Initialize the automation manager."""
        self.hass = hass
        self.file_manager = file_manager
        self._config_dir = Path(hass.config.config_dir)

    async def candidate_files(self) -> list[str]:
        """Return every file that may define automations, default file first."""
        candidates: list[str] = []
        if (self._config_dir / DEFAULT_AUTOMATIONS_FILE).is_file():
            candidates.append(DEFAULT_AUTOMATIONS_FILE)
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
        """Load a candidate file's parsed YAML document (not just the automation list).

        Returns None if the file doesn't exist yet - callers writing a brand
        new automation to a not-yet-created default file rely on this rather
        than a FileNotFoundError; find_all_locations never hits this case
        since candidate_files() only returns files that already exist.
        """
        try:
            content = await self.file_manager.read_file(file_path)
        except FileNotFoundError:
            return None
        return await self.hass.async_add_executor_job(_new_yaml().load, content)

    def _automation_list(self, file_path: str, document: Any) -> CommentedSeq | list | None:
        """Return the automation list within a loaded document, or None if it has none.

        The default automations.yaml is a plain list at the document root
        (that's what `automation: !include automations.yaml` substitutes).
        A package file is a dict with an `automation:` key whose value is a
        list (HA also allows a single mapping there, normalized to a
        one-item list on read but always written back as a list).
        """
        if file_path == DEFAULT_AUTOMATIONS_FILE:
            if document is None:
                return CommentedSeq()
            if not isinstance(document, list):
                raise ValueError(f"{file_path} does not contain a YAML list at its root")
            return document

        if document is None or "automation" not in document:
            return None
        automations = document["automation"]
        if isinstance(automations, dict):
            return CommentedSeq([automations])
        if not isinstance(automations, list):
            raise ValueError(f"{file_path}'s 'automation:' key is not a list or mapping")
        return automations

    async def find_all_locations(self, automation_id: str) -> list[AutomationLocation]:
        """Find every file that defines the given automation id.

        More than one result means HA's package merge would concatenate
        duplicates - see DuplicateAutomationIdError.
        """
        locations: list[AutomationLocation] = []
        for file_path in await self.candidate_files():
            document = await self._load_document(file_path)
            automations = self._automation_list(file_path, document)
            if not automations:
                continue
            for entry in automations:
                if isinstance(entry, dict) and str(entry.get("id")) == str(automation_id):
                    locations.append(
                        AutomationLocation(
                            file_path=file_path,
                            is_package=(file_path != DEFAULT_AUTOMATIONS_FILE),
                        )
                    )
                    break
        return locations

    async def find_automation(self, automation_id: str) -> AutomationLocation:
        """Resolve exactly one location for an automation id.

        Raises AutomationNotFoundError if it's defined nowhere, and
        DuplicateAutomationIdError if it's defined in more than one file
        (rather than silently picking one).
        """
        locations = await self.find_all_locations(automation_id)
        if not locations:
            raise AutomationNotFoundError(f"No automation with id '{automation_id}' found")
        if len(locations) > 1:
            raise DuplicateAutomationIdError(automation_id, locations)
        return locations[0]

    async def get_automation(self, automation_id: str) -> tuple[AutomationLocation, dict[str, Any]]:
        """Return the location and config dict for an automation id."""
        location = await self.find_automation(automation_id)
        document = await self._load_document(location.file_path)
        automations = self._automation_list(location.file_path, document)
        for entry in automations:
            if isinstance(entry, dict) and str(entry.get("id")) == str(automation_id):
                return location, dict(entry)
        # Shouldn't happen - find_automation already confirmed presence.
        raise AutomationNotFoundError(f"No automation with id '{automation_id}' found")

    async def write_automation(
        self,
        automation_id: str,
        config: dict[str, Any],
        *,
        package: str | None = None,
        expected_hash: str | None = None,
    ) -> AutomationLocation:
        """Create or update an automation, writing through the correct file.

        - If the id already exists, it's updated in place in whatever file
          defines it (package or default) - `package` is ignored in this case.
        - If the id is new, `package` selects the target
          (`packages/<package>` must already exist); omitting it targets the
          default `automations.yaml`.
        - `expected_hash` is checked against the target file's current
          content hash (whole-file granularity, matching FileManager's
          existing conflict model) before writing.

        Always calls `automation.reload` after a successful write - never a
        full restart.
        """
        config = dict(config)
        config["id"] = str(automation_id)

        locations = await self.find_all_locations(automation_id)
        if len(locations) > 1:
            raise DuplicateAutomationIdError(automation_id, locations)

        if locations:
            location = locations[0]
        elif package:
            file_path = f"{PACKAGES_DIR}/{package}"
            if not (self._config_dir / file_path).is_file():
                raise AutomationNotFoundError(
                    f"Package file '{file_path}' does not exist - create it "
                    "first, this tool won't invent a new package file"
                )
            location = AutomationLocation(file_path=file_path, is_package=True)
        else:
            location = AutomationLocation(file_path=DEFAULT_AUTOMATIONS_FILE, is_package=False)

        document = await self._load_document(location.file_path)
        content = await self.hass.async_add_executor_job(
            self._build_content, location, document, automation_id, config
        )

        # Routes through FileManager so this write gets the same treatment
        # as every other write in this integration: security allowlist
        # enforcement, hash-conflict re-check right before the write (not
        # earlier - minimizes the race window), backup, and atomic write.
        # Bypassing FileManager here would silently skip all of that.
        await self.file_manager.write_file(
            location.file_path,
            content,
            expected_hash=expected_hash,
            validate_before_write=True,
        )

        await self.hass.services.async_call(
            "automation", "reload", blocking=True
        )
        _LOGGER.info(
            "Wrote automation '%s' to %s and reloaded automations",
            automation_id,
            location.file_path,
        )
        return location

    def _build_content(
        self,
        location: AutomationLocation,
        document: Any,
        automation_id: str,
        config: dict[str, Any],
    ) -> str:
        """Synchronous: splice the automation into its document, return the new file content.

        Uses ruamel's round-trip dumper so everything else in the document
        (other automations, comments, other domains in a package file) is
        preserved as-is - only the target automation's node is replaced or
        appended.
        """
        yaml = _new_yaml()

        if location.file_path == DEFAULT_AUTOMATIONS_FILE:
            automations = document if isinstance(document, list) else CommentedSeq()
            document = automations
        else:
            if document is None:
                document = CommentedMap()
            automations = document.get("automation")
            if isinstance(automations, dict):
                automations = CommentedSeq([automations])
            elif not isinstance(automations, list):
                automations = CommentedSeq()
            document["automation"] = automations

        replaced = False
        for i, entry in enumerate(automations):
            if isinstance(entry, dict) and str(entry.get("id")) == str(automation_id):
                automations[i] = config
                replaced = True
                break
        if not replaced:
            automations.append(config)

        from io import StringIO

        buffer = StringIO()
        yaml.dump(document, buffer)
        return buffer.getvalue()
