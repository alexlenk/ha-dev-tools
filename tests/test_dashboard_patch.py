"""Tests for patch_dashboard (issue #158): the ops on their own, and the
tool against real HA lovelace."""

import copy
import json

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.ha_dev_tools.dashboard_patch import (
    DashboardPatchError,
    apply_ops,
    config_hash,
)
from tests.test_llm_api import _llm_context

# Embedded JavaScript, as in the issue's apexcharts cards - passed through,
# never re-sent.
JS = 'EVAL:function(entity) { return entity.attributes["a\\\\b"] || `x ${1}`; }'


def _dashboard() -> dict:
    return {
        "title": "Solar",
        "views": [
            {
                "title": "Overview",
                "path": "overview",
                "cards": [{"type": "entities", "entities": ["sensor.a"]}],
            },
            {
                "title": "Dev",
                "path": "dev",
                "type": "sections",
                "sections": [
                    {
                        "type": "grid",
                        "cards": [
                            {"type": "heading", "heading": "Today"},
                            {
                                "type": "custom:apexcharts-card",
                                "data_generator": JS,
                                "graph_span": "24h",
                            },
                        ],
                    },
                    {"type": "grid", "title": "Battery", "cards": []},
                ],
            },
            {"title": "Dev", "path": "dev-old", "cards": []},
        ],
    }


NEW_SECTION = {
    "type": "grid",
    "cards": [
        {"type": "heading", "heading": "Export"},
        {"type": "tile", "entity": "sensor.export"},
        {"type": "tile", "entity": "sensor.feed_in"},
    ],
}


# --- the ops ------------------------------------------------------------------


def test_add_a_section_and_remove_it_again():
    config = _dashboard()
    before = copy.deepcopy(config)
    added = apply_ops(
        config, [{"op": "add_section", "view": "dev", "section_config": NEW_SECTION}]
    )
    assert config == before  # the input is left as it is
    sections = added.config["views"][1]["sections"]
    assert sections[2] == NEW_SECTION
    assert sections[:2] == before["views"][1]["sections"]
    assert [added.config["views"][i] for i in (0, 2)] == [
        before["views"][i] for i in (0, 2)
    ]
    assert added.changes == [
        {
            "op": 0,
            "kind": "add_section",
            "pointer": "/views/1/sections/2",
            "after": NEW_SECTION,
        }
    ]

    removed = apply_ops(
        added.config, [{"op": "remove_section", "view": "dev", "section": "Export"}]
    )
    assert config_hash(removed.config) == config_hash(before)
    assert removed.changes[0]["before"] == NEW_SECTION


def test_selectors():
    config = _dashboard()
    # A view by index, path, or title; "Dev" is two views' title, but one
    # view's path is "dev" - the path wins only when it's an exact path.
    for view in (1, "dev"):
        result = apply_ops(config, [{"op": "remove_view", "view": view}])
        assert [v["path"] for v in result.config["views"]] == ["overview", "dev-old"]
    assert apply_ops(config, [{"op": "remove_view", "view": "Overview"}]).changes[0][
        "pointer"
    ] == ("/views/0")
    with pytest.raises(DashboardPatchError, match="'Dev' is ambiguous - 2 match"):
        apply_ops(config, [{"op": "remove_view", "view": "Dev"}])
    with pytest.raises(DashboardPatchError, match="no view 'nope'"):
        apply_ops(config, [{"op": "remove_view", "view": "nope"}])
    with pytest.raises(DashboardPatchError, match="view 7 doesn't exist - there are 3"):
        apply_ops(config, [{"op": "remove_view", "view": 7}])

    # A section by index, title, or a heading card's text.
    for section in (0, "Today"):
        result = apply_ops(
            config, [{"op": "remove_section", "view": "dev", "section": section}]
        )
        assert result.changes[0]["pointer"] == "/views/1/sections/0"
    result = apply_ops(
        config, [{"op": "remove_section", "view": "dev", "section": "Battery"}]
    )
    assert result.changes[0]["pointer"] == "/views/1/sections/1"
    twice = apply_ops(
        config, [{"op": "add_section", "view": 1, "section_config": {"title": "Today"}}]
    ).config
    with pytest.raises(DashboardPatchError, match="section 'Today' is ambiguous"):
        apply_ops(twice, [{"op": "remove_section", "view": 1, "section": "Today"}])


def test_cards_in_sections_and_in_masonry_views():
    config = _dashboard()
    tile = {"type": "tile", "entity": "sensor.b"}
    result = apply_ops(
        config,
        [
            {
                "op": "add_card",
                "view": "dev",
                "section": "Battery",
                "card_config": tile,
            },
            {
                "op": "replace_card",
                "view": "dev",
                "section": 0,
                "card": 1,
                "card_config": tile,
            },
            {"op": "add_card", "view": 0, "card_config": tile, "position": 0},
            {"op": "remove_card", "view": 0, "card": 1},
        ],
    )
    views = result.config["views"]
    assert views[1]["sections"][1]["cards"] == [tile]
    assert views[1]["sections"][0]["cards"][1] == tile
    assert views[0]["cards"] == [tile]
    assert [change["pointer"] for change in result.changes] == [
        "/views/1/sections/1/cards/0",
        "/views/1/sections/0/cards/1",
        "/views/0/cards/0",
        "/views/0/cards/1",
    ]
    assert result.changes[1]["before"]["data_generator"] == JS

    with pytest.raises(DashboardPatchError, match="a sections view - pass section"):
        apply_ops(config, [{"op": "add_card", "view": "dev", "card_config": tile}])
    with pytest.raises(DashboardPatchError, match="isn't a sections view"):
        apply_ops(
            config,
            [{"op": "add_card", "view": 0, "section": 0, "card_config": tile}],
        )
    with pytest.raises(DashboardPatchError, match="card 5 doesn't exist - there are 1"):
        apply_ops(config, [{"op": "remove_card", "view": 0, "card": 5}])
    with pytest.raises(DashboardPatchError, match="an index or a name, got 'x'"):
        apply_ops(config, [{"op": "remove_card", "view": 0, "card": "x"}])


def test_set_at_a_pointer():
    config = _dashboard()
    result = apply_ops(
        config,
        [
            {
                "op": "set",
                "pointer": "/views/1/sections/0/cards/1/graph_span",
                "value": "48h",
            },
            {"op": "set", "pointer": "/views/0/icon", "value": "mdi:sun"},
            {
                "op": "set",
                "pointer": "/views/0/cards/0/entities/0",
                "value": "sensor.z",
            },
            {
                "op": "set",
                "pointer": "/views/0/cards/0/entities/-",
                "value": "sensor.y",
            },
        ],
    )
    views = result.config["views"]
    assert views[1]["sections"][0]["cards"][1]["graph_span"] == "48h"
    assert views[0]["icon"] == "mdi:sun"
    assert views[0]["cards"][0]["entities"] == ["sensor.z", "sensor.y"]
    assert result.changes[0] == {
        "op": 0,
        "kind": "set",
        "pointer": "/views/1/sections/0/cards/1/graph_span",
        "before": "24h",
        "after": "48h",
    }
    assert "before" not in result.changes[1]  # a new key
    assert result.changes[3]["pointer"] == "/views/0/cards/0/entities/1"

    for pointer, expected in (
        ("", "the whole config is write_dashboard's"),
        ("views/0", "a JSON pointer"),
        ("/views/9/title", "index 9 doesn't exist"),
        ("/views/01/title", "isn't a list index"),
        ("/nope/x", "/nope doesn't exist"),
        ("/title/x", "its parent isn't an object or list"),
        ("/views/0/cards/3", "index 3 doesn't exist"),
    ):
        with pytest.raises(DashboardPatchError, match=expected):
            apply_ops(config, [{"op": "set", "pointer": pointer, "value": 1}])


def test_positions_and_views():
    config = _dashboard()
    result = apply_ops(
        config,
        [
            {"op": "add_view", "view_config": {"title": "New"}, "position": 0},
            {"op": "add_view", "view_config": {"title": "Last"}},
        ],
    )
    assert [view["title"] for view in result.config["views"]] == [
        "New",
        "Overview",
        "Dev",
        "Dev",
        "Last",
    ]
    for position, expected in ((4, "outside 0-3"), (-1, "outside"), ("1", "or 'end'")):
        with pytest.raises(DashboardPatchError, match=expected):
            apply_ops(
                config,
                [{"op": "add_view", "view_config": {}, "position": position}],
            )


def test_refusals_name_the_op_and_change_nothing():
    config = _dashboard()
    before = copy.deepcopy(config)
    with pytest.raises(DashboardPatchError, match=r"op 1 \(remove_view\): no view"):
        apply_ops(
            config,
            [
                {"op": "remove_view", "view": 0},
                {"op": "remove_view", "view": "overview"},  # gone after op 0
            ],
        )
    assert config == before
    for op, expected in (
        ({"op": "frobnicate"}, "op is one of"),
        ({"op": "add_section", "view": 1}, "add_section needs section_config"),
        ({"op": "replace_card", "view": 0}, "needs card, card_config"),
        (
            {"op": "add_section", "view": 1, "section_config": []},
            "section_config is an object",
        ),
        ("x", "op 0 is an object"),
    ):
        with pytest.raises(DashboardPatchError, match=expected):
            apply_ops(config, [op])
    with pytest.raises(DashboardPatchError, match="a strategy dashboard"):
        apply_ops(
            {"strategy": {"type": "original-states"}},
            [{"op": "remove_view", "view": 0}],
        )


def test_the_hash_ignores_key_order_only():
    config = _dashboard()
    reordered = json.loads(json.dumps(config, sort_keys=True))
    assert config_hash(reordered) == config_hash(config)
    changed = copy.deepcopy(config)
    changed["views"][1]["sections"][0]["cards"][1]["data_generator"] += " "
    assert config_hash(changed) != config_hash(config)


# --- the tool, against real lovelace -------------------------------------------


@pytest.fixture
async def admin_user(hass: HomeAssistant):
    return MockUser(is_owner=True).add_to_hass(hass)


@pytest.fixture
async def lovelace(hass: HomeAssistant):
    assert await async_setup_component(hass, "websocket_api", {})
    assert await async_setup_component(hass, "lovelace", {})


def _input(name: str, **args) -> llm.ToolInput:
    return llm.ToolInput(tool_name=name, tool_args=args)


@pytest.mark.asyncio
async def test_patch_dashboard_tool(
    hass: HomeAssistant, setup_integration_with_entry, admin_user, lovelace
):
    from custom_components.ha_dev_tools.llm_api import (
        GetDashboardTool,
        PatchDashboardTool,
        WriteDashboardTool,
    )

    context = _llm_context(admin_user.id)
    original = _dashboard()
    # config_hash from get_dashboard, sent back, isn't saved into the config.
    saved = await WriteDashboardTool()._write(
        hass,
        _input("write_dashboard", config={**original, "config_hash": "x"}),
        context,
    )
    assert saved["saved"] is True
    read = await GetDashboardTool()._run(hass, _input("get_dashboard"), context)
    expected_hash = read.pop("config_hash")
    assert read == original and expected_hash == config_hash(original)

    tool = PatchDashboardTool()
    ops = [{"op": "add_section", "view": "dev", "section_config": NEW_SECTION}]
    args = {"ops": ops, "expected_hash": expected_hash}
    tool.parameters(args)
    assert len(json.dumps(args)) < 2048  # the issue's acceptance: < 2 KB
    with pytest.raises(vol.Invalid):
        tool.parameters({**args, "ops": [{"op": "frobnicate"}]})
    with pytest.raises(vol.Invalid):
        tool.parameters({"ops": ops})  # expected_hash is required

    preview = await tool._preview_context(hass, _input(tool.name, **args), context)
    change = preview["would_change"]
    assert change["changes"][0]["pointer"] == "/views/1/sections/2"
    assert change["config_hash_before"] == expected_hash
    stale = await tool._preview_context(
        hass, _input(tool.name, ops=ops, expected_hash="0" * 64), context
    )
    assert "changed since expected_hash" in stale["problems"]

    result = await tool._write(hass, _input(tool.name, **args), context)
    assert result["saved"] is True
    assert result["config_hash"] == change["config_hash_after"]
    after = await GetDashboardTool()._run(hass, _input("get_dashboard"), context)
    assert after.pop("config_hash") == result["config_hash"]
    assert after["views"][1]["sections"][2] == NEW_SECTION
    assert after["views"][1]["sections"][0]["cards"][1]["data_generator"] == JS
    assert [after["views"][i] for i in (0, 2)] == [original["views"][i] for i in (0, 2)]

    # The same ops again: planned against the old hash, refused.
    again = await tool._write(hass, _input(tool.name, **args), context)
    assert "changed since expected_hash" in again["error"]

    # Removing it again restores the previous config exactly.
    await tool._write(
        hass,
        _input(
            tool.name,
            ops=[{"op": "remove_section", "view": "dev", "section": "Export"}],
            expected_hash=result["config_hash"],
        ),
        context,
    )
    restored = await GetDashboardTool()._run(hass, _input("get_dashboard"), context)
    assert restored.pop("config_hash") == expected_hash
    assert restored == original

    bad = await tool._write(
        hass,
        _input(
            tool.name,
            ops=[{"op": "remove_view", "view": "nope"}],
            expected_hash=expected_hash,
        ),
        context,
    )
    assert "no view 'nope'" in bad["error"]
