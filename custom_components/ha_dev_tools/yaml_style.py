"""Helpers for splicing caller config into round-trip-loaded YAML safely.

Shared by automation_manager.py, script_manager.py and
template_yaml_manager.py, which all load a hand-maintained YAML file with
ruamel.yaml's round-trip loader, splice one item's new config into it
from a tool call's plain dict/list/str, and dump the whole document back.
Two things can go wrong there, one helper each:

- quote_ambiguous_scalars: a new string can be written in a form Home
  Assistant reads back as something else.
- merge_preserving_style: swapping an existing item for the caller's
  plain dict loses the original formatting of every field in it, changed
  or not (issue #91).
"""

from __future__ import annotations

from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString
from yaml.nodes import ScalarNode
from yaml.resolver import Resolver as PyYamlResolver

# Home Assistant reads these files back with PyYAML, whose default resolver
# follows YAML 1.1; ruamel.yaml's own resolver follows YAML 1.2. Plain
# scalars the two disagree on are the problem: when ruamel dumps a
# brand-new plain `str` it didn't load with existing quote styling, it
# only quotes it if *YAML 1.2* would misread it. Two confirmed live cases:
# - the "Norway problem": unquoted `state: off` reloads via HA's loader as
#   `False`, fails schema validation (expects a str), and silently
#   disables the whole automation.
# - YAML 1.1 base-60 ints: unquoted `before: 17:00:00` reloads as the int
#   61200, which HA's time condition rejects ("Invalid time specified:
#   61200"), again disabling the automation (issue #91).
# So rather than a hand-maintained list of such scalars, ask PyYAML's own
# resolver directly and quote anything it wouldn't read back as a str.
_PYYAML_RESOLVER = PyYamlResolver()
_PYYAML_STR_TAG = "tag:yaml.org,2002:str"


def pyyaml_misreads(value: str) -> bool:
    """True if PyYAML would read this plain (unquoted) scalar as a non-str."""
    return _PYYAML_RESOLVER.resolve(ScalarNode, value, (True, False)) != _PYYAML_STR_TAG


def quote_ambiguous_scalars(value: Any) -> Any:
    """Recursively force-quote plain strings PyYAML would misread as a non-str.

    Only ever applied to brand-new config a caller passed in (plain dict/
    list/str from a tool call), never to values already loaded from the
    file - those already round-trip with whatever quote style they were
    written with (merge_preserving_style handles the one exception).
    """
    if isinstance(value, dict):
        return {k: quote_ambiguous_scalars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [quote_ambiguous_scalars(v) for v in value]
    if isinstance(value, str) and pyyaml_misreads(value):
        return DoubleQuotedScalarString(value)
    return value


def _same_scalar(old: Any, new: Any) -> bool:
    """True if old and new are the same scalar value, treating bool, number,
    str and None as distinct kinds (so True != 1 and 1 != "1" here, unlike
    plain ==). ruamel's styled subclasses (SingleQuotedScalarString,
    HexInt, ...) compare by value like their plain base types."""

    def kind(value: Any) -> str | None:
        if value is None:
            return "none"
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, (int, float)):
            return "number"
        if isinstance(value, str):
            return "str"
        return None

    old_kind = kind(old)
    return old_kind is not None and old_kind == kind(new) and old == new


def merge_preserving_style(old: Any, new: Any) -> Any:
    """Return `new`, reusing `old`'s loaded ruamel nodes wherever they already
    hold the same value, so unchanged fields keep their original quote style,
    comments and number formatting (issue #91).

    Mappings are patched in place: keys `new` drops are deleted, existing
    keys keep their original position, and new keys go at the end. Lists
    are merged by position. A scalar is replaced only if its value changed,
    or if it's an unquoted plain string PyYAML would misread (keeping it
    would preserve a bug the caller's write should fix). Anchored nodes and
    mappings using `<<:` merge keys are never patched in place - editing
    one would silently change every other node sharing it - so they're
    replaced wholesale, like before this fix.
    """
    if getattr(getattr(old, "anchor", None), "value", None):
        return new
    if isinstance(old, CommentedMap) and isinstance(new, dict):
        if old.merge:
            return new
        for key in [k for k in old if k not in new]:
            del old[key]
        for key, value in new.items():
            old[key] = merge_preserving_style(old[key], value) if key in old else value
        return old
    if isinstance(old, CommentedSeq) and isinstance(new, list):
        while len(old) > len(new):
            del old[-1]
        for i, value in enumerate(new):
            if i < len(old):
                old[i] = merge_preserving_style(old[i], value)
            else:
                old.append(value)
        return old
    if _same_scalar(old, new):
        if type(old) is str and pyyaml_misreads(old):
            return new
        return old
    return new
