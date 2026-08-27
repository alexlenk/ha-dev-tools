<img src="custom_components/ha_dev_tools/brand/icon.png" width="72" align="left" alt="HA Dev Tools logo">

# HA Dev Tools

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)
[![hacs][hacsbadge]][hacs]

A Home Assistant custom integration that gives an AI coding assistant direct,
tool-gated access to develop and maintain your Home Assistant instance:
authoring and validating automations, reading logs, managing helpers and
dashboards, auditing for common reliability bugs. It works by registering
tools into Home Assistant's own [LLM tool
registry](https://developers.home-assistant.io/docs/core/llm/) and letting
Home Assistant's native `mcp_server` integration serve them over
[MCP](https://modelcontextprotocol.io/) - there's no separate MCP server
process to install or run, and no custom transport or auth code in this
repository.

## Requirements

- Home Assistant **2026.8.2 or newer** - the release that shipped the native
  `mcp_server` integration this depends on. That release itself requires
  Python 3.14.2+.
- The `mcp_server` integration, configured to expose the `dev_tools` API (see
  Setup below).

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

## Setup

1. **Settings → Devices & Services → Add Integration → HA Dev Tools.** This
   registers the `dev_tools` LLM API; it does nothing reachable on its own
   yet.
2. **Settings → Devices & Services → Add Integration → Model Context Protocol
   Server** (Home Assistant's own built-in integration, not part of this
   repo). In its setup, add `dev_tools` to the exposed APIs.
3. **Arm it.** Every tool except a diagnostic ping refuses to run until you
   prove real filesystem access - the same kind SSH or the Terminal add-on
   already requires - by creating a file:

   ```bash
   date +%s > /config/.storage/ha_dev_tools.armed
   ```

   This enables `dev_tools` for up to 4 hours, extended by 30 minutes each
   time a tool is actually used, and expires on its own if idle for 30
   minutes. See [docs/SECURITY.md](docs/SECURITY.md) for why this exists and
   exactly how it works - it's the single most important thing to understand
   before pointing an MCP client at this integration.
4. **Connect an MCP client** to `https://<your-ha-instance>/api/mcp/dev_tools`,
   authenticated with a normal Home Assistant admin long-lived access token as
   a Bearer token - see [Connecting an MCP client](#connecting-an-mcp-client)
   below for exactly how to do that in Claude Code and Claude Desktop.
5. **Optional: turn on dry-run mode.** From this integration's card in
   Settings → Devices & Services, click **Configure** and enable dry-run.
   Every write tool (`write_automation`, `create_helper`/`update_helper`/
   `delete_helper`, `create_derived_sensor`/`update_derived_sensor`/
   `delete_derived_sensor`, `create_template_entity`/`update_template_entity`/
   `delete_template_entity`, `write_dashboard`) then returns the exact input
   it would have applied instead of actually applying it, so an agent's
   proposed changes can be reviewed before you turn dry-run back off. Takes
   effect immediately, no restart needed. Read-only tools and
   `reload_domain`/`reload_derived_sensor`/`check_config` are unaffected
   either way.

## Connecting an MCP client

This integration is a **remote** MCP server - Home Assistant's own `mcp_server`
integration serves it over Streamable HTTP at
`https://<your-ha-instance>/api/mcp/dev_tools`, authenticated with a Bearer
token (a normal Home Assistant admin long-lived access token, created under
your HA profile's **Security** tab). It is not a local/stdio server, so it
doesn't go through `claude_desktop_config.json`-style local server setup, and
it doesn't use OAuth - which changes what "add an MCP server" means in each
client:

- **Claude Code** (CLI) supports remote HTTP servers with custom headers
  directly, so a Bearer token works as-is:

  ```bash
  claude mcp add --transport http ha-dev-tools \
    https://<your-ha-instance>/api/mcp/dev_tools \
    --header "Authorization: Bearer <your-long-lived-access-token>" \
    --scope user
  ```

  `--scope user` makes it available in every project rather than just the
  current one. Run `claude mcp list` afterward to confirm it shows as
  connected.

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
| `get_automation` | Layout-aware read: resolves whether an automation lives in `automations.yaml` or a `packages/*.yaml` file |
| `write_automation` | Layout-aware, package-safe write - never silently duplicates a package-defined automation, always reloads afterward |
| `check_config` | Home Assistant's own full config validation |
| `reload_domain` | Reload a domain's config (e.g. `automation`) without restarting |

**Configure**
| Tool | What it does |
|---|---|
| `list_helpers` / `create_helper` / `update_helper` / `delete_helper` | CRUD for storage-defined helpers (`input_boolean`, `counter`, `timer`, ...) |
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

**Audit**
| Tool | What it does |
|---|---|
| `entity_health_report` | Per-integration counts of disabled/hidden/unavailable/missing entities, scannable instead of a wall of text |
| `audit_automations` | Flags duplicate automation IDs across packages and references to currently-unavailable entities |

`dev_tools_ping` also exists as a zero-dependency smoke test for the
integration/MCP wiring itself - it's the one tool that isn't gated (see
Security).

## Security

Every tool above requires two things: proof of recent out-of-band filesystem
access, and a genuine Home Assistant admin account. Neither is optional, and
neither is enforced by Home Assistant on our behalf - both live in this
integration's own code, on purpose. **Read
[docs/SECURITY.md](docs/SECURITY.md)** before exposing this to anything
beyond your own local network - it explains the actual threat this design
defends against and why a simpler "just require admin" gate isn't enough on
its own.

File access from `write_automation` and other file-touching tools is
additionally bounded by a path allowlist/denylist - not currently
customizable, defaults only (see [docs/SECURITY.md](docs/SECURITY.md#path-allowlist-file-touching-tools)
for exactly what's allowed).

## Architecture

For how this is built - why tools instead of a REST API, how automation
package-safety works, what Home Assistant's storage layer will and won't let
a custom integration do safely - see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT - see [LICENSE](LICENSE).

[releases-shield]: https://img.shields.io/github/release/alexlenk/ha-dev-tools.svg?style=for-the-badge
[releases]: https://github.com/alexlenk/ha-dev-tools/releases
[commits-shield]: https://img.shields.io/github/commit-activity/y/alexlenk/ha-dev-tools.svg?style=for-the-badge
[commits]: https://github.com/alexlenk/ha-dev-tools/commits/main
[license-shield]: https://img.shields.io/github/license/alexlenk/ha-dev-tools.svg?style=for-the-badge
[hacs]: https://github.com/hacs/integration
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
