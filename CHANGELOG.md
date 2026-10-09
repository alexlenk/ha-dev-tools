# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.26.2] - 2026-10-09

### Fixed
- **`merge_statistics` (2.26.1) into a meter with no hourly statistics yet made its next hour drop by the whole merged total** (issue #141). That is a target entity only minutes old, with at most a few 5-minute rows at `sum` 0. The merge only shifted a target's sum basis when it had hourly rows, so the 5-minute rows stayed at 0, and the next compile continued from 0. The hourly rows were correct, but the next hour showed a `change` of minus the merged total.
  - Such a target now gets the merged total added from its first 5-minute row's hour (or the current hour), by HA's own sum adjustment, which also shifts any compile row written in the meantime.
  - A target with no 5-minute row at all gets one, carrying the merged sum and its own meter reading.
  - Targets that already had hourly rows were not affected. Reproduced with HA's own compile: the meter continued from 2.0 / 0.0 instead of 5 before the fix, and from 5 after.
  - **Who can be affected:** 2.26.1 merges whose target had no hourly statistics yet.
  - **How an agent checks:** call `get_statistics` on the target with `period="hour"` and `types=["sum", "change"]`, from the hour of the merge on. It was hit if the first hour after the merge shows a negative `change` of about the merged total, with `sum` back near 0.
  - **How to repair it:** `restore_statistics` with the backup that merge made (`ha_dev_tools:backup_<target>_<time>`, named in its result and listed by `list_statistics source=ha_dev_tools`), then merge again with this version. Alternatively, "Adjust sum" in Developer Tools > Statistics on that hour, by the drop.
- `merge_statistics`, `migrate_statistics` and `restore_statistics` results carry `next_compile`. It compares the target's newest 5-minute sum, which the next compile continues from, with its last hourly sum, and gives `ok: false` with the drop if they don't line up. A failure like #141 shows up in the result instead of an hour later.
- A statistics write that outlasted the MCP client's timeout came back as an error while it was still happening (issue #141). After 40 s, `clear_statistics` / `migrate_statistics` / `merge_statistics` / `restore_statistics` now answer `still_running`, with the backups already made. The write carries on and posts a Home Assistant notification with its result.
- `trigger_automation` no longer cancels the automation when the client times out (issue #131). The run was awaited (`blocking=True`), so a client giving up after its timeout cut the automation, and the scripts it waited on, off part-way through.
  - The run is now left to finish on its own, like the UI's "Run actions" button. The tool returns at once with `finished: false` and the run's `context_id`.
  - `wait_seconds` (up to 50) waits for a short run and reports its error, without ever cancelling it.
  - `set_number_value` / `set_boolean_value` are unaffected: they only wait for the helper's own service, and automations triggered by the change run separately.

### Added
- `update_template_entity` can change a trigger-based block's `triggers`, `conditions`, `variables` and `actions` in place (issue #140). Before this, only delete + create could do it, and that loses the state of entities built on their previous state (accumulators, counters, rolling attributes).
  - Each key is replaced in place, keeping the singular name an older block uses. `null` removes conditions, variables or actions.
  - Changes are checked with HA's own validation before anything is written.
  - The preview lists every entity in the block, since all of them change, and shows the keys before and after.
  - A reload keeps a trigger-based entity's state and attributes.
  - Turning a state-based block into a trigger-based one, or removing the triggers, is refused.

### Changed
- Tests run against HA 2026.10.0. Tool and flow schemas are typed as probatio's, which HA 2026.9+ uses (issue #138). There's no runtime change: HA's `voluptuous` there hands out the same objects.

## [2.26.1] - 2026-10-09

### Fixed
- `create_derived_sensor` / `update_derived_sensor` crashed on HA 2026.10 with "Object of type _Unsupported is not JSON serializable" on every flow's first step, for all derived-sensor domains, so none could be created (issue #137). The #80/#81 fix looked up probatio's `to_field_list` on HA's `config_validation`, which re-exported it on 2026.9. On 2026.10 it doesn't, so the lookup silently fell back to `voluptuous_serialize`, which doesn't recognize probatio's `UNSUPPORTED` sentinel. The converter is now imported from probatio itself, as HA's own config-flow HTTP view does. A regression test walks the first step of every domain with a real recorder (for `filter`), and passes on HA 2026.8, 2026.9 and 2026.10.
- **`migrate_statistics` (2.25.0) could restart the migrated meter's sum at 0** (issue #136). A meter's 5-minute statistics compile continues its running `sum` from the newest *5-minute* row only, never from the hourly ones. Moving a series takes its 5-minute rows along, but HA deletes those after `purge_keep_days` (10 days by default). So if the old entity had stopped more than about that long before the migration, the replacement's next compile found no 5-minute row and started again at `sum` 0. From the following hour, the Energy dashboard shows one large negative value, and all later hours sit on a total that is too low by the old sum. Hourly rows from before the migration are unaffected. `migrate_statistics` now writes one 5-minute row carrying the moved series' last sum and state, and the entity continues from that. A regression test runs HA's own compile and fails (sum 0.0) without the fix.
  - **Who can be affected:** only 2.25.0 migrations onto a meter (`has_sum`) statistic whose old entity had stopped more than about 10 days (your `purge_keep_days`) before the migration ran. A same-day replacement still had its 5-minute rows and wasn't affected.
  - **How an agent checks a migration:** call `get_statistics` with `statistic_ids=[<to_statistic_id>]`, `start_time` an hour or two before the migration, `period="hour"` and `types=["sum", "change"]`. It was hit if the first hour or two after the migration show a negative `change` of about the whole previous `sum`, with `sum` dropping to near 0 and then growing again from there. A meter's `change` is otherwise never strongly negative. HA's own statistics validation (`list_statistics` `issues`) doesn't flag this.
  - **How to repair it:** in Developer Tools > Statistics, use "Adjust sum" on that statistic, pick the hour with the negative `change`, and adjust it by that drop as a positive number. That shifts it and every later hour back up, and `get_statistics` should then show a continuous `sum`. 2.25.0 made no in-recorder backup, only a mirror-repo copy of the series it replaced, so this is the way to repair it.

### Added
- `merge_statistics` combines one or more source statistics into a target's series (issue #136). HA itself keeps one series per id and can't combine two. Typical uses: several old meters that became one, or a replacement that ran in parallel before the old device was retired.
  - Meters are rebuilt from every series' per-hour changes, so the sum is continuous across seams. The target keeps its own `state`.
  - A live target goes on recording from the merged sum. The rows from the point where its offset to the old sums becomes constant are written on the old basis, then shifted with HA's own sum adjustment. That moves the hourly and 5-minute rows together, in one recorder transaction, including any row a compile wrote meanwhile.
  - Overlapping hours follow `overlap`: `refuse` (the default), `target_wins`, `source_wins` (sources in the order given) or `add` (meters only). `start`/`end` limit what's taken from the sources.
  - Units are converted within one class (Wh/kWh), and sum/mean kinds must match.
  - The preview shows each series, the overlaps and their resolution, every seam (gap hours, state jump), the inputs' total change vs the result's, and the sum shift. Sources are left as they are.
- In-recorder backups and `restore_statistics` (issue #136). Before `clear_statistics`, `migrate_statistics` (onto an existing series), `merge_statistics` or `restore_statistics` change a series, it is copied into a backup statistic of this integration's own: `ha_dev_tools:backup_<id>_<UTC time>`, source `ha_dev_tools`, named "Backup of <id> before <operation> <time>".
  - It isn't an entity, so it shows up neither in entity pickers nor the Energy dashboard. It is visible in Developer Tools > Statistics and in statistics-graph cards, and it's included in every regular HA backup.
  - The copy is checked row by row count before the write goes ahead. A failed copy refuses the write, as does a failed mirror push. `allow_no_backup` skips only the mirror copy, never the in-recorder one.
  - Results name each backup. `restore_statistics` puts one back onto the statistic it backs up, or onto `target_statistic_id`, after backing up what it overwrites, so a restore can be undone too. A restored meter gets a 5-minute row with its last sum.
  - Only hourly rows are copied and restored. HA imports only hourly rows, and keeps 5-minute ones for about 10 days.

### Changed
- `list_statistics` shows backups (`source=ha_dev_tools`) with what they back up, the operation, when, their age and `stale: true` after 90 days. Backups are never deleted automatically; `clear_statistics` removes them, without making a backup of the backup.

## [2.26.0] - 2026-10-09

### Added
- `list_traces` and `get_trace`: read automation and script traces, the automation editor's Traces view.
  - `list_traces` lists the stored runs, newest first: trigger, start/finish, how each run ended (`script_execution`), last step and any error. Filter by automation/script (`entity_id`, or `domain` plus `item_id`), `errors_only`, and `include_not_triggered`.
  - `get_trace` reads one run (the latest by default) step by step, in the order it ran: each trigger, condition and action with its result, error, template errors and changed variables (`include_variables`, on by default). A step that ran a script carries a `child_id` to read that script's run. `include_config` adds the config as it was at the time of the run.
  - Both are read-only and use Home Assistant's own `trace/list` and `trace/get` commands, which are admin-only. The trace debugger (breakpoints, stepping) is not offered, since it pauses live runs.

## [2.25.0] - 2026-10-08

### Added
- `clear_statistics` deletes long-term statistics, as Developer Tools > Statistics' "Fix issue" > Delete does (issue #134). The preview shows each series' source, unit, whether its entity exists, first/last period and row count.
  - It refuses series that an existing entity still records into (`allow_live`) or that the Energy dashboard uses (`allow_energy`).
  - The series is first pushed to the mirror repo, in `recorder/import_statistics` shape, so a mistake can be imported back. Nothing is cleared if that push fails, and with mirroring off it needs `allow_no_backup`.
- `migrate_statistics` moves a dead entity's statistics onto its replacement, so the replacement keeps its new name and continues the old series (issue #134).
  - The hours the replacement had collected are backed up and replaced, since HA can't merge two series.
  - Units and sum/mean kind must match.
  - The result reports the gap since the last period and the jump to the entity's current state.

### Changed
- The `update_entities` preview warns when a rename's new entity_id already has statistics (issue #134). HA won't move the entity's history onto it, so the entity silently continues the other series. The warning names `migrate_statistics` as the fix.

## [2.24.1] - 2026-10-07

### Changed
- Renamed the display name from "HA Dev Tools" to "HA Dev Tools MCP" (in HACS, the integration list and the setup dialog), so the integration turns up when people search for an MCP server. The integration domain (`ha_dev_tools`), the `dev_tools` API and the `/api/mcp/dev_tools` endpoint are unchanged, so existing installs and MCP client configurations keep working. Existing config entries keep their current title.
- README now leads with "MCP server". The HACS install steps no longer go through a custom repository, since the integration is in the default HACS list.

### Fixed
- Removed `pyyaml` from the manifest's requirements. Home Assistant already ships it, and newer hassfest versions reject a custom integration that lists one of Home Assistant's own dependencies.

## [2.24.0] - 2026-09-30

### Added
- Labels and categories, without new tools (issue #128):
  - `list_helpers` / `create_helper` / `update_helper` / `delete_helper` take `domain: label` and `domain: category`. A category needs a `scope` (`automation`, `script`, `scene` or `helpers`), and its id is `<scope>/<category_id>`.
  - Writes to either are mirrored as `labels.json` / `categories.json`, like `areas.json` for rooms.
  - `update_entities` sets them: `labels` (the whole set), `add_labels` / `remove_labels`, and `categories` (`{scope: category}`, `null` clears a scope), all resolved by exact name or id. Devices take labels too.
  - An unknown label or category refuses the whole batch and lists what exists.

## [2.23.2] - 2026-09-30

### Fixed
- A batch `delete_template_entity` (`unique_ids`) could leave a package half-edited and unmirrored (issue #127). Each id was deleted separately, with a file write and a full template reload per id. Ten ids were slow enough for the client to time out, which cancelled the call partway: in the reported case 7 of 10 entities were gone, and no mirror commit recorded it.
  - All removals are now computed in memory, each file is written once, and templates are reloaded once.
  - Once validation has passed, the write, reload and mirror run to completion even if the client gives up, so the change is never half-applied or missing from git.
  - If a write fails partway through a multi-file batch, the files already written are still reloaded and mirrored, and the response lists what was deleted, what wasn't, and why.
- Mirror results now say plainly when the live file had changed since its last mirrored copy (`drift`). That change is recorded as its own "live state ... before write" commit, so it isn't mistaken for the current write.

## [2.23.1] - 2026-09-29

### Fixed
- `create_template_entity` wrote `triggers` into the YAML as quoted strings, producing a trigger block HA silently rejected (issue #125). The parameter was declared as an untyped list, which MCP clients are shown as a list of strings, so trigger objects arrived JSON-encoded. The entity then never came up, while the tool still reported `reloaded: true`.
  - `triggers` is now declared as a list of objects.
  - Triggers are checked with HA's own trigger validation before anything is written, in the preview, the dry-run and the write. A string, an unknown trigger type or a missing `trigger` key refuses the call and leaves the file unchanged.
  - Valid triggers are written exactly as given.
  - After the reload, the result reports the new `entity_id`, or a `warning` if the entity didn't come up.
- `update_template_entity` gets the same check: an edit HA rejects on reload used to drop the entity just as silently. The check also handles an entity in a block with its own `unique_id`, which HA registers as `<block>-<entity>`, so it doesn't warn falsely there.
- `write_energy_config`'s `energy_sources`, `device_consumption` and `device_consumption_water` had the same untyped-list declaration and were shown to clients as lists of strings. They're now lists of objects as well.

## [2.23.0] - 2026-09-29

### Added
- `list_statistics` and `get_statistics`: read-only access to the recorder's long-term statistics (issue #123). The Energy dashboard and long-term graphs run on these, and HA keeps them indefinitely, also for entities deleted long ago and for external statistics that were never entities (e.g. `tibber:energy_consumption_<home_id>`). Previously they could only be looked up by hand in Developer Tools → Statistics.
  - `list_statistics` gives each statistic's source, unit and sum/mean, plus:
    - `has_entity`: `false` means an orphan whose entity is gone; `null` means an external statistic.
    - `first_period` / `last_period`: shows whether a source is still fed, before calling two Energy sources duplicates.
    - `issues`: the problems HA's own validation reports for it (the "Fix issue" list, e.g. no longer recorded, units changed). This is folded in rather than added as a separate tool.
    - Filters: `search`, `statistic_type`, `source`, `unit`, `orphaned_only`, `issues_only`.
  - `get_statistics` reads rows for one or more ids by period (5minute to month), with a choice of columns and unit conversion. Rows come oldest first, up to a limit per id. A cut series returns `next_start_time` to continue from, so a year of hourly data can be read in pages, e.g. to check a history migration for a continuous `sum`. An unknown id is reported as `known: false` rather than silently empty.
  - Both use the recorder's own query functions. Importing, adjusting or clearing statistics is deliberately not offered.

## [2.22.2] - 2026-09-28

### Added
- `get_config_file` takes an optional `key`: it returns just that top-level block, e.g. `irrigation_unlimited:`, with its file and line range (issue #15). This is how an installed integration's own YAML config gets read without knowing which file holds it. With no `path`, every covered file is searched; with a `path`, only that file is. The block is found by scanning the text, so it also works in a file that no longer parses. Each block is checked for credentials on its own, so a password elsewhere in the same package no longer hides the block you asked for. A block holding a literal credential is withheld and reported with the line and key. Editing such blocks is deliberately not supported.

## [2.22.1] - 2026-09-28

### Security
- Mirroring a tool's write now checks for credentials inside strings too (issue #119). Previously only a credential-shaped key with a plain value was caught. So a `rest_command` payload like `'{"password": "..."}'`, a password in a URL (`https://user:pass@host`), an `Authorization: Bearer ...` header or a commented-out credential was pushed to the mirror repo as-is. Every mirror push, whether a write, a dry-run or a snapshot, now uses the same text-level check that config snapshots use (#105), in addition to the structured one. A flagged file is skipped and reported with the line and key, never the value. `true`/`false` values no longer count as credentials, so a flag like `show_token: true` doesn't block mirroring.

### Changed
- Tier-2 behavioral simulation (issue #37) is marked not planned in `docs/AUTOMATION_TESTING_DESIGN.md`, with the reasons why.

## [2.22.0] - 2026-09-28

### Added
- `update_entities`: change entities and devices the way their settings dialogs in the UI do, several in one call (issue #117). The only new tool.
  - For entities: rename the entity_id, set name, room (area), `device_class` (e.g. show a binary_sensor as a door or window), icon, disabled and hidden, and "Show as" a switch as a cover/fan/light/lock/siren/valve. "Show as" runs HA's own `switch_as_x` flow, which also hides the switch.
  - For devices: name, room and disabled.
  - Changes go through HA's own entity and device registry commands, so HA's own rules apply. For example, enabling an entity of a disabled device is refused, and enabling one reloads its integration after a short delay, as in the UI.
  - Every item is checked before anything changes. An unknown entity, device or room, a taken entity_id, or a field that doesn't apply refuses the whole batch and lists every problem.
  - Rooms are matched exactly, by name or id. A room that doesn't exist yet is created first (below).
  - A rename doesn't change the old id where it's used, and HA itself only follows renames for a few single-source helpers. So the preview lists every reference: YAML config, storage dashboards, persons' device trackers, and helper config entries.
  - With `update_references: true`, the writable references are rewritten: `automations.yaml`, `scripts.yaml`, `packages/`, storage dashboards and persons. Only the id token changes, so formatting and comments stay. Changed YAML is then reloaded, and anything still naming the old id is reported.
  - Mirrored as a before/after snapshot of the changed registry entries, plus each rewritten file.
- The helper tools now cover rooms and persons, with no new tools (issue #117):
  - `domain: area` lists, creates, renames and deletes rooms through HA's area registry.
  - `domain: person` does the same for UI-made persons, e.g. `update_helper` with `device_trackers` to link trackers to a person.
  - Areas are mirrored as the area list (`areas.json`). Persons aren't mirrored: that's personal data, which is why `.storage/person` is denylisted.

## [2.21.0] - 2026-09-28

### Added
- Hand-edited config is now mirrored (issue #105). With git mirroring on, `configuration.yaml`, every file it (or those files) `!include`s or `!include_dir_*`s, `packages/**/*.yaml` and `custom_templates/**/*.jinja` are committed to the mirror repo as-is, one commit per changed file. This happens at Home Assistant start, once a day, and on every `check_config`, but only while Home Assistant's own config check passes. The mirror's latest copy is therefore always a last-known-good one, and edits made over SSH or by hand show up as diffs. Previously only files touched by a write tool were ever mirrored, so `configuration.yaml` never was. `check_config` reports what it committed or skipped as `config_snapshot`.
- `get_config_file`: the raw text of any of those files, with `!secret`/`!include` returned as written and never resolved (issue #105). `source: mirror` returns the last good snapshot instead of the live file. It reads a file that no longer parses, where `get_rest_command` and the other structured tools can't, so a broken block can be seen and repaired without SSH. Without `path`, it lists the files it covers and why each is included. Read-only.
- Snapshots and `get_config_file` release a file only if no line of it looks like a literal credential: a credential-shaped key with a value that isn't a `!secret`/`!env_var` reference, a password in a URL, or a bearer token. Comments and strings such as a JSON `payload` are checked too, and so is a file that no longer parses. A withheld file is reported with the line and key, never the value. `secrets.yaml` is never returned or mirrored.

### Security
- A `secrets.yaml` in a subfolder (e.g. `packages/secrets.yaml`) is now denied like the top-level one. Home Assistant looks `!secret` values up in every folder from the including file up to `/config`, so these hold real secrets too, and their keys are arbitrary names no credential check would recognize.

### Changed
- `custom_templates/**/*.jinja` is readable by default (read-only), and `.jinja`, the extension Home Assistant loads templates from, is an allowed file extension (`.jinja2` already was).

## [2.20.14] - 2026-09-28

### Fixed
- `write_automation`, `write_script` and the template-entity writers no longer refuse to write a file that uses Home Assistant's own YAML tags anywhere in it (issue #115). A package with `password: !secret ...` in a `rest_command` next to the automation being edited failed its confirmed write with "could not determine a constructor for the tag '!secret'", and the same happened for `!include`, `!include_dir_*`, `!env_var` and `!input`. Validation now accepts exactly the tags HA's own loader knows, without resolving them (no secrets or included files are read), so a typo like `!secrets` is still rejected. The tags are kept verbatim in the written file.
- Editing an automation, script or template entity keeps its nested lists in the file's own style (issue #115). In a file written `triggers:` / `  - trigger: ...` (dashes indented under their key, common in hand-written YAML), the edited item's nested lists came back unindented, so a one-word change still showed up as a reflow of the whole item. The re-rendered item now uses the same dash indentation as the original, and a newly added item follows the file's style.

## [2.20.13] - 2026-09-28

### Added
- `delete_helper`, `delete_derived_sensor` and `delete_template_entity` can delete several items in one call, like `delete_automation` in 2.20.12 (issue #66). Pass `item_ids`, `entry_ids` or `unique_ids` instead of the single id. One confirmation covers the batch, and every id is checked before any item is deleted: helper ids against HA's current list, derived-sensor entries by lookup, template unique_ids by resolving their location. An unknown or repeated id refuses the whole batch. Mirroring pushes one commit per storage file or per template file for the batch (one per entry for derived sensors). No new tools.

## [2.20.12] - 2026-09-28

### Added
- `delete_automation` can delete several automations in one call (issue #66). Pass `automation_ids` (a list) instead of `automation_id`: one confirmation covers the batch, instead of a propose/confirm pair per automation. Every id is resolved before anything is written, so an unknown, ambiguous or repeated id refuses the whole batch rather than applying part of it. Each affected file is written once, with only the removed automations' lines taken out, and automations reload once. When mirroring is on, each file gets its own mirror commit. `expected_hash` is accepted only when every id is in the same file. No new tool.

## [2.20.11] - 2026-09-28

### Added
- `reload_domain` can now reload integrations set up through the UI (issue #14), e.g. a cloud or local-push integration whose connection is stuck, which previously needed a full HA restart. These have no `<domain>.reload` service, so their config entry is reloaded instead, the same as the UI's "Reload". A domain with several entries lists them with their state, and you pick one with the new `entry_id` parameter. `ha_dev_tools` and `mcp_server` are never reloaded, since that would drop the calling session. No new tool.
- `check_config` returns `config_entry_problems`: UI-set-up integrations whose setup failed or is being retried, whose migration or unload failed, or that are waiting for the user to re-authenticate, each with HA's own reason. Re-authentication needs the user's credentials, so it's surfaced rather than started. HA's own Repairs entry for it also appears in `repairs`.

## [2.20.10] - 2026-09-28

### Added
- `audit_automations` now lints templates and conditions (issue #36; built into the existing tool rather than a new `lint_automation` tool):
  - `template_errors`: every template string in an automation is compiled with HA's own template engine, so a syntax error shows up with HA's error and its path (e.g. `actions[0].data.message`) instead of only when the automation fires.
  - `constant_conditions`: conditions that always or never pass, i.e. `{{ true }}`/`{{ false }}` template conditions (including the shorthand and YAML booleans) and an empty `and`/`or`.

  The design doc's more heuristic rules (`choose` without `default`, a condition on an entity no trigger supplies, possible feedback loops) are deliberately not implemented. They're routinely intentional, so they'd mostly be noise. HA-schema validation of each automation is already covered by `check_config`'s `setup_failures`.

## [2.20.9] - 2026-09-28

### Fixed
- Writes no longer rewrite the whole YAML file (issue #53). Every write used to re-dump the entire document, and ruamel's dump isn't byte-for-byte faithful. It re-indents lists, re-joins long lines an editor wrapped, and collapses extra spaces (`mode:   single`), all in items the write never touched. A one-automation edit could produce a 269-line mirrored diff. Now only the edited item's own lines change. This covers `write_automation`, `delete_automation`, `write_script`, and creating, updating or deleting template entities, where a delete removes the entity, or its platform key or block when it empties them. Every other byte of the file stays as it was. As a safety net, the spliced result is only used if it parses to exactly the same data as a full re-dump; otherwise the full re-dump is written as before.
- A new automation is now written with `id` as its first key, the way HA's own editor writes it, instead of last.

### Changed
- CI's `pip install` steps retry for longer (`--retries 10 --timeout 60`). A PyPI read timeout during install failed a CI run before any test ran.

## [2.20.8] - 2026-09-28

### Added
- `shell_command:` config can now be read (issue #100). `get_rest_command` and `list_rest_commands` take an optional `domain` (`rest_command`, the default, or `shell_command`), with the same package-safe lookup across `configuration.yaml` and `packages/*.yaml`. A shell_command's config is returned as its command string. Read-only, deliberately: shell_command runs raw shell commands. The issue asked for two new tools, but `shell_command:` uses the same layout, so the existing pair covers it with no new tools.

## [2.20.7] - 2026-09-28

### Added
- dev_tools' arm status is now visible without a write having to fail first (issue #63). `dev_tools_ping`, the one tool that works while not armed, returns `arm`. When armed, it shows `expires_at`, `minutes_left`, and the idle-window and 4-hour-cap expiry times. When not armed, it shows the command to run on the HA host. Every write tool's response also includes `arm`, so a long batch of writes sees expiry coming and doesn't fail partway through. No new tool.

## [2.20.6] - 2026-09-28

### Added
- `update_derived_sensor`'s confirmation step now shows the entry's `current_options` and `would_change`: each field the call would change, with `from` and `to` values (issue #86). Before, it only echoed the caller's own arguments, so whoever confirmed a partial update never saw what the entry held or what would actually change. Section fields are listed as dotted paths (e.g. `additional_options.availability`). A field set to `null` shows as cleared. It's computed without starting a flow. HA's own validation still runs on the real write. Any write tool can now add this kind of context to its confirmation step (`WriteGatedTool._preview_context`).

### Changed
- CI now tests the current Home Assistant release (2026.9.4) as well as the minimum from `hacs.json` (2026.8.2). A nightly workflow also tests the newest HA release (issue #84). Until now only 2026.8.2 was tested, so HA 2026.9's probatio change reached users before CI noticed. The test requirements are split into `requirements-test.txt` (current HA, the default), `requirements-test-ha-min.txt` and a shared `requirements-test-common.txt`. On 2026.9 the test env also needs `voluptuous-serialize` and `gazetteer-matcher`, which HA itself no longer pulls in. No runtime changes.

## [2.20.5] - 2026-09-28

### Added
- `check_config` now returns `repairs`: every active Repairs issue, with the title and description the Repairs page shows (issues #88, #102). Until now the only way to see why HA rejected something was to open the Repairs page by hand. No new tool.
- `write_automation` and `write_script` return `setup_error`: HA's own error if it refused to set up the item just written, read right after the reload (issue #102). E.g. `invalid time_pattern value at 'minutes'` for issue #101's comma list, which previously reported plain success.

### Fixed
- `check_config` reported `valid: true` for automations and scripts that HA parsed but refused to set up, e.g. `Invalid time specified: 61200` (issue #89). HA's config check doesn't fail on these; it loads them as unavailable entities and raises a Repairs issue instead. They're now listed in `setup_failures` (domain, id, entity_id, HA's error) and make `valid` false.

## [2.20.4] - 2026-09-28

### Fixed
- `get_rest_command` and `list_rest_commands` crashed with `Object of type TaggedScalar is not JSON serializable` when a rest_command used `!secret` (or another HA YAML tag), and one such command broke the whole listing (issue #90). `get_automation`, `get_script` and `list_scripts` crashed the same way on a `!secret` in an automation or script. Tags are now returned as their literal text (e.g. `"!secret doorbird_auth"`) and never resolved, so reads don't leak `secrets.yaml`. `get_template_entity` already did this. The conversion now lives in `yaml_style.to_json_safe`, shared by every read tool.
- `create_derived_sensor` and `update_derived_sensor` left Home Assistant config/options flows in progress whenever they stopped early: schema discovery (`needs_input`), rejected input, or a field that can't be edited (issue #85). Each call left an orphaned flow until HA restarted. The flow is now aborted whenever the tool stops before finishing.

## [2.20.3] - 2026-09-24

### Added
- `get_automation`, `get_script` and `get_template_entity` now return `misread_values`, the same check `audit_automations` gained in 2.20.2 (issue #97). It lists each unquoted value Home Assistant reads as a different type than written. `config` shows the intended text, e.g. `delay: 1:30`, but HA reads it as 90 seconds instead of 1½ hours, silently and with no Repairs entry. An agent now sees this on the item it's about to edit. Scripts and template entities had no check at all before. No new tools. `get_template_entity` reports paths without line numbers, because its read path converts `!secret`/`!include` tags to plain values.

## [2.20.2] - 2026-09-24

### Added
- `audit_automations` now reports `misread_values`: unquoted values that Home Assistant reads as a different type than written, e.g. `before: 17:00:00` read as the integer `61200`, or `state: off` read as `False` (issue #96). Each finding gives the automation, file, path (e.g. `conditions[1].before`), line, the text as written and what HA reads it as. 2.20.1 stopped `write_automation` from writing such values, but values already in a file stay broken until written again. Nothing pointed to them: `get_automation` shows the intended string and `check_config` reports the config as valid, while HA disables the automation. Writing the automation again with `write_automation` quotes the value.

### Fixed
- `delete_entities` with mirroring enabled could crash while building the backup snapshot if one of the entity ids had no registry entry. It now records `null` for that entity, the same way `delete_entity` already does. Found while fixing the type errors below.

### Changed
- CI now runs the same checks as `.pre-commit-config.yaml` (black, isort, flake8, mypy) in a new `lint` job (issue #93). None of them were enforced before, and `main` had drifted: mypy reported 33 errors, 10 files weren't black/isort-formatted, and one test had an unused import. All of that is fixed. No other behavior changes.

## [2.20.1] - 2026-09-24

### Fixed
- `write_automation`, `write_script` and the template entity tools wrote time strings like `"17:00:00"` unquoted. Home Assistant reads YAML with PyYAML (YAML 1.1), which reads an unquoted `17:00:00` as the base-60 integer `61200`, so a `time` condition written this way failed with `Invalid time specified: 61200` and the automation was disabled (found while investigating issue #91). ruamel.yaml, which writes these files, follows YAML 1.2 and only quotes strings that YAML 1.2 would misread. The quoting check used to be a fixed list of `on`/`off`/`yes`/`no`-style words. It now asks PyYAML's own resolver and quotes any new string it wouldn't read back as a string, which also covers `9:30`, `0x1F`, `2024-01-01` and similar values.
- `write_automation` reformatted every field of the automation it edited, not just the fields that changed (issue #91). Changing one condition's `before:` also turned an untouched `state: 'off'` two lines away into `state: "off"`. The caller's config replaced the loaded automation wholesale, so all of its original quote styles, comments and number formats (e.g. `0x10`) were lost. The loaded automation is now patched in place: values that didn't change keep their original YAML, and only changed, added or removed fields are rewritten. Two exceptions: an unchanged but unquoted value that Home Assistant misreads (e.g. `state: off`) is still re-quoted, and anchored (`&name`) or `<<:` merge-key nodes are still replaced wholesale, because patching them in place would also change every other automation that shares them.
- `write_script` and template entity updates (`update_template_entity`) had the same problem as `write_automation` above: editing one field reformatted every field of that script or entity (issue #92). They now patch the loaded item in place the same way. The quoting and merge helpers now live in one shared module (`yaml_style.py`) instead of a copy in each manager.

## [2.20.0] - 2026-09-24

### Fixed
- `update_derived_sensor` silently deleted every optional field the caller didn't restate. Changing only a template sensor's `state` dropped its `device_id`, which removed the entity's device link with no warning (issue #81). Home Assistant's options flow deletes any optional key missing from the submitted input; the UI never hits this because it pre-fills each form with the current values. Each step's input now starts from that step's current values (its schema's `suggested_value`s, including fields inside sections), with the caller's fields laid over it. Pass a field as `null` to clear it on purpose.
- `create_derived_sensor`/`update_derived_sensor` crashed with `Object of type _Unsupported is not JSON serializable` whenever they returned `needs_input` on Home Assistant 2026.9 or later. That broke schema discovery (issue #81) and also caused the `min_max` "confirm" crash in issue #80, where the wrong step id triggered a `needs_input` response that then crashed. In 2026.9, `cv.custom_serializer` moved to probatio, whose "unsupported" sentinel `voluptuous_serialize` doesn't recognise. The schema converter now matches whichever one `cv` itself uses.
- A field a derived-sensor step doesn't accept was rejected one at a time (`Schema validation failed at 'cycle'`, then `'offset'`, ...), so callers had to guess field by field (issue #80, `utility_meter`). The error now lists every field that step accepts. Most of `utility_meter`'s fields (`cycle`, `offset`, `tariffs`, ...) can only be set when the helper is created; its edit step accepts `source`, `periodically_resetting` and `always_available`.
- The derived-sensor tool descriptions now say that schema discovery (`steps={}`) returns `needs_input` on the confirmed call, not on the first propose call.

### Added
- `update_derived_sensor` accepts `options`, a flat patch of just the fields to change (e.g. `{"entity_ids": [...]}`). No step id is needed and every other field keeps its current value (issue #82). The patch still goes through Home Assistant's own options flow and validation, rather than writing the config entry directly as the issue first proposed, so it can't store options the UI would reject. A key that isn't editable after creation is rejected before anything is written, and the error lists the fields that are editable.

## [2.19.0] - 2026-09-23

### Added
- `list_dashboards` - `get_dashboard` could only read one dashboard at a time, by `url_path`, with no way to enumerate what dashboards exist on the instance at all. A real session hit this directly: a sweep for stale entity references checked the default dashboard via `get_dashboard` and reported clean, missing a second, non-default dashboard (`dashboard-solar`) entirely - only found because the user happened to paste its URL (issue #77). Goes through the frontend's own `get_panels` WS command rather than `lovelace/dashboards/list` (which only covers storage-mode dashboards) - every Lovelace dashboard of either mode registers a frontend panel with `component_name == "lovelace"`, which is the one place both modes show up together, matching `get_dashboard`'s own both-modes read coverage.
- `trigger_automation` / `set_number_value` / `set_boolean_value` - debugging Modbus writes (EMHASS's battery-schedule slot updates intermittently failing with `No response received after 3 retries`) needed calling a single HA service on demand to test one write in isolation, which this integration had no way to do. The workaround - a temporary automation just for one-shot `number.set_value` calls - went wrong concretely: its `time_pattern: minutes: "*"` trigger kept re-firing every minute (`mode: single` only blocks concurrent re-entry, not repeat firing), racing EMHASS's own legitimate write cycle on the same registers (issue #76). Deliberately not a generic `call_service` tool - that was considered and rejected as too broad a surface (could reach `lock.unlock`, `alarm_control_panel.disarm`, `backup.*`, `homeassistant.restart` on an instance with real physical devices). Instead, three narrow tools each wrap exactly one hardcoded service: `trigger_automation` only runs an automation already in reviewed config (same boundary `write_automation` draws); `set_number_value`/`set_boolean_value` are scoped to just the `number`/`input_number` and `input_boolean` domains, refusing everything else outright - in particular `switch` and any other domain that could be a real-world actuator, which would need their own separate risk review. All three go through the same arm+admin gate plus the same propose/confirm pattern as `write_automation`. See `docs/SECURITY.md`'s new "Scoped service-call tools" section for the full reasoning.

## [2.18.0] - 2026-09-20

### Added
- `get_energy_config` / `write_energy_config` - the Energy dashboard's own source config (grid consumption/return, solar production per source, battery in/out, gas/water sources, cost/compensation settings) is a separate HA subsystem from Lovelace dashboards, living in `.storage/energy` and read/written via its own `energy/get_prefs`/`energy/save_prefs` websocket commands - `get_dashboard`/`write_dashboard` never reached it (issue #74). `write_energy_config` mirrors `energy/save_prefs`'s own real semantics: each of `energy_sources`/`device_consumption`/`device_consumption_water`, if supplied, wholesale-replaces that section; omitting a field leaves it untouched rather than clearing it - a genuine partial update, unlike `write_dashboard`'s full-document replace.
- `list_rest_commands` / `get_rest_command` - `get_automation`/`get_script` already resolve which file (default file or a `packages/*.yaml`) actually defines an entry; `rest_command:` had no equivalent, so a package-defined `rest_command` was only readable by having the owner paste the file's contents manually (issue #73). Read-only by design - a write path needs its own safety review and is out of scope here. Structurally closest to `script:` (a mapping of id -> config), with one simplification: `rest_command:` has no dedicated default include-file the way `script: !include scripts.yaml` does, so `configuration.yaml` itself is always a read candidate, same as `template:` entities already are.

## [2.17.0] - 2026-09-18

### Added
- `diagnostics.py` - Home Assistant's standard "Download diagnostics" support (Settings → Devices & Services → HA Dev Tools → the three-dot menu). Reports integration version, whether dry-run/git-mirroring are enabled, and whether the arm gate is currently armed (read-only - downloading diagnostics never itself extends an active arm window). The git-mirroring access token is redacted; the mirror repository name is not, since it's not a secret.

### Fixed
- `mirror_secrets.py`'s credential scanner (gates what's safe to push to your git mirror) only ever recognized the literal `!secret` tag as safe by accident of an `isinstance` check ordering, not by actually checking the tag - any tag at all (a typo, `!env_var`, `!include`, ...) on a sensitive key was silently treated the same as a real `!secret` reference. No literal credential could actually leak this way (no YAML tag mechanism embeds a literal value directly), but the scanner's own documented intent - only `!secret` is the safe/unsafe signal - wasn't actually enforced. Fixed so only a genuine `!secret` tag is exempted; anything else on a sensitive key is now correctly flagged as unsafe to mirror.

### Changed
- Test coverage: 91% -> 99% overall, closing the gaps in `file_manager.py`, `llm_api.py`, `security.py`, `dashboard_manager.py`, `validation.py`, and a dozen smaller modules - mostly untested error/exception branches, several tool classes that had never been instantiated by any test at all, and one new `tests/test_init.py` (none previously existed). Updated `quality_scale.yaml` accordingly.

## [2.16.1] - 2026-09-18

### Fixed
- `read_file`'s `UnicodeDecodeError` handling was unreachable dead code - the broader `except (FileNotFoundError, PermissionError, ValueError):` clause was checked first, and since `UnicodeDecodeError` is itself a `ValueError` subclass, it always caught it first and re-raised the raw low-level decode error instead of the intended, friendlier "File encoding error: ..." message. Reordered so the specific clause runs first. Found and fixed while writing tests to close `file_manager.py`'s coverage gap (64% -> 98%).

### Added
- `custom_components/ha_dev_tools/quality_scale.yaml` - a self-declared, honest scorecard against Home Assistant's [Integration Quality Scale](https://www.home-assistant.io/docs/quality_scale/). This is a HACS-only custom integration and can never earn an official badge (hassfest's quality-scale validation only runs for integrations inside `home-assistant/core`, so it silently no-ops here despite hassfest otherwise running in this repo's CI) - this file exists purely to track real progress against the same bar, and lists the current genuine gaps (test coverage, `diagnostics.py`, `strict-typing`, and others) rather than a good-looking score.

## [2.16.0] - 2026-09-18

### Added
- `list_mqtt_topics` - a new capability area (first MQTT-aware tool), read-only. Found via a real triage session: a "ghost" entity can have a live state with no entity registry entry at all (no `unique_id`, so nothing to register) - `delete_entity`/`delete_entities` can't touch these, since there's no registry entry to remove. The most common real cause is a plain YAML-configured MQTT sensor whose device is gone but whose last retained message the broker still holds. MQTT has no "list retained messages" query - the only way to discover one exists is to subscribe to a topic filter and see what the broker immediately delivers (retained messages are always delivered synchronously right after a matching subscribe). `list_mqtt_topics` does exactly that: subscribes to a caller-supplied topic filter (default `homeassistant/#`, HA's own discovery prefix - explicitly documented as *not* where a plain MQTT sensor's state lives, so the caller can point it at the actual topic tree instead, e.g. `watermeter/#`) for a bounded window (default 3s, capped at 10s), and reports the last message per matching topic. Deliberately read-only - no publish/clear-retained capability yet; that's a materially different risk tier (MQTT can drive physical devices) that needs its own security-review pass, not folded into this by default.

## [2.15.0] - 2026-09-18

### Added
- `delete_entities` - a real gap surfaced by a real bulk cleanup: removing 55 stale entities left behind by a replaced device via `delete_entity` one at a time took ~110+ tool calls (a mandatory propose/confirm pair per entity), tens of thousands of tokens, and produced up to 110 separate mirror commits. `delete_entities` accepts a list of `entity_id`s under one propose/confirm pair - refuses to delete any of them if even one id doesn't resolve, rather than guessing which ones were meant (a typo partway through a long list shouldn't silently delete everything up to that point). If git mirroring is enabled, every entity's registry snapshot is pushed together as one combined before/after commit pair - not one pair per entity - so a large batch doesn't flood the mirror repository. `delete_entity` (singular) is unchanged and still the right tool for a one-off removal.

## [2.14.0] - 2026-09-18

### Added
- `delete_entity` - a real gap: entities registered by a removed/renamed device or integration had no supported way to clean up, short of hand-editing the denylisted `.storage/core.entity_registry`. Deletes through `EntityRegistry.async_remove` - the same in-process API HA's own UI uses. This is a soft delete on Home Assistant's own side (confirmed by reading `entity_registry.py` directly): the entry moves into the registry's own `deleted_entities` table, so HA reconnects it automatically with its old entity_id and customizations if the same integration re-registers it later; only truly orphaned entries (no owning config entry) get purged for good, after 30 days. If git mirroring is enabled, the entity's full registry snapshot (name, area, labels, options, ...) is pushed to a synthetic `entities/<entity_id>.json` path before removal, and a small deletion marker after - same tombstone pattern as `delete_derived_sensor` - so that data survives past HA's own 30-day window. Gets the full write-tool treatment (propose/confirm token gate); no dry-run preview mirror hook, matching every other non-file-backed write tool (helpers, derived sensors).

## [2.13.0] - 2026-09-18

### Added
- `delete_automation` - a real gap: `write_automation` could create/update but never delete, so removing an automation meant editing YAML by hand. Same layout-aware, package-safe pattern as `write_automation`/`write_script`: resolves which file actually defines the id first (default `automations.yaml` or a `packages/*.yaml` file), refuses to guess if it isn't found or is defined in more than one file, always reloads automations afterward. Gets the full write-tool treatment: propose/confirm token gate, dry-run preview, and dry-run + mirroring support (pushes to its own `proposed/automation-<id>` branch, same as `write_automation`).

## [2.12.0] - 2026-09-18

### Added
- Git mirroring now covers config-entry-based derived/calculated sensor helpers (issue #34's last remaining piece): `create_derived_sensor`/`update_derived_sensor`/`delete_derived_sensor` push the resolved `ConfigEntry`'s own `.data`/`.options` (the same shape `get_derived_sensor` already returns) - there's no real file to mirror, so this goes to a synthetic `derived_sensors/<domain>/<entry_id>.json` path in the mirror repo instead, never the raw `.storage/core.config_entries` file (shared by every integration on the instance, explicitly in `DEFAULT_DENYLIST`). Deleting an entry has no file to remove either, so it pushes a small `{"deleted": true, "entry_id": ...}` marker instead - the entry's last real config stays visible in the mirror repo's own git history right before that commit. `update`/`delete` only fetch the "before" snapshot when mirroring is actually enabled, avoiding an extra lookup (and its own not-found risk) on every call otherwise. No dry-run + mirroring support for these three (unlike `write_automation`/`write_script`/the template-entity tools) - there's no way to compute a "would-be" config entry without actually running the real config/options flow, so a true dry-run preview isn't possible the way it is for a plain YAML/JSON write.

## [2.11.0] - 2026-09-18

### Added
- Git mirroring now covers storage-defined helpers (issue #43): `create_helper`/`update_helper`/`delete_helper` push their `.storage/<domain>` before/after content the same as every other mirrored write tool. HA's generic helper storage collection debounces its own save 10 seconds (`helpers/collection.py`'s `StorageCollection._async_schedule_save()`), so reading the file again right after a write would almost always capture stale, pre-write content - worse than not mirroring at all for something meant to be a rollback source. Instead, the "after" content is reconstructed entirely in memory: the last-known "before" content (from `_read_storage_file`, read *before* the write) has the write's own known result (the WS command's returned item for create/update, just the deleted id for delete) spliced into its `data.items` list directly - never by reading the file again. Confirmed directly against `home-assistant/core` source that every helper storage file shares the same shape and that `"id"` (`CONF_ID`) is every item's identifier key uniformly, not domain-specific.

## [2.10.0] - 2026-09-18

### Added
- Script support (issue #42): `list_scripts`, `get_script`, `write_script` - the same layout-aware, package-safe pattern `get_automation`/`write_automation` already have, applied to `script:`. `scripts.yaml` (the default file, a bare mapping of script id -> config at its document root) or a specific `packages/*.yaml` file (a `script:` key holding that same mapping) - resolved before any read or write, always written through the same file found. `write_script` gets the full write-tool treatment: propose/confirm token gate, dry-run preview, and dry-run + mirroring support (pushes to its own `proposed/script-<id>` branch, same as `write_automation`). `scripts.yaml` moved from `DEFAULT_READ_ONLY_PATHS`-only to also being in `DEFAULT_WRITE_PATHS`, matching `automations.yaml`'s status.

### Fixed
- README's mirroring section still said mirrored writes went to the mirror repo's `main` branch, and dry-run's proposed branch was described as branching off `main` - both now say "the mirror repo's actual default branch", matching 2.9.3's fix below (not pushed with that release, folded in here instead).

## [2.9.3] - 2026-09-18

### Fixed
- Mirroring (`mirror.py`) hardcoded its push target to a branch named `"main"` (issue #50) - a mirror repo bootstrapped for another purpose, or one with a different default branch name, silently failed every real (non-dry-run) mirrored write, and only dry-run's `proposed/*` branching had a clear "branch missing" error for it. `mirror_write()`/`mirror_dry_run()` now resolve the mirror repo's actual `default_branch` via `GET /repos/{owner}/{repo}` on every call and target that instead.
- `README.md`'s logo `<img>` used a repo-relative path (`custom_components/ha_dev_tools/brand/icon.png`) - GitHub's own README viewer silently rewrites that to a raw URL, but HACS's own README renderer doesn't, so the logo showed as a broken image on the HACS store page. Switched to an absolute `raw.githubusercontent.com` URL.

### Changed
- README's badge row switched from shields.io's chunky `for-the-badge` style to its plain flat default, matching this author's other repos.

### Fixed
- **Safety-critical**: `write_automation` and the template-entity write tools serialized a brand-new plain string like `"off"`/`"on"`/`"yes"`/`"no"` unquoted, since ruamel.yaml's own resolver follows YAML 1.2 (only `true`/`false` are boolean-like there) and sees no reason to quote it. Home Assistant's own YAML loader follows PyYAML's default resolver (YAML 1.1 semantics), which reads an unquoted `off` back as the boolean `False` - so a state condition written as `state: "off"` came back from disk as `state: False`, failed schema validation ("expected str"), and silently disabled the whole automation. Confirmed live: a real safety automation (a lawn-mower off-limits containment automation gated on `state` conditions) was disabled this way immediately after a write. `automation_manager.py`/`template_yaml_manager.py` now force-quote any brand-new plain string that exactly matches one of PyYAML's implicit bool/null scalars (`yes`/`no`/`true`/`false`/`on`/`off` and case variants, plus `null`/`~`) before splicing it into the document - values already loaded from an existing file are untouched, since those already round-trip with whatever quote style they were written with.

## [2.9.1] - 2026-09-17

### Fixed
- The propose/confirm and dry-run response `note` fields only said "show this to the user," which a live test showed wasn't specific enough - an agent surfaced just the automation's id, not the actual `would_apply` config it was about to write. Both notes now say explicitly to show the full `would_apply` content, not just its id or a summary.

## [2.9.0] - 2026-09-17

### Added
- Dry-run mode + git mirroring, together, now actually push something (issue #35): `write_automation` and the three template-entity write tools resolve their would-be content the same way a real write would, then push it to its own `proposed/<kind>-<id>` branch (e.g. `proposed/automation-my_automation`) - freshly branched from the mirror repo's `main` HEAD on every dry-run, after still syncing `main` to the live-drift-detected "before" state first, matching `docs/AUTOMATION_TESTING_DESIGN.md`'s original design exactly. Before this, dry-run mode pushed nothing at all to the mirror repo, silently - easy to mistake for mirroring simply not being wired up for dry-run. `write_dashboard` has no way to compute its would-be storage JSON without performing the real write, so it's excluded (still mirrors nothing in dry-run); `create_helper`/`update_helper`/`delete_helper` remain excluded from mirroring entirely either way (issue #43).
- `AutomationManager.write_automation()` and `TemplateYamlManager.create_entity()`/`update_entity()`/`delete_entity()` all gained a `dry_run: bool = False` parameter - resolves location and builds the same content a live write would, but returns before ever calling `file_manager.write_file()` or reloading.
- `mirror.py` gained `mirror_dry_run()` (the proposed-branch push, with its own before-sync-to-main reuse via a new shared `_sync_before()` helper) and `proposed_branch_name()` (sanitizes an arbitrary automation id/unique_id into a git-ref-safe branch name). `MirrorResult` gained a `branch` field, populated only for a proposed-branch push, so the write tool's response can point at exactly which branch to look at.

### Fixed
- `mirror_enabled`/`dry_run`'s config option descriptions, corrected twice in one day: 2.8.5 first documented the (accurate at the time) fact that dry-run mode mirrored nothing; this release makes that no longer true for `write_automation`/template entities, so both descriptions - and README's Setup step 7 - are updated again to describe the `proposed/*` branch behavior instead of the now-outdated "nothing is mirrored" claim.

## [2.8.6] - 2026-09-17

### Changed
- Every write tool's `description` (write_automation, create/update/delete_helper, create/update/delete_derived_sensor, create/update/delete_template_entity, write_dashboard) now explicitly states the propose/confirm flow and `confirm_token`'s exact field name, instead of that only being documented in `WriteGatedTool`'s source docstring and each response's own free-text `note`. Found live: a caller guessed the confirm token belonged in `expected_hash` (a real, schema-declared field with a plausible-sounding purpose) instead, and got a silently-reissued fresh token with no indication `expected_hash` was the wrong field - confirmed against a real MCP client that `write_automation`'s served schema has neither a `confirm_token` key nor any `required` array at all, so a caller relying on the schema alone has no way to discover this. `description` is the one part of a tool's definition every MCP client reliably shows in full regardless of how it renders `parameters`/`inputSchema` - see issue #48 for the open question on whether that missing schema metadata is this integration's fault or a client/harness-side simplification.

## [2.8.5] - 2026-09-17

### Fixed
- `write_automation`/`create_template_entity`/`update_template_entity`/`delete_template_entity` did a blocking filesystem scan directly on the event loop - HA's own blocking-call detector caught it live: `Detected blocking call to scandir ... at automation_manager.py, line 46: yaml = YAML(typ="rt")`. Root cause: `hass.async_add_executor_job(_new_yaml().load, content)` evaluates `_new_yaml()` (which constructs a `ruamel.yaml.YAML` instance - its `__init__` does the scan) eagerly on the event loop, before ever handing off to the executor - only the already-bound `.load` method actually ran in a worker thread. New `_load_yaml()` helper builds the `YAML()` instance *inside* the executor job in both `automation_manager.py` and `template_yaml_manager.py` (6 call sites total - 3 of them introduced by 2.8.1's own `TemplateWriteResult` refactor, which had copied the same pattern).
- Clarified two config option descriptions that were stale/incomplete and caused real confusion during a live dry-run-mirroring test: `mirror_enabled`'s only ever mentioned `write_automation`, missing the template-entity tools and `write_dashboard` it's covered since 2.8.1/2.8.2; neither `dry_run` nor `mirror_enabled` explained that dry-run mode short-circuits before any write happens, so mirroring - which only ever pushes from inside the real write path - never has anything to push while dry-run is on. Not a bug in the mirroring logic itself, just an undocumented interaction between two independently-correct features.

## [2.8.4] - 2026-09-17

### Fixed
- `get_entity_history` and `get_logbook` crashed with a raw, unhandled `Error: 'start_time'` (a bare `KeyError`) when a caller omitted `start_time` - and `get_entity_history` had the same crash risk for a missing `entity_ids`. Both fields were already declared `vol.Required` in each tool's `parameters` schema, but that schema was never actually invoked anywhere to validate `tool_args` before `_run()` - it's only ever used as metadata for the tool's exposed JSON schema - so an omitted "required" field reached a direct `args["..."]` index instead of being rejected cleanly. New `_require()` helper raises a clear `ValueError` (already caught and turned into a normal tool-error payload) instead. Found live while testing v2.8.3, filed and fixed as issue #45.

### Added
- README's Setup section documents how to turn on git mirroring and, since no guide existed for it, walks through creating the scoped fine-grained GitHub personal access token it needs step by step. Also corrected a stale line in the Architecture section still calling git mirroring "design-stage, not built" - it shipped in 2.8.0-2.8.2.

## [2.8.3] - 2026-09-17

### Fixed
- `get_automation` and `audit_automations` only ever read `automations.yaml`/`packages/*.yaml` - so whether an automation is actually enabled right now was invisible to both. Toggling an automation via the UI or the `automation.turn_off`/`turn_on` services never touches the YAML `enabled:` key; that state lives purely on the live `automation.*` entity, whose `entity_id` is derived from `alias` (slugified), not from the config `id` - so it can't be guessed, only looked up by scanning `automation.*` entities for a matching `id` attribute. `get_automation` now reports `currently_enabled` (and a note when no matching entity exists yet, e.g. not reloaded since being added); `audit_automations` now reports a `currently_disabled` list and tags every `references_unavailable_entities` finding with `currently_enabled`, since that finding is real but lower-urgency on an automation that's off anyway. New `audit_manager.find_automation_state()` helper backs both. Found via a real case of an agent treating a disabled automation as if it were live.

## [2.8.2] - 2026-09-17

### Added
- Git mirroring extended to `write_dashboard`, storage-file-based (`.storage/lovelace` or `.storage/lovelace.<url_path>`) rather than reading a resolved YAML file like `write_automation`/the template-entity tools do - confirmed against `home-assistant/core` that `LovelaceStorage.async_save()` writes immediately, so reading the storage file right after `write_dashboard()` returns reliably captures the new content. `mirror.mirror_write()` and `mirror_secrets.py`'s credential scan now take a `content_type` ("yaml" or "json") so the same mirroring/scanning logic serves both real config files and raw `.storage/*` JSON.
- `create_helper`/`update_helper`/`delete_helper` mirroring was investigated but deliberately NOT implemented this round: HA's generic `StorageCollection` (`helpers/collection.py`) debounces its save 10 seconds (`async_delay_save(..., SAVE_DELAY=10)`), so a read right after the write would almost always capture stale, pre-write content rather than the real change - tracked in issue #43 pending a design decision (reconstruct after-content from the API response in-memory, force an immediate flush, or a background file-watcher).

### Fixed
- `/config/.storage/schedule` was missing from `DEFAULT_READ_ONLY_PATHS` - confirmed against `homeassistant/components/schedule`'s own `Store(key=DOMAIN)` that this was a genuine pre-existing gap, unrelated to this release's own changes but found while researching helper storage paths for the mirroring work above.

## [2.8.1] - 2026-09-17

### Added
- Git mirroring extended to `create_template_entity`/`update_template_entity`/`delete_template_entity`, completing the file-based half of issue #34's mirroring scope (`write_automation` was #40; helpers/dashboard/derived-sensor mirroring, which need a different content source, remain open). `TemplateYamlManager.create_entity`/`update_entity`/`delete_entity` now return the touched file's before/after content (`TemplateWriteResult`), same shape and same reasoning as `AutomationWriteResult`. `llm_api.py`'s three template-entity write tools share a new `_mirror_file_write()` helper with `write_automation` rather than repeating the same mirror-and-report logic four times.

## [2.8.0] - 2026-09-17

### Added
- Git mirroring for `write_automation`, per `docs/AUTOMATION_TESTING_DESIGN.md`'s "Mirroring" section. New options (integration's Configure dialog): enable mirroring, a dedicated private mirror repository (`owner/repo`), and a scoped access token. When enabled, every confirmed `write_automation` call pushes the touched file's before/after content to that repo's `main` branch - a before-commit only if the file has drifted since the last mirrored write (a no-op otherwise), an after-commit with the new content. Talks to GitHub's REST API directly (`mirror.py`, via Home Assistant's own shared `aiohttp` client) rather than shelling out to git or adding GitPython, keeping this integration's runtime footprint unchanged.
- Content is scanned for likely credentials before every mirror push (`mirror_secrets.py`) - a credential-shaped key (`password`, `token`, `api_key`, `secret`, ...) holding a literal value rather than a `!secret` reference blocks mirroring for that write entirely (never the underlying write itself) and is reported back in the tool's response so the agent can tell the user which file/key to move into `secrets.yaml`.
- `AutomationManager.write_automation()` now captures and returns the touched file's content immediately before and after the write (`AutomationWriteResult`), the foundation the above two features are built on - captured inline since that "before" state only exists in the narrow window before the write itself.

## [2.7.0] - 2026-09-17

### Added
- Every write tool (`write_automation`, `create_helper`/`update_helper`/`delete_helper`, `create_derived_sensor`/`update_derived_sensor`/`delete_derived_sensor`, `create_template_entity`/`update_template_entity`/`delete_template_entity`, `write_dashboard`) now requires two calls: the first ("propose") never writes anything and returns a preview plus a short-lived `confirm_token`; only a second call echoing that exact token back proceeds to the existing dry-run/live behavior. Applies in both dry-run and live mode - see `docs/AUTOMATION_TESTING_DESIGN.md`'s "Per-write confirmation" section for the design and why this is deliberately friction/UX rather than a hard guarantee. A token is bound to its exact tool name and arguments (via `write_confirmation.py`), expires after 10 minutes, and is single-use.

### Changed
- **Breaking**: every write tool's calling contract - a call that previously wrote (or dry-run-previewed) immediately now requires a first propose call and a second confirming call with `confirm_token` set.

## [2.6.2] - 2026-09-16

### Fixed
- `mcp_repair.py`'s Repairs issue (`mcp_server_not_exposing_dev_tools`) could fire as a false positive on a real restart and then never clear itself. The check only reran on `SIGNAL_CONFIG_ENTRY_CHANGED` (entry added/removed/updated) or at this integration's own setup - but `mcp_server` isn't in this integration's `after_dependencies`, so on a full restart its config entry can still be mid-setup when `ha_dev_tools`' own setup-time check runs, creating the issue even though `mcp_server` finishes loading and exposing `dev_tools` moments later. A plain successful setup completion doesn't fire `SIGNAL_CONFIG_ENTRY_CHANGED` (that's only for add/remove/update), so nothing was left to clear the stale issue. Now also rechecked once on `EVENT_HOMEASSISTANT_STARTED`, by which point every integration that loads at boot has had its chance to.

## [2.6.1] - 2026-09-16

### Fixed
- `derived_sensor_manager.py` imports `voluptuous_serialize` directly but never declared it in `manifest.json`'s `requirements`. It usually worked anyway since Home Assistant core itself depends on `voluptuous-serialize`, but that's an implicit transitive dependency, not a guarantee - on at least one real install it was missing, breaking setup for the whole integration with `ModuleNotFoundError: No module named 'voluptuous_serialize'` (every other module in `__init__.py`'s import chain, including unrelated tools, failed alongside it). Now declared explicitly as `voluptuous-serialize>=2.6.0`.

## [2.6.0] - 2026-08-27

### Added
- A Settings → System → Repairs issue (`mcp_server_not_exposing_dev_tools`) that fires when Home Assistant's native `mcp_server` integration isn't loaded, or is loaded but doesn't have `dev_tools` in its exposed APIs. Found by debugging a real install: HA Dev Tools registers `dev_tools` into HA's internal LLM API registry and has no HTTP transport of its own (see docs/ARCHITECTURE.md), so it shows up as a healthy integration with zero log errors even when `mcp_server` was never installed at all - the only visible symptom was an MCP client getting a bare 404 from `/api/mcp/dev_tools`, with nothing in this integration's own logs pointing at why. Deliberately not a config-flow blocker: the documented setup order (README) installs HA Dev Tools before `mcp_server`, so refusing setup until `mcp_server` already exists would contradict those instructions. The issue is fully self-clearing instead - it reacts to `mcp_server`'s config entries being added, removed, or reconfigured (via `SIGNAL_CONFIG_ENTRY_CHANGED`), with no restart needed either way. See `mcp_repair.py`.

## [2.5.1] - 2026-08-27

### Fixed
- `access_control.py`'s arm-file checks (`check_armed`, `touch_armed`, and the periodic/startup cleanup task) ran synchronous file I/O (`Path.read_text`/`stat`/`unlink`, `os.utime`) directly on the event loop. Home Assistant's blocking-call detector flagged `path.read_text()` in `_read_armed_at` at integration setup, and the same functions ran on every single gated tool call via `check_armed`/`touch_armed` - not just at startup. All three entry points now do their file I/O via `hass.async_add_executor_job`, matching the pattern already used everywhere else in this codebase (`file_manager.py`, `automation_manager.py`, etc.); `check_armed`/`touch_armed`/`async_setup_cleanup` are now `async def` and callers updated accordingly. No behavior change to the arming/expiry logic itself.

## [2.5.0] - 2026-08-22

### Added
- `list_derived_sensors`/`create_derived_sensor`/`update_derived_sensor` now also cover the config-entry-based **Template** helper - the last piece of issue #13. Template's config flow branches through a real `FlowResultType.MENU` (pick an entity domain - sensor, switch, light, cover, ...) rather than a form; it needed no new machinery, `_drive_flow`'s existing generic step-driver just needed to accept `MENU` results the same way it already accepts `FORM` ones (HA represents the menu choice as a `next_step_id` field in the menu's own schema, discovered and supplied exactly like any other step's fields).

### Fixed
- `_drive_flow` (`derived_sensor_manager.py`, backing `create_derived_sensor`/`update_derived_sensor`) hung indefinitely when a step's `validate_user_input` rejected the supplied input - Home Assistant re-shows the identical step with `errors` set rather than aborting, and the driver kept resubmitting the same rejected input forever. Confirmed directly building this release's Template support (had to force-kill the test process past two minutes on `statistics`'s validated `options` step, an existing domain from 2.3.0 - not something new to Template). A second visit to an already-attempted step now raises `FlowStepRequiredError` with the fresh error instead of looping.
- Same function also let a raw schema/type mismatch - a bad `MENU` `next_step_id`, or a validator raising plain `vol.Invalid` instead of `SchemaFlowError` (Template's own sensor validator does this for a device_class/unit-of-measurement mismatch, and `_async_form_step` only catches the latter) - escape as an HA-internal `InvalidData`/`vol.Invalid` exception instead of a clean, catchable error. Now caught and re-raised as `FlowAbortedError`, already handled by every tool built on this module.

## [2.4.0] - 2026-08-22

### Added
- `list_template_entities`, `get_template_entity`, `create_template_entity`, `update_template_entity`, and `delete_template_entity` tools, closing the YAML half of issue #13's ask: entities defined via the modern, trigger-based `template:` YAML syntax were invisible to every tool here, same gap as the derived-sensor helpers added in 2.3.0 but for a mechanism with no config-entry involved at all. Layout-aware and package-safe the same way `get_automation`/`write_automation` are - resolves whether an entity lives in `configuration.yaml` or a `packages/*.yaml` file before reading or writing anything, and refuses to guess when a `unique_id` is defined more than once. Reads span both `configuration.yaml` and packages; writes are narrower on purpose - new entities always go into an existing package (`configuration.yaml` itself is read-only under this integration's default security policy), and updating/deleting an entity that lives in `configuration.yaml` fails with a clear permission error rather than attempting it. Every write requires the entity to have its own `unique_id`, since that's the only stable way to address one again afterward. See `template_yaml_manager.py` and docs/ARCHITECTURE.md's new "Layout-aware YAML template: entities" section for the full design. The config-entry-based Template *helper* (the UI-created kind) is still not covered - tracked in ARCHITECTURE.md's "Still open" as the one remaining piece of #13.

## [2.3.1] - 2026-08-22

### Fixed
- **Security:** `FileManager.write_file`/`delete_file` validated their target path as a read operation instead of a write - `SecurityManager.validate_file_path`'s default is `OPERATION_READ`, and neither call site passed `operation=OPERATION_WRITE`. Since the read check (`is_allowlisted`, covering both `read_paths` and `write_paths`) is a superset of the write check (`is_writable`, `write_paths` only), this meant any path this integration considers read-only - `configuration.yaml`, `scripts.yaml`, `scenes.yaml`, and every `.storage/*` snapshot in `DEFAULT_READ_ONLY_PATHS` - was actually writable and deletable through `FileManager`, contradicting the documented security model (see `docs/SECURITY.md`/`security.py`). Confirmed directly: `write_file("configuration.yaml", ...)` succeeded against the real default security config before this fix. `SecurityManager.validate_file_path` itself was already correct and already tested in isolation with the right operation passed explicitly - the gap was only in these two integration points never actually passing it through. No tool currently exposes raw `delete_file`, and `write_automation` (the only existing `write_file` caller) only ever targets `automations.yaml`/`packages/*.yaml`, both of which are write-permitted - so this fix changes no currently-reachable behavior, only closes the gap for future callers (starting with the in-progress derived-sensor/template YAML work in #13).

## [2.3.0] - 2026-08-22

### Added
- `list_derived_sensors`, `get_derived_sensor`, `create_derived_sensor`, `update_derived_sensor`, `delete_derived_sensor`, and `reload_derived_sensor` tools, covering the biggest gap flagged in issue #13: `list_helpers`/`create_helper`/`update_helper`/`delete_helper` only ever covered the nine flat storage-collection helper domains (`input_boolean`, `counter`, `timer`, ...) - a second, equally common helper family (Min/Max, Utility Meter, Integration [Riemann sum], Statistics, Threshold, Derivative, Filter) was completely opaque to inspect or fix, despite showing up constantly in real configs as calculated/derived sensors. These are config-entry integrations, not storage items, so create/update drive the real config/options flow step by step rather than taking a flat config dict - call with no/partial `steps` first to discover the current step's fields from a `needs_input` response, then retry with them filled in, repeating for domains with more than one step (`statistics` is a fixed 3-step flow; `filter` branches into a different step depending on the filter type chosen). See `derived_sensor_manager.py` and docs/ARCHITECTURE.md's "Driving config/options flows generically" for the full design. Template helpers and YAML `template:` sensors - issue #13's other ask - are deliberately not covered yet, given the size of `template`'s own config flow; tracked in ARCHITECTURE.md's "Still open".

## [2.2.2] - 2026-08-21

### Fixed
- `get_logs` always returned `{"entries": [], "count": 0}`, even with maximally permissive parameters, while the native Settings → System → Logs page showed live WARNING/ERROR entries from the same instance. It was reading a static `home-assistant.log` file in the config directory - the wrong data source, since the native Logs page (and HA's own `system_log/list` websocket command) is backed by the `system_log` integration's in-memory WARNING+ record buffer instead. Depending on the install, that file can be missing, rotated, or simply not what's being written to, producing exactly this always-empty-but-reachable symptom. `get_core_logs` now reads `hass.data["system_log"].records` directly, matching what the native page actually shows.

## [2.2.1] - 2026-08-21

### Changed
- The arm gate's error message and the config flow's setup description now spell out the literal, copy-pasteable arm command (`date +%s > /config/.storage/ha_dev_tools.armed`) instead of describing it ("create the file with the current unix timestamp as its content") - the latter left a reader to work out how to actually generate and write a unix timestamp themselves, which turned a one-line copy-paste into a real obstacle in practice.

## [2.2.0] - 2026-08-21

### Added
- `get_entity_history` and `get_logbook` tools, closing the gap `docs/ARCHITECTURE.md`'s API-vs-file-access table already flagged: nothing previously exposed the recorder's own state-history/logbook data, so a question like "why didn't this automation fire yesterday" had no timestamped trace to answer it from - only current live state and a core log with no retention of its own. `get_entity_history` wraps `recorder.history.get_significant_states` (same call the History page's websocket API makes); `get_logbook` wraps `logbook.processor.EventProcessor` (same class the Logbook page and `logbook/get_events` WS command use), returning entries already humanized rather than raw `state_changed` events. Both are bounded by the recorder's own retention (`purge_keep_days`, 10 days by default) - see `history_manager.py`.

## [2.1.0] - 2026-08-21

### Added
- Dry-run mode, toggleable at any time from this integration's Configure page (no restart needed - `access_control.is_dry_run()` re-reads the config entry's options fresh on every call, the same "never cache" approach `check_armed()` uses for the arm file). When enabled, every write tool (`write_automation`, `create_helper`/`update_helper`/`delete_helper`, `write_dashboard`) is blocked at the gate before any of its own logic runs - the tool call's own validated input is returned to the agent as a `would_apply` preview instead of being applied. This is a policy block, not a simulation: it doesn't verify the write would have succeeded, only that it didn't happen. Implemented as a new `WriteGatedTool` base class (extends `GatedTool`) so every write tool gets this centrally rather than each one needing its own check. `reload_domain`/`check_config` and every read-only tool are unaffected either way.
- `strings.json`'s `options` section and this integration's first options flow (`options_flow.py`) - previously this integration had no options at all, config-entry-only with a single confirm step.

## [2.0.2] - 2026-08-21

The integration wasn't showing up in Home Assistant's "+ Add Integration" search at all after installing via HACS, despite `config_flow: true` and a clean startup log (confirmed via a real install: HA found and parsed the manifest fine, no exceptions anywhere, requirements installed without issue - so this wasn't a HACS placement problem or a Python error).

### Added
- `strings.json` and `translations/en.json` - the integration had neither, unlike every comparable custom integration (including the author's own `ha-concierge-mcp`), leaving the config flow with no title/description text to source.

### Changed
- `integration_type` changed from `"system"` to `"service"`. `"system"` is meant for integrations representing a core system concept that users don't manually search for and add (the ones tracked in HA's frontend source only explicitly exclude `"hardware"` from the add-integration search filter, so this wasn't confirmed as *the* cause from source alone) - but it's the one meaningful manifest difference from `ha-concierge-mcp`, a directly comparable custom integration confirmed working on the same Home Assistant instance, so this is the best evidence-based fix available without live access to reproduce the picker's exact behavior.
- Added `single_config_entry: true`, declaring at the manifest level what `config_flow.py`'s `_async_current_entries()` check already enforced imperatively - matches `ha-concierge-mcp`'s manifest.
- Removed `quality_scale: "silver"` - inaccurate, unearned metadata; no actual quality-scale compliance audit has been done against Home Assistant's silver-tier ruleset, and `ha-concierge-mcp`'s manifest doesn't declare one either.

## [2.0.1] - 2026-08-21

### Added
- Real brand assets (`icon.png`/`icon@2x.png`/`logo.png`/`logo@2x.png`, plus the `icon.svg`/`logo.svg` source) under `custom_components/ha_dev_tools/brand/`, replacing the old pre-restart placeholder (a broken 256x128 purple hexagon graphic with its own title text running off the edge of the canvas). Embedded at the top of `README.md`. Uses Home Assistant's native local-brand-icon support (since HA 2026.3.0) rather than submitting to the now-legacy `home-assistant/brands` repo, which no longer accepts custom-integration submissions.

## [2.0.0] - 2026-08-21

This is an architectural restart: `ha_dev_tools` moves from a standalone
MCP-server process talking to this integration's REST bridge to registering
its tools directly into Home Assistant's own native `mcp_server`/`llm.Tool`
system - no separate process, Add-on, or REST API surface. See
`docs/ARCHITECTURE.md` for the full design and `docs/SECURITY.md` for the
arm-file/admin-gate access-control model this restart introduced.

### Removed
- The `/api/management/*` REST surface (`api.py`'s `ManagementAPIHandler` and its five `HomeAssistantView`s) - dead weight from the retired standalone-MCP-server design, still registered and live in `__init__.py` alongside the new MCP/`llm.Tool` path until now. It wasn't a new security hole (each endpoint independently enforced admin), just a redundant second way to reach the same files that bypassed the arm-file gate entirely and contradicted `docs/ARCHITECTURE.md`'s own "no REST API surface" claim. Its tests (`test_metadata_api.py`, `test_write_api.py`, the already-skipped `test_ha_api_integration.py`) removed with it.
- `models.py` - a `SecurityConfiguration` dataclass duplicating `const.py`'s schema that nothing in the codebase ever imported.
- `configuration.yaml`-based `ha_dev_tools: security: {...}` config (`CONFIG_SCHEMA`/`SECURITY_CONFIG_SCHEMA` in `const.py`, `async_setup()` in `__init__.py`, `config_flow.py`'s dead `async_step_import` - never actually invoked by anything, confirmed by grepping for callers) and `docs/CONFIGURATION_EXAMPLES.md`, which documented it. Replaced by giving `SecurityManager`'s defaults real values instead - see Fixed below for why this was necessary, not just simplification. A future per-install customization mechanism, if ever needed, belongs in a config-flow options flow (`entry.options`), not a YAML/config-entry dual-source.
- `config_flow.py`'s stale title ("Home Assistant Configuration Manager", the pre-restart project name - visibly shown in the HA UI when adding the integration) fixed to "HA Dev Tools".
- Dead `setup_integration` test fixture in `tests/conftest.py` - after removing `test_ha_api_integration.py` (the only thing that ever used it), nothing called it; `setup_integration_with_entry` covers the same setup already.

### Fixed
- **`write_automation` could not write to any path, ever, out of the box.** `SecurityManager._build_write_paths()` had no fallback (unlike `_build_read_paths()`, which does default to `DEFAULT_READ_ONLY_PATHS`) - `DEFAULT_WRITE_PATHS` was an empty list, and the only thing that could have populated `write_paths` was the `configuration.yaml` bridge above, which never actually reached the config-entry `SecurityManager` the live tools use (`entry.data["security"]` is always `{}` - `async_setup_entry` builds its `SecurityManager` from that, `async_setup` built a *different* `SecurityManager` instance from YAML, feeding only the now-removed REST layer). Every write attempt hit `ERROR_WRITE_NOT_PERMITTED`, unconditionally, for every install. Fixed by giving `DEFAULT_WRITE_PATHS` real values scoped to exactly what `write_automation` can target (`automations.yaml`, `packages/**/*.yaml` - matching `AutomationManager.candidate_files()`) and wiring `_build_write_paths()` to use them under the same "only if nothing was explicitly configured" rule `_build_read_paths()` already used, so an explicit `write_paths` config (once there's a real way to supply one) still overrides cleanly.

### Documentation
- Reworked all project documentation for the current architecture, deleting what described the retired standalone-MCP-server/REST-API design: `README.md` and `docs/SECURITY.md` rewritten from scratch; `docs/RESTART_PLAN.md` (its job - planning the restart - is done) retired in favor of a new `docs/ARCHITECTURE.md` that keeps the durable, source-grounded reasoning (file-vs-API decision matrix, package provenance rule, WS loopback pattern); `docs/API.md` (entirely about the now-dead `/api/management/*` REST surface), `GITHUB_RELEASE_INSTRUCTIONS.md` and `RELEASE_NOTES_v1.0.0.md` (one-time historical artifacts for a defunct architecture), and the empty `HACS_SUBMISSION_GUIDE.md` deleted outright; `CONTRIBUTING.md` rewritten (project structure, Python 3.14 prerequisite, actual test layout); `docs/CONFIGURATION_EXAMPLES.md` corrected in place - it also documented a `rate_limiting:` config block and a configurable `backup:` block that don't exist anywhere in the code (rate limiting isn't implemented at all; backups are real but automatic and hardcoded to `.ha_dev_tools_backups/`, not configurable) and a "required configuration, Home Assistant won't load without it" claim that doesn't match `__init__.py`'s actual graceful-default behavior - all struck rather than left describing features that were never real. All in-code docstring references to `docs/RESTART_PLAN.md` repointed to `docs/ARCHITECTURE.md`.

### Security
- Every dev_tools tool except the diagnostic `dev_tools_ping` now requires two independent checks before running (`access_control.py`, wired via a new `GatedTool` base class in `llm_api.py`): (1) an out-of-band "arm file" that only real filesystem access (SSH, the Terminal add-on) can create - never dev_tools itself, which is denylisted from writing it and only ever touches its mtime, not its content - must exist and be recent; (2) the resolved calling user must be a genuine HA admin, checked independently of `mcp_server`'s own gate. Both were added after tracing HA's actual security model: `homeassistant/auth/models.py`'s `RefreshToken` has no scope field at all, so every token a user holds - a mobile app token, a browser session, a forgotten long-lived token - is exactly as powerful as every other one; and `mcp_server/http.py`'s admin check only covers the explicit `/api/mcp/<api_id>` URL, not the bare `/api/mcp` endpoint that serves a config entry's configured APIs with no admin check at all. Without the arm-file gate, a single leaked ordinary HA credential would grant SSH-key-equivalent capability (raw file read/write, and a `shell_command`/`rest_command` path to arbitrary execution via `write_automation`) through a credential class nobody manages with that severity in mind. The arm file's content (original arm time, immutable by dev_tools - the 4-hour hard cap) and mtime (last-used time, extended by dev_tools on every successful call, up to 30 minutes idle) are deliberately asymmetric so dev_tools can extend an already-granted session but never manufacture or reset one from nothing. A best-effort periodic task removes an expired arm file, but is never what enforces expiry - every check re-derives state fresh from the file on disk, so a missed cleanup (e.g. a restart) can't leave dev_tools armed longer than intended.
- Fixed: `ws_call.py`'s Python 3.14 `inspect.Format.FORWARDREF` fallback (added in the previous CI fix) used `except TypeError` to detect older interpreters, but referencing `inspect.Format` at all raises `AttributeError` on Python <3.14, before the call even happens - `except TypeError` never caught it. Found by actually running the full suite against the local Python 3.12 venv after that change, which this project's "verify on fresh venvs, not just CI" discipline should have caught the first time. Fixed with `getattr(inspect, "Format", None)` instead.

### Added
- Helper CRUD tools (`list_helpers`/`create_helper`/`update_helper`/`delete_helper`) covering all nine helper domains (`input_boolean`, `input_number`, `input_text`, `input_select`, `input_datetime`, `input_button`, `counter`, `timer`, `schedule`). These only have a WebSocket API, no in-process access point (their `StorageCollection` is a private variable inside each component's own `async_setup` - confirmed by reading the source). `ws_call.py` makes this reachable in-process by constructing a real `websocket_api.ActiveConnection` with a fake transport and calling its actual command dispatch directly - reusing HA's real schema validation and admin enforcement, faking only the socket. Verified with a real `input_boolean` create/list/update/delete round-trip and a real admin-vs-non-admin permission check before any tool was built on it (`tests/test_ws_call.py`). Resolves the real calling user from the MCP request's context rather than a synthetic admin bypass.
- Dashboard tools (`get_dashboard`/`write_dashboard`) using the same `ws_call.py` mechanism against `lovelace/config`/`lovelace/config/save`. Storage-mode dashboards only - a write against a YAML-mode dashboard (which HA itself hard-rejects at the WS level) now raises a clear `YamlModeDashboardError` explaining why, instead of a confusing raw WS error.
- `entity_health_report` tool: per-integration counts of disabled/hidden/unavailable/unknown/"missing" (registered, enabled, but no state at all - usually means the owning integration failed to load) entities, plus a capped sample of the actual problem entities, optionally scoped by area or integration. Turns "hundreds of entities" into something scannable instead of a wall of text from `find_entities`.

- Restart plan for the project: `ha_dev_tools` becomes a Home Assistant integration that registers development-workflow tools directly into HA's native `llm.Tool`/`mcp_server` system, replacing the standalone `ha-dev-tools-mcp` process and its `/api/management/*` REST bridge. See `docs/RESTART_PLAN.md`.
- Release automation (version-check, auto-pr, auto-merge, auto-release) mirroring the pattern used across the author's other HACS integrations.
- Phase 1 foundation: enabled the config flow, registered a `dev_tools` `llm.API` with one diagnostic tool (`dev_tools_ping`), and confirmed it's discoverable and callable through Home Assistant's LLM tool registry with a real headless test (`tests/test_llm_api.py`). Real tools land in later phases per `docs/RESTART_PLAN.md`.
- Bumped `hacs.json`'s minimum Home Assistant version to 2026.8.2 — the version that shipped the native `mcp_server` integration this design depends on.

- Phase 2 core authoring loop, all registered as real `llm.Tool`s on `dev_tools`:
  - `find_entities` - area/domain/name-scoped entity lookup (`entity_manager.py`), resolving an entity's area through its device the way HA itself does, and reporting live availability (registries have no such field - see `docs/RESTART_PLAN.md`).
  - `get_logs` - tail/filter/search over the real log file via the existing `LogManager`, replacing the old unbounded raw-blob `get_error_log`.
  - `check_config` / `reload_domain` - HA's own config-check helper and `<domain>.reload` services, wired so config changes never require a restart.
  - `get_automation` / `write_automation` - layout-aware, package-safe automation read/write (`automation_manager.py`), implementing the "hard safety rule" from `docs/RESTART_PLAN.md`: resolves which file (default `automations.yaml` or a specific `packages/*.yaml`) actually defines a given automation id before reading or writing, refuses to guess when an id is duplicated across files, and uses `ruamel.yaml`'s round-trip loader so hand-maintained package files keep their comments/formatting instead of being reformatted on every edit.
- `render_template` / `validate_template` tools (`template_manager.py`), running entirely in-process via `homeassistant.helpers.template.Template` against live state - no WS/HTTP round-trip needed, closing the "draft, render, adjust" loop the restart plan calls out as core to the author/iterate workflow. `validate_template` distinguishes a syntax error (never rendered) from a render error (valid syntax, failed against live state) from success, and separately flags any referenced entity_id that doesn't currently exist. Despite the `async_` naming, `Template.async_render`/`async_render_to_info` are plain synchronous functions in the pinned HA test version (confirmed via `inspect.iscoroutinefunction()`) - handled with an `inspect.isawaitable()`-gated helper rather than assuming either calling convention.
- `list_addons` / `get_addon_logs` tools (`supervisor_manager.py`) for Home Assistant OS/Supervised installs. Guarded by a `SupervisorNotAvailableError` on Core-only installs (most real installs, and this test environment), where `hass.data[hassio.const.DATA_COMPONENT or DOMAIN]` (version-dependent key, resolved directly rather than assumed) is never populated. Add-on logs go through the lower-level `HassIO.send_command("/addons/{slug}/logs", ...)` REST wrapper rather than the typed `aiohasupervisor` client, since that client's `addons` object has no `logs` method in the installed HA version - the same path HA's own frontend log viewer proxies through.

### Fixed
- Several legacy test files (`test_metadata_api.py`, four files under `tests/property/`) replaced `sys.modules['homeassistant']` and friends with `unittest.mock.Mock` objects at import time with no teardown, corrupting the real `homeassistant` package for every test file collected afterward in the same pytest process. Harmless while nothing else needed the real package, but broke `tests/test_llm_api.py`'s real `pytest-homeassistant-custom-component` fixtures. Now snapshotted and restored after each file's own imports.
- `SecurityManager`'s glob matcher: `packages/**/*.yaml` (the documented recommended pattern, and `DEFAULT_READ_ONLY_PATHS`'s own default) only matched files in a *subdirectory* of `packages/`, not direct children like `packages/emhas.yaml` - the exact real-world layout this project is built around. Plain `fnmatch` requires the pattern's literal `/` between the two `*` groups to be present in the path; `**` now also matches zero intermediate directories, as its own docstring already claimed it did.
- CI was silently testing against `homeassistant==2025.1.4` - two release lines behind the version that actually ships `mcp_server` - and a previous version of `docs/RESTART_PLAN.md` misdiagnosed why, claiming `mcp_server` "hadn't reached a stable release yet." It had (`homeassistant==2026.8.2`, on PyPI since 2026-08-14, confirmed by reading its manifest at that git tag). The real cause: HA's `Requires-Python` floor climbed to `>=3.14.2` starting with the 2026.3 release line, but `test.yml`'s matrix only ran Python 3.12/3.13, so `pip install homeassistant>=2024.1.0` silently fell back to the newest release still installable there (2025.1.4) instead of erroring - indistinguishable from "not released" without checking PyPI directly. Fixed: `test.yml` now runs Python 3.14; `requirements-test.txt` pins `pytest-homeassistant-custom-component==0.13.356` (whose own exact pin is `homeassistant==2026.8.2`) plus the matching `hassil==3.11.0`/`home-assistant-intents==2026.7.30` (read directly from `conversation`'s manifest at the 2026.8.2 tag) instead of the stale 2025.1.4-era pins.
- `tests/test_file_manager.py::test_read_directory_as_file` created a directory in `pytest-homeassistant-custom-component`'s shared (not per-test) `testing_config` dir and never removed it, so a second full-suite run in the same checkout failed with `FileExistsError` instead of exercising the test. Now cleaned up in a `finally` block.

### Removed
- The last of the pre-restart `security.py` config surface: the `allowed_paths`/`allowed_storage_files` legacy config keys (superseded by `read_paths`/`write_paths`, which have covered the same ground since `_build_read_paths()`/`_build_write_paths()` landed) and the now-fully-unused `SECURITY_MODE_ALLOWLIST`/`SECURITY_MODE_DENYLIST` constants - the system has only ever operated in strict allowlist mode, so a "mode" was never actually selectable. `RECOMMENDED_SAFE_STORAGE_PATTERNS` also removed - a strict subset of `DEFAULT_READ_ONLY_PATHS` that existed only to feed a "config ended up empty" fallback path in `_build_allowlist()` that `_build_read_paths()`/`_build_write_paths()`'s reciprocal-defaulting rule already made unreachable. Tests that only ever exercised the removed keys were deleted; tests that exercised real, still-live behavior (glob patterns, denylist precedence, default-path fallback) were rewritten against `read_paths`/`write_paths`/`denied_paths` instead of removed.

### Changed
- Brought the project's own linting into compliance with itself rather than leaving `.pre-commit-config.yaml` as aspirational config nothing actually passed: fixed every real `mypy`/`flake8` finding (a genuine type-safety gap in `AutomationManager.get_automation()`, two `helper_manager.py` false positives from `**dict[str, str]` against a keyword-only `float` parameter, several unused imports/variables), reformatted the full tree with `black`+`isort` (added `isort`'s `profile = "black"` in `pyproject.toml` to stop the two tools fighting each other), and added `.flake8` (`max-line-length = 100` - black itself doesn't wrap comments/docstrings/strings at 88, so pairing with flake8's default 79 or even black's own 88 produced dozens of false-positive-feeling violations against otherwise-black-formatted code). `.pre-commit-config.yaml`'s tool pins updated to the versions actually exercised (`black` 26.5.1, `isort` 8.0.1, `flake8` 7.3.0, `mypy` v2.3.1) and its `mypy` hook now passes `--ignore-missing-imports --python-version 3.14` - the former because Home Assistant's own package isn't fully `py.typed`-clean across every submodule this project imports, the latter because mypy's default target predates PEP 649/749 deferred-annotation evaluation and flagged two false positives (`inspect.signature`'s `annotation_format` kwarg, `typing.override`) without it.
- Removed `pyproject.toml`'s `[tool.pytest.ini_options]` section - `pytest.ini` already exists in this repo and pytest always prefers it when both are present, so the `pyproject.toml` copy was silently dead (confirmed by every test run's own `configfile: pytest.ini` header) and had drifted from what `pytest.ini` actually configures.
- `requires-python` in `pyproject.toml` bumped from `>=3.12` to `>=3.14`, matching the floor `test.yml`/`requirements-test.txt` already enforce.
