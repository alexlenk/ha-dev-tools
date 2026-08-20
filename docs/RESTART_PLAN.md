# Restart: HA-native dev-workflow tools (no separate MCP server)

## Context

Three repos, all stale/unused: `ha-development-power` (Kiro wrapper, no real code — retire), `ha-dev-tools-mcp` (standalone laptop process, dead/duplicated sync-layer code, `get_error_log` returns unbounded raw blobs, no tool annotations — retire), `ha-dev-tools` (this repo — solid `FileManager`/`LogManager`/`SecurityManager`, but its `/api/management/*` REST surface exists only to serve the now-retired standalone MCP process).

Two rounds of pushback from the user reshaped this plan from where it started:

1. **"Why two processes at all?"** → led to questioning whether MCP should be hosted by the integration directly instead of running on a laptop.
2. **"Research how Home Assistant actually stores things before deciding what needs file access vs API access — download the source and look."** This surfaced two things that change the architecture more than anything else in this plan:
   - Home Assistant shipped a **native `mcp_server` integration** (core, since 2026.8.2) that is a pure protocol adapter with zero tool logic of its own — it hosts MCP transport (Streamable HTTP + legacy SSE) as plain `HomeAssistantView`s bridged to the official `mcp` SDK's `Server` over `anyio` memory streams, and exposes whatever is registered in `homeassistant/helpers/llm.py`'s `API`/`Tool` registry. That registry is a **public extension point** — any custom integration can register tools into it (`llm.async_register_api`), exactly like HA's own `mcp` client component does. Once registered, those tools automatically become reachable at `/api/mcp/<api_id>`, with HA's own auth (admin-gated by default for non-Assist APIs — already the security posture we wanted).
   - Reading `helpers/storage.py`, the entity/device/area registries, `components/config/automation.py`, the `automation` component, and `logbook`/`logger`/`system_log` confirmed real, specific gaps where **only raw file access works** — not a general "files vs API" toss-up, a short, concrete list (below).

Net effect: **no separate MCP server, no Add-on, no FastMCP, no custom transport or auth code.** `ha_dev_tools` becomes a custom integration that defines tools and registers them into HA's own LLM tool system; the native `mcp_server` component (which ships in core, zero extra install) does all transport/session/auth work.

## Architecture

- `ha_dev_tools` registers an `llm.API` (e.g. id `dev_tools`) full of `llm.Tool` subclasses in `async_setup_entry`, via `llm.async_register_api(hass, DevToolsAPI(...))`.
- User enables the `dev_tools` API in the native `mcp_server` integration's config flow (alongside or instead of `assist`) → tools become reachable at `/api/mcp/dev_tools` over Streamable HTTP from Claude Code/Claude Desktop/any MCP client, authenticated with a normal HA admin token.
- Runs in-process, in HA's event loop. File I/O via `hass.async_add_executor_job`, same pattern `FileManager`/`LogManager` already use — keep them, re-wire as backing implementations called directly from `Tool.async_call()` instead of from `HomeAssistantView` HTTP handlers. **The entire `/api/management/*` REST layer goes away** — it only ever existed to give the old standalone process something to call over HTTP; in-process tools don't need it.
- Dev loop: tool logic is unit-testable headless via `pytest-homeassistant-custom-component` fixtures — the existing 91-test `SecurityManager` suite already proves this pattern works well here. Only end-to-end protocol verification (a real MCP client hitting `/api/mcp/dev_tools`) needs a live instance.

## File vs. API — settled by reading the actual source, not assumed

| Need | Use | Why (source-grounded) |
|---|---|---|
| Entity/device/area hygiene (staleness, area membership, disabled/hidden) | WebSocket `config/entity_registry/list_for_display`, `config/device_registry/list`, `config/area_registry/list` | Live, richer than raw `.storage/*.json`, no staleness or torn-read risk |
| Registry **writes** (disable/relabel a stale entity) | Registry update services/WS commands only | `.storage/*.json` writes while HA is running get **silently clobbered** on HA's next debounced save — `storage.py` confirms no cross-process locking or merge |
| "Is this entity actually broken" (availability) | Runtime state machine (`get_states`) | Registries have **no availability/last-seen field at all** — purely a runtime concept, never persisted |
| Automation CRUD, default layout (`automations.yaml`) | `config/automation/config/{id}` REST (what HA's own UI editor uses) | Auto-triggers `automation.reload` scoped to that automation; hand-written file writes don't |
| Automation CRUD, custom layout (`packages:`, `!include_dir_merge_list`, inline in `configuration.yaml`) | Direct YAML file read/write + explicit `automation.reload` call | Both the UI and its REST API **hard-code** `automations.yaml` — split/packaged configs are invisible to them |
| Recent warnings/errors, quick glance | `system_log/list` WS command | Cheap, no file I/O — but capped ~50 entries, WARNING+ only, in-memory/lossy |
| Real log forensics (DEBUG/INFO, full history, full-text search, tail) | Raw `home-assistant.log` file | **No API substitute exists anywhere in core** — confirmed by reading `logbook` (recorder/DB-backed), `logger` (live level control only, no history), `system_log` (lossy cap) — this is the one place file access isn't a fallback, it's the only path |
| Add-on/Supervisor logs | Supervisor API (`SUPERVISOR_TOKEN`) | Separate subsystem, HA OS/Supervised installs only |
| Dashboards, storage mode (default since core, YAML mode being removed in 2026.8) | WS `lovelace/config`, `lovelace/config/save`/`delete` | Fully sufficient, no file access |
| Dashboards, YAML mode (legacy) | Raw `ui-lovelace.yaml` (supports `!include`) | `lovelace/config/save`/`delete` WS commands explicitly raise `"Not supported"` for YAML-mode dashboards — hard-blocked, not just discouraged |
| Helpers (`input_boolean`/`number`/`text`/`select`/`datetime`/`button`, `counter`, `timer`, `schedule`) | WS `<domain>/{list,create,update,delete,subscribe}` — fully generic, same collection pattern across all nine domains | 100% API-sufficient for storage-defined helpers. YAML-defined helpers coexist as read-only via WS (`editable: false`) — same layout-awareness principle as automations |
| Blueprints | WS `blueprint/{list,import,save,delete,substitute}` | File-based on disk but has a real API — no raw file access needed. **Must** call `blueprint/substitute` when auditing an automation with `use_blueprint:` — the automation YAML alone is `{path, input}`, not the actual trigger/condition/action logic |
| Scripts | Identical pattern to automations: `config/script/config/{id}` REST for default `scripts.yaml`; raw file for packages | Dict-keyed instead of list-keyed, otherwise the same reload-on-write behavior and the same package blind spot below |

**Every automation/script/dashboard-YAML tool starts by detecting layout**: does the domain resolve to the default single-file `!include`, or to packages/custom includes? Cache the answer, route reads/writes accordingly. The old repo never did this — "just read/write `automations.yaml`" was a fragile, unstated assumption.

## Hard safety rule: package provenance (found by reading `config.py`'s package merge, directly relevant to the `packages/emhas.yaml`-style workflow)

Package merging (`merge_packages_config` in `config.py`) splices a package's `automation:`/`script:` lists straight into the same in-memory list that `automations.yaml`/`scripts.yaml` populate — **and then discards all provenance**. Nothing in the merged runtime config records that a given automation came from a package file rather than the default one. There's also no hard error on duplicate entity/automation IDs across packages — a scalar-key collision in a dict-merged domain just logs an error and drops the loser; list-merged domains (automation/script) just concatenate, duplicates and all.

The consequence is worse than "the REST API doesn't know about packages" — **it's actively unsafe for anything package-defined**, because `config/automation/config/{id}` and `config/script/config/{id}` hard-code `automations.yaml`/`scripts.yaml` and never look anywhere else:
- `GET` on a package-defined automation's ID → 404, even though it's live and running.
- `POST` (an "edit") on a package-defined automation's ID → doesn't fail. It finds no matching ID in `automations.yaml`, so it **appends a new, diverging duplicate there** and reloads. Now two automations share an ID, one real (in the package) and one stray (in `automations.yaml`), both live.
- `DELETE` on a package-defined automation → looks for the ID only in `automations.yaml`, finds nothing, returns "not found" — giving the false impression nothing happened, while the real one keeps running untouched.

**Hard rule for the tool**: before any automation/script write by ID, first resolve which file actually defines that ID — scan the default file *and* every package file's `automation:`/`script:` block for a matching ID. Only use the `config/automation|script` REST API when the ID is confirmed to live in the default file. Anything package-defined goes through raw YAML file editing of that specific package file, followed by an explicit `automation.reload`/`script.reload` call. Never let the tool call the REST API "just to be safe" on an ID it hasn't located first — that's precisely the silent-duplicate failure mode above. This also means the automation-audit tool should check for accidental duplicate IDs across packages/default file as its own hygiene finding, since HA itself won't stop you from creating one.

This is also the answer to "what's the professional way to develop complex configs": current HA direction (per 2026.8 release notes and community consensus) is UI-first for casual use, with YAML/packages explicitly preserved as the sanctioned path for exactly this kind of version-controlled, template-heavy, cross-domain config. That's not a legacy path to migrate off of; it's the one this tool needs to handle correctly, including the unsafe-REST-API trap above.

## Tool surface, derived from real workflows

Grouped by four workflow archetypes, not a flat CRUD list:

- **Author/iterate** (new automation, presence-based automation, solar logic, template debugging, dashboards/helpers): area-scoped entity lookup (so e.g. "kid's bedroom" resolves without dumping hundreds of entities) → draft YAML → `render_template`/`validate_template` against live state, repeatedly, before writing anything → write via the layout-aware automation/script tool (package-safe per the rule above) or the generic helper/dashboard WS-CRUD tool → `check_config` → `automation.reload`/`script.reload` (never a full restart) → read back fresh `get_logs`/`system_log` scoped to that change to confirm it registered clean. If the automation uses `use_blueprint:`, resolve it via `blueprint/substitute` before treating the YAML as the full picture.
- **Diagnose** (log triage, "why is this failing"): `get_logs` (tail/filter/search over the raw log file, replacing the old unbounded `get_error_log`) with results correlated against config (which automation/integration owns this error, resolving package provenance where relevant) and live state (is the referenced entity `unavailable` right now).
- **Audit/hygiene** (race conditions, silent failures, stale entities, duplicate package IDs): a static-analysis tool that walks every automation's trigger/condition/action tree (layout-aware, package-provenance-aware, blueprint-resolving) and flags: service calls (`rest_command`/`shell_command`/etc.) with no response-check or failure notification; automations with default/`mode: single` sharing overlapping triggers with another automation (real race risk, confirmed via `helpers/script.py`: re-triggers are dropped while running, logged at WARNING — easy to miss); actions referencing entities currently `unavailable`/`unknown`; duplicate automation/entity IDs across packages and the default file (HA's own package merge doesn't hard-error on these). Separately, an entity-hygiene tool buckets all entities by registry state (disabled/hidden) crossed with live availability, grouped by integration/area, so "hundreds of broken entities" becomes a scannable list instead of a wall of text.
- **Configure** (helpers, dashboards — new, previously uncovered): thin, mostly-generic wrappers over the `<domain>/{list,create,update,delete}` WS collection API shared by all nine helper domains, and `lovelace/config`/`lovelace/config/save` for storage-mode dashboards. Low engineering cost — this is closer to a mechanical wrapper than the automation tooling, since HA's own API already does the hard part. YAML-mode dashboards (legacy, being removed in 2026.8) fall back to raw `ui-lovelace.yaml` editing like the automation-YAML case.
- **Monitor** (add-on/app logs — previously uncovered entirely): a Supervisor-log tool, gated to HA OS/Supervised installs, same tail/filter shape as the core log tool.

## What carries over vs. gets dropped

**Keep, re-wire as `Tool` backing implementations:** `FileManager`, `LogManager` (already does real level/search/time filtering — just needs the raw-file tail/forensics case added), `SecurityManager`'s path allowlist/denylist engine (solid, well-tested, no changes needed — reuse as-is for path authorization inside tools). Port `SyncManifest`'s checksum-based conflict model from `ha-dev-tools-mcp` for the automation-write tool's conflict detection (drop the competing timestamp-based `conflict_resolution.py` entirely — never reconciled with `SyncManifest`, never wired in).

**Drop entirely:** `/api/management/*` REST surface and `api.py`'s `HomeAssistantView`s (no HTTP hop needed in-process), `manager.py`'s multi-instance abstraction, `HAAPIConnection` stubs, `PathValidator` (duplicate of `FileSaver`'s sanitizer, never used), `workflow_state.py` (never wired, duplicate `FileStatus` semantics), the disabled `config_flow.py` (replace with a real one — also needed to let the user pick which tool categories to expose, mirroring how `mcp_server`'s own config flow multi-selects APIs).

**Fix while in there:** `docs/SECURITY.md` overstates what's implemented (documents rate limiting/audit logging that don't exist — implement or strike); `docs/API.md` documents routes that don't match reality — both are moot anyway once `/api/management/*` is deleted, replace with docs of the actual `llm.Tool` surface.

## Resolved: no MCP tool annotations in this architecture

Confirmed by reading `mcp_server/server.py`'s `_format_tool()`: it builds `types.Tool(name, description, inputSchema)` from an `llm.Tool` and never sets `annotations` — and `llm.Tool` itself (`helpers/llm.py:158-176`) has no `readOnlyHint`/`destructiveHint`/etc. fields to source them from. Schemas are `voluptuous`, converted to JSON Schema via `voluptuous_openapi.convert`. So tool annotations simply aren't a concept in this architecture — don't design for them; lean on tool naming/description and the `llm.Tool` docstring to communicate intent instead.

## Concrete tool list (v1), by archetype

**Author/iterate**
- `find_entities(area?, domain?, name_search?)` — area/domain-scoped lookup via entity+area registry WS, replaces flat `list_entities`.
- `get_entity_state(entity_ids)`, `render_template(template)`, `validate_template(template)` — kept from the old design, must actually be reliable this time.
- `get_automation(id)` / `get_script(id)` — layout-aware read: resolves default-file vs package source, expands `use_blueprint:` via `blueprint/substitute`, returns fully-resolved logic + source file.
- `write_automation(id, config, expected_hash?)` / `write_script(...)` — layout-aware write implementing the package-provenance hard rule above; conflict check via the ported `SyncManifest` checksum model.
- `check_config()`, `reload_automations()`, `reload_scripts()` — never a full restart.
- `list_blueprints(domain)`, `get_blueprint(domain, path)`.

**Configure**
- `list_helpers(domain?)`, `create_helper(domain, config)`, `update_helper(domain, id, config)`, `delete_helper(domain, id)` — one implementation parameterized across all nine helper domains via the shared WS collection pattern.
- `get_dashboard(url_path?)`, `write_dashboard(url_path, config)` — WS-backed in storage mode, raw-file-backed in YAML mode (mode-detected).

**Diagnose**
- `get_logs(source="core", tail=200, level?, search?, since?, until?, offset?, limit?)` — the consolidated, capped-response tail/filter tool replacing the old three-tool mess.
- `get_recent_warnings()` — thin `system_log/list` wrapper for the cheap/fast path.
- `get_addon_logs(slug, ...)` — Supervisor-gated, same shape as `get_logs`.
- `get_logbook(entity_id?, start_time?, end_time?)` — pass-through to the recorder-backed API.

**Audit/hygiene**
- `audit_automations()` — provenance-mapped, blueprint-resolved static analysis; flags unhandled `rest_command`/`shell_command` failures, default-mode overlapping-trigger race risk, references to currently-unavailable entities, and duplicate IDs across packages/default file.
- `entity_health_report(area?, integration?)` — registry state × live availability × staleness, grouped for scannability.
- `bulk_update_entities(filter, patch, dry_run=true)` — for mass-migration cases (e.g. an IP address change across many entities); dry-run by default given the blast radius.

## Implementation phasing

1. **Foundation** — new `ha_dev_tools` integration skeleton, real (enabled) config flow, registers an empty `llm.API`. Verify end-to-end: a trivial tool shows up and is callable through the native `mcp_server`'s `/api/mcp/dev_tools` from a real MCP client. Prove the architecture before building on it.
2. **Core authoring loop** — ✅ landed (`feature/llm-tools-foundation`): layout detection, `get_logs` (raw file), `find_entities`, `get_automation`/`write_automation`, `check_config`/`reload_domain`. `render_template`/`validate_template` not yet ported - still open.
3. **Package safety** — ✅ landed alongside Phase 2 rather than after it (the two turned out inseparable: `write_automation` isn't safe without provenance resolution from the start). Tested directly against a `packages/emhas.yaml`-shaped fixture, which also surfaced and fixed a real bug: `SecurityManager`'s `**` glob matching didn't match direct children of `packages/`, only subdirectories - see CHANGELOG. Blueprint resolution (`blueprint/substitute` for `use_blueprint:` automations) still open - `get_automation` currently returns unexpanded blueprint references as-is.
4. **Configure surface (helpers + dashboards)** — ✅ landed, both halves: `ws_call.py` constructs a real `websocket_api.ActiveConnection` with a fake transport (a `send_message` callback capturing the result into a Future) and calls `connection.async_handle(msg)` directly - reusing 100% of HA's real command lookup, schema validation, and admin enforcement, faking only the network socket. Verified before building anything on top of it: `tests/test_ws_call.py` does a real `input_boolean` create→list→update→delete round-trip through actual WS command dispatch, plus a real admin-vs-non-admin permission check. `helper_manager.py` builds on this for all nine helper domains (verified against two: `input_boolean` and `counter`), resolving the real calling user from `LLMContext.context.user_id` rather than a synthetic admin bypass - refuses if unresolvable (moved to `ws_call.py` as `resolve_user`, shared by both helpers and dashboards rather than helper-specific). `dashboard_manager.py` covers storage-mode dashboards the same way (`lovelace/config`/`lovelace/config/save`, verified with a real write-then-read round-trip); YAML-mode dashboard writes are explicitly rejected with a clear error (HA itself hard-rejects them at the WS level) rather than silently failing - raw `ui-lovelace.yaml` access for that case isn't implemented.
5. **Audit/hygiene** — ⚠️ partial: `audit_automations` (duplicate ids + unavailable-entity references) and `entity_health_report` (per-integration disabled/hidden/unavailable/unknown/missing counts + a capped problem-entity sample, scoped by area/integration) both landed. Overlapping-trigger race detection and unhandled rest_command/shell_command failure detection in `audit_automations` are real but need more careful semantic analysis to avoid false positives - deliberately deferred rather than shipped unreliable. `bulk_update_entities` (Concrete tool list) not started.
6. **Monitor** — Supervisor add-on logs. Not started.
7. **Retirement** — archive `ha-dev-tools-mcp` and `ha-development-power` once parity is reached; fix `docs/API.md`/`docs/SECURITY.md` to describe what's actually built.

**Also open, not yet in any tool**: `render_template`/`validate_template` (mentioned in Phase 2 but not ported), blueprint resolution for `get_automation`/`audit_automations`.

**A real open question surfaced mid-implementation, not yet resolved**: both this sandbox's package mirror AND a real GitHub Actions runner (independent, real internet access) resolve `homeassistant==2025.1.4` as the latest release on PyPI - a year+ behind the `mcp_server`-having `dev` branch this whole design is built on (confirmed to exist by directly reading home-assistant/core's source). Neither this sandbox nor CI can currently validate the actual MCP-serving path end-to-end - only a real, currently-running HA instance can. Check what version your own instance reports before assuming this works as designed; if it's older than 2026.8.2, the `llm.Tool`s are all still correctly built and unit-tested, but the "reachable via `/api/mcp/dev_tools`" part is unverified until an HA release with `mcp_server` actually ships.

## CI / release process (mirrors `alexlenk/ecowitt_local`)

This repo's existing `hassfest.yml`/`test.yml`/`validate.yml` cover validation and tests but had no release automation. Adopted the same pattern already proven across the user's other HACS integrations:

- **`manifest.json`'s `version` field is the single source of truth.** Every release bumps it; nothing else defines the version independently.
- **`version-check.yml`** — PR gate on `claude/release-*` branches: fails unless the version in `manifest.json` is a valid, strictly-incremented semver compared to `main`.
- **`auto-pr.yml`** — after CI passes on a `claude/**` branch with a version bump, auto-opens a `Release vX.Y.Z` PR to `main` with release notes pulled from `CHANGELOG.md`.
- **`auto-merge.yml`** — auto-merges open `claude/release-*` PRs once their checks pass.
- **`auto-release.yml`** — on push to `main` (or after auto-merge completes — `workflow_run`, since bot-triggered merges don't fire `push` events), extracts the version from `manifest.json`, creates an annotated `vX.Y.Z` git tag, and creates a GitHub Release with the matching `CHANGELOG.md` section as its body. **The tag is what HACS actually watches** — no tag, no update notification for users.
- **`CHANGELOG.md`** in [Keep a Changelog](https://keepachangelog.com/) format — `## [X.Y.Z] - YYYY-MM-DD` sections with `### Added`/`### Fixed`/`### Changed` subsections; the auto-release workflow extracts the matching section verbatim as the GitHub Release body, so an entry is required for every version bump.
- **`.pre-commit-config.yaml`** — black/isort/flake8/mypy + basic hygiene hooks, matching the CI checks so failures surface locally before a push.

Deliberately **not** mirrored yet: `claude-code.yml` (a GitHub Action that lets Claude autonomously triage issues and open PRs) and `protect-agent-files.yml` (guards agent-instruction files from bot edits). Those two are a further step — full autonomous-bot operation on this repo — worth a separate, explicit decision rather than bundling into a release-automation mirror.

## Verification

- Unit tests via `pytest-homeassistant-custom-component` fixtures for every `Tool.async_call()` — no live HA needed for logic-level iteration.
- Integration tests reusing the existing `SecurityManager`/`FileManager`/`LogManager` suites, adjusted for the new call sites.
- End-to-end: enable `dev_tools` in the native `mcp_server` config flow on a real (or test) HA instance, connect an actual MCP client (Claude Code) to `/api/mcp/dev_tools`, and walk one full workflow per archetype above (author a real automation end-to-end with no manual copy-paste or restart; triage real logs; run the audit tool against real automations; pull add-on logs if on Supervised) before calling it done.
