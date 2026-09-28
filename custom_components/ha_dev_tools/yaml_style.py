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

find_misread_scalars finds values a file already has in that broken
form, for the audit tools (issue #96).
"""

from __future__ import annotations

import math
from io import StringIO
from typing import Any, cast

import yaml
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


def to_json_safe(value: Any) -> Any:
    """Recursively convert ruamel's round-trip types into plain JSON-safe values.

    CommentedMap/CommentedSeq already subclass dict/list and ScalarStrings
    subclass str, so most values pass through fine - but a raw HA YAML tag
    this loader doesn't understand (!secret, !include, !env_var, ...) loads
    as a TaggedScalar, which is not JSON serializable, and crashed every
    read tool returning config that contained one (issue #90). A tag is
    returned as its literal `!tag argument` text: never resolved, so a read
    never leaks secrets.yaml contents. Scalars keep their own types, so
    find_misread_scalars works on the result too (minus line numbers).
    """
    if isinstance(value, dict):
        return {k: to_json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_json_safe(v) for v in value]
    tag = getattr(value, "tag", None)
    if tag is not None and hasattr(value, "value"):
        return f"{tag.value} {value.value}"
    return value


def _pyyaml_reading(value: str) -> Any:
    """What PyYAML (so Home Assistant) actually reads this plain scalar as,
    in a JSON-safe form for tool responses."""
    try:
        read = yaml.safe_load(value)
    except yaml.YAMLError:
        # e.g. a bare `=` (YAML 1.1's "value" tag): PyYAML can't construct
        # it at all, so HA fails to load the whole file.
        return "<load error>"
    if read is None or isinstance(read, (bool, int, str)):
        return read
    if isinstance(read, float) and math.isfinite(read):
        return read
    return str(read)  # dates, inf/nan


def find_misread_scalars(node: Any, path: str = "") -> list[dict[str, Any]]:
    """Find every unquoted string value in a loaded document that Home
    Assistant reads back as a different type (issue #96).

    Loaded with ruamel's round-trip loader, an unquoted `before: 17:00:00`
    or `state: off` comes back as a plain `str` (YAML 1.2), so every read
    through this integration looks fine - but HA's PyYAML loader reads the
    same text as 61200 or False. A file already written that way (e.g. by
    a version before quote_ambiguous_scalars covered it) stays broken
    until the value is written again, and nothing else points to it.

    Quoted and block scalars load as ScalarString subclasses, never plain
    `str`, so they're never reported. Each finding has the value's path
    (e.g. `conditions[1].before`), 1-based line (None where ruamel kept no
    position), the text as written, and what HA reads it as.
    """
    findings: list[dict[str, Any]] = []
    if isinstance(node, dict):
        items: Any = node.items()
    elif isinstance(node, list):
        items = enumerate(node)
    else:
        return findings
    lc = getattr(node, "lc", None)
    for key, value in items:
        child = (
            f"{path}[{key}]"
            if isinstance(node, list)
            else (f"{path}.{key}" if path else str(key))
        )
        if type(value) is str and pyyaml_misreads(value):
            line = None
            if lc is not None:
                position = lc.data.get(key)
                if position is not None:
                    # Mappings record the value's line as position[2],
                    # sequences only have the item's own line.
                    line = (position[2] if len(position) > 2 else position[0]) + 1
            findings.append(
                {
                    "path": child,
                    "line": line,
                    "written": value,
                    "ha_reads_as": _pyyaml_reading(value),
                }
            )
        else:
            findings.extend(find_misread_scalars(value, child))
    return findings


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


# --- Surgical splicing (issue #53) ------------------------------------------
#
# Every write used to re-dump the whole document, and ruamel's round-trip
# dump isn't byte-lossless: it normalizes list indentation, collapses extra
# spaces (`mode:   single`) and re-joins plain scalars an editor wrapped
# across lines - in items the write never touched. That made mirrored
# proposed-branch diffs unreviewable (a one-automation edit touched 269
# lines). These helpers instead replace only the edited item's own lines in
# the original text; every other byte stays as it was. The caller still
# computes the full re-dump and passes both to `spliced_or_full`, which
# keeps the splice only if it parses to exactly the same data - anything the
# span logic can't handle cleanly falls back to the full dump, never to
# different data.


def _is_skippable(line: str) -> bool:
    """Blank or comment-only - never ends an item, never starts one."""
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _span_end(lines: list[str], start: int, indent: int) -> int:
    """Line index just past an item that starts at `start` with `indent`:
    the last content line before the next line at or left of `indent`.
    Trailing blank/comment lines are left outside the span - ruamel
    attaches them to the item, so they'd otherwise be re-rendered too."""
    end = start + 1
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if _is_skippable(line):
            continue
        if _indent(line) <= indent:
            break
        end = index + 1
    return end


def item_span(
    lines: list[str], container: Any, key: Any
) -> tuple[int, int, int] | None:
    """(start, end, indent) of one existing item's lines in the original
    text, from ruamel's position data - or None if it can't be located
    cleanly (then the caller's full dump is used)."""
    if not isinstance(container, (CommentedSeq, CommentedMap)):
        return None
    lc = container.lc
    try:
        if isinstance(container, CommentedSeq):
            line = lc.item(key)[0]
            if not lines[line].lstrip().startswith("-"):
                return None
        else:
            line = lc.key(key)[0]
    except (KeyError, IndexError, TypeError):
        return None
    indent = _indent(lines[line])
    if isinstance(container, CommentedMap) and indent != lc.key(key)[1]:
        return None
    return line, _span_end(lines, line, indent), indent


def append_point(lines: list[str], container: Any) -> tuple[int, int] | None:
    """(insert-at line, indent) just after a container's last item."""
    if not isinstance(container, (CommentedSeq, CommentedMap)) or not container:
        return None
    last = (
        len(container) - 1
        if isinstance(container, CommentedSeq)
        else list(container)[-1]
    )
    span = item_span(lines, container, last)
    if span is None:
        return None
    return span[1], span[2]


_WRAP_KEY = "__ha_dev_tools_splice__"


def _key_column(line: str) -> int:
    """Column of a line's first key, past any `- ` item dashes before it."""
    column = _indent(line)
    rest = line[column:]
    while rest.startswith("- "):
        stripped = rest[2:].lstrip(" ")
        column += len(rest) - len(stripped)
        rest = stripped
    return column


def dash_offset(lines: list[str]) -> int:
    """How far these lines indent a block sequence's dashes past the key
    that owns it: 0 for `key:` / `- a` (ruamel's and HA's own style), 2
    for `key:` / `  - a` (common in hand-written files). Taken from the
    first nested sequence found; 0 if there's none or it's odd."""
    for index, line in enumerate(lines):
        if _is_skippable(line) or not line.split(" #", 1)[0].rstrip().endswith(":"):
            continue
        following = next(
            (nxt for nxt in lines[index + 1 :] if not _is_skippable(nxt)), None
        )
        if following is not None and following.lstrip(" ").startswith("- "):
            offset = _indent(following) - _key_column(line)
            return offset if offset > 0 and offset % 2 == 0 else 0
    return 0


def _has_block_sequence(node: Any) -> bool:
    """Whether dumping `node` writes a nested block (`- a`) sequence."""
    if isinstance(node, list):
        return bool(node) and not (
            isinstance(node, CommentedSeq) and node.fa.flow_style()
        )
    if isinstance(node, dict):
        return any(_has_block_sequence(value) for value in node.values())
    return False


def render_item(
    yaml: Any,
    container: Any,
    key: Any,
    node: Any,
    indent: int,
    offset: int = 0,
) -> str:
    """One item rendered on its own at column `indent`, without the
    trailing blank/comment lines ruamel attaches to its last value.

    Rendered at its real depth - inside placeholder mappings deep enough to
    put it at column `indent`, whose own lines are then dropped - rather than dumped at column 0 and
    shifted right: ruamel re-aligns an inline comment to the column it was
    read at, so a shifted dump would move every `# comment` right too.
    Falls back to shifting for an odd indent, which the placeholders can't
    reach with ruamel's 2-space indent.

    `offset` is the file's sequence dash offset (see dash_offset), so an
    item written as `key:` / `  - a` keeps its nested lists that way
    instead of being reflowed to ruamel's `key:` / `- a` (issue #115).
    """
    if not _has_block_sequence(node):
        offset = 0  # no nested list to place - keep ruamel's own layout
    as_mapping = (
        offset
        and isinstance(container, CommentedSeq)
        and isinstance(node, CommentedMap)
        and bool(node)
        and indent % 2 == 0
    )
    item: Any
    if as_mapping:
        # A list item's mapping, rendered as a mapping at its keys' column
        # (indent + 2) with the dash put back afterwards: no shifting, so
        # every `# comment` stays in its column.
        item, depth, shift = node, indent // 2 + 1, 0
    elif isinstance(container, CommentedSeq):
        item = CommentedSeq([node])
        # ruamel starts a block sequence `offset` columns right of its
        # parent key, so a dash at column N needs the innermost placeholder
        # key at N - offset (a root sequence has none, but still gets it).
        depth, shift = divmod(indent - offset, 2)
        depth += 1
        if not indent or depth < 1 or shift:
            depth, shift = 0, indent - offset
    else:
        item = CommentedMap({key: node})
        depth, shift = divmod(indent, 2)
        if shift:
            depth, shift = 0, indent
    wrapper = item
    for _ in range(depth):
        wrapper = CommentedMap({_WRAP_KEY: wrapper})
    saved = (yaml.map_indent, yaml.sequence_indent, yaml.sequence_dash_offset)
    if offset:
        yaml.indent(mapping=2, sequence=offset + 2, offset=offset)
    buffer = StringIO()
    try:
        yaml.dump(wrapper, buffer)
    finally:
        yaml.map_indent, yaml.sequence_indent, yaml.sequence_dash_offset = saved
    rendered = buffer.getvalue().splitlines(keepends=True)[depth:]
    if as_mapping:
        first = next(i for i, line in enumerate(rendered) if not _is_skippable(line))
        line = rendered[first]
        rendered[first] = line[:indent] + "- " + line[indent + 2 :]
    elif shift > 0:
        pad = " " * shift
        rendered = [pad + line if line.strip() else line for line in rendered]
    elif shift < 0:
        rendered = [line[min(-shift, _indent(line)) :] for line in rendered]
    while rendered and _is_skippable(rendered[-1]):
        rendered.pop()
    return "".join(rendered)


def splice(original: str, start: int, end: int, replacement: str) -> str:
    """Replace lines [start, end) of `original` with `replacement`.
    Deleting (empty replacement) between two blank lines drops one."""
    lines = original.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    if not replacement and 0 < start and end < len(lines):
        if not lines[start - 1].strip() and not lines[end].strip():
            end += 1
    return "".join(lines[:start]) + replacement + "".join(lines[end:])


def spliced_or_full(spliced: str | None, full: str, load: Any) -> str:
    """The surgical splice if it's valid YAML with exactly the same data as
    the full re-dump, otherwise the full re-dump."""
    if spliced is None:
        return full
    try:
        same = to_json_safe(load(spliced)) == to_json_safe(load(full))
    except Exception:  # noqa: BLE001 - any parse failure means "use full"
        return full
    return spliced if same else full


def surgical_edit(
    original: str | None,
    container: Any,
    key: Any,
    op: str,
    yaml: Any,
    apply: Any,
) -> str | None:
    """Run `apply()` (the in-memory edit, always - the caller's full dump
    needs it) and return `original` with only that item's lines changed,
    or None when a splice isn't possible. `op` is "replace" or "delete"
    (of `container[key]`) or "append" (`key` is the new map key; ignored
    for a sequence). Pass the result to spliced_or_full."""
    lines = original.splitlines(keepends=True) if original else []
    if op == "append":
        point = append_point(lines, container) if lines else None
        apply()
        if point is None:
            return None
        new_key = len(container) - 1 if isinstance(container, CommentedSeq) else key
        rendered = render_item(
            yaml, container, new_key, container[new_key], point[1], dash_offset(lines)
        )
        return splice(cast(str, original), point[0], point[0], rendered)
    span = item_span(lines, container, key) if lines else None
    apply()
    if span is None:
        return None
    start, end, indent = span
    if op == "delete":
        return splice(cast(str, original), start, end, "")
    offset = dash_offset(lines[start:end]) or dash_offset(lines)
    rendered = render_item(yaml, container, key, container[key], indent, offset)
    return splice(cast(str, original), start, end, rendered)


def remove_items(original: str | None, container: Any, keys: list[Any]) -> str | None:
    """Delete several items of one container (always, in memory) and return
    `original` with just their lines removed, or None when any of them
    can't be located cleanly. Pass the result to spliced_or_full."""
    lines = original.splitlines(keepends=True) if original else []
    spans = [item_span(lines, container, key) if lines else None for key in keys]
    for key in sorted(keys, reverse=True):
        del container[key]
    if not keys or any(span is None for span in spans):
        return None
    text = cast(str, original)
    for start, end, _indent in sorted(cast(list, spans), reverse=True):
        text = splice(text, start, end, "")
    return text
