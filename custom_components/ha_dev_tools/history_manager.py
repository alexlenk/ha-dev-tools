"""Recorder-backed state history and logbook access.

Fills the one real gap `docs/ARCHITECTURE.md`'s own API-vs-file-access table
already calls out: "logbook is recorder/DB-backed" was noted there as a
reason raw log forensics still need the log file, but nothing here ever
exposed that DB-backed data itself. That gap is exactly what "did the
cleaning automation actually trigger, and if not why" needs when the
current-state snapshot other tools give isn't enough - a timestamped
record of what an entity's state actually was, and what fired around it.

Both functions below reuse Home Assistant's own query machinery rather than
re-deriving it:

- `get_entity_history` wraps `homeassistant.components.recorder.history.
  get_significant_states` - the same call the History page's websocket API
  makes.
- `get_logbook_entries` wraps `homeassistant.components.logbook.processor.
  EventProcessor` - the same class the Logbook page and `logbook/get_events`
  WS command use, so "what fired and why" comes back already humanized
  (e.g. "triggered by state of ...") instead of raw state-changed events.

Both are bounded by the recorder's own retention (`purge_keep_days`,
10 by default) - a query for data older than that comes back empty. That
emptiness is itself diagnostic (the answer isn't recoverable, not that
nothing happened) and callers should say so rather than treating it as
"nothing to report".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.logbook.helpers import async_determine_event_types
from homeassistant.components.logbook.processor import EventProcessor
from homeassistant.components.recorder import get_instance, history
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util


class RecorderNotAvailableError(Exception):
    """Raised when the recorder (or a component that depends on it) isn't set up."""

    def __init__(self, component: str = "recorder") -> None:
        """Init with a message naming which required component is missing."""
        super().__init__(
            f"The {component} integration isn't set up on this instance - "
            "state history and the logbook aren't available regardless of "
            "the time range asked for."
        )


def _get_recorder_instance(hass: HomeAssistant) -> Any:
    # Checked explicitly rather than only catching get_instance()'s KeyError -
    # an operator can legitimately run Home Assistant without the recorder,
    # unlike this integration's own hard dependencies, so this needs to be a
    # clear, expected error path rather than an unhandled KeyError escaping
    # from an internal lookup. (Same check ha-concierge-mcp's get_history
    # tool uses for the same reason.)
    if "recorder" not in hass.config.components:
        raise RecorderNotAvailableError("recorder")
    return get_instance(hass)


def _state_to_dict(state: State) -> dict[str, Any]:
    return {
        "state": state.state,
        "attributes": dict(state.attributes),
        "last_changed": state.last_changed.isoformat(),
        "last_updated": state.last_updated.isoformat(),
    }


async def get_entity_history(
    hass: HomeAssistant,
    entity_ids: list[str],
    *,
    start_time: datetime,
    end_time: datetime | None = None,
    significant_changes_only: bool = True,
    limit: int = 200,
) -> dict[str, Any]:
    """Return recorded state history for the given entities over a period.

    Always includes every requested entity_id in the result, even with an
    empty list - a typo'd or never-recorded entity_id is itself worth
    surfacing, not silently dropped the way `get_significant_states` alone
    would (it only returns keys for entities that actually have rows).
    Each entity's list is capped to the most recent `limit` states,
    chronological order preserved; `truncated` says whether that cut
    anything.
    """
    instance = _get_recorder_instance(hass)

    start_time_utc = dt_util.as_utc(start_time)
    end_time_utc = dt_util.as_utc(end_time) if end_time else None

    raw = await instance.async_add_executor_job(
        history.get_significant_states,
        hass,
        start_time_utc,
        end_time_utc,
        entity_ids,
        None,  # filters
        True,  # include_start_time_state
        significant_changes_only,
        False,  # minimal_response - keep full State objects for every entry
        False,  # no_attributes
        False,  # compressed_state_format
    )

    entities: dict[str, Any] = {}
    for entity_id in entity_ids:
        states = raw.get(entity_id, [])
        truncated = len(states) > limit
        kept = states[-limit:] if truncated else states
        entities[entity_id] = {
            "states": [_state_to_dict(s) for s in kept],
            "count": len(kept),
            "truncated": truncated,
        }

    return {"entities": entities}


async def get_logbook_entries(
    hass: HomeAssistant,
    *,
    start_time: datetime,
    end_time: datetime | None = None,
    entity_ids: list[str] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Return humanized logbook entries for a period, optionally scoped to entities.

    Each entry is whatever the real Logbook page would show - e.g. an
    automation triggering, a script starting, a state change worth noting -
    already resolved to friendly names and, where applicable, what caused
    it (`context_*` fields). Capped to the most recent `limit` entries;
    `truncated` says whether that cut anything.
    """
    instance = _get_recorder_instance(hass)
    # logbook depends on recorder in its own manifest, but not the other way
    # around - an instance can run recorder without logbook, and
    # async_determine_event_types() would otherwise fail with a raw KeyError
    # reading logbook's own hass.data entry.
    if "logbook" not in hass.config.components:
        raise RecorderNotAvailableError("logbook")

    start_time_utc = dt_util.as_utc(start_time)
    end_time_utc = dt_util.as_utc(end_time) if end_time else dt_util.utcnow()

    event_types = async_determine_event_types(hass, entity_ids, None)
    processor = EventProcessor(
        hass,
        event_types,
        entity_ids,
        None,  # device_ids
        None,  # context_id
        timestamp=True,
        include_entity_name=True,
    )

    events = await instance.async_add_executor_job(
        processor.get_events, start_time_utc, end_time_utc
    )

    truncated = len(events) > limit
    kept = events[-limit:] if truncated else events

    entries = []
    for event in kept:
        entry = dict(event)
        when = entry.get("when")
        if isinstance(when, (int, float)):
            entry["when"] = dt_util.utc_from_timestamp(when).isoformat()
        entries.append(entry)

    return {"entries": entries, "count": len(entries), "truncated": truncated}
