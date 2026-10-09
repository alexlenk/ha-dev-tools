"""In-recorder backups of statistics, and restoring them (issue #136).

Before any write that destroys or rewrites a series - clear, migrate onto
an existing series, merge, restore - that series is copied into an
external statistic of this integration's own:
`ha_dev_tools:backup_<statistic id as slug>_<UTC time>`, source
`ha_dev_tools`, same unit and sum/mean kind, named "Backup of <id> before
<operation> <time>". It isn't an entity, so it shows up neither in entity
pickers nor the Energy dashboard, but it does in Developer Tools >
Statistics and in a statistics-graph card next to the result - and, being
in HA's own database, in every regular HA backup, independent of the
mirror repo (which gets its own copy, see llm_api.py). Restoring is a
pure in-HA operation.

The copy is made with HA's own import task and checked row by row count
before the write it protects goes ahead. Only hourly rows exist in a copy:
HA's import takes hourly rows, and 5-minute rows are purged after about 10
days anyway - a restored meter gets one 5-minute row carrying its last
sum, so its next compile continues from there (statistics_manager.py).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder.statistics import (
    STATISTIC_UNIT_TO_UNIT_CONVERTER,
    UNIT_CLASS_TO_UNIT_CONVERTER,
    valid_statistic_id,
)
from homeassistant.core import HomeAssistant, valid_entity_id
from homeassistant.util import dt as dt_util

from . import statistics_manager as sm
from .const import DOMAIN

BACKUP_PREFIX = f"{DOMAIN}:backup_"
STALE_AFTER_DAYS = 90
_NAME = re.compile(
    r"^Backup of (?P<of>\S+) before (?P<operation>\S+) (?P<created>\S+)$"
)


def is_backup(statistic_id: str) -> bool:
    """Whether `statistic_id` is one of this integration's backups."""
    return statistic_id.startswith(BACKUP_PREFIX)


def backup_statistic_id(statistic_id: str, when: datetime) -> str:
    """The backup id for `statistic_id` taken at `when`. External ids are
    lowercase slugs without double underscores, so the id is folded into
    one."""
    slug = re.sub(r"[^a-z0-9]+", "_", statistic_id.lower()).strip("_")
    return f"{BACKUP_PREFIX}{slug}_{when:%Y%m%dt%H%M%S}"


def backup_info(name: str | None) -> dict[str, Any] | None:
    """What a backup's name says: the statistic it copies, the operation
    it was made before, when, and whether it's older than
    STALE_AFTER_DAYS (listed, never deleted automatically)."""
    match = _NAME.match(name or "")
    created = dt_util.parse_datetime(match["created"]) if match else None
    if match is None or created is None:
        return None
    age = dt_util.utcnow() - created
    return {
        "of": match["of"],
        "operation": match["operation"],
        "created": created.isoformat(),
        "age_days": age.days,
        "stale": age > timedelta(days=STALE_AFTER_DAYS),
    }


async def create_backups(
    hass: HomeAssistant, statistic_ids: list[str], operation: str
) -> dict[str, str]:
    """Copy each series into a backup statistic and check every hourly row
    arrived; {statistic_id: backup id}. Backups themselves aren't copied
    again. Raises StatisticsBackupError if any copy is incomplete - the
    write it protects must not go ahead then."""
    wanted = [
        statistic_id for statistic_id in statistic_ids if not is_backup(statistic_id)
    ]
    if not wanted:
        return {}
    now = dt_util.utcnow().replace(microsecond=0)
    metadata = await sm.read_metadata(hass, wanted)
    rows = await sm.read_rows(hass, wanted)
    backups = {
        statistic_id: backup_statistic_id(statistic_id, now)
        for statistic_id in wanted
        if statistic_id in metadata
    }

    def queue() -> None:
        for statistic_id, backup_id in backups.items():
            sm.queue_import(
                hass,
                {
                    **metadata[statistic_id],
                    "name": f"Backup of {statistic_id} before {operation} "
                    f"{now.isoformat()}",
                    "source": DOMAIN,
                    "statistic_id": backup_id,
                },
                rows.get(statistic_id, []),
            )

    await sm.on_recorder(hass, queue)
    copied = await sm.describe_statistics(hass, list(backups.values()))
    for statistic_id, backup_id in backups.items():
        expected = len(rows.get(statistic_id, []))
        if (copied.get(backup_id) or {}).get("rows", -1) != expected:
            raise sm.StatisticsBackupError(
                f"the in-recorder backup of '{statistic_id}' ({backup_id}) "
                f"didn't get all {expected} hourly rows, so nothing was "
                "changed"
            )
    return backups


def unit_converter(
    metadata: dict[str, Any], to_unit: str | None
) -> tuple[Any, str | None]:
    """(convert, problem) for rows in `metadata`'s unit into `to_unit`:
    convert is None when the units already match; a problem when they
    differ and HA has no converter between them (only within one unit
    class, e.g. Wh and kWh)."""
    from_unit = metadata.get("unit_of_measurement")
    if from_unit == to_unit:
        return None, None
    unit_class = metadata.get("unit_class")
    if unit_class is None and from_unit in STATISTIC_UNIT_TO_UNIT_CONVERTER:
        unit_class = STATISTIC_UNIT_TO_UNIT_CONVERTER[from_unit].UNIT_CLASS
    converter = UNIT_CLASS_TO_UNIT_CONVERTER.get(unit_class) if unit_class else None
    if (
        converter is None
        or from_unit not in converter.VALID_UNITS
        or to_unit not in converter.VALID_UNITS
    ):
        return None, (
            f"'{metadata['statistic_id']}' is in {from_unit!r} and can't be "
            f"converted to {to_unit!r}"
        )
    return converter.converter_factory(from_unit, to_unit), None


def convert_rows(rows: list[dict[str, Any]], convert: Any) -> list[dict[str, Any]]:
    """Raw rows with their values converted (no-op without a converter)."""
    if convert is None:
        return rows
    return [
        {
            **row,
            **{
                column: convert(row[column])
                for column in ("mean", "min", "max", "state", "sum")
                if row.get(column) is not None
            },
        }
        for row in rows
    ]


async def plan_restore(
    hass: HomeAssistant,
    backup_id: str,
    target_id: str | None,
    *,
    can_back_up: bool,
    allow_no_backup: bool = False,
) -> dict[str, Any]:
    """What restoring `backup_id` onto `target_id` (by default the
    statistic it's a backup of) would overwrite. The target's current
    series - if any - is backed up first like any other destructive write,
    so a restore can itself be undone."""
    problems: list[str] = []
    if not is_backup(backup_id):
        raise sm.StatisticsChangeRefusedError(
            f"'{backup_id}' isn't a statistics backup (they start with "
            f"'{BACKUP_PREFIX}' - list_statistics source={DOMAIN} lists them)"
        )
    described = await sm.describe_statistics(hass, [backup_id])
    if backup_id not in described:
        raise sm.StatisticsChangeRefusedError(sm._unknown(backup_id))
    backup = described[backup_id]
    info = backup_info(backup["name"])
    target_id = target_id or (info or {}).get("of")
    if not target_id:
        raise sm.StatisticsChangeRefusedError(
            f"can't tell from its name which statistic '{backup_id}' is a "
            "backup of - pass target_statistic_id"
        )
    if is_backup(target_id):
        problems.append("the target can't be a backup itself")
    elif not (valid_entity_id(target_id) or valid_statistic_id(target_id)):
        problems.append(f"'{target_id}' isn't a statistic id")
    metadata = await sm.read_metadata(hass, [backup_id, target_id])
    current = (await sm.describe_statistics(hass, [target_id])).get(target_id)
    target_meta = metadata.get(target_id)
    convert = None
    if target_meta is not None:
        if target_meta["has_sum"] != metadata[backup_id]["has_sum"]:
            problems.append(
                "the backup and the target differ in kind - one is a sum "
                "(meter) statistic and the other a mean (measurement) one"
            )
        convert, unit_problem = unit_converter(
            dict(metadata[backup_id]), target_meta.get("unit_of_measurement")
        )
        if unit_problem:
            problems.append(unit_problem)
    if current is not None and not can_back_up and not allow_no_backup:
        problems.append(sm._no_backup())
    if problems:
        raise sm.StatisticsChangeRefusedError("; ".join(problems))
    if target_meta is None:
        # Gone (e.g. cleared): recreate it with the backup's metadata.
        target_meta = {
            **metadata[backup_id],
            "name": None,
            "source": target_id.split(":")[0] if ":" in target_id else "recorder",
            "statistic_id": target_id,
        }
    return {
        "backup": {**backup, "backup": info},
        "target": target_id,
        "overwrites": current,
        "metadata": target_meta,
        "convert": convert,
    }


async def restore_statistics(
    hass: HomeAssistant, plan: dict[str, Any]
) -> dict[str, Any]:
    """Apply plan_restore's plan: clear the target's current series, import
    the backup's rows, and give a meter a 5-minute row with its last sum.
    The caller backs the current series up first; the backup statistic
    stays."""
    target = plan["target"]
    rows = convert_rows(
        (await sm.read_rows(hass, [plan["backup"]["statistic_id"]])).get(
            plan["backup"]["statistic_id"], []
        ),
        plan["convert"],
    )
    metadata = plan["metadata"]
    instance = sm._instance(hass)

    def queue() -> None:
        if plan["overwrites"]:
            instance.async_clear_statistics([target])
        sm.queue_import(hass, metadata, rows)
        if metadata["has_sum"] and metadata["source"] == "recorder" and rows:
            sm.queue_short_term_seed(hass, metadata, rows[-1])

    await sm.on_recorder(hass, queue)
    restored = (await sm.describe_statistics(hass, [target])).get(target)
    result: dict[str, Any] = {
        "restored": restored,
        "complete": (restored or {}).get("rows") == len(rows),
    }
    if metadata["has_sum"]:
        result["next_compile"] = await sm.continuity_check(hass, target)
    return result
