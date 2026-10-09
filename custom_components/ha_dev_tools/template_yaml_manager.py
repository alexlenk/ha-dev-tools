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

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.condition import async_validate_conditions_config
from homeassistant.helpers.script import async_validate_actions_config
from homeassistant.helpers.trigger import async_validate_trigger_config
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .file_manager import FileManager, package_file
from .yaml_style import (
    merge_preserving_style,
    quote_ambiguous_scalars,
    spliced_or_full,
    surgical_edit,
    to_json_safe,
)

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


def _load_yaml(content: str) -> Any:
    """Synchronous: build a fresh YAML() and load content with it, for
    hass.async_add_executor_job() - same reasoning as automation_manager.py's
    helper of the same name: YAML(typ="rt")'s own __init__ does a blocking
    scandir, so a bare `_new_yaml().load` passed as the executor job's
    callable only defers load() to the executor - `_new_yaml()` itself is
    evaluated eagerly on the event loop before being passed in."""
    return _new_yaml().load(content)


@dataclass(frozen=True)
class TemplateEntityLocation:
    """Where a given template entity is defined."""

    file_path: str  # relative to the HA config dir
    is_package: bool
    block_index: int
    platform: str
    entity_index: int


async def validate_triggers(hass: HomeAssistant, triggers: Any) -> None:
    """Refuse triggers HA wouldn't load, before anything is written (issue
    #125) - checked with HA's own trigger validation, the way it checks a
    template block's triggers when loading it. The triggers are written as
    given; the validated form (e.g. `trigger:` normalized to `platform:`)
    is only used for the check."""
    if not triggers:
        return
    try:
        await async_validate_trigger_config(hass, cv.TRIGGER_SCHEMA(triggers))
    except (vol.Invalid, HomeAssistantError) as exc:
        raise ValueError(
            f"Invalid triggers - nothing was written: {exc}. Each trigger is "
            "a mapping, e.g. {'trigger': 'state', 'entity_id': 'sensor.x'}."
        ) from exc


# A trigger-based block's own keys (issue #140), with the singular names
# older configs use. An edit keeps whichever name the block already has.
BLOCK_FIELDS: dict[str, tuple[str, ...]] = {
    "triggers": ("triggers", "trigger"),
    "conditions": ("conditions", "condition"),
    "variables": ("variables",),
    "actions": ("actions", "action"),
}


def _block_key(block: Any, field: str) -> str | None:
    """The name `field` has in `block`, if it's there at all."""
    return next((name for name in BLOCK_FIELDS[field] if name in block), None)


def block_members(block: Any) -> list[dict[str, Any]]:
    """Every entity a template block defines - all of them change when its
    triggers do."""
    return [
        {
            "platform": platform,
            "unique_id": entity.get("unique_id"),
            "name": entity.get("name"),
        }
        for platform in TEMPLATE_PLATFORMS
        if isinstance(block.get(platform), list)
        for entity in block[platform]
        if isinstance(entity, dict)
    ]


async def validate_block_changes(hass: HomeAssistant, changes: dict[str, Any]) -> None:
    """Check new block-level triggers/conditions/variables/actions with HA's
    own validation, before anything is written (as validate_triggers)."""
    if "triggers" in changes:
        if not changes["triggers"]:
            raise ValueError(
                "a trigger-based block can't lose its triggers - that would "
                "turn every entity in it into a state-based one; use "
                "delete_template_entity + create_template_entity for that"
            )
        await validate_triggers(hass, changes["triggers"])
    checks = {
        "conditions": lambda value: async_validate_conditions_config(
            hass, cv.CONDITIONS_SCHEMA(value)
        ),
        "actions": lambda value: async_validate_actions_config(
            hass, cv.SCRIPT_SCHEMA(value)
        ),
    }
    for field, check in checks.items():
        if changes.get(field) is None:
            continue
        try:
            await check(changes[field])
        except (vol.Invalid, HomeAssistantError) as exc:
            raise ValueError(f"Invalid {field} - nothing was written: {exc}") from exc
    if changes.get("variables") is not None and not isinstance(
        changes["variables"], dict
    ):
        raise ValueError("variables is a mapping of name: template")


@dataclass
class TemplateBatchResult:
    """One file of a batch delete: which ids left it, and its content."""

    file_path: str
    unique_ids: list[str]
    content_before: str
    content_after: str
    reloaded: bool


class TemplateBatchError(Exception):
    """A batch delete stopped at a failed write; `written` already changed."""

    def __init__(self, message: str, written: list[TemplateBatchResult]) -> None:
        """Init with the error and the files already written."""
        super().__init__(message)
        self.written = written


@dataclass(frozen=True)
class TemplateWriteResult:
    """What a template entity write actually did - location, reload status,
    and before/after file content, for mirror.py (docs/AUTOMATION_TESTING_DESIGN.md's
    "Mirroring" section) to push. Unlike write_automation, content_before is
    never None here: create_entity already requires its target package file
    to exist, and update_entity/delete_entity only reach this point via
    find_entity, which already found the entity in an existing file."""

    location: TemplateEntityLocation
    reloaded: bool
    content_before: str
    content_after: str
    # For a block-level edit (issue #140): every entity the block defines,
    # and its changed keys before and after.
    block: dict[str, Any] | None = None


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

    def loaded_entity_id(
        self, platform: str, unique_id: str, block_unique_id: str | None = None
    ) -> str | None:
        """The entity_id a template entity came up as, or None if it didn't
        (not registered, or registered but not set up, e.g. after a
        trigger block HA rejected on reload - issue #125). An entity in a
        block with its own unique_id is registered as "<block>-<entity>",
        the way HA's template integration composes it."""
        registry_id = f"{block_unique_id}-{unique_id}" if block_unique_id else unique_id
        entity_id = er.async_get(self.hass).async_get_entity_id(
            platform, "template", registry_id
        )
        if entity_id is None or self.hass.states.get(entity_id) is None:
            return None
        return entity_id

    @staticmethod
    def block_unique_id(content: str, block_index: int) -> str | None:
        """Synchronous: the unique_id of template block `block_index` in
        `content`, if it has one."""
        document = _load_yaml(content)
        blocks = document.get(TEMPLATE_KEY) if isinstance(document, dict) else None
        if isinstance(blocks, dict):
            blocks = [blocks]
        if not isinstance(blocks, list) or not 0 <= block_index < len(blocks):
            return None
        block = blocks[block_index]
        unique_id = block.get("unique_id") if isinstance(block, dict) else None
        return str(unique_id) if unique_id is not None else None

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
        return await self.hass.async_add_executor_job(_load_yaml, content)

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
                                "config": to_json_safe(dict(entity_conf)),
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
        return location, to_json_safe(dict(entity_conf))

    async def create_entity(
        self,
        platform: str,
        config: dict[str, Any],
        *,
        package: str,
        triggers: list[dict[str, Any]] | None = None,
        expected_hash: str | None = None,
        dry_run: bool = False,
    ) -> TemplateWriteResult:
        """Create a new template entity in its own new template: block.

        See _reload_template for when the result's reloaded can come back
        False despite a successful write.

        `package` names an existing `packages/<package>` file (relative to
        packages/) - required, no default-file fallback, since
        configuration.yaml itself is read-only under this integration's
        default security policy (see module docstring); this tool won't
        invent a new package file either.

        `config` must include a `unique_id` - required for every write
        this module does (see module docstring) - and must not already be
        in use by another template entity.

        `dry_run=True` resolves everything and computes content_after the
        same way, but returns before file_manager.write_file()/reloading -
        see automation_manager.write_automation's identical parameter for
        why (issue #35's dry-run + mirroring proposed/* branch support).
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
        await validate_triggers(self.hass, triggers)

        config = quote_ambiguous_scalars(config)
        triggers = quote_ambiguous_scalars(triggers) if triggers else triggers

        file_path = package_file(package)
        if not (self._config_dir / file_path).is_file():
            raise TemplateEntityNotFoundError(
                f"Package file '{file_path}' does not exist - create it "
                "first, this tool won't invent a new package file"
            )

        content_before = await self.file_manager.read_file(file_path)
        document = await self.hass.async_add_executor_job(_load_yaml, content_before)
        content_after, block_index = await self.hass.async_add_executor_job(
            self._build_create_content,
            document,
            platform,
            config,
            triggers,
            content_before,
        )

        if dry_run:
            return TemplateWriteResult(
                location=TemplateEntityLocation(
                    file_path=file_path,
                    is_package=True,
                    block_index=block_index,
                    platform=platform,
                    entity_index=0,
                ),
                reloaded=False,
                content_before=content_before,
                content_after=content_after,
            )

        # Routes through FileManager for the same reasons write_automation
        # does: security allowlist enforcement (packages/*.yaml is
        # write-permitted, configuration.yaml is not - see module
        # docstring), hash-conflict re-check right before the write, backup,
        # atomic write.
        await self.file_manager.write_file(
            file_path,
            content_after,
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
        return TemplateWriteResult(
            location=location,
            reloaded=reloaded,
            content_before=content_before,
            content_after=content_after,
        )

    def _build_create_content(
        self,
        document: Any,
        platform: str,
        config: dict[str, Any],
        triggers: list[dict[str, Any]] | None,
        original: str | None = None,
    ) -> tuple[str, int]:
        """Synchronous: append a new template: block, return (new file content, its index).

        The block is inserted after the last existing one without
        re-dumping the rest of the file where possible - see
        yaml_style.surgical_edit (issue #53).
        """
        yaml = _new_yaml()

        spliceable = document is not None
        if document is None:
            document = CommentedMap()
        blocks = document.get(TEMPLATE_KEY)
        if not isinstance(blocks, list):
            blocks = (
                CommentedSeq([blocks]) if isinstance(blocks, dict) else CommentedSeq()
            )
            spliceable = False
        document[TEMPLATE_KEY] = blocks

        new_block: dict[str, Any] = {}
        if triggers:
            new_block["triggers"] = triggers
        new_block[platform] = [config]
        spliced = surgical_edit(
            original if spliceable else None,
            blocks,
            None,
            "append",
            yaml,
            lambda: blocks.append(new_block),
        )
        block_index = len(blocks) - 1

        from io import StringIO

        buffer = StringIO()
        yaml.dump(document, buffer)
        return spliced_or_full(spliced, buffer.getvalue(), _load_yaml), block_index

    async def update_entity(
        self,
        unique_id: str,
        config: dict[str, Any] | None,
        *,
        block: dict[str, Any] | None = None,
        expected_hash: str | None = None,
        dry_run: bool = False,
    ) -> TemplateWriteResult:
        """Update an existing template entity's config in place, by unique_id.

        Only touches the resolved entity's own dict within its existing
        block - triggers/conditions/variables and any sibling entities in
        the same block are left untouched. `config`'s own unique_id (if
        present) must match `unique_id`, or is set to it if omitted -
        renaming a unique_id isn't supported by this method (delete +
        create instead, deliberately - a rename is really two operations).

        See _reload_template for when the result's reloaded can come back
        False despite a successful write.

        `block` changes the entity's *block* instead of (or as well as) the
        entity itself (issue #140): its triggers, conditions, variables or
        actions (BLOCK_FIELDS; None removes conditions/variables/actions).
        That changes every entity in the block, and only works on a
        trigger-based one - turning a state-based block into a trigger-based
        one, or back, changes what every entity in it updates on, so it's
        refused (delete + create). A reload keeps a trigger-based entity's
        state, unlike delete + create.

        `dry_run=True` - see create_entity's identical parameter.
        """
        if config is None and not block:
            raise ValueError("nothing to change - pass config and/or block fields")
        if config is not None:
            config = dict(config)
            if config.get("unique_id") not in (None, unique_id):
                raise ValueError(
                    f"config's unique_id ('{config['unique_id']}') does not match "
                    f"the unique_id being updated ('{unique_id}') - use "
                    "delete_entity + create_entity to change a unique_id"
                )
            config["unique_id"] = unique_id
            config = quote_ambiguous_scalars(config)
        if block:
            unknown = sorted(set(block) - set(BLOCK_FIELDS))
            if unknown:
                raise ValueError(f"not block fields: {unknown}")
            await validate_block_changes(self.hass, block)
            block = {
                field: quote_ambiguous_scalars(value) if value else value
                for field, value in block.items()
            }

        location = await self.find_entity(unique_id)
        content_before = await self.file_manager.read_file(location.file_path)
        document = await self.hass.async_add_executor_job(_load_yaml, content_before)
        current = self._template_blocks(document)[location.block_index]
        block_report = None
        if block:
            if _block_key(current, "triggers") is None:
                raise ValueError(
                    f"'{unique_id}' is in a state-based template block (no "
                    "triggers) - adding triggers would turn every entity in "
                    "it into a trigger-based one; use delete_template_entity "
                    "+ create_template_entity with triggers instead"
                )
            block_report = {
                "members": block_members(current),
                "before": {
                    field: to_json_safe(
                        current.get(_block_key(current, field) or field)
                    )
                    for field in block
                },
                "after": {field: to_json_safe(value) for field, value in block.items()},
            }
        content_after = await self.hass.async_add_executor_job(
            self._build_update_content,
            document,
            location,
            config,
            content_before,
            block,
        )

        if dry_run:
            return TemplateWriteResult(
                location=location,
                reloaded=False,
                content_before=content_before,
                content_after=content_after,
                block=block_report,
            )

        await self.file_manager.write_file(
            location.file_path,
            content_after,
            expected_hash=expected_hash,
            validate_before_write=True,
        )

        reloaded = await self._reload_template()
        _LOGGER.info(
            "Updated template entity unique_id='%s' in %s",
            unique_id,
            location.file_path,
        )
        return TemplateWriteResult(
            location=location,
            reloaded=reloaded,
            content_before=content_before,
            content_after=content_after,
            block=block_report,
        )

    def _build_update_content(
        self,
        document: Any,
        location: TemplateEntityLocation,
        config: dict[str, Any] | None,
        original: str | None = None,
        block: dict[str, Any] | None = None,
    ) -> str:
        """Synchronous: patch one entity's config in place, return the new file content.

        The loaded entity is patched rather than swapped for the caller's
        plain dict, so its unchanged fields keep their original formatting -
        see yaml_style.merge_preserving_style (issue #92). Only the entity's
        own lines of `original` are rewritten where possible - see
        yaml_style.surgical_edit (issue #53).
        """
        yaml = _new_yaml()
        blocks = self._template_blocks(document)
        entities = blocks[location.block_index][location.platform]
        index = location.entity_index

        def _replace() -> None:
            entities[index] = merge_preserving_style(entities[index], config)

        def _patch_block() -> None:
            current = blocks[location.block_index]
            patched = dict(current)
            for field, value in (block or {}).items():
                name = _block_key(current, field) or field
                if value is None:
                    patched.pop(name, None)
                else:
                    patched[name] = value
            blocks[location.block_index] = merge_preserving_style(current, patched)

        if block and config is not None:
            # Both: the whole block is rewritten - entity and block keys.
            def _both() -> None:
                _replace()
                _patch_block()

            spliced = surgical_edit(
                original, blocks, location.block_index, "replace", yaml, _both
            )
        elif block:
            spliced = surgical_edit(
                original, blocks, location.block_index, "replace", yaml, _patch_block
            )
        else:
            spliced = surgical_edit(
                original, entities, index, "replace", yaml, _replace
            )

        from io import StringIO

        buffer = StringIO()
        yaml.dump(document, buffer)
        return spliced_or_full(spliced, buffer.getvalue(), _load_yaml)

    async def delete_entity(
        self,
        unique_id: str,
        *,
        expected_hash: str | None = None,
        dry_run: bool = False,
    ) -> TemplateWriteResult:
        """Delete a template entity by unique_id.

        Cleans up after itself: if removing this entity empties its
        platform's list, that platform key is removed; if that leaves the
        block with nothing but trigger/condition/variable keys (no
        remaining platform lists), the whole block is removed.

        See _reload_template for when the result's reloaded can come back
        False despite a successful write.

        `dry_run=True` - see create_entity's identical parameter.
        """
        location = await self.find_entity(unique_id)
        content_before = await self.file_manager.read_file(location.file_path)
        document = await self.hass.async_add_executor_job(_load_yaml, content_before)
        content_after = await self.hass.async_add_executor_job(
            self._build_delete_content, document, location, content_before
        )

        if dry_run:
            return TemplateWriteResult(
                location=location,
                reloaded=False,
                content_before=content_before,
                content_after=content_after,
            )

        await self.file_manager.write_file(
            location.file_path,
            content_after,
            expected_hash=expected_hash,
            validate_before_write=True,
        )

        reloaded = await self._reload_template()
        _LOGGER.info(
            "Deleted template entity unique_id='%s' from %s",
            unique_id,
            location.file_path,
        )
        return TemplateWriteResult(
            location=location,
            reloaded=reloaded,
            content_before=content_before,
            content_after=content_after,
        )

    def _locate(
        self, document: Any, file_path: str, unique_id: str
    ) -> TemplateEntityLocation:
        """Synchronous: `unique_id`'s location in an already-loaded document
        (find_entity already made sure it's there exactly once)."""
        for block_index, block in enumerate(self._template_blocks(document)):
            for platform in TEMPLATE_PLATFORMS:
                entities = block.get(platform) if isinstance(block, dict) else None
                for entity_index, conf in enumerate(entities or []):
                    if isinstance(conf, dict) and conf.get("unique_id") == unique_id:
                        return TemplateEntityLocation(
                            file_path=file_path,
                            is_package=file_path != DEFAULT_CONFIG_FILE,
                            block_index=block_index,
                            platform=platform,
                            entity_index=entity_index,
                        )
        raise TemplateEntityNotFoundError(
            f"No template entity with unique_id '{unique_id}' found"
        )

    def _build_multi_delete_content(
        self, file_path: str, content: str, unique_ids: list[str]
    ) -> str:
        """Synchronous: `content` with every one of `unique_ids` removed -
        one at a time in memory, each re-located after the previous
        removal shifted the others, so the file is written once."""
        for unique_id in unique_ids:
            document = _load_yaml(content)
            location = self._locate(document, file_path, unique_id)
            content = self._build_delete_content(document, location, content)
        return content

    async def delete_entities(
        self, unique_ids: list[str], *, dry_run: bool = False
    ) -> list[TemplateBatchResult]:
        """Delete several template entities: every id resolved first, all
        removals computed in memory, each file written once and template
        reloaded once (issue #127) - instead of a write and a full reload
        per id, which was slow enough to time out partway.

        One result per file. A write that fails stops the batch: the files
        already written are returned (and reloaded) with the error, so the
        caller can still report and mirror exactly what changed.
        """
        by_file: dict[str, list[str]] = {}
        for unique_id in unique_ids:
            location = await self.find_entity(unique_id)
            by_file.setdefault(location.file_path, []).append(unique_id)

        planned = []
        for file_path, ids in by_file.items():
            content_before = await self.file_manager.read_file(file_path)
            content_after = await self.hass.async_add_executor_job(
                self._build_multi_delete_content, file_path, content_before, ids
            )
            planned.append((file_path, ids, content_before, content_after))
        if dry_run:
            return [
                TemplateBatchResult(path, ids, before, after, reloaded=False)
                for path, ids, before, after in planned
            ]

        written: list[TemplateBatchResult] = []
        error: str | None = None
        for file_path, ids, content_before, content_after in planned:
            try:
                await self.file_manager.write_file(
                    file_path, content_after, validate_before_write=True
                )
            except (OSError, PermissionError, ValueError, RuntimeError) as exc:
                error = f"{file_path}: {exc}"
                break
            written.append(
                TemplateBatchResult(
                    file_path, ids, content_before, content_after, reloaded=False
                )
            )
        reloaded = await self._reload_template() if written else False
        _LOGGER.info(
            "Deleted template entities %s in one write per file",
            [unique_id for result in written for unique_id in result.unique_ids],
        )
        for result in written:
            result.reloaded = reloaded
        if error is not None:
            raise TemplateBatchError(error, written)
        return written

    def _build_delete_content(
        self,
        document: Any,
        location: TemplateEntityLocation,
        original: str | None = None,
    ) -> str:
        """Synchronous: remove one entity (and empty platform/block), return new file content.

        Only the removed lines leave `original` where possible - the
        entity's own, or its whole platform key or block when this empties
        it (see yaml_style.surgical_edit, issue #53).
        """
        yaml = _new_yaml()
        blocks = self._template_blocks(document)
        block = blocks[location.block_index]
        entities = block[location.platform]

        def _delete() -> None:
            del entities[location.entity_index]
            if not entities:
                del block[location.platform]
            if not any(platform in block for platform in TEMPLATE_PLATFORMS):
                del blocks[location.block_index]

        other_platforms = [
            p for p in TEMPLATE_PLATFORMS if p != location.platform and p in block
        ]
        container: Any
        key: Any
        if len(entities) > 1:
            container, key = entities, location.entity_index
        elif other_platforms:
            container, key = block, location.platform
        else:
            container, key = blocks, location.block_index
        spliced = surgical_edit(original, container, key, "delete", yaml, _delete)

        from io import StringIO

        buffer = StringIO()
        yaml.dump(document, buffer)
        return spliced_or_full(spliced, buffer.getvalue(), _load_yaml)
