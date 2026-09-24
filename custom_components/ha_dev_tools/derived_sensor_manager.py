"""CRUD for config-entry-based "helpers" - calculated sensors, and Template.

Home Assistant exposes a second family of "helpers" alongside the flat
storage-collection domains `helper_manager.py` covers (`input_boolean`,
`counter`, `timer`, ...): Min/Max, Utility Meter, Integration (Riemann
sum, domain `integration`), Statistics, Threshold, Derivative, Filter,
and Template. Each of these is a full config-entry integration driven by
`homeassistant.helpers.schema_config_entry_flow.SchemaConfigFlowHandler`
- confirmed by reading every one of these domains' own `config_flow.py`
at this repo's pinned HA version (2026.8.2) - not the simple
`<domain>/{list,create,update,delete}` WebSocket commands the nine
`helper_manager.py` domains use. "Derived sensor" is a slight misnomer
for Template specifically - unlike the other seven, it can create
entities across many domains (light, switch, cover, fan, lock, ...), not
just calculated sensors - kept in this module anyway rather than a
separate one because it needs none of its own machinery, see below.

Their config/options flows aren't all single-step either: `statistics`
is a fixed three-step flow ("user" -> "state_characteristic" ->
"options"); `filter` branches into a different type-specific step
("lowpass"/"outlier"/"range"/...) depending on which filter type is
picked in its "user" step; `template`'s very first step is a real MENU
(pick an entity platform - sensor, switch, light, ...) rather than a
form, branching the same way filter does but through
`FlowResultType.MENU` instead of a selector field. So rather than
hardcoding each domain's schema here, this module drives the real flow
machinery generically: it steps `async_configure` in a loop, handing
each step the caller-supplied input for that step's id, and raises
`FlowStepRequiredError` (carrying the step's own schema, serialized the
same way `homeassistant.helpers.data_entry_flow`'s HTTP view does -
`voluptuous_serialize.convert(..., custom_serializer=cv.custom_serializer)`)
when the caller hasn't supplied input for the step the flow is
currently on. A caller (the LLM) can therefore discover each step's
fields by calling with no/partial `steps` first, then retry with them
filled in - the same one-step-at-a-time shape a human fills out the
real "Add Helper" wizard with. A MENU step needs no special handling
beyond accepting that result type into the same loop - HA represents the
choice as a `next_step_id` field in the menu's own `data_schema`
(confirmed by reading `data_entry_flow.py`'s `async_show_menu`), so it's
discovered and supplied exactly like any other step's fields.

`_drive_flow` also guards against a step that comes back around because
its own `validate_user_input` rejected what was supplied (HA re-shows
the identical step_id with `errors` set, rather than aborting) -
confirmed directly that the unguarded version of this loop actually
hangs on repeated invalid input, not just a theoretical concern. A
second visit to an already-attempted step_id raises FlowStepRequiredError
with the fresh error instead of resubmitting the same rejected input
forever.

Every one of these eight domains sets `options_flow_reloads = True` on
its `ConfigFlowHandler` (confirmed the same way) - update_derived_sensor
does not need to separately call `async_reload` after a successful
options flow, `OptionsFlowManager.async_finish_flow` already schedules
one. `reload_derived_sensor` still exists as its own tool for the
separate case of wanting to force a recompute without changing any
options (e.g. after an entity it reads from was reconfigured elsewhere).

Every FORM step is pre-filled with that step's current values before the
caller's own input is laid over it (issue #81). HA's
`SchemaCommonFlowHandler` deletes every optional key the submitted input
omits (`_update_and_remove_omitted_optional_keys` - that's how the UI
clears a field), and the real frontend never trips over this because it
renders each form pre-populated with the entry's current values (the
`suggested_value` HA attaches to each schema key) and submits all of
them back. Submitting only the caller's fields, as this module used to,
silently wiped every unrestated optional field instead - `device_id` on a
template sensor, in the report. The step's own schema is the source of
those current values, never the entry's raw options, so keys the step
doesn't accept (template's `name`, utility_meter's create-only `cycle`)
are never injected. A caller clears an optional field explicitly by
passing it as None.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

import voluptuous_serialize
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, section
from homeassistant.helpers import config_validation as cv
from voluptuous import Invalid as VoluptuousInvalid
from voluptuous import Marker

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
    "template",  # "Template" in the UI - creates entities across many domains, not just sensors
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


# HA 2026.9 moved cv.custom_serializer onto probatio: it now returns
# probatio's own UNSUPPORTED sentinel for anything it doesn't handle, which
# voluptuous_serialize.convert doesn't recognize as "defer" - it hands that
# sentinel back as the serialized schema itself, which then crashes the
# MCP response with "Object of type _Unsupported is not JSON serializable"
# (issues #80, #81). Use whichever field-list converter cv itself imports
# (`to_field_list` from probatio on 2026.9+, the same function HA's own
# data_entry_flow HTTP view switched to) so the sentinel always matches.
_to_field_list = getattr(cv, "to_field_list", voluptuous_serialize.convert)


def _serialize_schema(schema: Any) -> list[dict[str, Any]]:
    if schema is None:
        return []
    return cast(
        list[dict[str, Any]],
        _to_field_list(schema, custom_serializer=cv.custom_serializer),
    )


def _schema_field_names(schema: Any) -> list[str]:
    """Top-level field names a step's schema accepts (a section counts as one)."""
    if schema is None:
        return []
    return [
        str(key.schema if isinstance(key, Marker) else key) for key in schema.schema
    ]


def _current_step_values(schema: Any) -> dict[str, Any]:
    """The values HA pre-populates a step's form with (see module docstring).

    Read from each key's `suggested_value`, recursing into sections, since
    that's exactly what the frontend renders and submits back unchanged
    for any field the user doesn't touch.
    """
    values: dict[str, Any] = {}
    if schema is None:
        return values
    for key, value in schema.schema.items():
        if not isinstance(key, Marker):
            continue
        if isinstance(value, section):
            if nested := _current_step_values(value.schema):
                values[key.schema] = nested
            continue
        description = key.description
        if isinstance(description, Mapping) and "suggested_value" in description:
            values[key.schema] = description["suggested_value"]
    return values


def _merge_step_input(
    current: dict[str, Any], supplied: Mapping[str, Any]
) -> dict[str, Any]:
    """Lay caller input over a step's current values; None clears a field."""
    merged = dict(current)
    for key, value in supplied.items():
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_step_input(merged[key], value)
        else:
            merged[key] = value
    return merged


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
    init_result: ConfigFlowResult,
    configure: Callable[[str, dict[str, Any]], Awaitable[ConfigFlowResult]],
    steps: dict[str, dict[str, Any]],
    options: dict[str, Any] | None = None,
) -> ConfigFlowResult:
    """Step a config/options flow to completion using caller-supplied per-step input.

    Generic across single-step (min_max, utility_meter, integration,
    threshold, derivative), fixed multi-step (statistics), branching
    (filter, template) - a MENU result is driven the same way as a FORM
    one: HA represents the choice as a `next_step_id` field in the menu's
    own `data_schema` (confirmed by reading data_entry_flow.py's
    `async_show_menu` - `vol.Schema({"next_step_id": vol.In(menu_options)})`),
    so it needs no special-casing beyond accepting FlowResultType.MENU
    into the same loop - and top-level-menu (template) flows alike. It
    only ever looks at the current step's id, never assumes a fixed
    sequence.

    Each FORM step's input is its current values (see module docstring)
    with the caller's input laid over them. `options` is the step-id-free
    alternative to `steps` (issue #82): one flat patch, applied to
    whichever FORM step the flow is on without needing that step's input
    under `steps`. Every patch key must be a field of the step it's
    applied to - checked before that step is submitted, since submitting
    may be what finishes the flow and writes the entry. All eight
    domains' options flows present exactly one FORM step (template's and
    filter's `init` have no schema and skip straight to the type-specific
    one), so in practice a patch key that isn't there is one HA only lets
    you set when creating the helper.

    A step already attempted that comes back around (its own
    `validate_user_input` rejected what `steps` supplied, so HA re-shows
    the identical step_id with `errors` populated) raises
    FlowStepRequiredError with that fresh error rather than resubmitting
    the same rejected input forever - confirmed directly that the
    unguarded version of this loop actually hangs (a real HA flow keeps
    re-showing the same step indefinitely on repeated invalid input, it
    doesn't abort), not just a theoretical concern.
    """
    result = init_result
    attempted_steps: set[str] = set()
    while result["type"] in (FlowResultType.FORM, FlowResultType.MENU):
        step_id = result["step_id"]
        data_schema = result.get("data_schema")
        is_form = result["type"] == FlowResultType.FORM
        use_options = options is not None and is_form
        if (not use_options and step_id not in steps) or step_id in attempted_steps:
            raise FlowStepRequiredError(
                step_id,
                _serialize_schema(data_schema),
                result.get("errors"),
            )
        attempted_steps.add(step_id)
        fields = _schema_field_names(data_schema)
        if use_options and options is not None:
            if unknown := sorted(set(options) - set(fields)):
                raise FlowAbortedError(
                    f"Not editable on step '{step_id}': {unknown} - usually "
                    "a field Home Assistant only lets you set when creating "
                    f"this helper. Editable fields: {fields}"
                )
            supplied: Mapping[str, Any] = options
        else:
            supplied = steps[step_id]
        if not isinstance(supplied, Mapping):
            raise FlowAbortedError(
                f"Input for step '{step_id}' must be an object of field "
                f"values, got {type(supplied).__name__}. Fields this step "
                f"accepts: {fields}"
            )
        user_input = (
            _merge_step_input(_current_step_values(data_schema), supplied)
            if is_form
            else dict(supplied)
        )
        try:
            result = await configure(result["flow_id"], user_input)
        except VoluptuousInvalid as exc:
            # A raw type/value mismatch (wrong type for a field, an
            # out-of-range MENU next_step_id, a field this step doesn't
            # have, ...) is rejected before HA ever dispatches to the step
            # handler - data_entry_flow.py's _async_configure raises
            # InvalidData (a vol.Invalid subclass) directly rather than
            # re-showing the form with `errors` set, unlike a
            # validate_user_input rejection. Translate it the same way as
            # any other malformed-input case instead of letting an
            # HA-internal exception type leak out of this module - and
            # name every field the step accepts, so a rejected unknown
            # field isn't a one-at-a-time guessing game (issue #80).
            raise FlowAbortedError(
                f"Invalid input for step '{step_id}': {exc}. Fields this "
                f"step accepts: {fields}"
            ) from exc
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
        domains: tuple[str, ...] = (domain,)
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
    hass: HomeAssistant,
    entry_id: str,
    steps: dict[str, dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update an existing derived-sensor entry by driving its real options flow.

    Either `steps` (same per-step discovery shape as create_derived_sensor)
    or `options` (a flat patch of just the fields to change, no step id
    needed - see _drive_flow), not both. Fields left out of either keep
    their current values. A successful finish updates the entry's options
    and reloads it automatically (see module docstring) - no separate
    reload call needed here.
    """
    if steps and options is not None:
        raise FlowAbortedError("Pass either 'steps' or 'options', not both")
    entry = _get_entry(hass, entry_id)
    init_result = await hass.config_entries.options.async_init(entry.entry_id)
    await _drive_flow(
        init_result=init_result,
        configure=hass.config_entries.options.async_configure,
        steps=steps or {},
        options=options,
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
