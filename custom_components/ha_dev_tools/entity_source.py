"""Where an entity is defined, and which tools read and change it (#185).

Diagnosis starts from an entity id, but each kind of definition has its own
tools: a UI Template helper is a derived-sensor config entry, a `template:`
entity lives in YAML, a storage helper in .storage, an integration's entity
in its config entry. Without a lookup an agent guesses from the tool names
- and on #185 concluded a UI Template helper wasn't readable at all.

Resolved from the entity registry (platform, config entry, unique_id),
then confirmed where it can be: the derived-sensor entry, the helper's
storage item, the automation/script file, the template block, the package
platform entry. An entity with no registry entry has no unique_id - YAML
without one, or a state set at runtime - and none of the update tools can
address it; candidates matching its name are listed.
"""

from __future__ import annotations

from typing import Any

from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from . import helper_manager
from .automation_manager import AutomationManager
from .derived_sensor_manager import DERIVED_SENSOR_DOMAINS
from .platform_entry_manager import PlatformEntryManager
from .script_manager import ScriptManager
from .template_yaml_manager import TemplateYamlManager
from .ws_call import WebSocketCommandError

# Registry fields any entity with a unique_id can change, whatever defines it.
_REGISTRY_CHANGES = (
    "registry: name, icon, area, device_class, labels, categories, "
    "disabled, hidden, show_as, entity_id"
)


def _tool(name: str, changes: str | None = None, **args: Any) -> dict[str, Any]:
    tool: dict[str, Any] = {"tool": name, "args": args}
    if changes:
        tool["changes"] = changes
    return tool


def _template_matches(registry_unique_id: str, entity: dict[str, Any]) -> bool:
    """A YAML template entity's registry unique_id is its own, or
    "<block unique_id>-<its own>" when the block sets one."""
    unique_id = entity.get("unique_id")
    if unique_id is None:
        return False
    return registry_unique_id == str(unique_id) or registry_unique_id.endswith(
        f"-{unique_id}"
    )


class EntitySourceResolver:
    """Resolves an entity id to where it's defined and its owning tools."""

    def __init__(
        self,
        hass: HomeAssistant,
        automation_manager: AutomationManager,
        script_manager: ScriptManager,
        template_yaml_manager: TemplateYamlManager,
        platforms: PlatformEntryManager,
    ) -> None:
        self.hass = hass
        self.automations = automation_manager
        self.scripts = script_manager
        self.templates = template_yaml_manager
        self.platforms = platforms

    async def describe(self, entity_id: str, user: User) -> dict[str, Any]:
        """The entity's state and registry entry, where it's defined, and the
        tools that read and change it."""
        state = self.hass.states.get(entity_id)
        entry = er.async_get(self.hass).async_get(entity_id)
        if state is None and entry is None:
            raise LookupError(f"no entity '{entity_id}' (no state, no registry entry)")
        result: dict[str, Any] = {
            "entity_id": entity_id,
            "state": state.state if state else None,
            "friendly_name": state.attributes.get("friendly_name") if state else None,
        }
        if entry is None:
            result["registry"] = None
            result["defined_in"] = await self._without_unique_id(entity_id, state)
            return result
        result["registry"] = self._registry(entry)
        defined_in = await self._defined_in(entity_id, entry, user)
        defined_in.setdefault("tools", []).append(
            _tool("update_entities", _REGISTRY_CHANGES, entity_id=entity_id)
        )
        result["defined_in"] = defined_in
        return result

    def _registry(self, entry: er.RegistryEntry) -> dict[str, Any]:
        device = (
            dr.async_get(self.hass).async_get(entry.device_id)
            if entry.device_id
            else None
        )
        area_id = entry.area_id or (device.area_id if device else None)
        area = ar.async_get(self.hass).async_get_area(area_id) if area_id else None
        return {
            "platform": entry.platform,
            "unique_id": entry.unique_id,
            "config_entry_id": entry.config_entry_id,
            "device": (device.name_by_user or device.name) if device else None,
            "area": area.name if area else None,
            "disabled_by": entry.disabled_by,
            "hidden_by": entry.hidden_by,
        }

    async def _defined_in(
        self, entity_id: str, entry: er.RegistryEntry, user: User
    ) -> dict[str, Any]:
        domain = entity_id.split(".", 1)[0]
        config_entry = (
            self.hass.config_entries.async_get_entry(entry.config_entry_id)
            if entry.config_entry_id
            else None
        )
        if config_entry is not None:
            return self._config_entry(entity_id, config_entry)
        if entry.platform == "automation":
            return await self._automation(entity_id)
        if entry.platform == "script":
            return await self._script(entity_id)
        if entry.platform == domain and domain in helper_manager.HELPER_DOMAINS:
            return await self._helper(domain, entry.unique_id, user)
        if entry.platform == "template":
            return await self._yaml_template(entry.unique_id)
        return await self._yaml_platform(domain, entry)

    def _config_entry(self, entity_id: str, config_entry: Any) -> dict[str, Any]:
        found: dict[str, Any] = {
            "config_entry_id": config_entry.entry_id,
            "integration": config_entry.domain,
            "title": config_entry.title,
        }
        if config_entry.domain in DERIVED_SENSOR_DOMAINS:
            entry_id = config_entry.entry_id
            return {
                "kind": "ui_helper",
                "helper_type": config_entry.domain,
                **found,
                "tools": [
                    _tool("get_derived_sensor", entry_id=entry_id),
                    _tool("update_derived_sensor", "its options", entry_id=entry_id),
                    _tool("delete_derived_sensor", entry_id=entry_id),
                ],
            }
        return {
            "kind": "integration",
            **found,
            "note": (
                f"provided by the {config_entry.domain} integration - its "
                "definition is the integration's own; reconfigure it in the UI"
            ),
            "tools": [
                _tool(
                    "write_customize",
                    "state_class, device_class, unit_of_measurement, "
                    "friendly_name, icon, assumed_state (fill in the {})",
                    entities={entity_id: {}},
                ),
            ],
        }

    async def _automation(self, entity_id: str) -> dict[str, Any]:
        state = self.hass.states.get(entity_id)
        automation_id = state.attributes.get("id") if state else None
        if automation_id is None:
            return {
                "kind": "automation",
                "note": "no config id - not editable here (an automation without id:)",
                "tools": [],
            }
        locations = await self.automations.find_all_locations(str(automation_id))
        return {
            "kind": "automation",
            "automation_id": automation_id,
            "files": [location.file_path for location in locations],
            "tools": [
                _tool("get_automation", automation_id=automation_id),
                _tool("write_automation", "its config", automation_id=automation_id),
                _tool(
                    "set_automation_enabled",
                    "on/off",
                    automation_ids=[automation_id],
                ),
            ],
        }

    async def _script(self, entity_id: str) -> dict[str, Any]:
        script_id = entity_id.split(".", 1)[1]
        locations = await self.scripts.find_all_locations(script_id)
        return {
            "kind": "script",
            "script_id": script_id,
            "files": [location.file_path for location in locations],
            "tools": [
                _tool("get_script", script_id=script_id),
                _tool("write_script", "its config", script_id=script_id),
            ],
        }

    async def _helper(self, domain: str, item_id: str, user: User) -> dict[str, Any]:
        try:
            items = await helper_manager.list_helpers(self.hass, user, domain)
        except WebSocketCommandError as err:
            return {"kind": "helper", "domain": domain, "error": str(err)}
        if any(str(item.get("id")) == item_id for item in items):
            return {
                "kind": "storage_helper",
                "domain": domain,
                "helper_id": item_id,
                "tools": [
                    _tool("list_helpers", domain=domain),
                    _tool(
                        "update_helper", "its settings", domain=domain, item_id=item_id
                    ),
                ],
            }
        return {
            "kind": "yaml_helper",
            "domain": domain,
            "note": (
                f"a YAML-defined {domain} (configuration.yaml or a package) - "
                "update_helper only reaches UI/storage helpers; edit the YAML"
            ),
            "tools": [_tool("get_config_file", key=domain)],
        }

    async def _yaml_template(self, registry_unique_id: str) -> dict[str, Any]:
        matches = [
            entity
            for entity in await self.templates.list_entities()
            if _template_matches(registry_unique_id, entity)
        ]
        if not matches:
            return {
                "kind": "yaml_template",
                "note": (
                    "a YAML template entity not found in configuration.yaml "
                    "or packages/ (a file this integration doesn't read?)"
                ),
                "tools": [],
            }
        unique_id = matches[0]["unique_id"]
        return {
            "kind": "yaml_template",
            "locations": [_template_location(entity) for entity in matches],
            "tools": [
                _tool("get_template_entity", unique_id=unique_id),
                _tool("update_template_entity", "its config", unique_id=unique_id),
            ],
            **(
                {}
                if all(entity["is_package"] for entity in matches)
                else {"note": "configuration.yaml is read-only for the write tools"}
            ),
        }

    async def _yaml_platform(
        self, domain: str, entry: er.RegistryEntry
    ) -> dict[str, Any]:
        matches = [
            item
            for item in await self.platforms.list_entries(
                domain=domain, platform=entry.platform
            )
            if item["unique_id"] == entry.unique_id
        ]
        if matches:
            item = matches[0]
            return {
                "kind": "package_platform_entry",
                "platform": entry.platform,
                "file_path": item["file_path"],
                "index": item["index"],
                "tools": [
                    _tool(
                        "list_package_platform_entities",
                        domain=domain,
                        platform=entry.platform,
                    ),
                    _tool(
                        "write_package_platform_entity",
                        "its config",
                        domain=domain,
                    ),
                ],
            }
        return {
            "kind": "yaml_platform",
            "platform": entry.platform,
            "note": (
                f"a `{domain}: - platform: {entry.platform}` entry outside "
                "packages/ (configuration.yaml or an included file) - read it "
                "with get_config_file; it's read-only for the write tools"
            ),
            "tools": [_tool("get_config_file", key=domain)],
        }

    async def _without_unique_id(self, entity_id: str, state: Any) -> dict[str, Any]:
        domain = entity_id.split(".", 1)[0]
        name = state.attributes.get("friendly_name") if state else None
        candidates: list[dict[str, Any]] = []
        if name is not None:
            candidates += [
                _template_location(entity)
                for entity in await self.templates.list_entities()
                if entity["platform"] == domain and entity.get("name") == name
            ]
            candidates += [
                {
                    "file_path": item["file_path"],
                    "domain": item["domain"],
                    "index": item["index"],
                    "platform": item["platform"],
                }
                for item in await self.platforms.list_entries(domain=domain)
                if item["name"] == name
            ]
        return {
            "kind": "no_unique_id",
            "note": (
                "no entity registry entry, so no unique_id: defined in YAML "
                "without one, or a state set at runtime. No update tool can "
                "address it, and update_entities can't rename or move it - "
                "give it a unique_id in its YAML first"
            ),
            "candidates": candidates,
        }


def _template_location(entity: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_path": entity["file_path"],
        "block_index": entity["block_index"],
        "platform": entity["platform"],
        "entity_index": entity["entity_index"],
        "unique_id": entity["unique_id"],
    }
