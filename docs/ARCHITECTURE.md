# Architecture

## No separate MCP server

Home Assistant ships a native `mcp_server` integration (core, since
2026.3, first released 2026.3): a pure protocol adapter with no tool logic
of its own. It hosts MCP transport (Streamable HTTP and legacy SSE) and
serves whatever is registered in `homeassistant.helpers.llm`'s `API`/`Tool`
registry - the same extension point Home Assistant's own `conversation`
component uses for Assist. Any custom integration can register into that
registry via `llm.async_register_api()`.

`ha_dev_tools` does exactly that: it defines `llm.Tool` subclasses and
registers a `dev_tools` `llm.API` in `async_setup_entry`. It never opens a
socket, never implements HTTP, never handles auth of its own beyond the
gates described in [SECURITY.md](SECURITY.md). `mcp_server` does all of
that, for free, because it doesn't know or care whether the tools it's
serving came from Assist or from a custom integration.

Consequence: this integration cannot function on a Home Assistant version
before `mcp_server` shipped (2026.3+; this project targets 2026.8.2+, the
version whose `Requires-Python` floor - 3.14.2 - happens to also be what CI
runs against). There is no fallback transport for older installs, by
design - building one would mean re-implementing exactly the thing
`mcp_server` already does correctly.

A sharper consequence: `async_setup_entry` succeeding tells you nothing
about whether `dev_tools` is actually reachable from outside Home
Assistant - that depends entirely on `mcp_server` being installed *and*
configured with `dev_tools` in its exposed APIs, neither of which this
integration controls or can detect at setup time (it may not even exist
yet - `mcp_server` is meant to be added *after* this integration, per
README's Setup section). `mcp_repair.py` closes that visibility gap with a
Repairs issue rather than a config-flow check: it inspects `mcp_server`'s
config entries directly (`hass.config_entries.async_entries("mcp_server")`,
checking each loaded entry's `data[CONF_LLM_HASS_API]`) and reacts to
`SIGNAL_CONFIG_ENTRY_CHANGED` so the issue appears/clears live as
`mcp_server` is added, removed, or reconfigured.

## File access vs. Home Assistant's own APIs

Home Assistant's registries, WebSocket API, and REST endpoints are the
right tool for most things - live, race-free, already validated. Raw file
access is reserved for the places nothing else works, determined by reading
the actual source rather than assumed:

| Need | Use | Why |
|---|---|---|
| Entity/device/area hygiene | WS `config/entity_registry/list_for_display` etc. | Live, richer than `.storage/*.json`, no torn-read risk |
| Registry writes | Registry update services/WS commands only | `.storage/*.json` writes while HA is running get silently clobbered on HA's next debounced save - no cross-process locking |
| "Is this entity actually broken" | Runtime state machine (`get_states`) | Registries have no availability field at all - it's a purely runtime concept |
| Automation/script CRUD, default layout | `config/automation/config/{id}` REST | Auto-triggers `automation.reload` scoped to that automation |
| Automation/script CRUD, packages/custom layout | Direct YAML file read/write + explicit reload | The REST API hard-codes `automations.yaml`/`scripts.yaml` - packaged configs are invisible to it (see below) |
| Recent warnings, quick glance | `system_log/list` WS command | Cheap, but capped ~50 entries, WARNING+ only, in-memory |
| Real log forensics | Raw `home-assistant.log` | No API substitute exists anywhere in core - `logbook` is recorder/DB-backed, `logger` is live-level-only, `system_log` is lossy |
| "Did entity X change state, and when" | `recorder.history.get_significant_states` (`get_entity_history`) | Same call the History page's own websocket API makes; bounded by the recorder's own retention (`purge_keep_days`, 10 days by default) - not a substitute for the log file, but the log file isn't a substitute for it either (a state change isn't a log line) |
| "What fired, and what caused it" | `logbook.processor.EventProcessor` (`get_logbook`) | Same class the Logbook page and `logbook/get_events` WS command use - entries come back already humanized and, where HA recorded it, with the triggering context, rather than raw `state_changed` events this integration would otherwise have to re-derive meaning from. Same recorder-retention bound as above |
| Add-on/Supervisor logs | Supervisor API | Separate subsystem, HA OS/Supervised only |
| Dashboards, storage mode | WS `lovelace/config`, `lovelace/config/save` | Fully sufficient |
| Dashboards, YAML mode | Raw `ui-lovelace.yaml` | `lovelace/config/save` explicitly raises "Not supported" for YAML-mode dashboards - not implemented here, read-only for that case |
| Helpers (`input_boolean` et al., `counter`, `timer`, `schedule`) | WS `<domain>/{list,create,update,delete}` | Fully API-sufficient, generic across all nine domains |
| Derived-sensor helpers (Min/Max, Utility Meter, Integration/Riemann sum, Statistics, Threshold, Derivative, Filter), and Template | `hass.config_entries.flow`/`.options` (real config/options flow) | These are config-entry integrations (`SchemaConfigFlowHandler`), not the flat storage-collection pattern above - no single generic WS command covers them. See "Driving config/options flows generically" below |
| YAML `template:` entities, default file | Direct YAML file read/write (`configuration.yaml`) + `template.reload` | No REST/WS API scoped to individual template entities exists; unlike automations, `configuration.yaml` is this integration's own read-only default, so only reads land here |
| YAML `template:` entities, packages layout | Direct YAML file read/write (`packages/*.yaml`) + `template.reload` | Same file-layout-resolution problem `write_automation` solves, applied to `template:` blocks - see "Layout-aware YAML template: entities" below |
| Blueprints | WS `blueprint/{list,import,save,delete,substitute}` | Has a real API - not yet implemented here (`get_automation` currently returns `use_blueprint:` references unexpanded) |

## Package provenance: the automation-write safety rule

Home Assistant's package merge (`merge_packages_config` in `config.py`)
splices a package's `automation:`/`script:` lists into the same in-memory
list that `automations.yaml`/`scripts.yaml` populate - then discards all
provenance. Nothing in the merged runtime config records that a given
automation came from `packages/foo.yaml` rather than the default file.
There's no hard error on duplicate IDs across packages either.

This makes the REST config API actively unsafe for anything package-defined,
not just blind to it:

- `GET` on a package-defined automation's ID → 404, even though it's live.
- `POST` (an "edit") → finds no match in `automations.yaml`, so it
  **appends a new, diverging duplicate there** and reloads. Two automations
  now share an ID, one real, one stray, both live.
- `DELETE` → looks only in `automations.yaml`, finds nothing, reports
  success - while the real automation keeps running untouched.

`write_automation` avoids this by resolving which file actually defines a
given ID - scanning the default file *and* every package file - before
writing anything. It refuses to guess when an ID is duplicated across
files. Any ID confirmed to live in the default file goes through the normal
REST path; anything package-defined goes through direct YAML editing of
that specific file, followed by an explicit `automation.reload`. This is
also why `audit_automations` flags duplicate IDs across packages as its own
finding - Home Assistant itself won't stop you from creating one.

## The WebSocket loopback pattern (helpers, dashboards)

Some state - storage-backed helpers (`input_boolean`, `counter`, `timer`,
...) and dashboards - is reachable only through the WebSocket API. The
component that owns each one keeps its `StorageCollection` as a private
variable inside its own `async_setup`, never exposed via `hass.data`.

Rather than open a real network connection to itself, `ws_call.py`
constructs a real `homeassistant.components.websocket_api.ActiveConnection`
with a fake `send_message` callback that captures the result into a
`Future`, then calls the real `connection.async_handle(msg)` - reusing
100% of Home Assistant's actual command dispatch, schema validation, and
admin enforcement, faking only the transport. `resolve_user()` resolves the
real calling user from the MCP request's `LLMContext`, never a synthetic
admin bypass, so admin-gated WS commands enforce against the real caller.

## Driving config/options flows generically (derived-sensor helpers, and Template)

Min/Max, Utility Meter, Integration (Riemann sum), Statistics, Threshold,
Derivative, Filter, and Template are each a real config-entry integration
built on `homeassistant.helpers.schema_config_entry_flow.
SchemaConfigFlowHandler` - confirmed by reading every one of these
domains' own `config_flow.py` at this repo's pinned HA version. "Derived
sensor" is a slight misnomer for Template specifically - unlike the other
seven, it can create an entity in almost any domain (light, switch, cover,
fan, lock, ...), not just calculated sensors - kept in
`derived_sensor_manager.py` anyway since it needs none of its own
machinery (see below), rather than a separate module.

Their flows aren't uniformly single-step either: `statistics` is a fixed
three-step sequence (`user` -> `state_characteristic` -> `options`);
`filter` branches into a different step (`lowpass`/`outlier`/`range`/...)
depending on which filter type is chosen in its first step; `template`'s
very first step is a real `FlowResultType.MENU` (pick an entity domain -
sensor, switch, light, ...) rather than a form - the same shape a human
fills out the real "Add Helper"/"Configure" wizard with, one step at a
time.

`derived_sensor_manager.py` drives this generically rather than hardcoding
each domain's schema: it calls `hass.config_entries.flow.async_init`/
`async_configure` (config flow, for create) or `.options.async_init`/
`async_configure` (options flow, for update) in a loop, looking only at
whatever step id the flow is currently on. When the caller hasn't supplied
input for that step, it raises `FlowStepRequiredError` carrying the step's
own schema - serialized the same way HA's own `config_entries` HTTP view
does (`voluptuous_serialize.convert(schema, custom_serializer=
cv.custom_serializer)`) - so a caller (the LLM) can discover each step's
fields and retry, accumulating a `steps` dict keyed by step id until the
flow finishes. A `MENU` result needs no special-casing beyond accepting
that result type into the same loop as `FORM` - HA represents the choice
as a `next_step_id` field in the menu's own `data_schema` (confirmed by
reading `data_entry_flow.py`'s `async_show_menu`), discovered and supplied
exactly like any other step's fields. This generalizes correctly across
single-step, fixed multi-step, and branching (selector- or menu-driven)
flows alike without this integration needing to know any domain's fields
in advance - see `tests/test_derived_sensor_manager.py` for all eight
confirmed working this way (seven directly; `filter` needs a real recorder
as a dependency, so it's `tests/test_derived_sensor_manager_recorder.py`
instead).

Two failure modes needed explicit handling, both found by actually
exercising them rather than assumed away:

- **A step's `validate_user_input` rejects what was supplied.** HA
  re-shows the identical step_id with `errors` set, rather than aborting -
  confirmed directly that the unguarded first version of `_drive_flow`
  actually hangs, resubmitting the same rejected input forever, not just a
  theoretical concern (had to be force-killed past two minutes on
  `statistics`'s validated `options` step). A second visit to an
  already-attempted step_id now raises `FlowStepRequiredError` with the
  fresh error instead of looping.
- **Raw schema/type mismatches** (a bad `MENU` `next_step_id`, or - as
  Template's own sensor validator does for a device_class/unit-of-measurement
  mismatch - a validator raising plain `vol.Invalid` instead of
  `SchemaFlowError`, which HA's `_async_form_step` only catches the latter
  of) surface as `homeassistant.data_entry_flow.InvalidData`/`vol.Invalid`
  raised directly out of `async_configure`, never as a re-shown form.
  `_drive_flow` catches `voluptuous.Invalid` around each `configure()` call
  and re-raises as `FlowAbortedError` instead of letting an HA-internal
  exception type escape this module.

All eight of these domains set `options_flow_reloads = True` on their
`ConfigFlowHandler` (also confirmed by reading each one) - Home Assistant's
own `OptionsFlowManager.async_finish_flow` already applies the new options
to the entry and schedules a reload on a successful finish, so
`update_derived_sensor` doesn't need to separately call
`config_entries.async_reload` after a successful update. For Template
specifically, the options flow's own first step ("init") has no schema of
its own - it reads the entry's already-stored `template_type` and skips
straight to that domain's options step, so updating an entity never needs
to redrive the menu the way creating one does.

## Layout-aware YAML `template:` entities (`template_yaml_manager.py`)

Issue #13's other ask alongside the derived-sensor helpers: entities
defined via the modern, trigger-based `template:` YAML syntax (not the
deprecated `sensor: - platform: template` legacy form, and not the
config-entry Template *helper* above either). Reuses
`automation_manager.py`'s provenance-resolution pattern - scan every
candidate file, resolve exactly one location, refuse to guess on
ambiguity - with one structural difference: `template:` entries have no
default include-file convention the way `automation: !include
automations.yaml` does, so `configuration.yaml` itself is always a read
candidate, not just `packages/*.yaml`.

Reads and writes are scoped differently on purpose. `configuration.yaml`
is in `DEFAULT_READ_ONLY_PATHS`, not `DEFAULT_WRITE_PATHS` - so an entity
defined directly in `configuration.yaml` can be listed/read same as any
other, but `create_entity` always targets an existing `packages/*.yaml`
file (`package` is a required argument, no default-file fallback the way
`write_automation` has one), and `update_entity`/`delete_entity` on an
entity that resolves to `configuration.yaml` fails with `FileManager`'s
own `PermissionError` - the correct, intended outcome. This split only
actually holds because `FileManager.write_file`/`delete_file` correctly
enforce `OPERATION_WRITE` - building this module is what surfaced that
they previously didn't (see CHANGELOG's `2.3.1` entry).

Individual template entities have no HA-tracked identity the way an
automation's `id:` does unless they set their own `unique_id:` -
`create_entity`/`update_entity`/`delete_entity` all require one (`config`
must include it for create; update/delete take it as the lookup key).
Entities without a `unique_id` still show up in `list_entities`/`get_entity`
reads, just aren't addressable for writes - the same "refuse to guess"
philosophy as `DuplicateAutomationIdError`, applied to "which entity"
instead of "which file" (`DuplicateTemplateUniqueIdError` is its
counterpart here). `create_entity` always creates a brand new `template:`
block for the entity being created rather than trying to merge into an
existing block that might share triggers/conditions/variables with
unrelated entities - simpler and unambiguous, at some cost to YAML
compactness versus a human hand-authoring several entities under one
shared trigger block.

Custom HA YAML tags (`!secret`, `!include`, ...) can legitimately appear
inside a template entity's own config (e.g. an availability template built
from a `!secret` value) - `ruamel.yaml`'s round-trip loader parses these
into `TaggedScalar` objects rather than erroring, confirmed directly
against a realistic `configuration.yaml` fixture containing
`automation: !include automations.yaml` alongside a `template:` block.
`_to_plain()` converts any such tag into a plain, JSON-safe string (its
tag name plus literal argument, e.g. `"!secret my_secret_template"`) for
list/get responses - it never resolves what the tag actually points to,
appropriate for a read path that shouldn't leak `secrets.yaml` contents.

## What was deliberately not built

- **No custom transport, session, or auth code.** `mcp_server` provides
  all of it.
- **No REST API surface for tools.** An earlier version of this project
  exposed `/api/management/*` REST endpoints for a separate standalone MCP
  process to call. That process no longer exists; tools call the backing
  managers (`FileManager`, `LogManager`, `AutomationManager`, ...) directly,
  in-process.
- **No MCP tool annotations** (`readOnlyHint`, `destructiveHint`, etc.) -
  `mcp_server/server.py`'s `_format_tool()` builds `types.Tool` from an
  `llm.Tool` and never sets `annotations`, and `llm.Tool` itself has no
  fields to source them from. Tool naming and description carry that intent
  instead.
- **No token scoping beyond the two gates in [SECURITY.md](SECURITY.md).**
  Home Assistant's auth model doesn't support it (see that document for
  why this matters more here than in a typical integration).

## Still open

- Blueprint resolution (`blueprint/substitute`) for `get_automation` and
  `audit_automations` - an automation using `use_blueprint:` currently
  returns the unexpanded reference, not its actual trigger/condition/action
  logic.
- `bulk_update_entities` - a dry-run-by-default bulk entity patch tool for
  mass-migration cases, not started.
- Overlapping-trigger race detection and unhandled `rest_command`/
  `shell_command` failure detection in `audit_automations` - real checks,
  deliberately deferred pending more careful false-positive analysis rather
  than shipped unreliable.
