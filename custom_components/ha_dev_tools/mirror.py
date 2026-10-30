"""Push-only git mirroring of confirmed writes to a dedicated private repo.

See docs/AUTOMATION_TESTING_DESIGN.md's "Mirroring" section for the full
design. Talks to the repo host's REST API (via HA's own shared aiohttp
client session) rather than shelling out to a git binary or adding
GitPython as a dependency - no local git repo needed at all, and this
integration otherwise keeps its runtime footprint deliberately light
(RPi/NUC-class installs).

The host is GitHub or GitLab (gitlab.com or self-hosted, issue #182) -
each a small backend with the same five operations (default branch, read
a file, write a file as one commit, read a branch head, point a branch at
a commit). Everything above them - before/after commits, drift detection,
proposed/* branches, the credential scan - is shared, so both hosts
mirror exactly alike.

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
from urllib.parse import quote

from aiohttp import ClientSession
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import mirror_secrets
from .const import (
    DOMAIN,
    GITLAB_DEFAULT_SERVER,
    OPT_MIRROR_ENABLED,
    OPT_MIRROR_PROVIDER,
    OPT_MIRROR_REPO,
    OPT_MIRROR_SERVER,
    OPT_MIRROR_TOKEN,
    OPT_MIRROR_VERIFY_SSL,
)

_LOGGER = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"
TLS_ISSUE_ID = "mirror_tls_unverified"


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


def tls_unverified(options: Any) -> bool:
    """True when these options mirror to GitLab without checking its
    certificate - the only host the switch applies to."""
    return bool(
        options.get(OPT_MIRROR_ENABLED)
        and options.get(OPT_MIRROR_PROVIDER) == "gitlab"
        and options.get(OPT_MIRROR_VERIFY_SSL, True) is False
    )


def refresh_tls_issue(hass: HomeAssistant, options: Any) -> None:
    """Raise or clear the repair issue saying certificate checks are off."""
    if not tls_unverified(options):
        ir.async_delete_issue(hass, DOMAIN, TLS_ISSUE_ID)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        TLS_ISSUE_ID,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=TLS_ISSUE_ID,
        translation_placeholders={
            "server": str(options.get(OPT_MIRROR_SERVER) or GITLAB_DEFAULT_SERVER)
        },
    )


def _url_path(path: str) -> str:
    """A repo path (or branch) as a URL path: each segment percent-encoded,
    and refused if a segment is empty, `.` or `..` - those would let a path
    built from a write's arguments steer the token to another API
    endpoint (`contents/../../...`)."""
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError(f"not a mirror repo path: {path!r}")
    return "/".join(quote(segment, safe="") for segment in segments)


def _url_id(path: str) -> str:
    """A repo path (or branch) as ONE URL segment - GitLab's form for file
    paths, branches and project paths. Checked like _url_path first."""
    _url_path(path)
    return quote(path, safe="")


def proposed_branch_name(kind: str, entity_id: str) -> str:
    """Build a proposed/<kind>-<id> branch name from an arbitrary entity id.

    Automation ids and template unique_ids are user-chosen strings, not
    guaranteed git-ref-safe (spaces, colons, etc. are all valid HA ids but
    invalid in a git ref) - sanitize rather than pass through raw.
    """
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", entity_id).strip("-.") or "unknown"
    return f"proposed/{kind}-{safe_id}"


class _GitHub:
    """GitHub's Contents and Git Data APIs."""

    def __init__(self, session: ClientSession, repo: str, token: str) -> None:
        self.session = session
        self.repo = repo
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def default_branch(self) -> str:
        url = f"{_API_BASE}/repos/{self.repo}"
        async with self.session.get(url, headers=self.headers) as resp:
            resp.raise_for_status()
            body = await resp.json()
        return body["default_branch"]

    async def get_file(self, path: str, branch: str) -> tuple[str, str] | None:
        url = f"{_API_BASE}/repos/{self.repo}/contents/{_url_path(path)}"
        params = {"ref": branch}
        async with self.session.get(url, headers=self.headers, params=params) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            body = await resp.json()
        if body.get("encoding") == "none":
            # A file over 1 MB: the JSON response has its sha but not its
            # content (an empty string, encoding "none" - issue #169); only
            # the raw media type returns it, up to 100 MB.
            raw = {**self.headers, "Accept": "application/vnd.github.raw+json"}
            async with self.session.get(url, headers=raw, params=params) as resp:
                resp.raise_for_status()
                return await resp.text(), body["sha"]
        content = base64.b64decode(body["content"]).decode("utf-8")
        return content, body["sha"]

    async def put_file(
        self, path: str, content: str, *, message: str, sha: str | None, branch: str
    ) -> str:
        url = f"{_API_BASE}/repos/{self.repo}/contents/{_url_path(path)}"
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha is not None:
            payload["sha"] = sha
        async with self.session.put(url, headers=self.headers, json=payload) as resp:
            resp.raise_for_status()
            body = await resp.json()
        return body["content"]["sha"]

    async def ref_sha(self, branch: str) -> str | None:
        url = f"{_API_BASE}/repos/{self.repo}/git/ref/heads/{_url_path(branch)}"
        async with self.session.get(url, headers=self.headers) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            body = await resp.json()
        return body["object"]["sha"]

    async def set_ref(self, branch: str, sha: str, existing: str | None) -> None:
        if existing is None:
            url = f"{_API_BASE}/repos/{self.repo}/git/refs"
            payload: dict[str, Any] = {"ref": f"refs/heads/{branch}", "sha": sha}
            async with self.session.post(
                url, headers=self.headers, json=payload
            ) as resp:
                resp.raise_for_status()
        else:
            url = f"{_API_BASE}/repos/{self.repo}/git/refs/heads/{_url_path(branch)}"
            payload = {"sha": sha, "force": True}
            async with self.session.patch(
                url, headers=self.headers, json=payload
            ) as resp:
                resp.raise_for_status()


class _GitLab:
    """GitLab's REST API v4 (issue #182) - gitlab.com or self-hosted.

    A file's version is its last commit id (the API's optimistic-locking
    token); GitLab returns none on a write, so it's read back with a HEAD.
    A branch can't be force-moved, so a proposed/* branch is deleted and
    recreated - never a protected branch, which GitLab refuses to delete.
    Writes need a token with the `api` scope (`write_repository` is for
    git-over-HTTPS only); a project access token keeps it to the one
    project.
    """

    def __init__(
        self, session: ClientSession, server: str, project: str, token: str
    ) -> None:
        self.session = session
        self.base = f"{server.rstrip('/')}/api/v4/projects/{_url_id(project)}"
        self.headers = {"PRIVATE-TOKEN": token}

    def _file_url(self, path: str) -> str:
        return f"{self.base}/repository/files/{_url_id(path)}"

    async def default_branch(self) -> str:
        async with self.session.get(self.base, headers=self.headers) as resp:
            resp.raise_for_status()
            body = await resp.json()
        if not body.get("default_branch"):
            raise ValueError(
                "the mirror project has no default branch yet - push an "
                "initial commit to it first"
            )
        return body["default_branch"]

    async def get_file(self, path: str, branch: str) -> tuple[str, str] | None:
        async with self.session.get(
            self._file_url(path), headers=self.headers, params={"ref": branch}
        ) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            body = await resp.json()
        content = base64.b64decode(body["content"]).decode("utf-8")
        return content, body["last_commit_id"]

    async def put_file(
        self, path: str, content: str, *, message: str, sha: str | None, branch: str
    ) -> str:
        payload: dict[str, Any] = {
            "branch": branch,
            "commit_message": message,
            "encoding": "base64",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        write = self.session.post
        if sha is not None:
            payload["last_commit_id"] = sha
            write = self.session.put
        async with write(
            self._file_url(path), headers=self.headers, json=payload
        ) as resp:
            resp.raise_for_status()
        async with self.session.head(
            self._file_url(path), headers=self.headers, params={"ref": branch}
        ) as resp:
            resp.raise_for_status()
            return resp.headers["X-Gitlab-Last-Commit-Id"]

    async def ref_sha(self, branch: str) -> str | None:
        url = f"{self.base}/repository/branches/{_url_id(branch)}"
        async with self.session.get(url, headers=self.headers) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            body = await resp.json()
        return body["commit"]["id"]

    async def set_ref(self, branch: str, sha: str, existing: str | None) -> None:
        if existing is not None:
            url = f"{self.base}/repository/branches/{_url_id(branch)}"
            async with self.session.delete(url, headers=self.headers) as resp:
                resp.raise_for_status()
        async with self.session.post(
            f"{self.base}/repository/branches",
            headers=self.headers,
            params={"branch": branch, "ref": sha},
        ) as resp:
            resp.raise_for_status()


def _forge(hass: HomeAssistant) -> _GitHub | _GitLab:
    """The configured host's backend, from the options as they are now."""
    options = _entry(hass).options
    token = options[OPT_MIRROR_TOKEN]
    if options.get(OPT_MIRROR_PROVIDER) == "gitlab":
        verify = options.get(OPT_MIRROR_VERIFY_SSL, True) is not False
        return _GitLab(
            async_get_clientsession(hass, verify_ssl=verify),
            options.get(OPT_MIRROR_SERVER) or GITLAB_DEFAULT_SERVER,
            options[OPT_MIRROR_REPO],
            token,
        )
    return _GitHub(async_get_clientsession(hass), options[OPT_MIRROR_REPO], token)


async def _get_default_branch(hass: HomeAssistant) -> str:
    """Return the mirror repo's actual default branch (issue #50).

    Not every mirror repo has (or should be forced to have) a branch named
    "main" - a repo bootstrapped for one purpose and never merged anywhere,
    or one with a deliberately different default branch name, are both
    real cases. Neither host's file API auto-creates a missing target
    branch, so pushing to a hardcoded "main" silently failed against a real
    repo without one. Queried fresh on every mirror operation rather than
    cached - this is one cheap GET, and a repo's default branch can change.
    """
    return await _forge(hass).default_branch()


async def _get_current(
    hass: HomeAssistant, path: str, *, branch: str
) -> tuple[str, str] | None:
    """Fetch (content, version) for path at the given branch's HEAD, or None
    if it doesn't exist there yet - the version being what the next write
    of it passes back (GitHub's blob sha, GitLab's last commit id)."""
    return await _forge(hass).get_file(path, branch)


async def _put(
    hass: HomeAssistant,
    path: str,
    content: str,
    *,
    message: str,
    sha: str | None,
    branch: str,
) -> str:
    """Create or update a single file at the given branch's HEAD, return its
    new version.

    One file per call, never a broader tree write - keeps each mirror
    commit scoped to exactly the file a write tool actually touched.
    """
    return await _forge(hass).put_file(
        path, content, message=message, sha=sha, branch=branch
    )


async def _get_ref_sha(hass: HomeAssistant, branch: str) -> str | None:
    """Get a branch's current HEAD commit sha, or None if it doesn't exist."""
    return await _forge(hass).ref_sha(branch)


async def _set_ref(hass: HomeAssistant, branch: str, sha: str) -> None:
    """Create branch if it doesn't exist yet, or force-move it to sha if it does.

    Always resetting an existing proposed/* branch to the base commit's
    current tip (rather than leaving old history on it) is what keeps that
    branch's diff a clean "current reality -> would-be change" comparison
    on every dry-run, not an accumulating pile of unrelated old attempts.
    """
    forge = _forge(hass)
    existing = await forge.ref_sha(branch)
    if existing != sha:
        await forge.set_ref(branch, sha, existing)


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


def _scan(content: str, content_type: str) -> list[str]:
    """The structured check for the content's shape, plus the text-level
    one config snapshots use (issue #119): the structured check alone only
    sees a credential-shaped key, so a password inside a string - a JSON
    `payload`, a URL's user:password@, a bearer header, a comment - was
    pushed as-is."""
    return mirror_secrets.find_text_credentials(content) + _SCANNERS[content_type](
        content
    )


def _credential_findings(
    content_before: str | None, content_after: str, content_type: str
) -> list[str]:
    findings = _scan(content_after, content_type)
    if content_before is not None:
        findings = findings + _scan(content_before, content_type)
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
    # Off the event loop: ruamel's YAML() scans its plugin directory, and
    # parsing a large file is CPU work - HA flagged the blocking scandir.
    findings = await hass.async_add_executor_job(
        _credential_findings, content_before, content_after, content_type
    )
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
    # Off the event loop: ruamel's YAML() scans its plugin directory, and
    # parsing a large file is CPU work - HA flagged the blocking scandir.
    findings = await hass.async_add_executor_job(
        _credential_findings, content_before, content_after, content_type
    )
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


@dataclass(frozen=True)
class SnapshotResult:
    """What mirror_snapshots() did, per file."""

    committed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()  # (path, reason)


async def mirror_snapshots(
    hass: HomeAssistant, files: list[tuple[str, str]]
) -> SnapshotResult:
    """Commit each hand-edited config file (issue #105) to the mirror repo's
    default branch as-is, one commit per file whose content changed.

    Unlike mirror_write() there's no before/after pair: nothing here wrote
    the file, so the snapshot itself is what records an edit made outside
    the write tools (by hand, over SSH, ...) as a diff. A file with a
    literal credential anywhere in it (mirror_secrets.find_file_credentials)
    is skipped. Never raises - failures are reported per file.
    """
    committed: list[str] = []
    unchanged: list[str] = []
    skipped: list[tuple[str, str]] = []
    try:
        branch = await _get_default_branch(hass)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        _LOGGER.warning("Config snapshot failed: %s", exc)
        return SnapshotResult(
            skipped=tuple((path, f"mirror push failed: {exc}") for path, _ in files)
        )
    for path, content in files:
        findings = await hass.async_add_executor_job(
            mirror_secrets.find_file_credentials, path, content
        )
        if findings:
            skipped.append((path, _snapshot_skip_reason(findings)))
            continue
        try:
            current = await _get_current(hass, path, branch=branch)
            if current is not None and current[0] == content:
                unchanged.append(path)
                continue
            await _put(
                hass,
                path,
                content,
                message=f"Mirror: {path} snapshot",
                sha=current[1] if current is not None else None,
                branch=branch,
            )
            committed.append(path)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            _LOGGER.warning("Config snapshot of %s failed: %s", path, exc)
            skipped.append((path, f"mirror push failed: {exc}"))
    return SnapshotResult(tuple(committed), tuple(unchanged), tuple(skipped))


def _snapshot_skip_reason(findings: list[str]) -> str:
    return (
        f"looks like it holds a literal credential ({', '.join(findings)}) - "
        "move it to secrets.yaml (`!secret`) to include this file."
    )


async def read_mirrored(hass: HomeAssistant, path: str) -> str | None:
    """The mirror repo's current copy of `path` on its default branch, or
    None if it has none. Raises on a failed request."""
    branch = await _get_default_branch(hass)
    current = await _get_current(hass, path, branch=branch)
    return current[0] if current is not None else None
