#!/usr/bin/env bash
# Ensures a Home Assistant config repo's .gitignore excludes secrets.yaml
# and other sensitive/noisy files, and drops a secrets.yaml.example
# template next to it. Idempotent - safe to re-run.
#
# Usage: setup-gitignore.sh [path-to-ha-config-repo]   (default: .)

set -euo pipefail

TARGET="${1:-.}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES="$SCRIPT_DIR/templates"

if [ ! -d "$TARGET/.git" ]; then
  echo "error: $TARGET is not a git repository (no .git directory)." >&2
  echo "Run 'git init' there yourself first - this script won't do it for you." >&2
  exit 1
fi

GITIGNORE="$TARGET/.gitignore"
BEGIN_MARK="# --- ha-dev-tools: secrets & sensitive files (managed block) ---"

if [ -f "$GITIGNORE" ] && grep -qF "$BEGIN_MARK" "$GITIGNORE"; then
  echo "Managed .gitignore block already present in $GITIGNORE - leaving it as is."
else
  {
    echo ""
    cat "$TEMPLATES/gitignore.snippet"
  } >> "$GITIGNORE"
  echo "Added managed block to $GITIGNORE"
fi

EXAMPLE="$TARGET/secrets.yaml.example"
if [ -f "$EXAMPLE" ]; then
  echo "$EXAMPLE already exists - not overwriting."
else
  cp "$TEMPLATES/secrets.yaml.example" "$EXAMPLE"
  echo "Created $EXAMPLE"
fi

# The one thing a .gitignore can't fix after the fact: if secrets.yaml was
# ever committed, it's already in history regardless of what .gitignore
# says now.
if git -C "$TARGET" ls-files --error-unmatch secrets.yaml >/dev/null 2>&1; then
  echo "" >&2
  echo "WARNING: secrets.yaml is already tracked by git in this repo." >&2
  echo "Adding it to .gitignore does NOT remove it from history - anything" >&2
  echo "in it is still exposed to anyone with repo access. You need to:" >&2
  echo "  1. Rotate every credential that was ever in that file." >&2
  echo "  2. Remove it from history (e.g. 'git filter-repo --path secrets.yaml --invert-paths')," >&2
  echo "     force-push, and have any other clone re-clone rather than pull." >&2
  echo "This script deliberately does not do that for you - it's destructive" >&2
  echo "and rewrites history other clones depend on." >&2
fi

echo "Done."
