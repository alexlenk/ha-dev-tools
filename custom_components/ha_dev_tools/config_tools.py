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

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.check_config import async_check_ha_config_file
from homeassistant.helpers.translation import async_get_translations

# Domains whose "failed to set up" repairs mean a config item HA refused to
# load - automation/script create them as `validation_failed_<part>` with
# the item's id at the end of the `edit` placeholder (/config/<domain>/
# edit/<id>) and HA's own error text in `error`.
_SETUP_FAILURE_DOMAINS = ("automation", "script")


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
    }


async def reload_domain(hass: HomeAssistant, domain: str) -> dict[str, Any]:
    """Call `<domain>.reload` (e.g. automation, script, scene) instead of restarting."""
    if not hass.services.has_service(domain, "reload"):
        return {
            "reloaded": False,
            "error": f"Domain '{domain}' has no reload service",
        }
    await hass.services.async_call(domain, "reload", blocking=True)
    return {"reloaded": True, "domain": domain}
