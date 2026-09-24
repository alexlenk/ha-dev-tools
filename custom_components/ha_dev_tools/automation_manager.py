"""Layout-aware, package-safe automation config access.

Home Assistant's `config/automation` REST API (and the UI editor built on
it) hard-codes `automations.yaml` and has zero awareness of packages.
Editing a package-defined automation through it doesn't fail loudly - a
write silently creates a diverging duplicate in `automations.yaml`, and a
delete silently no-ops while reporting success, leaving the real
package-defined automation untouched. See docs/ARCHITECTURE.md's "Hard
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
from .yaml_style import merge_preserving_style, quote_ambiguous_scalars

_LOGGER = logging.getLogger(__name__)

DEFAULT_AUTOMATIONS_FILE = "automations.yaml"
PACKAGES_DIR = "packages"


def _new_yaml() -> YAML:
    """Return a ruamel.yaml instance configured for round-trip editing."""
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096  # avoid re-wrapping long lines
    return yaml


def _load_yaml(content: str) -> Any:
    """Synchronous: build a fresh YAML() and load content with it, for
    hass.async_add_executor_job() - YAML(typ="rt")'s own __init__ does a
    blocking scandir (HA's own blocking-call detector caught this: a bare
    `_new_yaml().load` passed as the executor job's callable only defers
    load() to the executor, since `_new_yaml()` itself is evaluated eagerly
    on the event loop before being passed in)."""
    return _new_yaml().load(content)


@dataclass(frozen=True)
class AutomationLocation:
    """Where a given automation id is defined."""

    file_path: str  # relative to the HA config dir
    is_package: bool


@dataclass(frozen=True)
class AutomationWriteResult:
    """What write_automation() actually wrote - location plus before/after
    file content, for mirror.py (docs/AUTOMATION_TESTING_DESIGN.md's
    "Mirroring" section) to push. content_before is None for a brand new
    file (nothing existed there to capture)."""

    location: AutomationLocation
    content_before: str | None
    content_after: str


class AutomationNotFoundError(Exception):
    """Raised when an automation id can't be resolved to any known file."""


class DuplicateAutomationIdError(Exception):
    """Raised when an automation id is defined in more than one file.

    HA's own package merge doesn't hard-error on this (see
    docs/ARCHITECTURE.md) - it silently concatenates. We refuse to guess
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
        return await self.hass.async_add_executor_job(_load_yaml, content)

    def _automation_list(
        self, file_path: str, document: Any
    ) -> CommentedSeq | list | None:
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
                raise ValueError(
                    f"{file_path} does not contain a YAML list at its root"
                )
            return document

        if document is None or "automation" not in document:
            return None
        automations = document["automation"]
        if isinstance(automations, dict):
            return CommentedSeq([automations])
        if not isinstance(automations, list):
            raise ValueError(
                f"{file_path}'s 'automation:' key is not a list or mapping"
            )
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
                if isinstance(entry, dict) and str(entry.get("id")) == str(
                    automation_id
                ):
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
            raise AutomationNotFoundError(
                f"No automation with id '{automation_id}' found"
            )
        if len(locations) > 1:
            raise DuplicateAutomationIdError(automation_id, locations)
        return locations[0]

    async def all_automations(self) -> list[tuple[AutomationLocation, dict[str, Any]]]:
        """Return every automation across every candidate file, for auditing.

        Unlike get_automation, this doesn't resolve/refuse on duplicate ids -
        an audit needs to see every definition, duplicates included.
        """
        results: list[tuple[AutomationLocation, dict[str, Any]]] = []
        for file_path in await self.candidate_files():
            document = await self._load_document(file_path)
            automations = self._automation_list(file_path, document)
            if not automations:
                continue
            location = AutomationLocation(
                file_path=file_path, is_package=(file_path != DEFAULT_AUTOMATIONS_FILE)
            )
            for entry in automations:
                if isinstance(entry, dict):
                    # The loaded mapping itself, not a dict() copy: the
                    # audit reads its per-key line numbers (ruamel's .lc).
                    results.append((location, entry))
        return results

    async def get_automation(
        self, automation_id: str
    ) -> tuple[AutomationLocation, dict[str, Any]]:
        """Return the location and config dict for an automation id."""
        location = await self.find_automation(automation_id)
        document = await self._load_document(location.file_path)
        # find_automation() already confirmed this id lives in this file, so
        # None here would mean the file changed underneath us since that
        # read - fall back to an empty list so that's a clean
        # AutomationNotFoundError below, not a raw TypeError on `for`.
        automations = self._automation_list(location.file_path, document) or []
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
        dry_run: bool = False,
    ) -> AutomationWriteResult:
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
        full restart. Returns the file's before/after content alongside its
        location - captured here, not by a caller reading the file again
        afterward, since "before" only exists in the narrow window before
        this method's own write_file() call.

        `dry_run=True` resolves the location and computes content_after
        exactly the same way, but returns before ever calling
        file_manager.write_file() or reloading - nothing live changes. For
        mirroring's dry-run + proposed/* branch support (issue #35): the
        resolved would-be content is real and correct, it just never
        touches disk.
        """
        config = dict(config)
        config["id"] = str(automation_id)
        config = quote_ambiguous_scalars(config)

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
            location = AutomationLocation(
                file_path=DEFAULT_AUTOMATIONS_FILE, is_package=False
            )

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
            self._build_content, location, document, automation_id, config
        )

        if dry_run:
            return AutomationWriteResult(
                location=location,
                content_before=content_before,
                content_after=content_after,
            )

        # Routes through FileManager so this write gets the same treatment
        # as every other write in this integration: security allowlist
        # enforcement, hash-conflict re-check right before the write (not
        # earlier - minimizes the race window), backup, and atomic write.
        # Bypassing FileManager here would silently skip all of that.
        await self.file_manager.write_file(
            location.file_path,
            content_after,
            expected_hash=expected_hash,
            validate_before_write=True,
        )

        await self.hass.services.async_call("automation", "reload", blocking=True)
        _LOGGER.info(
            "Wrote automation '%s' to %s and reloaded automations",
            automation_id,
            location.file_path,
        )
        return AutomationWriteResult(
            location=location,
            content_before=content_before,
            content_after=content_after,
        )

    async def delete_automation(
        self,
        automation_id: str,
        *,
        expected_hash: str | None = None,
        dry_run: bool = False,
    ) -> AutomationWriteResult:
        """Delete an automation by id, writing through the correct file.

        Resolves which file actually defines it first (find_automation) -
        raises AutomationNotFoundError if the id doesn't exist,
        DuplicateAutomationIdError if it's defined in more than one file,
        rather than guessing. `expected_hash` and `dry_run` behave exactly
        the same as write_automation's identical parameters.
        """
        location = await self.find_automation(automation_id)
        content_before = await self.file_manager.read_file(location.file_path)
        document = await self.hass.async_add_executor_job(_load_yaml, content_before)
        content_after = await self.hass.async_add_executor_job(
            self._build_delete_content, location, document, automation_id
        )

        if dry_run:
            return AutomationWriteResult(
                location=location,
                content_before=content_before,
                content_after=content_after,
            )

        await self.file_manager.write_file(
            location.file_path,
            content_after,
            expected_hash=expected_hash,
            validate_before_write=True,
        )

        await self.hass.services.async_call("automation", "reload", blocking=True)
        _LOGGER.info(
            "Deleted automation '%s' from %s and reloaded automations",
            automation_id,
            location.file_path,
        )
        return AutomationWriteResult(
            location=location,
            content_before=content_before,
            content_after=content_after,
        )

    def _build_delete_content(
        self,
        location: AutomationLocation,
        document: Any,
        automation_id: str,
    ) -> str:
        """Synchronous: remove the automation from its document, return the new file content.

        Unlike _build_content, this never needs to handle a missing/empty
        document or automation list - find_automation() (called by every
        caller before this) already confirmed automation_id lives in
        location.file_path, so document and its automation list are always
        already there. The one normalization still needed is a package's
        single-mapping `automation:` form (vs. a list) - see
        _automation_list's docstring.
        """
        yaml = _new_yaml()

        if location.file_path == DEFAULT_AUTOMATIONS_FILE:
            automations = document
        else:
            automations = document.get("automation")
            if isinstance(automations, dict):
                automations = CommentedSeq([automations])
                document["automation"] = automations

        for i, entry in enumerate(automations):
            if isinstance(entry, dict) and str(entry.get("id")) == str(automation_id):
                del automations[i]
                break

        from io import StringIO

        buffer = StringIO()
        yaml.dump(document, buffer)
        return buffer.getvalue()

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
        preserved as-is - only the target automation's node is patched or
        appended. An existing automation is patched rather than swapped
        for the caller's plain dict, so its unchanged fields keep their
        original formatting too - see yaml_style.merge_preserving_style.
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
                automations[i] = merge_preserving_style(entry, config)
                replaced = True
                break
        if not replaced:
            automations.append(config)

        from io import StringIO

        buffer = StringIO()
        yaml.dump(document, buffer)
        return buffer.getvalue()
