"""ESPHome device configs in /config/esphome (issue #197).

The ESPHome Builder add-on keeps its device YAMLs in /config/esphome; none
of the config tools reached them. An agent editing firmware on a separate
build host (compiles kept off a small HA box) needs to read the live
configs, notice drift when someone edits a device in the add-on, and bring
changed configs back. Compiling and flashing stay out of scope.

Per the owner's decisions on #197:
- every *.yaml/*.yml under esphome/, subfolders included (shared includes
  a device file `!include`s) - never a secrets.yaml (any folder), the
  .esphome/ build folder or another hidden file;
- literal credentials are masked on read with PLACEHOLDER: a value under a
  credential-shaped key (password, token, api_key, ...) or under `key`
  (api: encryption: key). `!secret` references stay as written;
- a write puts a placeholder sent back unchanged back to the original -
  the same place in the current file, else the one credential under the
  same key - so read, edit, write never replaces a device's API/OTA key
  with the placeholder (it would break its pairing); one that matches
  nothing is refused. A NEW literal credential is refused: !secret only;
- a write must parse as YAML, ESPHome's own tags (!secret, !lambda,
  !include, !extend, ...) allowed - not HA's loader, which doesn't know
  them;
- delete backs the file up first.

The masking edits the raw text in place (the value's own characters on its
line), so everything else in the file - comments, tags, formatting - comes
back byte for byte.
"""

from __future__ import annotations

import difflib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from homeassistant.core import HomeAssistant
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq, TaggedScalar
from ruamel.yaml.error import YAMLError

from . import mirror_secrets
from .file_manager import FileManager

ESPHOME_DIR = "esphome"
PLACEHOLDER = "<withheld>"
_QUOTED_PLACEHOLDER = f'"{PLACEHOLDER}"'
_SUFFIXES = (".yaml", ".yml")


class EsphomeConfigError(ValueError):
    """An ESPHome config request that can't be served - nothing changed."""


def esphome_path(name: str) -> str:
    """The config-relative path of a device config `name` (relative to
    esphome/), refusing anything else."""
    path = PurePosixPath(name.replace("\\", "/").strip())
    if (
        not name.strip()
        or path.is_absolute()
        or any(part in ("..", "") or part.startswith(".") for part in path.parts)
        or path.suffix not in _SUFFIXES
    ):
        raise EsphomeConfigError(
            f"name is a .yaml file's path inside esphome/ (e.g. 'kitchen.yaml' "
            f"or 'common/wifi.yaml'), not {name!r}"
        )
    if path.name == "secrets.yaml":
        raise EsphomeConfigError(
            "secrets.yaml is never read or written here - keep it on the host"
        )
    return f"{ESPHOME_DIR}/{path.as_posix()}"


def list_configs(config_dir: str) -> list[dict[str, Any]]:
    """Every ESPHome config file: name, size, modified, and whether it's a
    device (top level) or an include (subfolder). Blocking."""
    root = Path(config_dir) / ESPHOME_DIR
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if (
            not path.is_file()
            or path.suffix not in _SUFFIXES
            or path.name == "secrets.yaml"
            or any(part.startswith(".") for part in rel.parts)
        ):
            continue
        stat = path.stat()
        found.append(
            {
                "name": rel.as_posix(),
                "kind": "device" if len(rel.parts) == 1 else "include",
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
            }
        )
    return found


def _is_credential_key(key: Any) -> bool:
    return isinstance(key, str) and (
        key.lower() == "key" or mirror_secrets._is_sensitive_key(key)
    )


def _is_literal(value: Any) -> bool:
    """A value written out - not a `!secret` (or any other tagged) value,
    which ruamel loads as a TaggedScalar."""
    if isinstance(value, TaggedScalar | bool) or not isinstance(
        value, str | int | float
    ):
        return False
    return value != PLACEHOLDER


def _load(content: str) -> Any:
    try:
        return YAML(typ="rt").load(content)
    except YAMLError as err:
        raise EsphomeConfigError(f"not valid YAML: {err}") from None


def _leaves(node: Any, path: tuple[Any, ...] = ()):
    """(path, key, value, line, col) for every scalar under a mapping key."""
    if isinstance(node, CommentedMap):
        for key, value in node.items():
            if isinstance(value, CommentedMap | CommentedSeq):
                yield from _leaves(value, path + (key,))
            else:
                line, col = node.lc.value(key)
                yield path + (key,), key, value, line, col
    elif isinstance(node, CommentedSeq):
        for index, item in enumerate(node):
            yield from _leaves(item, path + (index,))


def _credentials(document: Any) -> list[tuple[tuple[Any, ...], Any, Any, int, int]]:
    return [
        leaf
        for leaf in _leaves(document)
        if _is_credential_key(leaf[1]) and _is_literal(leaf[2])
    ]


def _token_end(line: str, col: int) -> int:
    """Where the scalar starting at `col` ends on its line."""
    quote = line[col] if col < len(line) else ""
    if quote in ('"', "'"):
        index = col + 1
        while index < len(line):
            if line[index] == "\\" and quote == '"':
                index += 2
                continue
            if line[index] == quote:
                if quote == "'" and line[index + 1 : index + 2] == "'":
                    index += 2
                    continue
                return index + 1
            index += 1
        return len(line)
    end = line.find(" #", col)
    end = len(line) if end == -1 else end
    for stop in (",", "}", "]"):
        flow = line.find(stop, col, end)
        if flow != -1:
            end = flow
    return col + len(line[col:end].rstrip())


def _replace_tokens(content: str, replacements: list[tuple[int, int, str]]) -> str:
    """`content` with the scalar at each (line, col) replaced by text -
    right to left within a line, so earlier columns stay put."""
    lines = content.splitlines(keepends=True)
    for line_no, col, text in sorted(replacements, key=lambda r: (r[0], -r[1])):
        line = lines[line_no]
        body = line.rstrip("\r\n")
        lines[line_no] = (
            body[:col] + text + body[_token_end(body, col) :] + line[len(body) :]
        )
    return "".join(lines)


def _multiline(content: str, line: int, col: int) -> bool:
    text = content.splitlines()[line]
    return text[col : col + 1] in ("|", ">")


def mask(content: str) -> tuple[str, list[str]]:
    """`content` with every literal credential replaced by PLACEHOLDER, and
    where each was. Refuses a credential it can't mask in place."""
    document = _load(content)
    found = _credentials(document)
    paths = [_path(path) for path, *_ in found]
    for path, _, _, line, col in found:
        if _multiline(content, line, col):
            raise EsphomeConfigError(
                f"{_path(path)} holds a literal credential in a multi-line "
                "value, which can't be masked - move it to secrets.yaml"
            )
    masked = _replace_tokens(
        content, [(line, col, _QUOTED_PLACEHOLDER) for _, _, _, line, col in found]
    )
    return masked, paths


def _path(parts: tuple[Any, ...]) -> str:
    text = ""
    for part in parts:
        text += (
            f"[{part}]"
            if isinstance(part, int)
            else (f".{part}" if text else str(part))
        )
    return text


def restore(content: str, current: str | None) -> str:
    """`content` with each PLACEHOLDER put back to the original from
    `current` - or refused, with every reason, if one can't be, or if a new
    literal credential is in it. See the module docstring."""
    document = _load(content)
    placeholders = [leaf for leaf in _leaves(document) if leaf[2] == PLACEHOLDER]
    originals: dict[tuple[Any, ...], tuple[Any, str]] = {}
    current_lines = current.splitlines() if current is not None else []
    if current is not None:
        for path, key, value, line, col in _credentials(_load(current)):
            raw = current_lines[line][col : _token_end(current_lines[line], col)]
            originals[path] = (key, raw)
    problems: list[str] = []
    replacements: list[tuple[int, int, str]] = []
    for path, key, _, line, col in placeholders:
        original = originals.get(path)
        if original is None:
            fits = {raw for each_key, raw in originals.values() if each_key == key}
            original = (key, next(iter(fits))) if len(fits) == 1 else None
        if original is None:
            problems.append(
                f"{_path(path)}: a masked credential that matches nothing in the "
                "current file - send the full value as !secret"
            )
            continue
        replacements.append((line, col, original[1]))
    restored = _replace_tokens(content, replacements) if replacements else content
    known = {raw for _, raw in originals.values()}
    restored_lines = restored.splitlines()
    for path, _, _, line, col in _credentials(_load(restored)):
        raw = restored_lines[line][col : _token_end(restored_lines[line], col)]
        if raw not in known:
            problems.append(
                f"{_path(path)}: a new literal credential - use '!secret <name>' "
                "(esphome/secrets.yaml) instead"
            )
    if problems:
        raise EsphomeConfigError("; ".join(problems))
    return restored


def diff(before: str | None, after: str, name: str) -> str:
    """A unified diff of the masked before and after - never a credential."""
    old = mask(before)[0] if before is not None else ""
    new = mask(after)[0]
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"{name} (current)" if before is not None else "/dev/null",
            tofile=f"{name} (new)",
        )
    )


class EsphomeManager:
    """Reads and writes ESPHome configs through the FileManager."""

    def __init__(self, hass: HomeAssistant, file_manager: FileManager) -> None:
        self.hass = hass
        self.file_manager = file_manager

    async def list(self) -> list[dict[str, Any]]:
        return await self.hass.async_add_executor_job(
            list_configs, self.hass.config.config_dir
        )

    async def read(self, name: str) -> str | None:
        """A config's raw text, or None if there is none (never masked)."""
        try:
            return await self.file_manager.read_file(esphome_path(name))
        except FileNotFoundError:
            return None

    async def get(self, name: str) -> dict[str, Any]:
        content = await self.read(name)
        if content is None:
            raise EsphomeConfigError(f"there is no '{esphome_path(name)}'")
        masked, credentials = await self.hass.async_add_executor_job(mask, content)
        metadata = await self.file_manager.get_file_metadata(esphome_path(name))
        return {
            "name": name,
            "file_path": esphome_path(name),
            "content": masked,
            "masked": credentials,
            "content_hash": metadata.get("content_hash"),
        }

    async def plan_write(self, name: str, content: str) -> tuple[str | None, str]:
        """(current content or None, the content to write) - refused with
        every reason first."""
        current = await self.read(name)
        restored = await self.hass.async_add_executor_job(restore, content, current)
        return current, restored

    async def write(
        self, name: str, content: str, expected_hash: str | None = None
    ) -> None:
        await self.file_manager.write_file(
            esphome_path(name),
            content,
            expected_hash=expected_hash,
            validate_before_write=False,
        )

    async def delete(self, name: str) -> tuple[str, str]:
        """Back the file up, then delete it: (its content, the backup's
        path). No backup, no delete - unlike a write's backup, a failure
        here isn't just logged."""
        file_path = esphome_path(name)
        content = await self.read(name)
        if content is None:
            raise EsphomeConfigError(f"there is no '{file_path}'")
        config_dir = Path(self.hass.config.config_dir)
        backup = await self.hass.async_add_executor_job(
            self.file_manager._backup_sync, config_dir / file_path, datetime.now()
        )
        await self.file_manager.delete_file(file_path)
        return content, Path(backup).relative_to(config_dir).as_posix()
