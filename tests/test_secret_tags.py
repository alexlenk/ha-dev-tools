"""`!secret` / `!env_var` references from a tool call are written as YAML
tags, not as quoted text HA would take literally (issue #176) - on new
values, and when an update sends back what a read returned."""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools.yaml_style import quote_ambiguous_scalars
from tests.test_llm_api import _template_file_manager  # noqa: F401 - fixture
from tests.test_llm_api import _template_security_manager  # noqa: F401 - fixture
from tests.test_llm_api import template_yaml_manager  # noqa: F401 - fixture
from tests.test_llm_api import (
    _write_automation_manager,
    _write_package,
    _write_script_manager,
)


def test_quote_ambiguous_scalars_tags_references():
    prepared = quote_ambiguous_scalars(
        {"a": "!secret token", "b": ["!env_var HOME", "!secret"], "c": "yes"}
    )
    assert prepared["a"].tag.value == "!secret" and prepared["a"].value == "token"
    assert prepared["b"][0].tag.value == "!env_var"
    assert prepared["b"][1] == "!secret"  # no name: just text
    assert prepared["c"] == "yes" and type(prepared["c"]) is not str  # quoted


@pytest.mark.asyncio
async def test_write_script_keeps_and_writes_secrets(hass: HomeAssistant, tmp_path):
    manager = _write_script_manager(hass, tmp_path)
    (tmp_path / "scripts.yaml").write_text(
        "notify_me:\n  sequence:\n  - action: notify.x\n    data:\n"
        "      token: !secret my_token\n      message: hi\n"
    )
    # An update that sends back what get_script returned.
    updated = await manager.write_script(
        "notify_me",
        {
            "sequence": [
                {
                    "action": "notify.x",
                    "data": {"token": "!secret my_token", "message": "changed"},
                }
            ]
        },
        dry_run=True,
    )
    assert "token: !secret my_token" in updated.content_after
    assert "'!secret" not in updated.content_after
    new = await manager.write_script(
        "other", {"sequence": [{"delay": "!env_var DELAY"}]}, dry_run=True
    )
    assert "delay: !env_var DELAY" in new.content_after


@pytest.mark.asyncio
async def test_write_automation_writes_secrets(hass: HomeAssistant, tmp_path):
    manager = _write_automation_manager(hass, tmp_path)
    (tmp_path / "automations.yaml").write_text("[]\n")
    result = await manager.write_automation(
        "a",
        {
            "triggers": [],
            "actions": [{"action": "notify.x", "data": {"token": "!secret t"}}],
        },
        dry_run=True,
    )
    assert "token: !secret t" in result.content_after


@pytest.mark.asyncio
async def test_template_entities_write_secrets(
    hass: HomeAssistant, template_yaml_manager, tmp_path  # noqa: F811
):
    _write_package(tmp_path, "packages/t.yaml", "template: []\n")
    created = await template_yaml_manager.create_entity(
        "sensor",
        {"name": "x", "unique_id": "x", "state": "{{ 1 }}", "icon": "!secret icon"},
        package="t.yaml",
    )
    assert "icon: !secret icon" in created.content_after
    updated = await template_yaml_manager.update_entity(
        "x",
        {"name": "y", "unique_id": "x", "state": "{{ 1 }}", "icon": "!secret icon"},
    )
    assert "icon: !secret icon" in updated.content_after
    assert "name: y" in updated.content_after
