"""Layout-aware, package-safe access to YAML `template:` entities.

Issue #13's second ask, alongside `derived_sensor_manager.py`'s config-entry
helpers: Template sensors/binary_sensors/etc. defined via the modern,
trigger-based `template:` YAML syntax (not the deprecated
`sensor: - platform: template` legacy form, and not the config-entry-based
Template *helper* either - that's a ~900-line config flow of its own, out
of scope here same as it was for derived_sensor_manager.py).

Reuses `automation_manager.py`'s provenance-resolution pattern - scan every
candidate file, resolve exactly one location, refuse to guess on
ambiguity - with one structural difference: `template:` entries have no
default include-file convention the way `automation: !include
automations.yaml` does. A block can live directly in `configuration.yaml`
or in any `packages/*.yaml` file, so `configuration.yaml` itself is always
a candidate for *reads*, not just packages.

Writes are narrower than reads on purpose. `configuration.yaml` is in this
integration's `DEFAULT_READ_ONLY_PATHS`, not `DEFAULT_WRITE_PATHS` (see
security.py) - `FileManager.write_file` enforces that split for real now
(see the write_file/delete_file operation-check fix this module's own
development surfaced). So an entity that happens to live in
configuration.yaml can be listed/read here same as any other, but
create_entity always targets a `packages/*.yaml` file (package is a
required argument, no "default file" fallback the way write_automation
has one), and update_entity/delete_entity on an entity resolved to
configuration.yaml will fail with FileManager's own PermissionError -
that's the correct, intentional outcome, not a bug to work around here.

Individual entities have no HA-tracked identity the way an automation's
`id:` does, unless they set their own `unique_id:` - every create/update/
delete here requires one (config must include it for create; update/
delete take it as the lookup key). Entities without a unique_id still show
up in list_entities/get reads (flagged there), just aren't addressable for
writes - the same "refuse to guess" philosophy as DuplicateAutomationIdError,
applied to "which entity" instead of "which file".

create_entity always creates a brand new template: block for the entity
being created, rather than trying to merge into an existing block that
might share triggers/conditions/variables with unrelated entities - simpler
and unambiguous, at the cost of slightly less compact YAML than a human
hand-authoring multiple entities under one shared trigger block.
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

TEMPLATE_KEY = "template"
DEFAULT_CONFIG_FILE = "configuration.yaml"
PACKAGES_DIR = "packages"

# Every platform the template integration supports (confirmed by reading
# homeassistant/components/template/const.py at this repo's pinned HA
# version, 2026.8.2) - entity configs are treated as opaque dicts here
# (never modeling per-platform fields, same as automation_manager.py treats
# automation configs), so supporting all of them costs nothing extra.
TEMPLATE_PLATFORMS = (
    "alarm_control_panel",
    "binary_sensor",
    "button",
    "cover",
    "device_tracker",
    "event",
    "fan",
    "image",
    "light",
    "lock",
    "number",
    "select",
    "sensor",
    "switch",
    "update",
    "vacuum",
    "weather",
)


def _new_yaml() -> YAML:
    """Return a ruamel.yaml instance configured for round-trip editing.

    Same config as automation_manager.py's helper of the same name -
    duplicated rather than imported since it's five lines and these two
    modules aren't otherwise coupled.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096  # avoid re-wrapping long lines
    return yaml


def _to_plain(value: Any) -> Any:
    """Recursively convert ruamel's round-trip types into plain JSON-safe values.

    CommentedMap/CommentedSeq already subclass dict/list so most values pass
    through fine, but a raw HA YAML tag this loader doesn't understand
    (!secret, !include, ...) - which can legitimately appear inside a
    template entity's own config, e.g. an availability template built from
    a !secret value - loads as a TaggedScalar, which is not JSON-safe. Never
    resolves what a !secret/!include actually points to (this only ever
    reads the tag name and its literal argument) - appropriate given this
    is a read path that shouldn't leak secrets.yaml contents.
    """
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    tag = getattr(value, "tag", None)
    if tag is not None and hasattr(value, "value"):
        return f"{tag.value} {value.value}"
    return value


@dataclass(frozen=True)
class TemplateEntityLocation:
    """Where a given template entity is defined."""

    file_path: str  # relative to the HA config dir
    is_package: bool
    block_index: int
    platform: str
    entity_index: int


class TemplateEntityNotFoundError(Exception):
    """Raised when a unique_id doesn't resolve to any known template entity."""


class DuplicateTemplateUniqueIdError(Exception):
    """Raised when a unique_id is defined on more than one template entity.

    Nothing in Home Assistant's own template loading enforces uniqueness
    across files the way an entity registry would - two entities sharing a
    unique_id in different files would both load, colliding at the entity
    registry only once both actually try to register. Refuse to guess
    which one a caller means, same as DuplicateAutomationIdError.
    """

    def __init__(self, unique_id: str, locations: list[TemplateEntityLocation]) -> None:
        self.unique_id = unique_id
        self.locations = locations
        super().__init__(
            f"unique_id '{unique_id}' is defined on more than one template "
            f"entity: {[(loc.file_path, loc.block_index, loc.platform) for loc in locations]}"
        )


class TemplateYamlManager:
    """Layout-aware, package-safe read/write access to YAML template: entities."""

    def __init__(self, hass: HomeAssistant, file_manager: FileManager) -> None:
        """Initialize the template YAML manager."""
        self.hass = hass
        self.file_manager = file_manager
        self._config_dir = Path(hass.config.config_dir)

    async def _reload_template(self) -> bool:
        """Call template.reload if it's registered; report whether it ran.

        The `template` integration is only set up (and its `reload` service
        registered) if something already references it - either a `template:`
        YAML block or a config-entry Template helper. In the ordinary case
        this tool is used for (an instance that already has template
        entities, which is exactly issue #13's scenario), that's already
        true. But the very first template entity ever created via this tool
        on an instance with none before now would find no such service yet -
        same edge case config_tools.reload_domain already guards against for
        arbitrary domains, applied here specifically since create/update/
        delete_entity always want to attempt this, not leave it to the
        caller.
        """
        if not self.hass.services.has_service("template", "reload"):
            _LOGGER.warning(
                "template.reload service not registered yet (no prior "
                "template: config or Template helper on this instance) - "
                "the write succeeded, but won't take effect until the "
                "template integration is otherwise loaded"
            )
            return False
        await self.hass.services.async_call("template", "reload", blocking=True)
        return True

    async def candidate_files(self) -> list[str]:
        """Return every file that may define template: entities.

        Unlike automation_manager.py's candidate_files, configuration.yaml
        itself is always a candidate (not just packages) - template:
        entries have no default include-file convention to gate on.
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
        package file a caller is about to create content in; candidate_files()
        only lists files that already exist, and configuration.yaml always
        exists on a running instance.
        """
        try:
            content = await self.file_manager.read_file(file_path)
        except FileNotFoundError:
            return None
        return await self.hass.async_add_executor_job(_new_yaml().load, content)

    def _template_blocks(self, document: Any) -> CommentedSeq | list:
        """Return the template: list within a loaded document, or [] if it has none."""
        if document is None or TEMPLATE_KEY not in document:
            return []
        blocks = document[TEMPLATE_KEY]
        if isinstance(blocks, dict):
            return CommentedSeq([blocks])
        if not isinstance(blocks, list):
            raise ValueError(f"'{TEMPLATE_KEY}:' key is not a list or mapping")
        return blocks

    async def list_entities(self) -> list[dict[str, Any]]:
        """Return every template entity across every candidate file.

        Each result includes its location (file/block/platform/index),
        unique_id (None if the entity doesn't set one - such entities are
        listed here but aren't addressable for create/update/delete), and
        its full config.
        """
        results: list[dict[str, Any]] = []
        for file_path in await self.candidate_files():
            document = await self._load_document(file_path)
            blocks = self._template_blocks(document)
            is_package = file_path != DEFAULT_CONFIG_FILE
            for block_index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                for platform in TEMPLATE_PLATFORMS:
                    entities = block.get(platform)
                    if not isinstance(entities, list):
                        continue
                    for entity_index, entity_conf in enumerate(entities):
                        if not isinstance(entity_conf, dict):
                            continue
                        results.append(
                            {
                                "file_path": file_path,
                                "is_package": is_package,
                                "block_index": block_index,
                                "platform": platform,
                                "entity_index": entity_index,
                                "unique_id": entity_conf.get("unique_id"),
                                "name": entity_conf.get("name"),
                                "config": _to_plain(dict(entity_conf)),
                            }
                        )
        return results

    async def find_all_locations(self, unique_id: str) -> list[TemplateEntityLocation]:
        """Find every entity defining the given unique_id."""
        locations: list[TemplateEntityLocation] = []
        for file_path in await self.candidate_files():
            document = await self._load_document(file_path)
            blocks = self._template_blocks(document)
            is_package = file_path != DEFAULT_CONFIG_FILE
            for block_index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                for platform in TEMPLATE_PLATFORMS:
                    entities = block.get(platform)
                    if not isinstance(entities, list):
                        continue
                    for entity_index, entity_conf in enumerate(entities):
                        if (
                            isinstance(entity_conf, dict)
                            and entity_conf.get("unique_id") == unique_id
                        ):
                            locations.append(
                                TemplateEntityLocation(
                                    file_path=file_path,
                                    is_package=is_package,
                                    block_index=block_index,
                                    platform=platform,
                                    entity_index=entity_index,
                                )
                            )
        return locations

    async def find_entity(self, unique_id: str) -> TemplateEntityLocation:
        """Resolve exactly one location for a unique_id.

        Raises TemplateEntityNotFoundError if it's defined nowhere, and
        DuplicateTemplateUniqueIdError if it's defined more than once
        (rather than silently picking one).
        """
        locations = await self.find_all_locations(unique_id)
        if not locations:
            raise TemplateEntityNotFoundError(
                f"No template entity with unique_id '{unique_id}' found"
            )
        if len(locations) > 1:
            raise DuplicateTemplateUniqueIdError(unique_id, locations)
        return locations[0]

    async def get_entity(
        self, unique_id: str
    ) -> tuple[TemplateEntityLocation, dict[str, Any]]:
        """Return the location and config dict for a template entity by unique_id."""
        location = await self.find_entity(unique_id)
        document = await self._load_document(location.file_path)
        blocks = self._template_blocks(document)
        entity_conf = blocks[location.block_index][location.platform][
            location.entity_index
        ]
        return location, _to_plain(dict(entity_conf))

    async def create_entity(
        self,
        platform: str,
        config: dict[str, Any],
        *,
        package: str,
        triggers: list[dict[str, Any]] | None = None,
        expected_hash: str | None = None,
    ) -> tuple[TemplateEntityLocation, bool]:
        """Create a new template entity in its own new template: block.

        Returns (location, reloaded) - see _reload_template for when
        reloaded can come back False despite a successful write.

        `package` names an existing `packages/<package>` file (relative to
        packages/) - required, no default-file fallback, since
        configuration.yaml itself is read-only under this integration's
        default security policy (see module docstring); this tool won't
        invent a new package file either.

        `config` must include a `unique_id` - required for every write
        this module does (see module docstring) - and must not already be
        in use by another template entity.
        """
        config = dict(config)
        unique_id = config.get("unique_id")
        if not unique_id:
            raise ValueError(
                "config must include a 'unique_id' - required for write "
                "operations here (see module docstring)"
            )

        existing = await self.find_all_locations(unique_id)
        if existing:
            raise DuplicateTemplateUniqueIdError(unique_id, existing)

        file_path = f"{PACKAGES_DIR}/{package}"
        if not (self._config_dir / file_path).is_file():
            raise TemplateEntityNotFoundError(
                f"Package file '{file_path}' does not exist - create it "
                "first, this tool won't invent a new package file"
            )

        document = await self._load_document(file_path)
        content, block_index = await self.hass.async_add_executor_job(
            self._build_create_content, document, platform, config, triggers
        )

        # Routes through FileManager for the same reasons write_automation
        # does: security allowlist enforcement (packages/*.yaml is
        # write-permitted, configuration.yaml is not - see module
        # docstring), hash-conflict re-check right before the write, backup,
        # atomic write.
        await self.file_manager.write_file(
            file_path,
            content,
            expected_hash=expected_hash,
            validate_before_write=True,
        )

        reloaded = await self._reload_template()
        location = TemplateEntityLocation(
            file_path=file_path,
            is_package=True,
            block_index=block_index,
            platform=platform,
            entity_index=0,
        )
        _LOGGER.info(
            "Created template entity unique_id='%s' (%s) in %s",
            unique_id,
            platform,
            file_path,
        )
        return location, reloaded

    def _build_create_content(
        self,
        document: Any,
        platform: str,
        config: dict[str, Any],
        triggers: list[dict[str, Any]] | None,
    ) -> tuple[str, int]:
        """Synchronous: append a new template: block, return (new file content, its index)."""
        yaml = _new_yaml()

        if document is None:
            document = CommentedMap()
        blocks = document.get(TEMPLATE_KEY)
        if isinstance(blocks, dict):
            blocks = CommentedSeq([blocks])
        elif not isinstance(blocks, list):
            blocks = CommentedSeq()
        document[TEMPLATE_KEY] = blocks

        new_block: dict[str, Any] = {}
        if triggers:
            new_block["triggers"] = triggers
        new_block[platform] = [config]
        blocks.append(new_block)
        block_index = len(blocks) - 1

        from io import StringIO

        buffer = StringIO()
        yaml.dump(document, buffer)
        return buffer.getvalue(), block_index

    async def update_entity(
        self,
        unique_id: str,
        config: dict[str, Any],
        *,
        expected_hash: str | None = None,
    ) -> tuple[TemplateEntityLocation, bool]:
        """Update an existing template entity's config in place, by unique_id.

        Only touches the resolved entity's own dict within its existing
        block - triggers/conditions/variables and any sibling entities in
        the same block are left untouched. `config`'s own unique_id (if
        present) must match `unique_id`, or is set to it if omitted -
        renaming a unique_id isn't supported by this method (delete +
        create instead, deliberately - a rename is really two operations).

        Returns (location, reloaded) - see _reload_template for when
        reloaded can come back False despite a successful write.
        """
        config = dict(config)
        if config.get("unique_id") not in (None, unique_id):
            raise ValueError(
                f"config's unique_id ('{config['unique_id']}') does not match "
                f"the unique_id being updated ('{unique_id}') - use delete_entity "
                "+ create_entity to change a unique_id"
            )
        config["unique_id"] = unique_id

        location = await self.find_entity(unique_id)
        document = await self._load_document(location.file_path)
        content = await self.hass.async_add_executor_job(
            self._build_update_content, document, location, config
        )

        await self.file_manager.write_file(
            location.file_path,
            content,
            expected_hash=expected_hash,
            validate_before_write=True,
        )

        reloaded = await self._reload_template()
        _LOGGER.info(
            "Updated template entity unique_id='%s' in %s",
            unique_id,
            location.file_path,
        )
        return location, reloaded

    def _build_update_content(
        self, document: Any, location: TemplateEntityLocation, config: dict[str, Any]
    ) -> str:
        """Synchronous: replace one entity's config in place, return the new file content."""
        yaml = _new_yaml()
        blocks = self._template_blocks(document)
        blocks[location.block_index][location.platform][location.entity_index] = config

        from io import StringIO

        buffer = StringIO()
        yaml.dump(document, buffer)
        return buffer.getvalue()

    async def delete_entity(
        self, unique_id: str, *, expected_hash: str | None = None
    ) -> tuple[TemplateEntityLocation, bool]:
        """Delete a template entity by unique_id.

        Cleans up after itself: if removing this entity empties its
        platform's list, that platform key is removed; if that leaves the
        block with nothing but trigger/condition/variable keys (no
        remaining platform lists), the whole block is removed.

        Returns (location, reloaded) - see _reload_template for when
        reloaded can come back False despite a successful write.
        """
        location = await self.find_entity(unique_id)
        document = await self._load_document(location.file_path)
        content = await self.hass.async_add_executor_job(
            self._build_delete_content, document, location
        )

        await self.file_manager.write_file(
            location.file_path,
            content,
            expected_hash=expected_hash,
            validate_before_write=True,
        )

        reloaded = await self._reload_template()
        _LOGGER.info(
            "Deleted template entity unique_id='%s' from %s",
            unique_id,
            location.file_path,
        )
        return location, reloaded

    def _build_delete_content(
        self, document: Any, location: TemplateEntityLocation
    ) -> str:
        """Synchronous: remove one entity (and empty platform/block), return new file content."""
        yaml = _new_yaml()
        blocks = self._template_blocks(document)
        block = blocks[location.block_index]
        entities = block[location.platform]
        del entities[location.entity_index]

        if not entities:
            del block[location.platform]

        if not any(platform in block for platform in TEMPLATE_PLATFORMS):
            del blocks[location.block_index]

        from io import StringIO

        buffer = StringIO()
        yaml.dump(document, buffer)
        return buffer.getvalue()
