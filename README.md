<img src="https://raw.githubusercontent.com/alexlenk/ha-dev-tools/main/custom_components/ha_dev_tools/brand/icon.png" width="72" align="left" alt="HA Dev Tools logo">

# HA Dev Tools

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![codecov][codecov-shield]][codecov]
[![License][license-shield]](LICENSE)
[![hacs][hacsbadge]][hacs]

**Give your AI coding assistant hands-on access to your Home Assistant
instance** - the same way it already helps you with code, but for
automations, entities, dashboards, and helpers.

It runs inside Home Assistant's own [`mcp_server`
integration](https://developers.home-assistant.io/docs/core/llm/), so there's
no separate server to install, host, or keep running - just a Bearer token
and an MCP client. And because handing write access to an AI assistant is a
big ask, every write is proposed and previewed before anything happens, an
optional dry-run mode can keep it that way permanently, and optional git
mirroring gives you a private, before/after history to roll back from by
hand. See [The safety model](#the-safety-model) below for exactly what that
does and doesn't guarantee - it's worth reading before you turn this loose on
a live instance.

### What you can ask it to do

- *"Add an automation that turns on the hallway light when the hallway
  motion sensor trips after sunset."*
- *"Find every automation that references an entity that's now
  unavailable."*
- *"The porch light didn't turn on last night - check the logs and logbook
  and tell me why."*
- *"Clean up the 40 leftover entities from the Zigbee device I just
  removed."*

> **⚠️ Use at your own risk.** This is a community project, built and
> maintained in spare time - no SLA, no guaranteed support, and no guarantee
> that dry-run, the confirm step, or git mirroring catch every mistake
> before it reaches your live instance. AI can be wrong, and this is all
> still a work in progress. Provided as-is, with no warranty - see
> [LICENSE](LICENSE).

## Quick start

Needs Home Assistant **2026.8.2+** (see [Requirements](#requirements) below
for the full list, including client-specific versions).

1. Install via HACS or manually - see [Installation](#installation) below.
2. **Settings → Devices & Services → Add Integration → HA Dev Tools.**
3. **Settings → Devices & Services → Add Integration → Model Context
   Protocol Server** (Home Assistant's own built-in integration) - in its
   setup, add `dev_tools` to the exposed APIs. This step is easy to miss and
   is the single most common way to end up stuck: skip it and HA Dev Tools'
   card still shows healthy, but an MCP client just gets a bare 404 (a
   **Settings → System → Repairs** issue will flag it if you do).
4. **Arm it.** Every tool except a diagnostic ping refuses to run until you
   prove real filesystem access - the same kind SSH or the Terminal add-on
   already requires:

   ```bash
   date +%s > /config/.storage/ha_dev_tools.armed
   ```

   This enables `dev_tools` for up to 4 hours (extended by 30 minutes each
   time a tool is used, and it expires on its own after 30 minutes idle).
   Since it expires, a shell alias saves retyping it:

   ```bash
   echo 'alias dev-tools-arm="date +%s > /config/.storage/ha_dev_tools.armed && echo armed"' >> ~/.bashrc && source ~/.bashrc
   ```

5. **Connect your MCP client** to
   `https://<your-ha-instance>/api/mcp/dev_tools`, authenticated with a
   normal Home Assistant admin long-lived access token as a Bearer token.
   For Claude Code:

   ```bash
   claude mcp add --transport http ha-dev-tools \
     https://<your-ha-instance>/api/mcp/dev_tools \
     --header "Authorization: Bearer <your-long-lived-access-token>" \
     --scope user
   ```

   See [Connecting an MCP client](#connecting-an-mcp-client) below for other
   clients, and why Claude Desktop/claude.ai generally can't be pointed at a
   home LAN instance directly.

That's it - ask it something.

## The safety model

Every write tool (`write_automation`, `delete_automation`, `write_script`,
the helper/derived-sensor/template-entity CRUD tools, `delete_entity`/
`delete_entities`, `write_dashboard`) always requires two calls: **propose**
(no arguments changed, no side effects - returns a preview of what would be
applied plus a short-lived `confirm_token`) and **confirm** (the identical
call, plus that token - only this one can actually do anything). That's
always on and can't be turned off. Two optional layers sit on top of it.
Here's exactly what each one actually guarantees:

| Layer | Guarantee | What it's for |
|---|---|---|
| **Confirm step** (always on) | UX friction, not an enforced human check - nothing stops an agent from calling propose then confirm back to back with no one actually reading the preview in between | Catches accidental first-call writes and stops a token from silently being reused for a different call |
| **Dry-run mode** (optional) | Hard guarantee - a confirmed write's content is resolved but never reaches disk while it's on | Lets you review an agent's proposed changes for as long as you want, with zero risk of any of them going live |
| **Git mirroring** (optional) | Audit trail only - it never gates a write, live or dry-run | A private, before/after history of every change, so you have something to manually restore from (copy the old YAML back) if something goes wrong |

In short: **dry-run is the actual safety net.** The confirm step and git
mirroring make mistakes easier to catch and easier to undo, but neither one
*stops* a bad write from happening. If you want a real guarantee that
nothing touches your live config while you're still trusting an agent,
turn dry-run on and read what it proposes.

See [Setup](#setup---optional-hardening) below for how to enable each, and
[docs/SECURITY.md](docs/SECURITY.md) for the full threat model - including
what these gates do *not* protect against (this integration isn't
sandboxed; a confirmed, non-dry-run write has the same power as your admin
account, full stop).

## Requirements

- Home Assistant **2026.8.2 or newer** - the release that shipped the native
  `mcp_server` integration this depends on. That release itself requires
  Python 3.14.2+.
- The `mcp_server` integration, configured to expose the `dev_tools` API (see
  Quick start above).
- If connecting with Claude Code, **Claude Code 2.1 or newer** - it's the
  release line that correctly prioritizes a configured Bearer token over
  `mcp_server`'s OAuth discovery metadata. Run `claude update` if unsure.

## Installation

### HACS (recommended)

1. Open HACS → Integrations → the three-dot menu → **Custom repositories**.
2. Add `https://github.com/alexlenk/ha-dev-tools`, category **Integration**.
3. Find **HA Dev Tools** in the integration list and download it.
4. Restart Home Assistant.

### Manual

1. Download the latest release.
2. Copy the `ha_dev_tools` folder into `<config>/custom_components/`.
3. Restart Home Assistant.

## Connecting an MCP client

This integration is a **remote** MCP server - Home Assistant's own `mcp_server`
integration serves it over Streamable HTTP at
`https://<your-ha-instance>/api/mcp/dev_tools`, authenticated with a Bearer
token (a normal Home Assistant admin long-lived access token, created under
your HA profile's **Security** tab). It is not a local/stdio server, so it
doesn't go through `claude_desktop_config.json`-style local server setup - and
the token, not OAuth, is the auth mechanism actually meant to be used here,
even though `mcp_server` also *advertises* OAuth discovery metadata (RFC 9728)
alongside it, for web-based clients that require it. That advertisement
matters for setup, covered below.

- **Claude Code** (CLI, 2.1+ - see Requirements) supports remote HTTP
  servers with custom headers directly, so a Bearer token works as-is (see
  the command in Quick start above). Run `claude mcp list` afterward to
  confirm it shows as connected.

- **Claude Desktop and claude.ai** connect to remote servers only through
  **Settings → Connectors → Add custom connector**, which has two problems
  for a typical home HA setup, not just one:
  - That flow is built around OAuth - there's no field in it for a static
    Bearer token or API key for an individual account. (An org-admin
    `static_headers` connector option exists on some plans and can carry a
    fixed `Authorization` header instead, but that's not available to a
    regular Pro account.)
  - Separately, the connector doesn't connect from your Desktop app's own
    network the way a local/stdio server would - it's opened from Anthropic's
    cloud infrastructure. So the URL also has to be reachable on the public
    internet over HTTPS with a valid (non-self-signed) certificate. A plain
    LAN address or `homeassistant.local` won't work even with auth solved;
    it would need to already be exposed publicly, e.g. via Nabu Casa's
    remote UI or your own reverse proxy with a real certificate.

  Between those two, Claude Desktop generally can't be pointed at this
  integration directly - use Claude Code instead.

- Any other MCP client that supports Streamable HTTP with custom request
  headers (not just OAuth) can connect the same way as Claude Code: point it
  at the URL above and set an `Authorization: Bearer <token>` header.

## Tools

**Author & iterate**
| Tool | What it does |
|---|---|
| `find_entities` | Area/domain/name-scoped entity lookup - avoids dumping hundreds of entities |
| `render_template` | Render a Jinja2 template against live state, never raising on error |
| `validate_template` | Check template syntax and flag referenced entities that don't exist |
| `get_automation` | Layout-aware read: resolves whether an automation lives in `automations.yaml` or a `packages/*.yaml` file, and reports whether it's currently enabled - that's runtime-only state the YAML itself never shows |
| `write_automation` | Layout-aware, package-safe write - never silently duplicates a package-defined automation, always reloads afterward |
| `delete_automation` | Layout-aware, package-safe delete - resolves which file actually defines it first, refuses to guess if the id isn't found or is defined in more than one file |
| `list_scripts` / `get_script` / `write_script` | Same layout-aware, package-safe pattern as `get_automation`/`write_automation`, for `script:` - resolves whether a script lives in `scripts.yaml` or a `packages/*.yaml` file, and writes through the correct one |
| `check_config` | Home Assistant's own full config validation |
| `reload_domain` | Reload a domain's config (e.g. `automation`) without restarting |

**Configure**
| Tool | What it does |
|---|---|
| `list_helpers` / `create_helper` / `update_helper` / `delete_helper` | CRUD for storage-defined helpers (`input_boolean`, `counter`, `timer`, ...) |
| `delete_entity` | Soft-delete an entity from the entity registry - Home Assistant reconnects it automatically if the same integration re-registers it later; only truly orphaned entries are purged for good, after 30 days. That 30-day window is Home Assistant's own behavior, not something this tool adds - a backup past it only happens if git mirroring (below) is also turned on |
| `delete_entities` | Same as `delete_entity`, for a list of entity_ids in one propose/confirm pair - for bulk cleanup, avoids one round trip (and one mirror commit pair) per entity |
| `list_derived_sensors` / `get_derived_sensor` / `create_derived_sensor` / `update_derived_sensor` / `delete_derived_sensor` / `reload_derived_sensor` | CRUD for calculated/derived sensor helpers (Min/Max, Utility Meter, Integration [Riemann sum], Statistics, Threshold, Derivative, Filter) plus the general-purpose Template helper (any entity domain - light, switch, sensor, ...) - a second helper family implemented as config entries rather than storage items; create/update discover each step's fields interactively since some of these flows are multi-step or menu-driven (Template's first step picks which entity domain to create) |
| `list_template_entities` / `get_template_entity` / `create_template_entity` / `update_template_entity` / `delete_template_entity` | Layout-aware, package-safe CRUD for YAML `template:` entities (sensor, binary_sensor, number, switch, ...) - resolves whether an entity lives in `configuration.yaml` or a `packages/*.yaml` file, same pattern as `get_automation`/`write_automation`. New entities always go into an existing package (`configuration.yaml` itself is read-only here); every write requires the entity to have its own `unique_id`. For the config-entry Template *helper* instead, see the row above |
| `get_dashboard` / `write_dashboard` | Read/write a Lovelace dashboard (storage mode; YAML-mode dashboards are read-only here, matching HA's own restriction) |

**Diagnose**
| Tool | What it does |
|---|---|
| `get_logs` | Tail/filter/search the core Home Assistant log |
| `get_entity_history` | Recorder-backed state history for one or more entities over a time range |
| `get_logbook` | Recorder-backed, humanized logbook entries (automations/scripts triggering, notable state changes) over a time range |
| `list_addons` / `get_addon_logs` | Supervisor add-on info and logs (Home Assistant OS/Supervised only) |
| `list_mqtt_topics` | Read-only, time-bounded snapshot of a topic filter - the only way to discover a retained MQTT message's existence, since MQTT itself has no "list retained" query. Useful for tracing a "ghost" entity (live state, no registry entry - `delete_entity` can't touch these) back to the topic keeping it alive. Never publishes anything; requires the `mqtt` integration to be configured |

**Audit**
| Tool | What it does |
|---|---|
| `entity_health_report` | Per-integration counts of disabled/hidden/unavailable/missing entities, scannable instead of a wall of text |
| `audit_automations` | Flags duplicate automation IDs across packages and references to currently-unavailable entities (tagged with whether that automation is actually enabled), plus a list of currently-disabled automations |

`dev_tools_ping` also exists as a zero-dependency smoke test for the
integration/MCP wiring itself - it's the one tool that isn't gated (see
Security).

## Setup - optional hardening

Both of these are optional and off by default. Neither requires a restart to
take effect.

**Dry-run mode:** from this integration's card in Settings → Devices &
Services, click **Configure** and enable dry-run. Every write tool's
confirm step still returns its preview, but the underlying write never
actually happens - see [The safety model](#the-safety-model) above for what
that does and doesn't guarantee.

**Git mirroring:** from the same **Configure** dialog, enable it, set
**Mirror repository** to a dedicated private GitHub repo (`owner/repo` -
separate from your HA config repo, and never the repo any deploy mechanism
pulls from), and paste a **Mirror repository access token**. To create the
token: GitHub → your avatar → **Settings → Developer settings → Personal
access tokens → Fine-grained tokens → Generate new token** → restrict
**Repository access** to that one repo → set **Contents** permission to
**Read and write** (nothing else) → set an **Expiration** date → generate
and paste it in.

Every confirmed write then pushes the touched file's before/after content
to that repo's default branch (in dry-run, to its own `proposed/<kind>-<id>`
branch instead, so you can review the diff on GitHub before turning dry-run
off). Content that looks like a literal credential (not routed through
`!secret`) is never pushed. Rollback is manual: find the old version in the
mirror repo's git history and paste it back in - there's no automated
revert. Helper and derived-sensor writes are reconstructed in memory rather
than re-read from disk, to dodge Home Assistant's own storage-save debounce;
`delete_entity`/`delete_entities` also back up the entity's full registry
data first, since HA's own registry-purge safety net only lasts 30 days
either way. Supported today: `write_automation`, `delete_automation`,
`write_script`, the template-entity tools, `write_dashboard` (live mode
only - it has no compute-without-writing path to mirror in dry-run), the
helper tools, the derived-sensor tools, and `delete_entity`/`delete_entities`.

## Security

Every tool above requires two things: proof of recent out-of-band filesystem
access, and a genuine Home Assistant admin account. Neither is optional, and
neither is enforced by Home Assistant on our behalf - both live in this
integration's own code, on purpose. These gates control *when* and *by
whom* a tool can be invoked - they do not sandbox what it's capable of once
invoked, and a confirmed, non-dry-run write has the same power as the admin
account behind it. **Read [docs/SECURITY.md](docs/SECURITY.md)** before
exposing this to anything beyond your own local network - it explains the
actual threat this design defends against, exactly what it doesn't defend
against, and why a simpler "just require admin" gate isn't enough on its
own.

File access from `write_automation` and other file-touching tools is
additionally bounded by a path allowlist/denylist - not currently
customizable, defaults only (see [docs/SECURITY.md](docs/SECURITY.md#path-allowlist-file-touching-tools)
for exactly what's allowed).

## Architecture

For how this is built - why tools instead of a REST API, how automation
package-safety works, what Home Assistant's storage layer will and won't let
a custom integration do safely - see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Git mirroring of writes made through this integration is built - see
[Setup - optional hardening](#setup---optional-hardening) above to turn it
on. Offline automation testing (the two-tier lint/CI concept) is still
design-stage, not built - see
[docs/AUTOMATION_TESTING_DESIGN.md](docs/AUTOMATION_TESTING_DESIGN.md).
[scripts/config-repo-setup/](scripts/config-repo-setup/) has standalone
scripts for bootstrapping the security hygiene (`.gitignore`, secret
scanning, config-validation CI) that design assumes onto your own HA
*config* repo - a different repo from the dedicated mirror repo mirroring
actually pushes to, independent of whether the rest of that design ever
gets built.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT - see [LICENSE](LICENSE).

[releases-shield]: https://img.shields.io/github/release/alexlenk/ha-dev-tools.svg
[releases]: https://github.com/alexlenk/ha-dev-tools/releases
[commits-shield]: https://img.shields.io/github/commit-activity/y/alexlenk/ha-dev-tools.svg
[commits]: https://github.com/alexlenk/ha-dev-tools/commits/main
[codecov-shield]: https://codecov.io/gh/alexlenk/ha-dev-tools/branch/main/graph/badge.svg
[codecov]: https://codecov.io/gh/alexlenk/ha-dev-tools
[license-shield]: https://img.shields.io/github/license/alexlenk/ha-dev-tools.svg
[hacs]: https://github.com/hacs/integration
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg
