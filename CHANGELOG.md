# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Restart plan for the project: `ha_dev_tools` becomes a Home Assistant integration that registers development-workflow tools directly into HA's native `llm.Tool`/`mcp_server` system, replacing the standalone `ha-dev-tools-mcp` process and its `/api/management/*` REST bridge. See `docs/RESTART_PLAN.md`.
- Release automation (version-check, auto-pr, auto-merge, auto-release) mirroring the pattern used across the author's other HACS integrations.
- Phase 1 foundation: enabled the config flow, registered a `dev_tools` `llm.API` with one diagnostic tool (`dev_tools_ping`), and confirmed it's discoverable and callable through Home Assistant's LLM tool registry with a real headless test (`tests/test_llm_api.py`). Real tools land in later phases per `docs/RESTART_PLAN.md`.
- Bumped `hacs.json`'s minimum Home Assistant version to 2026.8.2 — the version that shipped the native `mcp_server` integration this design depends on.

### Fixed
- Several legacy test files (`test_metadata_api.py`, four files under `tests/property/`) replaced `sys.modules['homeassistant']` and friends with `unittest.mock.Mock` objects at import time with no teardown, corrupting the real `homeassistant` package for every test file collected afterward in the same pytest process. Harmless while nothing else needed the real package, but broke `tests/test_llm_api.py`'s real `pytest-homeassistant-custom-component` fixtures. Now snapshotted and restored after each file's own imports.
