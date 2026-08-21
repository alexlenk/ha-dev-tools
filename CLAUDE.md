# Working in this repo

## Release process

Merging a PR to `main` does **not** by itself create a release. The
`Auto Release` workflow fires on every push to `main`, but it only tags/
publishes a GitHub Release when `custom_components/ha_dev_tools/
manifest.json`'s `version` field is new - if it matches an already-tagged
version (e.g. unchanged from the last release), the workflow finds that
tag already exists and silently skips creating a new one. A feature/fix PR
merged without a version bump produces no visible release, no HACS update,
and no error - this is easy to miss.

So: every PR that should result in a release must bump the version itself,
as part of that same PR - not as a separate follow-up.

1. Bump `version` in `custom_components/ha_dev_tools/manifest.json`.
2. In `CHANGELOG.md`, move the relevant `## [Unreleased]` content under a
   new `## [X.Y.Z] - YYYY-MM-DD` heading (today's date), leaving
   `## [Unreleased]` empty above it for next time.
3. Default to a **patch** bump (`2.1.0` → `2.1.1`) - most changes here are
   fixes/small additions to existing tools. Only bump minor (`2.1.0` →
   `2.2.0`) or major when the change clearly warrants it (a new tool/
   capability, or a breaking change) - don't reach for minor/major by
   default.
4. Always via a PR (branch name doesn't need the `claude/release-` prefix
   unless you want `version-check.yml`'s enforcement, which only applies
   to that prefix anyway) - never push a version bump directly to `main`.
5. Merge once CI is green. `Auto Release` then tags and publishes
   `vX.Y.Z` automatically; HACS picks it up from there.

## PR workflow

Always open a PR for work on this repo, and merge it once CI is green -
don't leave a green PR waiting for a separate go-ahead.
