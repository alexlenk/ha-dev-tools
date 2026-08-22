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

from dataclasses import dataclass
from typing import Any, override

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType

from . import (
    access_control,
    audit_manager,
    config_tools,
    dashboard_manager,
    derived_sensor_manager,
    entity_manager,
    helper_manager,
    history_manager,
    supervisor_manager,
    template_manager,
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
from .helper_manager import (
    HELPER_DOMAINS,
    InvalidHelperDomainError,
    UnresolvedUserError,
)
from .history_manager import RecorderNotAvailableError
from .log_manager import LogFilters, LogManager
from .supervisor_manager import SupervisorNotAvailableError
from .template_yaml_manager import (
    DuplicateTemplateUniqueIdError,
    TemplateEntityNotFoundError,
    TemplateYamlManager,
)
from .ws_call import WebSocketCommandError

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


def _flow_step_required_payload(exc: FlowStepRequiredError) -> JsonObjectType:
    """Structured (not error) payload for a config/options flow step needing input.

    Deliberately not routed through _tool_error - that would collapse
    exc.step_id/exc.schema into a plain string and lose exactly the
    information a caller needs to retry correctly.
    """
    return {
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


def _parse_datetime(value: str, *, field: str) -> Any:
    """Parse an ISO 8601 datetime string, raising ValueError with a clear message on failure."""
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        raise ValueError(f"'{field}' is not a valid ISO 8601 datetime: {value!r}")
    return parsed


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
        access_control.check_armed(hass)
        await access_control.require_admin(hass, llm_context)
        result = await self._run(hass, tool_input, llm_context)
        access_control.touch_armed(hass)
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

    When this integration's dry-run option is enabled, the underlying
    write never runs at all - the call's own validated arguments are
    returned as a "would_apply" preview instead, so the agent can show
    the user what it was about to do before anything actually changes.
    This is a policy block, not a simulation: it does not attempt to
    verify the write would have succeeded (e.g. path/schema checks),
    only that it didn't happen.
    """

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Short-circuit into a dry-run preview, or hand off to _write()."""
        if access_control.is_dry_run(hass):
            return {
                "dry_run": True,
                "action": self.name,
                "would_apply": tool_input.tool_args,
                "note": (
                    "Dry-run mode is enabled for this integration - no "
                    "changes were made. Show this to the user; dry-run can "
                    "be turned off from this integration's Configure page."
                ),
            }
        return await self._write(hass, tool_input, llm_context)

    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Subclasses implement their actual write logic here, not _run."""
        raise NotImplementedError


class DevToolsPingTool(llm.Tool):
    """Confirm the dev_tools API is registered and reachable.

    Not part of the real tool surface (see README.md's Tools table) - kept
    as a zero-dependency smoke test for the llm.API/mcp_server wiring
    itself, and deliberately the one tool NOT behind GatedTool.
    """

    name = "dev_tools_ping"
    description = (
        "Check that the ha_dev_tools API is registered and reachable. "
        "Returns a static status payload; has no side effects."
    )
    parameters = vol.Schema({})

    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Return a static status payload."""
        return {"status": "ok", "domain": DOMAIN}


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
            start_time = _parse_datetime(args["start_time"], field="start_time")
            end_time = (
                _parse_datetime(args["end_time"], field="end_time")
                if args.get("end_time")
                else None
            )
            return await history_manager.get_entity_history(
                hass,
                args["entity_ids"],
                start_time=start_time,
                end_time=end_time,
                significant_changes_only=args.get("significant_changes_only", True),
                limit=args.get("limit", 200),
            )
        except (RecorderNotAvailableError, ValueError) as exc:
            return _tool_error(exc)


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
            start_time = _parse_datetime(args["start_time"], field="start_time")
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
        return {"addons": addons}


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
        "the UI's 'Check configuration' button). Always run this after "
        "writing an automation, before assuming it's correct."
    )
    parameters = vol.Schema({})

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Run HA's own config check."""
        return await config_tools.check_ha_config(hass)


class ReloadDomainTool(GatedTool):
    """Reload a domain's config (automation/script/scene/...) - never a full restart."""

    name = "reload_domain"
    description = (
        "Reload a domain's configuration without restarting Home Assistant "
        "- e.g. domain='automation' after editing automations. Most config "
        "domains (automation, script, scene, input_boolean, ...) support "
        "this; call check_config first if you're not sure the edit is valid."
    )
    parameters = vol.Schema({vol.Required("domain"): str})

    @override
    async def _run(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Reload the given domain."""
        return await config_tools.reload_domain(hass, tool_input.tool_args["domain"])


class GetAutomationTool(GatedTool):
    """Layout-aware automation read - resolves the file that actually defines it."""

    name = "get_automation"
    description = (
        "Read an automation's config by id, resolving which file actually "
        "defines it (the default automations.yaml, or a packages/*.yaml "
        "file) - a plain file read can silently miss package-defined "
        "automations. Fails clearly if the id isn't found or is defined in "
        "more than one file, rather than guessing."
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
        return {
            "file_path": location.file_path,
            "is_package": location.is_package,
            "config": config,
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
    )
    parameters = vol.Schema(
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
            location = await self._manager.write_automation(
                args["automation_id"],
                args["config"],
                package=args.get("package"),
                expected_hash=args.get("expected_hash"),
            )
        except (AutomationNotFoundError, DuplicateAutomationIdError, ValueError) as exc:
            return _tool_error(exc)
        return {"file_path": location.file_path, "is_package": location.is_package}


def _helper_domain_schema() -> vol.Schema:
    return vol.In(HELPER_DOMAINS)


class ListHelpersTool(GatedTool):
    """List every storage-defined item in a helper domain (input_boolean, counter, etc.)."""

    name = "list_helpers"
    description = (
        "List every helper (input_boolean, input_number, input_text, "
        "input_select, input_datetime, input_button, counter, timer, or "
        "schedule) currently defined via the UI/storage in the given "
        "domain. Does not include YAML-defined helpers of the same "
        "domain - those aren't reachable this way (see get_automation's "
        "'layout-aware' approach for the analogous YAML case)."
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
        return {"items": items}


class CreateHelperTool(WriteGatedTool):
    """Create a new helper item."""

    name = "create_helper"
    description = (
        "Create a new helper (input_boolean, counter, timer, etc.) via "
        "the same mechanism the UI's Helpers page uses. 'config' fields "
        "vary by domain - e.g. input_boolean/counter/timer mainly need "
        "'name'; input_number additionally needs 'min'/'max'; "
        "input_select needs 'options' (a list)."
    )
    parameters = vol.Schema(
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
        try:
            user = await helper_manager.resolve_user(hass, llm_context)
            created = await helper_manager.create_helper(
                hass, user, args["domain"], args["config"]
            )
        except (
            UnresolvedUserError,
            InvalidHelperDomainError,
            WebSocketCommandError,
        ) as exc:
            return _tool_error(exc)
        return created


class UpdateHelperTool(WriteGatedTool):
    """Update an existing helper item by id."""

    name = "update_helper"
    description = (
        "Update an existing storage-defined helper by id. Only works on "
        "helpers created via the UI/storage, not YAML-defined ones - "
        "list_helpers' results only include the former for exactly this "
        "reason."
    )
    parameters = vol.Schema(
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
        try:
            user = await helper_manager.resolve_user(hass, llm_context)
            updated = await helper_manager.update_helper(
                hass, user, args["domain"], args["item_id"], args["config"]
            )
        except (
            UnresolvedUserError,
            InvalidHelperDomainError,
            WebSocketCommandError,
        ) as exc:
            return _tool_error(exc)
        return updated


class DeleteHelperTool(WriteGatedTool):
    """Delete a helper item by id."""

    name = "delete_helper"
    description = "Delete a storage-defined helper by id."
    parameters = vol.Schema(
        {vol.Required("domain"): _helper_domain_schema(), vol.Required("item_id"): str}
    )

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Delete the helper."""
        args = tool_input.tool_args
        try:
            user = await helper_manager.resolve_user(hass, llm_context)
            await helper_manager.delete_helper(
                hass, user, args["domain"], args["item_id"]
            )
        except (
            UnresolvedUserError,
            InvalidHelperDomainError,
            WebSocketCommandError,
        ) as exc:
            return _tool_error(exc)
        return {"deleted": True, "domain": args["domain"], "item_id": args["item_id"]}


def _derived_sensor_domain_schema() -> vol.Schema:
    return vol.In(DERIVED_SENSOR_DOMAINS)


class ListDerivedSensorsTool(GatedTool):
    """List config-entry-based derived/calculated sensor helpers."""

    name = "list_derived_sensors"
    description = (
        "List calculated/derived sensor helpers - Min/Max, Utility Meter, "
        "Integration (Riemann sum), Statistics, Threshold, Derivative, and "
        "Filter - a second helper family alongside list_helpers' nine "
        "domains, implemented as config entries rather than storage items. "
        "Omit domain to list across all seven. Does not cover Template "
        "helpers or YAML-defined template: sensors - not yet supported by "
        "any tool here."
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
        return {"items": items}


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
    "fixed multi-step sequence, and others (filter) branch into a "
    "different step depending on an earlier answer. Call with steps={} (or "
    "omitted) first: the response comes back with needs_input=true, the "
    "current step_id, and that step's field schema. Fill those fields in "
    "under steps[step_id] and call again - repeat, accumulating entries in "
    "steps, until the call returns the created/updated entry instead of "
    "needs_input."
)


class CreateDerivedSensorTool(WriteGatedTool):
    """Create a new derived-sensor config entry by driving its real config flow."""

    name = "create_derived_sensor"
    description = (
        "Create a new calculated/derived sensor helper (min_max, "
        "utility_meter, integration [Riemann sum], statistics, threshold, "
        "derivative, or filter) via the same config flow the UI's Add "
        "Helper wizard uses. " + _DERIVED_SENSOR_STEPS_DESCRIPTION
    )
    parameters = vol.Schema(
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
            return await derived_sensor_manager.create_derived_sensor(
                hass, args["domain"], args.get("steps") or {}
            )
        except FlowStepRequiredError as exc:
            return _flow_step_required_payload(exc)
        except (InvalidDerivedSensorDomainError, FlowAbortedError) as exc:
            return _tool_error(exc)


class UpdateDerivedSensorTool(WriteGatedTool):
    """Update an existing derived-sensor entry by driving its real options flow."""

    name = "update_derived_sensor"
    description = (
        "Update an existing calculated/derived sensor helper's config by "
        "its entry id (from list_derived_sensors), via the same options "
        "flow the UI's helper edit page uses. " + _DERIVED_SENSOR_STEPS_DESCRIPTION
    )
    parameters = vol.Schema(
        {
            vol.Required("entry_id"): str,
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
        """Drive the options flow forward with the given steps."""
        args = tool_input.tool_args
        try:
            return await derived_sensor_manager.update_derived_sensor(
                hass, args["entry_id"], args.get("steps") or {}
            )
        except FlowStepRequiredError as exc:
            return _flow_step_required_payload(exc)
        except (DerivedSensorNotFoundError, FlowAbortedError) as exc:
            return _tool_error(exc)


class DeleteDerivedSensorTool(WriteGatedTool):
    """Delete a derived-sensor config entry by id."""

    name = "delete_derived_sensor"
    description = "Delete a calculated/derived sensor helper by its entry id."
    parameters = vol.Schema({vol.Required("entry_id"): str})

    @override
    async def _write(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Delete the derived-sensor entry."""
        try:
            return await derived_sensor_manager.delete_derived_sensor(
                hass, tool_input.tool_args["entry_id"]
            )
        except DerivedSensorNotFoundError as exc:
            return _tool_error(exc)


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
        return {"items": await self._manager.list_entities()}


class GetTemplateEntityTool(GatedTool):
    """Read one YAML template: entity's config by unique_id."""

    name = "get_template_entity"
    description = (
        "Read a YAML `template:` entity's current config by its unique_id "
        "(from list_template_entities)."
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
            "config": config,
        }


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
        "optional (a list of trigger dicts) for the new block."
    )
    parameters = vol.Schema(
        {
            vol.Required("platform"): str,
            vol.Required("config"): dict,
            vol.Required("package"): str,
            vol.Optional("triggers"): list,
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
            location, reloaded = await self._manager.create_entity(
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
        return {
            "file_path": location.file_path,
            "platform": location.platform,
            "reloaded": reloaded,
        }


class UpdateTemplateEntityTool(WriteGatedTool):
    """Update an existing YAML template: entity's config in place."""

    name = "update_template_entity"
    description = (
        "Update an existing YAML `template:` entity's config by its "
        "unique_id (from list_template_entities). Only that entity's own "
        "dict is replaced - sibling entities and the block's triggers/"
        "conditions/variables are left untouched. To change an entity's "
        "unique_id, use delete_template_entity + create_template_entity "
        "instead (a rename is really two operations, not supported "
        "directly here). Fails with a permission error if the entity "
        "lives in configuration.yaml itself, which is read-only under "
        "this integration's default security policy - only "
        "package-defined entities can be updated this way."
    )
    parameters = vol.Schema(
        {vol.Required("unique_id"): str, vol.Required("config"): dict}
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
        """Update the template entity."""
        args = tool_input.tool_args
        try:
            location, reloaded = await self._manager.update_entity(
                args["unique_id"], args["config"]
            )
        except (
            ValueError,
            TemplateEntityNotFoundError,
            DuplicateTemplateUniqueIdError,
        ) as exc:
            return _tool_error(exc)
        return {
            "file_path": location.file_path,
            "platform": location.platform,
            "reloaded": reloaded,
        }


class DeleteTemplateEntityTool(WriteGatedTool):
    """Delete a YAML template: entity by unique_id."""

    name = "delete_template_entity"
    description = (
        "Delete a YAML `template:` entity by its unique_id. Cleans up "
        "after itself - removes the platform key if that empties it, and "
        "the whole template: block if that empties it too. Same "
        "read-only-configuration.yaml restriction as "
        "update_template_entity."
    )
    parameters = vol.Schema({vol.Required("unique_id"): str})

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
        """Delete the template entity."""
        try:
            location, reloaded = await self._manager.delete_entity(
                tool_input.tool_args["unique_id"]
            )
        except (TemplateEntityNotFoundError, DuplicateTemplateUniqueIdError) as exc:
            return _tool_error(exc)
        return {
            "file_path": location.file_path,
            "platform": location.platform,
            "reloaded": reloaded,
        }


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
    )
    parameters = vol.Schema(
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
        try:
            user = await helper_manager.resolve_user(hass, llm_context)
            await dashboard_manager.write_dashboard(
                hass, user, args["config"], url_path=args.get("url_path")
            )
        except (
            UnresolvedUserError,
            YamlModeDashboardError,
            WebSocketCommandError,
        ) as exc:
            return _tool_error(exc)
        return {"saved": True, "url_path": args.get("url_path")}


class AuditAutomationsTool(GatedTool):
    """Static analysis over every known automation for latent reliability bugs."""

    name = "audit_automations"
    description = (
        "Audit every automation (default file and all packages) for "
        "duplicate ids across files, and for triggers/conditions/actions "
        "referencing an entity that is currently unavailable or unknown - "
        "the class of bug that fails silently with no error anywhere. "
        "Does not yet detect overlapping-trigger race conditions or "
        "unhandled rest_command/shell_command failures (see the result's "
        "'note' field)."
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


@dataclass(slots=True, kw_only=True)
class DevToolsAPI(llm.API):
    """The ha_dev_tools LLM API - holds the backing services real tools are built from."""

    log_manager: LogManager
    automation_manager: AutomationManager
    template_yaml_manager: TemplateYamlManager

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
                RenderTemplateTool(),
                ValidateTemplateTool(),
                GetLogsTool(self.log_manager),
                GetEntityHistoryTool(),
                GetLogbookTool(),
                ListAddonsTool(),
                GetAddonLogsTool(),
                CheckConfigTool(),
                ReloadDomainTool(),
                GetAutomationTool(self.automation_manager),
                WriteAutomationTool(self.automation_manager),
                AuditAutomationsTool(self.automation_manager),
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
                GetDashboardTool(),
                WriteDashboardTool(),
            ],
        )


def async_register(
    hass: HomeAssistant,
    *,
    log_manager: LogManager,
    automation_manager: AutomationManager,
    template_yaml_manager: TemplateYamlManager,
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
            template_yaml_manager=template_yaml_manager,
        ),
    )
