"""Tests for the config flow (config_flow.py) and options flow (options_flow.py)."""

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_dev_tools.const import DOMAIN, OPT_DRY_RUN


@pytest.mark.asyncio
async def test_user_flow_shows_form(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


@pytest.mark.asyncio
async def test_user_flow_creates_entry(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "HA Dev Tools"
    assert result["data"] == {}


@pytest.mark.asyncio
async def test_user_flow_aborts_when_already_configured(hass: HomeAssistant):
    MockConfigEntry(domain=DOMAIN).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


@pytest.mark.asyncio
async def test_options_flow_shows_form_with_current_value_as_default(
    hass: HomeAssistant,
):
    entry = MockConfigEntry(domain=DOMAIN, options={OPT_DRY_RUN: True})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["data_schema"]({})[OPT_DRY_RUN] is True


@pytest.mark.asyncio
async def test_options_flow_defaults_to_false_when_unset(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["data_schema"]({})[OPT_DRY_RUN] is False


@pytest.mark.asyncio
async def test_options_flow_enables_dry_run(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {OPT_DRY_RUN: True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_DRY_RUN] is True
