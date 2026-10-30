"""Tests for the config flow (config_flow.py) and options flow (options_flow.py)."""

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_dev_tools.const import (
    DOMAIN,
    OPT_DRY_RUN,
    OPT_MIRROR_ENABLED,
    OPT_MIRROR_REPO,
    OPT_MIRROR_TOKEN,
)


@pytest.mark.asyncio
async def test_user_flow_shows_form(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


@pytest.mark.asyncio
async def test_user_flow_creates_entry(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "HA Dev Tools MCP"
    assert result["data"] == {}


@pytest.mark.asyncio
async def test_user_flow_aborts_when_already_configured(hass: HomeAssistant):
    MockConfigEntry(domain=DOMAIN).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


@pytest.mark.asyncio
async def test_options_flow_shows_form_with_current_value_as_default(
    hass: HomeAssistant,
):
    entry = MockConfigEntry(domain=DOMAIN, options={OPT_DRY_RUN: True})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["data_schema"]({})[OPT_DRY_RUN] is True


@pytest.mark.asyncio
async def test_options_flow_defaults_to_false_when_unset(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["data_schema"]({})[OPT_DRY_RUN] is False


@pytest.mark.asyncio
async def test_options_flow_enables_dry_run(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {OPT_DRY_RUN: True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_DRY_RUN] is True


@pytest.mark.asyncio
async def test_options_flow_mirror_fields_default_unset(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    defaults = result["data_schema"]({})
    assert defaults[OPT_MIRROR_ENABLED] is False
    assert defaults[OPT_MIRROR_REPO] == ""
    assert OPT_MIRROR_TOKEN not in defaults


@pytest.mark.asyncio
async def test_options_flow_never_sends_the_stored_token(hass: HomeAssistant):
    """A form's defaults go to the browser; the token used to be one."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_stored_secret",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    # Every default the form carries - what the frontend is sent.
    defaults = result["data_schema"]({})
    assert "ghp_stored_secret" not in defaults.values()
    assert defaults[OPT_MIRROR_REPO] == "alexlenk/ha-mirror"

    # Left empty, the stored token stays.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            OPT_DRY_RUN: False,
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_MIRROR_TOKEN] == "ghp_stored_secret"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repo", ["not a repo", "owner/repo/extra", "../../user", "owner/"]
)
async def test_options_flow_refuses_a_malformed_repo(hass: HomeAssistant, repo):
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {OPT_DRY_RUN: False, OPT_MIRROR_ENABLED: True, OPT_MIRROR_REPO: repo},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {OPT_MIRROR_REPO: "invalid_repo"}


@pytest.mark.asyncio
async def test_options_flow_configures_mirroring(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            OPT_DRY_RUN: False,
            OPT_MIRROR_ENABLED: True,
            OPT_MIRROR_REPO: "alexlenk/ha-mirror",
            OPT_MIRROR_TOKEN: "ghp_example",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_MIRROR_ENABLED] is True
    assert entry.options[OPT_MIRROR_REPO] == "alexlenk/ha-mirror"
    assert entry.options[OPT_MIRROR_TOKEN] == "ghp_example"


@pytest.mark.asyncio
async def test_options_flow_arm_limits(hass: HomeAssistant):
    from custom_components.ha_dev_tools.const import (
        OPT_ARM_IDLE_MINUTES,
        OPT_ARM_MAX_MINUTES,
    )

    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = result["data_schema"]({})
    assert (defaults[OPT_ARM_IDLE_MINUTES], defaults[OPT_ARM_MAX_MINUTES]) == (30, 240)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {OPT_ARM_IDLE_MINUTES: 90, OPT_ARM_MAX_MINUTES: 60}
    )
    assert result["errors"] == {OPT_ARM_IDLE_MINUTES: "idle_above_cap"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {OPT_ARM_IDLE_MINUTES: 45.0, OPT_ARM_MAX_MINUTES: 480.0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_ARM_IDLE_MINUTES] == 45
    assert entry.options[OPT_ARM_MAX_MINUTES] == 480


@pytest.mark.asyncio
async def test_options_flow_control_exclusions(hass: HomeAssistant):
    from custom_components.ha_dev_tools.const import (
        OPT_CONTROL_EXCLUDED_DEVICES,
        OPT_CONTROL_EXCLUDED_ENTITIES,
    )

    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = result["data_schema"]({})
    assert defaults[OPT_CONTROL_EXCLUDED_DEVICES] == []
    assert defaults[OPT_CONTROL_EXCLUDED_ENTITIES] == []

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            OPT_CONTROL_EXCLUDED_DEVICES: ["abc123"],
            OPT_CONTROL_EXCLUDED_ENTITIES: ["cover.garage_door"],
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_CONTROL_EXCLUDED_DEVICES] == ["abc123"]
    assert entry.options[OPT_CONTROL_EXCLUDED_ENTITIES] == ["cover.garage_door"]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["data_schema"]({})[OPT_CONTROL_EXCLUDED_ENTITIES] == [
        "cover.garage_door"
    ]


@pytest.mark.asyncio
async def test_options_flow_gitlab_mirror(hass: HomeAssistant):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.ha_dev_tools.const import (
        OPT_MIRROR_PROVIDER,
        OPT_MIRROR_SERVER,
        OPT_MIRROR_VERIFY_SSL,
    )
    from custom_components.ha_dev_tools.mirror import TLS_ISSUE_ID

    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = result["data_schema"]({})
    assert defaults[OPT_MIRROR_PROVIDER] == "github"
    assert defaults[OPT_MIRROR_SERVER] == "https://gitlab.com"
    assert defaults[OPT_MIRROR_VERIFY_SSL] is True

    gitlab = {
        OPT_MIRROR_ENABLED: True,
        OPT_MIRROR_PROVIDER: "gitlab",
        OPT_MIRROR_TOKEN: "glpat-x",
    }
    for repo, server, error in (
        (
            "home/ha/mirror",
            "http://git.example.com",
            {OPT_MIRROR_SERVER: "invalid_server"},
        ),
        ("mirror", "https://git.example.com", {OPT_MIRROR_REPO: "invalid_project"}),
        ("../x/y", "https://git.example.com", {OPT_MIRROR_REPO: "invalid_project"}),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {**gitlab, OPT_MIRROR_REPO: repo, OPT_MIRROR_SERVER: server},
        )
        assert result["errors"] == error

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **gitlab,
            OPT_MIRROR_REPO: "home/ha/mirror",
            OPT_MIRROR_SERVER: " https://git.example.com:8443/gitlab/ ",
            OPT_MIRROR_VERIFY_SSL: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_MIRROR_SERVER] == "https://git.example.com:8443/gitlab"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, TLS_ISSUE_ID)
    assert issue is not None

    # Certificate checks back on: the issue goes.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {**dict(entry.options), OPT_MIRROR_VERIFY_SSL: True}
    )
    assert ir.async_get(hass).async_get_issue(DOMAIN, TLS_ISSUE_ID) is None
