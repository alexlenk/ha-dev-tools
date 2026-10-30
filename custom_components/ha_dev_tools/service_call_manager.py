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

`control_entities` (2026-10-22) is the owner's decision to open the
actuator domains after all - on / off / toggle (plus a light's brightness)
for a fixed list of domains, each mapped to its own hardcoded service, so
it's still no passthrough. lock, alarm_control_panel and siren stay out;
devices and entities listed on the Configure page (a garage door, say) are
refused, and every call is confirm-gated like the rest.

Every caller of these goes through the same WriteGatedTool propose/confirm
gate as write_automation (see write_confirmation.py) on top of the two
GatedTool checks (arm file + admin) every tool already requires - these
aren't a replacement for that, just a narrower surface underneath it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import Context, HomeAssistant

from . import audit_manager

_LOGGER = logging.getLogger(__name__)

NUMBER_DOMAINS = ("number", "input_number")
MAX_WAIT_SECONDS = 50
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
    hass: HomeAssistant,
    automation_id: str,
    *,
    skip_condition: bool = True,
    wait_seconds: float = 0,
) -> dict[str, Any]:
    """Trigger an existing automation by its config id.

    Resolves automation_id (the YAML `id:` field) to a live automation.*
    entity the same way get_automation's runtime-state check does -
    `automation.<id>` is never a valid entity_id guess, since entity_id is
    derived from the automation's `alias`, not its `id` (see
    audit_manager.find_automation_state). Raises if no live entity exists
    yet for this id, rather than guessing or silently no-oping.

    The run is detached from the caller, as the UI's "Run actions" button
    is: it used to be awaited (blocking=True), so a client that gave up
    after its timeout cancelled the automation - and the scripts it was
    waiting on - part-way through (issue #131). Now it runs as a task of
    its own with its own context, and this waits at most `wait_seconds`
    (default 0) to report how a short run ended; waiting longer never
    cancels it. `context_id` identifies the run in the logbook and its
    trace.
    """
    state = audit_manager.find_automation_state(hass, automation_id)
    if state is None:
        raise AutomationNotRunningError(
            f"No automation.* entity found for id {automation_id!r} - it "
            "may not have been reloaded since being added or last edited. "
            "Call reload_domain with domain='automation' first."
        )
    context = Context()
    run = hass.async_create_background_task(
        hass.services.async_call(
            "automation",
            "trigger",
            {"entity_id": state.entity_id, "skip_condition": skip_condition},
            blocking=True,
            context=context,
        ),
        f"ha_dev_tools trigger_automation {state.entity_id}",
    )
    run.add_done_callback(_log_failed_run)
    if wait_seconds > 0:
        await asyncio.wait({run}, timeout=min(wait_seconds, MAX_WAIT_SECONDS))
    result: dict[str, Any] = {
        "entity_id": state.entity_id,
        "context_id": context.id,
        "finished": run.done(),
    }
    if run.done() and not run.cancelled() and run.exception() is not None:
        result["error"] = str(run.exception())
    return result


def _log_failed_run(run: asyncio.Task[Any]) -> None:
    """Retrieve a detached run's error, so it's logged once, as HA would."""
    if not run.cancelled() and (exc := run.exception()) is not None:
        _LOGGER.warning("Triggered automation failed: %s", exc)


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


# --- control_entities: on / off / toggle without an automation ----------------
#
# The owner's decisions (2026-10-22): switch, light, fan, input_boolean,
# climate, media_player, humidifier, cover and valve; on / off / toggle,
# plus brightness for lights; every call confirm-gated; devices and
# entities listed in the Configure page are never acted on. lock,
# alarm_control_panel and siren aren't controllable at all - none of them
# is a domain below, and they're refused by name for a clear message.

# domain -> (on, off, toggle) service
CONTROL_SERVICES: dict[str, tuple[str, str, str]] = {
    domain: ("turn_on", "turn_off", "toggle")
    for domain in (
        "switch",
        "light",
        "fan",
        "input_boolean",
        "climate",
        "media_player",
        "humidifier",
    )
} | {
    "cover": ("open_cover", "close_cover", "toggle"),
    "valve": ("open_valve", "close_valve", "toggle"),
}
NEVER_CONTROLLED = ("lock", "alarm_control_panel", "siren")
CONTROL_ACTIONS = ("on", "off", "toggle")


class ControlRefusedError(Exception):
    """A control_entities call that can't go ahead - nothing was called."""


def _excluded(hass: HomeAssistant) -> tuple[set[str], set[str]]:
    from .const import (
        DOMAIN,
        OPT_CONTROL_EXCLUDED_DEVICES,
        OPT_CONTROL_EXCLUDED_ENTITIES,
    )

    entries = hass.config_entries.async_entries(DOMAIN)
    options = entries[0].options if entries else {}
    return (
        set(options.get(OPT_CONTROL_EXCLUDED_DEVICES) or []),
        set(options.get(OPT_CONTROL_EXCLUDED_ENTITIES) or []),
    )


def plan_control(
    hass: HomeAssistant,
    entity_ids: list[str],
    action: str,
    brightness_pct: float | None = None,
) -> list[dict[str, Any]]:
    """Each entity's service call and what it is now - or every reason the
    call is refused, before anything happens."""
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    if action not in CONTROL_ACTIONS:
        raise ControlRefusedError(f"action is one of {CONTROL_ACTIONS}")
    problems: list[str] = []
    if brightness_pct is not None and action != "on":
        problems.append("brightness_pct goes with action 'on'")
    if len(set(entity_ids)) != len(entity_ids):
        problems.append("an entity is listed more than once")
    excluded_devices, excluded_entities = _excluded(hass)
    entities, devices, areas = (
        er.async_get(hass),
        dr.async_get(hass),
        ar.async_get(hass),
    )
    plan = []
    for entity_id in entity_ids:
        domain = _domain_of(entity_id)
        if domain in NEVER_CONTROLLED:
            problems.append(f"{entity_id}: {domain} is never controlled by an agent")
            continue
        if domain not in CONTROL_SERVICES:
            problems.append(
                f"{entity_id}: {domain} isn't controllable here (one of "
                f"{', '.join(sorted(CONTROL_SERVICES))})"
            )
            continue
        state = hass.states.get(entity_id)
        if state is None:
            problems.append(f"{entity_id}: no such entity")
            continue
        entry = entities.async_get(entity_id)
        device = (
            devices.async_get(entry.device_id) if entry and entry.device_id else None
        )
        if entity_id in excluded_entities:
            problems.append(
                f"{entity_id}: excluded from agent control (Configure page)"
            )
            continue
        if device is not None and device.id in excluded_devices:
            problems.append(
                f"{entity_id}: its device '{device.name_by_user or device.name}' is "
                "excluded from agent control (Configure page)"
            )
            continue
        if brightness_pct is not None and domain != "light":
            problems.append(f"{entity_id}: brightness_pct is for lights only")
            continue
        area_id = (entry.area_id if entry else None) or (
            device.area_id if device else None
        )
        area = areas.async_get_area(area_id) if area_id else None
        service = CONTROL_SERVICES[domain][CONTROL_ACTIONS.index(action)]
        data: dict[str, Any] = {"entity_id": entity_id}
        if brightness_pct is not None:
            data["brightness_pct"] = brightness_pct
        plan.append(
            {
                "entity_id": entity_id,
                "name": state.attributes.get("friendly_name", entity_id),
                "area": area.name if area else None,
                "state_now": state.state,
                "service": f"{domain}.{service}",
                "data": data,
            }
        )
    if problems:
        raise ControlRefusedError("; ".join(problems))
    return plan


async def control_entities(
    hass: HomeAssistant, plan: list[dict[str, Any]], user_id: str | None = None
) -> list[dict[str, Any]]:
    """Call each planned service as `user_id` (so the logbook says who);
    what each entity is afterwards. One that fails doesn't stop the
    others - each reports its own outcome."""
    results = []
    for step in plan:
        domain, service = step["service"].split(".", 1)
        entity_id = step["entity_id"]
        try:
            await hass.services.async_call(
                domain,
                service,
                step["data"],
                blocking=True,
                context=Context(user_id=user_id),
            )
        except Exception as err:  # noqa: BLE001 - reported per entity
            results.append({"entity_id": entity_id, "error": str(err)})
            continue
        state = hass.states.get(entity_id)
        results.append(
            {
                "entity_id": entity_id,
                "service": step["service"],
                "state_before": step["state_now"],
                "state_after": state.state if state else None,
            }
        )
    return results


# --- set_automation_enabled: an automation on or off (issue #184) ---------------
#
# automation.turn_on / turn_off - the UI's enable toggle, a runtime state HA
# restores across restarts, never the YAML. control_entities doesn't cover
# it (an automation isn't a device) and update_entities' `disabled` is the
# registry disable, a different thing. Addressed by config id like
# trigger_automation; the preview shows what each one reacts to, so turning
# one on shows what will start firing.


class AutomationEnableRefusedError(Exception):
    """A set_automation_enabled call that can't go ahead - nothing changed."""


def summarize_trigger(trigger: Any) -> str:
    """One line for a trigger: its type, then its other fields."""
    if not isinstance(trigger, dict):
        return str(trigger)
    kind = trigger.get("trigger") or trigger.get("platform") or "?"
    rest = ", ".join(
        f"{key}={value}"
        for key, value in trigger.items()
        if key not in ("trigger", "platform", "id")
    )
    return f"{kind}: {rest}" if rest else str(kind)


def plan_automation_enabled(
    hass: HomeAssistant,
    automation_ids: list[str],
    enabled: bool,
    configs: dict[str, dict[str, Any] | None],
) -> list[dict[str, Any]]:
    """Each automation's live entity, state and triggers - or every reason
    the call is refused. `configs` holds each id's config (None when it
    isn't in a file this integration reads)."""
    problems: list[str] = []
    if len(set(automation_ids)) != len(automation_ids):
        problems.append("an automation id is listed more than once")
    plan = []
    for automation_id in automation_ids:
        state = audit_manager.find_automation_state(hass, automation_id)
        if state is None:
            problems.append(
                f"{automation_id}: no automation.* entity for this id - reload "
                "automations (reload_domain) if it was just added"
            )
            continue
        config = configs.get(automation_id) or {}
        triggers = config.get("triggers", config.get("trigger")) or []
        if isinstance(triggers, dict):
            triggers = [triggers]
        wanted = "on" if enabled else "off"
        plan.append(
            {
                "automation_id": automation_id,
                "entity_id": state.entity_id,
                "alias": state.attributes.get("friendly_name", state.entity_id),
                "state_now": state.state,
                "last_triggered": (
                    str(last)
                    if (last := state.attributes.get("last_triggered"))
                    else None
                ),
                "triggers": [summarize_trigger(trigger) for trigger in triggers],
                "change": (
                    f"already {wanted}"
                    if state.state == wanted
                    else f"{state.state} -> {wanted}"
                ),
            }
        )
    if problems:
        raise AutomationEnableRefusedError("; ".join(problems))
    return plan


async def set_automations_enabled(
    hass: HomeAssistant,
    plan: list[dict[str, Any]],
    enabled: bool,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """automation.turn_on / turn_off for each planned automation, as
    `user_id`; each one's state before and after. turn_off stops a run in
    progress too, as the UI's toggle does."""
    service = "turn_on" if enabled else "turn_off"
    results = []
    for step in plan:
        entity_id = step["entity_id"]
        try:
            await hass.services.async_call(
                "automation",
                service,
                {"entity_id": entity_id},
                blocking=True,
                context=Context(user_id=user_id),
            )
        except Exception as err:  # noqa: BLE001 - reported per automation
            results.append({"automation_id": step["automation_id"], "error": str(err)})
            continue
        state = hass.states.get(entity_id)
        results.append(
            {
                "automation_id": step["automation_id"],
                "entity_id": entity_id,
                "state_before": step["state_now"],
                "state_after": state.state if state else None,
            }
        )
    return results
