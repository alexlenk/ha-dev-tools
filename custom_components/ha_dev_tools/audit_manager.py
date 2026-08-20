"""Static analysis over automation config for latent reliability bugs.

Home Assistant won't stop you from creating a duplicate automation id
across packages (package merge just concatenates - see
docs/RESTART_PLAN.md), and an automation whose trigger/condition/action
references a currently-unavailable entity will simply never fire without
any error - the exact class of "latent failure" this project exists to
surface. `audit_automations` walks every known automation and reports
both.

Scope: this first pass covers duplicate ids and unavailable-entity
references, the two checks that are cheap and reliable to detect
statically. Overlapping-trigger race detection and unhandled
rest_command/shell_command failures (also called out in
docs/RESTART_PLAN.md) are real but need more careful semantic analysis
to avoid false positives/negatives - deliberately left for a follow-up
rather than shipped half-confident.
"""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .automation_manager import AutomationManager

UNAVAILABLE_STATES = {"unavailable", "unknown"}


def _iter_entity_ids(node: Any) -> list[str]:
    """Recursively collect every entity_id reference in a trigger/condition/action tree."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "entity_id":
                if isinstance(value, str):
                    found.append(value)
                elif isinstance(value, list):
                    found.extend(v for v in value if isinstance(v, str))
            else:
                found.extend(_iter_entity_ids(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_iter_entity_ids(item))
    return found


async def audit_automations(
    hass: HomeAssistant, automation_manager: AutomationManager
) -> dict[str, Any]:
    """Audit every known automation for duplicate ids and unavailable entity references."""
    all_automations = await automation_manager.all_automations()
    id_locations: dict[str, list[str]] = {}
    unavailable_findings: list[dict[str, Any]] = []

    for location, entry in all_automations:
        automation_id = entry.get("id")
        if automation_id is None:
            continue
        automation_id = str(automation_id)
        id_locations.setdefault(automation_id, []).append(location.file_path)

        referenced = set(_iter_entity_ids(entry))
        unavailable = sorted(
            entity_id
            for entity_id in referenced
            if (state := hass.states.get(entity_id)) is not None
            and state.state in UNAVAILABLE_STATES
        )
        if unavailable:
            unavailable_findings.append(
                {
                    "automation_id": automation_id,
                    "file_path": location.file_path,
                    "unavailable_entities": unavailable,
                }
            )

    duplicate_findings = [
        {"automation_id": automation_id, "files": files}
        for automation_id, files in id_locations.items()
        if len(files) > 1
    ]

    return {
        "automations_checked": len(all_automations),
        "duplicate_ids": duplicate_findings,
        "references_unavailable_entities": unavailable_findings,
        "note": (
            "Overlapping-trigger race detection and unhandled "
            "rest_command/shell_command failure detection are not "
            "implemented yet - see automation_manager module docstring."
        ),
    }
