"""Tests for mirror.py's GitHub Contents API client and mirror_write() orchestration.

Uses aioresponses to intercept the real aiohttp.ClientSession HA's own
async_get_clientsession(hass) returns - see docs/AUTOMATION_TESTING_DESIGN.md's
"Mirroring" section for the design this implements.
"""

import base64

import pytest
from aioresponses import aioresponses
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
CONTENTS_URL = f"https://api.github.com/repos/{REPO}/contents/automations.yaml"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


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
    result = await mirror.mirror_write(
        hass,
        path="automations.yaml",
        content_before=None,
        content_after="- id: a\n  password: hunter2\n",
    )

    assert result.mirrored is False
    assert "password" in result.reason
    assert "secrets.yaml" in result.reason


@pytest.mark.asyncio
async def test_mirror_write_skips_when_before_content_has_credential(
    hass: HomeAssistant, mirror_entry
):
    result = await mirror.mirror_write(
        hass,
        path="automations.yaml",
        content_before="- id: a\n  token: literal\n",
        content_after="- id: a\n  alias: clean\n",
    )

    assert result.mirrored is False
    assert "token" in result.reason


@pytest.mark.asyncio
async def test_mirror_write_new_file_pushes_after_commit_only(
    hass: HomeAssistant, mirror_entry
):
    with aioresponses() as mocked:
        mocked.get(
            CONTENTS_URL + "?ref=main",
            status=404,
        )
        mocked.put(
            CONTENTS_URL,
            status=201,
            payload={"content": {"sha": "new-sha"}},
        )

        result = await mirror.mirror_write(
            hass,
            path="automations.yaml",
            content_before=None,
            content_after="- id: a\n  alias: clean\n",
        )

    assert result.mirrored is True
    assert result.commits == ("after",)


@pytest.mark.asyncio
async def test_mirror_write_pushes_before_commit_on_drift_then_after(
    hass: HomeAssistant, mirror_entry
):
    """Live content differs from what the mirror last recorded - both commits happen."""
    recorded_content = "- id: a\n  alias: recorded\n"
    live_before = "- id: a\n  alias: drifted\n"
    live_after = "- id: a\n  alias: new\n"

    with aioresponses() as mocked:
        mocked.get(
            CONTENTS_URL + "?ref=main",
            status=200,
            payload={"content": _b64(recorded_content), "sha": "old-sha"},
        )
        mocked.put(
            CONTENTS_URL,
            status=200,
            payload={"content": {"sha": "before-sha"}},
        )
        mocked.put(
            CONTENTS_URL,
            status=200,
            payload={"content": {"sha": "after-sha"}},
        )

        result = await mirror.mirror_write(
            hass,
            path="automations.yaml",
            content_before=live_before,
            content_after=live_after,
        )

    assert result.mirrored is True
    assert result.commits == ("before", "after")


@pytest.mark.asyncio
async def test_mirror_write_before_commit_is_noop_when_unchanged(
    hass: HomeAssistant, mirror_entry
):
    """Mirror already has exactly the pre-write content - only the after-commit pushes."""
    same_content = "- id: a\n  alias: same\n"

    with aioresponses() as mocked:
        mocked.get(
            CONTENTS_URL + "?ref=main",
            status=200,
            payload={"content": _b64(same_content), "sha": "sha-1"},
        )
        mocked.put(
            CONTENTS_URL,
            status=200,
            payload={"content": {"sha": "sha-2"}},
        )

        result = await mirror.mirror_write(
            hass,
            path="automations.yaml",
            content_before=same_content,
            content_after="- id: a\n  alias: changed\n",
        )

    assert result.mirrored is True
    assert result.commits == ("after",)


@pytest.mark.asyncio
async def test_mirror_write_noop_when_after_already_matches(
    hass: HomeAssistant, mirror_entry
):
    """Nothing to push at all - after-content is already what the mirror has."""
    content = "- id: a\n  alias: same\n"

    with aioresponses() as mocked:
        mocked.get(
            CONTENTS_URL + "?ref=main",
            status=200,
            payload={"content": _b64(content), "sha": "sha-1"},
        )

        result = await mirror.mirror_write(
            hass, path="automations.yaml", content_before=None, content_after=content
        )

    assert result.mirrored is True
    assert result.commits == ()


@pytest.mark.asyncio
async def test_mirror_write_reports_failure_without_raising(
    hass: HomeAssistant, mirror_entry
):
    """A GitHub API failure never propagates - it comes back as a normal result."""
    with aioresponses() as mocked:
        mocked.get(CONTENTS_URL + "?ref=main", status=500)

        result = await mirror.mirror_write(
            hass,
            path="automations.yaml",
            content_before=None,
            content_after="- id: a\n  alias: x\n",
        )

    assert result.mirrored is False
    assert "mirror push failed" in result.reason
