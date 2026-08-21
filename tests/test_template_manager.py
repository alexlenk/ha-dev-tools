"""Tests for template render/validate (template_manager.py), against real Jinja2/HA templating."""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools.template_manager import (
    render_template,
    validate_template,
)


@pytest.mark.asyncio
async def test_render_template_success(hass: HomeAssistant):
    hass.states.async_set("sensor.temperature", "21.5")

    result = await render_template(hass, "{{ states('sensor.temperature') }}")

    # parse_result=True: numeric-looking output is parsed to a real number,
    # not left as a string - real behavior, confirmed here rather than assumed.
    assert result == {"success": True, "result": 21.5}


@pytest.mark.asyncio
async def test_render_template_with_variables(hass: HomeAssistant):
    result = await render_template(hass, "{{ x + y }}", variables={"x": 2, "y": 3})

    assert result == {"success": True, "result": 5}


@pytest.mark.asyncio
async def test_render_template_error_surfaced_not_raised(hass: HomeAssistant):
    result = await render_template(hass, "{{ 1 / 0 }}")

    assert result["success"] is False
    assert "error" in result


@pytest.mark.asyncio
async def test_validate_template_syntax_error(hass: HomeAssistant):
    result = await validate_template(hass, "{{ states('sensor.x'")  # unclosed brace

    assert result["valid"] is False
    assert "syntax_error" in result
    assert result["referenced_entities"] == []


@pytest.mark.asyncio
async def test_validate_template_valid_known_entity(hass: HomeAssistant):
    hass.states.async_set("sensor.temperature", "21.5")

    result = await validate_template(hass, "{{ states('sensor.temperature') }}")

    assert result["valid"] is True
    assert result["referenced_entities"] == ["sensor.temperature"]
    assert result["unknown_entities"] == []


@pytest.mark.asyncio
async def test_validate_template_flags_unknown_entity(hass: HomeAssistant):
    result = await validate_template(hass, "{{ states('sensor.does_not_exist') }}")

    assert (
        result["valid"] is True
    )  # renders fine - states() on a missing entity just returns "unknown"
    assert result["referenced_entities"] == ["sensor.does_not_exist"]
    assert result["unknown_entities"] == ["sensor.does_not_exist"]


@pytest.mark.asyncio
async def test_validate_template_render_error_with_valid_syntax(hass: HomeAssistant):
    hass.states.async_set("sensor.text_value", "not_a_number")

    result = await validate_template(hass, "{{ states('sensor.text_value') | float }}")

    assert result["valid"] is False
    assert "render_error" in result
    assert result["referenced_entities"] == ["sensor.text_value"]
