"""Unit tests for yaml_style's merge/quoting helpers.

The manager tests (test_automation_manager.py etc.) cover these end to
end through real file writes; these cover the individual merge cases
directly.
"""

from io import StringIO

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from custom_components.ha_dev_tools.yaml_style import (
    append_point,
    dash_offset,
    find_misread_scalars,
    item_span,
    merge_preserving_style,
    pyyaml_misreads,
    quote_ambiguous_scalars,
    render_item,
    splice,
    spliced_or_full,
    surgical_edit,
)


def _load(content: str):
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    return yaml.load(content)


def _dump(document) -> str:
    yaml = YAML(typ="rt")
    buffer = StringIO()
    yaml.dump(document, buffer)
    return buffer.getvalue()


def test_pyyaml_misreads():
    for value in ("off", "yes", "~", "", "17:00:00", "1:30", "0x1F", "2024-01-01"):
        assert pyyaml_misreads(value), value
    for value in ("07:00:00", "abc", "1e3", "switch.heater"):
        assert not pyyaml_misreads(value), value


def test_quote_ambiguous_scalars_only_quotes_misread_strings():
    quoted = quote_ambiguous_scalars({"a": ["off", "on_time"], "b": 1})
    assert _dump(quoted) == 'a:\n- "off"\n- on_time\nb: 1\n'


def test_merge_keeps_unchanged_none_and_bool_nodes():
    old = _load("a:\nb: yes_i_am_a_string\nc: true\n")
    merged = merge_preserving_style(
        old, {"a": None, "b": "yes_i_am_a_string", "c": True}
    )
    assert merged is old
    assert _dump(merged) == "a:\nb: yes_i_am_a_string\nc: true\n"


def test_merge_does_not_treat_bool_and_int_as_same():
    old = _load("a: 1\n")
    assert _dump(merge_preserving_style(old, {"a": True})) == "a: true\n"


def test_merge_replaces_scalar_with_container_and_back():
    old = _load("a: x\nb:\n  c: 1\n")
    merged = merge_preserving_style(old, {"a": {"c": 1}, "b": "x"})
    assert _dump(merged) == "a:\n  c: 1\nb: x\n"


def test_merge_appends_extra_list_items():
    old = _load("a:\n- 'one'\n")
    merged = merge_preserving_style(old, {"a": ["one", "two"]})
    assert _dump(merged) == "a:\n- 'one'\n- two\n"


def test_merge_replaces_mapping_using_merge_key_wholesale():
    document = _load("base: &base\n  x: 1\nitem:\n  <<: *base\n  y: 2\n")
    merged = merge_preserving_style(document["item"], {"x": 1, "y": 3})
    assert merged is not document["item"]
    assert merged == {"x": 1, "y": 3}
    # The shared anchor itself is untouched.
    assert document["base"] == {"x": 1}


def test_find_misread_scalars_reports_path_line_and_reading():
    document = _load(
        "mode: off\n"
        "items:\n"
        "- 1:30\n"
        "- '1:30'\n"
        "- nested:\n"
        "    day: 2024-01-01\n"
        "    when: |\n"
        "      17:00:00\n"
        "    ok: 07:00:00\n"
    )
    assert find_misread_scalars(document) == [
        {"path": "mode", "line": 1, "written": "off", "ha_reads_as": False},
        {"path": "items[0]", "line": 3, "written": "1:30", "ha_reads_as": 90},
    ]


def test_find_misread_scalars_without_line_info_and_odd_readings():
    # Plain dicts/lists (no ruamel position data) still get paths; values
    # PyYAML reads as floats or non-JSON types are reported JSON-safe.
    findings = find_misread_scalars({"a": ["1.5", ".inf", "~", "="]})
    assert findings == [
        {"path": "a[0]", "line": None, "written": "1.5", "ha_reads_as": 1.5},
        {"path": "a[1]", "line": None, "written": ".inf", "ha_reads_as": "inf"},
        {"path": "a[2]", "line": None, "written": "~", "ha_reads_as": None},
        {"path": "a[3]", "line": None, "written": "=", "ha_reads_as": "<load error>"},
    ]


# --- Surgical splice helpers: every "can't splice cleanly" path (issue #53) --


def _yaml():
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    return yaml


def test_item_span_declines_what_it_cannot_locate_cleanly():
    lines = ["a:\n", "  b: 1\n"]
    doc = _load("a:\n  b: 1\n")
    assert item_span(lines, {"plain": "dict"}, "plain") is None  # not ruamel-loaded
    assert item_span(lines, "not a container", 0) is None
    assert item_span(lines, doc, "missing") is None
    # Position data that doesn't match the text (edited since it was read).
    assert item_span(["x: 1\n"], _load("[1, 2]\n"), 1) is None  # no dash there
    assert item_span(["    a:\n"], doc, "a") is None  # key column mismatch


def test_append_point_needs_an_existing_item():
    assert append_point([], CommentedSeq()) is None
    assert append_point([], {"plain": "dict"}) is None
    flow = _load("[a, b]\n")  # flow style: no block dash to anchor on
    assert append_point(["[a, b]\n"], flow) is None


def test_surgical_edit_declines_without_original_or_span_but_still_applies():
    seq = CommentedSeq([1])
    applied = []
    assert (
        surgical_edit(None, seq, 0, "replace", _yaml(), lambda: applied.append(1))
        is None
    )
    assert (
        surgical_edit("", seq, None, "append", _yaml(), lambda: applied.append(2))
        is None
    )
    assert applied == [1, 2]


def test_render_item_odd_indent_falls_back_to_shifting():
    node = CommentedMap({"a": 1})
    assert render_item(_yaml(), CommentedSeq([node]), 0, node, 3) == "   - a: 1\n"


def test_splice_delete_between_blank_lines_drops_one_and_fixes_missing_newline():
    assert splice("a\n\nb\n\nc", 2, 3, "") == "a\n\nc\n"
    assert splice("a\nb", 1, 2, "B\n") == "a\nB\n"


def test_spliced_or_full_keeps_the_full_dump_unless_data_is_identical():
    load = _yaml().load
    full = "a: 1\n"
    assert spliced_or_full(None, full, load) == full
    assert spliced_or_full("a: 2\n", full, load) == full  # different data
    assert spliced_or_full("a: [\n", full, load) == full  # not even YAML
    assert spliced_or_full("a:   1\n", full, load) == "a:   1\n"  # same data


def test_render_item_drops_trailing_blank_lines_ruamel_attaches():
    """The blank line after an item belongs to the item in ruamel's model -
    re-rendering it would duplicate the blank line the splice keeps."""
    seq = _load("- a: 1\n\n- b: 2\n")
    assert render_item(_yaml(), seq, 0, seq[0], 0) == "- a: 1\n"


def test_dash_offset_reads_the_nested_list_style():
    assert dash_offset(["a:\n", "- 1\n"]) == 0
    assert dash_offset(["a:\n", "  - 1\n"]) == 2
    assert dash_offset(["a:  # note\n", "\n", "# c\n", "    - 1\n"]) == 4
    # The key after an item's own dash is what owns the nested list.
    assert dash_offset(["- id: x\n", "  actions:\n", "    - a: 1\n"]) == 2
    assert dash_offset(["  - actions:\n", "      - a: 1\n"]) == 2
    assert dash_offset(["a:\n", "   - 1\n"]) == 0  # odd: not reproducible
    assert dash_offset(["a:\n", "  b: 1\n"]) == 0  # no nested list
    assert dash_offset(["a: 1\n", "b:\n"]) == 0  # key with nothing after it


def test_render_item_keeps_the_files_list_offset():
    """Issue #115: nested lists keep `key:` / `  - a`, at every placement."""
    seq = _load("- id: a   # keep\n  actions:\n    - x: 1   # inner\n")
    root = render_item(_yaml(), seq, 0, seq[0], 0, 2)
    assert root == "- id: a   # keep\n  actions:\n    - x: 1   # inner\n"
    nested_seq = _load("k:\n  - id: a   # keep\n    actions:\n      - x: 1\n")["k"]
    nested = render_item(_yaml(), nested_seq, 0, nested_seq[0], 2, 2)
    assert nested == "  - id: a   # keep\n    actions:\n      - x: 1\n"
    seq = _load("- id: a\n  actions:\n    - x: 1\n")
    odd = render_item(_yaml(), seq, 0, seq[0], 3, 2)
    assert odd == "   - id: a\n     actions:\n       - x: 1\n"
    mapping = _load("s:\n  sequence:\n    - x: 1\n")
    assert render_item(_yaml(), mapping, "s", mapping["s"], 2, 2) == (
        "  s:\n    sequence:\n      - x: 1\n"
    )
    assert render_item(_yaml(), mapping, "s", mapping["s"], 3, 2) == (
        "   s:\n     sequence:\n       - x: 1\n"
    )
    # The caller's YAML instance is left exactly as it was.
    yaml = _yaml()
    render_item(yaml, seq, 0, seq[0], 2, 2)
    assert (yaml.map_indent, yaml.sequence_indent, yaml.sequence_dash_offset) == (
        None,
        None,
        0,
    )


def test_render_item_offset_for_a_list_item_that_is_not_a_mapping():
    """A list of lists has no keys to anchor on - placed by shifting, and
    still the same data at the right column (spliced_or_full checks it)."""
    root = _load("- - a\n  - - b\n")
    rendered = render_item(_yaml(), root, 0, root[0], 0, 2)
    assert rendered.startswith("- ") and _load(rendered) == [["a", ["b"]]]
    nested = _load("k:\n  - - a\n    - - b\n")["k"]
    for indent, offset in ((2, 2), (1, 4)):
        rendered = render_item(_yaml(), nested, 0, nested[0], indent, offset)
        assert rendered.startswith(" " * indent + "- ")
        assert _load("k:\n" + rendered)["k"] == [["a", ["b"]]]


def test_render_item_ignores_the_offset_without_a_nested_block_list():
    """Nothing to place, so ruamel's own layout (and comment columns) stay."""
    seq = _load("- id: a   # keep\n  flow: [1, 2]\n  empty: []\n")
    assert render_item(_yaml(), seq, 0, seq[0], 0, 2) == (
        "- id: a   # keep\n  flow: [1, 2]\n  empty: []\n"
    )
