# Automation testing & git mirroring — design notes

Design record for offline automation testing and git mirroring of writes
made through `ha_dev_tools`. Nothing in this document is built. It exists
to capture the reasoning behind the shape that was chosen (and the shapes
that were explicitly rejected) before any of it becomes code, the same way
[ARCHITECTURE.md](ARCHITECTURE.md)'s "Still open" section tracks other
deferred work.

The git-mirroring section below replaces an earlier "git-based promotion
flow" design that shipped in this same document. That earlier shape framed
a branch + PR + CI + human-merge sequence as something that gated whether a
change reached the live instance. It didn't, and pretending it did was the
problem: every write tool already applies live via reload, unconditionally,
before any of that git activity happens. Git can't un-happen a write, so a
"promotion pipeline" downstream of it was an audit trail wearing a gate's
clothes. The replacement below keeps the audit trail (that part was real
and useful) and adds an actual gate - a per-write confirmation step - that
sits where it can do something, before the write happens rather than after.

## The gap

Every write tool this integration exposes (`write_automation`,
`create_template_entity`, ...) takes effect on the *live* instance via
reload - that's deliberate, see README's Tools table and
[ARCHITECTURE.md](ARCHITECTURE.md). There is currently no way to verify an
automation change - corner cases in its triggers/conditions, templates,
rare `choose` branches - before it's live. `audit_automations` catches
duplicate ids and references to unavailable entities; nothing checks
behavior.

This isn't unique to this project. Home Assistant's own tooling
(automation traces, the editor's "Run actions" button, the Template dev
tool) is real-time and manual - useful for debugging something that
already ran, not for verifying something before it runs. Two community
threads confirm this is a recognized, unaddressed gap rather than a solved
problem this project would be duplicating:

- [Automations: Testing Framework](https://community.home-assistant.io/t/automations-testing-framework/688889) -
  a feature request for mocked actions, state simulation, and isolated
  scenarios, motivated by automations that only trigger once a month and
  whose bugs surface only when they finally fire for real.
- [Discussion #3870](https://github.com/orgs/home-assistant/discussions/3870) -
  a request for a "test trigger" option; a reply explicitly proposes
  "trigger mocking, trace generation, and simulation mode." Current
  workaround: wiring a temporary helper button to a specific `trigger_id`,
  testing, then deleting it.

The one place real automated testing already exists is AppDaemon/pyscript
apps, because they're plain Python and get plain `pytest` - at the cost of
leaving YAML automations behind entirely. This project's own test suite
(`tests/conftest.py`, `pytest-homeassistant-custom-component`) proves the
underlying technique - an isolated, throwaway HA core with mocked services
- works for exactly this kind of verification. It's just currently scoped
to testing `ha_dev_tools`' own Python, never a user's automation YAML.

## Proposed shape: two tiers, two projects

### Tier 1 - `lint_automation` (this repo, MCP-exposed, runs against the live instance)

Static analysis only - no execution, no side effects. Composes tools that
already exist plus a modest extension of `audit_manager.py`'s existing rule
engine:

- Validate against HA's real trigger/condition/action platform schemas,
  not just YAML syntax.
- Walk the whole automation config and run every embedded template through
  `template_manager.py`'s existing `validate_template` automatically,
  instead of requiring the agent to find and check each one by hand.
- New lint rules: `choose`/`if` with no `default`, a condition referencing
  an entity no trigger in the same automation ever supplies, always-true/
  false literal conditions, missing `mode` on a retriggerable automation,
  an action targeting the same entity as the trigger with no guard
  (feedback-loop risk).

This stays in `ha_dev_tools` because it benefits from being live (real
entity registry, real current state for template checks) and is cheap and
safe enough to run on every edit as part of the authoring conversation.
It's also the natural content for the "local testing" step in the
per-write confirmation flow below - `lint_automation`'s findings belong in
the preview a human reviews before confirming a write, not as a separate
tool call the agent has to remember to make.

### Tier 2 - offline behavioral simulation (a separate project, not this repo)

Not built inside `ha_dev_tools`, and not run on the live instance, for
reasons specific to this integration's constraints:

- You cannot run a second `HomeAssistant()` core on the same instance's
  event loop; genuine isolation needs its own process, which is a real
  architectural piece, not glue code.
- `pytest-homeassistant-custom-component` is dev-only weight (full HA core
  a second time, plus `hypothesis`/`aioresponses`/`freezegun`) that doesn't
  belong as a runtime dependency on hardware this integration otherwise
  keeps deliberately light (RPi/NUC-class installs).
- A sandboxed core needs to track the live instance's HA version to be
  faithful, which is easier to manage as a standalone, disposable tool
  (dev laptop or CI) than as a pinned runtime dependency shipped alongside
  a live integration.

What it does instead: load the candidate automation's resolved YAML into
an isolated, throwaway HA core (the same mechanism `tests/conftest.py`
already uses, invoked standalone rather than under `pytest`), seed it with
a snapshot of relevant entity states for template realism, mock every
service call, fire synthetic trigger events across a scenario matrix, and
report which actions would have fired with what data. Scenario generation
should reuse `hypothesis` (already a dependency here) to derive boundary
cases per trigger/condition type - numeric_state right at/above/below
threshold, `for:` duration edges, source state `unavailable`/`unknown`,
multiple `choose` branches true at once - rather than hand-writing every
case.

Tier 2 runs as GitHub Actions CI on commits pushed to the mirror repo (see
below), not as a separate always-on service. Not designed yet - format
(plain `pytest`, or a declarative YAML scenario DSL) is an open question.

## Run modes, per-write confirmation, and mirroring

Three independent, orthogonal controls, not one setting doing three jobs:

### Run mode - `live` | `dry-run`

A global, persistent integration option (`access_control.is_dry_run()`,
unchanged from what's built today). Every write tool runs identically in
either mode, right up to the very last step: in `dry-run`, the resolved
change is returned as a preview instead of being written to disk; in
`live`, it's written and the affected config reloaded. This is a mode you
set for an environment (a test instance vs. a real one), not something
toggled per change - and specifically **not** the mechanism that decides
whether an individual write was approved. A human forgetting to flip a
global option back is exactly the failure mode that must not gate safety.

### Per-write confirmation - always on, both modes

Every write tool call is two calls, regardless of run mode:

1. **Propose.** Called without a valid token. Builds the same config dict
   the real write would use (matching what `automation_manager.py` etc.
   already construct), renders it as YAML with the `id` key stripped (not
   something a user should hand-edit back in - it's bookkeeping for
   `find_automation`'s duplicate-id detection), runs `lint_automation`
   against it, and returns the YAML snippet plus the lint findings plus a
   short-lived token bound to a hash of the tool name and its normalized
   arguments. No side effects. The tool's own response text instructs the
   agent to show this to the user and ask for explicit confirmation before
   calling again - the same pattern `WriteGatedTool`'s existing dry-run
   short-circuit already uses successfully today.
2. **Confirm.** Same tool, same arguments, plus the matching token. Only
   then does it fall through to the run-mode-appropriate outcome above.

This is friction and a better experience, deliberately not a hard security
boundary - it doesn't stop an agent from calling both steps back to back
with no real human in between. Binding the token to the specific tool +
arguments (not a bare nonce) at least stops silent scope drift between the
two calls. An HA-native out-of-band approval gate (a mobile actionable
notification or a dashboard button, with the real write wired to an
automation the LLM/MCP surface has no path to at all) was considered and
rejected - see "Explicitly rejected" below.

### Mirroring - independent on/off, either run mode

A dedicated, private GitHub repo, configured in the integration -
deliberately **not** the same repo the existing Git Pull add-on deploys
from (see `scripts/config-repo-setup/`, which hardens that different repo
for a different purpose). Keeping them separate is what avoids two
independent writers racing on the same live config tree: this mirror is
written only by `ha_dev_tools` itself, on its own schedule, and nothing
else ever pulls from or pushes to it.

On every **confirmed** write (mirroring on, either run mode):

- **Before-commit, always to `main`.** Sync `main` to the actual current
  live state of the file(s) about to be touched, before anything else
  happens. If nothing has changed since the last mirrored write, this is
  an empty diff - git has nothing new to record, which is itself a useful
  confirmation that live state hasn't drifted. If it *has* drifted (a
  manual SSH edit, the Git Pull add-on deploying something), that drift
  becomes its own visible commit on `main` before the new change lands on
  top of it - `main` ends up an honest, continuously-verified mirror of
  live truth, not just a log of agent actions.
- **Live mode:** the write applies, then the new live state is committed
  to `main` as well.
- **Dry-run mode:** nothing live changes. The resolved would-be YAML is
  pushed instead to its own branch, one branch per automation/entity
  (`proposed/<kind>-<id>`), branched from the `main` HEAD `main` was just
  synced to - so the branch's diff cleanly shows "this is what would
  change from current reality," and multiple explored-but-unapplied ideas
  can coexist without clobbering each other. Accepted tradeoff: this
  accumulates branches over time with no pruning policy yet (see "Open
  questions").

Manual rollback is just: find the old YAML in `main`'s history, copy it
back via the same paste-able-snippet flow the propose step already uses.
No merge, no deploy credential, no special tooling - the mirror repo is a
read source for a human, not a system another process consumes.

## Blast-radius analysis

**Effect on the live instance** - mirroring itself never touches the live
instance and never gates anything; it happens strictly after (live mode)
or instead of (dry-run) a write that the run mode + confirmation token
already decided. The things that actually control live effect are, in
order: run mode (can this write ever touch disk at all), then per-write
confirmation (was this specific call deliberately confirmed).

**New attack surface beyond the live instance**, per
[SECURITY.md](SECURITY.md)'s framing of this integration already
collapsing one previously-separate credential domain (HA auth vs.
SSH/file access): the mirroring push credential, scoped to a single
dedicated private repo, outbound-push only - no merge/admin rights needed
because nothing in this design ever merges anything back into the live
system. Reading tier-2 CI results back (see "Open questions") would add an
outbound read of the GitHub API using that same credential; deliberately
not an inbound webhook, since that would mean exposing an endpoint to the
internet from an instance that's typically behind NAT with no port
forward - a much larger new surface than an outbound-only credential.

## Explicitly rejected

- **Treating git branch/PR/CI/merge as a live-deploy gate.** The write
  already happened (or didn't) via run mode + confirmation before
  mirroring ever runs. Framing human review/merge as "the" gate was
  inaccurate about what it actually blocks - nothing, by that point.
- **Relying on the dry-run toggle's on/off timing as an approval signal.**
  Too coarse - a persistent environment-level mode, not a per-change
  decision - and depends on a human remembering to flip it back at exactly
  the right moment.
- **An HA-native notification/automation-based approval gate** (mobile
  actionable notification or dashboard button, wired so the real write
  only happens via a service call the LLM/MCP surface can't itself reach).
  Would have been the one mechanism that actually proves a human looked,
  but the real engineering cost (a pending-change store, plus an
  automation every user has to wire up themselves) wasn't worth it for
  what turned out to be the actual goal - "we don't need a full guarantee,
  just a better experience." The two-call confirmation token gets most of
  the UX benefit for a fraction of the build.
- **Running a second `HomeAssistant()` core inside the live instance.**
- **The MCP server holding `git pull`/production-deploy credentials, or
  reimplementing anything the Git Pull add-on already does.** Reinforced
  by mirroring deliberately targeting a separate repo from whatever the
  Git Pull add-on manages.

## Open questions

- **`proposed/*` branch lifecycle.** One branch per automation/entity is
  the current answer, chosen for simplicity even knowing it can accumulate
  clutter with no pruning policy yet - revisit if that turns out to be a
  real problem rather than a theoretical one.
- **How tier-2 CI results get back to the agent.** Leaning toward a
  pull-based tool (e.g. `get_mirror_status`) the agent calls on demand,
  querying the pushed commit's check-run/status via the same outbound
  credential used to push - not a webhook, for the inbound-connectivity
  reason above, and not blocking the write call itself, since a hosted CI
  run can take minutes. Not fully decided.
- **Whether `confirm_token` can be added once via `WriteGatedTool`'s shared
  base, or needs touching each write tool's own `vol.Schema`
  individually.** Implementation detail, not yet checked against the code.
- Where tier 2 actually lives - new repo, name, scope: not decided.
- Scenario-generation design - which `hypothesis` strategies per trigger/
  condition type: not designed.
- `write_automation_test` scenario format - plain `pytest` vs. a
  declarative YAML DSL: not decided.
- Whether `lint_automation` ships ahead of tier 2: no blocking dependency
  between them; tier 1 is buildable against this repo's existing code
  today.

## Sources consulted

- [Testing and troubleshooting automations - Home Assistant docs](https://www.home-assistant.io/docs/automation/troubleshooting/)
- [Automations: Testing Framework - Feature Requests](https://community.home-assistant.io/t/automations-testing-framework/688889)
- [Virtual Test Devices - clone real devices for safe automation testing](https://community.home-assistant.io/t/virtual-test-devices-clone-real-devices-for-safe-automation-testing/1019587)
- ["Test trigger" option to test complex automations - Discussion #3870](https://github.com/orgs/home-assistant/discussions/3870)
- [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)
- [AppDaemon, next level home automation](https://medium.com/@marcelblijleven/appdaemon-part-1-e63d1bffe7ca)
- [Version control HA config with GitHub, not snapshots](https://botmonster.com/smart-home/how-to-back-up-home-assistant-config-to-github-automatically/)
- [frenck/home-assistant-config - GitHub Actions workflow](https://github.com/frenck/home-assistant-config/blob/master/.github/workflows/home-assistant.yml)
- [home-assistant/actions - official Actions (hassfest, etc.)](https://github.com/home-assistant/actions)
- [Git Pull add-on README](https://github.com/home-assistant/addons/blob/master/git_pull/README.md)
- [How I GitOps Home Assistant Configurations](https://budimanjojo.com/2021/11/04/gitops-home-assistant-configurations/)
- [MCP Elicitation: Human-in-the-Loop for MCP Servers](https://dzone.com/articles/mcp-elicitation-human-in-the-loop-for-mcp-servers) -
  confirms elicitation exists at the protocol level but depends on client
  support.
- [ni-c/mcp-approval](https://github.com/ni-c/mcp-approval) - source of the
  two-call confirmation token pattern used above.
- [home-assistant/core `mcp_server` component](https://github.com/home-assistant/core/tree/dev/homeassistant/components/mcp_server) -
  checked `server.py`/`session.py` directly; no elicitation support as of
  the `dev` branch, which is why this design doesn't rely on it.
