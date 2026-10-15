"""YAML platform entries in packages: `<domain>:` lists of `platform:`
entries (issue #164) - `sensor: [{platform: statistics, ...}]`,
`binary_sensor:`, `switch:`, ... in `packages/*.yaml`.

The template tools cover `template:`, the derived-sensor tools config-entry
helpers; these entries had no write path at all, so retiring four
`platform: statistics` sensors meant hand-editing a package. Listed,
created, replaced and deleted here, in packages only - configuration.yaml
stays read-only.

An entry is found by domain + platform + its `unique_id`, or its `name`
when it has none (many of these platforms have no unique_id), across every
package; more than one match is refused with where each is, and `file`
narrows it. A config is checked with the platform's own PLATFORM_SCHEMA
before anything is written - a platform that can't be loaded is refused.
Like a `template:` platform key, a domain key may hold a single mapping
instead of a list (issue #163); it's read as a list of one, and kept.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.loader import IntegrationNotFound, async_get_integration
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from . import config_tools
from .file_manager import FileManager, package_file
from .yaml_style import (
    merge_preserving_style,
    quote_ambiguous_scalars,
    spliced_or_full,
    surgical_edit,
    tag_references,
    to_json_safe,
)

_LOGGER = logging.getLogger(__name__)

PACKAGES_DIR = "packages"
# Top-level keys whose lists aren't platform entries.
_NOT_PLATFORM_LISTS = {"automation", "script", "template", "homeassistant"}


class PlatformEntryError(ValueError):
    """An entry that can't be found, resolved or written - nothing changed."""


def _new_yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _load(content: str) -> Any:
    return _new_yaml().load(content)


@dataclass(slots=True, frozen=True)
class EntryLocation:
    file_path: str
    domain: str
    index: int
    platform: str
    unique_id: str | None
    name: str | None

    def describe(self) -> str:
        return f"{self.file_path} {self.domain}[{self.index}]"


def _entries(value: Any) -> list[Any] | None:
    """A domain key's entries - a list, or the one mapping (short form)."""
    if isinstance(value, dict):
        return [value]
    return value if isinstance(value, list) else None


def _platform_lists(document: Any) -> dict[str, list[Any]]:
    """{domain: entries} for every top-level key holding platform entries."""
    found: dict[str, list[Any]] = {}
    if not isinstance(document, dict):
        return found
    for domain, value in document.items():
        entries = _entries(value)
        if (
            domain in _NOT_PLATFORM_LISTS
            or not entries
            or not all(
                isinstance(entry, dict) and "platform" in entry for entry in entries
            )
        ):
            continue
        found[str(domain)] = entries
    return found


def _identity(entry: dict[str, Any]) -> tuple[str | None, str | None]:
    unique_id = entry.get("unique_id")
    name = entry.get("name")
    return (
        str(unique_id) if unique_id is not None else None,
        str(name) if name is not None else None,
    )


async def validation_error(
    hass: HomeAssistant, domain: str, config: dict[str, Any]
) -> str | None:
    """The platform's own PLATFORM_SCHEMA error for `config`, or None."""
    platform = config.get("platform")
    if not isinstance(platform, str) or not platform:
        return "an entry needs its platform"
    try:
        integration = await async_get_integration(hass, platform)
        module = await integration.async_get_platform(domain)
    except (IntegrationNotFound, ImportError) as err:
        return f"platform '{platform}' for '{domain}' can't be loaded: {err}"
    schema = getattr(module, "PLATFORM_SCHEMA", None)
    if schema is None:
        return f"'{platform}' has no YAML platform schema for '{domain}'"
    try:
        schema(dict(config))
    except config_tools.INVALID as err:
        return config_tools.humanize_invalid(config, err)
    return None


class PlatformEntryManager:
    """Package-scoped read/write of `<domain>:` platform entries."""

    def __init__(self, hass: HomeAssistant, file_manager: FileManager) -> None:
        self.hass = hass
        self.file_manager = file_manager
        self._config_dir = Path(hass.config.config_dir)

    def _glob_packages(self) -> list[str]:
        packages = self._config_dir / PACKAGES_DIR
        if not packages.is_dir():
            return []
        return sorted(
            str(path.relative_to(self._config_dir)) for path in packages.rglob("*.yaml")
        )

    async def _documents(self) -> list[tuple[str, str, Any]]:
        documents = []
        for file_path in await self.hass.async_add_executor_job(self._glob_packages):
            content = await self.file_manager.read_file(file_path)
            documents.append(
                (
                    file_path,
                    content,
                    await self.hass.async_add_executor_job(_load, content),
                )
            )
        return documents

    async def list_entries(
        self, *, domain: str | None = None, platform: str | None = None
    ) -> list[dict[str, Any]]:
        """Every platform entry in packages, optionally of one domain or
        platform. `config` is JSON-safe; the caller masks credentials."""
        results = []
        for file_path, _, document in await self._documents():
            for each_domain, entries in _platform_lists(document).items():
                if domain is not None and each_domain != domain:
                    continue
                for index, entry in enumerate(entries):
                    if platform is not None and entry.get("platform") != platform:
                        continue
                    unique_id, name = _identity(entry)
                    results.append(
                        {
                            "file_path": file_path,
                            "domain": each_domain,
                            "index": index,
                            "platform": entry.get("platform"),
                            "unique_id": unique_id,
                            "name": name,
                            "config": to_json_safe(dict(entry)),
                        }
                    )
        return results

    async def find(
        self,
        domain: str,
        platform: str,
        *,
        unique_id: str | None = None,
        name: str | None = None,
        file: str | None = None,
    ) -> list[EntryLocation]:
        """Every entry matching - by unique_id, or by name for an entry
        without one."""
        if (unique_id is None) == (name is None):
            raise PlatformEntryError("pass exactly one of unique_id or name")
        file_path = package_file(file) if file is not None else None
        found = []
        for path, _, document in await self._documents():
            if file_path is not None and path != file_path:
                continue
            for index, entry in enumerate(_platform_lists(document).get(domain, [])):
                if entry.get("platform") != platform:
                    continue
                entry_id, entry_name = _identity(entry)
                if (unique_id is not None and entry_id == unique_id) or (
                    name is not None and entry_id is None and entry_name == name
                ):
                    found.append(
                        EntryLocation(
                            path, domain, index, platform, entry_id, entry_name
                        )
                    )
        return found

    async def resolve(
        self, domain: str, platform: str, **selector: Any
    ) -> EntryLocation:
        found = await self.find(domain, platform, **selector)
        what = f"{domain} {platform} entry " + (
            f"with unique_id '{selector['unique_id']}'"
            if selector.get("unique_id") is not None
            else f"named '{selector.get('name')}' (and no unique_id)"
        )
        if not found:
            raise PlatformEntryError(f"no {what} in packages")
        if len(found) > 1:
            raise PlatformEntryError(
                f"{len(found)} of a {what}: "
                + ", ".join(location.describe() for location in found)
                + " - pass file to pick one"
            )
        return found[0]

    async def get_entry(self, location: EntryLocation) -> dict[str, Any]:
        content = await self.file_manager.read_file(location.file_path)
        document = await self.hass.async_add_executor_job(_load, content)
        return dict(_platform_lists(document)[location.domain][location.index])

    def _build(
        self,
        content: str,
        domain: str,
        location: EntryLocation | None,
        config: dict[str, Any] | None,
    ) -> str:
        """Synchronous: `content` with the entry replaced (location and
        config), appended (config only) or deleted (location only)."""
        yaml = _new_yaml()
        document = _load(content)
        if not isinstance(document, dict):
            document = CommentedMap()
        value = document.get(domain)
        spliceable = value is not None or location is None
        if location is None:
            if value is None:
                document[domain] = value = CommentedSeq()
                spliceable = False
            elif isinstance(value, dict):
                # One entry in the short form: a list now, to add a second.
                document[domain] = value = CommentedSeq([value])
                spliceable = False
            container: Any = value
            key: Any = len(value)

            def apply() -> None:
                container.append(config)

            spliced = surgical_edit(
                content if spliceable else None, container, key, "append", yaml, apply
            )
        else:
            short = isinstance(value, dict)
            if config is not None:
                container, key = (
                    (document, domain) if short else (value, location.index)
                )

                def apply() -> None:
                    container[key] = merge_preserving_style(container[key], config)

                spliced = surgical_edit(content, container, key, "replace", yaml, apply)
            else:
                last = short or len(value) == 1
                container, key = (document, domain) if last else (value, location.index)

                def apply() -> None:
                    del container[key]

                spliced = surgical_edit(content, container, key, "delete", yaml, apply)
        buffer = StringIO()
        yaml.dump(document, buffer)
        return spliced_or_full(spliced, buffer.getvalue(), _load)

    async def plan_write(
        self,
        domain: str,
        config: dict[str, Any],
        *,
        package: str | None = None,
        file: str | None = None,
    ) -> tuple[EntryLocation | None, str, str, str]:
        """(the entry replaced, or None for a new one; its file; content
        before; content after). An entry with this domain, platform and
        unique_id (or name, without one) is replaced; otherwise the config
        is a new entry in `package`."""
        platform = str(config.get("platform", ""))
        unique_id, name = _identity(config)
        if unique_id is None and name is None:
            raise PlatformEntryError(
                "the config needs a unique_id or a name to be found by later"
            )
        selector = {"unique_id": unique_id} if unique_id is not None else {"name": name}
        found = await self.find(domain, platform, file=file, **selector)
        if len(found) > 1:
            raise PlatformEntryError(
                f"{len(found)} matching entries: "
                + ", ".join(location.describe() for location in found)
                + " - pass file to pick one"
            )
        location = found[0] if found else None
        if location is not None:
            file_path = location.file_path
        elif package is not None:
            file_path = package_file(package)
        else:
            raise PlatformEntryError(
                "no such entry yet - pass package (a packages/ file) to create it"
            )
        try:
            content = await self.file_manager.read_file(file_path)
        except FileNotFoundError:
            raise PlatformEntryError(
                f"package file '{file_path}' doesn't exist - create it first"
            ) from None
        prepared = tag_references(quote_ambiguous_scalars(dict(config)))
        after = await self.hass.async_add_executor_job(
            self._build, content, domain, location, prepared
        )
        return location, file_path, content, after

    async def plan_delete(
        self, locations: list[EntryLocation]
    ) -> list[tuple[str, list[EntryLocation], str, str]]:
        """Per file: (file, its entries, content before, content after) -
        each removed in memory, highest index first so the others stay put."""
        by_file: dict[str, list[EntryLocation]] = {}
        for location in locations:
            by_file.setdefault(location.file_path, []).append(location)
        planned = []
        for file_path, entries in by_file.items():
            before = await self.file_manager.read_file(file_path)
            after = before
            for location in sorted(
                entries, key=lambda each: (each.domain, -each.index)
            ):
                after = await self.hass.async_add_executor_job(
                    self._build, after, location.domain, location, None
                )
            planned.append((file_path, entries, before, after))
        return planned

    async def write_file(self, file_path: str, content: str) -> None:
        await self.file_manager.write_file(
            file_path, content, validate_before_write=True
        )

    async def reload(self, platform: str) -> str:
        """Reload the platform if it can be (`<platform>.reload`), else say
        a restart is needed."""
        if self.hass.services.has_service(platform, "reload"):
            await self.hass.services.async_call(platform, "reload", blocking=True)
            return f"reloaded ({platform}.reload)"
        return (
            f"'{platform}' has no reload service - restart Home Assistant for "
            "the change to take effect"
        )
