"""Partial dashboard updates: ops applied to the current config (issue #158).

write_dashboard only saves a complete config, so one new section meant
re-sending the whole dashboard - tens of KB, embedded JavaScript included,
where one escaping slip breaks an unrelated card. patch_dashboard instead
takes a list of ops and applies them, in order, to the config as it is
now; everything they don't touch is passed through as read.

Ops (`view`, `section` are selectors; `position` an index to insert
before, the end by default; indices count from 0, after earlier ops):

- add_view {view_config, position?} / remove_view {view}
- add_section {view, section_config, position?} /
  replace_section {view, section, section_config} /
  remove_section {view, section}
- add_card {view, section?, card_config, position?} /
  replace_card {view, section?, card, card_config} /
  remove_card {view, section?, card} - without `section`, the view's own
  `cards` (masonry, panel, sidebar views)
- set {pointer, value}: a JSON pointer (RFC 6901) into the config - an
  object key is added or replaced, a list index replaced, "-" appends.

A view is its index, or a string: its `path`, else its `title`. A section
is its index, or a string: its `title`, or the text of a heading card in
it. A card is its index. A string matching more than one is refused.

Ops are planned against a config hash (config_hash, as get_dashboard
returns it); a config changed since is refused rather than patched.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

OPS = (
    "add_view",
    "remove_view",
    "add_section",
    "replace_section",
    "remove_section",
    "add_card",
    "replace_card",
    "remove_card",
    "set",
)
HASH_KEY = "config_hash"


class DashboardPatchError(ValueError):
    """An op that can't be applied - nothing is saved."""


def config_hash(config: Any) -> str:
    """SHA-256 of the config's canonical JSON - key order and whitespace
    don't count, any value does."""
    canonical = json.dumps(
        config, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(slots=True)
class PatchResult:
    config: dict[str, Any]
    changes: list[dict[str, Any]]


def _pointer(*parts: str | int) -> str:
    return "".join(
        "/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts
    )


def _list(node: dict[str, Any], key: str, what: str) -> list[Any]:
    value = node.get(key)
    if not isinstance(value, list):
        raise DashboardPatchError(f"{what} has no '{key}' list")
    return value


def _index(items: list[Any], index: Any, what: str) -> int:
    if isinstance(index, bool) or not isinstance(index, int):
        raise DashboardPatchError(f"{what} is an index or a name, got {index!r}")
    if not 0 <= index < len(items):
        raise DashboardPatchError(
            f"{what} {index} doesn't exist - there are {len(items)} (0-{len(items) - 1})"
            if items
            else f"{what} {index} doesn't exist - there are none"
        )
    return index


def _match(items: list[Any], name: str, names, what: str) -> int:
    """The one item `names(item)` contains `name` for."""
    found = [index for index, item in enumerate(items) if name in names(item)]
    if len(found) > 1:
        raise DashboardPatchError(
            f"{what} '{name}' is ambiguous - {len(found)} match (indices "
            f"{', '.join(map(str, found))}); pass the index"
        )
    if not found:
        raise DashboardPatchError(f"no {what} '{name}'")
    return found[0]


def _view_index(views: list[Any], selector: Any) -> int:
    if not isinstance(selector, str):
        return _index(views, selector, "view")

    def field(key: str):
        return lambda view: [view.get(key)] if isinstance(view, dict) else []

    by_path = field("path")
    if any(selector in by_path(view) for view in views):
        return _match(views, selector, by_path, "view")
    return _match(views, selector, field("title"), "view")


def _section_names(section: Any) -> list[str]:
    if not isinstance(section, dict):
        return []
    names = [section["title"]] if isinstance(section.get("title"), str) else []
    for card in section.get("cards") or []:
        if isinstance(card, dict) and card.get("type") == "heading":
            if isinstance(card.get("heading"), str):
                names.append(card["heading"])
    return names


def _position(items: list[Any], position: Any) -> int:
    if position is None or position == "end":
        return len(items)
    if isinstance(position, bool) or not isinstance(position, int):
        raise DashboardPatchError(f"position is an index or 'end', got {position!r}")
    if not 0 <= position <= len(items):
        raise DashboardPatchError(
            f"position {position} is outside 0-{len(items)} (the end)"
        )
    return position


def _required(op: dict[str, Any], *keys: str) -> None:
    missing = [key for key in keys if key not in op]
    if missing:
        raise DashboardPatchError(f"{op['op']} needs {', '.join(missing)}")


def _config(op: dict[str, Any], key: str) -> dict[str, Any]:
    value = op[key]
    if not isinstance(value, dict):
        raise DashboardPatchError(f"{key} is an object, got {type(value).__name__}")
    return copy.deepcopy(value)


class _Patcher:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.changes: list[dict[str, Any]] = []

    def _views(self) -> list[Any]:
        return _list(self.config, "views", "the dashboard (a strategy dashboard?)")

    def _view(self, selector: Any) -> tuple[int, dict[str, Any]]:
        views = self._views()
        index = _view_index(views, selector)
        view = views[index]
        if not isinstance(view, dict):
            raise DashboardPatchError(f"view {index} isn't an object")
        return index, view

    def _sections(self, view_index: int, view: dict[str, Any]) -> list[Any]:
        if "sections" not in view and view.get("type") != "sections":
            raise DashboardPatchError(
                f"view {view_index} isn't a sections view - its cards are "
                "the view's own (leave out section)"
            )
        return view.setdefault("sections", [])

    def _section(
        self, view_index: int, view: dict[str, Any], selector: Any
    ) -> tuple[int, dict[str, Any]]:
        sections = self._sections(view_index, view)
        index = (
            _match(sections, selector, _section_names, "section")
            if isinstance(selector, str)
            else _index(sections, selector, "section")
        )
        section = sections[index]
        if not isinstance(section, dict):
            raise DashboardPatchError(f"section {index} isn't an object")
        return index, section

    def _cards(self, op: dict[str, Any]) -> tuple[list[Any], tuple[str | int, ...]]:
        view_index, view = self._view(op["view"])
        if "section" in op:
            section_index, section = self._section(view_index, view, op["section"])
            return section.setdefault("cards", []), (
                "views",
                view_index,
                "sections",
                section_index,
                "cards",
            )
        if "sections" in view or view.get("type") == "sections":
            raise DashboardPatchError(
                f"view {view_index} is a sections view - pass section"
            )
        return view.setdefault("cards", []), ("views", view_index, "cards")

    def _record(self, number: int, op: str, at: str, before: Any, after: Any) -> None:
        change: dict[str, Any] = {"op": number, "kind": op, "pointer": at}
        if before is not _ABSENT:
            change["before"] = before
        if after is not _ABSENT:
            change["after"] = after
        self.changes.append(change)

    def apply(self, number: int, op: dict[str, Any]) -> None:
        kind = op.get("op")
        if kind not in OPS:
            raise DashboardPatchError(f"op is one of {', '.join(OPS)}, got {kind!r}")
        if kind == "add_view":
            _required(op, "view_config")
            views = self._views()
            index = _position(views, op.get("position"))
            views.insert(index, _config(op, "view_config"))
            self._record(number, kind, _pointer("views", index), _ABSENT, views[index])
        elif kind == "remove_view":
            _required(op, "view")
            index, view = self._view(op["view"])
            del self._views()[index]
            self._record(number, kind, _pointer("views", index), view, _ABSENT)
        elif kind in ("add_section", "replace_section", "remove_section"):
            _required(
                op,
                "view",
                *(("section",) if kind != "add_section" else ()),
                *(("section_config",) if kind != "remove_section" else ()),
            )
            view_index, view = self._view(op["view"])
            sections = self._sections(view_index, view)
            if kind == "add_section":
                index = _position(sections, op.get("position"))
                sections.insert(index, _config(op, "section_config"))
                before, after = _ABSENT, sections[index]
            else:
                index, before = self._section(view_index, view, op["section"])
                if kind == "replace_section":
                    sections[index] = after = _config(op, "section_config")
                else:
                    del sections[index]
                    after = _ABSENT
            self._record(
                number,
                kind,
                _pointer("views", view_index, "sections", index),
                before,
                after,
            )
        elif kind in ("add_card", "replace_card", "remove_card"):
            _required(
                op,
                "view",
                *(("card",) if kind != "add_card" else ()),
                *(("card_config",) if kind != "remove_card" else ()),
            )
            cards, at = self._cards(op)
            if kind == "add_card":
                index = _position(cards, op.get("position"))
                cards.insert(index, _config(op, "card_config"))
                before, after = _ABSENT, cards[index]
            else:
                index = _index(cards, op["card"], "card")
                before = cards[index]
                if kind == "replace_card":
                    cards[index] = after = _config(op, "card_config")
                else:
                    del cards[index]
                    after = _ABSENT
            self._record(number, kind, _pointer(*at, index), before, after)
        else:
            _required(op, "pointer", "value")
            self._set(number, op["pointer"], copy.deepcopy(op["value"]))

    def _set(self, number: int, pointer: Any, value: Any) -> None:
        if not isinstance(pointer, str) or not pointer.startswith("/"):
            raise DashboardPatchError(
                "pointer is a JSON pointer into the config, e.g. "
                "'/views/0/sections/1/cards/2/days_to_show' - the whole config "
                "is write_dashboard's"
            )
        tokens = [
            token.replace("~1", "/").replace("~0", "~")
            for token in pointer[1:].split("/")
        ]
        node: Any = self.config
        for depth, token in enumerate(tokens[:-1]):
            node = self._step(node, token, _pointer(*tokens[: depth + 1]))
        last = tokens[-1]
        if isinstance(node, dict):
            before = node.get(last, _ABSENT)
            node[last] = value
        elif isinstance(node, list):
            if last == "-":
                node.append(value)
                before = _ABSENT
                pointer = _pointer(*tokens[:-1], len(node) - 1)
            else:
                index = self._list_index(node, last, pointer)
                before = node[index]
                node[index] = value
        else:
            raise DashboardPatchError(f"{pointer}: its parent isn't an object or list")
        self._record(number, "set", pointer, before, value)

    @staticmethod
    def _list_index(node: list[Any], token: str, at: str) -> int:
        if not token.isdigit() or (token != "0" and token.startswith("0")):
            raise DashboardPatchError(f"{at}: '{token}' isn't a list index")
        if int(token) >= len(node):
            raise DashboardPatchError(
                f"{at}: index {token} doesn't exist (the list has {len(node)})"
            )
        return int(token)

    def _step(self, node: Any, token: str, at: str) -> Any:
        if isinstance(node, dict):
            if token not in node:
                raise DashboardPatchError(f"{at} doesn't exist")
            return node[token]
        if isinstance(node, list):
            return node[self._list_index(node, token, at)]
        raise DashboardPatchError(f"{at} doesn't exist - its parent is a value")


class _Absent:
    def __repr__(self) -> str:
        return "<absent>"


_ABSENT: Any = _Absent()


def apply_ops(config: dict[str, Any], ops: list[dict[str, Any]]) -> PatchResult:
    """The config with every op applied, in order, and what each changed.
    `config` itself is left as it is. Any op that can't be applied refuses
    them all, naming it."""
    patcher = _Patcher(copy.deepcopy(config))
    for number, op in enumerate(ops):
        if not isinstance(op, dict):
            raise DashboardPatchError(f"op {number} is an object, got {op!r}")
        try:
            patcher.apply(number, op)
        except DashboardPatchError as exc:
            raise DashboardPatchError(f"op {number} ({op.get('op')}): {exc}") from None
    return PatchResult(patcher.config, patcher.changes)
