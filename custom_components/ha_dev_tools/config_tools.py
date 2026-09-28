"""Config validation and reload - never a full restart for automation changes.

`check_ha_config` wraps HA's own config-check helper (what `homeassistant.
check_config` / the UI's "Check configuration" button uses) with a
structured result instead of a plain joined string. `reload_domain` wraps
`<domain>.reload` service calls (automation/script/scene/input_boolean
etc. all support the pattern) - always prefer this over restarting.

HA's config check alone misses the most common real breakage: an
automation or script that *parses* fine but that HA refuses to set up (an
invalid trigger value, `Invalid time specified: 61200`, ...). HA doesn't
fail the check or log it - it loads the item as an unavailable entity and
raises a Repairs issue instead (issue #89). `active_repairs` reads HA's
issue registry, the same data the Repairs page shows, and `setup_error`
finds the one for a specific automation/script right after a write.
"""

from __future__ import annotations

import string
from typing import Any

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.check_config import async_check_ha_config_file
from homeassistant.helpers.translation import async_get_translations

# Domains whose "failed to set up" repairs mean a config item HA refused to
# load - automation/script create them as `validation_failed_<part>` with
# the item's id at the end of the `edit` placeholder (/config/<domain>/
# edit/<id>) and HA's own error text in `error`.
_SETUP_FAILURE_DOMAINS = ("automation", "script")

_PROBLEM_STATES = (
    ConfigEntryState.SETUP_ERROR,
    ConfigEntryState.SETUP_RETRY,
    ConfigEntryState.MIGRATION_ERROR,
    ConfigEntryState.FAILED_UNLOAD,
)


class _KeepMissing(dict[str, Any]):
    """format_map mapping that leaves unknown {placeholders} as written."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render(template: str | None, placeholders: dict[str, Any]) -> str | None:
    if template is None:
        return None
    try:
        return string.Formatter().vformat(template, (), _KeepMissing(placeholders))
    except (ValueError, IndexError):
        return template


def _is_setup_failure(issue: ir.IssueEntry) -> bool:
    return issue.domain in _SETUP_FAILURE_DOMAINS and (
        issue.translation_key or ""
    ).startswith("validation_failed")


def _item_id(issue: ir.IssueEntry) -> str | None:
    edit = (issue.translation_placeholders or {}).get("edit")
    return edit.rsplit("/", 1)[-1] if edit else None


async def active_repairs(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Every active, non-dismissed Repairs issue, with HA's own title and
    description text rendered the way the Repairs page shows them."""
    issues = [
        issue
        for issue in ir.async_get(hass).issues.values()
        if issue.active and issue.dismissed_version is None
    ]
    domains = {issue.domain for issue in issues}
    translations = (
        await async_get_translations(hass, hass.config.language, "issues", domains)
        if domains
        else {}
    )
    repairs = []
    for issue in issues:
        placeholders = issue.translation_placeholders or {}
        prefix = f"component.{issue.domain}.issues.{issue.translation_key}"
        repairs.append(
            {
                "domain": issue.domain,
                "issue_id": issue.issue_id,
                "severity": issue.severity,
                "title": _render(translations.get(f"{prefix}.title"), placeholders),
                "description": _render(
                    translations.get(f"{prefix}.description"), placeholders
                ),
                "translation_key": issue.translation_key,
                "placeholders": placeholders,
                "is_fixable": issue.is_fixable,
                "learn_more_url": issue.learn_more_url,
                "created": issue.created.isoformat(),
            }
        )
    return repairs


def setup_error(hass: HomeAssistant, domain: str, item_id: str) -> str | None:
    """HA's own error for an automation/script it refused to set up, or None.

    Read straight after a write's reload: the unavailable entity HA creates
    for a broken item raises its repair while it's being added, so the
    repair is already there by the time the reload service call returns.
    """
    for issue in ir.async_get(hass).issues.values():
        if (
            issue.active
            and issue.domain == domain
            and _is_setup_failure(issue)
            and _item_id(issue) == str(item_id)
        ):
            return (issue.translation_placeholders or {}).get("error") or (
                issue.translation_key
            )
    return None


async def check_ha_config(hass: HomeAssistant) -> dict[str, Any]:
    """Validate the full HA configuration without writing or restarting anything."""
    result = await async_check_ha_config_file(hass)
    repairs = await active_repairs(hass)
    setup_failures = [
        {
            "domain": issue.domain,
            "id": _item_id(issue),
            "entity_id": (issue.translation_placeholders or {}).get("entity_id"),
            "error": (issue.translation_placeholders or {}).get("error"),
        }
        for issue in ir.async_get(hass).issues.values()
        if issue.active and _is_setup_failure(issue)
    ]
    return {
        "valid": not result.errors and not setup_failures,
        "errors": [
            {"message": err.message, "domain": err.domain} for err in result.errors
        ],
        "warnings": [
            {"message": warn.message, "domain": warn.domain} for warn in result.warnings
        ],
        "setup_failures": setup_failures,
        "repairs": repairs,
        "config_entry_problems": config_entry_problems(hass),
    }


# Reloading these would tear down the MCP session making the call.
_NEVER_RELOAD = ("ha_dev_tools", "mcp_server")


def _entry_summary(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    reauth = any(
        flow["context"].get("source") == SOURCE_REAUTH
        and flow["context"].get("entry_id") == entry.entry_id
        for flow in hass.config_entries.flow.async_progress()
    )
    return {
        "entry_id": entry.entry_id,
        "domain": entry.domain,
        "title": entry.title,
        "state": entry.state.value,
        "reason": entry.reason,
        "reauth_required": reauth,
    }


def config_entry_problems(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Config entries (UI-set-up integrations) that aren't working (issue #14):
    failed or retrying setup, a failed migration/unload, or waiting for the
    user to re-authenticate - with HA's own reason. Disabled entries are
    skipped: that's deliberate, not a problem."""
    problems = []
    for entry in hass.config_entries.async_entries():
        if entry.disabled_by is not None:
            continue
        summary = _entry_summary(hass, entry)
        if entry.state in _PROBLEM_STATES or summary["reauth_required"]:
            problems.append(summary)
    return problems


async def reload_domain(
    hass: HomeAssistant, domain: str, entry_id: str | None = None
) -> dict[str, Any]:
    """Reload a domain without restarting Home Assistant.

    YAML domains (automation, script, scene, ...) use `<domain>.reload`.
    An integration set up through the UI has no such service - its config
    entry is reloaded instead, the same as the UI's "Reload" (issue #14).
    `entry_id` picks one entry explicitly; without it, a domain with
    several entries returns them to choose from rather than reloading all.
    """
    if domain in _NEVER_RELOAD:
        return {
            "reloaded": False,
            "error": (
                f"Refusing to reload '{domain}': it serves this MCP session, "
                "so reloading it would drop the connection mid-call."
            ),
        }
    if entry_id is None and hass.services.has_service(domain, "reload"):
        await hass.services.async_call(domain, "reload", blocking=True)
        return {"reloaded": True, "domain": domain}

    entries = hass.config_entries.async_entries(domain)
    if entry_id is not None:
        entries = [e for e in entries if e.entry_id == entry_id]
    if not entries:
        return {
            "reloaded": False,
            "error": (
                f"No config entry '{entry_id}' for domain '{domain}'"
                if entry_id is not None
                else f"Domain '{domain}' has no reload service and no config entries"
            ),
        }
    if len(entries) > 1:
        return {
            "reloaded": False,
            "error": f"Domain '{domain}' has {len(entries)} config entries - "
            "pass entry_id to pick one.",
            "entries": [_entry_summary(hass, e) for e in entries],
        }
    entry = entries[0]
    reloaded = await hass.config_entries.async_reload(entry.entry_id)
    return {
        "reloaded": reloaded,
        "domain": domain,
        "entry": _entry_summary(hass, entry),
    }
