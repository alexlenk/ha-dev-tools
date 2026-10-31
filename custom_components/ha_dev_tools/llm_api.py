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
import logging
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, cast, override

import aiohttp

# HA validates with probatio and types its schemas (llm.Tool's parameters,
# a flow's data_schema) as probatio's - the voluptuous it installs is a
# shim handing out the same objects (issue #138).
import probatio as vol
from homeassistant.auth.models import User
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType, JsonValueType

from . import (
    access_control,
    audit_manager,
    config_snapshot,
    config_tools,
    customize_write,
    dashboard_manager,
    dashboard_patch,
    derived_sensor_manager,
    energy_manager,
    entity_manager,
    entity_source,
    helper_manager,
    history_manager,
    mirror,
    mirror_secrets,
    mqtt_manager,
    packages,
    platform_entry_manager,
    references,
    registry_manager,
    rest_command_write,
    service_call_manager,
    statistics_backup,
    statistics_derive,
    statistics_manager,
    statistics_merge,
    statistics_redistribute,
    statistics_write,
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
from .dashboard_patch import DashboardPatchError
from .derived_sensor_manager import (
    DERIVED_SENSOR_DOMAINS,
    DerivedSensorNotFoundError,
    FlowAbortedError,
    FlowStepRequiredError,
    InvalidDerivedSensorDomainError,
)
from .file_manager import FileManager, package_file
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

_LOGGER = logging.getLogger(__name__)

API_ID = "dev_tools"
API_NAME = "Dev Tools MCP"
API_PROMPT = (
    "Tools for developing and maintaining this Home Assistant instance: "
    "authoring and validating automations, reading logs, and looking up "
    "entities. Prefer these over asking the user to copy/paste YAML or "
    "restart Home Assistant - config changes take effect via reload."
)


def _validated(tool: llm.Tool, tool_input: llm.ToolInput) -> llm.ToolInput:
    """tool_input with its arguments checked against the tool's own
    parameters schema, defaults filled in. Home Assistant doesn't do this:
    llm.APIInstance.async_call_tool hands the client's JSON to the tool
    as-is, so every type, range and required field a schema declares was
    only documentation - a string where a list belongs, a 1-hour MQTT
    listen past its 10 s cap, an unknown key crashing a `**args` call."""
    return replace(tool_input, tool_args=tool.parameters(tool_input.tool_args or {}))


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


# Issue #151: what a read tool returns, and what a write takes back.
_MASKED_READ_NOTE = (
    " Literal credentials (a password, token or key written in plain text, a "
    "password in a URL, a bearer token) come back masked; !secret "
    "references are shown as they are."
)
_MASKED_WRITE_NOTE = (
    " A masked credential from a read tool can be sent back unchanged - the "
    "real value is kept; one that no longer matches the current config is "
    "refused (send the full value or a !secret)."
)


_CONFIRM_TOKEN_NOTE = (
    " Every call to this tool must happen twice: the first call never "
    "writes anything - it returns a preview of the change plus a "
    "confirm_token; call again with the identical arguments plus "
    "confirm_token set to that exact value to actually apply it. Don't "
    "guess which other field might hold it (e.g. expected_hash is "
    "unrelated) - confirm_token is its own field, and may not always be "
    "visible in this tool's declared schema depending on your MCP client."
)


# MCP tool annotations (issue #152) - hints for the client, not a gate:
# every write is still arm + admin + confirm-gated. Nothing here reaches
# outside Home Assistant (the mirror push is an audit side effect).
READ_ONLY = llm.ToolAnnotations(
    read_only=True, destructive=False, idempotent=True, open_world=False
)
# Replaces or removes what's there - the default for a write.
OVERWRITE = llm.ToolAnnotations(
    read_only=False, destructive=True, idempotent=False, open_world=False
)
# Adds something new; nothing existing is changed.
ADDITIVE = llm.ToolAnnotations(
    read_only=False, destructive=False, idempotent=False, open_world=False
)
# Sets a state to a given value - the same call twice changes nothing more.
SET_STATE = llm.ToolAnnotations(
    read_only=False, destructive=False, idempotent=True, open_world=False
)
# Runs something (an automation, a toggle) - each call acts again.
ACTION = llm.ToolAnnotations(
    read_only=False, destructive=False, idempotent=False, open_world=False
)


def _tool_result(data: JsonObjectType, *, failed: bool | None = None) -> llm.ToolResult:
    """A tool's response as HA's ToolResult (issue #152): a top-level
    "error" is a failed call, so MCP clients see isError."""
    return llm.ToolResult(
        data=data, error="error" in data if failed is None else failed
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

    # HA records which integration provides a tool (and logs one that
    # doesn't say).
    integration = DOMAIN
    annotations = READ_ONLY

    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> llm.ToolResult:
        """The gated call's response as a ToolResult."""
        data = await self._call(hass, tool_input, llm_context)
        return _tool_result(data, failed=self._failed(data))

    def _failed(self, data: JsonObjectType) -> bool | None:
        """Override when a top-level "error" isn't the call failing; None
        keeps the default."""
        return None

    async def _call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Check both gates, run the tool, then extend the idle window."""
        await access_control.check_armed(hass)
        await access_control.require_admin(hass, llm_context)
        try:
            tool_input = _validated(self, tool_input)
        except vol.Invalid as exc:
            return {"error": f"invalid arguments: {exc}", "error_type": "Invalid"}
        try:
            result = await self._run(hass, tool_input, llm_context)
        except Exception as exc:  # noqa: BLE001 - see _tool_error
            _LOGGER.exception("dev_tools %s failed", self.name)
            return _tool_error(exc)
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

    annotations = OVERWRITE

    @override
    async def _call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Run the gated call, then report the arm window it just extended."""
        result = await super()._call(hass, tool_input, llm_context)
        return {
            **result,
            "arm": cast(JsonValueType, await access_control.arm_status(hass)),
        }

    def _refused(self, problem: str) -> JsonObjectType:
        return {
            "error": problem,
            "error_type": "Invalid",
            "action": self.name,
            "note": (
                "Refused before anything was written, and no "
                "confirm_token was issued - fix the problem above and "
                "call again."
            ),
        }

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Put masked credentials back (issue #151), then the confirm flow.

        A read tool masks literal credentials; the agent sends a config
        back with the placeholder where it didn't change them. The confirm
        token stays bound to what was sent; everything that reads the
        config - validation, preview, the write, the dry-run mirror - gets
        the originals, and nothing restored goes back out in the answer.
        """
        sent = tool_input.tool_args
        if not mirror_secrets.contains_withheld(sent):
            return await self._confirmed(hass, tool_input, llm_context, sent)
        try:
            restored, originals = await self._restore_masked(hass, sent)
        except ValueError as exc:
            return self._refused(str(exc))
        response = await self._confirmed(
            hass, replace(tool_input, tool_args=restored), llm_context, sent
        )
        return cast(JsonObjectType, mirror_secrets.scrub(response, originals))

    async def _masked_originals(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> dict[str, Any]:
        """{argument: what it holds now} for the arguments a read tool
        returns masked - override where a write takes one back. An
        argument with no original here can't have a masked value restored."""
        return {}

    async def _restore_masked(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> tuple[dict[str, Any], list[Any]]:
        originals = await self._masked_originals(hass, args)
        restored, put_back, problems = dict(args), [], []
        for name, value in args.items():
            if not mirror_secrets.contains_withheld(value):
                continue
            restored[name], missing, found = mirror_secrets.restore_withheld(
                value, originals.get(name)
            )
            put_back += found
            problems += [f"{name}.{path}" if path else name for path in missing]
        if problems:
            raise ValueError(
                "masked credential(s) at "
                + ", ".join(problems)
                + " can't be put back - nothing in the current config masks "
                "to exactly that (the text around the mask was changed, the "
                "item is new, or two values fit). Send the full value, or a "
                "'!secret <name>' reference"
            )
        return restored, put_back

    async def _confirmed(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
        sent: dict[str, Any],
    ) -> JsonObjectType:
        """Require a confirmed token, then short-circuit into dry-run or
        hand off to _write(). The token is bound to `sent` - the arguments
        as the caller sent them."""
        args = sent
        # Refused outright - no token for a write that can only fail, and
        # nothing written on a confirm either (issue #165).
        if problem := await self._refusal(hass, tool_input, llm_context):
            return self._refused(problem)
        token = args.get("confirm_token")
        preview = {key: value for key, value in args.items() if key != "confirm_token"}

        user_id = llm_context.context.user_id if llm_context.context else None
        if not token or not write_confirmation.consume_pending(
            hass, self.name, args, token, user_id=user_id
        ):
            new_token = write_confirmation.create_pending(
                hass, self.name, args, user_id=user_id
            )
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

    async def _refusal(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> str | None:
        """Override to refuse a call before a token is issued or anything
        is written - e.g. a config HA would refuse to set up (issue #165).
        Checked on the propose and on the confirm call."""
        return None

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

    integration = DOMAIN
    name = "dev_tools_ping"
    annotations = READ_ONLY
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
    ) -> llm.ToolResult:
        """Return a status payload, including the arm window (issue #63)."""
        return _tool_result(
            {
                "status": "ok",
                "domain": DOMAIN,
                "arm": cast(JsonValueType, await access_control.arm_status(hass)),
            }
        )


class DescribeEntityTool(GatedTool):
    """Where an entity is defined and which tools own it - see
    entity_source.py (issue #185)."""

    name = "describe_entity"
    description = (
        "Where is this entity defined, and which tools read and change it? "
        "Start here when diagnosing an entity: returns its state, registry "
        "entry (platform, config entry, device, area) and defined_in - one "
        "of ui_helper (a UI-created helper config entry: Template helper of "
        "any domain, Min/Max, Utility Meter, ... - get/update_derived_sensor), "
        "integration (an integration's config entry), automation, script, "
        "storage_helper (input_*, counter, timer, ... made in the UI), "
        "yaml_helper, yaml_template (a template: block in "
        "configuration.yaml or a package, with file and block), "
        "package_platform_entry (a `<domain>: - platform: ...` entry in a "
        "package), yaml_platform (one outside packages) or no_unique_id "
        "(YAML without a unique_id - with candidates by name) - plus the "
        "tools, with their arguments, that read and change it. Read-only."
    )
    parameters = vol.Schema({vol.Required("entity_id"): str})

    def __init__(self, resolver: entity_source.EntitySourceResolver) -> None:
        """Init with the resolver backing this tool."""
        self._resolver = resolver

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Resolve the entity's definition."""
        try:
            user = await helper_manager.resolve_user(hass, llm_context)
            return cast(
                JsonObjectType,
                await self._resolver.describe(tool_input.tool_args["entity_id"], user),
            )
        except (UnresolvedUserError, LookupError) as exc:
            return _tool_error(exc)


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
            # No default: lines, when given, overrides offset/limit paging.
            vol.Optional("lines"): vol.All(int, vol.Range(min=1, max=1000)),
            vol.Optional("level"): str,
            vol.Optional("search"): str,
            vol.Optional("since"): str,
            vol.Optional("until"): str,
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
        try:
            filters = LogFilters(
                lines=args.get("lines"),
                since=(
                    dt_util.as_utc(_parse_datetime(args["since"], field="since"))
                    if args.get("since")
                    else None
                ),
                until=(
                    dt_util.as_utc(_parse_datetime(args["until"], field="until"))
                    if args.get("until")
                    else None
                ),
                level=args.get("level"),
                search=args.get("search"),
                offset=args.get("offset", 0),
                limit=args.get("limit", 100),
            )
        except ValueError as exc:
            return _tool_error(exc)
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
    *_STATISTICS_ERRORS,
    statistics_manager.StatisticsBackupError,
)
# The write was queued, but whether - or how far - it changed the
# statistics isn't known from the error alone.
_STATISTICS_UNCONFIRMED = (
    statistics_manager.StatisticsTimeoutError,
    statistics_manager.StatisticsNotAppliedError,
    HomeAssistantError,
)
_STATISTICS_WRITES_RUNNING = f"{DOMAIN}_statistics_writes"


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


def _statistics_busy(
    hass: HomeAssistant, statistic_ids: Iterable[str | None]
) -> JsonObjectType | None:
    """The error for a write on a statistic another write is still changing
    - checked before planning too, so nothing is planned against rows that
    are about to change. None when they're all free."""
    running: set[str] = hass.data.get(_STATISTICS_WRITES_RUNNING, set())
    if busy := sorted(running.intersection(statistic_ids)):
        return _tool_error(
            statistics_manager.StatisticsBusyError(
                f"another statistics write on {', '.join(busy)} is still "
                "running - wait for its notification, then check the "
                "statistics before trying again"
            )
        )
    return None


async def _guarded_statistics_write(
    hass: HomeAssistant,
    statistic_ids: list[str],
    operation: str,
    write: Callable[[], Coroutine[Any, Any, JsonObjectType]],
    *,
    allow_no_backup: bool,
    touches: list[str] | None = None,
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
    task posts a Home Assistant notification with its result when done.

    One write at a time per statistic (`touches`, default statistic_ids):
    a second one - a retry after `still_running` - would plan against rows
    the first is still changing, and a sum adjustment applied twice
    doesn't undo itself."""
    locked = set(touches if touches is not None else statistic_ids)
    if busy := _statistics_busy(hass, locked):
        return busy
    running: set[str] = hass.data.setdefault(_STATISTICS_WRITES_RUNNING, set())
    running |= locked
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
        except _STATISTICS_UNCONFIRMED as exc:
            # Queued but unconfirmed: report what the statistics hold now,
            # not a guess (issue #145).
            response = _tool_error(exc)
            response["now"] = cast(
                JsonValueType,
                await statistics_manager.describe_statistics(hass, sorted(locked)),
            )
        except _STATISTICS_WRITE_ERRORS as exc:
            response = _tool_error(exc)
        if "error" in response and progress.get("backups"):
            response["backups"] = progress["backups"]
            response["restore"] = statistics_manager.RESTORE_HINT
        if mirrored is not None:
            response["mirror"] = mirrored
        return response

    async def commit_once() -> JsonObjectType:
        try:
            return await commit()
        finally:
            running.difference_update(locked)

    task: asyncio.Task[JsonObjectType] = hass.async_create_background_task(
        commit_once(), f"ha_dev_tools {operation}"
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
        title=f"Dev Tools MCP: {operation}",
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
        if busy := _statistics_busy(hass, args["statistic_ids"]):
            return busy
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
        if busy := _statistics_busy(
            hass, [args["from_statistic_id"], args["to_statistic_id"]]
        ):
            return busy
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
            touches=[args["from_statistic_id"], plan["to"]],
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
        if busy := _statistics_busy(
            hass, [args["target_statistic_id"], *args["source_statistic_ids"]]
        ):
            return busy
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
            touches=[args["target_statistic_id"], *args["source_statistic_ids"]],
        )


class DeriveStatisticsTool(WriteGatedTool):
    """Build a meter statistic from another one - see statistics_derive.py."""

    name = "derive_statistics"
    description = (
        "Build a meter (sum) statistic from another one, hour by hour: each "
        "hour's change of source_statistic_id times factor - a number (a "
        "feed-in rate), rate periods [{from, value}, ...] (tariff changes), "
        "or a measurement statistic's id whose hourly mean is the factor (a "
        "dynamic price; a missing hour takes the last known price, or with "
        "missing_price='refuse' stops the write naming the hours). For the "
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
        "for an existing target, what the range held before, by month too "
        "(before, after, difference). An existing "
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
            vol.Optional("missing_price"): vol.In(statistics_derive.MISSING_PRICE),
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
            missing_price=args.get("missing_price", "carry"),
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
        if busy := _statistics_busy(
            hass, [args["target_statistic_id"], args["source_statistic_id"]]
        ):
            return busy
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
            touches=[args["target_statistic_id"], args["source_statistic_id"]],
        )


def _time_or_none(args: dict[str, Any], field: str) -> Any:
    return _parse_datetime(args[field], field=field) if args.get(field) else None


class WriteStatisticsTool(WriteGatedTool):
    """Write statistic values no other statistic has - see
    statistics_write.py."""

    name = "write_statistics"
    description = (
        "Write hourly statistic values that no other statistic has (invoice "
        "readings, a fee history, a modelled price, a single-hour "
        "correction). mode 'import' (default): rows for one statistic, "
        "inline as rows or as file (a .json - a list or {rows: [...]} - or "
        ".csv with a header, in the mirror repo). A meter or cost "
        "(kind 'sum') takes each hour's change, {start, change}: inside "
        "start..end (default: the rows' first to last hour) the rows replace "
        "what is there (an existing hour without a row counts 0), after end "
        "every row keeps its own change and the sums carry on seamlessly, a "
        "live entity goes on recording from the new sum (next_compile "
        "checks it). A measurement (kind 'mean', e.g. a price) takes "
        "{start, mean, min?, max?}; an existing hour in the range must be "
        "given too. mode 'adjust': [{start, change}] sets single hours of a "
        "meter, every later sum moves by the difference. Where the "
        "statistic still has 5-minute rows (~10 days), each hour is split "
        "evenly over its 12, or by the row's five_minute (12 numbers adding "
        "up to the change). A new statistic only under 'ha_dev_tools:' (kind "
        "and unit, name optional). Refused: a meter hour going down unless "
        "allow_negative, an incomplete hour, a time not on the hour; every "
        "problem listed, nothing written while there's one. The preview "
        "shows rows, totals by month before/after, the seams at start and "
        "end and the 5-minute rows rewritten. An existing statistic is "
        "backed up first, like merge_statistics. Never uses HA's own "
        "adjust_statistics."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("statistic_id"): str,
            vol.Optional("mode"): vol.In(statistics_write.MODES),
            vol.Optional("rows"): vol.All([dict], vol.Length(min=1)),
            vol.Optional("file"): str,
            vol.Optional("kind"): vol.In(statistics_write.KINDS),
            vol.Optional("start"): str,
            vol.Optional("end"): str,
            vol.Optional("unit"): str,
            vol.Optional("name"): str,
            vol.Optional("allow_negative"): bool,
            vol.Optional("allow_no_backup"): bool,
        }
    )

    async def _rows(self, hass: HomeAssistant, args: dict[str, Any]) -> list[Any]:
        if ("rows" in args) == ("file" in args):
            raise ValueError("pass exactly one of rows or file")
        if "rows" in args:
            return list(args["rows"])
        if not mirror.is_mirror_enabled(hass):
            raise ValueError("file is read from the mirror repo - mirroring is off")
        content = await mirror.read_mirrored(hass, args["file"])
        if content is None:
            raise ValueError(f"the mirror repo has no '{args['file']}'")
        return statistics_write.parse_rows(content, args["file"])

    async def _plan(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> statistics_write.WritePlan:
        return await statistics_write.plan_write(
            hass,
            args["statistic_id"],
            await self._rows(hass, args),
            mode=args.get("mode", "import"),
            kind=args.get("kind"),
            start=_time_or_none(args, "start"),
            end=_time_or_none(args, "end"),
            unit=args.get("unit"),
            name=args.get("name"),
            allow_negative=bool(args.get("allow_negative")),
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
        """What would be written, or every reason it's refused."""
        try:
            plan = await self._plan(hass, tool_input.tool_args)
        except (*_STATISTICS_ERRORS, ValueError, aiohttp.ClientError) as exc:
            return {"problems": str(exc)}
        return {"would_write": cast(JsonValueType, plan.preview())}

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Back up an existing statistic, then write the rows."""
        args = tool_input.tool_args
        if busy := _statistics_busy(hass, [args["statistic_id"]]):
            return busy
        try:
            plan = await self._plan(hass, args)
        except (*_STATISTICS_ERRORS, ValueError, aiohttp.ClientError) as exc:
            return _tool_error(exc)

        async def write() -> JsonObjectType:
            return cast(
                JsonObjectType, await statistics_write.write_statistics(hass, plan)
            )

        return await _guarded_statistics_write(
            hass,
            [] if plan.target.get("new") else [args["statistic_id"]],
            self.name,
            write,
            allow_no_backup=bool(args.get("allow_no_backup")),
            # Locked even when new - nothing to back up, but a retry mustn't
            # race the first write.
            touches=[args["statistic_id"]],
        )


class RedistributeStatisticsTool(WriteGatedTool):
    """Spread a meter's catch-up hour over its silent window - see
    statistics_redistribute.py."""

    name = "redistribute_statistics"
    description = (
        "Spread the energy a meter booked into one catch-up hour (after a "
        "reader or recorder outage) back over the silent hours it belongs "
        "to, without changing any total: the sum at the end of the catch-up "
        "hour stays, so later hours, the live state and the next compile are "
        "untouched. A window runs from its first silent hour to the catch-up "
        "hour: pass windows=[{start, catchup_hour}] (ISO 8601, start of an "
        "hour), or detect={min_silent_hours, min_catchup, start?, end?} for "
        "every run of at least min_silent_hours hours without a change "
        "followed by an hour above min_catchup (both required). detect_on: "
        "detect on, and move only the catch-up of, another meter - a "
        "component of a merged target; its other components' hours stay. "
        "Shape, per hour: reference_statistic_ids (another meter's hourly "
        "changes, e.g. an inverter's own counter, scaled to the moved "
        "amount; several in priority order), then profile_weeks (the "
        "series' own mean by hour of week over that many weeks around the "
        "window), then even (with a warning). Or values: the window's new "
        "hourly values, oldest first - and for the part the meter still has "
        "5-minute rows for, 12 per hour instead (the preview's values_needed "
        "says how many of each); or [{start, value}] with every other period "
        "0. They must add up to the window total (normalize scales a miss "
        "within 2 %); none negative, none above max_per_hour; references "
        "given with values are only compared (ratio, fit, hour by hour next "
        "to the values' before/after by_hour). For several windows, give "
        "each its own: windows=[{start, catchup_hour, values}] - every window "
        "or none; one backup and one write for all, and any window's problem "
        "refuses the call, every failing window named. 5-minute rows "
        "inside the window are rewritten to match. Refused: a negative hour "
        "or a meter reset in a window, overlapping windows, an incomplete "
        "catch-up hour. The preview shows per window the moved amount, which "
        "source shaped how many hours, each reference's total vs the moved "
        "amount (warning beyond 15 %, or when it looks like a flat "
        "estimate), its fit to the meter on clean hours around the window, "
        "before/after by day and month and the largest hour - with several "
        "windows compact (no by_hour/by_day; detail: true for them). "
        "Amounts are in "
        "the statistic's unit. Backed up first, like merge_statistics; "
        "series derive_statistics built from it are named, to rerun."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("statistic_id"): str,
            vol.Optional("windows"): vol.All(
                [
                    {
                        vol.Required("start"): str,
                        vol.Required("catchup_hour"): str,
                        vol.Optional("values"): vol.All(
                            [vol.Any(int, float, dict)], vol.Length(min=1)
                        ),
                    }
                ],
                vol.Length(min=1),
            ),
            vol.Optional("detect"): {
                vol.Required("min_silent_hours"): vol.All(int, vol.Range(min=1)),
                vol.Required("min_catchup"): vol.All(
                    vol.Coerce(float), vol.Range(min=0, min_included=False)
                ),
                vol.Optional("start"): str,
                vol.Optional("end"): str,
            },
            vol.Optional("detect_on"): str,
            vol.Optional("reference_statistic_ids"): [str],
            vol.Optional("profile_weeks"): vol.All(int, vol.Range(min=1, max=26)),
            vol.Optional("values"): vol.All(
                [vol.Any(int, float, dict)], vol.Length(min=1)
            ),
            vol.Optional("normalize"): bool,
            vol.Optional("max_per_hour"): vol.All(
                vol.Coerce(float), vol.Range(min=0, min_included=False)
            ),
            vol.Optional("detail"): bool,
            vol.Optional("allow_no_backup"): bool,
        }
    )

    async def _plan(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> statistics_redistribute.RedistributePlan:
        detect = args.get("detect")
        return await statistics_redistribute.plan_redistribute(
            hass,
            args["statistic_id"],
            windows=(
                [
                    {
                        "start": _parse_datetime(item["start"], field="start"),
                        "catchup_hour": _parse_datetime(
                            item["catchup_hour"], field="catchup_hour"
                        ),
                        **({"values": item["values"]} if "values" in item else {}),
                    }
                    for item in args["windows"]
                ]
                if args.get("windows") is not None
                else None
            ),
            detect=(
                {
                    **detect,
                    "start": _time_or_none(detect, "start"),
                    "end": _time_or_none(detect, "end"),
                }
                if detect is not None
                else None
            ),
            detect_on=args.get("detect_on"),
            reference_ids=args.get("reference_statistic_ids"),
            profile_weeks=args.get("profile_weeks"),
            values=args.get("values"),
            normalize=bool(args.get("normalize")),
            max_per_hour=args.get("max_per_hour"),
            detail=bool(args.get("detail")),
            can_back_up=mirror.is_mirror_enabled(hass),
            allow_no_backup=bool(args.get("allow_no_backup")),
        )

    def _touches(self, args: dict[str, Any]) -> list[str]:
        return [
            args["statistic_id"],
            *([args["detect_on"]] if args.get("detect_on") else []),
        ]

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """What each window becomes, or why it's refused."""
        try:
            plan = await self._plan(hass, tool_input.tool_args)
        except (*_STATISTICS_ERRORS, ValueError) as exc:
            return {"problems": str(exc)}
        return {"would_redistribute": cast(JsonValueType, plan.preview())}

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Back up the statistic, then write the redistributed hours."""
        args = tool_input.tool_args
        if busy := _statistics_busy(hass, self._touches(args)):
            return busy
        try:
            plan = await self._plan(hass, args)
        except (*_STATISTICS_ERRORS, ValueError) as exc:
            return _tool_error(exc)

        async def write() -> JsonObjectType:
            return cast(
                JsonObjectType,
                await statistics_redistribute.redistribute_statistics(hass, plan),
            )

        return await _guarded_statistics_write(
            hass,
            [args["statistic_id"]],
            self.name,
            write,
            allow_no_backup=bool(args.get("allow_no_backup")),
            touches=self._touches(args),
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
        if busy := _statistics_busy(
            hass, [args["backup_statistic_id"], args.get("target_statistic_id")]
        ):
            return busy
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
            touches=[plan["target"], args["backup_statistic_id"]],
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
    annotations = SET_STATE
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
        (
            "Read an automation's config by id, resolving which file actually "
            "defines it (the default automations.yaml, or a packages/*.yaml "
            "file) - a plain file read can silently miss package-defined "
            "automations. Fails clearly if the id isn't found or is defined in "
            "more than one file, rather than guessing. Also reports whether "
            "the automation is currently enabled - that's runtime-only state "
            "(toggling it via the UI or automation.turn_off never touches the "
            "YAML), so the config content alone never tells you whether it's "
            "actually active right now."
        )
        + _MASKED_READ_NOTE
        + _MISREAD_VALUES_NOTE
    )
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
            "config": mirror_secrets.mask_credentials(to_json_safe(config)),
            "currently_enabled": (
                None if live_state is None else live_state.state == "on"
            ),
            "runtime_state_note": (
                "No automation.<x> entity found for this id yet - it may "
                "not have been reloaded since being added or last edited."
                if live_state is None
                else None
            ),
            "misread_values": cast(
                JsonValueType,
                mirror_secrets.mask_misread(find_misread_scalars(config)),
            ),
        }


class WriteAutomationTool(WriteGatedTool):
    """Layout-aware, package-safe automation write - see docs/ARCHITECTURE.md."""

    name = "write_automation"
    description = (
        (
            "Create or update an automation. If the id already exists, it's "
            "updated in place in whichever file actually defines it (default "
            "file or a package) - never blindly appended to automations.yaml, "
            "which would create a silent duplicate for package-defined "
            "automations. For a brand new automation, pass 'package' to target "
            "a packages/*.yaml file (created if missing), "
            "or omit it for the default "
            "automations.yaml. Always reloads automations afterward - never "
            "requires a restart. Pass expected_hash (from get_file_metadata or "
            "a prior read) to detect concurrent edits. The config is checked with HA's own "
            "automation validation first; one HA would refuse to set up is refused "
            "with HA's error, before any token or write. enabled (true/false) "
            "also turns it on or off after the reload - HA otherwise restores "
            "an id's last on/off state, so a deleted automation restored under "
            "its old id comes back off (set_automation_enabled does this alone)."
        )
        + _MASKED_WRITE_NOTE
        + _CONFIRM_TOKEN_NOTE
    )
    parameters = _write_schema(
        {
            vol.Required("automation_id"): str,
            vol.Required("config"): dict,
            vol.Optional("package"): str,
            vol.Optional("expected_hash"): str,
            vol.Optional("enabled"): bool,
        }
    )

    def __init__(self, automation_manager: AutomationManager) -> None:
        """Init with the AutomationManager backing this tool."""
        self._manager = automation_manager

    @override
    async def _masked_originals(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> dict[str, Any]:
        """The automation's config as it is now (issue #151)."""
        try:
            _, config = await self._manager.get_automation(args["automation_id"])
        except (AutomationNotFoundError, DuplicateAutomationIdError):
            return {}
        return {"config": to_json_safe(config)}

    @override
    async def _refusal(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> str | None:
        """HA's own error for a config it would refuse to set up."""
        args = tool_input.tool_args
        return await config_tools.validation_error(
            hass, "automation", args["automation_id"], args["config"]
        )

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
        if "enabled" in args:
            response["enabled"] = await _set_enabled_after_write(
                hass, args, llm_context
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


async def _set_enabled_after_write(
    hass: HomeAssistant, args: dict[str, Any], llm_context: llm.LLMContext
) -> JsonObjectType:
    """write_automation's `enabled`: turn the just-reloaded automation on
    or off (issue #184)."""
    automation_id = args["automation_id"]
    try:
        plan = service_call_manager.plan_automation_enabled(
            hass, [automation_id], args["enabled"], {automation_id: args["config"]}
        )
    except service_call_manager.AutomationEnableRefusedError as exc:
        return _tool_error(exc)
    results = await service_call_manager.set_automations_enabled(
        hass,
        plan,
        args["enabled"],
        llm_context.context.user_id if llm_context.context else None,
    )
    return cast(JsonObjectType, results[0])


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
    annotations = ADDITIVE
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


_UI_HELPERS = (
    "UI helpers (config entries): Template helper (any domain: sensor, "
    "binary_sensor, switch, light, cover, ...), Min/Max, Utility Meter, "
    "Integration (Riemann sum), Statistics, Threshold, Derivative, Filter. "
)


class ListDerivedSensorsTool(GatedTool):
    """List config-entry-based derived/calculated sensor helpers."""

    name = "list_derived_sensors"
    description = (
        _UI_HELPERS + "List them - the Template helper can create an entity in almost "
        "any domain (picking which one is that domain's own first, "
        "menu-driven step). A second helper family alongside list_helpers', "
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
        _UI_HELPERS + "Read one's current config (for a Template helper: its template "
        "and options) by its config entry id (from list_derived_sensors, or "
        "describe_entity for an entity id)."
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
    annotations = ADDITIVE
    description = (
        _UI_HELPERS + "Create a new calculated/derived sensor helper (min_max, "
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
        _UI_HELPERS + "Update an existing one's config (a Template helper's template "
        "too) by its entry id (from list_derived_sensors), via the same options "
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
        _UI_HELPERS + "Delete one by its entry id. To "
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
    annotations = SET_STATE
    description = (
        _UI_HELPERS + "Reload one by its entry id, without "
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
        "with a unique_id are. UI-created Template helpers (config entries, "
        "any domain) are covered by list_derived_sensors / "
        "get_derived_sensor / update_derived_sensor (domain='template'). "
        "describe_entity says where any entity is defined."
    ) + _MASKED_READ_NOTE
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
        items = await self._manager.list_entities()
        for item in items:
            item["config"] = mirror_secrets.mask_credentials(item["config"])
        return cast(JsonObjectType, {"items": items})


class GetTemplateEntityTool(GatedTool):
    """Read one YAML template: entity's config by unique_id."""

    name = "get_template_entity"
    description = (
        (
            "Read a YAML `template:` entity's current config by its unique_id "
            "(from list_template_entities)."
        )
        + _MASKED_READ_NOTE
        + _MISREAD_VALUES_NOTE
    )
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
            "config": mirror_secrets.mask_credentials(config),
            "misread_values": cast(
                JsonValueType,
                mirror_secrets.mask_misread(find_misread_scalars(config)),
            ),
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
    annotations = ADDITIVE
    description = (
        "Create a new YAML `template:` entity (sensor, binary_sensor, "
        "number, switch, ...) - always in a brand new template: block, "
        "never merged into an existing one (see get_template_entity's "
        "sibling entities if you want to hand-craft a shared trigger "
        "block instead). 'config' must include a 'unique_id' - required "
        "for every write this tool family does, since a template entity "
        "otherwise has no way to be addressed again by update/"
        "delete_template_entity. 'package' names a packages/*.yaml file "
        "(relative to packages/; created if missing) - "
        "required, no "
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
        (
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
        )
        + _MASKED_WRITE_NOTE
        + _CONFIRM_TOKEN_NOTE
    )
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
    async def _masked_originals(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> dict[str, Any]:
        """The entity's config as it is now (issue #151)."""
        try:
            _, config = await self._manager.get_entity(args["unique_id"])
        except (TemplateEntityNotFoundError, DuplicateTemplateUniqueIdError):
            return {}
        return {"config": config}

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
        "YAML-mode. config_hash (not part of the config) is what "
        "patch_dashboard's expected_hash takes."
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
        return {**config, dashboard_patch.HASH_KEY: dashboard_patch.config_hash(config)}


async def _save_dashboard(
    hass: HomeAssistant,
    user: Any,
    config: dict[str, Any],
    url_path: str | None,
    response: JsonObjectType,
) -> JsonObjectType:
    """Save a dashboard config and mirror the storage file's before/after."""
    if not mirror.is_mirror_enabled(hass):
        await dashboard_manager.write_dashboard(hass, user, config, url_path=url_path)
        return response
    storage_path = await dashboard_manager.storage_path(hass, user, url_path)
    content_before = (
        await _read_storage_file(hass, storage_path) if storage_path else None
    )
    await dashboard_manager.write_dashboard(hass, user, config, url_path=url_path)
    content_after = (
        await _read_storage_file(hass, storage_path) if storage_path else None
    )
    if storage_path is None or content_after is None:
        # Saved, but not mirrored - say so rather than only logging it
        # (issue #192).
        return {
            **response,
            "mirror": {
                "mirrored": False,
                "reason": (
                    f"saved, but its storage file ({storage_path}) wasn't found "
                    "to mirror"
                    if storage_path
                    else "saved, but HA lists no storage file for this dashboard"
                ),
            },
        }
    return await _mirror_file_write(
        hass,
        response,
        file_path=storage_path,
        content_before=content_before,
        content_after=content_after,
        content_type="json",
    )


class WriteDashboardTool(WriteGatedTool):
    """Write a dashboard's config - storage mode only."""

    name = "write_dashboard"
    description = (
        "Save a Lovelace dashboard's config (views/cards). Storage-mode "
        "dashboards only - HA hard-rejects saving YAML-mode dashboards "
        "through this path (get_dashboard still works for those, just "
        "not this). Omit url_path for the default dashboard. To change one "
        "view, section or card, patch_dashboard sends only that."
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
        # get_dashboard's config_hash, sent back with the config, isn't
        # part of it.
        config = {
            key: value
            for key, value in args["config"].items()
            if key != dashboard_patch.HASH_KEY
        }
        try:
            user = await helper_manager.resolve_user(hass, llm_context)
            return await _save_dashboard(
                hass, user, config, url_path, {"saved": True, "url_path": url_path}
            )
        except (
            UnresolvedUserError,
            YamlModeDashboardError,
            WebSocketCommandError,
        ) as exc:
            return _tool_error(exc)


class PatchDashboardTool(WriteGatedTool):
    """Change part of a dashboard's config - see dashboard_patch.py."""

    name = "patch_dashboard"
    description = (
        "Change part of a storage-mode Lovelace dashboard without re-sending "
        "the whole config: ops applied in order to the config as it is now, "
        "everything else saved exactly as read. expected_hash (required) is "
        "get_dashboard's config_hash - a dashboard changed since is refused, "
        "read it again. Ops: add_view {view_config, position?}, remove_view "
        "{view}; add_section {view, section_config, position?}, "
        "replace_section {view, section, section_config}, remove_section "
        "{view, section}; add_card {view, section?, card_config, position?}, "
        "replace_card {view, section?, card, card_config}, remove_card "
        "{view, section?, card} - without section, a masonry/panel view's "
        "own cards; set {pointer, value} at a JSON pointer, e.g. "
        "'/views/2/sections/0/cards/1/days_to_show' (an object key added or "
        "replaced, a list index replaced, '-' appends). view: index, or its "
        "path, else its title; section: index, or its title or a heading "
        "card's text; card: index - a name matching more than one is "
        "refused. position: index to insert before (default: the end). "
        "Indices count from 0, after the ops before. The preview lists each "
        "touched node (pointer, before, after) and the hashes; the result "
        "has the new config_hash for the next patch. Omit url_path for the "
        "default dashboard."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("ops"): vol.All(
                [
                    vol.Schema(
                        {vol.Required("op"): vol.In(dashboard_patch.OPS)},
                        extra=vol.ALLOW_EXTRA,
                    )
                ],
                vol.Length(min=1),
            ),
            vol.Required("expected_hash"): str,
            vol.Optional("url_path"): str,
        }
    )

    async def _plan(
        self, hass: HomeAssistant, args: dict[str, Any], user: Any
    ) -> tuple[dict[str, Any], dashboard_patch.PatchResult]:
        config = await dashboard_manager.get_dashboard(
            hass, user, url_path=args.get("url_path")
        )
        if dashboard_patch.config_hash(config) != args["expected_hash"]:
            raise DashboardPatchError(
                "the dashboard changed since expected_hash - read it again "
                "(get_dashboard) and plan the ops against what it holds now"
            )
        return config, dashboard_patch.apply_ops(config, args["ops"])

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Each touched node before and after, or why the ops are refused."""
        try:
            user = await helper_manager.resolve_user(hass, llm_context)
            config, result = await self._plan(hass, tool_input.tool_args, user)
        except (UnresolvedUserError, WebSocketCommandError, ValueError) as exc:
            return {"problems": str(exc)}
        return {
            "would_change": cast(
                JsonValueType,
                {
                    "changes": result.changes,
                    "untouched": "everything outside these pointers is saved "
                    "exactly as read",
                    "config_hash_before": dashboard_patch.config_hash(config),
                    "config_hash_after": dashboard_patch.config_hash(result.config),
                },
            )
        }

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Re-read the dashboard, check the hash, apply the ops and save."""
        args = tool_input.tool_args
        url_path = args.get("url_path")
        try:
            user = await helper_manager.resolve_user(hass, llm_context)
            _, result = await self._plan(hass, args, user)
            response = await _save_dashboard(
                hass,
                user,
                result.config,
                url_path,
                {"saved": True, "url_path": url_path},
            )
        except (
            UnresolvedUserError,
            YamlModeDashboardError,
            WebSocketCommandError,
            ValueError,
        ) as exc:
            return _tool_error(exc)
        try:
            # The hash of what HA now holds, for the next patch.
            saved = await dashboard_manager.get_dashboard(hass, user, url_path=url_path)
        except WebSocketCommandError:
            saved = result.config
        return {
            **response,
            "changes": cast(JsonValueType, result.changes),
            dashboard_patch.HASH_KEY: dashboard_patch.config_hash(saved),
        }


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
        "automation's config as it was when this run happened, literal "
        "credentials masked like get_automation's. Read-only."
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
    annotations = ACTION
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

    @override
    def _failed(self, data: JsonObjectType) -> bool | None:
        """A run that failed after being started is the run's outcome,
        reported under "error" - the trigger itself worked."""
        return False if data.get("triggered") else None


class SetAutomationEnabledTool(WriteGatedTool):
    """Turn automations on or off - see service_call_manager.py (issue #184)."""

    name = "set_automation_enabled"
    annotations = SET_STATE
    description = (
        "Turn one or more automations on or off (the UI's enable toggle - "
        "automation.turn_on / turn_off), by config id (as get_automation; "
        "automation_id, or automation_ids for several). A runtime state HA "
        "restores across restarts - the YAML isn't changed. Not the entity "
        "registry's disable (update_entities' disabled). Turning one off "
        "also stops a run in progress, like the UI. An id without a live "
        "automation.* entity is refused before a confirm_token is issued. "
        "The preview shows each one's alias, current state, last_triggered "
        "and its triggers one per line, so turning one on shows what will "
        "start firing; the result each one's state before and after."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Optional("automation_id"): str,
            vol.Optional("automation_ids"): [str],
            vol.Required("enabled"): bool,
        }
    )

    def __init__(self, automation_manager: AutomationManager) -> None:
        """Init with the AutomationManager the triggers are read from."""
        self._manager = automation_manager

    async def _plan(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> list[dict[str, Any]]:
        ids = _one_or_many(args, "automation_id", "automation_ids")
        configs: dict[str, dict[str, Any] | None] = {}
        for automation_id in ids:
            try:
                _, config = await self._manager.get_automation(automation_id)
            except (AutomationNotFoundError, DuplicateAutomationIdError):
                config = None
            configs[automation_id] = config
        return service_call_manager.plan_automation_enabled(
            hass, ids, args["enabled"], configs
        )

    @override
    async def _refusal(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> str | None:
        """Every reason the automations can't be switched, if any."""
        try:
            await self._plan(hass, tool_input.tool_args)
        except (ValueError, service_call_manager.AutomationEnableRefusedError) as exc:
            return str(exc)
        return None

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Each automation, its state and what it reacts to."""
        return {
            "would_set": cast(
                JsonValueType, await self._plan(hass, tool_input.tool_args)
            )
        }

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Turn each one on or off; report its state before and after."""
        args = tool_input.tool_args
        results = await service_call_manager.set_automations_enabled(
            hass,
            await self._plan(hass, args),
            args["enabled"],
            llm_context.context.user_id if llm_context.context else None,
        )
        return {"results": cast(JsonValueType, results)}


class SetValueTool(WriteGatedTool):
    """Set a value entity's value - see service_call_manager.py (issue #194)."""

    name = "set_value"
    annotations = SET_STATE
    description = (
        "Set the value of an entity that holds one - without an automation: "
        "input_text / text (a string, checked against min/max length and "
        "pattern), input_select / select (one of its options), input_number "
        "/ number (within min/max), counter (a whole number within "
        "minimum/maximum), input_datetime (a date '2026-10-31', a time "
        "'07:30:00' or both '2026-10-31 07:30:00', as the helper has), date, "
        "time, datetime. On/off is control_entities. An entity or device "
        "excluded on the Configure page is refused, and so is a value the "
        "entity wouldn't take - before a confirm_token is issued. The "
        "preview shows the current state, the service call, and every "
        "automation whose triggers name the entity (an 'on' one will fire); "
        "the result the state before and after."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("entity_id"): str,
            vol.Required("value"): vol.Any(str, int, float),
        }
    )

    def __init__(self, automation_manager: AutomationManager) -> None:
        """Init with the AutomationManager the triggers are read from."""
        self._automations = automation_manager

    @staticmethod
    def _plan(hass: HomeAssistant, args: dict[str, Any]) -> dict[str, Any]:
        return service_call_manager.plan_value(hass, args["entity_id"], args["value"])

    async def _triggered(self, hass: HomeAssistant, entity_id: str) -> list[Any]:
        """Automations whose triggers name the entity, and whether each is on."""
        regex = references.pattern(entity_id)
        found = []
        for _, config in await self._automations.all_automations():
            triggers = config.get("triggers", config.get("trigger"))
            if not regex.search(json.dumps(to_json_safe(triggers))):
                continue
            automation_id = config.get("id")
            state = (
                audit_manager.find_automation_state(hass, str(automation_id))
                if automation_id is not None
                else None
            )
            found.append(
                {
                    "automation_id": automation_id,
                    "alias": config.get("alias"),
                    "state": state.state if state else None,
                }
            )
        return found

    @override
    async def _refusal(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> str | None:
        """Why the value can't be set, if it can't."""
        try:
            self._plan(hass, tool_input.tool_args)
        except service_call_manager.ValueRefusedError as exc:
            return str(exc)
        return None

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """The current state, the call, and what it will trigger."""
        args = tool_input.tool_args
        return {
            "would_set": cast(JsonValueType, self._plan(hass, args)),
            "triggers_automations": cast(
                JsonValueType, await self._triggered(hass, args["entity_id"])
            ),
        }

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Set it; report the state before and after."""
        result = await service_call_manager.set_entity_value(
            hass,
            self._plan(hass, tool_input.tool_args),
            llm_context.context.user_id if llm_context.context else None,
        )
        return cast(JsonObjectType, result)


class SetNumberValueTool(WriteGatedTool):
    """Set a number/input_number entity's value - see service_call_manager.py."""

    name = "set_number_value"
    annotations = SET_STATE
    description = (
        "Set a `number` or `input_number` entity's value directly - e.g. "
        "to test a single write in isolation (an EMHASS battery-schedule "
        "slot, a Modbus-backed inverter setting exposed as a number "
        "entity) without a full automation edit cycle (see issue #76). "
        "Deliberately scoped to just these two domains, not a generic "
        "service-call tool - refuses any other entity domain rather than "
        "guessing which service to call. set_value does this and more "
        "(text, select, datetime, counter) - prefer it."
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
    annotations = SET_STATE
    description = (
        "Turn an `input_boolean` helper on or off directly. Scoped to just "
        "this virtual helper domain - control_entities turns other "
        "entities (switch, light, cover, ...) on and off."
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


class ControlEntitiesTool(WriteGatedTool):
    """Turn entities on/off/toggle without an automation - see
    service_call_manager.py's control_entities."""

    name = "control_entities"
    annotations = ACTION
    description = (
        "Turn one or more entities on, off or toggle them directly, "
        "without writing an automation: switch, light, fan, input_boolean, "
        "climate, media_player, humidifier (turn_on/turn_off/toggle), cover "
        "(on opens, off closes) and valve (on opens, off closes). "
        "brightness_pct (0-100) goes with action 'on' and lights only. "
        "lock, alarm_control_panel and siren are never controlled, and "
        "neither is any device or entity excluded on this integration's "
        "Configure page - such a call is refused before a confirm_token is "
        "issued, with every reason listed. The preview shows each entity's "
        "name, area, current state and the service that would be called; "
        "the result each one's state before and after (one that fails "
        "doesn't stop the others)."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("entity_ids"): vol.All([str], vol.Length(min=1)),
            vol.Required("action"): vol.In(service_call_manager.CONTROL_ACTIONS),
            vol.Optional("brightness_pct"): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=100)
            ),
        }
    )

    @staticmethod
    def _plan(hass: HomeAssistant, args: dict[str, Any]) -> list[dict[str, Any]]:
        return service_call_manager.plan_control(
            hass, args["entity_ids"], args["action"], args.get("brightness_pct")
        )

    @override
    async def _refusal(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> str | None:
        """Every reason the entities can't be controlled, if any."""
        try:
            self._plan(hass, tool_input.tool_args)
        except service_call_manager.ControlRefusedError as exc:
            return str(exc)
        return None

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Each entity, where it is, what it is now and the service call."""
        return {
            "would_call": cast(JsonValueType, self._plan(hass, tool_input.tool_args))
        }

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Call each entity's service; report its state before and after."""
        results = await service_call_manager.control_entities(
            hass,
            self._plan(hass, tool_input.tool_args),
            llm_context.context.user_id if llm_context.context else None,
        )
        return {"results": cast(JsonValueType, results)}


class ListScriptsTool(GatedTool):
    """List every script across scripts.yaml and packages."""

    name = "list_scripts"
    description = (
        "List every script (the default scripts.yaml and every "
        "packages/*.yaml file), each with its id, source file, and full "
        "config. Same layout-aware, package-safe file resolution as "
        "write_script - a plain file read can silently miss "
        "package-defined scripts."
    ) + _MASKED_READ_NOTE
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
                "config": mirror_secrets.mask_credentials(to_json_safe(config)),
            }
            for location, script_id, config in await self._manager.all_scripts()
        ]
        return cast(JsonObjectType, {"items": items})


class GetScriptTool(GatedTool):
    """Layout-aware script read - resolves the file that actually defines it."""

    name = "get_script"
    description = (
        (
            "Read a script's config by id, resolving which file actually "
            "defines it (the default scripts.yaml, or a packages/*.yaml "
            "file) - a plain file read can silently miss package-defined "
            "scripts. Fails clearly if the id isn't found or is defined in "
            "more than one file, rather than guessing."
        )
        + _MASKED_READ_NOTE
        + _MISREAD_VALUES_NOTE
    )
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
            "config": mirror_secrets.mask_credentials(to_json_safe(config)),
            "misread_values": cast(
                JsonValueType,
                mirror_secrets.mask_misread(find_misread_scalars(config)),
            ),
        }


class WriteScriptTool(WriteGatedTool):
    """Layout-aware, package-safe script write - see script_manager.py."""

    name = "write_script"
    description = (
        (
            "Create or update a script. If the id already exists, it's "
            "updated in place in whichever file actually defines it (default "
            "file or a package) - never blindly appended to scripts.yaml, "
            "which would create a silent duplicate for package-defined "
            "scripts. For a brand new script, pass 'package' to target a "
            "packages/*.yaml file (created if missing), "
            "or omit it for the default "
            "scripts.yaml. Always reloads scripts afterward - never requires "
            "a restart. Pass expected_hash (from get_file_metadata or a prior "
            "read) to detect concurrent edits. The config is checked with HA's own "
            "script validation first; one HA would refuse to set up is refused "
            "with HA's error, before any token or write."
        )
        + _MASKED_WRITE_NOTE
        + _CONFIRM_TOKEN_NOTE
    )
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
    async def _masked_originals(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> dict[str, Any]:
        """The script's config as it is now (issue #151)."""
        try:
            _, config = await self._manager.get_script(args["script_id"])
        except (ScriptNotFoundError, DuplicateScriptIdError):
            return {}
        return {"config": to_json_safe(config)}

    @override
    async def _refusal(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> str | None:
        """HA's own error for a config it would refuse to set up."""
        args = tool_input.tool_args
        return await config_tools.validation_error(
            hass, "script", args["script_id"], args["config"]
        )

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
    ) + _MASKED_READ_NOTE
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
                "config": mirror_secrets.mask_credentials(to_json_safe(config)),
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
        "file, rather than guessing. Written with write_rest_command / "
        "delete_rest_command. Pass domain='shell_command' to read a "
        "shell_command entry "
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
            # Read-only, so never written back: a literal password or
            # token in it is withheld, as get_config_file withholds it.
            "config": mirror_secrets.mask_credentials(to_json_safe(config)),
        }


# --- package platform entries (issue #164) and rest_command writes (#168) ----

# Platforms that run shell commands - shell_command's own class of risk,
# kept read-only on purpose (see rest_command_manager.py).
_SHELL_PLATFORMS = {"command_line", "shell_command"}
# A `rest` platform entry sends requests like a rest_command - the same
# egress rules (#168's decisions) apply to its resource.
_REST_PLATFORMS = {"rest"}

_ENTRY_SELECTOR = vol.Schema(
    {
        vol.Required("domain"): str,
        vol.Required("platform"): str,
        vol.Optional("unique_id"): str,
        vol.Optional("name"): str,
        vol.Optional("file"): str,
    }
)


def _entry_entity_id(
    hass: HomeAssistant, domain: str, platform: str, unique_id: Any, name: Any
) -> str | None:
    """The live entity a platform entry created, if it can be told."""
    if unique_id is not None:
        return er.async_get(hass).async_get_entity_id(domain, platform, str(unique_id))
    from homeassistant.util import slugify

    # Every entry has a unique_id or a name (platform_entry_manager).
    entity_id = f"{domain}.{slugify(str(name))}"
    return entity_id if hass.states.get(entity_id) is not None else None


async def _entry_references(
    hass: HomeAssistant, llm_context: llm.LLMContext, entity_id: str | None
) -> JsonValueType:
    if entity_id is None:
        return None
    try:
        user = await helper_manager.resolve_user(hass, llm_context)
        found = await references.find_references(hass, user, entity_id)
    except UnresolvedUserError:
        return None
    except WebSocketCommandError as exc:
        # The preview still stands - it just can't list them.
        return {"entity_id": entity_id, "error": str(exc)}
    return cast(JsonValueType, {"entity_id": entity_id, **found})


async def _egress_problems(
    writer: rest_command_write.RestCommandWriter,
    url: Any,
    *,
    old_url: Any = None,
    allow_new_host: bool,
) -> tuple[list[str], dict[str, Any]]:
    """#168's host rules for a URL a write would send requests to."""
    try:
        host = rest_command_write.target_host(url)
    except rest_command_write.RestCommandWriteError as err:
        return [str(err)], {}
    try:
        old_host = rest_command_write.target_host(old_url) if old_url else None
    except rest_command_write.RestCommandWriteError:
        old_host = None
    known = await writer._known_hosts()
    new = host not in known and host != old_host
    report = {
        "host": host,
        "host_class": rest_command_write.host_class(host),
        "new_host": new,
    }
    if new and not allow_new_host:
        return [
            f"'{host}' ({report['host_class']}) is a host no rest_command uses "
            "yet - pass allow_new_host to send requests there"
        ], report
    return [], report


class ListPackagePlatformEntitiesTool(GatedTool):
    """List YAML platform entries in packages - see platform_entry_manager.py."""

    name = "list_package_platform_entities"
    description = (
        "List the YAML platform entries in packages/*.yaml - every top-level "
        "<domain>: list of platform: entries (sensor: - platform: statistics, "
        "binary_sensor:, switch:, ...), with file, domain, index, platform, "
        "unique_id, name and config (literal credentials withheld). Filter "
        "by domain and/or platform. configuration.yaml isn't listed - it "
        "stays read-only."
    )
    parameters = vol.Schema(
        {vol.Optional("domain"): str, vol.Optional("platform"): str}
    )

    def __init__(self, manager: platform_entry_manager.PlatformEntryManager) -> None:
        self._manager = manager

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        args = tool_input.tool_args
        entries = await self._manager.list_entries(
            domain=args.get("domain"), platform=args.get("platform")
        )
        for entry in entries:
            entry["config"] = mirror_secrets.mask_credentials(entry["config"])
        return {"entries": cast(JsonValueType, entries)}


class WritePackagePlatformEntityTool(WriteGatedTool):
    """Create or replace a YAML platform entry in a package."""

    name = "write_package_platform_entity"
    description = (
        (
            "Create or replace one YAML platform entry in a package: config is "
            "the entry (with platform, and a unique_id or else a name to find it "
            "by). An entry of this domain + platform with the same unique_id - or "
            "the same name, for one without - is replaced in its own file; "
            "otherwise the config is a new entry in package (a packages/ "
            "file, created if missing). More than one "
            "match is refused; file narrows it. "
            "The config is checked against the platform's own PLATFORM_SCHEMA "
            "first. Refused: literal credentials (use !secret), command_line / "
            "shell_command platforms (shell execution, read-only by design), "
            "and for a rest platform a templated host, a new host unless "
            "allow_new_host, verify_ssl: false unless allow_insecure_tls. The "
            "preview shows the entry before and after and every reference to "
            "its entity; the result says whether a reload took it in or a "
            "restart is needed."
        )
        + _MASKED_WRITE_NOTE
        + _CONFIRM_TOKEN_NOTE
    )
    parameters = _write_schema(
        {
            vol.Required("domain"): str,
            vol.Required("config"): dict,
            vol.Optional("package"): str,
            vol.Optional("file"): str,
            vol.Optional("allow_new_host"): bool,
            vol.Optional("allow_insecure_tls"): bool,
        }
    )

    def __init__(
        self,
        manager: platform_entry_manager.PlatformEntryManager,
        writer: rest_command_write.RestCommandWriter,
    ) -> None:
        self._manager = manager
        self._writer = writer

    @override
    async def _masked_originals(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> dict[str, Any]:
        """The entry's config as it is now (issue #151) - found by the
        unique_id, else the name, it was sent back with."""
        config = args.get("config") or {}
        unique_id, name = config.get("unique_id"), config.get("name")
        selector = (
            {"unique_id": str(unique_id)}
            if unique_id is not None
            else {"name": str(name)} if name is not None else None
        )
        if selector is None:
            return {}
        try:
            found = await self._manager.find(
                args["domain"],
                str(config.get("platform", "")),
                file=args.get("file"),
                **selector,
            )
        except ValueError:
            return {}
        if len(found) != 1:
            return {}
        return {"config": to_json_safe(await self._manager.get_entry(found[0]))}

    async def _plan(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> tuple[dict[str, Any], Any, str, str | None, str]:
        domain, config = args["domain"], dict(args["config"])
        platform = str(config.get("platform", ""))
        problems: list[str] = []
        if platform in _SHELL_PLATFORMS:
            raise platform_entry_manager.PlatformEntryError(
                f"'{platform}' runs shell commands on the host - read-only by "
                "design, like shell_command"
            )
        if error := await platform_entry_manager.validation_error(hass, domain, config):
            problems.append(error)
        if credentials := mirror_secrets.literal_credentials(to_json_safe(config)):
            problems.append(
                f"literal credential(s) at {', '.join(credentials)} - use "
                "'!secret <name>' instead"
            )
        if problems:
            raise platform_entry_manager.PlatformEntryError("; ".join(problems))
        location, file_path, before, after = await self._manager.plan_write(
            domain, config, package=args.get("package"), file=args.get("file")
        )
        old = await self._manager.get_entry(location) if location else None
        report: dict[str, Any] = {
            "domain": domain,
            "platform": platform,
            "file_path": file_path,
            "action": "replace" if location else "create",
            "before": (
                mirror_secrets.mask_credentials(to_json_safe(old))
                if old is not None
                else None
            ),
            "after": mirror_secrets.mask_credentials(to_json_safe(config)),
        }
        if platform in _REST_PLATFORMS:
            url = config.get("resource_template") or config.get("resource")
            egress, host = await _egress_problems(
                self._writer,
                url,
                old_url=(old or {}).get("resource"),
                allow_new_host=bool(args.get("allow_new_host")),
            )
            report.update(host)
            if rest_command_write._insecure(config) and not (
                old is not None and rest_command_write._insecure(old)
            ):
                if not args.get("allow_insecure_tls"):
                    egress.append(
                        "verify_ssl: false turns off certificate checks - pass "
                        "allow_insecure_tls if that's intended"
                    )
            if egress:
                raise platform_entry_manager.PlatformEntryError("; ".join(egress))
        return report, location, file_path, before, after

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        args = tool_input.tool_args
        try:
            report, location, *_ = await self._plan(hass, args)
        except ValueError as exc:
            return {"problems": str(exc)}
        config = args["config"]
        report["references"] = await _entry_references(
            hass,
            llm_context,
            (
                _entry_entity_id(
                    hass,
                    args["domain"],
                    str(config.get("platform")),
                    config.get("unique_id"),
                    config.get("name"),
                )
                if location
                else None
            ),
        )
        return {"would_write": cast(JsonValueType, report)}

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        args = tool_input.tool_args
        try:
            report, _, file_path, before, after = await self._plan(hass, args)
            await self._manager.write_file(file_path, after)
        except (ValueError, OSError, RuntimeError) as exc:
            return _tool_error(exc)
        report["reload"] = await self._manager.reload(report["platform"])
        return await _mirror_file_write(
            hass,
            {"written": cast(JsonValueType, report)},
            file_path=file_path,
            content_before=before,
            content_after=after,
        )


class DeletePackagePlatformEntitiesTool(WriteGatedTool):
    """Delete YAML platform entries in packages."""

    name = "delete_package_platform_entities"
    description = (
        "Delete YAML platform entries in packages: entries, a list of "
        "{domain, platform, unique_id or name, file?} - each must match "
        "exactly one entry, or nothing is deleted. Each file is written "
        "once; a domain key left empty is removed. The preview shows every "
        "entry and every reference to its entity; the result says whether "
        "a reload took the change in or a restart is needed."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {vol.Required("entries"): vol.All([_ENTRY_SELECTOR], vol.Length(min=1))}
    )

    def __init__(self, manager: platform_entry_manager.PlatformEntryManager) -> None:
        self._manager = manager

    async def _locations(
        self, args: dict[str, Any]
    ) -> list[platform_entry_manager.EntryLocation]:
        locations = []
        for selector in args["entries"]:
            locations.append(
                await self._manager.resolve(
                    selector["domain"],
                    selector["platform"],
                    unique_id=selector.get("unique_id"),
                    name=selector.get("name"),
                    file=selector.get("file"),
                )
            )
        if len(set(locations)) != len(locations):
            raise platform_entry_manager.PlatformEntryError(
                "the same entry is listed twice"
            )
        return locations

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        try:
            locations = await self._locations(tool_input.tool_args)
        except ValueError as exc:
            return {"problems": str(exc)}
        entries = []
        for location in locations:
            config = await self._manager.get_entry(location)
            entries.append(
                {
                    "file_path": location.file_path,
                    "domain": location.domain,
                    "index": location.index,
                    "config": mirror_secrets.mask_credentials(to_json_safe(config)),
                    "references": await _entry_references(
                        hass,
                        llm_context,
                        _entry_entity_id(
                            hass,
                            location.domain,
                            location.platform,
                            location.unique_id,
                            location.name,
                        ),
                    ),
                }
            )
        return {"would_delete": cast(JsonValueType, entries)}

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        try:
            locations = await self._locations(tool_input.tool_args)
            planned = await self._manager.plan_delete(locations)
        except ValueError as exc:
            return _tool_error(exc)
        results = []
        for file_path, entries, before, after in planned:
            try:
                await self._manager.write_file(file_path, after)
            except (ValueError, OSError, RuntimeError) as exc:
                return _tool_error(exc)
            results.append(
                await _mirror_file_write(
                    hass,
                    {"file_path": file_path, "deleted": len(entries)},
                    file_path=file_path,
                    content_before=before,
                    content_after=after,
                )
            )
        reloads = {
            platform: await self._manager.reload(platform)
            for platform in sorted({location.platform for location in locations})
        }
        return {
            "deleted": cast(JsonValueType, results),
            "reload": cast(JsonValueType, reloads),
        }


class WriteRestCommandTool(WriteGatedTool):
    """Create or replace a rest_command in a package - see rest_command_write.py."""

    name = "write_rest_command"
    description = (
        "Create or replace one rest_command entry (url, method, headers, "
        "payload, ...) by id in a packages/*.yaml file - an existing id is "
        "replaced where it's defined, a new one goes into package (created "
        "if missing). Writes "
        "config only; running it still takes an automation or script. "
        "Refused: a templated scheme or host in url (templates only in the "
        "path, query, payload, headers), a host no rest_command uses yet "
        "unless allow_new_host (the preview says if it's a LAN or public "
        "address), literal credentials (headers like Authorization, "
        "username, password, a password in the URL - use !secret), "
        "verify_ssl: false unless allow_insecure_tls, and an entry in "
        "configuration.yaml (read-only). The preview shows the entry "
        "before/after (credentials withheld), method, timeout, host and "
        "every automation/script calling it. Reloads rest_command after."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("rest_command_id"): str,
            vol.Required("config"): dict,
            vol.Optional("package"): str,
            vol.Optional("allow_new_host"): bool,
            vol.Optional("allow_insecure_tls"): bool,
        }
    )

    def __init__(self, writer: rest_command_write.RestCommandWriter) -> None:
        self._writer = writer

    async def _plan(self, args: dict[str, Any]) -> rest_command_write.RestCommandPlan:
        return await self._writer.plan_write(
            args["rest_command_id"],
            dict(args["config"]),
            package=args.get("package"),
            allow_new_host=bool(args.get("allow_new_host")),
            allow_insecure_tls=bool(args.get("allow_insecure_tls")),
        )

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        try:
            plan = await self._plan(tool_input.tool_args)
        except (ValueError, DuplicateRestCommandIdError) as exc:
            return {"problems": str(exc)}
        report = dict(plan.report)
        report["references"] = await _entry_references(
            hass, llm_context, f"rest_command.{plan.rest_command_id}"
        )
        return {"would_write": cast(JsonValueType, report)}

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        try:
            plan = await self._plan(tool_input.tool_args)
            reload = await self._writer.write(plan)
        except (ValueError, DuplicateRestCommandIdError, OSError, RuntimeError) as exc:
            return _tool_error(exc)
        return await _mirror_file_write(
            hass,
            {"written": cast(JsonValueType, {**plan.report, "reload": reload})},
            file_path=plan.file_path,
            content_before=plan.content_before,
            content_after=plan.content_after,
        )


class DeleteRestCommandTool(WriteGatedTool):
    """Delete a rest_command from a package."""

    name = "delete_rest_command"
    description = (
        "Delete one rest_command entry by id from the packages/*.yaml file "
        "that defines it (not from configuration.yaml, read-only). The "
        "preview shows the entry (credentials withheld) and every "
        "automation/script still calling it. Reloads rest_command after."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema({vol.Required("rest_command_id"): str})

    def __init__(self, writer: rest_command_write.RestCommandWriter) -> None:
        self._writer = writer

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        rest_command_id = tool_input.tool_args["rest_command_id"]
        try:
            plan = await self._writer.plan_delete(rest_command_id)
        except (
            ValueError,
            RestCommandNotFoundError,
            DuplicateRestCommandIdError,
        ) as exc:
            return {"problems": str(exc)}
        report = dict(plan.report)
        report["references"] = await _entry_references(
            hass, llm_context, f"rest_command.{rest_command_id}"
        )
        return {"would_delete": cast(JsonValueType, report)}

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        try:
            plan = await self._writer.plan_delete(
                tool_input.tool_args["rest_command_id"]
            )
            reload = await self._writer.write(plan)
        except (
            ValueError,
            RestCommandNotFoundError,
            DuplicateRestCommandIdError,
            OSError,
            RuntimeError,
        ) as exc:
            return _tool_error(exc)
        return await _mirror_file_write(
            hass,
            {"deleted": cast(JsonValueType, {**plan.report, "reload": reload})},
            file_path=plan.file_path,
            content_before=plan.content_before,
            content_after=plan.content_after,
        )


class WriteCustomizeTool(WriteGatedTool):
    """`homeassistant: customize:` entries in packages - see
    customize_write.py (issue #183)."""

    name = "write_customize"
    description = (
        "Set or remove customize attributes (homeassistant: customize: in a "
        "package) for one or more entities - the only way to give an "
        "integration's sensor a state_class (without one a numeric sensor "
        "gets no long-term statistics) or a missing device_class. entities "
        "maps entity_id -> {attribute: value}; null removes that attribute, "
        "an entity left with none is removed. Only state_class "
        "(measurement / total / total_increasing, sensors only), "
        "device_class (one of the domain's own), unit_of_measurement, "
        "friendly_name, icon and assumed_state. An entity already "
        "customized is changed where it is; otherwise pass package (a "
        "packages/ file - created if missing). One "
        "customized in another "
        "file, or in configuration.yaml (read-only), is refused. The "
        "preview shows each entity's state and attributes now, its entry "
        "before/after, and warnings (a combination HA won't compile "
        "statistics for, a non-numeric state, an entity that doesn't "
        "exist). Applied without a restart: HA's config check (a new error "
        "rolls the write back), homeassistant.reload_core_config, each "
        "entity's state rewritten - the result says per entity whether it "
        "now has the attributes. Statistics start from then; no backfill."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema(
        {
            vol.Required("entities"): vol.All({str: dict}, vol.Length(min=1)),
            vol.Optional("package"): str,
        }
    )

    def __init__(self, writer: customize_write.CustomizeWriter) -> None:
        """Init with the writer backing this tool."""
        self._writer = writer

    async def _plan(self, args: dict[str, Any]) -> customize_write.CustomizePlan:
        return await self._writer.plan(
            dict(args["entities"]), package=args.get("package")
        )

    @override
    async def _refusal(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> str | None:
        """Every reason the customize write is refused, if any."""
        try:
            await self._plan(tool_input.tool_args)
        except ValueError as exc:
            return str(exc)
        return None

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Each entity's attributes now and its entry before/after."""
        plan = await self._plan(tool_input.tool_args)
        return {"would_write": cast(JsonValueType, plan.preview())}

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Write, check, reload core config, report per entity."""
        try:
            plan = await self._plan(tool_input.tool_args)
            applied = await self._writer.apply(plan)
        except (ValueError, OSError, HomeAssistantError) as exc:
            return _tool_error(exc)
        return await _mirror_file_write(
            hass,
            {"customized": cast(JsonValueType, applied)},
            file_path=plan.file_path,
            content_before=plan.content_before,
            content_after=plan.content_after,
        )


class DeletePackageTool(WriteGatedTool):
    """Delete an empty package file - see packages.py (issue #189)."""

    name = "delete_package"
    description = (
        "Delete a packages/*.yaml file that holds no configuration any more "
        "(only comments, blank lines or {}) - e.g. after its last entry was "
        "deleted. A file that still holds anything is refused, naming its "
        "top-level keys; delete those with their own tools first. The "
        "write tools create a missing package file themselves, so there is "
        "no create_package."
    ) + _CONFIRM_TOKEN_NOTE
    parameters = _write_schema({vol.Required("package"): str})

    def __init__(self, file_manager: FileManager) -> None:
        """Init with the FileManager the package is deleted through."""
        self._files = file_manager

    async def _content(self, args: dict[str, Any]) -> tuple[str, str]:
        file_path = package_file(args["package"])
        try:
            return file_path, await self._files.read_file(file_path)
        except FileNotFoundError:
            raise ValueError(f"there is no '{file_path}'") from None

    @override
    async def _refusal(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> str | None:
        """A missing file, or one that still holds configuration."""
        try:
            file_path, content = await self._content(tool_input.tool_args)
        except ValueError as exc:
            return str(exc)
        if await hass.async_add_executor_job(packages.is_empty, content):
            return None
        keys = await hass.async_add_executor_job(packages.top_level_keys, content)
        return (
            f"'{file_path}' still holds configuration ({', '.join(keys)}) - "
            "delete its entries with their own tools first"
        )

    @override
    async def _preview_context(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """The file and what's left in it (comments)."""
        file_path, content = await self._content(tool_input.tool_args)
        return {"would_delete": file_path, "content": content}

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Delete the file; mirror it as emptied."""
        file_path, content = await self._content(tool_input.tool_args)
        await self._files.delete_file(file_path)
        return await _mirror_file_write(
            hass,
            {"deleted": file_path},
            file_path=file_path,
            content_before=content,
            content_after="",
        )


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
        writer = rest_command_write.RestCommandWriter(
            self.hass, self.rest_command_manager
        )
        platforms = platform_entry_manager.PlatformEntryManager(
            self.hass, self.rest_command_manager.file_manager
        )
        return llm.APIInstance(
            self,
            API_PROMPT,
            llm_context,
            tools=[
                DevToolsPingTool(),
                FindEntitiesTool(),
                DescribeEntityTool(
                    entity_source.EntitySourceResolver(
                        self.hass,
                        self.automation_manager,
                        self.script_manager,
                        self.template_yaml_manager,
                        platforms,
                    )
                ),
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
                WriteStatisticsTool(),
                RedistributeStatisticsTool(),
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
                DeletePackageTool(self.rest_command_manager.file_manager),
                WriteCustomizeTool(
                    customize_write.CustomizeWriter(
                        self.hass, self.rest_command_manager.file_manager
                    )
                ),
                SetAutomationEnabledTool(self.automation_manager),
                SetValueTool(self.automation_manager),
                SetNumberValueTool(),
                SetBooleanValueTool(),
                ControlEntitiesTool(),
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
                PatchDashboardTool(),
                GetEnergyConfigTool(),
                WriteEnergyConfigTool(),
                ListRestCommandsTool(self.rest_command_manager),
                GetRestCommandTool(self.rest_command_manager),
                WriteRestCommandTool(writer),
                DeleteRestCommandTool(writer),
                ListPackagePlatformEntitiesTool(platforms),
                WritePackagePlatformEntityTool(platforms, writer),
                DeletePackagePlatformEntitiesTool(platforms),
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
