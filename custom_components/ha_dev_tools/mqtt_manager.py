"""Read-only MQTT topic snapshot - see llm_api.py's ListMqttTopicsTool.

Deliberately read-only for now (no publish/clear-retained capability) -
MQTT can drive physical devices (locks, valves, relays), a different and
larger risk tier than anything else this integration touches, so a
publish-capable tool needs its own security-review pass rather than
folding into the existing write-tool pattern by default. This module
exists to solve a narrower, real problem first: a "ghost" entity with a
live state but no entity registry entry (see delete_entity's docstring -
this is the other kind of orphan, one delete_entity/delete_entities
can't touch, since there's no registry entry to remove) is very often a
plain MQTT-configured sensor whose device is gone but whose last
retained message the broker still holds - and MQTT itself has no "list
retained messages" query. The only way to discover one exists at all is
to subscribe to a topic filter and see what the broker immediately
delivers - retained messages are always delivered synchronously right
after a matching subscribe, before any new live traffic, which is what
the bounded listen window below relies on.

`homeassistant/#` (HA's own MQTT *discovery* prefix - where the `mqtt`
integration publishes/reads auto-discovery config payloads) is offered
as this tool's default topic, but it is NOT where a plain YAML-configured
MQTT sensor's state lives - a `mqtt: sensor:` entry with its own
`state_topic` (e.g. `watermeter/uptime`) never touches `homeassistant/#`
at all. `topic` is a caller-supplied parameter, not hardcoded, precisely
so a caller can point this at the actual topic tree a suspected ghost's
device publishes under (e.g. `watermeter/#`) once that's known or
guessed from the entity's own naming.

Can't be verified end-to-end in this environment - there's no way to
stand up a real MQTT broker in a test sandbox. Tests here mock
`mqtt.async_subscribe` directly and invoke the captured callback
synchronously (simulating a broker's immediate retained-message
delivery), same limitation and same approach as supervisor_manager.py's
own acknowledged gap for the real Supervisor.

`homeassistant.components.mqtt` is imported lazily, inside list_topics()
rather than at module level, deliberately: that package's own
`__init__.py` imports `paho.mqtt.client` (mqtt's own manifest.json
requirement), which Home Assistant only ever installs when the `mqtt`
integration itself is actually set up (HA's requirement installation is
lazy per-component, not a monolithic install). llm_api.py imports this
module unconditionally at ha_dev_tools' own setup, for every install
regardless of whether MQTT is configured there - a module-level import
here would raise ModuleNotFoundError and break ha_dev_tools' entire
setup for any user without MQTT configured, not just this one tool.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant, callback

DEFAULT_TOPIC = "homeassistant/#"
DEFAULT_TIMEOUT = 3.0
MAX_TIMEOUT = 10.0
DEFAULT_LIMIT = 200
MAX_LIMIT = 500
MAX_PAYLOAD_CHARS = 2000


class MqttNotAvailableError(Exception):
    """Raised when the mqtt integration isn't configured/loaded, or isn't
    installed on this Home Assistant instance at all."""


def _import_mqtt() -> tuple[Any, Any]:
    """Lazily import homeassistant.components.mqtt - see this module's
    docstring for why this can't be a top-level import. Isolated into its
    own function so tests can exercise the "not installed at all" path
    directly, without needing real sys.modules manipulation everywhere
    else that calls this."""
    try:
        from homeassistant.components import mqtt
        from homeassistant.components.mqtt.util import mqtt_config_entry_enabled
    except ImportError as exc:
        raise MqttNotAvailableError(
            "The mqtt integration isn't installed on this Home Assistant "
            "instance - nothing to subscribe to"
        ) from exc
    return mqtt, mqtt_config_entry_enabled


async def list_topics(
    hass: HomeAssistant,
    *,
    topic: str = DEFAULT_TOPIC,
    timeout: float = DEFAULT_TIMEOUT,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Subscribe to `topic` for `timeout` seconds, return the last message
    per matching topic that showed up.

    Only the most recent message per topic is kept, not a running log -
    this is a snapshot of "what's currently there", not a traffic
    capture. Returns {"topics": {...}, "truncated": bool, ...} rather
    than a bare dict, same truncation-flag shape as find_entities/
    entity_health_report, so a caller that hits `limit` knows to narrow
    the topic filter instead of silently getting a partial answer.
    """
    mqtt, mqtt_config_entry_enabled = _import_mqtt()

    if not mqtt_config_entry_enabled(hass):
        raise MqttNotAvailableError(
            "The mqtt integration isn't configured on this instance - "
            "nothing to subscribe to"
        )

    timeout = max(0.5, min(timeout, MAX_TIMEOUT))
    limit = max(1, min(limit, MAX_LIMIT))

    messages: dict[str, dict[str, Any]] = {}
    truncated = False

    @callback
    def _on_message(msg: Any) -> None:
        nonlocal truncated
        if msg.topic not in messages and len(messages) >= limit:
            truncated = True
            return
        payload = msg.payload
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8", errors="replace")
        else:
            payload = str(payload)
        if len(payload) > MAX_PAYLOAD_CHARS:
            payload = payload[:MAX_PAYLOAD_CHARS] + "...<truncated>"
        messages[msg.topic] = {
            "payload": payload,
            "retain": msg.retain,
            "qos": msg.qos,
        }

    unsubscribe = await mqtt.async_subscribe(hass, topic, _on_message)
    try:
        await asyncio.sleep(timeout)
    finally:
        unsubscribe()

    return {
        "topic_filter": topic,
        "topics": messages,
        "count": len(messages),
        "truncated": truncated,
    }
