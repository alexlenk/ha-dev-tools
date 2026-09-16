#!/usr/bin/env bash
# Seeds the branch a future write_automation runtime hook would push
# before/after commits to. Kept deliberately separate from whatever
# branch your deploy mechanism (e.g. the Git Pull add-on) tracks - see
# docs/AUTOMATION_TESTING_DESIGN.md's "Live-state mirror" section and
# this directory's README.md for why those must never be the same
# branch, regardless of how tempting it is to just use `main` for both.
#
# This script only creates/seeds the branch and pushes it - it does not
# configure any credential, and does not touch the live instance. See
# README.md for what's deliberately manual and why.
#
# Usage: setup-live-state-branch.sh [path-to-config-repo] [branch-name]
#        (defaults: . and live-state)

set -euo pipefail

TARGET="${1:-.}"
BRANCH="${2:-live-state}"

if [ ! -d "$TARGET/.git" ]; then
  echo "error: $TARGET is not a git repository." >&2
  exit 1
fi

cd "$TARGET"

if ! git rev-parse --verify -q HEAD >/dev/null; then
  echo "error: $TARGET has no commits yet - nothing to branch from." >&2
  echo "Get an initial commit onto its default branch first." >&2
  exit 1
fi

CURRENT_BRANCH="$(git symbolic-ref --short -q HEAD || true)"

if [ "$CURRENT_BRANCH" = "$BRANCH" ]; then
  echo "Already on '$BRANCH'."
elif git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo "Local branch '$BRANCH' already exists - not touching it."
else
  git checkout -b "$BRANCH"
  echo "Created local branch '$BRANCH' from '$CURRENT_BRANCH'."
fi

cat <<EOF

Branch ready. Deliberately manual next steps - none of this is
something a script should do on your behalf:

  1. On github.com, create a fine-grained personal access token scoped
     to ONLY this repo: 'Contents: Read and write', nothing else - no
     'workflow' permission, no admin, set an actual expiration date.
  2. Configure that credential on whatever will actually run the push
     (the live instance's own process, once the write_automation hook
     that uses it exists) - never write it into a file this script or
     git tracks.
  3. Confirm whatever polls for deployment (e.g. the Git Pull add-on) is
     pointed at a DIFFERENT branch than '$BRANCH'. This branch is a
     mirror/audit log only, never a deploy source.

Once a credential is configured somewhere that can push, seed the
remote with:
  git push -u origin $BRANCH
EOF
