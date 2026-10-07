"""Read-only access to automation and script traces.

Home Assistant keeps the last few runs of every automation and script (5 by
default, `stored_traces` per automation/script), in memory and saved across
restarts. Each run records which trigger fired, every trigger, condition
and action step with its result, the variables each step changed, and any
error - the automation editor's "Traces" view. That answers "why did (or
didn't) this automation do X" directly, where the log and logbook only
answer it indirectly.

The traces live under a private `hass.data` key with no public Python API,
so this goes through the trace integration's own WebSocket commands
(`trace/list`, `trace/get` - what the Traces view itself calls), via
ws_call.py's loopback like dashboards and the Energy config. Both are
admin-only there too.

Read-only on purpose: the trace integration's breakpoint/step debugger
commands pause live runs, so they aren't offered here.
"""

from __future__ import annotations

from typing import Any

from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .ws_call import call_ws_command

DOMAINS = ("automation", "script")
DEFAULT_LIST_LIMIT = 20


class TraceNotFoundError(Exception):
    """No such automation/script, or no stored trace for it."""


def resolve_item(
    hass: HomeAssistant,
    *,
    domain: str | None = None,
    item_id: str | None = None,
    entity_id: str | None = None,
) -> tuple[str, str | None]:
    """(domain, item_id) to read traces for, from either form.

    A trace's item_id is an automation's config `id` (its unique_id, not
    its entity_id - those diverge as soon as either is renamed) or a
    script's object id (`script.<object_id>`'s key in scripts.yaml).
    """
    if entity_id is None:
        domain = domain or "automation"
        if domain not in DOMAINS:
            raise ValueError(f"'domain' must be one of {', '.join(DOMAINS)}")
        return domain, item_id

    entity_domain, _, object_id = entity_id.partition(".")
    if entity_domain not in DOMAINS or not object_id:
        raise ValueError(f"'{entity_id}' is not an automation.* or script.* entity_id")
    if domain is not None and domain != entity_domain:
        raise ValueError(f"'{entity_id}' is not in domain '{domain}'")
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None and entry.platform == entity_domain:
        return entity_domain, entry.unique_id
    if entity_domain == "script":
        return entity_domain, object_id
    raise TraceNotFoundError(
        f"No registered automation with entity_id '{entity_id}'. Home "
        "Assistant only keeps traces for automations that have an 'id'."
    )


def _entity_ids(hass: HomeAssistant, domain: str) -> dict[str, str]:
    """{item_id: entity_id} for this domain's registered entities."""
    return {
        entry.unique_id: entry.entity_id
        for entry in er.async_get(hass).entities.values()
        if entry.domain == domain and entry.platform == domain
    }


def _summary(trace: dict[str, Any], entity_ids: dict[str, str]) -> dict[str, Any]:
    """One run's short form, flattened."""
    timestamp = trace.get("timestamp") or {}
    summary: dict[str, Any] = {
        "domain": trace.get("domain"),
        "item_id": trace.get("item_id"),
        "entity_id": entity_ids.get(trace.get("item_id") or ""),
        "run_id": trace.get("run_id"),
        "start": timestamp.get("start"),
        "finish": timestamp.get("finish"),
        "state": trace.get("state"),
        "script_execution": trace.get("script_execution"),
        "last_step": trace.get("last_step"),
    }
    if "trigger" in trace:
        summary["trigger"] = trace["trigger"]
    if trace.get("error") is not None:
        summary["error"] = trace["error"]
    if trace.get("not_triggered"):
        summary["not_triggered"] = True
    return summary


async def list_traces(
    hass: HomeAssistant,
    user: User,
    *,
    domain: str,
    item_id: str | None = None,
    errors_only: bool = False,
    include_not_triggered: bool = False,
    limit: int = DEFAULT_LIST_LIMIT,
) -> dict[str, Any]:
    """Stored runs, newest first."""
    kwargs: dict[str, Any] = {"domain": domain}
    if item_id is not None:
        kwargs["item_id"] = item_id
    traces = await call_ws_command(hass, user, "trace/list", **kwargs)
    entity_ids = _entity_ids(hass, domain)
    rows = [_summary(trace, entity_ids) for trace in traces]
    if not include_not_triggered:
        rows = [row for row in rows if not row.get("not_triggered")]
    if errors_only:
        rows = [row for row in rows if "error" in row]
    rows.sort(key=lambda row: row["start"] or "", reverse=True)
    return {
        "traces": rows[:limit],
        "total": len(rows),
        "truncated": len(rows) > limit,
    }


async def _latest_run_id(
    hass: HomeAssistant, user: User, domain: str, item_id: str
) -> str:
    listed = await list_traces(hass, user, domain=domain, item_id=item_id, limit=1)
    if not listed["traces"]:
        raise TraceNotFoundError(
            f"No stored traces for {domain} '{item_id}' - it hasn't run since "
            "it was created (or its traces were cleared), or the id is wrong. "
            "list_traces without an item_id shows what has traces."
        )
    return listed["traces"][0]["run_id"]


def _steps(
    trace: dict[str, list[dict[str, Any]]], include_variables: bool
) -> list[dict[str, Any]]:
    """Every recorded step, in the order it ran.

    HA groups steps by path (`trigger/0`, `condition/1`, `action/2/then/0`,
    ...); a path that ran more than once (a repeat) has several entries.
    """
    steps = []
    for path, elements in trace.items():
        for element in elements:
            step = {**element, "path": path}
            if not include_variables:
                step.pop("changed_variables", None)
            steps.append(step)
    steps.sort(key=lambda step: step.get("timestamp") or "")
    return steps


async def get_trace(
    hass: HomeAssistant,
    user: User,
    *,
    domain: str,
    item_id: str,
    run_id: str | None = None,
    include_variables: bool = True,
    include_config: bool = False,
) -> dict[str, Any]:
    """One run in full: summary, context and every step in order."""
    if run_id is None:
        run_id = await _latest_run_id(hass, user, domain, item_id)
    trace = await call_ws_command(
        hass, user, "trace/get", domain=domain, item_id=item_id, run_id=run_id
    )
    result = _summary(trace, _entity_ids(hass, domain))
    context = trace.get("context") or {}
    result["context"] = {
        "id": context.get("id"),
        "parent_id": context.get("parent_id"),
        "user_id": context.get("user_id"),
    }
    result["steps"] = _steps(trace.get("trace") or {}, include_variables)
    if trace.get("blueprint_inputs") is not None:
        result["blueprint_inputs"] = trace["blueprint_inputs"]
    if include_config:
        result["config"] = trace.get("config")
    return result
