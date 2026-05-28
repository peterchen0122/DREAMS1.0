from __future__ import annotations

import logging
import signal
import threading
import time
import uuid
from typing import Any

from .buffer import EventBufferStore
from .command_log import CommandLogStore
from .dnp3_gateway import Dnp3Gateway, create_dnp3_gateway
from .models import AppConfig, BufferedEvent, MqttTopic, SiteConfig
from .mqtt_client import DreamsMqttClient
from .points import AO_POINTS, build_mqtt_command
from .site_bindings import SiteBindingStore, apply_stored_bindings, load_effective_config
from .state import SiteState

LOGGER = logging.getLogger(__name__)

CLASS1_PERIODIC = 1
CLASS2_DEADBAND = 2
BINDING_RELOAD_CHECK_SECONDS = 2.0


class DreamsOutstationService:
    def __init__(self, config: AppConfig):
        self.base_config = config
        config, self.effective_config_status = load_effective_config(config)
        self.dnp3_id_lookup_status = self.effective_config_status["dreams_api"]
        self.config = config
        self._lock = threading.RLock()
        self._binding_signature = _dnp3_binding_signature(config)
        self._last_binding_check = 0.0
        self._started = False
        if self.dnp3_id_lookup_status.get("ok") is False:
            LOGGER.warning("DREAMS DNP3 ID lookup skipped/failed: %s", self.dnp3_id_lookup_status.get("message"))
        elif self.dnp3_id_lookup_status.get("applied"):
            LOGGER.info("DREAMS DNP3 ID lookup applied: %s", self.dnp3_id_lookup_status.get("message"))
        if self.effective_config_status["bindings"].get("count"):
            LOGGER.info("Applied %s DNP3 ID binding(s) from database", self.effective_config_status["bindings"]["count"])
        self.loggers_by_key: dict[str, SiteConfig] = {site.key: site for site in config.enabled_sites()}
        self.sites_by_id = self.loggers_by_key
        self.states: dict[str, SiteState] = {
            site.key: SiteState(site.key, config.dnp3.include_spare_point_31)
            for site in config.enabled_sites()
        }
        self.last_mqtt_targets: dict[str, str] = {}
        self.buffer = EventBufferStore(config.runtime.sqlite_path, config.dnp3.site_buffer_limit)
        self.command_log = CommandLogStore(config.runtime.sqlite_path)
        self.dnp3: Dnp3Gateway = create_dnp3_gateway(config)
        self.dnp3.set_command_callback(self.handle_ao_command)
        self.mqtt = DreamsMqttClient(config.mqtt, self.handle_mqtt_message)
        self.stop_event = threading.Event()
        self.scheduler_thread = threading.Thread(target=self._periodic_loop, name="periodic-scheduler", daemon=True)
        self.pending_commands: dict[str, dict[str, Any]] = {}

    def run_forever(self) -> None:
        self.start()
        self._install_signal_handlers()
        try:
            while not self.stop_event.is_set():
                self.reload_bindings_if_changed()
                self.flush_buffer_once()
                time.sleep(1)
        finally:
            self.stop()

    def start(self) -> None:
        LOGGER.info("Starting DREAMS outstation service")
        self.dnp3.start()
        self.mqtt.start()
        self.scheduler_thread.start()
        self._started = True

    def stop(self) -> None:
        LOGGER.info("Stopping DREAMS outstation service")
        self.stop_event.set()
        self._started = False
        try:
            self.mqtt.stop()
        finally:
            self.dnp3.stop()

    def handle_mqtt_message(self, topic: MqttTopic, payload: dict[str, Any]) -> None:
        with self._lock:
            logger_key = self._resolve_topic_logger_key(topic)
            if logger_key is None:
                LOGGER.debug("Ignoring MQTT message for unconfigured logger_id=%s topic_suffix=%s", topic.logger_id, topic.suffix)
                return
            self.last_mqtt_targets[logger_key] = topic.logger_id

            suffix = topic.suffix.lower()
            state = self.states[logger_key]
            if suffix == "snapshot":
                changed = state.apply_snapshot(payload)
                LOGGER.info("Snapshot received logger=%s key=%s changed=%s", topic.logger_id, logger_key, len(changed))
                reason = str(payload.get("reason", "")).lower()
                if reason in {"startup", "periodic"}:
                    self.send_periodic_snapshot(logger_key)
            elif suffix == "event":
                changed = state.apply_event(payload)
                dnp_values = state.dnp_values_for_changed(changed)
                LOGGER.info("Deadband event received logger=%s key=%s points=%s", topic.logger_id, logger_key, sorted(dnp_values))
                if dnp_values:
                    self._send_or_buffer(logger_key, dnp_values, CLASS2_DEADBAND, "event", payload, periodic=False)
            elif suffix == "cmd_ack":
                self.handle_command_ack(logger_key, payload, logger_id=topic.logger_id)
            elif suffix == "status":
                state.apply_status(payload)
                LOGGER.info("Status received logger=%s key=%s status=%s", topic.logger_id, logger_key, payload.get("status"))
            else:
                LOGGER.debug("Ignoring MQTT suffix=%s logger=%s", topic.suffix, topic.logger_id)

    def handle_ao_command(self, logger_key: str, ao_index: int, raw_value: float) -> bool:
        with self._lock:
            site = self.loggers_by_key.get(logger_key)
            state = self.states.get(logger_key)
            if site is None or state is None:
                LOGGER.error("DNP3 AO command for unknown logger=%s", logger_key)
                return False
            if ao_index not in AO_POINTS:
                LOGGER.error("Unsupported AO index logger=%s ao=%s", logger_key, ao_index)
                return False

            cmd_id = str(uuid.uuid4())
            payload = build_mqtt_command(ao_index, raw_value, cmd_id)
            payload["ts"] = int(time.time())
            state.reset_control_success()
            self.pending_commands[cmd_id] = {
                "logger_key": logger_key,
                "logger_id": site.logger_id,
                "dnp3_address": site.dnp3_address,
                "ao_index": ao_index,
                "raw_value": raw_value,
                "payload": payload,
            }
            mqtt_topic = None
            try:
                mqtt_logger_id = self._publish_target(logger_key, site)
                mqtt_topic = f"{self.config.mqtt.root_topic}/{mqtt_logger_id}/cmd"
                self.pending_commands[cmd_id]["logger_id"] = mqtt_logger_id
                self.mqtt.publish_command(mqtt_logger_id, payload)
                self.command_log.record_published(
                    logger_key=logger_key,
                    logger_id=mqtt_logger_id,
                    dnp3_address=site.dnp3_address,
                    source="dnp3_master",
                    mqtt_topic=mqtt_topic,
                    payload=payload,
                )
                LOGGER.info("Accepted DNP3 AO command logger=%s ao=%s raw=%s cmd_id=%s", logger_key, ao_index, raw_value, cmd_id)
                return True
            except Exception as exc:
                pending = self.pending_commands.pop(cmd_id, None) or {}
                LOGGER.exception("Failed to publish MQTT command for DNP3 AO logger=%s ao=%s", logger_key, ao_index)
                self.command_log.record_publish_failed(
                    logger_key=logger_key,
                    logger_id=str(pending.get("logger_id") or site.logger_id or logger_key),
                    dnp3_address=site.dnp3_address,
                    source="dnp3_master",
                    mqtt_topic=mqtt_topic,
                    payload=payload,
                    error_message=str(exc),
                )
                return False

    def handle_command_ack(self, logger_key: str, payload: dict[str, Any], logger_id: str | None = None) -> None:
        cmd_id = str(payload.get("cmd_id", ""))
        status = str(payload.get("status", "")).upper()
        pending = self.pending_commands.get(cmd_id)
        ack_logger_id = logger_id or str((pending or {}).get("logger_id") or logger_key)
        LOGGER.info("Command ack logger=%s cmd_id=%s status=%s", logger_key, cmd_id, status)
        if pending is None:
            self.command_log.record_ack(
                logger_key=logger_key,
                logger_id=ack_logger_id,
                payload=payload,
                error_message="No pending DNP3 command for this cmd_id",
            )
            return

        ao_index = int(pending["ao_index"])
        raw_value = float(pending["raw_value"])
        state = self.states[logger_key]
        if status == "SUCCESS":
            try:
                state.mark_control_success(int(payload.get("inverter_index", 1)))
            except Exception as exc:
                self.command_log.record_ack(
                    logger_key=logger_key,
                    logger_id=ack_logger_id,
                    payload=payload,
                    error_message=str(exc),
                )
                LOGGER.error("Invalid cmd_ack logger=%s cmd_id=%s error=%s", logger_key, cmd_id, exc)
                return
            ao = AO_POINTS[ao_index]
            if ao.feedback_ai is not None:
                state.update_ai(ao.feedback_ai, ao.engineering_value(raw_value))
            feedback = state.snapshot_dnp()
            points = {18: feedback[18], 19: feedback[19]}
            if ao.feedback_ai is not None and ao.feedback_ai in feedback:
                points[ao.feedback_ai] = feedback[ao.feedback_ai]
            self.pending_commands.pop(cmd_id, None)
            self.command_log.record_ack(logger_key=logger_key, logger_id=ack_logger_id, payload=payload, dnp_values=points)
            self._send_or_buffer(logger_key, points, CLASS2_DEADBAND, "cmd_ack", payload, periodic=False)
        elif status == "FAILED":
            self.pending_commands.pop(cmd_id, None)
            self.command_log.record_ack(logger_key=logger_key, logger_id=ack_logger_id, payload=payload)
        else:
            self.command_log.record_ack(
                logger_key=logger_key,
                logger_id=ack_logger_id,
                payload=payload,
                error_message=f"Unsupported cmd_ack status: {status or '(empty)'}",
            )

    def send_periodic_snapshot(self, site_id: str) -> None:
        with self._lock:
            state = self.states[site_id]
            dnp_values = state.snapshot_dnp()
            payload = {
                "ts": int(time.time()),
                "reason": "periodic_outstation",
                "data": state.snapshot_engineering(),
            }
            self._send_or_buffer(site_id, dnp_values, CLASS1_PERIODIC, "snapshot", payload, periodic=True)

    def flush_buffer_once(self) -> None:
        with self._lock:
            if not self.dnp3.available:
                return
            for site_id in self.loggers_by_key:
                for row in self.buffer.peek(site_id, limit=20):
                    payload = row["payload"]
                    data = payload.get("dnp_values")
                    if not isinstance(data, dict):
                        self.buffer.ack(row["id"])
                        continue
                    values = {int(index): int(value) for index, value in data.items()}
                    periodic = int(row["event_class"]) == CLASS1_PERIODIC
                    try:
                        self.dnp3.send_measurements(site_id, values, periodic=periodic)
                    except Exception:
                        LOGGER.exception("Buffered DNP3 replay failed key=%s event_id=%s", site_id, row["id"])
                        return
                    self.buffer.ack(row["id"])
                    LOGGER.info("Replayed buffered event key=%s event_id=%s", site_id, row["id"])

    def reload_bindings_if_changed(self, force: bool = False) -> bool:
        now = time.time()
        if not force and now - self._last_binding_check < BINDING_RELOAD_CHECK_SECONDS:
            return False
        self._last_binding_check = now
        try:
            config, binding_status = apply_stored_bindings(
                self.base_config,
                SiteBindingStore(self.base_config.runtime.sqlite_path),
            )
        except Exception:
            LOGGER.exception("Failed to check DNP3 ID bindings for reload")
            return False

        signature = _dnp3_binding_signature(config)
        with self._lock:
            if signature == self._binding_signature:
                return False
            old_signature = self._binding_signature
            LOGGER.info("DNP3 ID binding change detected; reloading DNP3 gateway from %s to %s", old_signature, signature)
            try:
                self._apply_reloaded_config(config, binding_status, signature)
            except Exception:
                LOGGER.exception("DNP3 gateway reload failed")
                return False
        return True

    def _apply_reloaded_config(
        self,
        config: AppConfig,
        binding_status: dict[str, Any],
        signature: tuple[tuple[str, str, str, int, bool], ...],
    ) -> None:
        was_started = self._started
        self.dnp3.stop()
        self.config = config
        self.effective_config_status["bindings"] = binding_status
        self.loggers_by_key = {site.key: site for site in config.enabled_sites()}
        self.sites_by_id = self.loggers_by_key
        self.states = self._states_for_config(config)
        self.last_mqtt_targets = {
            key: value
            for key, value in self.last_mqtt_targets.items()
            if key in self.loggers_by_key or value in self.loggers_by_key
        }
        self.command_log = CommandLogStore(config.runtime.sqlite_path)
        self.dnp3 = create_dnp3_gateway(config)
        self.dnp3.set_command_callback(self.handle_ao_command)
        self._binding_signature = signature
        if was_started:
            self.dnp3.start()
            LOGGER.info("DNP3 gateway reload completed; active bindings=%s", signature)

    def _states_for_config(self, config: AppConfig) -> dict[str, SiteState]:
        old_states = self.states
        states: dict[str, SiteState] = {}
        for site in config.enabled_sites():
            state = old_states.get(site.key)
            if state is None and not _is_wildcard(site.logger_id):
                source_key = next(
                    (
                        key
                        for key, target_logger in self.last_mqtt_targets.items()
                        if target_logger == site.logger_id and key in old_states
                    ),
                    None,
                )
                if source_key is not None:
                    state = old_states[source_key]
            if state is not None and state.include_spare_point_31 == config.dnp3.include_spare_point_31:
                states[site.key] = state
            else:
                states[site.key] = SiteState(site.key, config.dnp3.include_spare_point_31)
        return states

    def _send_or_buffer(
        self,
        site_id: str,
        dnp_values: dict[int, int],
        event_class: int,
        msg_type: str,
        payload: dict[str, Any],
        periodic: bool,
    ) -> None:
        envelope = dict(payload)
        envelope["dnp_values"] = {str(index): value for index, value in dnp_values.items()}
        if self.dnp3.available:
            try:
                self.dnp3.send_measurements(site_id, dnp_values, periodic=periodic)
                return
            except Exception:
                LOGGER.exception("DNP3 send failed; buffering event key=%s type=%s", site_id, msg_type)
        event = BufferedEvent(site_id=site_id, event_class=event_class, msg_type=msg_type, payload=envelope, priority=event_class)
        event_id = self.buffer.push(event)
        LOGGER.info("Buffered event key=%s event_id=%s type=%s", site_id, event_id, msg_type)

    def _periodic_loop(self) -> None:
        while not self.stop_event.is_set():
            sleep_seconds = self._seconds_until_next_period()
            if self.stop_event.wait(sleep_seconds):
                break
            with self._lock:
                site_ids = list(self.states)
            for site_id in site_ids:
                self.send_periodic_snapshot(site_id)

    def _seconds_until_next_period(self) -> float:
        period = max(1, self.config.runtime.periodic_seconds)
        now = time.time()
        return period - (now % period)

    def _resolve_topic_logger_key(self, topic: MqttTopic) -> str | None:
        for site in self.config.enabled_sites():
            if site.key in self.states and _site_matches_topic(site, topic):
                return site.key
        return None

    def _publish_target(self, service_site_id: str, site: SiteConfig) -> str:
        if not _is_wildcard(site.logger_id):
            return site.logger_id
        target = self.last_mqtt_targets.get(service_site_id)
        if target is None:
            raise RuntimeError(
                "No MQTT logger has been learned yet; wait for a snapshot/event/status before sending AO commands."
            )
        return target

    def _install_signal_handlers(self) -> None:
        def _handler(_signum, _frame):
            self.stop_event.set()

        try:
            signal.signal(signal.SIGINT, _handler)
            signal.signal(signal.SIGTERM, _handler)
        except ValueError:
            return


def _is_wildcard(value: str) -> bool:
    return value.strip() in {"", "*"}


def _site_matches_topic(site: SiteConfig, topic: MqttTopic) -> bool:
    logger_matches = _is_wildcard(site.logger_id) or site.logger_id == topic.logger_id
    return logger_matches


def _dnp3_binding_signature(config: AppConfig) -> tuple[tuple[str, str, str, int, bool], ...]:
    return tuple(
        sorted(
            (
                site.key,
                site.site_id,
                site.logger_id,
                int(site.dnp3_address),
                bool(site.enabled),
            )
            for site in config.sites
        )
    )
