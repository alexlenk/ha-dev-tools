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
)


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
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
    ([\w.-]*(?:password|passwd|passcode|pwd|token|api_?key|secret|private_?key
    |credential|authorization)[\w.-]*)
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
