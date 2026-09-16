#!/usr/bin/env bash
# Scaffolds two GitHub Actions workflows into a Home Assistant config repo:
#   - secret-scan.yml    : gitleaks, runs on every push/PR
#   - validate-config.yml: yamllint + `hass --script check_config` (via
#                           frenck/action-home-assistant) against
#                           secrets.yaml.example, since the real
#                           secrets.yaml is gitignored and never reaches CI
#
# Idempotent - won't overwrite a file it didn't create; re-run after
# editing templates/workflows/ to pick up changes.
#
# Usage: setup-ci.sh [path-to-ha-config-repo]   (default: .)

set -euo pipefail

TARGET="${1:-.}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES="$SCRIPT_DIR/templates/workflows"
WORKFLOWS_DIR="$TARGET/.github/workflows"

if [ ! -d "$TARGET/.git" ]; then
  echo "error: $TARGET is not a git repository (no .git directory)." >&2
  exit 1
fi

mkdir -p "$WORKFLOWS_DIR"

for f in secret-scan.yml validate-config.yml; do
  dest="$WORKFLOWS_DIR/$f"
  if [ -f "$dest" ]; then
    echo "$dest already exists - not overwriting. Diff against $TEMPLATES/$f if you want the latest version."
  else
    cp "$TEMPLATES/$f" "$dest"
    echo "Created $dest"
  fi
done

if [ ! -f "$TARGET/secrets.yaml.example" ]; then
  echo "" >&2
  echo "WARNING: validate-config.yml points check_config at secrets.yaml.example," >&2
  echo "which doesn't exist yet in $TARGET. Run setup-gitignore.sh first, or" >&2
  echo "create it yourself, or this workflow will fail on every run." >&2
fi

echo "Done. Commit .github/workflows/ on a branch and open a PR to see them run."
