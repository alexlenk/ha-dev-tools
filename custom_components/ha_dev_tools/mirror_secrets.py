"""Credential detection for content about to be pushed by mirror.py.

See docs/AUTOMATION_TESTING_DESIGN.md and issue #39: mirroring pushes real
config content to a private GitHub repo, so before any push happens this
scans that content for a credential-shaped key holding a literal value -
HA's own `!secret` convention is the safe/unsafe signal, reused rather
than invented. Deliberately a precise, HA-convention-aware check, not a
general entropy/pattern secret scanner (gitleaks-style) - see issue #39's
"explicitly not in scope for a first version".
"""

from __future__ import annotations

import json
import re
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import TaggedScalar
from ruamel.yaml.scalarstring import ScalarString

# Substrings, not exact matches - "api_key", "wifi_password", "client_secret",
# and "secret_key" (say) should all be caught by "key"/"password"/"secret"/"token".
SENSITIVE_KEY_SUBSTRINGS = (
    "password",
    "token",
    "api_key",
    "apikey",
    "access_token",
    "client_secret",
    "private_key",
    "secret",
    "passphrase",
    "psk",
    "encryption_key",
    "network_key",
    "authorization",
)


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    # X-Api-Key, client-secret: an HTTP header's spelling of the same names.
    lowered = key.lower().replace("-", "_")
    return any(needle in lowered for needle in SENSITIVE_KEY_SUBSTRINGS)


def _is_secret_tagged(value: Any) -> bool:
    """True if this value is a ruamel !secret tagged scalar, not a plain string."""
    tag = getattr(value, "tag", None)
    if tag is None:
        return False
    tag_value = tag.value if hasattr(tag, "value") else str(tag)
    return tag_value == "!secret"


def _walk(node: Any, *, path: str, findings: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            if (
                _is_sensitive_key(key)
                and isinstance(value, (str, ScalarString, TaggedScalar))
                and not _is_secret_tagged(value)
            ):
                findings.append(child_path)
            _walk(value, path=child_path, findings=findings)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _walk(item, path=f"{path}[{index}]", findings=findings)


def find_yaml_credentials(content: str) -> list[str]:
    """Return dotted-path findings for credential-shaped keys with a literal value.

    A key counts as flagged only if its name looks credential-shaped (see
    SENSITIVE_KEY_SUBSTRINGS) AND its value is a plain string rather than a
    `!secret xxx` reference - ruamel's round-trip loader represents that tag
    as a distinct scalar type (see docs/AUTOMATION_TESTING_DESIGN.md), so
    this never flags the safe, secrets.yaml-routed form. Returns an empty
    list for content that doesn't parse as YAML at all - mirror.py's
    content is always this integration's own generated output, so an
    unparseable string here would be a bug elsewhere, not a reason to
    silently treat it as credential-free.
    """
    yaml = YAML(typ="rt")
    try:
        document = yaml.load(content)
    except Exception:  # noqa: BLE001 - any parse failure, ruamel raises several types
        return ["<content did not parse as YAML - treated as unsafe to mirror>"]
    findings: list[str] = []
    _walk(document, path="", findings=findings)
    return findings


def find_storage_credentials(content: str) -> list[str]:
    """Same check for storage-based content (a .storage/<domain> file's raw
    JSON text, or a resolved config-entry's .data/.options serialized the
    same way) - no !secret mechanism exists there, so any credential-shaped
    key with a literal string value is flagged. Same shape as
    find_yaml_credentials (raw text in, findings out) for a uniform call
    site in mirror.py; unparseable content is treated as unsafe to mirror,
    same reasoning as find_yaml_credentials.
    """
    try:
        data = json.loads(content)
    except ValueError:
        return ["<content did not parse as JSON - treated as unsafe to mirror>"]
    findings: list[str] = []
    _walk(data, path="", findings=findings)
    return findings


# Text-level check for raw config files (issue #105) - snapshots and
# get_config_file return a hand-edited file verbatim, so a file is only
# released if no line looks like it holds a literal credential. Stricter
# than find_yaml_credentials on purpose: it also works on a file that no
# longer parses (the case get_config_file exists for), on Jinja macros, on
# comments, and inside strings (a JSON `payload` with "password": "...").
# A false positive only means a refused read, never a leak.
_TEXT_CREDENTIAL = re.compile(
    r"""(?<![\w.-])["']?
    ([\w.-]*(?:password|passwd|passcode|passphrase|pwd|psk|token|api_?key
    |secret|private_?key|encryption_?key|network_?key|credential
    |authorization)[\w.-]*)
    ["']?[ \t]*[:=](?!=)[ \t]*([^\s,}\]]*)""",
    re.IGNORECASE | re.VERBOSE,
)
# `key:` with nothing after it opens a nested block (its lines are checked
# on their own); these are references, empty, or a flag (`show_token:
# true`) - not literal credentials.
_SAFE_VALUES = {
    "",
    '""',
    "''",
    "!secret",
    "!env_var",
    "null",
    "~",
    "none",
    "true",
    "false",
}
_URL_USERINFO = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
_BEARER = re.compile(r"\bbearer\s+[a-z0-9._~+/=-]{8,}", re.IGNORECASE)


def find_text_credentials(content: str) -> list[str]:
    """`line N: <key>` for every line that looks like it holds a literal
    credential. Never includes the value itself."""
    findings: list[str] = []
    for number, line in enumerate(content.splitlines(), start=1):
        for match in _TEXT_CREDENTIAL.finditer(line):
            if match.group(2).lower() not in _SAFE_VALUES:
                findings.append(f"line {number}: {match.group(1)}")
        if _URL_USERINFO.search(line):
            findings.append(f"line {number}: password in a URL")
        if _BEARER.search(line):
            findings.append(f"line {number}: bearer token")
    return findings


def find_file_credentials(path: str, content: str) -> list[str]:
    """Credential findings for one raw config file: the text check, plus
    find_yaml_credentials for a YAML file that parses (a broken one is left
    to the text check alone rather than refused outright)."""
    findings = find_text_credentials(content)
    if path.endswith((".yaml", ".yml")):
        structured = find_yaml_credentials(content)
        if not (structured and structured[0].startswith("<content did not parse")):
            findings += [f"key {finding}" for finding in structured]
    return findings


WITHHELD = "<withheld: a literal credential - move it to secrets.yaml (!secret)>"


def _mask_entry(key: Any, value: Any) -> Any:
    """`value` as mask_credentials shows it under `key`."""
    if (
        _is_sensitive_key(key)
        and isinstance(value, str | int | float)
        and not isinstance(value, bool)
        and not str(value).startswith(("!secret ", "!env_var "))
    ):
        return WITHHELD
    return mask_credentials(value)


def mask_credentials(node: Any) -> Any:
    """A JSON-safe config (yaml_style.to_json_safe) with every literal
    credential replaced by WITHHELD, for a read-only tool's answer:
    credential-shaped keys' values (but not `!secret ...` references), and
    passwords in URLs and bearer tokens inside any string - the same checks
    get_config_file withholds a block for (issue #105)."""
    if isinstance(node, dict):
        return {key: _mask_entry(key, value) for key, value in node.items()}
    if isinstance(node, list):
        return [mask_credentials(item) for item in node]
    if isinstance(node, str):
        node = _URL_USERINFO.sub(f"://{WITHHELD}@", node)
        return _BEARER.sub(f"Bearer {WITHHELD}", node)
    return node


def mask_misread(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """find_misread_scalars' findings, without the value of one under a
    credential-shaped key - they echo what's written."""
    masked = []
    for finding in findings:
        key = re.split(r"[.\[]", str(finding.get("path", "")))[-1]
        if _is_sensitive_key(key):
            finding = {**finding, "written": WITHHELD, "ha_reads_as": WITHHELD}
        masked.append(finding)
    return masked


# --- A masked config written back (issue #151) --------------------------------
#
# The read tools mask literal credentials, but an agent reads a config,
# changes one field and writes the whole item back - the placeholder would
# overwrite the real value. So a write puts each original back: the value
# at the same place in the current config if it masks to exactly what came
# back, else the one value anywhere under the same key that does. Anything
# else - the text around a masked part edited, a new item, two originals
# that fit - is refused, never guessed.

_MISSING = object()


def contains_withheld(node: Any) -> bool:
    if isinstance(node, dict):
        return any(contains_withheld(value) for value in node.values())
    if isinstance(node, list):
        return any(contains_withheld(item) for item in node)
    return isinstance(node, str) and WITHHELD in node


def _path(parts: tuple[Any, ...]) -> str:
    text = ""
    for part in parts:
        text += (
            f"[{part}]"
            if isinstance(part, int)
            else (f".{part}" if text else str(part))
        )
    return text


def _leaves(node: Any, key: Any = None) -> list[tuple[Any, Any]]:
    """(the key it's under, value) for every scalar - a list item's key is
    the list's."""
    if isinstance(node, dict):
        return [leaf for k, v in node.items() for leaf in _leaves(v, k)]
    if isinstance(node, list):
        return [leaf for item in node for leaf in _leaves(item, key)]
    return [(key, node)]


def _at(node: Any, parts: tuple[Any, ...]) -> Any:
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and isinstance(part, int) and part < len(node):
            node = node[part]
        else:
            return _MISSING
    return node


def restore_withheld(new: Any, current: Any) -> tuple[Any, list[str], list[Any]]:
    """`new` with each value holding WITHHELD replaced by its original from
    `current` (None: there is none). Returns (restored, paths that couldn't
    be, the originals put back)."""
    problems: list[str] = []
    restored: list[Any] = []
    index = _leaves(current) if current is not None else []

    def fix(node: Any, parts: tuple[Any, ...], key: Any) -> Any:
        if isinstance(node, dict):
            return {k: fix(v, parts + (k,), k) for k, v in node.items()}
        if isinstance(node, list):
            return [fix(item, parts + (i,), key) for i, item in enumerate(node)]
        if not (isinstance(node, str) and WITHHELD in node):
            return node
        same = _at(current, parts) if current is not None else _MISSING
        if same is not _MISSING and _mask_entry(key, same) == node:
            restored.append(same)
            return same
        fits = {
            repr(value): value
            for leaf_key, value in index
            if leaf_key == key and _mask_entry(leaf_key, value) == node
        }
        if len(fits) == 1:
            value = next(iter(fits.values()))
            restored.append(value)
            return value
        problems.append(_path(parts))
        return node

    return fix(new, (), None), problems, restored


def _fragments(value: Any) -> list[str]:
    """The parts of a restored value that must not show up in an answer."""
    text = str(value)
    found = [text]
    found += [m.group(0)[3:-1] for m in _URL_USERINFO.finditer(text)]
    found += [m.group(0).split(None, 1)[1] for m in _BEARER.finditer(text)]
    return [fragment for fragment in found if len(fragment) >= 3]


def scrub(node: Any, originals: list[Any]) -> Any:
    """`node` with every restored original (and its URL password / bearer
    token) replaced by WITHHELD - an answer computed from a restored
    config, a validation error quoting a value, must not carry it."""
    fragments = sorted(
        {fragment for value in originals for fragment in _fragments(value)},
        key=len,
        reverse=True,
    )

    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: clean(value) for key, value in item.items()}
        if isinstance(item, list):
            return [clean(value) for value in item]
        if isinstance(item, str):
            for fragment in fragments:
                item = item.replace(fragment, WITHHELD)
            return item
        if not isinstance(item, bool) and item in originals:
            return WITHHELD
        return item

    return clean(node)


def literal_credentials(node: Any, path: str = "") -> list[str]:
    """Where a JSON-safe config holds a literal credential - for a write
    that refuses one (issue #168): a credential-shaped key's value that
    isn't a `!secret`/`!env_var` reference, `username` included, and a
    password in a URL or a bearer token inside any string. Paths only,
    never the values."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if (
                (_is_sensitive_key(key) or str(key).lower() == "username")
                and isinstance(value, str | int | float)
                and not isinstance(value, bool)
                and not str(value).startswith(("!secret ", "!env_var "))
            ):
                found.append(child)
            else:
                found += literal_credentials(value, child)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found += literal_credentials(item, f"{path}[{index}]")
    elif isinstance(node, str) and (_URL_USERINFO.search(node) or _BEARER.search(node)):
        found.append(path)
    return found
