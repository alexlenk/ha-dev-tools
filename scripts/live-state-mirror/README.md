# Live-state mirror branch setup

Seeds the branch a future `write_automation` runtime hook would push
before/after commits to - see
[docs/AUTOMATION_TESTING_DESIGN.md](../../docs/AUTOMATION_TESTING_DESIGN.md)'s
"Live-state mirror" section for the full design and why this has to be a
different branch from whatever your Git Pull add-on (or anything else)
auto-deploys from.

## What this script does, and doesn't do

`setup-live-state-branch.sh` only creates and seeds a local branch
(default name `live-state`) and tells you what to do next. It
deliberately does **not**:

- Create, generate, or store any GitHub credential. That's a manual step
  on github.com (a fine-grained PAT, scoped to this repo only, `Contents:
  Read and write`, nothing else - no `workflow`, no admin, an actual
  expiration date set) - never something a script should do on your
  behalf, and never something to paste into a file this script or git
  tracks.
- Configure anything on the live Home Assistant instance itself. The
  runtime piece - `write_automation` actually calling `git commit`/`git
  push` on this branch, with drift detection for the "before" commit - is
  a separate, not-yet-built change to `automation_manager.py` in this
  repo (see the design doc's Open Questions). This script only gets the
  branch ready for that to push into once it exists.
- Touch whatever branch your deployment mechanism (e.g. the Git Pull
  add-on) is actually configured to pull from. Point that at something
  else entirely - this branch is a mirror/audit log, never a deploy
  source.

## Usage

    ./setup-live-state-branch.sh [path-to-config-repo] [branch-name]

Defaults: current directory, `live-state`. Idempotent - safe to re-run;
won't touch the branch if it already exists.
