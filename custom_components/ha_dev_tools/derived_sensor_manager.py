"""CRUD for calculated/derived sensor "helpers" implemented as config entries.

Home Assistant exposes a second family of "helpers" alongside the flat
storage-collection domains `helper_manager.py` covers (`input_boolean`,
`counter`, `timer`, ...): Min/Max, Utility Meter, Integration (Riemann
sum, domain `integration`), Statistics, Threshold, Derivative, and
Filter. Each of these is a full config-entry integration driven by
`homeassistant.helpers.schema_config_entry_flow.SchemaConfigFlowHandler`
- confirmed by reading every one of these domains' own `config_flow.py`
at this repo's pinned HA version (2026.8.2) - not the simple
`<domain>/{list,create,update,delete}` WebSocket commands the nine
`helper_manager.py` domains use.

Their config/options flows aren't all single-step either: `statistics`
is a fixed three-step flow ("user" -> "state_characteristic" ->
"options"), and `filter` branches into a different type-specific step
("lowpass"/"outlier"/"range"/...) depending on which filter type is
picked in its "user"/"init" step. So rather than hardcoding each
domain's schema here, this module drives the real flow machinery
generically: it steps `async_configure` in a loop, handing each step
the caller-supplied input for that step's id, and raises
`FlowStepRequiredError` (carrying the step's own schema, serialized the
same way `homeassistant.helpers.data_entry_flow`'s HTTP view does -
`voluptuous_serialize.convert(..., custom_serializer=cv.custom_serializer)`)
when the caller hasn't supplied input for the step the flow is
currently on. A caller (the LLM) can therefore discover each step's
fields by calling with no/partial `steps` first, then retry with them
filled in - the same one-step-at-a-time shape a human fills out the
real "Add Helper" wizard with.

Template is deliberately out of scope here - its `config_flow.py` alone
is ~900 lines covering many different entity platforms (sensor,
binary_sensor, number, select, switch, button, image, ...), enough to
warrant its own dedicated module. See issue #13.

Every one of these seven domains sets `options_flow_reloads = True` on
its `ConfigFlowHandler` (confirmed the same way) - update_derived_sensor
does not need to separately call `async_reload` after a successful
options flow, `OptionsFlowManager.async_finish_flow` already schedules
one. `reload_derived_sensor` still exists as its own tool for the
separate case of wanting to force a recompute without changing any
options (e.g. after an entity it reads from was reconfigured elsewhere).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import voluptuous_serialize
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv

__all__ = [
    "DERIVED_SENSOR_DOMAINS",
    "DerivedSensorNotFoundError",
    "FlowAbortedError",
    "FlowStepRequiredError",
    "InvalidDerivedSensorDomainError",
    "create_derived_sensor",
    "delete_derived_sensor",
    "get_derived_sensor",
    "list_derived_sensors",
    "reload_derived_sensor",
    "update_derived_sensor",
]

DERIVED_SENSOR_DOMAINS = (
    "min_max",
    "utility_meter",
    "integration",  # "Integration - Riemann sum" in the UI
    "statistics",
    "threshold",
    "derivative",
    "filter",
)


class InvalidDerivedSensorDomainError(Exception):
    """Raised for a domain that isn't one of the known derived-sensor domains."""


class DerivedSensorNotFoundError(Exception):
    """Raised when a config entry id doesn't resolve to a known derived-sensor entry."""


class FlowAbortedError(Exception):
    """Raised when HA's own config/options flow refuses the given input (an ABORT result)."""


class FlowStepRequiredError(Exception):
    """Raised when the flow is on a step the caller didn't supply input for.

    `schema` is that step's fields, serialized the same way HA's own
    config_entries HTTP view does - safe to hand back to an LLM caller as
    the description of what to fill in and retry with, keyed by
    `step_id` in the next call's `steps` dict.
    """

    def __init__(
        self,
        step_id: str,
        schema: list[dict[str, Any]],
        errors: dict[str, str] | None,
    ) -> None:
        self.step_id = step_id
        self.schema = schema
        self.errors = errors or {}
        super().__init__(
            f"Flow step '{step_id}' needs input - see this error's 'schema' for "
            "its fields, then retry with steps={{'" + step_id + "': {{...}}}}"
        )


def _check_domain(domain: str) -> None:
    if domain not in DERIVED_SENSOR_DOMAINS:
        raise InvalidDerivedSensorDomainError(
            f"'{domain}' is not a derived-sensor domain; must be one of "
            f"{DERIVED_SENSOR_DOMAINS}"
        )


def _get_entry(hass: HomeAssistant, entry_id: str) -> ConfigEntry:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain not in DERIVED_SENSOR_DOMAINS:
        raise DerivedSensorNotFoundError(
            f"No derived-sensor config entry with id '{entry_id}' found"
        )
    return entry


def _serialize_schema(schema: Any) -> list[dict[str, Any]]:
    if schema is None:
        return []
    return voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)


def _entry_to_dict(entry: ConfigEntry) -> dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "domain": entry.domain,
        "title": entry.title,
        "state": entry.state.value,
        "data": dict(entry.data),
        "options": dict(entry.options),
        "disabled_by": entry.disabled_by,
    }


async def _drive_flow(
    *,
    init_result: dict[str, Any],
    configure: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    steps: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Step a config/options flow to completion using caller-supplied per-step input.

    Generic across single-step (min_max, utility_meter, integration,
    threshold, derivative), fixed multi-step (statistics), and branching
    (filter) flows alike - it only ever looks at the current step's id,
    never assumes a fixed sequence.
    """
    result = init_result
    while result["type"] == FlowResultType.FORM:
        step_id = result["step_id"]
        if step_id not in steps:
            raise FlowStepRequiredError(
                step_id,
                _serialize_schema(result.get("data_schema")),
                result.get("errors"),
            )
        result = await configure(result["flow_id"], steps[step_id])
    if result["type"] == FlowResultType.ABORT:
        raise FlowAbortedError(f"Flow aborted: {result.get('reason', 'unknown')}")
    if result["type"] != FlowResultType.CREATE_ENTRY:
        raise FlowAbortedError(f"Unsupported flow result type: {result['type']}")
    return result


def list_derived_sensors(
    hass: HomeAssistant, domain: str | None = None
) -> list[dict[str, Any]]:
    """List every derived-sensor config entry, optionally scoped to one domain."""
    if domain is not None:
        _check_domain(domain)
        domains = (domain,)
    else:
        domains = DERIVED_SENSOR_DOMAINS
    entries = [
        entry
        for scan_domain in domains
        for entry in hass.config_entries.async_entries(scan_domain)
    ]
    return [_entry_to_dict(entry) for entry in entries]


def get_derived_sensor(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Return one derived-sensor config entry's current config by id."""
    return _entry_to_dict(_get_entry(hass, entry_id))


async def create_derived_sensor(
    hass: HomeAssistant, domain: str, steps: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Create a new derived-sensor config entry by driving its real config flow.

    `steps` is keyed by step id (e.g. `{"user": {...}}`). Start with an
    empty or partial dict to discover the current step's required fields
    via the raised FlowStepRequiredError, then retry with it filled in -
    repeat for however many steps that domain's flow actually has.
    """
    _check_domain(domain)
    init_result = await hass.config_entries.flow.async_init(
        domain, context={"source": "user"}
    )
    result = await _drive_flow(
        init_result=init_result,
        configure=hass.config_entries.flow.async_configure,
        steps=steps or {},
    )
    return _entry_to_dict(result["result"])


async def update_derived_sensor(
    hass: HomeAssistant, entry_id: str, steps: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Update an existing derived-sensor entry by driving its real options flow.

    Same per-step discovery shape as create_derived_sensor. A successful
    finish updates the entry's options and reloads it automatically (see
    module docstring) - no separate reload call needed here.
    """
    entry = _get_entry(hass, entry_id)
    init_result = await hass.config_entries.options.async_init(entry.entry_id)
    await _drive_flow(
        init_result=init_result,
        configure=hass.config_entries.options.async_configure,
        steps=steps or {},
    )
    # Re-fetch rather than trust the flow result's own data: async_finish_flow
    # applies the new options to the entry object itself (see module
    # docstring), so the entry we already hold is already up to date, but
    # re-fetching keeps this honest about what's actually stored rather than
    # assuming the flow's returned shape matches the entry's.
    return _entry_to_dict(_get_entry(hass, entry_id))


async def delete_derived_sensor(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Delete a derived-sensor config entry by id."""
    _get_entry(hass, entry_id)  # validate scope + existence before removing
    result = await hass.config_entries.async_remove(entry_id)
    return {"deleted": True, "entry_id": entry_id, **result}


async def reload_derived_sensor(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Force a derived-sensor entry to reload without changing its options."""
    _get_entry(hass, entry_id)  # validate scope + existence before reloading
    reloaded = await hass.config_entries.async_reload(entry_id)
    return {"reloaded": reloaded, "entry_id": entry_id}
