# Automation testing & git-based promotion — design notes

Design record for offline automation testing and a git-based promotion path
out of `ha_dev_tools`. Nothing in this document is built. It exists to
capture the reasoning behind the shape that was chosen (and the shapes that
were explicitly rejected) before any of it becomes code, the same way
[ARCHITECTURE.md](ARCHITECTURE.md)'s "Still open" section tracks other
deferred work.

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

The bridge back to this repo: a possible future `write_automation_test`
tool that has the MCP-connected agent author a scenario test into the
user's config repo alongside an automation edit, so authoring stays
conversational while execution stays fully decoupled from the live
instance. Not designed yet - format (plain `pytest`, or a declarative YAML
scenario DSL) is an open question.

## Git-based promotion flow

Proposed pipeline: local edit + `lint_automation` (tier 1) -> local commit
-> push a branch + open a PR -> independent CI re-test (tier 2, hosted
runner) -> required human review/merge -> the existing Git Pull add-on
deploys to production, unchanged.

Each stage, and why it's shaped this way:

- **Local commit, no remote.** Safe by construction - no remote credential
  involved at all. Only if the target directory is already a git working
  tree; never auto-`git init` an unmanaged directory. Closes a real gap
  (no audit trail today) without adding any new attack surface, but note
  it doesn't change what happens to the *live* instance - `write_automation`
  already writes and reloads regardless of git state; committing just
  records that same change.
- **Push a branch, never the branch `git-pull` tracks.** The credential
  the MCP server holds is scoped to branch-push + PR creation only -
  explicitly no merge/admin/bypass rights on the protected branch.
- **Branch protection enforced by the git host**, not by the pipeline's
  good behavior. This is what actually re-establishes a human confirmation
  gate - a compromised token that technically can't merge can't bypass a
  server-side protection rule, whereas "the bot just doesn't push to main"
  is not a security boundary.
- **CI re-test at this stage is a correctness gate, not an authorization
  gate.** "CI green" must never auto-merge or auto-deploy. If it did, the
  same production box would be authoring, approving, and receiving its own
  change - the CI step would be rubber-stamping rather than reviewing, and
  it would corrupt the property git history is supposed to guarantee (that
  a human looked at this before it landed).
- **Git Pull add-on stays exactly as it is.** Its own independent deploy
  credential, only ever pulling the protected branch, only after a human
  merge. Not folded into `ha_dev_tools` - it already exists, is
  purpose-built, and is deliberately outside the LLM-facing tool surface;
  merging the two would put a second, previously-separate credential
  domain behind whatever compromises the MCP server.

## Live-state mirror (push-only audit trail)

A second, distinct piece from the branch+PR promotion flow above, though
it composes with it: every `write_automation` call also commits the
specific file it touched to a dedicated branch on the live instance,
before and after the change, giving a real git history of what the
instance actually did - independent of whether any of it went through a
reviewed PR. Motivated by the same gap the rest of this document is
about: today there is no record at all of what an MCP-driven change was,
only what it currently is.

**Push-only, never pull, from this mechanism specifically.** That
one-way property is what makes it meaningfully safer than the branch+PR
flow's own risk profile: a fully compromised MCP server can write bogus
history to this branch, but has no path back into the live instance
through it, because nothing on the instance side ever reads it.

**It must not be the same branch anything else pulls from - regardless
of intent.** The instinct that this branch should just be `main`,
because `main` "is" the live instance's true state, is reasonable on its
own - and would be fine, if the only thing that branch ever received was
this mechanism's own retroactive mirror of already-applied changes. The
problem is that a push-capable credential's blast radius is defined by
what it's technically capable of, not what it's intended for (the same
point [SECURITY.md](SECURITY.md) makes about HA's own tokens) - if that
credential is compromised, nothing stops it from pushing a
forward-looking, non-mirrored change instead of a retroactive one. If the
Git Pull add-on (or anything else) is watching that same branch, it'll
deploy that too, and the whole point of "push-only, never pull" being
safe - that nothing on the instance side reads this branch back - stops
being true at the system level, even though the MCP server itself still,
technically, never calls `git pull`. So: mirror to a branch nothing
auto-deploys from (`live-state`, say); keep whatever branch the Git Pull
add-on tracks human-merge-only, exactly as in the promotion flow above.
If you want the live instance's own state to periodically become the new
deploy baseline, that's a deliberate, reviewed merge from `live-state`
into the deploy branch - a human decision, not something this mechanism
does on its own.

**The "before" commit is conditional on detected drift, not
unconditional.** Immediately before applying a change, compare the
current on-disk file against what this branch's last commit for that
file says. If they match, there's nothing new to record - skip straight
to committing "after". If they differ, something changed the file
outside `write_automation` since the last recorded state (a UI edit, SSH,
anything) - commit that drift as its own "before" snapshot first, so it
doesn't get silently absorbed into the diff that's actually about to
happen. This is also the answer to whether the MCP server needs to push
twice every time: only when there's something to record.

**Reading the branch's state to detect that drift is not "pulling".**
"Never pull" means never merging remote history into the live working
tree - it says nothing about reading the remote's current content or
commit log for reference, the same way `template_manager.py`'s
`render_template` reads live `hass` state without mutating it. A
read-only GitHub API call (fetch the file's current content at that
branch's HEAD, or list recent commits) is how the drift check above
works, and it's also how the MCP server could show a real diff back to
whoever's driving it, without needing a second local git operation to
produce one.

**Same credential-scoping rules as the promotion-flow PAT**: this repo
only, `contents: write`, no `workflow` scope (so even full compromise
can't touch anything under `.github/workflows/`), no admin, no
force-push. `write_automation` commits only the specific file
`automation_manager.py` already resolved for that automation - never a
broad `git add -A` of the live `/config` tree - and the push is
best-effort and asynchronous with respect to the actual reload: a failed
push logs and gets retried, it never blocks or fails the live change
itself.

## Blast-radius analysis

Two separate axes matter here, and conflating them is the easiest way to
get this wrong.

**Effect on the live instance**, weakest to strongest:
1. `lint_automation` / tier-2 simulation - no persistence anywhere.
2. Branch + PR (scoped credential, no merge rights) - zero live effect
   until a separate human merge plus the existing Git Pull add-on act on
   it. Strictly weaker than what `write_automation` can already do today.
3. `write_automation`'s existing full write + reload - the ceiling that
   already exists, unconditionally, today.

Local commit doesn't sit on this axis at all - it piggybacks on whatever
already wrote the file; it adds provenance, not live impact.

**New attack surface beyond the live instance** (this is the part worth
being honest about, per [SECURITY.md](SECURITY.md)'s framing of this
integration already collapsing one previously-separate credential domain -
HA auth vs. SSH/file access):
- Local commit: none.
- Branch + PR: introduces a git-host credential to the MCP server's
  process for the first time. Mitigated, not eliminated, by scoping (no
  merge/admin) and host-enforced branch protection. Also makes the CI
  triggered by that branch reachable from an HA-token compromise - if that
  CI exposes any secret to PR-triggered jobs, a compromised MCP server now
  has a second path to it. That's a pre-existing CI-hardening
  responsibility (secrets shouldn't be exposed to PR-triggered jobs
  regardless of who pushes), not a vulnerability this design creates - but
  it is a precondition to state explicitly before relying on this pipeline,
  not something to assume away.

**Explicitly rejected:**
- Running a second `HomeAssistant()` core inside the live instance.
- The MCP server holding `git pull`/production-deploy credentials, or
  reimplementing anything the Git Pull add-on already does.
- The MCP server pushing directly to the branch `git-pull` tracks, or any
  "CI green implies auto-merge/auto-deploy" shortcut.
- The live-state mirror branch being the same branch anything auto-pulls
  from (Git Pull or otherwise) - collapses back into the same circularity
  the promotion flow's branch separation exists to prevent, even though
  the mirror mechanism itself never calls `git pull`.

## Open questions

- Where tier 2 actually lives - new repo, name, scope: not decided.
- Scenario-generation design - which `hypothesis` strategies per trigger/
  condition type: not designed.
- `write_automation_test` scenario format - plain `pytest` vs. a
  declarative YAML DSL: not decided.
- Whether `lint_automation` ships ahead of tier 2: no blocking dependency
  between them; tier 1 is buildable against this repo's existing code
  today.
- Live-state mirror: where the scoped push credential is actually read
  from at runtime (a config-entry option? a restricted-permission secret
  file?), and the drift-detection + conditional-commit logic in
  `automation_manager.py` itself - not built. `scripts/live-state-mirror/`
  only seeds the branch; it doesn't wire up the runtime hook.

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
