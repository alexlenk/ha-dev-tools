"""Mirroring to GitLab (issue #182): the GitLab backend behind the same
mirror_write / mirror_dry_run / snapshot / read paths as GitHub, the
certificate switch and its repair issue."""

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_dev_tools import mirror
from custom_components.ha_dev_tools.const import (
    DOMAIN,
    OPT_MIRROR_ENABLED,
    OPT_MIRROR_PROVIDER,
    OPT_MIRROR_REPO,
    OPT_MIRROR_SERVER,
    OPT_MIRROR_TOKEN,
    OPT_MIRROR_VERIFY_SSL,
)
from tests.test_mirror import FakeSession, _b64, _FakeResponse

BASE = "https://git.example.com/api/v4/projects/home%2Fha%2Fmirror"
FILE = f"{BASE}/repository/files/automations.yaml"


class _GitLabResponse(_FakeResponse):
    def __init__(self, status: int, payload: object = None, headers=None):
        super().__init__(status, payload)
        self.headers = headers or {}


class GitLabSession(FakeSession):
    def head(self, url, **kwargs):
        return self._next("HEAD", url, kwargs)

    def delete(self, url, **kwargs):
        return self._next("DELETE", url, kwargs)


def _committed(commit: str) -> _GitLabResponse:
    """The HEAD after a write: the file's new last commit id."""
    return _GitLabResponse(200, headers={"X-Gitlab-Last-Commit-Id": commit})


def _file(content: str, commit: str) -> _GitLabResponse:
    return _GitLabResponse(200, {"content": _b64(content), "last_commit_id": commit})


@pytest.fixture
async def gitlab_entry(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_PROVIDER: "gitlab",
            OPT_MIRROR_SERVER: "https://git.example.com/",
            OPT_MIRROR_REPO: "home/ha/mirror",
            OPT_MIRROR_TOKEN: "glpat-test",
        },
    )
    entry.add_to_hass(hass)
    return entry


def _patched(session):
    return patch(
        "custom_components.ha_dev_tools.mirror.async_get_clientsession",
        return_value=session,
    )


async def test_mirror_write_creates_then_updates(hass: HomeAssistant, gitlab_entry):
    session = GitLabSession(
        [
            _GitLabResponse(200, {"default_branch": "main"}),
            _GitLabResponse(404),  # no file yet
            _GitLabResponse(201),  # POST create
            _committed("c1"),
        ]
    )
    with _patched(session) as get_session:
        result = await mirror.mirror_write(
            hass, path="automations.yaml", content_before=None, content_after="a: 1\n"
        )
    assert result.mirrored is True and result.commits == ("after",)
    assert get_session.call_args.kwargs == {"verify_ssl": True}
    (m0, u0, k0), (m1, u1, k1), (m2, u2, k2), (m3, u3, _) = session.calls
    assert (m0, u0) == ("GET", BASE)
    assert k0["headers"] == {"PRIVATE-TOKEN": "glpat-test"}
    assert (m1, u1, k1["params"]) == ("GET", FILE, {"ref": "main"})
    assert (m2, u2) == ("POST", FILE)
    assert k2["json"] == {
        "branch": "main",
        "commit_message": "Mirror: automations.yaml after write",
        "encoding": "base64",
        "content": _b64("a: 1\n"),
    }
    assert (m3, u3) == ("HEAD", FILE)

    # Drifted since the last mirror: a before commit, then the after commit,
    # each passing the version the previous one left.
    session = GitLabSession(
        [
            _GitLabResponse(200, {"default_branch": "main"}),
            _file("a: 0\n", "c1"),
            _GitLabResponse(200),  # PUT before
            _committed("c2"),
            _GitLabResponse(200),  # PUT after
            _committed("c3"),
        ]
    )
    with _patched(session):
        result = await mirror.mirror_write(
            hass,
            path="automations.yaml",
            content_before="a: 1\n",
            content_after="a: 2\n",
        )
    assert result.commits == ("before", "after")
    puts = [kwargs["json"] for method, _, kwargs in session.calls if method == "PUT"]
    assert [put["last_commit_id"] for put in puts] == ["c1", "c2"]


async def test_dry_run_recreates_the_proposed_branch(hass: HomeAssistant, gitlab_entry):
    branch = f"{BASE}/repository/branches"
    session = GitLabSession(
        [
            _GitLabResponse(200, {"default_branch": "main"}),
            _file("a: 1\n", "c1"),  # in sync: no before commit
            _GitLabResponse(200, {"commit": {"id": "main-sha"}}),
            _GitLabResponse(200, {"commit": {"id": "old-sha"}}),  # proposed exists
            _GitLabResponse(204),  # DELETE it
            _GitLabResponse(201),  # POST it again at main-sha
            _file("a: 1\n", "c1"),
            _GitLabResponse(200),  # PUT proposed
            _committed("c9"),
        ]
    )
    with _patched(session):
        result = await mirror.mirror_dry_run(
            hass,
            path="automations.yaml",
            content_before="a: 1\n",
            content_after="a: 9\n",
            kind="automation",
            entity_id="lights on",
        )
    assert result.mirrored is True
    assert result.branch == "proposed/automation-lights-on"
    assert result.commits == ("proposed",)
    methods = [(method, url) for method, url, _ in session.calls]
    assert methods[3] == ("GET", f"{branch}/proposed%2Fautomation-lights-on")
    assert methods[4] == ("DELETE", f"{branch}/proposed%2Fautomation-lights-on")
    assert methods[5] == ("POST", branch)
    assert session.calls[5][2]["params"] == {
        "branch": "proposed/automation-lights-on",
        "ref": "main-sha",
    }

    # A new proposed branch: created, nothing to delete.
    session = GitLabSession(
        [
            _GitLabResponse(200, {"default_branch": "main"}),
            _GitLabResponse(404),
            _GitLabResponse(200, {"commit": {"id": "main-sha"}}),
            _GitLabResponse(404),
            _GitLabResponse(201),
            _GitLabResponse(404),
            _GitLabResponse(201),  # POST the file
            _committed("c1"),
        ]
    )
    with _patched(session):
        result = await mirror.mirror_dry_run(
            hass,
            path="automations.yaml",
            content_before=None,
            content_after="a: 9\n",
            kind="automation",
            entity_id="x",
        )
    assert result.mirrored is True
    assert "DELETE" not in [method for method, _, _ in session.calls]


async def test_empty_project_and_read(hass: HomeAssistant, gitlab_entry):
    session = GitLabSession([_GitLabResponse(200, {"default_branch": None})])
    with _patched(session):
        result = await mirror.mirror_write(
            hass, path="automations.yaml", content_before=None, content_after="a\n"
        )
    assert result.mirrored is False
    assert "no default branch yet" in result.reason

    session = GitLabSession(
        [_GitLabResponse(200, {"default_branch": "main"}), _file("a: 1\n", "c1")]
    )
    with _patched(session):
        assert await mirror.read_mirrored(hass, "automations.yaml") == "a: 1\n"


async def test_certificate_switch(hass: HomeAssistant, gitlab_entry):
    hass.config_entries.async_update_entry(
        gitlab_entry, options={**gitlab_entry.options, OPT_MIRROR_VERIFY_SSL: False}
    )
    session = GitLabSession([_GitLabResponse(200, {"default_branch": "main"})])
    with _patched(session) as get_session:
        await mirror._get_default_branch(hass)
    assert get_session.call_args.kwargs == {"verify_ssl": False}

    issues = ir.async_get(hass)
    mirror.refresh_tls_issue(hass, gitlab_entry.options)
    issue = issues.async_get_issue(DOMAIN, mirror.TLS_ISSUE_ID)
    assert issue.translation_placeholders == {"server": "https://git.example.com/"}

    mirror.refresh_tls_issue(hass, {**gitlab_entry.options, OPT_MIRROR_ENABLED: False})
    assert issues.async_get_issue(DOMAIN, mirror.TLS_ISSUE_ID) is None
    # The switch is GitLab's only - GitHub is always verified.
    github = {**gitlab_entry.options, OPT_MIRROR_PROVIDER: "github"}
    assert mirror.tls_unverified(github) is False


def test_paths_are_one_segment_and_checked():
    assert mirror._url_id("packages/a b.yaml") == "packages%2Fa%20b.yaml"
    with pytest.raises(ValueError):
        mirror._url_id("packages/../secrets.yaml")
