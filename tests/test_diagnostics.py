"""Tests for the diagnostics platform (diagnostics.py).

Covers: the mirroring access token is genuinely redacted (not just absent
by coincidence), the non-sensitive fields are present and correct, and the
result is sane whether or not mirroring/dry-run are configured. Doesn't
exercise Home Assistant's own download-diagnostics HTTP view - that's
Home Assistant's code, not ours; async_get_config_entry_diagnostics is the
only function this integration owns.
"""

import time

import pytest
from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_dev_tools import access_control, diagnostics
from custom_components.ha_dev_tools.const import (
    DOMAIN,
    OPT_DRY_RUN,
    OPT_MIRROR_ENABLED,
    OPT_MIRROR_REPO,
    OPT_MIRROR_TOKEN,
)


@pytest.fixture(autouse=True)
def _clean_arm_file(hass: HomeAssistant):
    """See test_access_control.py's fixture of the same name - the arm
    file lives in a shared, non-per-test-isolated config dir."""
    path = access_control._arm_file_path(hass)
    path.unlink(missing_ok=True)
    yield
    path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_redacts_mirror_token(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_supersecrettoken",
        },
    )
    entry.add_to_hass(hass)

    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    assert "ghp_supersecrettoken" not in str(result)
    assert result["options"][OPT_MIRROR_TOKEN] == REDACTED


@pytest.mark.asyncio
async def test_non_sensitive_fields_present_with_mirroring_configured(
    hass: HomeAssistant,
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            OPT_DRY_RUN: True,
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_supersecrettoken",
        },
    )
    entry.add_to_hass(hass)

    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    assert result["dry_run_enabled"] is True
    assert result["mirroring_enabled"] is True
    assert result["options"][OPT_MIRROR_REPO] == "alexlenk/ha-mirror"
    assert isinstance(result["integration_version"], str)
    assert result["integration_version"]
    assert result["armed"] is False


@pytest.mark.asyncio
async def test_defaults_when_nothing_configured(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)

    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    assert result["dry_run_enabled"] is False
    assert result["mirroring_enabled"] is False
    assert result["options"].get(OPT_MIRROR_TOKEN, "") == ""
    assert result["armed"] is False


@pytest.mark.asyncio
async def test_armed_true_when_arm_file_fresh(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)

    path = access_control._arm_file_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()))

    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    assert result["armed"] is True


@pytest.mark.asyncio
async def test_armed_false_when_arm_file_expired(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)

    path = access_control._arm_file_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    path.write_text(str(now))
    import os

    os.utime(path, (now, now - access_control.IDLE_TIMEOUT.total_seconds() - 1))

    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    assert result["armed"] is False
