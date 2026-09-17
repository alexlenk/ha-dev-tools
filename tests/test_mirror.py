"""Tests for mirror.py's GitHub Contents API client and mirror_write() orchestration.

Uses a small hand-rolled fake aiohttp session rather than aioresponses -
see requirements-test.txt's comment on aioresponses for why: it's
fundamentally incompatible with the exact aiohttp version
homeassistant==2026.8.2 hard-pins, not just a version this repo happens
to have picked.
"""

import base64
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_dev_tools import mirror
from custom_components.ha_dev_tools.const import (
    DOMAIN,
    OPT_MIRROR_ENABLED,
    OPT_MIRROR_REPO,
    OPT_MIRROR_TOKEN,
)

REPO = "alexlenk/ha-mirror"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class _FakeResponse:
    """Stands in for aiohttp.ClientResponse - just enough of its API for mirror.py."""

    def __init__(self, status: int, payload: object = None):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _FakeRequestContext:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    """Stands in for aiohttp.ClientSession - replays queued responses in call order.

    mirror.py's own call sequence within a single mirror_write() is
    deterministic (GET, then 0-2 PUTs), so a simple ordered queue is
    enough - no need to match by URL/method.
    """

    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def _next(self, method: str, url: str, kwargs: dict) -> _FakeRequestContext:
        self.calls.append((method, url, kwargs))
        return _FakeRequestContext(self._responses.pop(0))

    def get(self, url, **kwargs):
        return self._next("GET", url, kwargs)

    def put(self, url, **kwargs):
        return self._next("PUT", url, kwargs)


def _patched(fake_session: FakeSession):
    return patch(
        "custom_components.ha_dev_tools.mirror.async_get_clientsession",
        return_value=fake_session,
    )


@pytest.fixture
async def mirror_entry(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: REPO,
            OPT_MIRROR_TOKEN: "ghp_test",
        },
    )
    entry.add_to_hass(hass)
    return entry


def test_is_mirror_enabled_false_with_no_entry(hass: HomeAssistant):
    assert mirror.is_mirror_enabled(hass) is False


@pytest.mark.asyncio
async def test_is_mirror_enabled_requires_repo_and_token(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={OPT_MIRROR_ENABLED: True})
    entry.add_to_hass(hass)

    assert mirror.is_mirror_enabled(hass) is False


@pytest.mark.asyncio
async def test_is_mirror_enabled_true_when_fully_configured(
    hass: HomeAssistant, mirror_entry
):
    assert mirror.is_mirror_enabled(hass) is True


@pytest.mark.asyncio
async def test_mirror_write_skips_when_after_content_has_credential(
    hass: HomeAssistant, mirror_entry
):
    fake_session = FakeSession([])  # no HTTP call should even be attempted

    with _patched(fake_session):
        result = await mirror.mirror_write(
            hass,
            path="automations.yaml",
            content_before=None,
            content_after="- id: a\n  password: hunter2\n",
        )

    assert result.mirrored is False
    assert "password" in result.reason
    assert "secrets.yaml" in result.reason
    assert fake_session.calls == []


@pytest.mark.asyncio
async def test_mirror_write_skips_when_before_content_has_credential(
    hass: HomeAssistant, mirror_entry
):
    fake_session = FakeSession([])

    with _patched(fake_session):
        result = await mirror.mirror_write(
            hass,
            path="automations.yaml",
            content_before="- id: a\n  token: literal\n",
            content_after="- id: a\n  alias: clean\n",
        )

    assert result.mirrored is False
    assert "token" in result.reason
    assert fake_session.calls == []


@pytest.mark.asyncio
async def test_mirror_write_new_file_pushes_after_commit_only(
    hass: HomeAssistant, mirror_entry
):
    fake_session = FakeSession(
        [
            _FakeResponse(404),  # GET current - doesn't exist yet
            _FakeResponse(201, {"content": {"sha": "new-sha"}}),  # PUT after
        ]
    )

    with _patched(fake_session):
        result = await mirror.mirror_write(
            hass,
            path="automations.yaml",
            content_before=None,
            content_after="- id: a\n  alias: clean\n",
        )

    assert result.mirrored is True
    assert result.commits == ("after",)
    assert [c[0] for c in fake_session.calls] == ["GET", "PUT"]


@pytest.mark.asyncio
async def test_mirror_write_pushes_before_commit_on_drift_then_after(
    hass: HomeAssistant, mirror_entry
):
    """Live content differs from what the mirror last recorded - both commits happen."""
    recorded_content = "- id: a\n  alias: recorded\n"
    live_before = "- id: a\n  alias: drifted\n"
    live_after = "- id: a\n  alias: new\n"

    fake_session = FakeSession(
        [
            _FakeResponse(200, {"content": _b64(recorded_content), "sha": "old-sha"}),
            _FakeResponse(200, {"content": {"sha": "before-sha"}}),
            _FakeResponse(200, {"content": {"sha": "after-sha"}}),
        ]
    )

    with _patched(fake_session):
        result = await mirror.mirror_write(
            hass,
            path="automations.yaml",
            content_before=live_before,
            content_after=live_after,
        )

    assert result.mirrored is True
    assert result.commits == ("before", "after")
    assert [c[0] for c in fake_session.calls] == ["GET", "PUT", "PUT"]


@pytest.mark.asyncio
async def test_mirror_write_before_commit_is_noop_when_unchanged(
    hass: HomeAssistant, mirror_entry
):
    """Mirror already has exactly the pre-write content - only the after-commit pushes."""
    same_content = "- id: a\n  alias: same\n"

    fake_session = FakeSession(
        [
            _FakeResponse(200, {"content": _b64(same_content), "sha": "sha-1"}),
            _FakeResponse(200, {"content": {"sha": "sha-2"}}),
        ]
    )

    with _patched(fake_session):
        result = await mirror.mirror_write(
            hass,
            path="automations.yaml",
            content_before=same_content,
            content_after="- id: a\n  alias: changed\n",
        )

    assert result.mirrored is True
    assert result.commits == ("after",)
    assert [c[0] for c in fake_session.calls] == ["GET", "PUT"]


@pytest.mark.asyncio
async def test_mirror_write_noop_when_after_already_matches(
    hass: HomeAssistant, mirror_entry
):
    """Nothing to push at all - after-content is already what the mirror has."""
    content = "- id: a\n  alias: same\n"

    fake_session = FakeSession(
        [_FakeResponse(200, {"content": _b64(content), "sha": "sha-1"})]
    )

    with _patched(fake_session):
        result = await mirror.mirror_write(
            hass, path="automations.yaml", content_before=None, content_after=content
        )

    assert result.mirrored is True
    assert result.commits == ()
    assert [c[0] for c in fake_session.calls] == ["GET"]


@pytest.mark.asyncio
async def test_mirror_write_reports_failure_without_raising(
    hass: HomeAssistant, mirror_entry
):
    """A GitHub API failure never propagates - it comes back as a normal result."""
    fake_session = FakeSession([_FakeResponse(500)])

    with _patched(fake_session):
        result = await mirror.mirror_write(
            hass,
            path="automations.yaml",
            content_before=None,
            content_after="- id: a\n  alias: x\n",
        )

    assert result.mirrored is False
    assert "mirror push failed" in result.reason


@pytest.mark.asyncio
async def test_mirror_write_json_content_type_uses_storage_scanner(
    hass: HomeAssistant, mirror_entry
):
    """content_type='json' scans with find_storage_credentials (no !secret
    exemption), not find_yaml_credentials - a plain 'password' value in JSON
    content would slip past the YAML scanner's tag check entirely differently,
    so this proves the dispatch actually happens, not just that some scan runs."""
    fake_session = FakeSession([])

    with _patched(fake_session):
        result = await mirror.mirror_write(
            hass,
            path=".storage/input_boolean",
            content_before=None,
            content_after='{"data": {"items": [{"id": "a", "password": "literal"}]}}',
            content_type="json",
        )

    assert result.mirrored is False
    assert "password" in result.reason
    assert fake_session.calls == []


@pytest.mark.asyncio
async def test_mirror_write_json_content_type_pushes_clean_content(
    hass: HomeAssistant, mirror_entry
):
    fake_session = FakeSession(
        [
            _FakeResponse(404),
            _FakeResponse(201, {"content": {"sha": "sha-1"}}),
        ]
    )

    with _patched(fake_session):
        result = await mirror.mirror_write(
            hass,
            path=".storage/lovelace",
            content_before=None,
            content_after='{"data": {"config": {"views": []}}}',
            content_type="json",
        )

    assert result.mirrored is True
    assert result.commits == ("after",)
