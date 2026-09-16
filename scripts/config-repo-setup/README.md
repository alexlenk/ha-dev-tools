# HA config repo security setup

Scripts to bootstrap a Home Assistant *configuration* repo (a separate
repo from this one - your `automations.yaml`/`packages/` tree, e.g. an
`ha-installation-*` repo) with the baseline hygiene discussed in
[docs/AUTOMATION_TESTING_DESIGN.md](../../docs/AUTOMATION_TESTING_DESIGN.md).

They're standalone shell scripts with no dependency on this integration at
runtime - copy them, or run them straight out of a clone of this repo,
against any HA config repo.

## Why these exist

Branch protection / rulesets - the server-enforced "no direct push to
main, require review" control - is gated behind GitHub Pro (or making the
repo public) for private repositories. Confirmed directly against the
GitHub API for a private repo on the Free plan:

> "Upgrade to GitHub Pro or make this repository public to enable this
> feature."

If you're not paying for Pro and want to keep the repo private (the
common case for a HA config - it reveals your entity inventory, network
layout, presence patterns), there's no server-side enforced gate
available. These scripts are the best available compensating controls in
that situation, not a full replacement for one - see each script's own
caveats below before relying on it.

## Scripts

- **`setup-gitignore.sh [path]`** - ensures `.gitignore` excludes
  `secrets.yaml` and other sensitive/noisy files, and drops a
  `secrets.yaml.example` template next to it. Warns (doesn't auto-fix) if
  `secrets.yaml` is already tracked by git - fixing that needs history
  rewriting plus rotating every credential that was ever in the file,
  which is destructive and deliberately out of scope for an idempotent
  setup script.
- **`install-pre-push-guard.sh [path]`** - installs a local git hook that
  refuses `git push` directly to `main`/`master` unless you explicitly set
  `HA_DEV_TOOLS_ALLOW_DIRECT_PUSH=1`. Client-side only: it protects you
  from your own muscle memory, not from a compromised or malicious push
  from elsewhere - it's bypassable with `--no-verify` and only applies to
  clones that ran this script.
- **`setup-ci.sh [path]`** - scaffolds two GitHub Actions workflows into
  `.github/workflows/`: `secret-scan.yml` (gitleaks, catches a committed
  secret even if it slipped past `.gitignore`) and `validate-config.yml`
  (`yamllint` plus a real `check_config` run against
  `secrets.yaml.example`, since the real `secrets.yaml` is gitignored and
  never reaches CI).

All three take the target repo's path as an optional argument (default:
current directory), are idempotent, and never overwrite a file they
didn't create themselves.

## Usage

    ./setup-gitignore.sh ~/path/to/your-ha-config-repo
    ./install-pre-push-guard.sh ~/path/to/your-ha-config-repo
    ./setup-ci.sh ~/path/to/your-ha-config-repo

Then review the diff, commit on a branch, and open a PR as usual.
