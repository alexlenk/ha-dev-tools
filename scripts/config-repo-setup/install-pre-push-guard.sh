#!/usr/bin/env bash
# Installs a local pre-push hook that refuses a direct push to main/master
# unless HA_DEV_TOOLS_ALLOW_DIRECT_PUSH=1 is set.
#
# This is NOT a substitute for server-enforced branch protection - it's a
# client-side guardrail only (bypassable with --no-verify, or from any
# other clone/machine that hasn't had it installed). GitHub's real branch
# protection / rulesets are gated behind GitHub Pro for private repos -
# see this directory's README.md for how that was confirmed. This is the
# best available guard without paying for that or making the repo public.
#
# Usage: install-pre-push-guard.sh [path-to-ha-config-repo]   (default: .)

set -euo pipefail

TARGET="${1:-.}"
HOOKS_DIR="$TARGET/.git/hooks"

if [ ! -d "$HOOKS_DIR" ]; then
  echo "error: $TARGET is not a git repository (no .git/hooks directory)." >&2
  exit 1
fi

HOOK="$HOOKS_DIR/pre-push"

if [ -f "$HOOK" ] && ! grep -qF "ha-dev-tools: direct-push guard" "$HOOK"; then
  echo "error: $HOOK already exists and wasn't installed by this script." >&2
  echo "Merge it by hand - refusing to overwrite a hook that might do something else." >&2
  exit 1
fi

cat > "$HOOK" <<'HOOK_EOF'
#!/usr/bin/env bash
# ha-dev-tools: direct-push guard
# Refuses a push that would land commits directly on main/master.
# Deliberate bypass, once: HA_DEV_TOOLS_ALLOW_DIRECT_PUSH=1 git push ...
protected='^(refs/heads/)?(main|master)$'
while read -r local_ref local_sha remote_ref remote_sha; do
  if [[ "$remote_ref" =~ $protected ]]; then
    if [ "${HA_DEV_TOOLS_ALLOW_DIRECT_PUSH:-0}" != "1" ]; then
      echo "" >&2
      echo "Refusing to push directly to '$remote_ref'." >&2
      echo "Push a branch and open a PR instead." >&2
      echo "Deliberate direct push: HA_DEV_TOOLS_ALLOW_DIRECT_PUSH=1 git push ..." >&2
      exit 1
    fi
  fi
done
HOOK_EOF

chmod +x "$HOOK"
echo "Installed pre-push guard at $HOOK"
echo "Note: this only protects pushes made from this clone, with hooks enabled."
