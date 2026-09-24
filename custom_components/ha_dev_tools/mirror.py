"""Push-only git mirroring of confirmed writes to a dedicated private repo.

See docs/AUTOMATION_TESTING_DESIGN.md's "Mirroring" section for the full
design. Talks to GitHub over its REST API (via HA's own shared aiohttp
client session) rather than shelling out to a git binary or adding
GitPython as a dependency - no local git repo needed at all, and this
integration otherwise keeps its runtime footprint deliberately light
(RPi/NUC-class installs).

Independent of run mode (live/dry-run) and of the confirm-token gate -
mirror_write()/mirror_dry_run() are only ever called after a write has
already been confirmed (and, for mirror_write(), actually applied).
Mirroring failing must never fail or block the write it's mirroring -
every public function here catches its own errors and reports them in
its return value instead of raising.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import mirror_secrets
from .const import DOMAIN, OPT_MIRROR_ENABLED, OPT_MIRROR_REPO, OPT_MIRROR_TOKEN

_LOGGER = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"


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


def proposed_branch_name(kind: str, entity_id: str) -> str:
    """Build a proposed/<kind>-<id> branch name from an arbitrary entity id.

    Automation ids and template unique_ids are user-chosen strings, not
    guaranteed git-ref-safe (spaces, colons, etc. are all valid HA ids but
    invalid in a git ref) - sanitize rather than pass through raw.
    """
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", entity_id).strip("-.") or "unknown"
    return f"proposed/{kind}-{safe_id}"


async def _get_default_branch(hass: HomeAssistant) -> str:
    """Return the mirror repo's actual default branch (issue #50).

    Not every mirror repo has (or should be forced to have) a branch named
    "main" - a repo bootstrapped for one purpose and never merged anywhere,
    or one with a deliberately different default branch name, are both
    real cases. GitHub's Contents/Git Data APIs don't auto-create a target
    branch if it's missing, so pushing to a hardcoded "main" silently
    failed against a real repo without one. Queried fresh on every mirror
    operation rather than cached - this is one cheap GET, and a repo's
    default branch can change.
    """
    session = async_get_clientsession(hass)
    url = f"{_API_BASE}/repos/{_mirror_repo(hass)}"
    async with session.get(url, headers=_headers(hass)) as resp:
        resp.raise_for_status()
        body = await resp.json()
    return body["default_branch"]


async def _get_current(
    hass: HomeAssistant, path: str, *, branch: str
) -> tuple[str, str] | None:
    """Fetch (content, sha) for path at the given branch's HEAD, or None if it
    doesn't exist there yet."""
    session = async_get_clientsession(hass)
    url = f"{_API_BASE}/repos/{_mirror_repo(hass)}/contents/{path}"
    async with session.get(url, headers=_headers(hass), params={"ref": branch}) as resp:
        if resp.status == 404:
            return None
        resp.raise_for_status()
        body = await resp.json()
    content = base64.b64decode(body["content"]).decode("utf-8")
    return content, body["sha"]


async def _put(
    hass: HomeAssistant,
    path: str,
    content: str,
    *,
    message: str,
    sha: str | None,
    branch: str,
) -> str:
    """Create or update a single file at the given branch's HEAD, return its new sha.

    One file per call, never a broader tree write - keeps each mirror
    commit scoped to exactly the file a write tool actually touched.
    """
    session = async_get_clientsession(hass)
    url = f"{_API_BASE}/repos/{_mirror_repo(hass)}/contents/{path}"
    payload: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha is not None:
        payload["sha"] = sha
    async with session.put(url, headers=_headers(hass), json=payload) as resp:
        resp.raise_for_status()
        body = await resp.json()
    return body["content"]["sha"]


async def _get_ref_sha(hass: HomeAssistant, branch: str) -> str | None:
    """Get a branch's current HEAD commit sha, or None if it doesn't exist."""
    session = async_get_clientsession(hass)
    url = f"{_API_BASE}/repos/{_mirror_repo(hass)}/git/ref/heads/{branch}"
    async with session.get(url, headers=_headers(hass)) as resp:
        if resp.status == 404:
            return None
        resp.raise_for_status()
        body = await resp.json()
    return body["object"]["sha"]


async def _set_ref(hass: HomeAssistant, branch: str, sha: str) -> None:
    """Create branch if it doesn't exist yet, or force-move it to sha if it does.

    Always resetting an existing proposed/* branch to the base commit's
    current tip (rather than leaving old history on it) is what keeps that
    branch's diff a clean "current reality -> would-be change" comparison
    on every dry-run, not an accumulating pile of unrelated old attempts.
    """
    session = async_get_clientsession(hass)
    existing = await _get_ref_sha(hass, branch)
    if existing is None:
        url = f"{_API_BASE}/repos/{_mirror_repo(hass)}/git/refs"
        payload: dict[str, Any] = {"ref": f"refs/heads/{branch}", "sha": sha}
        async with session.post(url, headers=_headers(hass), json=payload) as resp:
            resp.raise_for_status()
    elif existing != sha:
        url = f"{_API_BASE}/repos/{_mirror_repo(hass)}/git/refs/heads/{branch}"
        payload = {"sha": sha, "force": True}
        async with session.patch(url, headers=_headers(hass), json=payload) as resp:
            resp.raise_for_status()


@dataclass(frozen=True)
class MirrorResult:
    """What mirror_write()/mirror_dry_run() actually did, for the write tool's response."""

    mirrored: bool
    reason: str | None = None
    commits: tuple[str, ...] = ()
    branch: str | None = None


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


def _credential_findings(
    content_before: str | None, content_after: str, content_type: str
) -> list[str]:
    scan = _SCANNERS[content_type]
    findings = scan(content_after)
    if content_before is not None:
        findings = findings + scan(content_before)
    return findings


def _credential_skip_reason(path: str, content_type: str, findings: list[str]) -> str:
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
    return f"'{path}' has a credential-shaped key ({', '.join(findings)}) {advice}"


async def _sync_before(
    hass: HomeAssistant, path: str, content_before: str | None, *, branch: str
) -> tuple[str | None, str | None, list[str]]:
    """Sync the target branch to content_before if it's drifted, return the
    resulting (current_content, current_sha, commits) - shared by
    mirror_write() (before its own after-commit to the default branch) and
    mirror_dry_run() (before branching proposed/* off of it)."""
    commits: list[str] = []
    current = await _get_current(hass, path, branch=branch)
    current_content = current[0] if current is not None else None
    current_sha = current[1] if current is not None else None

    if content_before is not None and current_content != content_before:
        current_sha = await _put(
            hass,
            path,
            content_before,
            message=f"Mirror: live state of {path} before write",
            sha=current_sha,
            branch=branch,
        )
        current_content = content_before
        commits.append("before")

    return current_content, current_sha, commits


async def mirror_write(
    hass: HomeAssistant,
    *,
    path: str,
    content_before: str | None,
    content_after: str,
    content_type: str = "yaml",
) -> MirrorResult:
    """Mirror a confirmed, applied write's before/after content for one file.

    - Scans content_after (and content_before, if present) for credentials
      first (mirror_secrets.py / issue #39, via the scanner content_type
      selects) - skips mirroring entirely, neither commit, if either is
      flagged.
    - Resolves the mirror repo's actual default branch (issue #50) rather
      than assuming "main" - not every repo has one, or has that name.
    - Before-commit: syncs the default branch to content_before, but only
      if it differs from what the mirror repo currently has recorded for
      this path - a no-op if nothing's drifted since the last mirrored
      write, a real commit (capturing that drift) if it has.
    - After-commit: pushes content_after, skipped if it's already what the
      mirror repo now has (e.g. the write produced byte-identical content).
    """
    findings = _credential_findings(content_before, content_after, content_type)
    if findings:
        return MirrorResult(
            mirrored=False,
            reason=_credential_skip_reason(path, content_type, findings),
        )

    try:
        default_branch = await _get_default_branch(hass)
        current_content, current_sha, commits = await _sync_before(
            hass, path, content_before, branch=default_branch
        )

        if current_content != content_after:
            await _put(
                hass,
                path,
                content_after,
                message=f"Mirror: {path} after write",
                sha=current_sha,
                branch=default_branch,
            )
            commits.append("after")
    except Exception as exc:  # noqa: BLE001 - never let a mirror failure fail the write
        _LOGGER.warning("Mirroring %s failed: %s", path, exc)
        return MirrorResult(mirrored=False, reason=f"mirror push failed: {exc}")

    return MirrorResult(mirrored=True, commits=tuple(commits))


async def mirror_dry_run(
    hass: HomeAssistant,
    *,
    path: str,
    content_before: str | None,
    content_after: str,
    kind: str,
    entity_id: str,
    content_type: str = "yaml",
) -> MirrorResult:
    """Mirror a dry-run write's resolved would-be content, per
    docs/AUTOMATION_TESTING_DESIGN.md's "Mirroring" section: nothing live
    changed, so instead of an after-commit to the default branch, the
    would-be content goes to its own proposed/<kind>-<id> branch, freshly
    branched from the default branch's current HEAD - after still syncing
    the default branch to content_before first (same drift-detection
    reasoning as mirror_write's before-commit; this runs "on every
    confirmed write, either run mode", per that doc).

    Never touches the default branch's own after-state, since nothing was
    actually written - only mirror_write() (a live, applied write) does
    that. Resolves the mirror repo's actual default branch (issue #50)
    rather than assuming "main".
    """
    findings = _credential_findings(content_before, content_after, content_type)
    if findings:
        return MirrorResult(
            mirrored=False,
            reason=_credential_skip_reason(path, content_type, findings),
        )

    branch = proposed_branch_name(kind, entity_id)
    try:
        default_branch = await _get_default_branch(hass)
        _current_content, _current_sha, commits = await _sync_before(
            hass, path, content_before, branch=default_branch
        )

        default_sha = await _get_ref_sha(hass, default_branch)
        if default_sha is None:
            return MirrorResult(
                mirrored=False,
                reason=(
                    f"'{default_branch}' branch doesn't exist yet in the "
                    "mirror repo - nothing to branch proposed/* off of. "
                    "Create it (even as an empty initial commit) to enable "
                    "dry-run mirroring."
                ),
            )
        await _set_ref(hass, branch, default_sha)

        proposed_current = await _get_current(hass, path, branch=branch)
        proposed_content = proposed_current[0] if proposed_current is not None else None
        proposed_sha = proposed_current[1] if proposed_current is not None else None
        if proposed_content != content_after:
            await _put(
                hass,
                path,
                content_after,
                message=f"Propose: would-be {path} from a dry-run write",
                sha=proposed_sha,
                branch=branch,
            )
            commits.append("proposed")
    except Exception as exc:  # noqa: BLE001 - never let a mirror failure fail the write
        _LOGGER.warning("Dry-run mirroring %s failed: %s", path, exc)
        return MirrorResult(mirrored=False, reason=f"mirror push failed: {exc}")

    return MirrorResult(mirrored=True, commits=tuple(commits), branch=branch)
