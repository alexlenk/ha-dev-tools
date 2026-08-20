# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Restart plan for the project: `ha_dev_tools` becomes a Home Assistant integration that registers development-workflow tools directly into HA's native `llm.Tool`/`mcp_server` system, replacing the standalone `ha-dev-tools-mcp` process and its `/api/management/*` REST bridge. See `docs/RESTART_PLAN.md`.
- Release automation (version-check, auto-pr, auto-merge, auto-release) mirroring the pattern used across the author's other HACS integrations.
