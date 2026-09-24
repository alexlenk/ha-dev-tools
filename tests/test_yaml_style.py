"""Unit tests for yaml_style's merge/quoting helpers.

The manager tests (test_automation_manager.py etc.) cover these end to
end through real file writes; these cover the individual merge cases
directly.
"""

from io import StringIO

from ruamel.yaml import YAML

from custom_components.ha_dev_tools.yaml_style import (
    find_misread_scalars,
    merge_preserving_style,
    pyyaml_misreads,
    quote_ambiguous_scalars,
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
