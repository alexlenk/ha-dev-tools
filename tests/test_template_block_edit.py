"""Editing a trigger-based template block's triggers in place (issue #140),
against the real template integration and a real reload."""

from pathlib import Path

import pytest
from homeassistant import config as conf_util
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ha_dev_tools.file_manager import FileManager
from custom_components.ha_dev_tools.security import SecurityManager
from custom_components.ha_dev_tools.template_yaml_manager import TemplateYamlManager

PACKAGE = """\
template:
  - triggers:
      - trigger: event
        event_type: step
    sensor:
      - name: Gas energy
        unique_id: energy_model_gas_energy
        # Builds on its own previous state, like the real accumulator.
        state: "{{ (this.state | float(0)) + 1 }}"
        attributes:
          last_camera: "{{ trigger.event.data.camera }}"
      - name: Gas sibling
        unique_id: energy_model_gas_sibling
        state: "{{ 5 }}"
  - sensor:
      - name: Plain
        unique_id: plain_sensor
        state: "{{ 1 }}"
"""


@pytest.fixture
async def manager(hass: HomeAssistant, tmp_path: Path):
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text(
        "homeassistant:\n  packages: !include_dir_named packages\n"
    )
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "energy_model.yaml").write_text(PACKAGE)
    config = await conf_util.async_hass_config_yaml(hass)
    assert await async_setup_component(hass, "template", config)
    await hass.async_block_till_done()
    security = SecurityManager(hass, {})
    return TemplateYamlManager(hass, FileManager(hass, security))


async def _step(hass: HomeAssistant, event_type: str, camera: str) -> None:
    hass.bus.async_fire(event_type, {"camera": camera})
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_trigger_change_keeps_the_accumulators_state(
    hass: HomeAssistant, manager, tmp_path
):
    await _step(hass, "step", "a")
    await _step(hass, "step", "b")
    assert hass.states.get("sensor.gas_energy").state == "2.0"

    preview = await manager.update_entity(
        "energy_model_gas_energy",
        None,
        block={"triggers": [{"trigger": "event", "event_type": "hourly_step"}]},
        dry_run=True,
    )
    assert [member["unique_id"] for member in preview.block["members"]] == [
        "energy_model_gas_energy",
        "energy_model_gas_sibling",
    ]
    assert preview.block["before"]["triggers"][0]["event_type"] == "step"

    result = await manager.update_entity(
        "energy_model_gas_energy",
        None,
        block={"triggers": [{"trigger": "event", "event_type": "hourly_step"}]},
    )
    await hass.async_block_till_done()
    assert result.reloaded is True
    # Reloaded, state and attributes kept - no restart from 0.
    gas = hass.states.get("sensor.gas_energy")
    assert (gas.state, gas.attributes["last_camera"]) == ("2.0", "b")

    await _step(hass, "step", "c")  # the old trigger no longer fires it
    assert hass.states.get("sensor.gas_energy").state == "2.0"
    await _step(hass, "hourly_step", "d")
    gas = hass.states.get("sensor.gas_energy")
    assert (gas.state, gas.attributes["last_camera"]) == ("3.0", "d")

    written = (tmp_path / "packages" / "energy_model.yaml").read_text()
    assert written.count("event_type: hourly_step") == 1  # once, for both
    assert "unique_id: energy_model_gas_sibling" in written
    assert "# Builds on its own previous state" in written


@pytest.mark.asyncio
async def test_conditions_variables_actions_and_legacy_key_names(
    hass: HomeAssistant, manager, tmp_path
):
    path = tmp_path / "packages" / "energy_model.yaml"
    path.write_text(PACKAGE.replace("  - triggers:\n", "  - trigger:\n", 1))

    await manager.update_entity(
        "energy_model_gas_sibling",
        {"name": "Gas sibling", "state": "{{ 6 }}"},
        block={
            "triggers": [{"trigger": "event", "event_type": "step"}],
            "conditions": [{"condition": "template", "value_template": "{{ true }}"}],
            "variables": {"factor": 2},
            "actions": [{"variables": {"seen": True}}],
        },
    )
    written = path.read_text()
    assert "  - trigger:\n" in written and "triggers:" not in written
    assert "conditions:" in written and "factor: 2" in written
    assert "{{ 6 }}" in written

    await manager.update_entity(
        "energy_model_gas_sibling",
        None,
        block={"conditions": None, "variables": None, "actions": None},
    )
    written = path.read_text()
    assert "conditions:" not in written and "factor" not in written


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("unique_id", "config", "block", "expected"),
    [
        (
            "plain_sensor",
            None,
            {"triggers": [{"trigger": "event", "event_type": "x"}]},
            "state-based",
        ),
        ("energy_model_gas_energy", None, {"triggers": []}, "can't lose its triggers"),
        (
            "energy_model_gas_energy",
            None,
            {"triggers": [{"trigger": "nope"}]},
            "Invalid triggers",
        ),
        (
            "energy_model_gas_energy",
            None,
            {"conditions": [{"condition": "nope"}]},
            "Invalid conditions",
        ),
        (
            "energy_model_gas_energy",
            None,
            {"actions": [{"nope": 1}]},
            "Invalid actions",
        ),
        (
            "energy_model_gas_energy",
            None,
            {"variables": ["x"]},
            "variables is a mapping",
        ),
        ("energy_model_gas_energy", None, {"sensor": []}, "not block fields"),
        ("energy_model_gas_energy", None, None, "nothing to change"),
    ],
)
async def test_block_edits_refused_before_writing(
    hass: HomeAssistant, manager, tmp_path, unique_id, config, block, expected
):
    before = (tmp_path / "packages" / "energy_model.yaml").read_text()
    with pytest.raises(ValueError, match=expected):
        await manager.update_entity(unique_id, config, block=block)
    assert (tmp_path / "packages" / "energy_model.yaml").read_text() == before


@pytest.mark.asyncio
async def test_update_template_entity_tool_block_edit(hass: HomeAssistant, manager):
    from homeassistant.helpers import llm

    from custom_components.ha_dev_tools.llm_api import UpdateTemplateEntityTool
    from tests.test_llm_api import _llm_context

    tool = UpdateTemplateEntityTool(manager)
    args = {
        "unique_id": "energy_model_gas_energy",
        "triggers": [{"trigger": "event", "event_type": "hourly_step"}],
    }
    tool.parameters(args)
    preview = await tool._preview_context(
        hass, llm.ToolInput(tool_name=tool.name, tool_args=args), _llm_context()
    )
    assert len(preview["block"]["members"]) == 2
    refused = await tool._preview_context(
        hass,
        llm.ToolInput(
            tool_name=tool.name,
            tool_args={"unique_id": "plain_sensor", "triggers": args["triggers"]},
        ),
        _llm_context(),
    )
    assert "state-based" in refused["problems"]
    plain = await tool._preview_context(
        hass,
        llm.ToolInput(
            tool_name=tool.name,
            tool_args={"unique_id": "plain_sensor", "config": {"state": "{{ 2 }}"}},
        ),
        _llm_context(),
    )
    assert plain == {}

    result = await tool._write(
        hass, llm.ToolInput(tool_name=tool.name, tool_args=args), _llm_context()
    )
    assert result["reloaded"] is True
    assert result["block"]["after"]["triggers"][0]["event_type"] == "hourly_step"
