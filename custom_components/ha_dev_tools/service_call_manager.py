"""Narrow, allowlisted service-call helpers - see issue #76.

Deliberately not a generic call_service passthrough. Issue #76 flagged that
as the broadest possible surface - it could reach any registered service,
including high-consequence ones on this specific instance (`lock.unlock`,
`alarm_control_panel.disarm`, `backup.*`, `homeassistant.restart`). Rather
than gate a single generic tool with an allow/deny list of domains, each
function here wraps exactly one service call, hardcoded, so the tool
surface itself is the allowlist:

- `trigger_automation` only ever calls `automation.trigger`, and only for
  an automation id that already resolves to a live entity - the security
  boundary is "already in reviewed automations.yaml/packages", not
  "whatever the caller passes".
- `set_number_value`/`set_boolean_value` only reach `number`/`input_number`
  and `input_boolean` entities respectively - refusing any other domain,
  in particular anything that could be a real-world actuator (a `switch`
  could be a desk lamp or a garage door opener; a `lock`/`alarm_control_panel`
  entity is out of scope entirely and was never considered here).

Every caller of these goes through the same WriteGatedTool propose/confirm
gate as write_automation (see write_confirmation.py) on top of the two
GatedTool checks (arm file + admin) every tool already requires - these
aren't a replacement for that, just a narrower surface underneath it.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from . import audit_manager

NUMBER_DOMAINS = ("number", "input_number")
BOOLEAN_DOMAIN = "input_boolean"


class AutomationNotRunningError(Exception):
    """Raised when an automation id has no live automation.* entity yet."""


class InvalidEntityDomainError(Exception):
    """Raised when an entity_id's domain isn't one this tool supports."""


class EntityNotFoundError(Exception):
    """Raised when an entity_id has no live state to act on."""


def _domain_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


async def trigger_automation(
    hass: HomeAssistant, automation_id: str, *, skip_condition: bool = True
) -> str:
    """Trigger an existing automation by its config id, return its entity_id.

    Resolves automation_id (the YAML `id:` field) to a live automation.*
    entity the same way get_automation's runtime-state check does -
    `automation.<id>` is never a valid entity_id guess, since entity_id is
    derived from the automation's `alias`, not its `id` (see
    audit_manager.find_automation_state). Raises if no live entity exists
    yet for this id, rather than guessing or silently no-oping.
    """
    state = audit_manager.find_automation_state(hass, automation_id)
    if state is None:
        raise AutomationNotRunningError(
            f"No automation.* entity found for id {automation_id!r} - it "
            "may not have been reloaded since being added or last edited. "
            "Call reload_domain with domain='automation' first."
        )
    await hass.services.async_call(
        "automation",
        "trigger",
        {"entity_id": state.entity_id, "skip_condition": skip_condition},
        blocking=True,
    )
    return state.entity_id


async def set_number_value(hass: HomeAssistant, entity_id: str, value: float) -> None:
    """Set a `number` or `input_number` entity's value via its own set_value service.

    Both domains take the same field shape (entity_id + value) but live on
    different services depending on whether the entity is helper-defined
    (input_number) or integration-provided (number - e.g. an EMHASS
    battery-schedule slot, or a Modbus-backed inverter setting, see
    issue #76). Refuses any other domain rather than guessing which
    service to call.
    """
    domain = _domain_of(entity_id)
    if domain not in NUMBER_DOMAINS:
        raise InvalidEntityDomainError(
            f"set_number_value only supports {NUMBER_DOMAINS} entities, got "
            f"domain {domain!r} from entity_id {entity_id!r}"
        )
    if hass.states.get(entity_id) is None:
        raise EntityNotFoundError(f"No live state for entity_id {entity_id!r}")
    await hass.services.async_call(
        domain, "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )


async def set_boolean_value(hass: HomeAssistant, entity_id: str, state: bool) -> None:
    """Turn an `input_boolean` helper on or off.

    Scoped to input_boolean only - a purely virtual helper domain, never a
    real-world actuator. `switch` (which could be anything from a desk lamp
    to a garage door opener) and every other domain are refused outright;
    they'd need their own separate risk review before ever getting a write
    tool, which issue #76 explicitly didn't ask for.
    """
    domain = _domain_of(entity_id)
    if domain != BOOLEAN_DOMAIN:
        raise InvalidEntityDomainError(
            f"set_boolean_value only supports {BOOLEAN_DOMAIN!r} entities, "
            f"got domain {domain!r} from entity_id {entity_id!r}"
        )
    if hass.states.get(entity_id) is None:
        raise EntityNotFoundError(f"No live state for entity_id {entity_id!r}")
    await hass.services.async_call(
        domain,
        "turn_on" if state else "turn_off",
        {"entity_id": entity_id},
        blocking=True,
    )
