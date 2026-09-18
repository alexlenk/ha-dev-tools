"""Tests for the read-only MQTT topic snapshot (mqtt_manager.py).

Like supervisor_manager.py, there's no way to stand up a real MQTT
broker in this test environment. These tests mock
`homeassistant.components.mqtt.async_subscribe` directly and invoke the
captured callback synchronously - simulating a broker's real behavior of
delivering retained messages immediately on subscribe, before any new
live traffic - rather than exercising a real broker round-trip.

mqtt/mqtt_config_entry_enabled are patched at their real source
(homeassistant.components.mqtt / .mqtt.util), not on mqtt_manager
itself, because list_topics() imports them lazily (inside the function,
not at module level - see mqtt_manager.py's docstring for why) - patching
the module-level name wouldn't exist to patch.
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_dev_tools.mqtt_manager import (
    MAX_PAYLOAD_CHARS,
    MqttNotAvailableError,
    _import_mqtt,
    list_topics,
)


def _fake_message(topic: str, payload, *, retain: bool = True, qos: int = 0):
    return SimpleNamespace(topic=topic, payload=payload, retain=retain, qos=qos)


def test_import_mqtt_raises_when_not_installed():
    """Simulates a real HA install that has never set up the mqtt
    integration at all, so paho-mqtt was never pip-installed and
    homeassistant.components.mqtt itself can't be imported - the
    scenario mqtt_manager's lazy-import docstring is about."""
    with patch.dict(sys.modules, {"homeassistant.components.mqtt": None}):
        with pytest.raises(MqttNotAvailableError):
            _import_mqtt()


@pytest.mark.asyncio
async def test_list_topics_raises_when_mqtt_not_configured(hass: HomeAssistant):
    """A default test hass has no mqtt config entry - Core-only, like most real installs."""
    with pytest.raises(MqttNotAvailableError):
        await list_topics(hass)


def _patched(*, enabled: bool, messages: list):
    """Context manager stack: mqtt "enabled", async_subscribe delivering
    `messages` synchronously to the captured callback, and asyncio.sleep
    skipped so tests run instantly rather than waiting the real timeout."""
    unsubscribe = AsyncMock()

    async def fake_subscribe(hass, topic, msg_callback, *args, **kwargs):
        for msg in messages:
            msg_callback(msg)
        return unsubscribe

    return (
        patch(
            "homeassistant.components.mqtt.util.mqtt_config_entry_enabled",
            return_value=enabled,
        ),
        patch(
            "homeassistant.components.mqtt.async_subscribe",
            side_effect=fake_subscribe,
        ),
        patch(
            "custom_components.ha_dev_tools.mqtt_manager.asyncio.sleep",
            AsyncMock(),
        ),
        unsubscribe,
    )


@pytest.mark.asyncio
async def test_list_topics_collects_retained_messages(hass: HomeAssistant):
    messages = [
        _fake_message("watermeter/uptime", "1234", retain=True),
        _fake_message("watermeter/flow", "0.5", retain=True, qos=1),
    ]
    p1, p2, p3, unsubscribe = _patched(enabled=True, messages=messages)
    with p1, p2, p3:
        result = await list_topics(hass, topic="watermeter/#")

    assert result["topic_filter"] == "watermeter/#"
    assert result["count"] == 2
    assert result["truncated"] is False
    assert result["topics"]["watermeter/uptime"] == {
        "payload": "1234",
        "retain": True,
        "qos": 0,
    }
    assert result["topics"]["watermeter/flow"]["qos"] == 1
    unsubscribe.assert_called_once()


@pytest.mark.asyncio
async def test_list_topics_keeps_latest_message_per_topic(hass: HomeAssistant):
    """A running log isn't the point - only the last value per topic matters."""
    messages = [
        _fake_message("watermeter/uptime", "1111"),
        _fake_message("watermeter/uptime", "2222"),
    ]
    p1, p2, p3, _ = _patched(enabled=True, messages=messages)
    with p1, p2, p3:
        result = await list_topics(hass, topic="watermeter/#")

    assert result["count"] == 1
    assert result["topics"]["watermeter/uptime"]["payload"] == "2222"


@pytest.mark.asyncio
async def test_list_topics_decodes_bytes_and_truncates_long_payload(
    hass: HomeAssistant,
):
    long_payload = "x" * (MAX_PAYLOAD_CHARS + 500)
    messages = [
        _fake_message("device/binary", b"raw-bytes"),
        _fake_message("device/long", long_payload),
    ]
    p1, p2, p3, _ = _patched(enabled=True, messages=messages)
    with p1, p2, p3:
        result = await list_topics(hass, topic="device/#")

    assert result["topics"]["device/binary"]["payload"] == "raw-bytes"
    truncated_payload = result["topics"]["device/long"]["payload"]
    assert truncated_payload.endswith("...<truncated>")
    assert len(truncated_payload) < len(long_payload)


@pytest.mark.asyncio
async def test_list_topics_truncates_at_limit(hass: HomeAssistant):
    messages = [_fake_message(f"device/{i}", "x") for i in range(5)]
    p1, p2, p3, _ = _patched(enabled=True, messages=messages)
    with p1, p2, p3:
        result = await list_topics(hass, topic="device/#", limit=3)

    assert result["count"] == 3
    assert result["truncated"] is True
