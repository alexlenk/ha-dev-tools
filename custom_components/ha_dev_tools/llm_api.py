"""LLM API for the Home Assistant Development Tools integration.

Registers a `dev_tools` API into Home Assistant's LLM tool registry
(`homeassistant.helpers.llm`). Home Assistant's own native `mcp_server`
integration exposes whatever is registered here over MCP (Streamable HTTP)
with no custom transport or auth code required on our side - see
docs/ARCHITECTURE.md for the full architecture, README.md's Tools table
for what each tool below implements and why, and docs/SECURITY.md for
why every tool but the diagnostic ping is gated.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from functools import partial
from typing import Any, cast, override

try:
    # HA 2026.9+ validates with probatio and types its schemas (llm.Tool's
    # parameters, a flow's data_schema) as probatio's - the voluptuous it
    # installs is a shim handing out the same objects (issue #138).
    import probatio as vol
except ImportError:  # pragma: no cover - HA before 2026.9
    import voluptuous as vol  # type: ignore[no-redef]
from homeassistant.auth.models import User
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType, JsonValueType

from . import (
    access_control,
    audit_manager,
    config_snapshot,
    config_tools,
    dashboard_manager,
    derived_sensor_manager,
    energy_manager,
    entity_manager,
    helper_manager,
    history_manager,
    mirror,
    mqtt_manager,
    references,
    registry_manager,
    service_call_manager,
    statistics_backup,
    statistics_derive,
    statistics_manager,
    statistics_merge,
    supervisor_manager,
    template_manager,
    trace_manager,
    write_confirmation,
)
from .automation_manager import (
    AutomationManager,
    AutomationNotFoundError,
    DuplicateAutomationIdError,
)
from .const import DOMAIN
from .dashboard_manager import YamlModeDashboardError
from .derived_sensor_manager import (
    DERIVED_SENSOR_DOMAINS,
    DerivedSensorNotFoundError,
    FlowAbortedError,
    FlowStepRequiredError,
    InvalidDerivedSensorDomainError,
)
from .file_manager import FileManager
from .helper_manager import (
    HELPER_DOMAINS,
    InvalidHelperDomainError,
    UnresolvedUserError,
)
from .history_manager import RecorderNotAvailableError
from .log_manager import LogFilters, LogManager
from .rest_command_manager import (
    COMMAND_KEYS,
    REST_COMMAND_KEY,
    DuplicateRestCommandIdError,
    RestCommandManager,
    RestCommandNotFoundError,
)
from .script_manager import (
    DuplicateScriptIdError,
    ScriptManager,
    ScriptNotFoundError,
)
from .supervisor_manager import SupervisorNotAvailableError
from .template_yaml_manager import (
    BLOCK_FIELDS,
    DuplicateTemplateUniqueIdError,
    TemplateBatchError,
    TemplateEntityNotFoundError,
    TemplateYamlManager,
    validate_triggers,
)
from .ws_call import WebSocketCommandError
from .yaml_style import find_misread_scalars, to_json_safe

API_ID = "dev_tools"
API_NAME = "HA Dev Tools"
API_PROMPT = (
    "Tools for developing and maintaining this Home Assistant instance: "
    "authoring and validating automations, reading logs, and looking up "
    "entities. Prefer these over asking the user to copy/paste YAML or "
    "restart Home Assistant - config changes take effect via reload."
)


def _tool_error(exc: Exception) -> JsonObjectType:
    """Uniform error payload for tool responses - never let a raw exception escape a Tool."""
    return {"error": str(exc), "error_type": type(exc).__name__}


def _mirror_result_payload(result: mirror.MirrorResult) -> JsonObjectType:
    """Turn a mirror.MirrorResult into a JSON-safe payload for a write tool's response.

    Always present when mirroring is enabled, whether or not it actually
    pushed anything - a skip (credential detected, or a push failure) is
    something the agent should show the user, not silently swallow.
    """
    payload: JsonObjectType = {"mirrored": result.mirrored}
    if result.reason is not None:
        payload["reason"] = result.reason
    if result.commits:
        payload["commits"] = list(result.commits)
    if "before" in result.commits:
        # The live file had drifted from its last mirrored copy - changed
        # outside this write (by hand, or a write that never got mirrored).
        # Recorded as its own commit so it isn't mistaken for this write
        # (issue #127).
        payload["drift"] = (
            "The live file had changed since its last mirrored copy; that "
            "change is committed separately as 'live state ... before write'."
        )
    if result.branch is not None:
        payload["branch"] = result.branch
    return payload


async def _run_to_completion(
    hass: HomeAssistant, work: Coroutine[Any, Any, JsonObjectType], name: str
) -> JsonObjectType:
    """Run a write's commit phase - write, reload, mirror - to the end even
    if the MCP request is cancelled (issue #127): a client that times out
    must not leave a half-applied, unmirrored change behind. Errors still
    reach the caller."""
    task: asyncio.Task[JsonObjectType] = hass.async_create_background_task(work, name)
    return await asyncio.shield(task)


async def _mirror_file_write(
    hass: HomeAssistant,
    response: JsonObjectType,
    *,
    file_path: str,
    content_before: str | None,
    content_after: str,
    content_type: str = "yaml",
) -> JsonObjectType:
    """Mirror a write's before/after content into response['mirror'], if
    mirroring is enabled - shared by every WriteGatedTool whose _write()
    resolves to a single file's content, whether that's a real YAML config
    file it wrote itself (write_automation, write_script,
    create/update/delete_template_entity) or a .storage/* file HA core
    wrote on its behalf and saves immediately
    (write_dashboard - see _storage_file_manager/_read_storage_file below).
    Not used for helpers (create/update/delete_helper): HA's
    StorageCollection debounces those writes 10 seconds
    (helpers/collection.py's async_delay_save), so a read right after the
    call would capture stale, pre-write content (issue #43) - those mirror
    via _reconstruct_helper_storage_json() instead, splicing the WS
    command's own known result into the "before" content in memory rather
    than ever reading the file again after the write."""
    if mirror.is_mirror_enabled(hass):
        mirror_result = await mirror.mirror_write(
            hass,
            path=file_path,
            content_before=content_before,
            content_after=content_after,
            content_type=content_type,
        )
        response["mirror"] = _mirror_result_payload(mirror_result)
    return response


def _storage_file_manager(hass: HomeAssistant) -> FileManager:
    """A FileManager for reading .storage/* files around a dashboard write,
    built on the same shared SecurityManager __init__.py already stores in
    hass.data[DOMAIN] - not the FileManager automation_manager.py/
    template_yaml_manager.py hold (those are for their own YAML writes); a
    fresh one here since dashboard_manager.py never touches files at all
    (HA core's own WS handler does, internally)."""
    security_manager = hass.data[DOMAIN]["security_manager"]
    return FileManager(hass, security_manager)


async def _read_storage_file(hass: HomeAssistant, path: str) -> str | None:
    """Read a .storage/* file's raw content for mirroring, or None if it
    doesn't exist yet (e.g. a dashboard that's never been saved before)."""
    try:
        return await _storage_file_manager(hass).read_file(path)
    except FileNotFoundError:
        return None


def _reconstruct_helper_storage_json(
    before_content: str | None,
    *,
    upsert: JsonObjectType | None = None,
    remove_id: str | None = None,
) -> str:
    """Build a helper's .storage/<domain> "after" content in memory, never
    by reading the file again after the write (issue #43).

    HA's generic helper storage collection (helpers/collection.py's
    StorageCollection) debounces its own save 10 seconds
    (_async_schedule_save() -> Store.async_delay_save()), so a read right
    after create/update/delete would almost always capture stale,
    pre-write content - worse than not mirroring at all for something
    meant to be a rollback source. Instead, splice the one known change
    (the WS command's own returned item, for create/update; just the
    deleted id, for delete) directly into the "before" document's own
    items list - reproducing exactly what the debounced save will
    eventually persist, without ever depending on the file's actual
    on-disk state for "after".

    Every helper storage file shares the same {"version", "minor_version",
    "data": {"items": [...]}} shape, and every item's own identifier key
    is "id" (collection.py's CONF_ID) - confirmed directly against
    home-assistant/core source (collection.py's StorageCollection,
    input_boolean/__init__.py's InputBooleanStorageCollection - neither
    overrides the base "id" key), not assumed. Exactly one of
    upsert/remove_id is ever passed.
    """
    document: dict[str, Any] = (
        json.loads(before_content)
        if before_content is not None
        else {"version": 1, "minor_version": 1, "data": {"items": []}}
    )
    items: list[dict[str, Any]] = document.setdefault("data", {}).setdefault(
        "items", []
    )

    if upsert is not None:
        for i, existing in enumerate(items):
            if existing.get("id") == upsert.get("id"):
                items[i] = upsert
                break
        else:
            items.append(upsert)
    else:
        document["data"]["items"] = [
            item for item in items if item.get("id") != remove_id
        ]

    return json.dumps(document)


# Shared by get_automation/get_script/get_template_entity (issue #97).
_MISREAD_VALUES_NOTE = (
    " 'misread_values' lists any unquoted value Home Assistant reads as a "
    "different type than written (e.g. `before: 17:00:00` is read as 61200, "
    "`delay: 1:30` as 90 seconds, `state: off` as False) - 'config' shows "
    "the intended text, but HA acts on what it reads. Writing the item "
    "again quotes such values."
)


def _flow_step_required_payload(exc: FlowStepRequiredError) -> JsonObjectType:
    """Structured (not error) payload for a config/options flow step needing input.

    Deliberately not routed through _tool_error - that would collapse
    exc.step_id/exc.schema into a plain string and lose exactly the
    information a caller needs to retry correctly.
    """
    payload: dict[str, Any] = {
        "needs_input": True,
        "step_id": exc.step_id,
        "schema": exc.schema,
        "errors": exc.errors,
        "note": (
            f"Supply this step's fields under steps['{exc.step_id}'] (merged "
            "with any steps already supplied) and retry. Some domains have "
            "more than one step - this may repeat with a different step_id "
            "until the flow finishes."
        ),
    }
    return cast(JsonObjectType, payload)


def _parse_datetime(value: str, *, field: str) -> Any:
    """Parse an ISO 8601 datetime string, raising ValueError with a clear message on failure."""
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        raise ValueError(f"'{field}' is not a valid ISO 8601 datetime: {value!r}")
    return parsed


def _require(args: dict, field: str) -> Any:
    """Fetch a required field, raising ValueError (not a raw KeyError) if it's missing.

    parameters (the voluptuous schema declaring `vol.Required(field)`) is
    only ever used to build the tool's exposed JSON schema - nothing
    invokes it to actually validate tool_args before _run(), so a caller
    omitting a "required" field previously reached a direct args[field]
    index and raised an unhandled KeyError instead of a clean, caught
    error (see issue #45's get_entity_history/get_logbook crash)."""
    if field not in args:
        raise ValueError(f"'{field}' is required")
    return args[field]


def _write_schema(fields: dict) -> vol.Schema:
    """A write tool's own fields, plus the confirm_token every WriteGatedTool needs.

    voluptuous.Schema rejects unknown keys by default, so confirm_token
    has to be declared explicitly in each write tool's own schema - this
    is the one place that's done, rather than repeating it 11 times.
    """
    return vol.Schema({**fields, vol.Optional("confirm_token"): str})


_CONFIRM_TOKEN_NOTE = (
    " Every call to this tool must happen twice: the first call never "
    "writes anything - it returns a preview of the change plus a "
    "confirm_token; call again with the identical arguments plus "
    "confirm_token set to that exact value to actually apply it. Don't "
    "guess which other field might hold it (e.g. expected_hash is "
    "unrelated) - confirm_token is its own field, and may not always be "
    "visible in this tool's declared schema depending on your MCP client."
)


class GatedTool(llm.Tool):
    """Base for every dev_tools tool except the diagnostic ping.

    Enforces two independent checks before a subclass's real logic
    (_run, not async_call) ever runs - see access_control.py for the
    full reasoning:
    - access_control.check_armed(): a human must have proven real
      filesystem access (SSH, Terminal add-on) recently, outside
      dev_tools' own reach - a leaked HA token alone isn't enough.
    - access_control.require_admin(): the resolved calling user must be
      a real admin, checked here rather than trusted to mcp_server's
      own gate alone (which has a bare-endpoint bypass).
    A successful call extends the idle arm window via touch_armed().
    """

    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Check both gates, run the tool, then extend the idle window."""
        await access_control.check_armed(hass)
        await access_control.require_admin(hass, llm_context)
        result = await self._run(hass, tool_input, llm_context)
        await access_control.touch_armed(hass)
        return result

    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Subclasses implement their actual logic here, not async_call."""
        raise NotImplementedError


class WriteGatedTool(GatedTool):
    """Base for every tool that mutates state (create/update/delete/write).

    Every call is two calls, in both run modes (see
    docs/AUTOMATION_TESTING_DESIGN.md's "Per-write confirmation"):

    1. Propose - called without a valid confirm_token. Nothing runs; the
       call's own arguments come back as a preview plus a short-lived
       token bound to this exact tool + arguments.
    2. Confirm - the same call again, with confirm_token set to what
       propose returned. Only then does this fall through to run-mode
       handling: when this integration's dry-run option is enabled, the
       underlying write still never runs - the same preview comes back
       instead, so the agent can show the user what it was about to do.
       Otherwise the real write happens via _write().

    Neither step is a simulation: propose doesn't verify the write would
    succeed (e.g. path/schema checks), only that it hasn't happened yet.

    Every response also carries `arm`: how long dev_tools stays armed
    after this call extended the idle window (issue #63), so a long batch
    of writes sees expiry coming instead of failing partway through.
    """

    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Run the gated call, then report the arm window it just extended."""
        result = await super().async_call(hass, tool_input, llm_context)
        return {
            **result,
            "arm": cast(JsonValueType, await access_control.arm_status(hass)),
        }

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Require a confirmed token, then short-circuit into dry-run or hand off to _write()."""
        args = tool_input.tool_args
        token = args.get("confirm_token")
        preview = {key: value for key, value in args.items() if key != "confirm_token"}

        if not token or not write_confirmation.consume_pending(
            hass, self.name, args, token
        ):
            new_token = write_confirmation.create_pending(hass, self.name, args)
            minutes = write_confirmation.TOKEN_TTL_SECONDS // 60
            return {
                "confirmation_required": True,
                "action": self.name,
                "would_apply": preview,
                **await self._preview_context(hass, tool_input, llm_context),
                "confirm_token": new_token,
                "note": (
                    "Show the user the full would_apply content above - the "
                    "complete config/definition, not just its id or a "
                    "summary of it (and would_change, where present) - and ask "
                    "them to confirm before calling "
                    "again with the identical arguments plus "
                    f"confirm_token={new_token!r}. Expires in {minutes} "
                    "minutes."
                ),
            }

        if access_control.is_dry_run(hass):
            response: JsonObjectType = {
                "dry_run": True,
                "action": self.name,
                "would_apply": preview,
                **await self._preview_context(hass, tool_input, llm_context),
                "note": (
                    "Dry-run mode is enabled for this integration - no "
                    "changes were made. Show the user the full would_apply "
                    "content above - the complete config/definition, not "
                    "just its id or a summary of it. Dry-run can be turned "
                    "off from this integration's Configure page."
                ),
            }
            if mirror.is_mirror_enabled(hass):
                mirror_result = await self._dry_run_mirror(
                    hass, tool_input, llm_context
                )
                if mirror_result is not None:
                    response["mirror"] = _mirror_result_payload(mirror_result)
            return response
        return await self._write(hass, tool_input, llm_context)

    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Subclasses implement their actual write logic here, not _run."""
        raise NotImplementedError

    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Override to add read-only context to the propose and dry-run
        responses, next to would_apply - e.g. what the target currently
        holds and what would change (issue #86). would_apply alone only
        echoes the caller's own arguments, so for a partial update the user
        confirming it never sees what it actually changes. Must not write
        anything; a failure here should return {} rather than raise - the
        real write reports its own errors."""
        return {}

    async def _dry_run_mirror(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> mirror.MirrorResult | None:
        """Override to push this dry-run call's resolved would-be content to
        a proposed/<kind>-<id> branch (docs/AUTOMATION_TESTING_DESIGN.md's
        "Mirroring" section, dry-run bullet - issue #35), for tools whose
        manager can compute that content without actually writing it.
        Returns None (the default) for tools that can't - write_dashboard
        (no such compute-without-writing path; the real WS command is the
        only way to resolve it) and every non-file-based write tool (helpers,
        derived sensors) - the dry-run preview then just has no 'mirror' key,
        same as before this existed."""
        return None


class DevToolsPingTool(llm.Tool):
    """Confirm the dev_tools API is registered and reachable.

    Not part of the real tool surface (see README.md's Tools table) - kept
    as a zero-dependency smoke test for the llm.API/mcp_server wiring
    itself, and deliberately the one tool NOT behind GatedTool.
    """

    name = "dev_tools_ping"
    description = (
        "Check that the ha_dev_tools API is registered and reachable, and "
        "whether dev_tools is currently armed ('arm': armed, expires_at, "
        "minutes_left, or the command to arm it on the Home Assistant "
        "host). The only tool that works while not armed - call it before "
        "a long batch of writes. No side effects; doesn't extend the arm "
        "window."
    )
    parameters = vol.Schema({})

    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Return a status payload, including the arm window (issue #63)."""
        return {
            "status": "ok",
            "domain": DOMAIN,
            "arm": cast(JsonValueType, await access_control.arm_status(hass)),
        }


class FindEntitiesTool(GatedTool):
    """Area/domain-scoped entity lookup - avoids dumping hundreds of entities."""

    name = "find_entities"
    description = (
        "Find entities scoped by area name, domain, and/or a name substring "
        "search. Always prefer this over dumping every entity - most HA "
        "instances have hundreds of them. Returns live state alongside "
        "registry metadata; set include_disabled to also see disabled "
        "entities (useful for hygiene audits)."
    )
    parameters = vol.Schema(
        {
            vol.Optional("area"): str,
            vol.Optional("domain"): str,
            vol.Optional("name_search"): str,
            vol.Optional("include_disabled", default=False): bool,
            vol.Optional("limit", default=entity_manager.DEFAULT_LIMIT): int,
        }
    )

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Look up entities matching the given filters."""
        return entity_manager.find_entities(hass, **tool_input.tool_args)


class EntityHealthReportTool(GatedTool):
    """Per-integration entity health summary - hundreds of entities, made scannable."""

    name = "entity_health_report"
    description = (
        "Summarize entity health per integration: counts of disabled, "
        "hidden, unavailable, unknown, and 'missing' (registered but no "
        "state at all - usually the owning integration failed to load) "
        "entities, plus a capped sample of the actual problem entities. "
        "Use this instead of find_entities when the goal is 'what's "
        "broken', not looking up a specific entity."
    )
    parameters = vol.Schema(
        {
            vol.Optional("area"): str,
            vol.Optional("integration"): str,
            vol.Optional(
                "limit", default=entity_manager.HEALTH_REPORT_DEFAULT_LIMIT
            ): int,
        }
    )

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Run the health report."""
        return entity_manager.entity_health_report(hass, **tool_input.tool_args)


def _entity_mirror_path(entity_id: str) -> str:
    return f"entities/{entity_id}.json"


class DeleteEntityTool(WriteGatedTool):
    """Soft-delete an entity from the entity registry - see entity_manager.py."""

    name = "delete_entity"
    description = (
        "Remove an entity from the entity registry by entity_id (e.g. a "
        "stale entity left behind by a removed/renamed device or "
        "integration). This is a soft delete on Home Assistant's own "
        "side, not a hard erase: if the same integration re-registers "
        "this entity later, HA reconnects it automatically with its old "
        "entity_id and customizations - no restore needed for that case. "
        "Only entities with no owning config entry (truly orphaned) get "
        "purged for good, and only after 30 days. If mirroring is "
        "enabled, this entity's current registry data (name, area, "
        "labels, options, ...) is pushed to a private backup path first, "
        "so it isn't lost once that 30-day window passes."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema({vol.Required("entity_id"): str})

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Delete the entity from the registry, mirroring a backup first if enabled."""
        entity_id = tool_input.tool_args["entity_id"]
        entity_reg = er.async_get(hass)
        # Only fetched when mirroring is on - same reasoning as
        # DeleteDerivedSensorTool's identical guard.
        before = (
            entity_reg.async_get(entity_id) if mirror.is_mirror_enabled(hass) else None
        )
        try:
            result = entity_manager.delete_entity(hass, entity_id)
        except entity_manager.EntityNotFoundError as exc:
            return _tool_error(exc)
        response: JsonObjectType = dict(result)
        if mirror.is_mirror_enabled(hass):
            mirror_result = await mirror.mirror_write(
                hass,
                path=_entity_mirror_path(entity_id),
                content_before=(
                    json.dumps(entity_manager.entity_registry_snapshot(before))
                    if before is not None
                    else None
                ),
                content_after=json.dumps({"deleted": True, "entity_id": entity_id}),
                content_type="json",
            )
            response["mirror"] = _mirror_result_payload(mirror_result)
        return response


def _entities_batch_mirror_path() -> str:
    return f"entities/batch-{dt_util.utcnow().strftime('%Y%m%dT%H%M%SZ')}.json"


class DeleteEntitiesTool(WriteGatedTool):
    """Batch soft-delete for multiple entities in one confirm - see entity_manager.py."""

    name = "delete_entities"
    description = (
        "Delete multiple entities from the entity registry in a single "
        "propose/confirm pair, for bulk cleanup (e.g. every leftover "
        "entity from a replaced device) - avoids the round-trip cost of "
        "calling delete_entity once per id. Same soft-delete behavior as "
        "delete_entity (see its description) for each entity. Refuses to "
        "delete any of them if even one entity_id in the list doesn't "
        "resolve, rather than guessing which ones you meant - fix the "
        "list and retry. If mirroring is enabled, every entity's "
        "registry data is pushed together as one combined backup commit "
        "(not one per entity), so a large batch doesn't flood the mirror "
        "repo with commits."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {vol.Required("entity_ids"): vol.All([str], vol.Length(min=1))}
    )

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Delete all entities from the registry, mirroring one combined backup first if enabled."""
        entity_ids = tool_input.tool_args["entity_ids"]
        entity_reg = er.async_get(hass)
        # Only fetched when mirroring is on - same reasoning as
        # DeleteEntityTool's identical guard.
        before_entries = (
            {eid: entity_reg.async_get(eid) for eid in entity_ids}
            if mirror.is_mirror_enabled(hass)
            else None
        )
        try:
            result = entity_manager.delete_entities(hass, entity_ids)
        except entity_manager.EntityNotFoundError as exc:
            return _tool_error(exc)
        response: JsonObjectType = dict(result)
        if mirror.is_mirror_enabled(hass) and before_entries is not None:
            before_snapshot = [
                (
                    entity_manager.entity_registry_snapshot(entry)
                    if (entry := before_entries[eid]) is not None
                    else None
                )
                for eid in entity_ids
            ]
            mirror_result = await mirror.mirror_write(
                hass,
                path=_entities_batch_mirror_path(),
                content_before=json.dumps(before_snapshot),
                content_after=json.dumps(
                    [{"deleted": True, "entity_id": eid} for eid in entity_ids]
                ),
                content_type="json",
            )
            response["mirror"] = _mirror_result_payload(mirror_result)
        return response


_UPDATE_ITEM = vol.Schema(
    {
        vol.Optional("entity_id"): str,
        vol.Optional("device_id"): str,
        vol.Optional("new_entity_id"): str,
        vol.Optional("name"): vol.Any(str, None),
        vol.Optional("area"): vol.Any(str, None),
        vol.Optional("device_class"): vol.Any(str, None),
        vol.Optional("icon"): vol.Any(str, None),
        vol.Optional("disabled"): bool,
        vol.Optional("hidden"): bool,
        vol.Optional("show_as"): vol.In(registry_manager.SHOW_AS_DOMAINS),
        vol.Optional("labels"): [str],
        vol.Optional("add_labels"): [str],
        vol.Optional("remove_labels"): [str],
        vol.Optional("categories"): {str: vol.Any(str, None)},
    }
)


def _renames(plans: list[registry_manager.PlannedUpdate]) -> list[tuple[str, str]]:
    return [
        (plan.target, plan.ws_changes["new_entity_id"])
        for plan in plans
        if plan.ws_changes.get("new_entity_id", plan.target) != plan.target
    ]


class UpdateEntitiesTool(WriteGatedTool):
    """Batch entity/device registry changes - see registry_manager.py."""

    name = "update_entities"
    description = (
        "Change entities and devices the way their settings dialogs in the "
        "UI do, several in one call. Each item names one entity_id or "
        "device_id and the fields to set. Entity: new_entity_id (rename), "
        "name, area (room, by name or id), device_class (e.g. show a "
        "binary_sensor as a door or window), icon, disabled, hidden, and "
        "show_as for a switch (cover/fan/light/lock/siren/valve - the UI's "
        "'Show as', which also hides the switch), labels, and categories "
        "({scope: category} for the automation/script/scene/helpers pages' "
        "categories, null clears a scope). Device: name, area, disabled, "
        "labels. 'labels' sets the whole set; add_labels/remove_labels "
        "change it. null clears name/area/device_class/icon back to the "
        "default. Every item is checked before anything changes; an unknown "
        "entity, device or room refuses the whole batch (rooms are matched "
        "exactly - list them with list_helpers domain='area', create one "
        "with create_helper domain='area'; labels and categories the same "
        "way, by name or id, with domain='label' / 'category'). A rename doesn't rename the id "
        "where it's used: the preview lists every reference (YAML config, "
        "dashboards, persons' trackers, helper config entries); with "
        "update_references=true, the ones that can be written are rewritten "
        "(automations.yaml, scripts.yaml, packages/, storage dashboards, "
        "persons) and YAML reloaded - the rest are reported. Enabling an "
        "entity reloads its integration after a short delay, as in the UI."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("items"): vol.All([_UPDATE_ITEM], vol.Length(min=1)),
            vol.Optional("update_references"): bool,
        }
    )

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """What each item changes, problems, and a rename's references."""
        try:
            plans = registry_manager.plan_updates(hass, tool_input.tool_args["items"])
        except registry_manager.RegistryUpdateError as exc:
            return {"problems": str(exc)}
        context: JsonObjectType = {
            "would_change": [
                {
                    f"{plan.kind}_id": plan.target,
                    "changes": cast(JsonValueType, plan.changes),
                    **({"show_as": plan.show_as} if plan.show_as else {}),
                }
                for plan in plans
            ]
        }
        if renames := _renames(plans):
            try:
                if conflicts := await statistics_manager.rename_conflicts(
                    hass, renames
                ):
                    context["statistics"] = cast(JsonValueType, conflicts)
            except RecorderNotAvailableError:
                pass
            try:
                user = await helper_manager.resolve_user(hass, llm_context)
                context["references"] = {
                    old: cast(
                        JsonValueType,
                        await references.find_references(hass, user, old),
                    )
                    for old, _ in renames
                }
            except (UnresolvedUserError, WebSocketCommandError):
                return context
            context["references_note"] = (
                "These still name the old entity_id after the rename. With "
                "update_references=true, the writable ones are rewritten."
            )
        return context

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Apply the batch, then rewrite or report a rename's references."""
        args = tool_input.tool_args
        mirroring = mirror.is_mirror_enabled(hass)
        try:
            user = await helper_manager.resolve_user(hass, llm_context)
            plans = registry_manager.plan_updates(hass, args["items"])
        except (UnresolvedUserError, registry_manager.RegistryUpdateError) as exc:
            return _tool_error(exc)
        before = [registry_manager.snapshot(hass, plan, plan.target) for plan in plans]
        results = await registry_manager.apply_updates(hass, user, plans)
        response: JsonObjectType = {"results": cast(JsonValueType, results)}

        applied = {result.get("entity_id") for result in results if "changed" in result}
        refs: JsonObjectType = {}
        for old, new in _renames(plans):
            if new not in applied:
                continue
            try:
                refs[old] = await self._handle_references(
                    hass, user, old, new, rewrite=bool(args.get("update_references"))
                )
            except WebSocketCommandError as exc:
                # The rename itself already happened - report, don't raise.
                refs[old] = {"error": f"references not checked: {exc}"}
        if refs:
            response["references"] = refs

        if mirroring:
            after = [
                registry_manager.snapshot(
                    hass, plan, str(result.get("entity_id", plan.target))
                )
                for plan, result in zip(plans, results)
            ]
            mirror_result = await mirror.mirror_write(
                hass,
                path=(
                    "entities/update-"
                    f"{dt_util.utcnow().strftime('%Y%m%dT%H%M%SZ')}.json"
                ),
                content_before=json.dumps(before, indent=2),
                content_after=json.dumps(after, indent=2),
                content_type="json",
            )
            response["mirror"] = _mirror_result_payload(mirror_result)
        return response

    async def _handle_references(
        self, hass: HomeAssistant, user: User, old: str, new: str, *, rewrite: bool
    ) -> JsonObjectType:
        """Rewrite a rename's references if asked; report what's left."""
        entry: JsonObjectType = {}
        if rewrite:
            result = await references.rewrite_references(hass, user, old, new)
            entry["rewritten"] = cast(JsonValueType, result.rewritten)
            if result.errors:
                entry["errors"] = cast(JsonValueType, result.errors)
            if result.reloaded:
                entry["reloaded"] = cast(JsonValueType, result.reloaded)
            if mirror.is_mirror_enabled(hass):
                entry["mirror"] = [
                    _mirror_result_payload(
                        await mirror.mirror_write(
                            hass,
                            path=item.path,
                            content_before=item.before,
                            content_after=item.after,
                            content_type=item.content_type,
                        )
                    )
                    for item in result.mirror
                ]
        entry["still_referenced"] = cast(
            JsonValueType, await references.find_references(hass, user, old)
        )
        return entry


class ListMqttTopicsTool(GatedTool):
    """Read-only, time-bounded MQTT topic snapshot - see mqtt_manager.py."""

    name = "list_mqtt_topics"
    description = (
        "Subscribe to an MQTT topic filter for a short window and report "
        "the last message seen on each matching topic - the only way to "
        "discover a *retained* MQTT message's existence, since MQTT has "
        "no 'list retained messages' query; retained messages are always "
        "delivered immediately on subscribe, before any new live "
        "traffic, which is what this relies on. Useful for tracing a "
        "'ghost' entity (a live state with no entity registry entry - "
        "delete_entity/delete_entities can't touch these, there's no "
        "registry entry to remove) back to the MQTT topic keeping it "
        "alive. Read-only - never publishes anything. The default topic "
        "'homeassistant/#' is Home Assistant's own MQTT *discovery* "
        "prefix, not where a plain YAML-configured MQTT sensor's state "
        "lives - point `topic` at the actual topic tree instead (e.g. "
        "'watermeter/#') once you know or suspect it from the entity's "
        "own naming. Raises if the mqtt integration isn't configured on "
        "this instance."
    )
    parameters = vol.Schema(
        {
            vol.Optional("topic", default=mqtt_manager.DEFAULT_TOPIC): str,
            vol.Optional("timeout", default=mqtt_manager.DEFAULT_TIMEOUT): vol.All(
                vol.Coerce(float), vol.Range(min=0.5, max=mqtt_manager.MAX_TIMEOUT)
            ),
            vol.Optional("limit", default=mqtt_manager.DEFAULT_LIMIT): int,
        }
    )

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Listen on the given topic filter and report what showed up."""
        try:
            return await mqtt_manager.list_topics(hass, **tool_input.tool_args)
        except mqtt_manager.MqttNotAvailableError as exc:
            return _tool_error(exc)


class RenderTemplateTool(GatedTool):
    """Render a Jinja2 template against live state - the core author/iterate loop primitive."""

    name = "render_template"
    description = (
        "Render a Jinja2 template against this instance's live state. "
        "Never raises - a render failure comes back as {'success': false, "
        "'error': ...} so you can iterate without a tool-call error "
        "interrupting the loop. Always prefer this over asking the user "
        "to paste a template into the Developer Tools UI."
    )
    parameters = vol.Schema(
        {vol.Required("template"): str, vol.Optional("variables"): dict}
    )

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Render the template."""
        args = tool_input.tool_args
        return await template_manager.render_template(
            hass, args["template"], variables=args.get("variables")
        )


class ValidateTemplateTool(GatedTool):
    """Check template syntax and referenced entities without necessarily needing a full render."""

    name = "validate_template"
    description = (
        "Validate a template's syntax and report which entities it "
        "references, distinguishing a syntax error (never rendered) from "
        "a render error (valid syntax, failed against live state) from "
        "success. 'unknown_entities' flags referenced entity_ids that "
        "don't currently exist - a template can render 'successfully' "
        "while silently treating a typo'd entity_id as always-unavailable, "
        "which this surfaces explicitly. Use this before write_automation "
        "when a template's correctness matters, not just render_template."
    )
    parameters = vol.Schema({vol.Required("template"): str})

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Validate the template."""
        return await template_manager.validate_template(
            hass, tool_input.tool_args["template"]
        )


class GetLogsTool(GatedTool):
    """Tail/filter/search the core Home Assistant log - never an unbounded raw dump."""

    name = "get_logs"
    description = (
        "Read Home Assistant's core log with filtering. Defaults to the "
        "most recent 100 entries (tail behavior) - use level/search/since/"
        "until to narrow further, or offset+limit to page through older "
        "entries instead of relying on lines/tail."
    )
    parameters = vol.Schema(
        {
            vol.Optional("lines", default=100): vol.All(
                int, vol.Range(min=1, max=1000)
            ),
            vol.Optional("level"): str,
            vol.Optional("search"): str,
            vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0)),
            vol.Optional("limit", default=100): vol.All(
                int, vol.Range(min=1, max=1000)
            ),
        }
    )

    def __init__(self, log_manager: LogManager) -> None:
        """Init with the LogManager backing this tool."""
        self._log_manager = log_manager

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Return filtered log entries, newest first."""
        args = tool_input.tool_args
        filters = LogFilters(
            lines=args.get("lines"),
            level=args.get("level"),
            search=args.get("search"),
            offset=args.get("offset", 0),
            limit=args.get("limit", 100),
        )
        entries = await self._log_manager.get_core_logs(filters)
        return {"entries": [e.to_dict() for e in entries], "count": len(entries)}


class GetEntityHistoryTool(GatedTool):
    """Recorder-backed state history - what an entity's state actually was, and when."""

    name = "get_entity_history"
    description = (
        "Read recorded state history for one or more entities over a time "
        "range - the recorder-backed equivalent of the History page. Use "
        "this to establish what actually happened (did a trigger entity "
        "change state, was an automation entity off) instead of guessing "
        "from current state alone. start_time/end_time are ISO 8601 "
        "datetimes; end_time defaults to now. Every requested entity_id is "
        "always present in the result, even with zero states, so a typo'd "
        "or never-recorded entity_id is visible rather than silently "
        "dropped. Bounded by the recorder's own retention (purge_keep_days, "
        "10 days by default) - an empty result for an old enough time range "
        "means the data is gone, not that nothing happened; say so rather "
        "than concluding nothing occurred."
    )
    parameters = vol.Schema(
        {
            vol.Required("entity_ids"): vol.All([str], vol.Length(min=1)),
            vol.Required("start_time"): str,
            vol.Optional("end_time"): str,
            vol.Optional("significant_changes_only", default=True): bool,
            vol.Optional("limit", default=200): vol.All(
                int, vol.Range(min=1, max=2000)
            ),
        }
    )

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Fetch state history for the requested entities."""
        args = tool_input.tool_args
        try:
            start_time = _parse_datetime(
                _require(args, "start_time"), field="start_time"
            )
            end_time = (
                _parse_datetime(args["end_time"], field="end_time")
                if args.get("end_time")
                else None
            )
            return await history_manager.get_entity_history(
                hass,
                _require(args, "entity_ids"),
                start_time=start_time,
                end_time=end_time,
                significant_changes_only=args.get("significant_changes_only", True),
                limit=args.get("limit", 200),
            )
        except (RecorderNotAvailableError, ValueError) as exc:
            return _tool_error(exc)


class ListStatisticsTool(GatedTool):
    """The recorder's long-term statistics - see statistics_manager.py."""

    name = "list_statistics"
    description = (
        "List the recorder's long-term statistics - what the Energy dashboard "
        "and long-term graphs run on, kept indefinitely, also for entities "
        "deleted long ago and for external statistics that were never "
        "entities (e.g. 'tibber:energy_consumption_<home_id>'). Each has its "
        "source, unit, has_sum/has_mean, has_entity (false: the entity is "
        "gone - an orphan; null: an external statistic), first_period/"
        "last_period (last_period shows whether it's still fed) and the "
        "issues HA's own validation reports for it (Developer Tools > "
        "Statistics' 'Fix issue' list). Filter by search (id or name), "
        "statistic_type (sum/mean), source, unit, orphaned_only, "
        "issues_only. Read-only."
    )
    parameters = vol.Schema(
        {
            vol.Optional("search"): str,
            vol.Optional("statistic_type"): vol.In(["sum", "mean"]),
            vol.Optional("source"): str,
            vol.Optional("unit"): str,
            vol.Optional("orphaned_only", default=False): bool,
            vol.Optional("issues_only", default=False): bool,
            vol.Optional(
                "limit", default=statistics_manager.DEFAULT_LIST_LIMIT
            ): vol.All(int, vol.Range(min=1, max=2000)),
        }
    )

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """List statistics."""
        try:
            return cast(
                JsonObjectType,
                await statistics_manager.list_statistics(hass, **tool_input.tool_args),
            )
        except RecorderNotAvailableError as exc:
            return _tool_error(exc)


class GetStatisticsTool(GatedTool):
    """Rows of one or more long-term statistics - see statistics_manager.py."""

    name = "get_statistics"
    description = (
        "Read long-term statistics rows (list_statistics finds the ids) "
        "between start_time and end_time (ISO 8601; end defaults to now), "
        "per period (5minute, hour, day, week, month - 5minute only covers "
        "the recorder's short retention). types picks the columns (sum, "
        "state, change, mean, min, max, last_reset; all by default); units "
        "converts, e.g. {'energy': 'kWh'}. Rows come oldest first, at most "
        "'limit' per id: a cut series has next_start_time to continue from. "
        "Use it to check a history migration (continuous sum, no spikes) or "
        "whether a source still gets data. Every requested id is in the "
        "result; known=false means the recorder has no such statistic. "
        "Read-only."
    )
    parameters = vol.Schema(
        {
            vol.Required("statistic_ids"): vol.All([str], vol.Length(min=1)),
            vol.Required("start_time"): str,
            vol.Optional("end_time"): str,
            vol.Optional("period", default="hour"): vol.In(statistics_manager.PERIODS),
            vol.Optional("types"): [vol.In(statistics_manager.TYPES)],
            vol.Optional("units"): {str: str},
            vol.Optional(
                "limit", default=statistics_manager.DEFAULT_ROW_LIMIT
            ): vol.All(int, vol.Range(min=1, max=10000)),
        }
    )

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Fetch statistics rows."""
        args = tool_input.tool_args
        try:
            return cast(
                JsonObjectType,
                await statistics_manager.get_statistics(
                    hass,
                    _require(args, "statistic_ids"),
                    start_time=_parse_datetime(
                        _require(args, "start_time"), field="start_time"
                    ),
                    end_time=(
                        _parse_datetime(args["end_time"], field="end_time")
                        if args.get("end_time")
                        else None
                    ),
                    period=args.get("period", "hour"),
                    types=args.get("types"),
                    units=args.get("units"),
                    limit=args.get("limit", statistics_manager.DEFAULT_ROW_LIMIT),
                ),
            )
        except (RecorderNotAvailableError, ValueError) as exc:
            return _tool_error(exc)


_STATISTICS_ERRORS = (
    RecorderNotAvailableError,
    statistics_manager.StatisticsChangeRefusedError,
)
_STATISTICS_WRITE_ERRORS = (
    statistics_manager.StatisticsTimeoutError,
    statistics_manager.StatisticsBackupError,
)


async def _back_up_statistics(
    hass: HomeAssistant, statistic_ids: list[str]
) -> JsonObjectType | None:
    """Push the series about to be cleared to the mirror repo, in
    recorder/import_statistics' shape (issue #134). None when mirroring is
    off; otherwise the mirror payload - check its 'mirrored'."""
    if not mirror.is_mirror_enabled(hass):
        return None
    document = await statistics_manager.backup_document(hass, statistic_ids)
    stamp = dt_util.utcnow().strftime("%Y%m%dT%H%M%SZ")
    result = await mirror.mirror_write(
        hass,
        path=f"statistics/cleared-{stamp}.json",
        content_before=None,
        content_after=json.dumps(document, indent=1),
        content_type="json",
    )
    return _mirror_result_payload(result)


def _backup_failed(backup: JsonObjectType) -> JsonObjectType:
    return {
        "error": (
            "the backup couldn't be pushed to the mirror repo, so nothing "
            "was changed - fix mirroring, or pass allow_no_backup=true"
        ),
        "error_type": "StatisticsBackupError",
        "mirror": backup,
    }


async def _guarded_statistics_write(
    hass: HomeAssistant,
    statistic_ids: list[str],
    operation: str,
    write: Callable[[], Coroutine[Any, Any, JsonObjectType]],
    *,
    allow_no_backup: bool,
) -> JsonObjectType:
    """Every destructive statistics write (issue #136): copy the series it
    changes to the mirror repo (refused if that fails, unless
    allow_no_backup) and into backup statistics in the recorder (always -
    refused if that fails), then write.

    Runs as a background task, so it finishes even if the client gives up.
    A big one (a merge of a year or two of hourly rows) can outlast the
    client's timeout, which then reported an error for a write that was in
    fact still happening (issue #141): after _STATISTICS_DEADLINE seconds
    this answers `still_running` with the backups already made, and the
    task posts a Home Assistant notification with its result when done."""
    progress: JsonObjectType = {}

    async def commit() -> JsonObjectType:
        mirrored = (
            await _back_up_statistics(hass, statistic_ids) if statistic_ids else None
        )
        if mirrored is not None:
            progress["mirror"] = mirrored
        if mirrored is not None and not mirrored["mirrored"] and not allow_no_backup:
            return _backup_failed(mirrored)
        response: JsonObjectType
        try:
            made = await statistics_backup.create_backups(
                hass, statistic_ids, operation
            )
            if made:
                progress["backups"] = cast(JsonValueType, made)
            response = await write()
            if made:
                response["backups"] = cast(JsonValueType, made)
                response["restore"] = statistics_manager.RESTORE_HINT
        except statistics_manager.StatisticsTimeoutError as exc:
            # Queued but unconfirmed: report what the statistics hold now,
            # not a guess (issue #145).
            response = _tool_error(exc)
            response["now"] = cast(
                JsonValueType,
                await statistics_manager.describe_statistics(hass, statistic_ids),
            )
            if progress.get("backups"):
                response["backups"] = progress["backups"]
        except _STATISTICS_WRITE_ERRORS as exc:
            response = _tool_error(exc)
        if mirrored is not None:
            response["mirror"] = mirrored
        return response

    task: asyncio.Task[JsonObjectType] = hass.async_create_background_task(
        commit(), f"ha_dev_tools {operation}"
    )
    # asyncio.wait doesn't cancel the task, on timeout or when this request
    # is cancelled.
    await asyncio.wait({task}, timeout=_STATISTICS_DEADLINE)
    if task.done():
        return task.result()
    task.add_done_callback(partial(_notify_statistics_result, hass, operation))
    return {
        "still_running": True,
        "operation": operation,
        **progress,
        "note": (
            f"Still writing after {_STATISTICS_DEADLINE:.0f} s - it carries on "
            "in Home Assistant and posts a notification with its result when "
            "done. Don't call it again; check the result with get_statistics "
            "or list_statistics in a minute."
        ),
    }


_STATISTICS_DEADLINE = 40.0


def _notify_statistics_result(
    hass: HomeAssistant, operation: str, task: asyncio.Task[JsonObjectType]
) -> None:
    """Tell the owner how a statistics write the tool stopped waiting for
    ended."""
    if task.cancelled():
        outcome = "was cancelled"
    elif (exc := task.exception()) is not None:
        outcome = f"failed: {exc}"
    else:
        result = task.result()
        outcome = (
            f"failed: {result['error']}"
            if "error" in result
            else "finished:\n\n```json\n"
            + json.dumps(result, indent=1, default=str)[:4000]
            + "\n```"
        )
    persistent_notification.async_create(
        hass,
        f"`{operation}` {outcome}",
        title=f"HA Dev Tools: {operation}",
        notification_id=f"{DOMAIN}_{operation}",
    )


class ClearStatisticsTool(WriteGatedTool):
    """Delete long-term statistics - see statistics_manager.py."""

    name = "clear_statistics"
    description = (
        "Delete one or more statistics (list_statistics finds the ids) - "
        "their long-term and short-term rows and metadata, what Developer "
        "Tools > Statistics' 'Fix issue' > Delete does. For orphaned or "
        "stray series, e.g. the hours a replacement entity collected before "
        "a history migration (or use migrate_statistics, which does that "
        "for you). The preview shows each id's source, unit, has_entity, "
        "first/last period and row count. Refused, for the whole batch: an "
        "unknown id; an id that belongs to an existing entity (the series "
        "it records into) unless allow_live=true; one the Energy dashboard "
        "uses unless allow_energy=true. Each series is backed up first, "
        "twice: into a backup statistic in HA's own database (the result "
        "names it; restore_statistics puts it back) and to the mirror repo "
        "(statistics/cleared-<time>.json, in recorder/import_statistics' "
        "shape). Nothing is cleared if either fails; with mirroring off "
        "it's refused unless allow_no_backup=true, which skips only the "
        "mirror copy. Clearing a backup statistic makes no backup of it."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("statistic_ids"): vol.All([str], vol.Length(min=1)),
            vol.Optional("allow_live"): bool,
            vol.Optional("allow_energy"): bool,
            vol.Optional("allow_no_backup"): bool,
        }
    )

    async def _plan(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return await statistics_manager.plan_clear(
            hass,
            args["statistic_ids"],
            can_back_up=mirror.is_mirror_enabled(hass),
            allow_live=bool(args.get("allow_live")),
            allow_energy=bool(args.get("allow_energy")),
            allow_no_backup=bool(args.get("allow_no_backup")),
        )

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """What would be deleted, or why it's refused."""
        try:
            planned = await self._plan(hass, tool_input.tool_args)
        except _STATISTICS_ERRORS as exc:
            return {"problems": str(exc)}
        return {"would_clear": cast(JsonValueType, planned)}

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Back up, then clear - to the end even if the client gives up."""
        args = tool_input.tool_args
        try:
            planned = await self._plan(hass, args)
        except _STATISTICS_ERRORS as exc:
            return _tool_error(exc)
        ids = [info["statistic_id"] for info in planned]

        async def write() -> JsonObjectType:
            await statistics_manager.clear_statistics(hass, ids)
            return {"cleared": cast(JsonValueType, planned)}

        return await _guarded_statistics_write(
            hass,
            ids,
            self.name,
            write,
            allow_no_backup=bool(args.get("allow_no_backup")),
        )


class MigrateStatisticsTool(WriteGatedTool):
    """Move a statistics series onto another statistic_id - see statistics_manager.py."""

    name = "migrate_statistics"
    description = (
        "Move one entity's statistics history onto another entity_id, e.g. "
        "onto the replacement after a device or firmware change renamed "
        "its entities - the new entity keeps its name and continues the "
        "old series. from_statistic_id must be an entity's own statistic "
        "whose entity is gone (delete the dead entity first with "
        "delete_entity). A series already under to_statistic_id (the hours "
        "the replacement collected so far) is cleared and replaced, since "
        "HA can't merge two series - it's backed up first, like "
        "clear_statistics (merge_statistics keeps both instead). A meter "
        "gets a 5-minute row with the moved series' last sum, so the "
        "entity's next compile continues it. Units and sum/mean kind must "
        "match. "
        "The result has the moved series and its continuity: the gap since "
        "its last period and the jump from its last state to the entity's "
        "current state; check the next hours with get_statistics. Energy "
        "dashboard entries naming from_statistic_id need updating "
        "afterwards (write_energy_config)."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("from_statistic_id"): str,
            vol.Required("to_statistic_id"): str,
            vol.Optional("allow_no_backup"): bool,
        }
    )

    async def _plan(self, hass: HomeAssistant, args: dict[str, Any]) -> dict[str, Any]:
        return await statistics_manager.plan_migrate(
            hass,
            args["from_statistic_id"],
            args["to_statistic_id"],
            can_back_up=mirror.is_mirror_enabled(hass),
            allow_no_backup=bool(args.get("allow_no_backup")),
        )

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """The series that moves and the one it replaces, or why it's refused."""
        try:
            plan = await self._plan(hass, tool_input.tool_args)
        except _STATISTICS_ERRORS as exc:
            return {"problems": str(exc)}
        context: JsonObjectType = {
            "would_move": cast(JsonValueType, plan["from"]),
            "would_replace": cast(JsonValueType, plan["replaces"]),
        }
        if plan["from"]["energy"]:
            context["energy_note"] = (
                "The Energy dashboard uses "
                f"{plan['from']['statistic_id']} "
                f"({', '.join(plan['from']['energy'])}); point it at "
                f"{plan['to']} afterwards with write_energy_config."
            )
        return context

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Back up what's replaced, then move - to the end even if the client gives up."""
        args = tool_input.tool_args
        try:
            plan = await self._plan(hass, args)
        except _STATISTICS_ERRORS as exc:
            return _tool_error(exc)

        async def write() -> JsonObjectType:
            return cast(
                JsonObjectType,
                await statistics_manager.migrate_statistics(hass, plan),
            )

        return await _guarded_statistics_write(
            hass,
            [plan["to"]] if plan["replaces"] else [],
            self.name,
            write,
            allow_no_backup=bool(args.get("allow_no_backup")),
        )


class MergeStatisticsTool(WriteGatedTool):
    """Merge statistics series into one - see statistics_merge.py."""

    name = "merge_statistics"
    description = (
        "Combine one or more source statistics into a target's series - "
        "for several old meters that became one (their hours added), or a "
        "replacement that ran in parallel before the old device was "
        "retired; HA itself can't combine two series. Meters (sum): every "
        "series' per-hour changes are combined and the target's sum is "
        "rebuilt from them, continuous across seams; the target keeps its "
        "own state, and a live target goes on recording from the merged "
        "sum. Measurements (mean): rows are taken as they are. Hours in "
        "more than one series follow overlap: refuse (default), "
        "target_wins, source_wins (sources in the order given) or add "
        "(meters only). start/end (ISO 8601) limit what's taken from the "
        "sources. Units are converted within one class (Wh/kWh), kinds "
        "must match. The preview shows each series, the overlaps and their "
        "resolution, every seam (gap, state jump), and the inputs' total "
        "change vs the result's. Sources stay as they are (clear_statistics "
        "removes them). The target is backed up first, in the recorder "
        "(restore_statistics) and to the mirror repo - refused if either "
        "fails; with mirroring off it needs allow_no_backup=true, which "
        "skips only the mirror copy. The result's next_compile checks the "
        "target's newest 5-minute sum - what its next compile continues "
        "from - against its last hourly one (ok=false: the next hour would "
        "drop). A long merge answers still_running after 40 s and finishes "
        "in the background - don't call it again."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("target_statistic_id"): str,
            vol.Required("source_statistic_ids"): vol.All([str], vol.Length(min=1)),
            vol.Optional("overlap"): vol.In(statistics_merge.OVERLAP_RULES),
            vol.Optional("start"): str,
            vol.Optional("end"): str,
            vol.Optional("allow_no_backup"): bool,
        }
    )

    async def _plan(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> statistics_merge.MergePlan:
        return await statistics_merge.plan_merge(
            hass,
            args["target_statistic_id"],
            args["source_statistic_ids"],
            overlap=args.get("overlap", "refuse"),
            start=(
                _parse_datetime(args["start"], field="start")
                if args.get("start")
                else None
            ),
            end=_parse_datetime(args["end"], field="end") if args.get("end") else None,
            can_back_up=mirror.is_mirror_enabled(hass),
            allow_no_backup=bool(args.get("allow_no_backup")),
        )

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """The merge as it would be written, or why it's refused."""
        try:
            plan = await self._plan(hass, tool_input.tool_args)
        except (*_STATISTICS_ERRORS, ValueError) as exc:
            return {"problems": str(exc)}
        return {"would_merge": cast(JsonValueType, plan.preview())}

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Back up the target, then write the merge."""
        args = tool_input.tool_args
        try:
            plan = await self._plan(hass, args)
        except (*_STATISTICS_ERRORS, ValueError) as exc:
            return _tool_error(exc)

        async def write() -> JsonObjectType:
            return cast(
                JsonObjectType, await statistics_merge.merge_statistics(hass, plan)
            )

        return await _guarded_statistics_write(
            hass,
            [args["target_statistic_id"]],
            self.name,
            write,
            allow_no_backup=bool(args.get("allow_no_backup")),
        )


class DeriveStatisticsTool(WriteGatedTool):
    """Build a meter statistic from another one - see statistics_derive.py."""

    name = "derive_statistics"
    description = (
        "Build a meter (sum) statistic from another one, hour by hour: each "
        "hour's change of source_statistic_id times factor - a number (a "
        "feed-in rate), rate periods [{from, value}, ...] (tariff changes), "
        "or a measurement statistic's id whose hourly mean is the factor (a "
        "dynamic price; a missing hour takes the last known price). For the "
        "Energy dashboard's cost/compensation series, which HA only fills "
        "from the moment a source is configured: rebuild the EUR history of "
        "a meter, corrections included. A price in e.g. 'EUR/kWh' converts "
        "the meter to kWh first; for a number or periods, source_unit says "
        "what they're per. target_statistic_id is a new statistic under "
        "'ha_dev_tools:' (pass unit, e.g. 'EUR', and optionally name) or an "
        "existing meter statistic whose start..end range is replaced: "
        "inside it the derived series counts (target hours without a source "
        "row count 0), outside it the target keeps its own changes, and a "
        "live target goes on recording from the new sum (next_compile "
        "checks it). The preview shows hours, total, totals by month and, "
        "for an existing target, what the range held before. An existing "
        "target is backed up first, like merge_statistics. Point the "
        "dashboard at a new statistic with write_energy_config (stat_cost / "
        "stat_compensation)."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("source_statistic_id"): str,
            vol.Required("factor"): vol.Any(int, float, [dict], str),
            vol.Required("target_statistic_id"): str,
            vol.Optional("unit"): str,
            vol.Optional("source_unit"): str,
            vol.Optional("name"): str,
            vol.Optional("start"): str,
            vol.Optional("end"): str,
            vol.Optional("allow_no_backup"): bool,
        }
    )

    async def _plan(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> statistics_merge.MergePlan:
        return await statistics_derive.plan_derive(
            hass,
            args["source_statistic_id"],
            args["factor"],
            args["target_statistic_id"],
            unit=args.get("unit"),
            source_unit=args.get("source_unit"),
            name=args.get("name"),
            start=(
                _parse_datetime(args["start"], field="start")
                if args.get("start")
                else None
            ),
            end=_parse_datetime(args["end"], field="end") if args.get("end") else None,
            can_back_up=mirror.is_mirror_enabled(hass),
            allow_no_backup=bool(args.get("allow_no_backup")),
        )

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """The derived series as it would be written, or why it's refused."""
        try:
            plan = await self._plan(hass, tool_input.tool_args)
        except (*_STATISTICS_ERRORS, ValueError) as exc:
            return {"problems": str(exc)}
        return {"would_derive": cast(JsonValueType, plan.preview())}

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Back up an existing target, then write the derived series."""
        args = tool_input.tool_args
        try:
            plan = await self._plan(hass, args)
        except (*_STATISTICS_ERRORS, ValueError) as exc:
            return _tool_error(exc)

        async def write() -> JsonObjectType:
            result = await statistics_merge.merge_statistics(hass, plan)
            result["derived"] = result.pop("merged")
            return cast(JsonObjectType, result)

        return await _guarded_statistics_write(
            hass,
            [] if plan.target.get("new") else [args["target_statistic_id"]],
            self.name,
            write,
            allow_no_backup=bool(args.get("allow_no_backup")),
        )


class RestoreStatisticsTool(WriteGatedTool):
    """Restore a statistics backup - see statistics_backup.py."""

    name = "restore_statistics"
    description = (
        "Put a statistics backup back: clear_statistics, "
        "migrate_statistics, merge_statistics and this tool copy every "
        "series they change into a backup statistic first "
        "('ha_dev_tools:backup_<id>_<time>'; their results name it, "
        "list_statistics source=ha_dev_tools lists them all with what "
        "they're a backup of, when, and stale=true after 90 days - they're "
        "never deleted automatically, clear_statistics removes them). "
        "Restores onto the statistic it's a backup of, or "
        "target_statistic_id: the target's current series is backed up "
        "too (so a restore can be undone), cleared, and replaced by the "
        "backup's hourly rows; a meter gets a 5-minute row with its last "
        "sum, so a live entity continues from it. 5-minute statistics "
        "themselves aren't restored (HA only imports hourly rows, and "
        "keeps 5-minute ones about 10 days). The backup stays."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("backup_statistic_id"): str,
            vol.Optional("target_statistic_id"): str,
            vol.Optional("allow_no_backup"): bool,
        }
    )

    async def _plan(self, hass: HomeAssistant, args: dict[str, Any]) -> dict[str, Any]:
        return await statistics_backup.plan_restore(
            hass,
            args["backup_statistic_id"],
            args.get("target_statistic_id"),
            can_back_up=mirror.is_mirror_enabled(hass),
            allow_no_backup=bool(args.get("allow_no_backup")),
        )

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """The backup and what it would overwrite, or why it's refused."""
        try:
            plan = await self._plan(hass, tool_input.tool_args)
        except _STATISTICS_ERRORS as exc:
            return {"problems": str(exc)}
        return {
            "would_restore": cast(JsonValueType, plan["backup"]),
            "onto": plan["target"],
            "would_overwrite": cast(JsonValueType, plan["overwrites"]),
        }

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Back up the target's current series, then restore."""
        args = tool_input.tool_args
        try:
            plan = await self._plan(hass, args)
        except _STATISTICS_ERRORS as exc:
            return _tool_error(exc)

        async def write() -> JsonObjectType:
            return cast(
                JsonObjectType,
                await statistics_backup.restore_statistics(hass, plan),
            )

        return await _guarded_statistics_write(
            hass,
            [plan["target"]] if plan["overwrites"] else [],
            self.name,
            write,
            allow_no_backup=bool(args.get("allow_no_backup")),
        )


class GetLogbookTool(GatedTool):
    """Recorder-backed logbook - humanized events, including what triggered what."""

    name = "get_logbook"
    description = (
        "Read humanized logbook entries for a time range - the same "
        "entries the Logbook page shows (automations/scripts triggering, "
        "notable state changes), including what caused each one where HA "
        "recorded that context. Prefer this over get_entity_history when "
        "the question is 'what happened and why', not just 'what was the "
        "state'. Omit entity_ids for every entity; scope it down when "
        "possible. start_time/end_time are ISO 8601 datetimes; end_time "
        "defaults to now. Bounded by the recorder's own retention "
        "(purge_keep_days, 10 days by default) - an empty result for an "
        "old enough time range means the data is gone, not that nothing "
        "happened; say so rather than concluding nothing occurred."
    )
    parameters = vol.Schema(
        {
            vol.Required("start_time"): str,
            vol.Optional("end_time"): str,
            vol.Optional("entity_ids"): vol.All([str], vol.Length(min=1)),
            vol.Optional("limit", default=200): vol.All(
                int, vol.Range(min=1, max=2000)
            ),
        }
    )

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Fetch logbook entries for the requested period."""
        args = tool_input.tool_args
        try:
            start_time = _parse_datetime(
                _require(args, "start_time"), field="start_time"
            )
            end_time = (
                _parse_datetime(args["end_time"], field="end_time")
                if args.get("end_time")
                else None
            )
            return await history_manager.get_logbook_entries(
                hass,
                start_time=start_time,
                end_time=end_time,
                entity_ids=args.get("entity_ids"),
                limit=args.get("limit", 200),
            )
        except (RecorderNotAvailableError, ValueError) as exc:
            return _tool_error(exc)


class ListAddonsTool(GatedTool):
    """List installed Supervisor add-ons - Home Assistant OS/Supervised only."""

    name = "list_addons"
    description = (
        "List installed Home Assistant add-ons (name, slug, state, "
        "version). Only available on Home Assistant OS or Supervised "
        "installs - returns a clear error on Core-only installs rather "
        "than an unrelated failure."
    )
    parameters = vol.Schema({})

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """List add-ons."""
        try:
            addons = await supervisor_manager.list_addons(hass)
        except SupervisorNotAvailableError as exc:
            return _tool_error(exc)
        return cast(JsonObjectType, {"addons": addons})


class GetAddonLogsTool(GatedTool):
    """Tail a Supervisor add-on's logs - Home Assistant OS/Supervised only."""

    name = "get_addon_logs"
    description = (
        "Read a Home Assistant add-on's log output by slug (see "
        "list_addons for slugs). Separate log source from get_logs - "
        "add-ons run outside HA core and have their own logs. Only "
        "available on Home Assistant OS or Supervised installs."
    )
    parameters = vol.Schema(
        {
            vol.Required("slug"): str,
            vol.Optional("lines", default=100): vol.All(
                int, vol.Range(min=1, max=1000)
            ),
        }
    )

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Get add-on logs."""
        args = tool_input.tool_args
        try:
            return await supervisor_manager.get_addon_logs(
                hass, args["slug"], lines=args.get("lines")
            )
        except SupervisorNotAvailableError as exc:
            return _tool_error(exc)


class CheckConfigTool(GatedTool):
    """Validate the full HA configuration - no changes, no restart."""

    name = "check_config"
    description = (
        "Validate the current Home Assistant configuration (same check as "
        "the UI's 'Check configuration' button), plus what that check "
        "misses: 'setup_failures' lists automations/scripts HA parsed but "
        "refused to set up (e.g. an invalid trigger value), with HA's own "
        "error - these make 'valid' false. 'repairs' lists every active "
        "Repairs issue with the title/description the Repairs page shows. "
        "'config_entry_problems' lists UI-set-up integrations that failed, "
        "are retrying, or need re-authentication, with HA's reason. "
        "Setup failures reflect the last reload: write_automation/"
        "write_script reload automatically and already return their own "
        "item's 'setup_error'. With mirroring on, a passing check also "
        "snapshots hand-edited config (configuration.yaml, its includes, "
        "packages, custom_templates) to the mirror repo: 'config_snapshot'."
    )
    parameters = vol.Schema({})

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Run HA's own config check, then snapshot a passing config."""
        result = await config_tools.check_ha_config(hass)
        if mirror.is_mirror_enabled(hass):
            # Only a config that passes HA's own check is snapshotted, so
            # the mirror always holds a last-known-good copy (issue #105).
            result["config_snapshot"] = (
                {"skipped_all": "configuration check failed - kept last good copy"}
                if result["errors"]
                else await config_snapshot.async_snapshot(hass)
            )
        return cast(JsonObjectType, result)


class GetConfigFileTool(GatedTool):
    """A hand-edited config file's raw text - live, or its mirrored copy."""

    name = "get_config_file"
    description = (
        "Read a hand-edited config file's raw text: configuration.yaml, any "
        "file it (or those files) !include / !include_dir_*, packages/*.yaml "
        "and custom_templates/*.jinja. Tags such as !secret and !include "
        "are returned as written, never resolved. Use it when a file no "
        "longer parses (get_automation/get_rest_command etc. can't read it "
        "then) or to see exactly how it's written. source='mirror' returns "
        "the mirror repo's copy instead - the last one that passed "
        "check_config - to recover a broken block. A file is withheld if "
        "any line looks like a literal credential (move it to secrets.yaml "
        "with !secret); secrets.yaml itself is never returned. Without "
        "'path', lists the files covered. With 'key' (e.g. "
        "'irrigation_unlimited'), returns just that top-level block from "
        "every covered file that has one - or from 'path' - with its file "
        "and lines: an integration's own YAML config, found wherever it "
        "lives, and checked for credentials on its own rather than with "
        "the rest of its file. Read-only."
    )
    parameters = vol.Schema(
        {
            vol.Optional("path"): str,
            vol.Optional("key"): str,
            vol.Optional("source", default="live"): vol.In(["live", "mirror"]),
        }
    )

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Return the file (or the list of files)."""
        args = tool_input.tool_args
        try:
            return cast(
                JsonObjectType,
                await config_snapshot.get_config_file(
                    hass, args.get("path"), args.get("source", "live"), args.get("key")
                ),
            )
        except config_snapshot.ConfigFileError as exc:
            return _tool_error(exc)


class ReloadDomainTool(GatedTool):
    """Reload a domain's config (automation/script/scene/...) - never a full restart."""

    name = "reload_domain"
    description = (
        "Reload a domain's configuration without restarting Home Assistant "
        "- e.g. domain='automation' after editing automations. Most config "
        "domains (automation, script, scene, input_boolean, ...) support "
        "this; call check_config first if you're not sure the edit is valid. "
        "An integration set up through the UI (e.g. a cloud or local-push "
        "integration with a wedged connection) has its config entry reloaded "
        "instead, like the UI's Reload; if the domain has several entries, "
        "pass entry_id (the entries are listed otherwise). check_config's "
        "config_entry_problems lists entries that failed, are retrying, or "
        "need re-authentication (re-auth needs the user's credentials: point "
        "them to Settings > Devices & services). Never reloads ha_dev_tools "
        "or mcp_server, which would drop this session."
    )
    parameters = vol.Schema(
        {vol.Required("domain"): str, vol.Optional("entry_id"): str}
    )

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Reload the given domain."""
        return await config_tools.reload_domain(
            hass, tool_input.tool_args["domain"], tool_input.tool_args.get("entry_id")
        )


class GetAutomationTool(GatedTool):
    """Layout-aware automation read - resolves the file that actually defines it."""

    name = "get_automation"
    description = (
        "Read an automation's config by id, resolving which file actually "
        "defines it (the default automations.yaml, or a packages/*.yaml "
        "file) - a plain file read can silently miss package-defined "
        "automations. Fails clearly if the id isn't found or is defined in "
        "more than one file, rather than guessing. Also reports whether "
        "the automation is currently enabled - that's runtime-only state "
        "(toggling it via the UI or automation.turn_off never touches the "
        "YAML), so the config content alone never tells you whether it's "
        "actually active right now."
    ) + _MISREAD_VALUES_NOTE
    parameters = vol.Schema({vol.Required("automation_id"): str})

    def __init__(self, automation_manager: AutomationManager) -> None:
        """Init with the AutomationManager backing this tool."""
        self._manager = automation_manager

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Resolve and return an automation's config and source file."""
        try:
            location, config = await self._manager.get_automation(
                tool_input.tool_args["automation_id"]
            )
        except (AutomationNotFoundError, DuplicateAutomationIdError) as exc:
            return _tool_error(exc)
        automation_id = tool_input.tool_args["automation_id"]
        live_state = audit_manager.find_automation_state(hass, automation_id)
        return {
            "file_path": location.file_path,
            "is_package": location.is_package,
            "config": to_json_safe(config),
            "currently_enabled": (
                None if live_state is None else live_state.state == "on"
            ),
            "runtime_state_note": (
                "No automation.<x> entity found for this id yet - it may "
                "not have been reloaded since being added or last edited."
                if live_state is None
                else None
            ),
            "misread_values": cast(JsonValueType, find_misread_scalars(config)),
        }


class WriteAutomationTool(WriteGatedTool):
    """Layout-aware, package-safe automation write - see docs/ARCHITECTURE.md."""

    name = "write_automation"
    description = (
        "Create or update an automation. If the id already exists, it's "
        "updated in place in whichever file actually defines it (default "
        "file or a package) - never blindly appended to automations.yaml, "
        "which would create a silent duplicate for package-defined "
        "automations. For a brand new automation, pass 'package' to target "
        "an existing packages/*.yaml file, or omit it for the default "
        "automations.yaml. Always reloads automations afterward - never "
        "requires a restart. Pass expected_hash (from get_file_metadata or "
        "a prior read) to detect concurrent edits."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("automation_id"): str,
            vol.Required("config"): dict,
            vol.Optional("package"): str,
            vol.Optional("expected_hash"): str,
        }
    )

    def __init__(self, automation_manager: AutomationManager) -> None:
        """Init with the AutomationManager backing this tool."""
        self._manager = automation_manager

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Write the automation through its correct file and reload."""
        args = tool_input.tool_args
        try:
            result = await self._manager.write_automation(
                args["automation_id"],
                args["config"],
                package=args.get("package"),
                expected_hash=args.get("expected_hash"),
            )
        except (AutomationNotFoundError, DuplicateAutomationIdError, ValueError) as exc:
            return _tool_error(exc)
        response: JsonObjectType = {
            "file_path": result.location.file_path,
            "is_package": result.location.is_package,
            # HA's own error if it refused to set the item up after the
            # reload - otherwise invisible until someone opens Repairs.
            "setup_error": config_tools.setup_error(
                hass, "automation", args["automation_id"]
            ),
        }
        return await _mirror_file_write(
            hass,
            response,
            file_path=result.location.file_path,
            content_before=result.content_before,
            content_after=result.content_after,
        )

    @override
    async def _dry_run_mirror(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> mirror.MirrorResult | None:
        """Compute this call's would-be automation content and mirror it to
        a proposed/automation-<id> branch (issue #35) - reuses
        write_automation's own resolve+build logic via dry_run=True, so
        "what would happen" here is exactly what a real write would
        produce, just never touching disk."""
        args = tool_input.tool_args
        try:
            result = await self._manager.write_automation(
                args["automation_id"],
                args["config"],
                package=args.get("package"),
                expected_hash=args.get("expected_hash"),
                dry_run=True,
            )
        except (AutomationNotFoundError, DuplicateAutomationIdError, ValueError) as exc:
            return mirror.MirrorResult(mirrored=False, reason=str(exc))
        return await mirror.mirror_dry_run(
            hass,
            path=result.location.file_path,
            content_before=result.content_before,
            content_after=result.content_after,
            kind="automation",
            entity_id=args["automation_id"],
        )


def _one_or_many(args: dict[str, Any], single: str, many: str) -> list[str]:
    """The ids a delete tool acts on (issue #66): exactly one of `single`
    (one id) or `many` (a list of ids, each at most once)."""
    one, several = args.get(single), args.get(many)
    if one is not None and several is None:
        return [one]
    if several is not None and one is None:
        ids = [str(item) for item in several]
        if not ids:
            raise ValueError(f"{many} is empty")
        if len(set(ids)) != len(ids):
            raise ValueError(f"{many} lists an id more than once: {ids}")
        return ids
    raise ValueError(f"Pass exactly one of {single} or {many}")


_NO_BATCH_DRY_RUN_MIRROR = mirror.MirrorResult(
    mirrored=False,
    reason="Dry-run mirroring covers single deletes only - a batch has no "
    "single proposed/<kind>-<id> branch.",
)


class DeleteAutomationTool(WriteGatedTool):
    """Layout-aware, package-safe automation delete - see docs/ARCHITECTURE.md."""

    name = "delete_automation"
    description = (
        "Delete an automation by id, resolving which file actually defines "
        "it (default file or a package) first - refuses to guess if the id "
        "isn't found or is defined in more than one file, rather than "
        "silently no-oping or deleting the wrong one. To delete several at "
        "once, pass automation_ids (a list) instead of automation_id: one "
        "confirmation for the batch, every id checked before anything is "
        "deleted (one bad id refuses the whole batch), each file written "
        "once. Always reloads automations afterward - never requires a "
        "restart. Pass expected_hash (from get_file_metadata or a prior "
        "read) to detect concurrent edits (single-file deletes only)."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Optional("automation_id"): str,
            vol.Optional("automation_ids"): [str],
            vol.Optional("expected_hash"): str,
        }
    )

    def __init__(self, automation_manager: AutomationManager) -> None:
        """Init with the AutomationManager backing this tool."""
        self._manager = automation_manager

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Delete the automation(s) from their correct file(s) and reload."""
        args = tool_input.tool_args
        try:
            ids = _one_or_many(args, "automation_id", "automation_ids")
            results = await self._manager.delete_automations(
                ids, expected_hash=args.get("expected_hash")
            )
        except (AutomationNotFoundError, DuplicateAutomationIdError, ValueError) as exc:
            return _tool_error(exc)
        if args.get("automation_id") is not None:
            (result,) = results
            response: JsonObjectType = {
                "deleted": True,
                "file_path": result.location.file_path,
                "is_package": result.location.is_package,
            }
            return await _mirror_file_write(
                hass,
                response,
                file_path=result.location.file_path,
                content_before=result.content_before,
                content_after=result.content_after,
            )
        response = {
            "deleted": cast(JsonValueType, ids),
            "files": [
                {"file_path": r.location.file_path, "is_package": r.location.is_package}
                for r in results
            ],
        }
        if mirror.is_mirror_enabled(hass):
            mirrored = []
            for result in results:
                mirror_result = await mirror.mirror_write(
                    hass,
                    path=result.location.file_path,
                    content_before=result.content_before,
                    content_after=result.content_after,
                )
                mirrored.append(_mirror_result_payload(mirror_result))
            response["mirror"] = cast(JsonValueType, mirrored)
        return response

    @override
    async def _dry_run_mirror(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> mirror.MirrorResult | None:
        """Compute this call's would-be (post-delete) content and mirror it
        to a proposed/automation-<id> branch, same pattern as
        WriteAutomationTool's identical hook. A batch spans possibly several
        files and ids, so it has no single proposed branch - not mirrored."""
        args = tool_input.tool_args
        if args.get("automation_id") is None:
            return _NO_BATCH_DRY_RUN_MIRROR
        try:
            result = await self._manager.delete_automation(
                args["automation_id"],
                expected_hash=args.get("expected_hash"),
                dry_run=True,
            )
        except (AutomationNotFoundError, DuplicateAutomationIdError, ValueError) as exc:
            return mirror.MirrorResult(mirrored=False, reason=str(exc))
        return await mirror.mirror_dry_run(
            hass,
            path=result.location.file_path,
            content_before=result.content_before,
            content_after=result.content_after,
            kind="automation",
            entity_id=args["automation_id"],
        )


def _helper_domain_schema() -> vol.In:
    return vol.In(HELPER_DOMAINS)


# person and area storage (.storage/person, .storage/core.area_registry)
# is denylisted, so those two can't be mirrored the way helpers are (issue
# #117): areas are mirrored as the area list itself, persons not at all -
# their data is personal, which is why .storage/person is denylisted.
# Registries mirrored as their item list (their .storage/core.* files are
# denylisted) - areas (#117), labels and categories (#128).
_REGISTRY_MIRROR_PATHS = {
    "area": "areas.json",
    "label": "labels.json",
    "category": "categories.json",
}
_PERSON_NOT_MIRRORED: JsonObjectType = {
    "mirrored": False,
    "reason": "person data isn't mirrored - it's personal data "
    "(.storage/person is denylisted for the same reason).",
}


async def _helper_state(hass: HomeAssistant, user: User, domain: str) -> str | None:
    """What a helper write's mirror records as "before": the domain's
    storage file, or for a registry (areas, labels, categories) its list."""
    if domain == "person":
        return None
    if domain in _REGISTRY_MIRROR_PATHS:
        items = await helper_manager.list_helpers(hass, user, domain)
        return json.dumps(items, indent=2, sort_keys=True)
    return await _read_storage_file(hass, f".storage/{domain}")


async def _mirror_helper_write(
    hass: HomeAssistant,
    user: User,
    domain: str,
    content_before: str | None,
    *,
    upsert: JsonObjectType | None = None,
    remove_ids: list[str] | None = None,
) -> JsonObjectType:
    """Mirror one helper write (see _helper_state) and return its payload."""
    if domain == "person":
        return _PERSON_NOT_MIRRORED
    if domain in _REGISTRY_MIRROR_PATHS:
        path = _REGISTRY_MIRROR_PATHS[domain]
        content_after = cast(str, await _helper_state(hass, user, domain))
    else:
        path = f".storage/{domain}"
        content_after = _reconstruct_helper_storage_json(content_before, upsert=upsert)
        for item_id in remove_ids or []:
            content_after = _reconstruct_helper_storage_json(
                content_after, remove_id=item_id
            )
    result = await mirror.mirror_write(
        hass,
        path=path,
        content_before=content_before,
        content_after=content_after,
        content_type="json",
    )
    return _mirror_result_payload(result)


class ListHelpersTool(GatedTool):
    """List every storage-defined item in a helper domain (input_boolean, counter, etc.)."""

    name = "list_helpers"
    description = (
        "List every helper (input_boolean, input_number, input_text, "
        "input_select, input_datetime, input_button, counter, timer, or "
        "schedule) currently defined via the UI/storage in the given "
        "domain. Does not include YAML-defined helpers of the same "
        "domain - those aren't reachable this way (see get_automation's "
        "'layout-aware' approach for the analogous YAML case). Also "
        "domain='area' for the rooms (areas) - the ids update_entities "
        "assigns - domain='person' for UI-made persons, with their "
        "device_trackers, domain='label' for labels and domain='category' "
        "for the categories of the Automations/Scripts/Scenes/Helpers "
        "pages (each with its scope; its id is '<scope>/<category_id>')."
    )
    parameters = vol.Schema({vol.Required("domain"): _helper_domain_schema()})

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """List helpers in the given domain."""
        try:
            user = await helper_manager.resolve_user(hass, llm_context)
            items = await helper_manager.list_helpers(
                hass, user, tool_input.tool_args["domain"]
            )
        except (
            UnresolvedUserError,
            InvalidHelperDomainError,
            WebSocketCommandError,
        ) as exc:
            return _tool_error(exc)
        return cast(JsonObjectType, {"items": items})


class CreateHelperTool(WriteGatedTool):
    """Create a new helper item."""

    name = "create_helper"
    description = (
        "Create a new helper (input_boolean, counter, timer, etc.) via "
        "the same mechanism the UI's Helpers page uses. 'config' fields "
        "vary by domain - e.g. input_boolean/counter/timer mainly need "
        "'name'; input_number additionally needs 'min'/'max'; "
        "input_select needs 'options' (a list). domain='area' creates a "
        "room ('name', optional 'icon', 'floor_id', 'aliases'); "
        "domain='person' a person ('name', optional 'device_trackers', "
        "'user_id'); domain='label' a label ('name', optional 'icon', "
        "'color', 'description'); domain='category' a category ('scope' - "
        "automation, script, scene or helpers - 'name', optional 'icon')."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {vol.Required("domain"): _helper_domain_schema(), vol.Required("config"): dict}
    )

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Create the helper."""
        args = tool_input.tool_args
        domain = args["domain"]
        mirroring = mirror.is_mirror_enabled(hass)
        try:
            user = await helper_manager.resolve_user(hass, llm_context)
            content_before = (
                await _helper_state(hass, user, domain) if mirroring else None
            )
            created = await helper_manager.create_helper(
                hass, user, domain, args["config"]
            )
        except (
            UnresolvedUserError,
            InvalidHelperDomainError,
            WebSocketCommandError,
            ValueError,
        ) as exc:
            return _tool_error(exc)
        response: JsonObjectType = dict(created)
        if mirroring:
            response["mirror"] = await _mirror_helper_write(
                hass, user, domain, content_before, upsert=created
            )
        return response


class UpdateHelperTool(WriteGatedTool):
    """Update an existing helper item by id."""

    name = "update_helper"
    description = (
        "Update an existing storage-defined helper by id. Only works on "
        "helpers created via the UI/storage, not YAML-defined ones - "
        "list_helpers' results only include the former for exactly this "
        "reason. Only the fields in 'config' change. domain='area' renames "
        "a room or sets its icon/floor; domain='person' e.g. sets "
        "'device_trackers' (the full list) to link trackers to a person; "
        "domain='label'/'category' renames one or changes its icon (color "
        "for a label)."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("domain"): _helper_domain_schema(),
            vol.Required("item_id"): str,
            vol.Required("config"): dict,
        }
    )

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Update the helper."""
        args = tool_input.tool_args
        domain = args["domain"]
        mirroring = mirror.is_mirror_enabled(hass)
        try:
            user = await helper_manager.resolve_user(hass, llm_context)
            content_before = (
                await _helper_state(hass, user, domain) if mirroring else None
            )
            updated = await helper_manager.update_helper(
                hass, user, domain, args["item_id"], args["config"]
            )
        except (
            UnresolvedUserError,
            InvalidHelperDomainError,
            WebSocketCommandError,
            ValueError,
        ) as exc:
            return _tool_error(exc)
        response: JsonObjectType = dict(updated)
        if mirroring:
            response["mirror"] = await _mirror_helper_write(
                hass, user, domain, content_before, upsert=updated
            )
        return response


class DeleteHelperTool(WriteGatedTool):
    """Delete a helper item by id."""

    name = "delete_helper"
    description = (
        "Delete a storage-defined helper by id. To delete several of the "
        "same domain at once, pass item_ids (a list) instead of item_id: one "
        "confirmation, and every id is checked to exist before any is "
        "deleted."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("domain"): _helper_domain_schema(),
            vol.Optional("item_id"): str,
            vol.Optional("item_ids"): [str],
        }
    )

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Delete the helper(s)."""
        args = tool_input.tool_args
        mirroring = mirror.is_mirror_enabled(hass)
        try:
            ids = _one_or_many(args, "item_id", "item_ids")
            user = await helper_manager.resolve_user(hass, llm_context)
            content_before = (
                await _helper_state(hass, user, args["domain"]) if mirroring else None
            )
            if len(ids) > 1 or "item_ids" in args:
                # All-or-nothing: check every id exists before deleting any.
                existing = {
                    str(item.get("id"))
                    for item in await helper_manager.list_helpers(
                        hass, user, args["domain"]
                    )
                }
                if missing := [i for i in ids if i not in existing]:
                    raise ValueError(
                        f"No {args['domain']} helper with id(s) {missing} - "
                        "nothing was deleted"
                    )
            for item_id in ids:
                await helper_manager.delete_helper(hass, user, args["domain"], item_id)
        except (
            UnresolvedUserError,
            InvalidHelperDomainError,
            WebSocketCommandError,
            ValueError,
        ) as exc:
            return _tool_error(exc)
        response: JsonObjectType = (
            {"deleted": True, "domain": args["domain"], "item_id": args["item_id"]}
            if "item_id" in args
            else {"deleted": cast(JsonValueType, ids), "domain": args["domain"]}
        )
        if mirroring:
            response["mirror"] = await _mirror_helper_write(
                hass, user, args["domain"], content_before, remove_ids=ids
            )
        return response


def _derived_sensor_domain_schema() -> vol.In:
    return vol.In(DERIVED_SENSOR_DOMAINS)


def _derived_sensor_mirror_path(domain: str, entry_id: str) -> str:
    """Synthetic mirror-repo path for a derived-sensor config entry (issue #34).

    There's no real file to mirror - the resolved ConfigEntry object's own
    `.data`/`.options` (the same shape get_derived_sensor already returns)
    is what gets pushed, never the raw `.storage/core.config_entries` file:
    that file is shared by every integration on the instance and is
    explicitly in DEFAULT_DENYLIST for exactly that reason. One JSON
    "file" per entry, grouped by domain for readability in the mirror
    repo's own history.
    """
    return f"derived_sensors/{domain}/{entry_id}.json"


class ListDerivedSensorsTool(GatedTool):
    """List config-entry-based derived/calculated sensor helpers."""

    name = "list_derived_sensors"
    description = (
        "List calculated/derived sensor helpers - Min/Max, Utility Meter, "
        "Integration (Riemann sum), Statistics, Threshold, Derivative, "
        "Filter - plus the general-purpose Template helper (can create an "
        "entity in almost any domain: sensor, switch, light, cover, ...; "
        "picking which one is that domain's own first, menu-driven step). "
        "A second helper family alongside list_helpers' nine domains, "
        "implemented as config entries rather than storage items. Omit "
        "domain to list across all eight. Does not cover YAML-defined "
        "template: sensors living in configuration.yaml/packages - use "
        "list_template_entities for those instead."
    )
    parameters = vol.Schema({vol.Optional("domain"): _derived_sensor_domain_schema()})

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """List derived-sensor entries, optionally scoped to one domain."""
        try:
            items = derived_sensor_manager.list_derived_sensors(
                hass, tool_input.tool_args.get("domain")
            )
        except InvalidDerivedSensorDomainError as exc:
            return _tool_error(exc)
        return cast(JsonObjectType, {"items": items})


class GetDerivedSensorTool(GatedTool):
    """Read one derived-sensor config entry's current config by id."""

    name = "get_derived_sensor"
    description = (
        "Read a calculated/derived sensor helper's current config by its "
        "config entry id (from list_derived_sensors)."
    )
    parameters = vol.Schema({vol.Required("entry_id"): str})

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Read the derived-sensor entry."""
        try:
            return derived_sensor_manager.get_derived_sensor(
                hass, tool_input.tool_args["entry_id"]
            )
        except DerivedSensorNotFoundError as exc:
            return _tool_error(exc)


_DERIVED_SENSOR_STEPS_DESCRIPTION = (
    "Home Assistant drives creating/editing one of these through a real "
    "config/options flow, not a flat field dict - some (statistics) are a "
    "fixed multi-step sequence, others (filter) branch into a different "
    "step depending on an earlier answer, and template's very first "
    "create step is a menu (its single field, next_step_id, picks which "
    "entity domain - sensor, switch, light, ... - to create). Call with "
    "steps={} (or omitted) first - like every call to this tool, that "
    "first returns a confirm_token, and it's the confirmed call that comes "
    "back with needs_input=true, the current step_id, and that step's "
    "field schema (a menu's schema is just its list of valid next_step_id "
    "choices; each field's description.suggested_value is its current "
    "value). Fill those fields in under steps[step_id] and call again - "
    "repeat, accumulating entries in steps, until the call returns the "
    "created/updated entry instead of needs_input. Any field of a step "
    "you leave out keeps its current value; pass it as null to clear it."
)


class CreateDerivedSensorTool(WriteGatedTool):
    """Create a new derived-sensor config entry by driving its real config flow."""

    name = "create_derived_sensor"
    description = (
        "Create a new calculated/derived sensor helper (min_max, "
        "utility_meter, integration [Riemann sum], statistics, threshold, "
        "derivative, or filter), or a Template helper (any entity domain, "
        "not just sensors) via the same config flow the UI's Add Helper "
        "wizard uses. " + _DERIVED_SENSOR_STEPS_DESCRIPTION
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("domain"): _derived_sensor_domain_schema(),
            vol.Optional("steps"): dict,
        }
    )

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Drive the config flow forward with the given steps."""
        args = tool_input.tool_args
        try:
            created = await derived_sensor_manager.create_derived_sensor(
                hass, args["domain"], args.get("steps") or {}
            )
        except FlowStepRequiredError as exc:
            return _flow_step_required_payload(exc)
        except (InvalidDerivedSensorDomainError, FlowAbortedError) as exc:
            return _tool_error(exc)
        response: JsonObjectType = dict(created)
        if mirror.is_mirror_enabled(hass):
            mirror_result = await mirror.mirror_write(
                hass,
                path=_derived_sensor_mirror_path(args["domain"], created["entry_id"]),
                content_before=None,
                content_after=json.dumps(created),
                content_type="json",
            )
            response["mirror"] = _mirror_result_payload(mirror_result)
        return response


class UpdateDerivedSensorTool(WriteGatedTool):
    """Update an existing derived-sensor entry by driving its real options flow."""

    name = "update_derived_sensor"
    description = (
        "Update an existing calculated/derived sensor helper's config by "
        "its entry id (from list_derived_sensors), via the same options "
        "flow the UI's helper edit page uses. Simplest form: pass options "
        "as a flat dict of just the fields to change (e.g. "
        '{"entity_ids": [...]}, or {"state": "{{ ... }}"} for a '
        "template sensor) - no step_id needed, every other field keeps its "
        "current value, null clears an optional field, and it still goes "
        "through Home Assistant's own validation. A field that isn't "
        "editable after creation (e.g. utility_meter's cycle) is rejected "
        "with the list of fields that are. Alternatively, use steps "
        "instead of options (not both): " + _DERIVED_SENSOR_STEPS_DESCRIPTION
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("entry_id"): str,
            vol.Optional("steps"): dict,
            vol.Optional("options"): dict,
        }
    )

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """The entry's current options and the fields this call would change."""
        args = tool_input.tool_args
        try:
            preview = derived_sensor_manager.preview_update(
                hass, args["entry_id"], args.get("steps"), args.get("options")
            )
        except DerivedSensorNotFoundError:
            return {}
        return cast(JsonObjectType, preview)

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Drive the options flow forward with the given steps."""
        args = tool_input.tool_args
        try:
            # Only fetched when mirroring is on - avoids an extra
            # get_derived_sensor() call (and its own not-found risk) on
            # every update when nothing will use it.
            content_before = (
                json.dumps(
                    derived_sensor_manager.get_derived_sensor(hass, args["entry_id"])
                )
                if mirror.is_mirror_enabled(hass)
                else None
            )
            updated = await derived_sensor_manager.update_derived_sensor(
                hass, args["entry_id"], args.get("steps") or {}, args.get("options")
            )
        except FlowStepRequiredError as exc:
            return _flow_step_required_payload(exc)
        except (DerivedSensorNotFoundError, FlowAbortedError) as exc:
            return _tool_error(exc)
        response: JsonObjectType = dict(updated)
        if mirror.is_mirror_enabled(hass):
            mirror_result = await mirror.mirror_write(
                hass,
                path=_derived_sensor_mirror_path(
                    updated["domain"], updated["entry_id"]
                ),
                content_before=content_before,
                content_after=json.dumps(updated),
                content_type="json",
            )
            response["mirror"] = _mirror_result_payload(mirror_result)
        return response


class DeleteDerivedSensorTool(WriteGatedTool):
    """Delete a derived-sensor config entry by id."""

    name = "delete_derived_sensor"
    description = (
        "Delete a calculated/derived sensor helper by its entry id. To "
        "delete several at once, pass entry_ids (a list) instead of "
        "entry_id: one confirmation, and every id is checked to exist "
        "before any is deleted."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {vol.Optional("entry_id"): str, vol.Optional("entry_ids"): [str]}
    )

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Delete the derived-sensor entry (or entries)."""
        args = tool_input.tool_args
        try:
            ids = _one_or_many(args, "entry_id", "entry_ids")
            # A batch looks every entry up before deleting any, so an
            # unknown id refuses the whole batch; the lookups double as the
            # mirror's "before" content. A single delete only needs them for
            # mirroring - delete_derived_sensor reports not-found itself.
            befores = (
                {
                    entry_id: derived_sensor_manager.get_derived_sensor(hass, entry_id)
                    for entry_id in ids
                }
                if "entry_ids" in args or mirror.is_mirror_enabled(hass)
                else {}
            )
            results = [
                await derived_sensor_manager.delete_derived_sensor(hass, entry_id)
                for entry_id in ids
            ]
        except (DerivedSensorNotFoundError, ValueError) as exc:
            return _tool_error(exc)
        response: JsonObjectType = (
            dict(results[0])
            if "entry_id" in args
            else {"deleted": cast(JsonValueType, ids)}
        )
        if mirror.is_mirror_enabled(hass) and befores:
            mirrored = []
            for entry_id, before in befores.items():
                mirror_result = await mirror.mirror_write(
                    hass,
                    path=_derived_sensor_mirror_path(before["domain"], entry_id),
                    content_before=json.dumps(before),
                    content_after=json.dumps({"deleted": True, "entry_id": entry_id}),
                    content_type="json",
                )
                mirrored.append(_mirror_result_payload(mirror_result))
            response["mirror"] = (
                mirrored[0] if "entry_id" in args else cast(JsonValueType, mirrored)
            )
        return response


class ReloadDerivedSensorTool(GatedTool):
    """Force a derived-sensor entry to recompute without changing its options."""

    name = "reload_derived_sensor"
    description = (
        "Reload a calculated/derived sensor helper by its entry id, without "
        "changing any of its options - e.g. after an entity it reads from "
        "was reconfigured elsewhere. update_derived_sensor already reloads "
        "automatically when it changes options; use this only when nothing "
        "about the helper itself needs to change."
    )
    parameters = vol.Schema({vol.Required("entry_id"): str})

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Reload the derived-sensor entry."""
        try:
            return await derived_sensor_manager.reload_derived_sensor(
                hass, tool_input.tool_args["entry_id"]
            )
        except DerivedSensorNotFoundError as exc:
            return _tool_error(exc)


class ListTemplateEntitiesTool(GatedTool):
    """List YAML `template:` entities across configuration.yaml and packages."""

    name = "list_template_entities"
    description = (
        "List every entity defined via YAML `template:` blocks (the "
        "modern, trigger-based syntax), across configuration.yaml and "
        "every packages/*.yaml file - the other half of issue #13's ask "
        "alongside list_derived_sensors, for Template sensors/binary_sensors/"
        "etc. specifically. Covers all template-supported platforms "
        "(sensor, binary_sensor, number, switch, ...). Entities without "
        "their own unique_id are listed here too, but note they aren't "
        "addressable by create/update/delete_template_entity - only ones "
        "with a unique_id are. Does not cover the config-entry-based "
        "Template *helper* (UI-created) - not yet supported by any tool "
        "here."
    )
    parameters = vol.Schema({})

    def __init__(self, template_yaml_manager: TemplateYamlManager) -> None:
        """Init with the TemplateYamlManager backing this tool."""
        self._manager = template_yaml_manager

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """List every template: entity."""
        return cast(JsonObjectType, {"items": await self._manager.list_entities()})


class GetTemplateEntityTool(GatedTool):
    """Read one YAML template: entity's config by unique_id."""

    name = "get_template_entity"
    description = (
        "Read a YAML `template:` entity's current config by its unique_id "
        "(from list_template_entities)."
    ) + _MISREAD_VALUES_NOTE
    parameters = vol.Schema({vol.Required("unique_id"): str})

    def __init__(self, template_yaml_manager: TemplateYamlManager) -> None:
        """Init with the TemplateYamlManager backing this tool."""
        self._manager = template_yaml_manager

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Read the template entity."""
        try:
            location, config = await self._manager.get_entity(
                tool_input.tool_args["unique_id"]
            )
        except (TemplateEntityNotFoundError, DuplicateTemplateUniqueIdError) as exc:
            return _tool_error(exc)
        return {
            "file_path": location.file_path,
            "is_package": location.is_package,
            "platform": location.platform,
            "config": config,
            "misread_values": cast(JsonValueType, find_misread_scalars(config)),
        }


async def _template_entity_status(
    hass: HomeAssistant,
    manager: TemplateYamlManager,
    result: Any,
    unique_id: str,
) -> JsonObjectType:
    """The entity_id a written template entity came up as - or, when the
    reload ran but it didn't come up, a warning instead of a bare
    reloaded: true (issue #125)."""
    block_unique_id = await hass.async_add_executor_job(
        manager.block_unique_id, result.content_after, result.location.block_index
    )
    entity_id = manager.loaded_entity_id(
        result.location.platform, unique_id, block_unique_id
    )
    if entity_id is not None:
        return {"entity_id": entity_id}
    if result.reloaded:  # not reloaded: "reloaded": false already says so
        return {
            "warning": (
                "Written and reloaded, but the entity didn't come up - HA "
                "rejected its config on reload. Check get_logs for the "
                "template error, then fix it with update_template_entity or "
                "remove it with delete_template_entity."
            )
        }
    return {}


class CreateTemplateEntityTool(WriteGatedTool):
    """Create a new YAML template: entity in its own new template: block."""

    name = "create_template_entity"
    description = (
        "Create a new YAML `template:` entity (sensor, binary_sensor, "
        "number, switch, ...) - always in a brand new template: block, "
        "never merged into an existing one (see get_template_entity's "
        "sibling entities if you want to hand-craft a shared trigger "
        "block instead). 'config' must include a 'unique_id' - required "
        "for every write this tool family does, since a template entity "
        "otherwise has no way to be addressed again by update/"
        "delete_template_entity. 'package' must name an existing "
        "packages/*.yaml file (relative to packages/) - required, no "
        "default-file fallback: configuration.yaml itself is read-only "
        "under this integration's default security policy, so new "
        "entities can only be created in a package. 'triggers' is "
        "optional: a list of trigger objects (e.g. {'trigger': 'state', "
        "'entity_id': 'sensor.x'}), not strings, making the new block "
        "trigger-based; they're checked with HA's own trigger validation "
        "before anything is written. If the entity doesn't come up after "
        "the reload, the result says so in 'warning'."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("platform"): str,
            vol.Required("config"): dict,
            vol.Required("package"): str,
            # [dict], not a bare list: a bare list is advertised to MCP
            # clients as an array of strings (issue #125).
            vol.Optional("triggers"): [dict],
        }
    )

    def __init__(self, template_yaml_manager: TemplateYamlManager) -> None:
        """Init with the TemplateYamlManager backing this tool."""
        self._manager = template_yaml_manager

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Create the template entity."""
        args = tool_input.tool_args
        try:
            result = await self._manager.create_entity(
                args["platform"],
                args["config"],
                package=args["package"],
                triggers=args.get("triggers"),
            )
        except (
            ValueError,
            DuplicateTemplateUniqueIdError,
            TemplateEntityNotFoundError,
        ) as exc:
            return _tool_error(exc)
        response: JsonObjectType = {
            "file_path": result.location.file_path,
            "platform": result.location.platform,
            "reloaded": result.reloaded,
        }
        response.update(
            await _template_entity_status(
                hass, self._manager, result, str(args["config"]["unique_id"])
            )
        )
        return await _mirror_file_write(
            hass,
            response,
            file_path=result.location.file_path,
            content_before=result.content_before,
            content_after=result.content_after,
        )

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Flag triggers HA would reject before the user confirms them."""
        try:
            await validate_triggers(hass, tool_input.tool_args.get("triggers"))
        except ValueError as exc:
            return {"problems": str(exc)}
        return {}

    @override
    async def _dry_run_mirror(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> mirror.MirrorResult | None:
        """See WriteAutomationTool's identical override - issue #35."""
        args = tool_input.tool_args
        try:
            result = await self._manager.create_entity(
                args["platform"],
                args["config"],
                package=args["package"],
                triggers=args.get("triggers"),
                dry_run=True,
            )
        except (
            ValueError,
            DuplicateTemplateUniqueIdError,
            TemplateEntityNotFoundError,
        ) as exc:
            return mirror.MirrorResult(mirrored=False, reason=str(exc))
        return await mirror.mirror_dry_run(
            hass,
            path=result.location.file_path,
            content_before=result.content_before,
            content_after=result.content_after,
            kind="template_entity",
            entity_id=str(args["config"].get("unique_id", "unknown")),
        )


class UpdateTemplateEntityTool(WriteGatedTool):
    """Update an existing YAML template: entity's config in place."""

    name = "update_template_entity"
    description = (
        "Update an existing YAML `template:` entity's config by its "
        "unique_id (from list_template_entities). config replaces only that "
        "entity's own dict - sibling entities and the block's triggers/"
        "conditions/variables are left untouched. To change the block "
        "itself - a trigger-based block's triggers, conditions, variables or "
        "actions - pass those instead of (or with) config: each replaces "
        "the block's key in place (null removes conditions/variables/"
        "actions), checked with HA's own validation, then reloaded. That "
        "changes every entity in the block - the preview lists them all - "
        "and keeps their state (accumulators, counters, attributes), unlike "
        "delete + create. Turning a state-based block into a trigger-based "
        "one (or back) is refused. To change an entity's "
        "unique_id, use delete_template_entity + create_template_entity "
        "instead (a rename is really two operations, not supported "
        "directly here). Fails with a permission error if the entity "
        "lives in configuration.yaml itself, which is read-only under "
        "this integration's default security policy - only "
        "package-defined entities can be updated this way."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("unique_id"): str,
            vol.Optional("config"): dict,
            vol.Optional("triggers"): [dict],
            vol.Optional("conditions"): vol.Any([dict], None),
            vol.Optional("variables"): vol.Any(dict, None),
            vol.Optional("actions"): vol.Any([dict], None),
        }
    )

    def __init__(self, template_yaml_manager: TemplateYamlManager) -> None:
        """Init with the TemplateYamlManager backing this tool."""
        self._manager = template_yaml_manager

    @staticmethod
    def _block(args: dict[str, Any]) -> dict[str, Any] | None:
        return {field: args[field] for field in BLOCK_FIELDS if field in args} or None

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """For a block edit: every entity it changes, and the keys before
        and after - or why it's refused."""
        args = tool_input.tool_args
        block = self._block(args)
        if not block:
            return {}
        try:
            result = await self._manager.update_entity(
                args["unique_id"], args.get("config"), block=block, dry_run=True
            )
        except (
            ValueError,
            TemplateEntityNotFoundError,
            DuplicateTemplateUniqueIdError,
        ) as exc:
            return {"problems": str(exc)}
        return {"block": cast(JsonValueType, result.block)}

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Update the template entity and/or its block."""
        args = tool_input.tool_args
        try:
            result = await self._manager.update_entity(
                args["unique_id"], args.get("config"), block=self._block(args)
            )
        except (
            ValueError,
            TemplateEntityNotFoundError,
            DuplicateTemplateUniqueIdError,
        ) as exc:
            return _tool_error(exc)
        response: JsonObjectType = {
            "file_path": result.location.file_path,
            "platform": result.location.platform,
            "reloaded": result.reloaded,
        }
        if result.block is not None:
            response["block"] = cast(JsonValueType, result.block)
        # An edit HA rejects on reload drops the entity just as silently.
        response.update(
            await _template_entity_status(
                hass, self._manager, result, args["unique_id"]
            )
        )
        return await _mirror_file_write(
            hass,
            response,
            file_path=result.location.file_path,
            content_before=result.content_before,
            content_after=result.content_after,
        )

    @override
    async def _dry_run_mirror(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> mirror.MirrorResult | None:
        """See WriteAutomationTool's identical override - issue #35."""
        args = tool_input.tool_args
        try:
            result = await self._manager.update_entity(
                args["unique_id"], args["config"], dry_run=True
            )
        except (
            ValueError,
            TemplateEntityNotFoundError,
            DuplicateTemplateUniqueIdError,
        ) as exc:
            return mirror.MirrorResult(mirrored=False, reason=str(exc))
        return await mirror.mirror_dry_run(
            hass,
            path=result.location.file_path,
            content_before=result.content_before,
            content_after=result.content_after,
            kind="template_entity",
            entity_id=args["unique_id"],
        )


class DeleteTemplateEntityTool(WriteGatedTool):
    """Delete a YAML template: entity by unique_id."""

    name = "delete_template_entity"
    description = (
        "Delete a YAML `template:` entity by its unique_id. Cleans up "
        "after itself - removes the platform key if that empties it, and "
        "the whole template: block if that empties it too. Same "
        "read-only-configuration.yaml restriction as "
        "update_template_entity. To delete several at once, pass "
        "unique_ids (a list) instead of unique_id: one confirmation, and "
        "every id is resolved before any is deleted."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {vol.Optional("unique_id"): str, vol.Optional("unique_ids"): [str]}
    )

    def __init__(self, template_yaml_manager: TemplateYamlManager) -> None:
        """Init with the TemplateYamlManager backing this tool."""
        self._manager = template_yaml_manager

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Delete the template entity (or entities)."""
        args = tool_input.tool_args
        try:
            ids = _one_or_many(args, "unique_id", "unique_ids")
            # Resolve every id before deleting any: an unknown or duplicated
            # unique_id refuses the whole batch.
            for unique_id in ids:
                await self._manager.find_entity(unique_id)
        except (
            TemplateEntityNotFoundError,
            DuplicateTemplateUniqueIdError,
            ValueError,
        ) as exc:
            return _tool_error(exc)
        work = (
            self._delete_one(hass, ids[0])
            if "unique_id" in args
            else self._delete_many(hass, ids)
        )
        return await _run_to_completion(hass, work, "ha_dev_tools template delete")

    async def _delete_one(self, hass: HomeAssistant, unique_id: str) -> JsonObjectType:
        try:
            result = await self._manager.delete_entity(unique_id)
        except (TemplateEntityNotFoundError, DuplicateTemplateUniqueIdError) as exc:
            return _tool_error(exc)
        response: JsonObjectType = {
            "file_path": result.location.file_path,
            "platform": result.location.platform,
            "reloaded": result.reloaded,
        }
        return await _mirror_file_write(
            hass,
            response,
            file_path=result.location.file_path,
            content_before=result.content_before,
            content_after=result.content_after,
        )

    async def _delete_many(self, hass: HomeAssistant, ids: list[str]) -> JsonObjectType:
        """All removals applied in one write per file and one reload (issue
        #127); whatever was written is mirrored, even if a later file failed."""
        error: str | None = None
        try:
            results = await self._manager.delete_entities(ids)
        except (TemplateEntityNotFoundError, DuplicateTemplateUniqueIdError) as exc:
            return _tool_error(exc)
        except TemplateBatchError as exc:
            results, error = exc.written, str(exc)
        deleted = [unique_id for result in results for unique_id in result.unique_ids]
        response: JsonObjectType = {
            "deleted": cast(JsonValueType, deleted),
            "files": cast(JsonValueType, sorted(r.file_path for r in results)),
            "reloaded": bool(results) and all(r.reloaded for r in results),
        }
        if error is not None:
            response["error"] = f"Stopped at a failed write - {error}"
            response["not_deleted"] = cast(
                JsonValueType, [i for i in ids if i not in deleted]
            )
        if mirror.is_mirror_enabled(hass):
            response["mirror"] = [
                _mirror_result_payload(
                    await mirror.mirror_write(
                        hass,
                        path=result.file_path,
                        content_before=result.content_before,
                        content_after=result.content_after,
                    )
                )
                for result in results
            ]
        return response

    @override
    async def _dry_run_mirror(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> mirror.MirrorResult | None:
        """See WriteAutomationTool's identical override - issue #35."""
        unique_id = tool_input.tool_args.get("unique_id")
        if unique_id is None:
            return _NO_BATCH_DRY_RUN_MIRROR
        try:
            result = await self._manager.delete_entity(unique_id, dry_run=True)
        except (TemplateEntityNotFoundError, DuplicateTemplateUniqueIdError) as exc:
            return mirror.MirrorResult(mirrored=False, reason=str(exc))
        return await mirror.mirror_dry_run(
            hass,
            path=result.location.file_path,
            content_before=result.content_before,
            content_after=result.content_after,
            kind="template_entity",
            entity_id=unique_id,
        )


class ListDashboardsTool(GatedTool):
    """List every configured dashboard - see dashboard_manager.py."""

    name = "list_dashboards"
    description = (
        "List every configured Lovelace dashboard (title, url_path, icon, "
        "mode, require_admin, show_in_sidebar) - both storage-mode and "
        "YAML-mode, same coverage get_dashboard already has per-dashboard. "
        "Use this before get_dashboard when checking every dashboard for "
        "something (e.g. leftover references to a renamed/removed entity) "
        "- otherwise a non-default dashboard whose url_path you don't "
        "already know is silently missed (see issue #77). Pass a result's "
        "url_path straight to get_dashboard/write_dashboard."
    )
    parameters = vol.Schema({})

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """List every dashboard."""
        try:
            user = await helper_manager.resolve_user(hass, llm_context)
            dashboards = await dashboard_manager.list_dashboards(hass, user)
        except (UnresolvedUserError, WebSocketCommandError) as exc:
            return _tool_error(exc)
        return cast(JsonObjectType, {"dashboards": dashboards})


class GetDashboardTool(GatedTool):
    """Read a dashboard's config - works in both storage and YAML mode."""

    name = "get_dashboard"
    description = (
        "Read a Lovelace dashboard's config (views/cards). Omit url_path "
        "for the default dashboard, or pass an additional dashboard's "
        "url_path. Works whether the dashboard is UI/storage-managed or "
        "YAML-mode."
    )
    parameters = vol.Schema({vol.Optional("url_path"): str})

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Read the dashboard config."""
        try:
            user = await helper_manager.resolve_user(hass, llm_context)
            config = await dashboard_manager.get_dashboard(
                hass, user, url_path=tool_input.tool_args.get("url_path")
            )
        except (UnresolvedUserError, WebSocketCommandError) as exc:
            return _tool_error(exc)
        return config


class WriteDashboardTool(WriteGatedTool):
    """Write a dashboard's config - storage mode only."""

    name = "write_dashboard"
    description = (
        "Save a Lovelace dashboard's config (views/cards). Storage-mode "
        "dashboards only - HA hard-rejects saving YAML-mode dashboards "
        "through this path (get_dashboard still works for those, just "
        "not this). Omit url_path for the default dashboard."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {vol.Required("config"): dict, vol.Optional("url_path"): str}
    )

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Write the dashboard config."""
        args = tool_input.tool_args
        url_path = args.get("url_path")
        # Storage key convention confirmed directly against home-assistant/
        # core's lovelace/dashboard.py: CONFIG_STORAGE_KEY_DEFAULT = "lovelace"
        # for the default dashboard, CONFIG_STORAGE_KEY = "lovelace.{}" (the
        # dashboard's id, which is its url_path for storage-mode dashboards)
        # for any other.
        storage_path = f".storage/lovelace{f'.{url_path}' if url_path else ''}"
        try:
            content_before = await _read_storage_file(hass, storage_path)
            user = await helper_manager.resolve_user(hass, llm_context)
            await dashboard_manager.write_dashboard(
                hass, user, args["config"], url_path=url_path
            )
        except (
            UnresolvedUserError,
            YamlModeDashboardError,
            WebSocketCommandError,
        ) as exc:
            return _tool_error(exc)
        content_after = await _read_storage_file(hass, storage_path)
        response: JsonObjectType = {"saved": True, "url_path": url_path}
        if content_after is not None:
            response = await _mirror_file_write(
                hass,
                response,
                file_path=storage_path,
                content_before=content_before,
                content_after=content_after,
                content_type="json",
            )
        return response


class GetEnergyConfigTool(GatedTool):
    """Read the Energy dashboard's own source config - see energy_manager.py."""

    name = "get_energy_config"
    description = (
        "Read the Energy dashboard's own source configuration (grid "
        "consumption/return entities, solar production per source, "
        "battery in/out, gas/water sources, cost/compensation settings) - "
        "a distinct HA subsystem from Lovelace dashboards, not reachable "
        "through get_dashboard. Fails with a not_found error if the "
        "Energy dashboard has never been configured."
    )
    parameters = vol.Schema({})

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Read the Energy dashboard's config."""
        try:
            user = await helper_manager.resolve_user(hass, llm_context)
            config = await energy_manager.get_energy_config(hass, user)
        except (UnresolvedUserError, WebSocketCommandError) as exc:
            return _tool_error(exc)
        return config


class WriteEnergyConfigTool(WriteGatedTool):
    """Update the Energy dashboard's own source config - see energy_manager.py."""

    name = "write_energy_config"
    description = (
        "Update the Energy dashboard's source configuration. Each of "
        "energy_sources/device_consumption/device_consumption_water, if "
        "supplied, wholesale-replaces that section - omit a field to "
        "leave it untouched rather than clearing it. Call "
        "get_energy_config first to see the current full config (and the "
        "exact shape each section expects) before editing one section."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Optional("energy_sources"): [dict],
            vol.Optional("device_consumption"): [dict],
            vol.Optional("device_consumption_water"): [dict],
        }
    )

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Write the Energy dashboard's config."""
        args = tool_input.tool_args
        storage_path = ".storage/energy"
        try:
            content_before = await _read_storage_file(hass, storage_path)
            user = await helper_manager.resolve_user(hass, llm_context)
            result = await energy_manager.write_energy_config(
                hass,
                user,
                energy_sources=args.get("energy_sources"),
                device_consumption=args.get("device_consumption"),
                device_consumption_water=args.get("device_consumption_water"),
            )
        except (UnresolvedUserError, WebSocketCommandError) as exc:
            return _tool_error(exc)
        content_after = await _read_storage_file(hass, storage_path)
        response: JsonObjectType = {"saved": True, "config": result}
        if content_after is not None:
            response = await _mirror_file_write(
                hass,
                response,
                file_path=storage_path,
                content_before=content_before,
                content_after=content_after,
                content_type="json",
            )
        return response


class AuditAutomationsTool(GatedTool):
    """Static analysis over every known automation for latent reliability bugs."""

    name = "audit_automations"
    description = (
        "Audit every automation (default file and all packages) for "
        "duplicate ids across files, and for triggers/conditions/actions "
        "referencing an entity that is currently unavailable or unknown - "
        "the class of bug that fails silently with no error anywhere. "
        "Also reports 'misread_values': unquoted values Home Assistant "
        "reads as a different type than written (e.g. `before: 17:00:00` "
        "is read as 61200, `state: off` as False), which get_automation "
        "and check_config don't show. Fix one by writing the automation "
        "again with write_automation, which quotes such values. "
        "Does not yet detect overlapping-trigger race conditions or "
        "unhandled rest_command/shell_command failures (see the result's "
        "'note' field). 'template_errors' lists templates HA's own "
        "engine can't compile (they'd only fail when the automation "
        "fires), and 'constant_conditions' conditions that always/never "
        "pass ({{ true }}/{{ false }} templates, an empty and/or)."
    )
    parameters = vol.Schema({})

    def __init__(self, automation_manager: AutomationManager) -> None:
        """Init with the AutomationManager backing this tool."""
        self._manager = automation_manager

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Run the audit."""
        return await audit_manager.audit_automations(hass, self._manager)


class ListTracesTool(GatedTool):
    """Stored automation/script runs - see trace_manager.py."""

    name = "list_traces"
    description = (
        "List the stored runs (traces) of automations or scripts, newest "
        "first - the automation editor's Traces view. Home Assistant keeps "
        "the last few runs of each (5 by default), across restarts. Each "
        "has its run_id, start/finish, the trigger that fired, "
        "script_execution (how it ended: finished, failed_conditions, "
        "error, cancelled, ...), last_step and any error. Pass entity_id "
        "(automation.x / script.x) or domain plus item_id (an automation's "
        "config id, a script's object id) for one item, or just domain for "
        "all of them. errors_only keeps runs that raised an error. "
        "include_not_triggered also lists the trigger checks that didn't "
        "fire (recent Home Assistant versions only). get_trace reads one "
        "run's steps. Read-only."
    )
    parameters = vol.Schema(
        {
            vol.Optional("entity_id"): str,
            vol.Optional("domain", default="automation"): vol.In(trace_manager.DOMAINS),
            vol.Optional("item_id"): str,
            vol.Optional("errors_only", default=False): bool,
            vol.Optional("include_not_triggered", default=False): bool,
            vol.Optional("limit", default=trace_manager.DEFAULT_LIST_LIMIT): vol.All(
                int, vol.Range(min=1, max=500)
            ),
        }
    )

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """List traces."""
        args = tool_input.tool_args
        try:
            domain, item_id = trace_manager.resolve_item(
                hass,
                domain=args.get("domain"),
                item_id=args.get("item_id"),
                entity_id=args.get("entity_id"),
            )
            user = await helper_manager.resolve_user(hass, llm_context)
            return cast(
                JsonObjectType,
                await trace_manager.list_traces(
                    hass,
                    user,
                    domain=domain,
                    item_id=item_id,
                    errors_only=args.get("errors_only", False),
                    include_not_triggered=args.get("include_not_triggered", False),
                    limit=args.get("limit", trace_manager.DEFAULT_LIST_LIMIT),
                ),
            )
        except (
            trace_manager.TraceNotFoundError,
            UnresolvedUserError,
            ValueError,
            WebSocketCommandError,
        ) as exc:
            return _tool_error(exc)


class GetTraceTool(GatedTool):
    """One automation/script run in full - see trace_manager.py."""

    name = "get_trace"
    description = (
        "Read one run (trace) of an automation or script step by step - "
        "the most direct way to see why it did or didn't do something. "
        "Pass entity_id (automation.x / script.x) or domain plus item_id, "
        "and a run_id from list_traces (omitted: the latest run). Returns "
        "the run's summary (trigger, script_execution, error), its context "
        "(parent_id is set when something else - e.g. another automation "
        "- caused it) and every trigger/condition/action step in the order "
        "it ran: path (e.g. 'condition/0', 'action/1/then/0'), timestamp, "
        "result (a condition's true/false, a service call's params, which "
        "choose branch ran, ...), error and template_errors. A step with "
        "child_id ran a script - pass that to get_trace for its own steps. "
        "include_variables (default true) adds what each step changed, "
        "e.g. the trigger's from_state/to_state; include_config adds the "
        "automation's config as it was when this run happened. Read-only."
    )
    parameters = vol.Schema(
        {
            vol.Optional("entity_id"): str,
            vol.Optional("domain", default="automation"): vol.In(trace_manager.DOMAINS),
            vol.Optional("item_id"): str,
            vol.Optional("run_id"): str,
            vol.Optional("include_variables", default=True): bool,
            vol.Optional("include_config", default=False): bool,
        }
    )

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Read one trace."""
        args = tool_input.tool_args
        try:
            domain, item_id = trace_manager.resolve_item(
                hass,
                domain=args.get("domain"),
                item_id=args.get("item_id"),
                entity_id=args.get("entity_id"),
            )
            if item_id is None:
                raise ValueError("Pass entity_id, or item_id")
            user = await helper_manager.resolve_user(hass, llm_context)
            return cast(
                JsonObjectType,
                await trace_manager.get_trace(
                    hass,
                    user,
                    domain=domain,
                    item_id=item_id,
                    run_id=args.get("run_id"),
                    include_variables=args.get("include_variables", True),
                    include_config=args.get("include_config", False),
                ),
            )
        except (
            trace_manager.TraceNotFoundError,
            UnresolvedUserError,
            ValueError,
            WebSocketCommandError,
        ) as exc:
            return _tool_error(exc)


class TriggerAutomationTool(WriteGatedTool):
    """Run an existing automation on demand - see service_call_manager.py."""

    name = "trigger_automation"
    description = (
        "Run an existing automation immediately by its config id (not its "
        "entity_id - automation.<id> is never valid, see get_automation), "
        "the same as the UI's 'Run actions' button. Only automations "
        "already defined in reviewed automations.yaml/packages can be "
        "triggered this way - unlike a generic service-call tool, this "
        "never accepts an arbitrary entity_id or service. Useful for "
        "one-shot testing without a throwaway automation edit cycle (see "
        "issue #76). skip_condition defaults to true (conditions in the "
        "automation are not evaluated, matching the UI default). The run "
        "is started and left to finish on its own, like the UI button - a "
        "client timeout never cuts it off. It returns right away with "
        "finished=false and the run's context_id; wait_seconds (up to 50) "
        "waits that long for a short run to end and reports finished and "
        "any error. Follow a longer run with list_traces / get_trace or "
        "get_logbook. Fails clearly if no live automation.* entity exists "
        "yet for this id (e.g. never reloaded since being added)."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("automation_id"): str,
            vol.Optional("skip_condition", default=True): bool,
            vol.Optional("wait_seconds", default=0): vol.All(
                vol.Coerce(float),
                vol.Range(min=0, max=service_call_manager.MAX_WAIT_SECONDS),
            ),
        }
    )

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Resolve the automation's live entity and trigger it."""
        args = tool_input.tool_args
        try:
            run = await service_call_manager.trigger_automation(
                hass,
                args["automation_id"],
                skip_condition=args.get("skip_condition", True),
                wait_seconds=args.get("wait_seconds", 0),
            )
        except service_call_manager.AutomationNotRunningError as exc:
            return _tool_error(exc)
        return {"triggered": True, **run}


class SetNumberValueTool(WriteGatedTool):
    """Set a number/input_number entity's value - see service_call_manager.py."""

    name = "set_number_value"
    description = (
        "Set a `number` or `input_number` entity's value directly - e.g. "
        "to test a single write in isolation (an EMHASS battery-schedule "
        "slot, a Modbus-backed inverter setting exposed as a number "
        "entity) without a full automation edit cycle (see issue #76). "
        "Deliberately scoped to just these two domains, not a generic "
        "service-call tool - refuses any other entity domain rather than "
        "guessing which service to call."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {vol.Required("entity_id"): str, vol.Required("value"): vol.Coerce(float)}
    )

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Call the entity's own domain-specific set_value service."""
        args = tool_input.tool_args
        try:
            await service_call_manager.set_number_value(
                hass, args["entity_id"], args["value"]
            )
        except (
            service_call_manager.InvalidEntityDomainError,
            service_call_manager.EntityNotFoundError,
        ) as exc:
            return _tool_error(exc)
        return {"entity_id": args["entity_id"], "value": args["value"]}


class SetBooleanValueTool(WriteGatedTool):
    """Turn an input_boolean helper on/off - see service_call_manager.py."""

    name = "set_boolean_value"
    description = (
        "Turn an `input_boolean` helper on or off directly. Scoped to just "
        "this virtual helper domain - never `switch` or any other domain "
        "that could be a real-world actuator (see issue #76's risk "
        "discussion); those would need their own separate security review "
        "before getting a write tool."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {vol.Required("entity_id"): str, vol.Required("state"): bool}
    )

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Call input_boolean.turn_on or turn_off."""
        args = tool_input.tool_args
        try:
            await service_call_manager.set_boolean_value(
                hass, args["entity_id"], args["state"]
            )
        except (
            service_call_manager.InvalidEntityDomainError,
            service_call_manager.EntityNotFoundError,
        ) as exc:
            return _tool_error(exc)
        return {"entity_id": args["entity_id"], "state": args["state"]}


class ListScriptsTool(GatedTool):
    """List every script across scripts.yaml and packages."""

    name = "list_scripts"
    description = (
        "List every script (the default scripts.yaml and every "
        "packages/*.yaml file), each with its id, source file, and full "
        "config. Same layout-aware, package-safe file resolution as "
        "write_script - a plain file read can silently miss "
        "package-defined scripts."
    )
    parameters = vol.Schema({})

    def __init__(self, script_manager: ScriptManager) -> None:
        """Init with the ScriptManager backing this tool."""
        self._manager = script_manager

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """List every script."""
        items = [
            {
                "script_id": script_id,
                "file_path": location.file_path,
                "is_package": location.is_package,
                "config": to_json_safe(config),
            }
            for location, script_id, config in await self._manager.all_scripts()
        ]
        return cast(JsonObjectType, {"items": items})


class GetScriptTool(GatedTool):
    """Layout-aware script read - resolves the file that actually defines it."""

    name = "get_script"
    description = (
        "Read a script's config by id, resolving which file actually "
        "defines it (the default scripts.yaml, or a packages/*.yaml "
        "file) - a plain file read can silently miss package-defined "
        "scripts. Fails clearly if the id isn't found or is defined in "
        "more than one file, rather than guessing."
    ) + _MISREAD_VALUES_NOTE
    parameters = vol.Schema({vol.Required("script_id"): str})

    def __init__(self, script_manager: ScriptManager) -> None:
        """Init with the ScriptManager backing this tool."""
        self._manager = script_manager

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Resolve and return a script's config and source file."""
        try:
            location, config = await self._manager.get_script(
                tool_input.tool_args["script_id"]
            )
        except (ScriptNotFoundError, DuplicateScriptIdError) as exc:
            return _tool_error(exc)
        return {
            "file_path": location.file_path,
            "is_package": location.is_package,
            "config": to_json_safe(config),
            "misread_values": cast(JsonValueType, find_misread_scalars(config)),
        }


class WriteScriptTool(WriteGatedTool):
    """Layout-aware, package-safe script write - see script_manager.py."""

    name = "write_script"
    description = (
        "Create or update a script. If the id already exists, it's "
        "updated in place in whichever file actually defines it (default "
        "file or a package) - never blindly appended to scripts.yaml, "
        "which would create a silent duplicate for package-defined "
        "scripts. For a brand new script, pass 'package' to target an "
        "existing packages/*.yaml file, or omit it for the default "
        "scripts.yaml. Always reloads scripts afterward - never requires "
        "a restart. Pass expected_hash (from get_file_metadata or a prior "
        "read) to detect concurrent edits."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("script_id"): str,
            vol.Required("config"): dict,
            vol.Optional("package"): str,
            vol.Optional("expected_hash"): str,
        }
    )

    def __init__(self, script_manager: ScriptManager) -> None:
        """Init with the ScriptManager backing this tool."""
        self._manager = script_manager

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Write the script through its correct file and reload."""
        args = tool_input.tool_args
        try:
            result = await self._manager.write_script(
                args["script_id"],
                args["config"],
                package=args.get("package"),
                expected_hash=args.get("expected_hash"),
            )
        except (ScriptNotFoundError, DuplicateScriptIdError, ValueError) as exc:
            return _tool_error(exc)
        response: JsonObjectType = {
            "file_path": result.location.file_path,
            "is_package": result.location.is_package,
            # HA's own error if it refused to set the item up after the
            # reload - otherwise invisible until someone opens Repairs.
            "setup_error": config_tools.setup_error(hass, "script", args["script_id"]),
        }
        return await _mirror_file_write(
            hass,
            response,
            file_path=result.location.file_path,
            content_before=result.content_before,
            content_after=result.content_after,
        )

    @override
    async def _dry_run_mirror(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> mirror.MirrorResult | None:
        """Compute this call's would-be script content and mirror it to a
        proposed/script-<id> branch (same pattern as write_automation's
        equivalent hook, issue #35) - reuses write_script's own
        resolve+build logic via dry_run=True."""
        args = tool_input.tool_args
        try:
            result = await self._manager.write_script(
                args["script_id"],
                args["config"],
                package=args.get("package"),
                expected_hash=args.get("expected_hash"),
                dry_run=True,
            )
        except (ScriptNotFoundError, DuplicateScriptIdError, ValueError) as exc:
            return mirror.MirrorResult(mirrored=False, reason=str(exc))
        return await mirror.mirror_dry_run(
            hass,
            path=result.location.file_path,
            content_before=result.content_before,
            content_after=result.content_after,
            kind="script",
            entity_id=args["script_id"],
        )


# rest_command and shell_command share one layout, so one pair of tools
# covers both via an optional `domain` (issue #100) - no extra tools.
_COMMAND_DOMAIN_SCHEMA = vol.In(COMMAND_KEYS)


class ListRestCommandsTool(GatedTool):
    """List every rest_command across configuration.yaml and packages."""

    name = "list_rest_commands"
    description = (
        "List every rest_command (configuration.yaml and every "
        "packages/*.yaml file), each with its id, source file, and full "
        "config. Same layout-aware, package-safe file resolution as "
        "get_automation/get_script - a plain file read can silently miss "
        "package-defined rest_commands. Read-only. Pass "
        "domain='shell_command' to list shell_command entries instead "
        "(their config is the command string itself)."
    )
    parameters = vol.Schema({vol.Optional("domain"): _COMMAND_DOMAIN_SCHEMA})

    def __init__(self, rest_command_manager: RestCommandManager) -> None:
        """Init with the RestCommandManager backing this tool."""
        self._manager = rest_command_manager

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """List every rest_command."""
        items = [
            {
                "rest_command_id": rest_command_id,
                "file_path": location.file_path,
                "is_package": location.is_package,
                "config": to_json_safe(config),
            }
            for location, rest_command_id, config in await self._manager.all_rest_commands(
                tool_input.tool_args.get("domain", REST_COMMAND_KEY)
            )
        ]
        return cast(JsonObjectType, {"items": items})


class GetRestCommandTool(GatedTool):
    """Layout-aware rest_command read - resolves the file that actually defines it."""

    name = "get_rest_command"
    description = (
        "Read a rest_command's config (url, method, headers, payload, "
        "...) by id, resolving which file actually defines it "
        "(configuration.yaml or a packages/*.yaml file) - a plain file "
        "read can silently miss package-defined rest_commands. Fails "
        "clearly if the id isn't found or is defined in more than one "
        "file, rather than guessing. Read-only - no write_rest_command "
        "yet. Pass domain='shell_command' to read a shell_command entry "
        "by id instead (its config is the command string itself) - also "
        "read-only, deliberately: shell_command runs raw shell commands."
    )
    parameters = vol.Schema(
        {
            vol.Required("rest_command_id"): str,
            vol.Optional("domain"): _COMMAND_DOMAIN_SCHEMA,
        }
    )

    def __init__(self, rest_command_manager: RestCommandManager) -> None:
        """Init with the RestCommandManager backing this tool."""
        self._manager = rest_command_manager

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Resolve and return a rest_command's config and source file."""
        try:
            location, config = await self._manager.get_rest_command(
                tool_input.tool_args["rest_command_id"],
                tool_input.tool_args.get("domain", REST_COMMAND_KEY),
            )
        except (RestCommandNotFoundError, DuplicateRestCommandIdError) as exc:
            return _tool_error(exc)
        return {
            "file_path": location.file_path,
            "is_package": location.is_package,
            "config": to_json_safe(config),
        }


@dataclass(slots=True, kw_only=True)
class DevToolsAPI(llm.API):
    """The ha_dev_tools LLM API - holds the backing services real tools are built from."""

    log_manager: LogManager
    automation_manager: AutomationManager
    script_manager: ScriptManager
    template_yaml_manager: TemplateYamlManager
    rest_command_manager: RestCommandManager

    @override
    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        """Return the instance of the API."""
        return llm.APIInstance(
            self,
            API_PROMPT,
            llm_context,
            tools=[
                DevToolsPingTool(),
                FindEntitiesTool(),
                EntityHealthReportTool(),
                DeleteEntityTool(),
                DeleteEntitiesTool(),
                UpdateEntitiesTool(),
                ListMqttTopicsTool(),
                RenderTemplateTool(),
                ValidateTemplateTool(),
                GetLogsTool(self.log_manager),
                GetEntityHistoryTool(),
                GetLogbookTool(),
                ListStatisticsTool(),
                GetStatisticsTool(),
                ClearStatisticsTool(),
                MigrateStatisticsTool(),
                MergeStatisticsTool(),
                DeriveStatisticsTool(),
                RestoreStatisticsTool(),
                ListAddonsTool(),
                GetAddonLogsTool(),
                CheckConfigTool(),
                GetConfigFileTool(),
                ReloadDomainTool(),
                GetAutomationTool(self.automation_manager),
                WriteAutomationTool(self.automation_manager),
                DeleteAutomationTool(self.automation_manager),
                AuditAutomationsTool(self.automation_manager),
                ListTracesTool(),
                GetTraceTool(),
                TriggerAutomationTool(),
                SetNumberValueTool(),
                SetBooleanValueTool(),
                ListScriptsTool(self.script_manager),
                GetScriptTool(self.script_manager),
                WriteScriptTool(self.script_manager),
                ListHelpersTool(),
                CreateHelperTool(),
                UpdateHelperTool(),
                DeleteHelperTool(),
                ListDerivedSensorsTool(),
                GetDerivedSensorTool(),
                CreateDerivedSensorTool(),
                UpdateDerivedSensorTool(),
                DeleteDerivedSensorTool(),
                ReloadDerivedSensorTool(),
                ListTemplateEntitiesTool(self.template_yaml_manager),
                GetTemplateEntityTool(self.template_yaml_manager),
                CreateTemplateEntityTool(self.template_yaml_manager),
                UpdateTemplateEntityTool(self.template_yaml_manager),
                DeleteTemplateEntityTool(self.template_yaml_manager),
                ListDashboardsTool(),
                GetDashboardTool(),
                WriteDashboardTool(),
                GetEnergyConfigTool(),
                WriteEnergyConfigTool(),
                ListRestCommandsTool(self.rest_command_manager),
                GetRestCommandTool(self.rest_command_manager),
            ],
        )


def async_register(
    hass: HomeAssistant,
    *,
    log_manager: LogManager,
    automation_manager: AutomationManager,
    script_manager: ScriptManager,
    template_yaml_manager: TemplateYamlManager,
    rest_command_manager: RestCommandManager,
) -> Any:
    """Register the dev_tools API and return its unsubscribe callable."""
    return llm.async_register_api(
        hass,
        DevToolsAPI(
            hass=hass,
            id=API_ID,
            name=API_NAME,
            log_manager=log_manager,
            automation_manager=automation_manager,
            script_manager=script_manager,
            template_yaml_manager=template_yaml_manager,
            rest_command_manager=rest_command_manager,
        ),
    )
