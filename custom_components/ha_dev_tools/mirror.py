"""Push-only git mirroring of confirmed writes to a dedicated private repo.

See docs/AUTOMATION_TESTING_DESIGN.md's "Mirroring" section for the full
design. Talks to GitHub over its REST API (via HA's own shared aiohttp
client session) rather than shelling out to a git binary or adding
GitPython as a dependency - no local git repo needed at all, and this
integration otherwise keeps its runtime footprint deliberately light
(RPi/NUC-class installs).

Independent of run mode (live/dry-run) and of the confirm-token gate -
mirror_write() is only ever called after a write has already been
confirmed and (in live mode) actually applied. Mirroring failing must
never fail or block the write it's mirroring - every public function here
catches its own errors and reports them in its return value instead of
raising.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import mirror_secrets
from .const import DOMAIN, OPT_MIRROR_ENABLED, OPT_MIRROR_REPO, OPT_MIRROR_TOKEN

_LOGGER = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"
_TARGET_BRANCH = "main"


def _entry(hass: HomeAssistant):
    entries = hass.config_entries.async_entries(DOMAIN)
    return entries[0] if entries else None


def is_mirror_enabled(hass: HomeAssistant) -> bool:
    """True if mirroring is turned on and minimally configured.

    Read fresh from the config entry on every call, matching
    access_control.is_dry_run()'s "always re-derive, never cache" approach.
    """
    entry = _entry(hass)
    if entry is None:
        return False
    options = entry.options
    return bool(
        options.get(OPT_MIRROR_ENABLED, False)
        and options.get(OPT_MIRROR_REPO)
        and options.get(OPT_MIRROR_TOKEN)
    )


def _mirror_repo(hass: HomeAssistant) -> str:
    entry = _entry(hass)
    return entry.options[OPT_MIRROR_REPO]


def _mirror_token(hass: HomeAssistant) -> str:
    entry = _entry(hass)
    return entry.options[OPT_MIRROR_TOKEN]


def _headers(hass: HomeAssistant) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_mirror_token(hass)}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _get_current(hass: HomeAssistant, path: str) -> tuple[str, str] | None:
    """Fetch (content, sha) for path at the mirror repo's main HEAD, or None if it
    doesn't exist there yet."""
    session = async_get_clientsession(hass)
    url = f"{_API_BASE}/repos/{_mirror_repo(hass)}/contents/{path}"
    async with session.get(
        url, headers=_headers(hass), params={"ref": _TARGET_BRANCH}
    ) as resp:
        if resp.status == 404:
            return None
        resp.raise_for_status()
        body = await resp.json()
    content = base64.b64decode(body["content"]).decode("utf-8")
    return content, body["sha"]


async def _put(
    hass: HomeAssistant, path: str, content: str, *, message: str, sha: str | None
) -> str:
    """Create or update a single file at the mirror repo's main HEAD, return its new sha.

    One file per call, never a broader tree write - keeps each mirror
    commit scoped to exactly the file a write tool actually touched.
    """
    session = async_get_clientsession(hass)
    url = f"{_API_BASE}/repos/{_mirror_repo(hass)}/contents/{path}"
    payload: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": _TARGET_BRANCH,
    }
    if sha is not None:
        payload["sha"] = sha
    async with session.put(url, headers=_headers(hass), json=payload) as resp:
        resp.raise_for_status()
        body = await resp.json()
    return body["content"]["sha"]


@dataclass(frozen=True)
class MirrorResult:
    """What mirror_write() actually did, for the write tool's response."""

    mirrored: bool
    reason: str | None = None
    commits: tuple[str, ...] = ()


# Which mirror_secrets.py scanner to run, keyed by the pushed content's own
# shape - "yaml" for a real config file (write_automation, template
# entities), "json" for a .storage/<domain> file's raw content (helpers,
# dashboards) or a resolved config-entry's serialized .data/.options
# (derived sensors). Both take raw text in, findings out - see
# mirror_secrets.py.
_SCANNERS = {
    "yaml": mirror_secrets.find_yaml_credentials,
    "json": mirror_secrets.find_storage_credentials,
}


async def mirror_write(
    hass: HomeAssistant,
    *,
    path: str,
    content_before: str | None,
    content_after: str,
    content_type: str = "yaml",
) -> MirrorResult:
    """Mirror a confirmed write's before/after content for one file.

    - Scans content_after (and content_before, if present) for credentials
      first (mirror_secrets.py / issue #39, via the scanner content_type
      selects) - skips mirroring entirely, neither commit, if either is
      flagged.
    - Before-commit: syncs main to content_before, but only if it differs
      from what the mirror repo currently has recorded for this path - a
      no-op if nothing's drifted since the last mirrored write, a real
      commit (capturing that drift) if it has.
    - After-commit: pushes content_after, skipped if it's already what the
      mirror repo now has (e.g. the write produced byte-identical content).
    """
    scan = _SCANNERS[content_type]
    findings = scan(content_after)
    if content_before is not None:
        findings = findings + scan(content_before)
    if findings:
        if content_type == "yaml":
            advice = (
                "not routed through !secret - move it to secrets.yaml to "
                "enable mirroring for this file."
            )
        else:
            advice = (
                "with a literal value - HA's storage files have no !secret "
                "mechanism, so this file can't be mirrored as-is."
            )
        return MirrorResult(
            mirrored=False,
            reason=(
                f"'{path}' has a credential-shaped key "
                f"({', '.join(findings)}) {advice}"
            ),
        )

    commits: list[str] = []
    try:
        current = await _get_current(hass, path)
        current_content = current[0] if current is not None else None
        current_sha = current[1] if current is not None else None

        if content_before is not None and current_content != content_before:
            current_sha = await _put(
                hass,
                path,
                content_before,
                message=f"Mirror: live state of {path} before write",
                sha=current_sha,
            )
            current_content = content_before
            commits.append("before")

        if current_content != content_after:
            await _put(
                hass,
                path,
                content_after,
                message=f"Mirror: {path} after write",
                sha=current_sha,
            )
            commits.append("after")
    except Exception as exc:  # noqa: BLE001 - never let a mirror failure fail the write
        _LOGGER.warning("Mirroring %s failed: %s", path, exc)
        return MirrorResult(mirrored=False, reason=f"mirror push failed: {exc}")

    return MirrorResult(mirrored=True, commits=tuple(commits))
