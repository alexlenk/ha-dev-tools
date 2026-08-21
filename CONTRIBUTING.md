# Contributing to HA Dev Tools

Thanks for your interest in contributing. This document covers how the
project is actually built and tested today - see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for why it's built this way.

## Code of Conduct

This project follows the [Home Assistant Code of
Conduct](https://www.home-assistant.io/code_of_conduct/).

## Project structure

```
ha-dev-tools/
├── custom_components/ha_dev_tools/
│   ├── __init__.py            # Integration setup
│   ├── manifest.json
│   ├── config_flow.py
│   ├── const.py
│   ├── llm_api.py             # Tool definitions, registered into HA's llm.API registry
│   ├── access_control.py      # The arm-file + admin gate every tool goes through
│   ├── security.py            # Path allowlist/denylist for file-touching tools
│   ├── file_manager.py / log_manager.py / automation_manager.py /
│   │   entity_manager.py / helper_manager.py / dashboard_manager.py /
│   │   template_manager.py / supervisor_manager.py / audit_manager.py /
│   │   config_tools.py        # Backing logic each Tool calls into
│   └── ws_call.py             # In-process WebSocket API loopback (helpers, dashboards)
├── tests/                     # Flat pytest files, one per module above
│   └── property/              # Hypothesis property-based tests
├── docs/                      # ARCHITECTURE.md, SECURITY.md
├── .github/workflows/         # test.yml, hassfest.yml, validate.yml
└── requirements-test.txt
```

## Prerequisites

- **Python 3.14+.** `requirements-test.txt` pins `homeassistant` transitively
  to a version whose own `Requires-Python` floor is 3.14.2 - anything older
  can't install it at all. `test.yml`'s CI matrix runs Python 3.14
  exclusively for the same reason.
- Git.

## Development setup

```bash
git clone https://github.com/YOUR_USERNAME/ha-dev-tools.git
cd ha-dev-tools
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements-test.txt
```

`requirements-test.txt` pins `pytest-homeassistant-custom-component` to an
exact version, which transitively pins an exact `homeassistant` version too
- this isn't a loose floor, it's deliberately reproducible. If you bump
either, re-verify the transitive `hassil`/`home-assistant-intents` pins
against the `conversation` component's manifest at the matching git tag
(see the comments in `requirements-test.txt` for why those matter and
`docs/ARCHITECTURE.md` for the full story on what broke the last time this
was assumed rather than checked).

**A note on local verification:** if your local Python isn't genuinely
3.14.2+, you may not be able to install the real dependency graph at all
- `pip` will silently resolve to whichever older `homeassistant` release
your interpreter *can* satisfy instead of erroring, which looks identical
to "nothing changed" until you check what actually got installed
(`pip show homeassistant`). When in doubt, trust CI's output over a local
run of unknown provenance.

## Running tests

```bash
PYTHONPATH=. pytest tests/ --ignore=tests/property/ -v
PYTHONPATH=. pytest tests/property/ -v --hypothesis-show-statistics
```

There's no `tests/unit/`/`tests/integration/` split - tests live flat in
`tests/`, one file per module, using real
[`pytest-homeassistant-custom-component`](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)
fixtures (a real `hass` instance, not a mock) rather than hand-rolled
mocks. Prefer that pattern for new tests: exercise the real Home Assistant
machinery (real WS command dispatch, real config entry setup) wherever
practical, and reserve mocks for things that genuinely can't run in a
sandbox (Supervisor, an actual MCP client).

**A recurring gotcha:** the default `hass` fixture reuses one physical
`testing_config` directory across test runs rather than a fresh
per-test tmp dir. Any test that writes a real file into
`hass.config.config_dir` needs its own cleanup (see
`test_access_control.py`'s `_clean_arm_file` fixture for the pattern) or it
will leak state into whatever test runs next.

## Code style

- 4-space indentation, type hints on function signatures, docstrings on
  public functions/classes.
- No comments explaining *what* code does - name things so it's obvious.
  Comments here explain *why*, especially where behavior was verified
  against actual Home Assistant source rather than assumed - see any
  existing module's docstring for the house style.

## Submitting changes

1. Branch off `main` with a descriptive name (`feature/...`, `fix/...`).
2. Add tests for new functionality; don't reduce coverage.
3. Run the full local suite, and if you touched anything version-sensitive
   (HA API surface, Python version handling), verify against a genuinely
   fresh install rather than your accumulated local venv - see the note
   above.
4. Add a CHANGELOG.md entry under `[Unreleased]`.
5. Open a PR describing what changed and why.

## Reporting issues

**Bug reports** should include: what you expected vs. what happened,
reproduction steps, your Home Assistant and this integration's versions,
and relevant log entries (`custom_components.ha_dev_tools: debug` under
`logger.logs` in `configuration.yaml` gets you more detail).

**Security issues**: please don't open a public issue. See
[docs/SECURITY.md](docs/SECURITY.md) for the threat model this project
defends against, then report privately via a GitHub security advisory or
to the maintainer directly.

## License

By contributing, you agree your contributions are licensed under this
project's [MIT License](LICENSE).
