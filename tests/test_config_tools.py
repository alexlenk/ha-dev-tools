"""Tests for config validation and reload (config_tools.py)."""

from pathlib import Path
from unittest.mock import AsyncMock

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
