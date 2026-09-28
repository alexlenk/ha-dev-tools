"""Tests for config validation and reload (config_tools.py)."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools import config_tools

_MINIMAL_CONFIG = """
homeassistant:
  name: Test Home
  latitude: 32.87336
  longitude: 117.22743
  elevation: 430
  unit_system: metric
  time_zone: America/Los_Angeles
"""


@pytest.mark.asyncio
async def test_check_ha_config_valid_default(hass: HomeAssistant, tmp_path: Path):
    """A minimal valid config has no errors.

    Writes its own configuration.yaml into an isolated tmp_path rather than
    relying on hass.config.config_dir's ambient default - async_check_ha_
    config_file needs a real file to find, and the package's shared default
    testing_config directory isn't guaranteed untouched by whatever else ran
    earlier in a full-suite pytest process (same class of issue as the
    sys.modules leak fixed elsewhere in this suite).
    """
    (tmp_path / "configuration.yaml").write_text(_MINIMAL_CONFIG)
    hass.config.config_dir = str(tmp_path)

    result = await config_tools.check_ha_config(hass)

    assert result["valid"] is True
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_reload_domain_calls_service(hass: HomeAssistant):
    mock = AsyncMock()
    hass.services.async_register("automation", "reload", mock)

    result = await config_tools.reload_domain(hass, "automation")

    assert result == {"reloaded": True, "domain": "automation"}
    mock.assert_called_once()


@pytest.mark.asyncio
async def test_reload_domain_without_reload_service(hass: HomeAssistant):
    result = await config_tools.reload_domain(hass, "not_a_real_domain")

    assert result["reloaded"] is False
    assert "error" in result


async def _setup_broken_automation(hass: HomeAssistant) -> None:
    """Load HA's real automation integration with a trigger it can't set up
    (issue #101's comma list) - HA then creates the unavailable entity and
    its Repairs issue itself, nothing here is simulated."""
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "fault_detection",
                    "alias": "Fault Detection",
                    "triggers": [{"trigger": "time_pattern", "minutes": "2,17,32,47"}],
                    "actions": [],
                },
                {
                    "id": "fine",
                    "alias": "Fine",
                    "triggers": [{"trigger": "time_pattern", "minutes": 2}],
                    "actions": [],
                },
            ]
        },
    )
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_check_ha_config_reports_automation_setup_failures(
    hass: HomeAssistant, tmp_path: Path
):
    """Issue #89: HA's config check passes an automation it then refuses to
    set up - the failure only shows as a Repairs issue. check_config must
    report it and not claim the config is valid."""
    (tmp_path / "configuration.yaml").write_text(_MINIMAL_CONFIG)
    hass.config.config_dir = str(tmp_path)
    await _setup_broken_automation(hass)

    result = await config_tools.check_ha_config(hass)

    assert result["errors"] == []
    assert result["valid"] is False
    assert len(result["setup_failures"]) == 1
    failure = result["setup_failures"][0]
    assert failure["domain"] == "automation"
    assert failure["id"] == "fault_detection"
    assert failure["entity_id"] == "automation.fault_detection"
    assert "invalid time_pattern value" in failure["error"]

    # Issues #88/#102: the same repair, rendered as the Repairs page shows it.
    (repair,) = [r for r in result["repairs"] if r["domain"] == "automation"]
    assert repair["title"] == "Automation Fault Detection failed to set up"
    assert "its triggers could not be set up" in repair["description"]
    assert "invalid time_pattern value" in repair["description"]
    assert repair["severity"] == "error"


@pytest.mark.asyncio
async def test_setup_error_finds_the_written_item_only(hass: HomeAssistant):
    await _setup_broken_automation(hass)

    error = config_tools.setup_error(hass, "automation", "fault_detection")
    assert error is not None and "invalid time_pattern value" in error
    assert config_tools.setup_error(hass, "automation", "fine") is None
    assert config_tools.setup_error(hass, "script", "fault_detection") is None


@pytest.mark.asyncio
async def test_active_repairs_skips_dismissed_and_renders_unknown_keys(
    hass: HomeAssistant,
):
    from homeassistant.helpers import issue_registry as ir

    ir.async_create_issue(
        hass,
        "demo_domain",
        "shown",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="no_such_key",
        translation_placeholders={"x": "1"},
    )
    ir.async_create_issue(
        hass,
        "demo_domain",
        "dismissed",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="no_such_key",
    )
    ir.async_ignore_issue(hass, "demo_domain", "dismissed", True)

    repairs = await config_tools.active_repairs(hass)

    assert [r["issue_id"] for r in repairs] == ["shown"]
    # No translation for this key: title/description None, raw key and
    # placeholders still returned so the issue is identifiable.
    assert repairs[0]["title"] is None
    assert repairs[0]["translation_key"] == "no_such_key"
    assert repairs[0]["placeholders"] == {"x": "1"}


def test_render_keeps_unknown_placeholders_and_survives_bad_templates():
    assert config_tools._render("Hi {name} {other}", {"name": "A"}) == "Hi A {other}"
    assert config_tools._render("broken {", {}) == "broken {"
    assert config_tools._render(None, {}) is None


# --- Config-entry reload and problems (issue #14) ----------------------------


def _entry(
    hass: HomeAssistant, domain: str, title: str, state=None, reason=None, **kwargs
):
    """A config entry for a made-up domain (a real integration's domain would
    be imported and unloaded at teardown)."""
    from homeassistant.config_entries import ConfigEntryState
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=domain, title=title, **kwargs)
    entry.add_to_hass(hass)
    entry.mock_state(hass, state or ConfigEntryState.LOADED, reason)
    return entry


@pytest.mark.asyncio
async def test_reload_domain_reloads_a_config_entry_integration(hass: HomeAssistant):
    """A UI-set-up integration has no <domain>.reload service - its config
    entry is reloaded instead, like the UI's Reload."""
    entry = _entry(hass, "test_nuki", "Nuki Bridge")
    with patch.object(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    ) as mock_reload:
        result = await config_tools.reload_domain(hass, "test_nuki")

    mock_reload.assert_awaited_once_with(entry.entry_id)
    assert result["reloaded"] is True
    assert result["entry"]["entry_id"] == entry.entry_id
    assert result["entry"]["title"] == "Nuki Bridge"


@pytest.mark.asyncio
async def test_reload_domain_with_several_entries_asks_for_entry_id(
    hass: HomeAssistant,
):
    first = _entry(hass, "test_ecowitt", "Garden")
    second = _entry(hass, "test_ecowitt", "Roof")

    ambiguous = await config_tools.reload_domain(hass, "test_ecowitt")
    with patch.object(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    ) as mock_reload:
        picked = await config_tools.reload_domain(hass, "test_ecowitt", second.entry_id)
    missing = await config_tools.reload_domain(hass, "test_ecowitt", "nope")

    assert ambiguous["reloaded"] is False
    assert {e["entry_id"] for e in ambiguous["entries"]} == {
        first.entry_id,
        second.entry_id,
    }
    mock_reload.assert_awaited_once_with(second.entry_id)
    assert picked["reloaded"] is True
    assert missing["reloaded"] is False and "No config entry 'nope'" in missing["error"]


@pytest.mark.asyncio
async def test_reload_domain_refuses_to_drop_its_own_session(hass: HomeAssistant):
    for domain in ("ha_dev_tools", "mcp_server"):
        result = await config_tools.reload_domain(hass, domain)
        assert result["reloaded"] is False
        assert "Refusing to reload" in result["error"]


def test_config_entry_problems_lists_failed_retrying_and_reauth(hass: HomeAssistant):
    from homeassistant.config_entries import (
        SOURCE_REAUTH,
        ConfigEntryDisabler,
        ConfigEntryState,
    )

    _entry(hass, "test_fine", "Fine")
    failed = _entry(
        hass, "test_ecowitt", "Garden", ConfigEntryState.SETUP_ERROR, "HTTP 500"
    )
    retrying = _entry(hass, "test_nuki", "Bridge", ConfigEntryState.SETUP_RETRY)
    blink = _entry(hass, "test_blink", "Blink")
    _entry(
        hass,
        "test_old",
        "Old",
        ConfigEntryState.SETUP_ERROR,
        disabled_by=ConfigEntryDisabler.USER,
    )

    with patch.object(
        hass.config_entries.flow,
        "async_progress",
        return_value=[
            {"context": {"source": SOURCE_REAUTH, "entry_id": blink.entry_id}}
        ],
    ):
        problems = config_tools.config_entry_problems(hass)

    by_id = {p["entry_id"]: p for p in problems}
    assert set(by_id) == {failed.entry_id, retrying.entry_id, blink.entry_id}
    assert by_id[failed.entry_id]["state"] == "setup_error"
    assert by_id[failed.entry_id]["reason"] == "HTTP 500"
    assert by_id[retrying.entry_id]["state"] == "setup_retry"
    assert by_id[blink.entry_id]["reauth_required"] is True
    assert by_id[blink.entry_id]["state"] == "loaded"
