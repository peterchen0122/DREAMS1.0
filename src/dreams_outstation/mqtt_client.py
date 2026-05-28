from __future__ import annotations

import json
import logging
from typing import Any, Callable

import paho.mqtt.client as mqtt

from .models import MqttConfig, MqttTopic

LOGGER = logging.getLogger(__name__)

MessageHandler = Callable[[MqttTopic, dict[str, Any]], None]
KNOWN_TOPIC_SUFFIXES = {"snapshot", "event", "status", "cmd_ack", "cmd"}


class DreamsMqttClient:
    def __init__(self, config: MqttConfig, handler: MessageHandler):
        self.config = config
        self.handler = handler
        self.client = self._create_client()
        if config.username:
            self.client.username_pw_set(config.username, config.password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def start(self) -> None:
        LOGGER.info("Connecting MQTT broker %s:%s", self.config.host, self.config.port)
        self.client.connect(self.config.host, self.config.port, self.config.keepalive_seconds)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def publish_command(self, logger_id: str, payload: dict[str, Any]) -> None:
        topic = f"{self.config.root_topic}/{logger_id}/cmd"
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        result = self.client.publish(topic, body, qos=self.config.qos, retain=False)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish failed topic={topic} rc={result.rc}")
        LOGGER.info("Published MQTT command topic=%s cmd_id=%s", topic, payload.get("cmd_id"))

    def _create_client(self) -> mqtt.Client:
        try:
            return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.config.client_id)
        except (AttributeError, TypeError):
            return mqtt.Client(client_id=self.config.client_id)

    def _on_connect(self, client: mqtt.Client, _userdata: Any, _flags: Any, reason_code: Any, *_args: Any) -> None:
        if _reason_code_value(reason_code) != 0:
            LOGGER.error("MQTT connect failed reason=%s", reason_code)
            return
        LOGGER.info("MQTT connected")
        topic = f"{self.config.root_topic}/+/+"
        client.subscribe(topic, qos=self.config.qos)
        LOGGER.info("Subscribed MQTT topic=%s", topic)

    def _on_disconnect(self, _client: mqtt.Client, _userdata: Any, reason_code: Any, *_args: Any) -> None:
        LOGGER.warning("MQTT disconnected reason=%s", reason_code)

    def _on_message(self, _client: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage) -> None:
        topic_text = message.topic
        parsed = self._parse_topic(topic_text)
        if parsed is None:
            LOGGER.debug("Ignoring MQTT topic outside DREAMS layout: %s", topic_text)
            return
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except json.JSONDecodeError:
            LOGGER.exception("Invalid JSON on MQTT topic=%s", topic_text)
            return
        if not isinstance(payload, dict):
            LOGGER.error("MQTT payload must be an object topic=%s", topic_text)
            return
        try:
            self.handler(parsed, payload)
        except Exception:
            LOGGER.exception("MQTT handler failed topic=%s", topic_text)

    def _parse_topic(self, topic: str) -> MqttTopic | None:
        return parse_mqtt_topic(self.config.root_topic, topic)


def parse_mqtt_topic(root_topic: str, topic: str) -> MqttTopic | None:
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != root_topic:
        return None
    if parts[2].lower() in KNOWN_TOPIC_SUFFIXES:
        return MqttTopic(logger_id=parts[1], suffix=parts[2])
    return None


def _reason_code_value(reason_code: Any) -> int:
    value = getattr(reason_code, "value", reason_code)
    return int(value)
