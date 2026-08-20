# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Helper CRUD tools (`list_helpers`/`create_helper`/`update_helper`/`delete_helper`) covering all nine helper domains (`input_boolean`, `input_number`, `input_text`, `input_select`, `input_datetime`, `input_button`, `counter`, `timer`, `schedule`). These only have a WebSocket API, no in-process access point (their `StorageCollection` is a private variable inside each component's own `async_setup` - confirmed by reading the source). `ws_call.py` makes this reachable in-process by constructing a real `websocket_api.ActiveConnection` with a fake transport and calling its actual command dispatch directly - reusing HA's real schema validation and admin enforcement, faking only the socket. Verified with a real `input_boolean` create/list/update/delete round-trip and a real admin-vs-non-admin permission check before any tool was built on it (`tests/test_ws_call.py`). Resolves the real calling user from the MCP request's context rather than a synthetic admin bypass.
- Dashboard tools (`get_dashboard`/`write_dashboard`) using the same `ws_call.py` mechanism against `lovelace/config`/`lovelace/config/save`. Storage-mode dashboards only - a write against a YAML-mode dashboard (which HA itself hard-rejects at the WS level) now raises a clear `YamlModeDashboardError` explaining why, instead of a confusing raw WS error.

- Restart plan for the project: `ha_dev_tools` becomes a Home Assistant integration that registers development-workflow tools directly into HA's native `llm.Tool`/`mcp_server` system, replacing the standalone `ha-dev-tools-mcp` process and its `/api/management/*` REST bridge. See `docs/RESTART_PLAN.md`.
- Release automation (version-check, auto-pr, auto-merge, auto-release) mirroring the pattern used across the author's other HACS integrations.
- Phase 1 foundation: enabled the config flow, registered a `dev_tools` `llm.API` with one diagnostic tool (`dev_tools_ping`), and confirmed it's discoverable and callable through Home Assistant's LLM tool registry with a real headless test (`tests/test_llm_api.py`). Real tools land in later phases per `docs/RESTART_PLAN.md`.
- Bumped `hacs.json`'s minimum Home Assistant version to 2026.8.2 — the version that shipped the native `mcp_server` integration this design depends on.

- Phase 2 core authoring loop, all registered as real `llm.Tool`s on `dev_tools`:
  - `find_entities` - area/domain/name-scoped entity lookup (`entity_manager.py`), resolving an entity's area through its device the way HA itself does, and reporting live availability (registries have no such field - see `docs/RESTART_PLAN.md`).
  - `get_logs` - tail/filter/search over the real log file via the existing `LogManager`, replacing the old unbounded raw-blob `get_error_log`.
  - `check_config` / `reload_domain` - HA's own config-check helper and `<domain>.reload` services, wired so config changes never require a restart.
  - `get_automation` / `write_automation` - layout-aware, package-safe automation read/write (`automation_manager.py`), implementing the "hard safety rule" from `docs/RESTART_PLAN.md`: resolves which file (default `automations.yaml` or a specific `packages/*.yaml`) actually defines a given automation id before reading or writing, refuses to guess when an id is duplicated across files, and uses `ruamel.yaml`'s round-trip loader so hand-maintained package files keep their comments/formatting instead of being reformatted on every edit.

### Fixed
- Several legacy test files (`test_metadata_api.py`, four files under `tests/property/`) replaced `sys.modules['homeassistant']` and friends with `unittest.mock.Mock` objects at import time with no teardown, corrupting the real `homeassistant` package for every test file collected afterward in the same pytest process. Harmless while nothing else needed the real package, but broke `tests/test_llm_api.py`'s real `pytest-homeassistant-custom-component` fixtures. Now snapshotted and restored after each file's own imports.
- `SecurityManager`'s glob matcher: `packages/**/*.yaml` (the documented recommended pattern, and `DEFAULT_READ_ONLY_PATHS`'s own default) only matched files in a *subdirectory* of `packages/`, not direct children like `packages/emhas.yaml` - the exact real-world layout this project is built around. Plain `fnmatch` requires the pattern's literal `/` between the two `*` groups to be present in the path; `**` now also matches zero intermediate directories, as its own docstring already claimed it did.
