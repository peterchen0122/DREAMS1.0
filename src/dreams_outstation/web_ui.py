from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import socket
import threading
import time
import uuid
from dataclasses import asdict, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import paho.mqtt.client as mqtt

from .buffer import EventBufferStore
from .command_log import CommandLogStore
from .config import load_config
from .dreams_api import build_plant_dnp3_id_url, fetch_plant_dnp3_ids
from .models import AppConfig, DreamsApiConfig, MqttTopic, SiteConfig
from .mqtt_client import _reason_code_value
from .mqtt_client import parse_mqtt_topic as parse_dreams_mqtt_topic
from .points import AI_POINTS, AO_POINTS, build_mqtt_command, enabled_ai_points
from .site_bindings import SiteBindingStore, load_effective_config
from .state import SiteState

DEFAULT_UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8088
DEFAULT_UI_USERNAME = "admin"
DEFAULT_UI_PASSWORD = "dreams"
SESSION_COOKIE = "dreams_ui_session"
SESSION_TTL_SECONDS = 8 * 60 * 60


class WebUiState:
    def __init__(self, config_path: str | Path, username: str, password: str):
        self.config_path = Path(config_path)
        self.config, self.effective_config_status = self._load_config()
        self.dnp3_id_lookup_status = self.effective_config_status["dreams_api"]
        self.username = username
        self.password = password
        self._sessions: dict[str, float] = {}
        self._session_lock = threading.RLock()
        self.live_monitor = LivePointMonitor(self.config)

    @property
    def pid_path(self) -> Path:
        return Path(self.config.runtime.log_path).with_suffix(".pid")

    def start(self) -> None:
        self.live_monitor.start()

    def stop(self) -> None:
        self.live_monitor.stop()

    def reload_config(self) -> None:
        self.config, self.effective_config_status = self._load_config()
        self.dnp3_id_lookup_status = self.effective_config_status["dreams_api"]
        self.live_monitor.restart(self.config)

    def _load_config(self) -> tuple[AppConfig, dict[str, Any]]:
        return load_effective_config(load_config(self.config_path))

    def binding_store(self) -> SiteBindingStore:
        return SiteBindingStore(self.config.runtime.sqlite_path)

    def command_log_store(self) -> CommandLogStore:
        return CommandLogStore(self.config.runtime.sqlite_path)

    def authenticate(self, username: str, password: str) -> bool:
        supplied_username = username.encode("utf-8")
        supplied_password = password.encode("utf-8")
        expected_username = self.username.encode("utf-8")
        expected_password = self.password.encode("utf-8")
        return hmac.compare_digest(supplied_username, expected_username) and hmac.compare_digest(
            supplied_password,
            expected_password,
        )

    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._session_lock:
            self._sessions[token] = time.time() + SESSION_TTL_SECONDS
        return token

    def validate_session(self, token: str | None) -> bool:
        if not token:
            return False
        now = time.time()
        with self._session_lock:
            expires_at = self._sessions.get(token)
            if expires_at is None or expires_at <= now:
                self._sessions.pop(token, None)
                return False
            self._sessions[token] = now + SESSION_TTL_SECONDS
            return True

    def clear_session(self, token: str | None) -> None:
        if not token:
            return
        with self._session_lock:
            self._sessions.pop(token, None)


class LivePointMonitor:
    def __init__(self, config: AppConfig):
        self._lock = threading.RLock()
        self._client: mqtt.Client | None = None
        self._running = False
        self._set_config(config)

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        self._start_client()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            client = self._client
            self._client = None
        if client is not None:
            client.loop_stop()
            client.disconnect()

    def restart(self, config: AppConfig) -> None:
        self.stop()
        with self._lock:
            self._set_config(config)
        self.start()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            sites: list[dict[str, Any]] = []
            configured_keys = {site.key for site in self.config.sites}
            dynamic_keys = sorted(key for key in self.states if key not in configured_keys and not _is_wildcard(key))
            wildcard_template = next((site for site in self.config.sites if _is_wildcard(site.key)), None)

            def append_site(site: SiteConfig, service_key: str) -> None:
                state = self.states.get(service_key)
                meta = self.meta.get(service_key, {})
                values = state.snapshot_engineering() if state is not None else {}
                points = []
                for index, point in enabled_ai_points(self.config.dnp3.include_spare_point_31).items():
                    value = values.get(index, point.default)
                    points.append(
                        {
                            "index": index,
                            "key": point.key,
                            "name": point.name,
                            "unit": point.unit,
                            "value": value,
                            "dnp_value": point.to_dnp_value(value),
                            "updated_at": self.updated_at.get(service_key, {}).get(index),
                        }
                    )
                sites.append(
                    {
                        "key": service_key,
                        "site_id": site.site_id,
                        "logger_id": site.logger_id,
                        "actual_site_id": meta.get("actual_site_id"),
                        "actual_logger_id": meta.get("actual_logger_id"),
                        "enabled": site.enabled,
                        "online": state.online if state is not None else False,
                        "seen": bool(meta.get("seen")),
                        "last_snapshot_ts": state.last_snapshot_ts if state is not None else None,
                        "last_event_ts": state.last_event_ts if state is not None else None,
                        "last_status_ts": meta.get("last_status_ts"),
                        "last_message_ts": meta.get("last_message_ts"),
                        "last_topic": meta.get("last_topic"),
                        "last_type": meta.get("last_type"),
                        "last_error": meta.get("last_error"),
                        "points": points,
                    }
                )

            for site in self.config.sites:
                if _is_wildcard(site.key) and dynamic_keys and not self.meta.get(site.key, {}).get("seen"):
                    continue
                append_site(site, site.key)

            for key in dynamic_keys:
                template = wildcard_template or SiteConfig(site_id="*", logger_id=key, dnp3_address=0)
                append_site(
                    SiteConfig(
                        site_id="*",
                        logger_id=key,
                        dnp3_address=template.dnp3_address,
                        enabled=True,
                        dnp3_address_source="live",
                    ),
                    key,
                )
            return {
                "mqtt_connected": self.connected,
                "last_error": self.last_error,
                "last_connect_ts": self.last_connect_ts,
                "sites": sites,
            }

    def apply_mqtt_message(self, topic: str, payload: dict[str, Any]) -> None:
        parsed = _parse_mqtt_topic(self.config.mqtt.root_topic, topic)
        if parsed is None:
            return
        logger_key = self._resolve_topic_logger_key(parsed)
        if logger_key is None:
            return
        if _is_wildcard(logger_key) and not _is_wildcard(parsed.logger_id):
            logger_key = parsed.logger_id

        now = int(time.time())
        suffix = parsed.suffix.lower()
        with self._lock:
            state = self.states.get(logger_key)
            if state is None:
                state = SiteState(logger_key, self.config.dnp3.include_spare_point_31)
                self.states[logger_key] = state
                self.meta[logger_key] = {}
                self.updated_at[logger_key] = {}
        changed: dict[int, float] = {}
        error: str | None = None
        try:
            if suffix == "snapshot":
                changed = state.apply_snapshot(payload)
            elif suffix == "event":
                changed = state.apply_event(payload)
            elif suffix == "status":
                state.apply_status(payload)
            elif suffix == "cmd_ack":
                self._apply_command_ack(state, payload)
            else:
                return
        except Exception as exc:
            error = str(exc)

        if suffix == "cmd_ack":
            try:
                self.command_log.record_ack(
                    logger_key=logger_key,
                    logger_id=parsed.logger_id,
                    payload=payload,
                    error_message=error,
                )
            except Exception as exc:
                error = error or str(exc)

        with self._lock:
            meta = self.meta.setdefault(logger_key, {})
            meta["seen"] = True
            meta["actual_site_id"] = parsed.site_id
            meta["actual_logger_id"] = parsed.logger_id
            meta["last_message_ts"] = now
            meta["last_topic"] = topic
            meta["last_type"] = suffix
            meta["last_error"] = error
            if suffix == "status":
                meta["last_status_ts"] = int(payload.get("ts") or now)
            updates = self.updated_at.setdefault(logger_key, {})
            for index in changed:
                updates[index] = now

    def _set_config(self, config: AppConfig) -> None:
        self.config = config
        self.states = {
            site.key: SiteState(site.key, config.dnp3.include_spare_point_31)
            for site in config.enabled_sites()
        }
        self.meta: dict[str, dict[str, Any]] = {site.key: {} for site in config.enabled_sites()}
        self.updated_at: dict[str, dict[int, int]] = {site.key: {} for site in config.enabled_sites()}
        self.command_log = CommandLogStore(config.runtime.sqlite_path)
        self.connected = False
        self.last_error: str | None = None
        self.last_connect_ts: int | None = None

    def _start_client(self) -> None:
        config = self.config
        client = _create_mqtt_client(f"{config.mqtt.client_id}-ui-live-{uuid.uuid4().hex[:8]}")
        if config.mqtt.username:
            client.username_pw_set(config.mqtt.username, config.mqtt.password)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        with self._lock:
            self._client = client
        try:
            client.connect(config.mqtt.host, config.mqtt.port, config.mqtt.keepalive_seconds)
            client.loop_start()
        except Exception as exc:
            with self._lock:
                self.connected = False
                self.last_error = str(exc)

    def _on_connect(self, client: mqtt.Client, _userdata: Any, _flags: Any, reason_code: Any, *_args: Any) -> None:
        ok = _reason_code_value(reason_code) == 0
        with self._lock:
            self.connected = ok
            self.last_connect_ts = int(time.time()) if ok else self.last_connect_ts
            self.last_error = None if ok else str(reason_code)
        if ok:
            client.subscribe(f"{self.config.mqtt.root_topic}/+/+", qos=self.config.mqtt.qos)

    def _on_disconnect(self, _client: mqtt.Client, _userdata: Any, reason_code: Any, *_args: Any) -> None:
        with self._lock:
            self.connected = False
            self.last_error = str(reason_code) if reason_code is not None else None

    def _on_message(self, _client: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            if isinstance(payload, dict):
                self.apply_mqtt_message(message.topic, payload)
        except Exception as exc:
            with self._lock:
                self.last_error = str(exc)

    def _apply_command_ack(self, state: SiteState, payload: dict[str, Any]) -> None:
        status = str(payload.get("status", "")).upper()
        if status == "SUCCESS":
            state.mark_control_success(int(payload.get("inverter_index", 1)))

    def _resolve_topic_logger_key(self, topic: MqttTopic) -> str | None:
        for site in self.config.enabled_sites():
            if site.key in self.states and _site_matches_topic(site, topic):
                return site.key
        if not _is_wildcard(topic.logger_id):
            return topic.logger_id
        return None


def run_web_ui(
    config_path: str | Path,
    host: str = DEFAULT_UI_HOST,
    port: int = DEFAULT_UI_PORT,
    username: str = DEFAULT_UI_USERNAME,
    password: str = DEFAULT_UI_PASSWORD,
) -> None:
    state = WebUiState(config_path, username, password)
    handler = _make_handler(state)
    server = ThreadingHTTPServer((host, port), handler)
    state.start()
    print(f"DREAMS Outstation UI listening on http://{host}:{port}")
    print(f"DREAMS Outstation UI username: {username}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop()
        server.server_close()


def build_status_payload(state: WebUiState) -> dict[str, Any]:
    config = state.config
    dnp3_host = "127.0.0.1" if config.dnp3.bind in {"0.0.0.0", "::"} else config.dnp3.bind
    pid = _read_pid(state.pid_path)
    service_running = _pid_running(pid) if pid is not None else False
    buffer = _buffer_payload(config)
    logger_bindings = _logger_bindings_payload(state)
    return {
        "generated_at": int(time.time()),
        "config_path": str(state.config_path),
        "service": {
            "pid": pid,
            "running": service_running,
            "pid_path": str(state.pid_path),
        },
        "dnp3": {
            "bind": config.dnp3.bind,
            "host_checked": dnp3_host,
            "port": config.dnp3.port,
            "listening": service_running,
            "status_source": "service_pid",
            "master_address": config.dnp3.master_address,
            "ai_event_class": config.dnp3.ai_event_class,
        },
        "mqtt": {
            "host": config.mqtt.host,
            "port": config.mqtt.port,
            "root_topic": config.mqtt.root_topic,
            "qos": config.mqtt.qos,
            "client_id": config.mqtt.client_id,
            "username": config.mqtt.username,
            "password": "********" if config.mqtt.password else None,
            "tcp_reachable": _tcp_check(config.mqtt.host, config.mqtt.port, timeout=0.75),
        },
        "runtime": asdict(config.runtime),
        "dreams_api": _dreams_api_payload(config, state.dnp3_id_lookup_status),
        "logger_bindings": logger_bindings,
        "site_bindings": logger_bindings,
        "sites": [_site_payload(site) for site in config.sites],
        "buffer": buffer,
    }


def build_points_payload(config: AppConfig) -> dict[str, Any]:
    return {
        "ai": [
            {
                "index": index,
                "key": point.key,
                "name": point.name,
                "unit": point.unit,
                "scale": point.scale,
                "static_variation": point.static_variation,
                "event_variation": point.event_variation,
                "deadband_trigger_raw": point.deadband_trigger_raw,
                "enabled": point.enabled or index in enabled_ai_points(config.dnp3.include_spare_point_31),
                "class2_enabled": point.class2_enabled,
                "default": point.default,
            }
            for index, point in sorted(AI_POINTS.items())
        ],
        "ao": [
            {
                "index": index,
                "name": point.name,
                "unit": point.unit,
                "command_type": point.command_type,
                "target": point.target,
                "feedback_ai": point.feedback_ai,
                "value_scale": point.value_scale,
                "reserved": point.reserved,
            }
            for index, point in sorted(AO_POINTS.items())
        ],
    }


def send_ui_command(
    config: AppConfig,
    site_id: str,
    ao_index: int,
    raw_value: float,
    inverter_index: int | None = None,
    logger_id: str | None = None,
    command_log: CommandLogStore | None = None,
) -> dict[str, Any]:
    target_logger_id = (logger_id or site_id).strip()
    site = _find_command_site(config, target_logger_id)
    cmd_id = str(uuid.uuid4())
    payload = build_mqtt_command(ao_index, raw_value, cmd_id)
    payload["ts"] = int(time.time())
    payload["source"] = "dreams-outstation-ui"
    if inverter_index is not None:
        payload["inverter_index"] = inverter_index

    mqtt_logger_id = site.logger_id if not _is_wildcard(site.logger_id) else target_logger_id
    if _is_wildcard(mqtt_logger_id):
        raise RuntimeError("送出命令前需要具體的 MQTT logger_id。")

    topic = f"{config.mqtt.root_topic}/{mqtt_logger_id}/cmd"
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    client = _create_mqtt_client(f"{config.mqtt.client_id}-ui-{uuid.uuid4().hex[:8]}")
    if config.mqtt.username:
        client.username_pw_set(config.mqtt.username, config.mqtt.password)

    connected = threading.Event()
    failed: list[str] = []

    def on_connect(_client: mqtt.Client, _userdata: Any, _flags: Any, reason_code: Any, *_args: Any) -> None:
        if _reason_code_value(reason_code) == 0:
            connected.set()
        else:
            failed.append(str(reason_code))
            connected.set()

    client.on_connect = on_connect
    try:
        client.connect(config.mqtt.host, config.mqtt.port, config.mqtt.keepalive_seconds)
        client.loop_start()
        if not connected.wait(timeout=5):
            raise TimeoutError("MQTT 連線逾時")
        if failed:
            raise RuntimeError(f"MQTT 連線失敗：{failed[0]}")
        result = client.publish(topic, body, qos=config.mqtt.qos, retain=False)
        result.wait_for_publish(timeout=5)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT 發布失敗 rc={result.rc}")
        if command_log is not None:
            command_log.record_published(
                logger_key=site.key,
                logger_id=mqtt_logger_id,
                dnp3_address=site.dnp3_address,
                source="outstation_ui",
                mqtt_topic=topic,
                payload=payload,
            )
    except Exception as exc:
        if command_log is not None:
            command_log.record_publish_failed(
                logger_key=site.key,
                logger_id=mqtt_logger_id,
                dnp3_address=site.dnp3_address,
                source="outstation_ui",
                mqtt_topic=topic,
                payload=payload,
                error_message=str(exc),
            )
        raise
    finally:
        client.loop_stop()
        client.disconnect()

    return {"topic": topic, "payload": payload}


def _binding_targets(state: WebUiState) -> list[dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {}

    def add_target(
        logger_id: str,
        source: str,
        actual_site_id: str | None = None,
    ) -> None:
        if _is_wildcard(logger_id):
            return
        key = logger_id
        existing = targets.get(key)
        if existing is None:
            targets[key] = {
                "site_id": "*",
                "logger_id": key,
                "actual_site_id": actual_site_id or "",
                "source": source,
            }
            return
        if actual_site_id and not existing.get("actual_site_id"):
            existing["actual_site_id"] = actual_site_id
        if source not in existing["source"].split("+"):
            existing["source"] = f"{existing['source']}+{source}"

    live = state.live_monitor.snapshot()
    for site in live.get("sites", []):
        actual_logger_id = str(site.get("actual_logger_id") or "")
        actual_site_id = str(site.get("actual_site_id") or "")
        add_target(actual_logger_id, "live", actual_site_id=actual_site_id)

    for site in state.config.sites:
        add_target(site.logger_id, "config")

    for binding in state.binding_store().list_bindings():
        add_target(binding["logger_id"], "database")

    return list(targets.values())


def _make_handler(state: WebUiState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "DreamsOutstationUI/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/login":
                if self._is_authenticated():
                    self._redirect("/")
                else:
                    self._send_html(LOGIN_HTML)
                return
            if not self._is_authenticated():
                self._send_auth_required(api=parsed.path.startswith("/api/"))
                return

            if parsed.path == "/":
                self._send_html(INDEX_HTML)
            elif parsed.path == "/api/status":
                self._send_json(build_status_payload(state))
            elif parsed.path == "/api/points":
                self._send_json(build_points_payload(state.config))
            elif parsed.path == "/api/live":
                self._send_json(state.live_monitor.snapshot())
            elif parsed.path == "/api/logs":
                query = parse_qs(parsed.query)
                lines = _query_int(query, "lines", 200, minimum=1, maximum=1000)
                self._send_json({"path": state.config.runtime.log_path, "lines": _tail_lines(state.config.runtime.log_path, lines)})
            elif parsed.path == "/api/buffer":
                self._send_json(_buffer_payload(state.config))
            elif parsed.path == "/api/command-log":
                query = parse_qs(parsed.query)
                limit = _query_int(query, "limit", 50, minimum=1, maximum=200)
                self._send_json(_command_log_payload(state, limit=limit))
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "找不到資源")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/login":
                form = self._read_form_body()
                username = form.get("username", [""])[0]
                password = form.get("password", [""])[0]
                if state.authenticate(username, password):
                    token = state.create_session()
                    self._redirect("/", cookie=_session_cookie_header(token))
                else:
                    self._send_html(LOGIN_HTML.replace("{{error}}", "帳號或密碼錯誤"), status=HTTPStatus.UNAUTHORIZED)
                return
            if parsed.path == "/logout":
                state.clear_session(self._session_token())
                self._redirect("/login", cookie=f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
                return
            if not self._is_authenticated():
                self._send_auth_required(api=parsed.path.startswith("/api/"))
                return

            if parsed.path == "/api/reload":
                state.reload_config()
                self._send_json({"ok": True, "status": build_status_payload(state)})
            elif parsed.path == "/api/dnp3-id/lookup":
                try:
                    body = self._read_json_body()
                    dreams_api = _dreams_api_from_request(state.config.dreams_api, body)
                    plants = fetch_plant_dnp3_ids(dreams_api)
                    state.binding_store().save_api_plants(plants)
                    self._send_json({"ok": True, "url": build_plant_dnp3_id_url(dreams_api), "plants": plants})
                except Exception as exc:
                    self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            elif parsed.path == "/api/bindings":
                try:
                    body = self._read_json_body()
                    site_id, logger_id = _validate_binding_target(body.get("site_id"), body.get("logger_id"))
                    dnp3_address = _validated_dnp3_address(body.get("dnp3_address"))
                    binding = state.binding_store().upsert_binding(
                        site_id=site_id,
                        logger_id=logger_id,
                        plant_no="",
                        plant_name="",
                        dnp3_address=dnp3_address,
                        source="database",
                        updated_by="ui",
                    )
                    state.reload_config()
                    self._send_json(
                        {
                            "ok": True,
                            "binding": binding,
                            "auto_reload": _service_auto_reload_available(state),
                            "restart_required": False,
                            "status": build_status_payload(state),
                        }
                    )
                except Exception as exc:
                    self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            elif parsed.path == "/api/bindings/clear":
                try:
                    body = self._read_json_body()
                    site_id, logger_id = _validate_binding_target(body.get("site_id"), body.get("logger_id"))
                    state.binding_store().clear_binding(site_id, logger_id)
                    state.reload_config()
                    self._send_json(
                        {
                            "ok": True,
                            "auto_reload": _service_auto_reload_available(state),
                            "restart_required": False,
                            "status": build_status_payload(state),
                        }
                    )
                except Exception as exc:
                    self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            elif parsed.path == "/api/commands":
                try:
                    body = self._read_json_body()
                    result = send_ui_command(
                        state.config,
                        site_id=str(body["site_id"]),
                        ao_index=int(body["ao_index"]),
                        raw_value=float(body["raw_value"]),
                        inverter_index=_optional_int(body.get("inverter_index")),
                        logger_id=_optional_str(body.get("logger_id")),
                        command_log=state.command_log_store(),
                    )
                except Exception as exc:
                    self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                self._send_json({"ok": True, **result})
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "找不到資源")

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("JSON body 必須是物件")
            return data

        def _read_form_body(self) -> dict[str, list[str]]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return parse_qs(raw, keep_blank_values=True)

        def _is_authenticated(self) -> bool:
            return state.validate_session(self._session_token())

        def _session_token(self) -> str | None:
            return _parse_cookies(self.headers.get("Cookie", "")).get(SESSION_COOKIE)

        def _send_auth_required(self, api: bool) -> None:
            if api:
                self._send_error(HTTPStatus.UNAUTHORIZED, "需要登入")
            else:
                self._redirect("/login")

        def _redirect(self, location: str, cookie: str | None = None) -> None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_html(self, html: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            html = html.replace("{{error}}", "")
            data = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            data = json.dumps({"ok": False, "error": message}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def _create_mqtt_client(client_id: str) -> mqtt.Client:
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except (AttributeError, TypeError):
        return mqtt.Client(client_id=client_id)


def _parse_mqtt_topic(root_topic: str, topic: str) -> MqttTopic | None:
    return parse_dreams_mqtt_topic(root_topic, topic)


def _is_wildcard(value: str) -> bool:
    return value.strip() in {"", "*"}


def _site_matches_topic(site: SiteConfig, topic: MqttTopic) -> bool:
    logger_matches = _is_wildcard(site.logger_id) or site.logger_id == topic.logger_id
    return logger_matches


def _session_cookie_header(token: str) -> str:
    return f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_TTL_SECONDS}; HttpOnly; SameSite=Lax"


def _parse_cookies(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in header.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies[name.strip()] = value.strip()
    return cookies


def _buffer_payload(config: AppConfig) -> dict[str, Any]:
    store = EventBufferStore(config.runtime.sqlite_path, config.dnp3.site_buffer_limit)
    rows = store.peek_all(limit_per_site=10)
    return {
        "sqlite_path": config.runtime.sqlite_path,
        "total": store.count(),
        "by_site": [{"site_id": site.key, "logger_id": site.logger_id, "count": store.count(site.key)} for site in config.sites],
        "recent": rows[-30:],
    }


def _find_enabled_site(config: AppConfig, site_id: str) -> SiteConfig:
    for site in config.enabled_sites():
        if site.key == site_id:
            return site
    raise KeyError(f"未知或停用的 logger：{site_id}")


def _find_command_site(config: AppConfig, logger_id: str) -> SiteConfig:
    for site in config.enabled_sites():
        if not _is_wildcard(site.logger_id) and site.logger_id == logger_id:
            return site
    for site in config.enabled_sites():
        topic = MqttTopic(logger_id=logger_id, suffix="cmd")
        if _site_matches_topic(site, topic):
            return site
    raise KeyError(f"未知或停用的 MQTT logger：{logger_id}")


def _site_payload(site: SiteConfig) -> dict[str, Any]:
    return {
        "key": site.key,
        "site_id": site.site_id,
        "logger_id": site.logger_id,
        "dnp3_address": site.dnp3_address,
        "enabled": site.enabled,
        "plant_no": site.plant_no,
        "plant_name": site.plant_name,
        "dnp3_address_source": site.dnp3_address_source,
    }


def _dreams_api_payload(config: AppConfig, lookup_status: dict[str, Any]) -> dict[str, Any]:
    try:
        url = build_plant_dnp3_id_url(config.dreams_api)
    except ValueError:
        url = None
    return {
        "enabled": config.dreams_api.enabled,
        "base_url": config.dreams_api.base_url,
        "plant_meter_no": config.dreams_api.plant_meter_no,
        "site_token": "********" if config.dreams_api.site_token else None,
        "timeout_seconds": config.dreams_api.timeout_seconds,
        "verify_tls": config.dreams_api.verify_tls,
        "apply_to_sites": config.dreams_api.apply_to_sites,
        "url": url,
        "lookup_status": lookup_status,
    }


def _dreams_api_from_request(config: DreamsApiConfig, body: dict[str, Any]) -> DreamsApiConfig:
    base_url = _optional_str(body.get("base_url")) or config.base_url
    plant_meter_no = _optional_str(body.get("plant_meter_no")) or config.plant_meter_no
    site_token = _optional_str(body.get("site_token"))
    if site_token in {None, "********"}:
        site_token = config.site_token
    return replace(
        config,
        base_url=base_url.rstrip("/"),
        plant_meter_no=plant_meter_no,
        site_token=site_token,
    )


def _logger_bindings_payload(state: WebUiState) -> dict[str, Any]:
    store = SiteBindingStore(state.config.runtime.sqlite_path)
    return {
        "sqlite_path": state.config.runtime.sqlite_path,
        "table": "logger_bindings",
        "bindings": store.list_bindings(),
        "api_plants": store.list_api_plants(),
        "targets": _binding_targets(state),
    }


def _site_bindings_payload(state: WebUiState) -> dict[str, Any]:
    return _logger_bindings_payload(state)


def _command_log_payload(state: WebUiState, limit: int = 50) -> dict[str, Any]:
    store = state.command_log_store()
    return {
        "sqlite_path": state.config.runtime.sqlite_path,
        "table": "command_log",
        "commands": store.recent(limit=limit),
    }


def _tail_lines(path: str | Path, lines: int = 200, max_bytes: int = 200_000) -> list[str]:
    log_path = Path(path)
    if not log_path.exists():
        return []
    with log_path.open("rb") as fh:
        try:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
        except OSError:
            fh.seek(0)
        text = fh.read().decode("utf-8", errors="replace")
    return text.splitlines()[-lines:]


def _query_int(
    query: dict[str, list[str]],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(query.get(name, [str(default)])[0])
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    return int(value)


def _optional_str(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    return str(value)


def _validated_dnp3_address(value: Any) -> int:
    try:
        address = int(value)
    except (TypeError, ValueError):
        raise ValueError("dnp3_address 必須是整數")
    if address < 0 or address > 65535:
        raise ValueError("dnp3_address 必須介於 0 到 65535")
    return address


def _validate_binding_target(site_id_value: Any, logger_id_value: Any) -> tuple[str, str]:
    logger_id = str(logger_id_value or "").strip()
    if _is_wildcard(logger_id):
        raise ValueError("綁定 DNP3 ID 前需要具體的 MQTT logger_id。")
    return "*", logger_id


def _service_auto_reload_available(state: WebUiState) -> bool:
    pid = _read_pid(state.pid_path)
    return _pid_running(pid) if pid is not None else False


def _read_pid(path: str | Path) -> int | None:
    pid_path = Path(path)
    if not pid_path.exists():
        return None
    text = pid_path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    try:
        return int(text.splitlines()[0])
    except ValueError:
        return None


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _tcp_check(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="DREAMS Outstation web operation interface")
    parser.add_argument("--config", default="config/config.yaml", help="Path to YAML config file")
    parser.add_argument("--host", default=DEFAULT_UI_HOST, help="UI bind host")
    parser.add_argument("--port", type=int, default=DEFAULT_UI_PORT, help="UI bind port")
    parser.add_argument("--username", default=os.getenv("DREAMS_UI_USERNAME", DEFAULT_UI_USERNAME), help="UI login username")
    parser.add_argument("--password", default=os.getenv("DREAMS_UI_PASSWORD", DEFAULT_UI_PASSWORD), help="UI login password")
    args = parser.parse_args()
    run_web_ui(args.config, args.host, args.port, args.username, args.password)


LOGIN_HTML = r"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DREAMS Outstation 登入</title>
  <style>
    :root {
      --bg: #eef2f6;
      --panel: #ffffff;
      --line: #d9dee7;
      --text: #18202a;
      --muted: #657084;
      --bad: #bd3038;
      --accent: #0f766e;
      --accent-strong: #0b5f59;
      --sans: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      min-height: 100vh;
      margin: 0;
      display: grid;
      place-items: center;
      padding: 24px;
      background: var(--bg);
      color: var(--text);
      font-family: var(--sans);
      letter-spacing: 0;
    }
    .panel {
      width: min(420px, 100%);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 22px;
      box-shadow: 0 18px 40px rgba(15, 23, 42, 0.08);
    }
    h1 {
      margin: 0;
      font-size: 21px;
      line-height: 1.2;
    }
    p {
      margin: 8px 0 0;
      color: var(--muted);
      line-height: 1.45;
    }
    form {
      display: grid;
      gap: 12px;
      margin-top: 18px;
    }
    label {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    input, button {
      min-height: 38px;
      border-radius: 6px;
      border: 1px solid var(--line);
      font: inherit;
    }
    input {
      padding: 0 10px;
      color: var(--text);
    }
    button {
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
      font-weight: 750;
      cursor: pointer;
    }
    button:hover { background: var(--accent-strong); }
    .error {
      min-height: 22px;
      color: var(--bad);
      font-weight: 700;
    }
  </style>
</head>
<body>
  <section class="panel">
    <h1>DREAMS Outstation 控制台</h1>
    <p>登入後可查看狀態、即時點值、紀錄與手動命令。</p>
    <form method="post" action="/login">
      <div class="error">{{error}}</div>
      <label>帳號
        <input name="username" autocomplete="username" autofocus>
      </label>
      <label>密碼
        <input name="password" type="password" autocomplete="current-password">
      </label>
      <button type="submit">登入</button>
    </form>
  </section>
</body>
</html>
"""


INDEX_HTML = r"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DREAMS Outstation 控制台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --line: #d9dee7;
      --text: #18202a;
      --muted: #657084;
      --good: #11845b;
      --bad: #bd3038;
      --warn: #a76500;
      --accent: #0f766e;
      --accent-strong: #0b5f59;
      --soft: #edf5f4;
      --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      --sans: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: var(--sans);
      font-size: 14px;
      letter-spacing: 0;
    }

    header {
      position: sticky;
      top: 0;
      z-index: 3;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.96);
      backdrop-filter: blur(10px);
    }

    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      max-width: 1480px;
      margin: 0 auto;
      padding: 14px 20px;
    }

    h1 {
      margin: 0;
      font-size: 18px;
      line-height: 1.2;
      font-weight: 700;
    }

    .subtle { color: var(--muted); }
    .mono { font-family: var(--mono); }

    .actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .actions form { margin: 0; }

    button, select, input {
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      font: inherit;
    }

    button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
      padding: 0 12px;
      cursor: pointer;
      font-weight: 650;
    }

    button.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: white;
    }

    button.primary:hover { background: var(--accent-strong); }
    button:hover { border-color: #aeb7c6; }
    button:disabled { cursor: not-allowed; opacity: 0.6; }

    main {
      max-width: 1480px;
      margin: 0 auto;
      padding: 18px 20px 28px;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }

    .panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfd;
    }

    .panel-title {
      margin: 0;
      font-size: 14px;
      font-weight: 750;
    }

    .panel-body { padding: 14px; }

    .metric {
      min-height: 118px;
      padding: 14px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }

    .metric-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }

    .metric-value {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 10px;
      font-size: 23px;
      line-height: 1.1;
      font-weight: 800;
    }

    .metric-detail {
      margin-top: 10px;
      color: var(--muted);
      line-height: 1.45;
      word-break: break-word;
    }

    .dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--muted);
      flex: 0 0 auto;
    }

    .dot.good { background: var(--good); }
    .dot.bad { background: var(--bad); }
    .dot.warn { background: var(--warn); }

    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(360px, 0.9fr);
      gap: 12px;
      margin-top: 12px;
      align-items: start;
    }

    .form-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }

    label {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }

    select, input {
      width: 100%;
      padding: 0 10px;
    }

    .span-2 { grid-column: 1 / -1; }
    .send-row {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      margin-top: 12px;
      flex-wrap: wrap;
    }

    .result {
      min-height: 40px;
      margin-top: 12px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfd;
      color: var(--muted);
      line-height: 1.45;
    }

    .result.ok {
      border-color: #9bd6c0;
      background: #eefaf5;
      color: #0f6b4a;
    }

    .result.error {
      border-color: #efb0b4;
      background: #fff1f2;
      color: #a51f29;
    }

    .result.compact {
      min-height: 0;
      margin: 10px 14px 14px;
      padding: 8px 10px;
    }

    .row-actions {
      display: flex;
      gap: 6px;
      align-items: center;
    }

    .panel-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    button.danger {
      border-color: #efb0b4;
      color: var(--bad);
      background: #fff;
    }

    button.danger:hover {
      border-color: var(--bad);
      background: #fff1f2;
    }

    button.small {
      min-height: 30px;
      padding: 0 9px;
      font-size: 12px;
    }

    .section-label {
      margin-top: 14px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }

    .flow-steps {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }

    .step {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfd;
      padding: 8px 10px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 750;
    }

    .advanced-box {
      margin-top: 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfd;
      padding: 10px 12px;
    }

    .advanced-box summary {
      cursor: pointer;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }

    .advanced-box .form-grid { margin-top: 12px; }

    .toolbar-grid {
      display: grid;
      grid-template-columns: minmax(140px, 0.8fr) minmax(140px, 1fr) minmax(160px, 1.1fr) auto;
      gap: 10px;
      align-items: end;
    }

    .command-log-wrap {
      max-height: 420px;
      border-top: 1px solid var(--line);
    }

    .details-row td {
      white-space: normal;
      background: #fbfcfd;
    }

    .payload-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }

    .payload-box {
      min-height: 120px;
      max-height: 240px;
      overflow: auto;
      margin: 6px 0 0;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #101418;
      color: #d9f3ed;
      font: 12px/1.45 var(--mono);
      white-space: pre-wrap;
    }

    .command-note {
      margin-top: 10px;
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfd;
      color: var(--muted);
      line-height: 1.45;
    }

    .lookup-table {
      margin-top: 8px;
      border-top: 1px solid var(--line);
    }

    .lookup-table table { min-width: 260px; }

    .table-wrap { overflow: auto; }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 680px;
    }

    th, td {
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      white-space: nowrap;
    }

    th {
      position: sticky;
      top: 0;
      background: #fbfcfd;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      z-index: 1;
    }

    .tabs {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }

    .tab {
      min-height: 32px;
      padding: 0 10px;
      border-radius: 6px;
      border: 1px solid var(--line);
      background: #fff;
    }

    .tab.active {
      border-color: var(--accent);
      background: var(--soft);
      color: var(--accent-strong);
    }

    .log {
      height: 310px;
      overflow: auto;
      margin: 0;
      padding: 12px;
      background: #101418;
      color: #d9f3ed;
      font: 12px/1.5 var(--mono);
      white-space: pre-wrap;
      word-break: break-word;
    }

    .stack {
      display: grid;
      gap: 12px;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 8px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
      font-size: 12px;
      font-weight: 750;
    }

    .pill.good { border-color: #9bd6c0; color: var(--good); background: #eefaf5; }
    .pill.bad { border-color: #efb0b4; color: var(--bad); background: #fff1f2; }

    .login-page {
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      background: #eef2f6;
    }

    .login-panel {
      width: min(420px, 100%);
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 22px;
      box-shadow: 0 18px 40px rgba(15, 23, 42, 0.08);
    }

    .login-panel h1 {
      font-size: 21px;
      margin-bottom: 8px;
    }

    .login-form {
      display: grid;
      gap: 12px;
      margin-top: 18px;
    }

    .login-error {
      min-height: 22px;
      color: var(--bad);
      font-weight: 700;
    }

    .live-meta {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
    }

    @media (max-width: 1080px) {
      .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .layout { grid-template-columns: 1fr; }
      .payload-grid { grid-template-columns: 1fr; }
      .toolbar-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }

    @media (max-width: 680px) {
      .topbar { align-items: flex-start; flex-direction: column; }
      .actions { width: 100%; justify-content: flex-start; }
      .grid { grid-template-columns: 1fr; }
      .form-grid { grid-template-columns: 1fr; }
      .flow-steps { grid-template-columns: 1fr; }
      .toolbar-grid { grid-template-columns: 1fr; }
      .span-2 { grid-column: auto; }
      main { padding: 12px; }
      .metric-value { font-size: 20px; }
    }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div>
        <h1>DREAMS Outstation 控制台</h1>
        <div class="subtle mono" id="configPath">載入設定中...</div>
      </div>
      <div class="actions">
        <button id="reloadConfig" title="重新載入 config.yaml">重新載入設定</button>
        <button id="refresh" class="primary" title="重新整理狀態">重新整理</button>
        <form method="post" action="/logout">
          <button type="submit" title="登出">登出</button>
        </form>
      </div>
    </div>
  </header>

  <main>
    <section class="grid">
      <div class="panel metric">
        <div>
          <div class="metric-label">DNP3</div>
          <div class="metric-value"><span id="dnp3Dot" class="dot"></span><span id="dnp3State">未知</span></div>
        </div>
        <div class="metric-detail mono" id="dnp3Detail">-</div>
      </div>
      <div class="panel metric">
        <div>
          <div class="metric-label">MQTT TCP</div>
          <div class="metric-value"><span id="mqttDot" class="dot"></span><span id="mqttState">未知</span></div>
        </div>
        <div class="metric-detail mono" id="mqttDetail">-</div>
      </div>
      <div class="panel metric">
        <div>
          <div class="metric-label">服務 PID</div>
          <div class="metric-value"><span id="pidDot" class="dot"></span><span id="pidState">未知</span></div>
        </div>
        <div class="metric-detail mono" id="pidDetail">-</div>
      </div>
      <div class="panel metric">
        <div>
          <div class="metric-label">暫存</div>
          <div class="metric-value"><span id="bufferState">0</span></div>
        </div>
        <div class="metric-detail mono" id="bufferDetail">-</div>
      </div>
    </section>

    <section class="layout">
      <div class="stack">
        <div class="panel">
          <div class="panel-head">
            <h2 class="panel-title">Logger 清單</h2>
            <span class="pill" id="siteCount">0 個 logger</span>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr><th>Logger</th><th>綁定</th><th>DNP3 ID</th><th>最後 MQTT</th><th>最後類型</th><th>暫存</th><th>操作</th></tr>
              </thead>
              <tbody id="siteRows"></tbody>
            </table>
          </div>
          <div id="loggerActionResult" class="result compact">已儲存的綁定可在此表解除。</div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <div>
              <h2 class="panel-title">即時 AI 點值</h2>
              <div class="live-meta" id="liveMeta">等待 MQTT 資料...</div>
            </div>
            <select id="liveSite" style="max-width: 190px"></select>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr><th>索引</th><th>名稱</th><th>值</th><th>單位</th><th>DNP Raw</th><th>更新時間</th></tr>
              </thead>
              <tbody id="liveRows"></tbody>
            </table>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h2 class="panel-title">點表</h2>
            <div class="tabs">
              <button class="tab active" data-tab="ao">AO</button>
              <button class="tab" data-tab="ai">AI</button>
            </div>
          </div>
          <div class="table-wrap">
            <table id="aoTable">
              <thead>
                <tr><th>索引</th><th>名稱</th><th>目標</th><th>單位</th><th>倍率</th><th>回饋 AI</th></tr>
              </thead>
              <tbody id="aoRows"></tbody>
            </table>
            <table id="aiTable" style="display:none">
              <thead>
                <tr><th>索引</th><th>名稱</th><th>單位</th><th>倍率</th><th>靜態</th><th>事件</th><th>Class 2</th></tr>
              </thead>
              <tbody id="aiRows"></tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="stack">
        <div class="panel">
          <div class="panel-head">
            <h2 class="panel-title">手動 AO 命令</h2>
            <span class="pill">MQTT cmd</span>
          </div>
          <div class="panel-body">
            <div class="form-grid">
              <label>Logger
                <select id="cmdSite"></select>
              </label>
              <label>AO 點位
                <select id="cmdAo"></select>
              </label>
              <label>Raw 值
                <input id="cmdRaw" type="number" step="any" value="0">
              </label>
              <label>變流器索引
                <input id="cmdInverter" type="number" min="1" max="50" placeholder="選填">
              </label>
              <label class="span-2">預覽
                <input id="cmdPreview" class="mono" readonly>
              </label>
            </div>
            <div id="cmdHelper" class="command-note">選擇 logger 與 AO 點位後，可預覽命令路由。</div>
            <div class="send-row">
              <button id="sendCommand" class="primary">送出命令</button>
            </div>
            <div id="commandResult" class="result">就緒。</div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h2 class="panel-title">命令紀錄</h2>
            <div class="panel-actions">
              <button id="refreshCommands">重新整理命令</button>
            </div>
          </div>
          <div class="panel-body">
            <div class="toolbar-grid">
              <label>狀態
                <select id="commandStatusFilter">
                  <option value="">全部</option>
                  <option value="PUBLISHED">已發布</option>
                  <option value="SUCCESS">成功</option>
                  <option value="FAILED">失敗</option>
                  <option value="UNKNOWN_ACK">未知 ACK</option>
                  <option value="PUBLISH_FAILED">發布失敗</option>
                  <option value="ACK_ERROR">ACK 錯誤</option>
                </select>
              </label>
              <label>Logger
                <input id="commandLoggerFilter" class="mono" placeholder="logger_id">
              </label>
              <label>cmd_id
                <input id="commandIdFilter" class="mono" placeholder="搜尋 cmd_id">
              </label>
              <button id="clearCommandFilters">清除</button>
            </div>
          </div>
          <div class="table-wrap command-log-wrap">
            <table>
              <thead>
                <tr><th>更新時間</th><th>狀態</th><th>Logger</th><th>DNP3 ID</th><th>AO</th><th>類型</th><th>目標</th><th>值</th><th>Inv</th><th>回饋 AI</th><th>cmd_id</th><th>細節</th></tr>
              </thead>
              <tbody id="commandRows">
                <tr><td colspan="12" class="subtle">尚無命令</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h2 class="panel-title">DREAMS API</h2>
            <span class="pill" id="dreamsApiEnabled">停用</span>
          </div>
          <div class="panel-body">
            <div class="flow-steps">
              <div class="step">1. 取得 DNP3 ID</div>
              <div class="step">2. 選擇 Logger</div>
              <div class="step">3. 儲存綁定</div>
            </div>
            <div class="form-grid">
              <label>電號
                <input id="dreamsApiMeter" class="mono">
              </label>
              <label>Token
                <input id="dreamsApiToken" class="mono">
              </label>
            </div>
            <div class="send-row">
              <button id="lookupDnp3Ids">取得 DNP3 ID</button>
            </div>
            <div id="dnp3LookupResult" class="result">就緒。</div>
            <details class="advanced-box">
              <summary>進階 API 設定</summary>
              <div class="form-grid">
                <label class="span-2">DREAMS Base URL
                  <input id="dreamsApiUrl" class="mono">
                </label>
                <label>套用至 Logger
                  <input id="dreamsApiApply" readonly>
                </label>
                <label>TLS 驗證
                  <input id="dreamsApiTls" readonly>
                </label>
              </div>
            </details>
            <div class="section-label">已取得的 DNP3 ID</div>
            <div class="table-wrap lookup-table">
              <table>
                <thead>
                  <tr><th>DNP3 ID</th></tr>
                </thead>
                <tbody id="dnp3LookupRows">
                  <tr><td class="subtle">尚未查詢</td></tr>
                </tbody>
              </table>
            </div>
            <div class="section-label">Logger 綁定</div>
            <div class="form-grid" style="margin-top:12px">
              <label>已看到的 Logger ID
                <select id="bindingSite"></select>
              </label>
              <label>Logger ID
                <input id="bindingLoggerId" class="mono" placeholder="輸入 logger_id">
              </label>
              <label>查詢取得的 DNP3 ID
                <select id="bindingPlant"></select>
              </label>
              <label>DNP3 ID
                <input id="bindingDnp3Address" type="number" min="0" max="65535" placeholder="輸入 DNP3 ID">
              </label>
              <label>啟用方式
                <input value="由 Outstation service 自動套用" readonly>
              </label>
            </div>
            <div class="send-row">
              <button id="saveBinding" class="primary">儲存綁定</button>
              <button id="clearBinding">清除綁定</button>
            </div>
            <div id="bindingResult" class="result">選擇或輸入 logger_id 與 DNP3 ID，然後儲存綁定。</div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h2 class="panel-title">近期紀錄</h2>
            <div class="panel-actions">
              <button id="refreshLogs">重新整理紀錄</button>
              <button id="clearLogView">清除畫面</button>
            </div>
          </div>
          <pre class="log" id="logBox"></pre>
        </div>
      </div>
    </section>
  </main>

  <script>
    const state = {
      status: null,
      points: null,
      live: null,
      commands: null,
      commandDetailsOpen: new Set(),
      dnp3Lookup: null,
      dreamsApiInput: null,
      logText: '',
      logCleared: false,
      logClearBaseline: null
    };

    const $ = (id) => document.getElementById(id);

    function setDot(id, ok, warn = false) {
      const el = $(id);
      el.className = 'dot ' + (ok ? 'good' : warn ? 'warn' : 'bad');
    }

    function text(id, value) {
      $(id).textContent = value == null ? '-' : String(value);
    }

    function inputValue(id, value) {
      $(id).value = value == null || value === '' ? '-' : String(value);
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: { 'Content-Type': 'application/json' },
        ...options
      });
      if (response.status === 401) {
        window.location.href = '/login';
        throw new Error('需要登入');
      }
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || response.statusText);
      }
      return data;
    }

    async function refreshStatus() {
      const data = await api('/api/status');
      state.status = data;
      renderStatus(data);
    }

    async function refreshPoints() {
      const data = await api('/api/points');
      state.points = data;
      renderPoints(data);
      updateCommandPreview();
    }

    async function refreshLive() {
      const data = await api('/api/live');
      state.live = data;
      renderLive(data);
    }

    async function refreshLogs(options = {}) {
      const data = await api('/api/logs?lines=240');
      const box = $('logBox');
      const joined = data.lines.join('\n');
      state.logText = joined;
      if (options.force) {
        state.logCleared = false;
        state.logClearBaseline = '';
      }
      if (state.logCleared) {
        const baseline = state.logClearBaseline;
        if (baseline == null) {
          state.logClearBaseline = joined;
          box.textContent = '紀錄畫面已清除，新紀錄會顯示在這裡。';
        } else if (!joined || joined === baseline) {
          box.textContent = '紀錄畫面已清除，新紀錄會顯示在這裡。';
        } else if (baseline && joined.startsWith(`${baseline}\n`)) {
          box.textContent = joined.slice(baseline.length + 1) || '紀錄畫面已清除，新紀錄會顯示在這裡。';
        } else {
          box.textContent = joined;
        }
      } else {
        box.textContent = joined || '目前沒有紀錄。';
      }
      box.scrollTop = box.scrollHeight;
    }

    function clearLogView() {
      state.logCleared = true;
      state.logClearBaseline = state.logText || null;
      const box = $('logBox');
      box.textContent = '紀錄畫面已清除，新紀錄會顯示在這裡。';
      box.scrollTop = 0;
    }

    async function refreshCommands() {
      const data = await api('/api/command-log?limit=50');
      state.commands = data;
      renderCommandLog(data.commands || []);
    }

    function renderStatus(data) {
      text('configPath', data.config_path);

      setDot('dnp3Dot', data.dnp3.listening);
      text('dnp3State', data.dnp3.listening ? '服務運行中' : '未啟動');
      text('dnp3Detail', `${data.dnp3.bind}:${data.dnp3.port} master=${data.dnp3.master_address} check=pid`);

      setDot('mqttDot', data.mqtt.tcp_reachable);
      text('mqttState', data.mqtt.tcp_reachable ? '可連線' : 'TCP 不通');
      text('mqttDetail', `${data.mqtt.host}:${data.mqtt.port} topic=${data.mqtt.root_topic}/+/+`);

      setDot('pidDot', data.service.running, data.service.pid && !data.service.running);
      text('pidState', data.service.running ? String(data.service.pid) : '未執行');
      text('pidDetail', data.service.pid_path);

      text('bufferState', data.buffer.total);
      text('bufferDetail', data.buffer.sqlite_path);

      renderDreamsApi(data.dreams_api || {}, loggerBindings(data));
      renderSites(data);
      fillSiteSelect(data);
      fillBindingControls(data);
    }

    function renderDreamsApi(config, bindings) {
      const status = config.lookup_status || {};
      const enabled = $('dreamsApiEnabled');
      enabled.className = 'pill ' + (config.enabled ? 'good' : '');
      enabled.textContent = config.enabled ? '啟動時查詢' : '手動模式';
      const apiInput = state.dreamsApiInput || {};
      if (!document.activeElement || document.activeElement.id !== 'dreamsApiUrl') {
        inputValue('dreamsApiUrl', apiInput.base_url || config.base_url);
      }
      if (!document.activeElement || document.activeElement.id !== 'dreamsApiMeter') {
        inputValue('dreamsApiMeter', apiInput.plant_meter_no || config.plant_meter_no);
      }
      if (!document.activeElement || document.activeElement.id !== 'dreamsApiToken') {
        inputValue('dreamsApiToken', apiInput.site_token || config.site_token);
      }
      inputValue('dreamsApiApply', config.apply_to_sites ? '是' : '否');
      inputValue('dreamsApiTls', config.verify_tls ? '是' : '否');
      if (!state.dnp3Lookup && status.plants && status.plants.length) {
        renderLookupPlants(status.plants);
      } else if (!state.dnp3Lookup && bindings.api_plants && bindings.api_plants.length) {
        renderLookupPlants(bindings.api_plants);
      }
      if (!state.dnp3Lookup && status.message) {
        const result = $('dnp3LookupResult');
        result.className = 'result ' + (status.ok === false ? 'error' : status.applied ? 'ok' : '');
        result.textContent = status.message;
      }
    }

    function renderSites(data) {
      const counts = new Map(data.buffer.by_site.map(row => [row.site_id, row.count]));
      const liveSites = state.live?.sites || [];
      const liveByLogger = new Map(liveSites.map(site => [site.actual_logger_id || site.logger_id || site.key, site]));
      const configuredKeys = new Set((data.sites || []).map(site => site.key || site.logger_id || site.site_id));
      const liveOnly = liveSites
        .filter(site => site.actual_logger_id && !configuredKeys.has(site.key || site.actual_logger_id))
        .map(site => ({
          key: site.key || site.actual_logger_id,
          site_id: '*',
          logger_id: site.actual_logger_id,
          dnp3_address: site.dnp3_address || '-',
          enabled: site.enabled,
          dnp3_address_source: 'live'
        }));
      const rows = (data.sites || [])
        .filter(site => !((site.key || site.logger_id || site.site_id) === '*' && liveOnly.length))
        .concat(liveOnly);
      const bindingByLogger = new Map((loggerBindings(data).bindings || []).map(binding => [binding.logger_id, binding]));
      $('siteCount').textContent = `${rows.length} 個 logger`;
      $('siteRows').innerHTML = rows.map(site => {
        const key = site.key || site.logger_id || site.site_id;
        const live = liveByLogger.get(site.logger_id || key) || liveByLogger.get(key);
        const loggerText = live?.actual_logger_id || site.logger_id || key;
        const binding = bindingByLogger.get(loggerText);
        const bound = Boolean(binding);
        const dnp3 = binding?.dnp3_address ?? site.dnp3_address ?? '-';
        const lastType = live?.last_type || '-';
        const lastTs = live?.last_message_ts;
        return `
          <tr>
            <td class="mono">${escapeHtml(loggerText)}</td>
            <td>${bound ? '<span class="pill good">已綁定</span>' : '<span class="pill bad">未綁定</span>'} ${sourcePill(binding?.dnp3_address_source || site.dnp3_address_source)}</td>
            <td class="mono">${escapeHtml(dnp3)}</td>
            <td>${escapeHtml(formatTs(lastTs))}</td>
            <td>${lastType === '-' ? '<span class="subtle">-</span>' : `<span class="pill">${escapeHtml(lastType)}</span>`}</td>
            <td>${escapeHtml(counts.get(key) || 0)}</td>
            <td>${loggerAction(loggerText, bound)}</td>
          </tr>
        `;
      }).join('');
      wireLoggerActions();
    }

    function renderCommandLog(commands) {
      const rows = filteredCommands(commands);
      if (!rows.length) {
        $('commandRows').innerHTML = '<tr><td colspan="12" class="subtle">沒有符合條件的命令</td></tr>';
        return;
      }
      $('commandRows').innerHTML = rows.map(command => {
        const payloadTitle = escapeHtml(JSON.stringify(command.command_payload || {}));
        const ackTitle = escapeHtml(JSON.stringify(command.ack_payload || {}));
        const isOpen = state.commandDetailsOpen.has(command.cmd_id);
        return `
          <tr title="cmd=${payloadTitle} ack=${ackTitle}">
            <td>${escapeHtml(formatTs(command.updated_at))}</td>
            <td>${commandStatusPill(command)}</td>
            <td class="mono">${escapeHtml(command.logger_id || command.logger_key || '-')}</td>
            <td class="mono">${escapeHtml(command.dnp3_address ?? '-')}</td>
            <td class="mono">${command.ao_index == null ? '-' : `AO_${escapeHtml(command.ao_index)}`}</td>
            <td class="mono">${escapeHtml(command.command_type || '-')}</td>
            <td class="mono">${escapeHtml(command.target || '-')}</td>
            <td class="mono">${escapeHtml(commandValueText(command))}</td>
            <td class="mono">${escapeHtml(command.inverter_index ?? command.ack_payload?.inverter_index ?? '-')}</td>
            <td class="mono">${escapeHtml(feedbackText(command.dnp_values || {}))}</td>
            <td class="mono">${escapeHtml(shortCmdId(command.cmd_id))}</td>
            <td><button class="small command-detail-toggle" data-cmd-id="${escapeHtml(command.cmd_id)}">${isOpen ? '收合' : '查看'}</button></td>
          </tr>
          ${isOpen ? commandDetailRow(command) : ''}
        `;
      }).join('');
      wireCommandDetails();
    }

    function filteredCommands(commands) {
      const status = $('commandStatusFilter').value;
      const logger = $('commandLoggerFilter').value.trim().toLowerCase();
      const cmdId = $('commandIdFilter').value.trim().toLowerCase();
      return commands.filter(command => {
        const commandStatus = String(command.status || '');
        if (status && commandStatus !== status) return false;
        if (logger) {
          const text = `${command.logger_id || ''} ${command.logger_key || ''}`.toLowerCase();
          if (!text.includes(logger)) return false;
        }
        if (cmdId && !String(command.cmd_id || '').toLowerCase().includes(cmdId)) return false;
        return true;
      });
    }

    function commandDetailRow(command) {
      const error = command.error_message
        ? `<div><strong>錯誤</strong><pre class="payload-box">${escapeHtml(command.error_message)}</pre></div>`
        : '';
      return `
        <tr class="details-row">
          <td colspan="12">
            <div class="payload-grid">
              <div><strong>MQTT cmd</strong><pre class="payload-box">${escapeHtml(JSON.stringify(command.command_payload || {}, null, 2))}</pre></div>
              <div><strong>cmd_ack</strong><pre class="payload-box">${escapeHtml(JSON.stringify(command.ack_payload || {}, null, 2))}</pre></div>
              <div><strong>DNP3 回饋</strong><pre class="payload-box">${escapeHtml(JSON.stringify(command.dnp_values || {}, null, 2))}</pre></div>
              ${error}
            </div>
          </td>
        </tr>
      `;
    }

    function wireCommandDetails() {
      document.querySelectorAll('.command-detail-toggle').forEach(button => {
        button.addEventListener('click', () => {
          const cmdId = button.dataset.cmdId || '';
          if (state.commandDetailsOpen.has(cmdId)) {
            state.commandDetailsOpen.delete(cmdId);
          } else {
            state.commandDetailsOpen.add(cmdId);
          }
          renderCommandLog(state.commands?.commands || []);
        });
      });
    }

    function commandStatusPill(command) {
      const status = command.status || '-';
      const text = command.ack_status ? `${status}/${command.ack_status}` : status;
      const className = status === 'SUCCESS'
        ? 'pill good'
        : ['FAILED', 'PUBLISH_FAILED', 'ACK_ERROR', 'UNKNOWN_ACK'].includes(status)
          ? 'pill bad'
          : 'pill';
      const title = command.error_message ? ` title="${escapeHtml(command.error_message)}"` : '';
      return `<span class="${className}"${title}>${escapeHtml(text)}</span>`;
    }

    function commandValueText(command) {
      const value = command.value ?? command.raw_value;
      if (value == null || value === '') return '-';
      return `${formatValue(value)} ${command.unit || ''}`.trim();
    }

    function feedbackText(values) {
      const entries = Object.entries(values || {})
        .sort((a, b) => Number(a[0]) - Number(b[0]))
        .map(([index, value]) => `AI_${index}=${value}`);
      return entries.length ? entries.join(', ') : '-';
    }

    function shortCmdId(cmdId) {
      const text = String(cmdId || '');
      return text.length > 12 ? `${text.slice(0, 8)}...` : text || '-';
    }

    function loggerAction(loggerId, canUnbind) {
      if (!canUnbind) return '<span class="subtle">-</span>';
      return `
        <div class="row-actions">
          <button class="danger unbind-logger" data-logger-id="${escapeHtml(loggerId)}">解除綁定</button>
        </div>
      `;
    }

    function wireLoggerActions() {
      document.querySelectorAll('.unbind-logger').forEach(button => {
        button.addEventListener('click', () => clearLoggerBinding(button.dataset.loggerId || '', $('loggerActionResult'), button));
      });
    }

    function sourcePill(source) {
      const text = source || 'config';
      return text === 'dreams_api'
        ? '<span class="pill good">DREAMS API</span>'
        : text === 'database'
          ? '<span class="pill good">SQLite</span>'
        : `<span class="pill">${escapeHtml(bindingSourceText(text))}</span>`;
    }

    function bindingSourceText(source) {
      const labels = { config: '設定檔', live: '即時', database: 'SQLite', dreams_api: 'DREAMS API' };
      return String(source || 'config')
        .split('+')
        .map(item => labels[item] || item)
        .join('+');
    }

    function fillBindingControls(data) {
      if (bindingFormHasFocus()) return;
      const siteSelect = $('bindingSite');
      const selectedSite = siteSelect.value;
      const targets = bindingTargets(data);
      siteSelect.innerHTML = targets.length
        ? targets.map(target => {
            const key = bindingSiteKey(target.site_id, target.logger_id);
            const label = `${target.logger_id} (${bindingSourceText(target.source)})`;
            return `<option value="${escapeHtml(key)}">${escapeHtml(label)}</option>`;
          }).join('')
        : '<option value="">尚未看到具體 logger_id</option>';
      if ([...siteSelect.options].some(option => option.value === selectedSite)) {
        siteSelect.value = selectedSite;
      }

      const plantSelect = $('bindingPlant');
      const plants = bindingPlantOptions(data);
      const selectedPlant = plantSelect.value;
      plantSelect.innerHTML = '<option value="">手動輸入</option>' + plants.map(plant => {
        const key = bindingPlantKey(plant);
        const label = `DNP3 ${plant.dnp3Address}`;
        return `<option value="${escapeHtml(key)}">${escapeHtml(label)}</option>`;
      }).join('');
      const hasSelectedPlantOption = [...plantSelect.options].some(option => option.value === selectedPlant);
      if (selectedPlant && hasSelectedPlantOption) {
        plantSelect.value = selectedPlant;
      }

      applySelectedSiteBinding(data);
      if (plantSelect.value) {
        applySelectedPlant(data);
      }
    }

    function bindingFormHasFocus() {
      return ['bindingSite', 'bindingLoggerId', 'bindingPlant', 'bindingDnp3Address']
        .includes(document.activeElement && document.activeElement.id);
    }

    function bindingTargets(data = state.status) {
      const targets = new Map();
      const add = (loggerId, source) => {
        const logger = loggerId || '';
        if (!logger || logger === '*') return;
        const key = bindingSiteKey('*', logger);
        if (!targets.has(key)) {
          targets.set(key, { site_id: '*', logger_id: logger, source });
        } else {
          const target = targets.get(key);
          if (!target.source.split('+').includes(source)) target.source = `${target.source}+${source}`;
        }
      };
      (state.live?.sites || []).forEach(site => add(site.actual_logger_id, 'live'));
      (loggerBindings(data).targets || []).forEach(target => add(target.logger_id, target.source));
      (data?.sites || []).forEach(site => add(site.logger_id, 'config'));
      return [...targets.values()];
    }

    function loggerBindings(data = state.status) {
      return data?.logger_bindings || data?.site_bindings || {};
    }

    function bindingSiteKey(siteId, loggerId) {
      return loggerId || '';
    }

    function splitBindingSiteKey(value) {
      return { site_id: '*', logger_id: String(value || '') };
    }

    function bindingPlantOptions(data = state.status) {
      const plants = state.dnp3Lookup?.plants
        || loggerBindings(data).api_plants
        || data?.dreams_api?.lookup_status?.plants
        || [];
      return uniqueDnp3Records(plants);
    }

    function uniqueDnp3Records(plants) {
      const seen = new Set();
      return plants.filter(plant => {
        const key = bindingPlantKey(plant);
        if (!key || seen.has(key)) return false;
        seen.add(key);
        return true;
      });
    }

    function bindingPlantKey(plant) {
      return String(plant.dnp3Address ?? '');
    }

    function selectFetchedPlant(plant, data = state.status) {
      if (!plant) return;
      $('bindingPlant').value = bindingPlantKey(plant);
      applySelectedPlant(data);
    }

    function applySelectedSiteBinding(data = state.status) {
      if (!data) return;
      const selected = splitBindingSiteKey($('bindingSite').value);
      if (selected.logger_id) $('bindingLoggerId').value = selected.logger_id;
      const loggerId = selected.logger_id || $('bindingLoggerId').value.trim();
      const site = (data.sites || []).find(item => item.logger_id === loggerId);
      const binding = (loggerBindings(data).bindings || []).find(item => item.logger_id === loggerId);
      const target = binding || site;
      if (!target) {
        $('bindingDnp3Address').value = '';
        return;
      }
      $('bindingDnp3Address').value = target.dnp3_address || '';
    }

    function applySelectedPlant(data = state.status) {
      const key = $('bindingPlant').value;
      if (!key) return;
      const plant = bindingPlantOptions(data).find(item => bindingPlantKey(item) === key);
      if (!plant) return;
      $('bindingDnp3Address').value = plant.dnp3Address;
    }

    function dreamsApiPayload() {
      const token = $('dreamsApiToken').value.trim();
      const payload = {
        base_url: $('dreamsApiUrl').value.trim(),
        plant_meter_no: $('dreamsApiMeter').value.trim()
      };
      state.dreamsApiInput = { ...payload, site_token: token };
      if (token && token !== '-' && token !== '********') {
        payload.site_token = token;
      }
      return payload;
    }

    function fillSiteSelect(data) {
      const liveTargets = commandTargetsFromLive();
      if (liveTargets.length) {
        setCommandSiteOptions(liveTargets);
        return;
      }
      const targets = data.sites
        .filter(site => site.enabled)
        .map(site => ({
          site_id: site.logger_id,
          logger_id: site.logger_id,
          label: site.logger_id && site.logger_id !== '*' ? site.logger_id : '等待即時 logger',
          dnp3_address: site.dnp3_address
        }));
      setCommandSiteOptions(targets);
    }

    function fillLiveSiteSelect(data) {
      const select = $('liveSite');
      const old = select.value;
      select.innerHTML = data.sites.map(site =>
        `<option value="${escapeHtml(site.key || site.site_id)}">${escapeHtml(site.actual_logger_id || site.logger_id)}</option>`
      ).join('');
      if ([...select.options].some(option => option.value === old)) select.value = old;
    }

    function fillCommandSiteSelectFromLive(data) {
      const targets = commandTargetsFromLive(data);
      if (!targets.length) return;
      setCommandSiteOptions(targets);
    }

    function commandTargetsFromLive(data = state.live) {
      return (data?.sites || [])
        .filter(site => site.actual_logger_id)
        .map(site => ({
          site_id: site.actual_logger_id,
          logger_id: site.actual_logger_id,
          label: site.actual_logger_id,
          key: site.actual_logger_id,
          dnp3_address: dnp3ForLogger(site.actual_logger_id)
        }));
    }

    function setCommandSiteOptions(targets) {
      const select = $('cmdSite');
      const selected = select.selectedOptions[0];
      const oldKey = selected ? selected.dataset.key || selected.dataset.logger || selected.value : '';
      select.innerHTML = targets.map(target => {
        const key = target.key || target.logger_id || target.site_id;
        const dnp3 = target.dnp3_address ?? dnp3ForLogger(target.logger_id);
        const label = dnp3 ? `${target.label} / DNP3 ${dnp3}` : target.label;
        return `<option value="${escapeHtml(target.site_id)}" data-logger="${escapeHtml(target.logger_id)}" data-key="${escapeHtml(key)}" data-dnp3="${escapeHtml(dnp3 || '')}">${escapeHtml(label)}</option>`;
      }).join('');
      const same = [...select.options].find(option => option.dataset.key === oldKey);
      if (same) same.selected = true;
      updateCommandPreview();
    }

    function dnp3ForLogger(loggerId) {
      const binding = (loggerBindings(state.status).bindings || []).find(item => item.logger_id === loggerId);
      if (binding) return binding.dnp3_address;
      const site = (state.status?.sites || []).find(item => item.logger_id === loggerId);
      return site?.dnp3_address || '';
    }

    function renderLive(data) {
      fillLiveSiteSelect(data);
      fillCommandSiteSelectFromLive(data);
      if (state.status) {
        renderSites(state.status);
        fillBindingControls(state.status);
      }
      const siteId = $('liveSite').value || (data.sites[0] && (data.sites[0].key || data.sites[0].site_id));
      const site = data.sites.find(item => (item.key || item.site_id) === siteId);
      const mqtt = data.mqtt_connected ? '<span class="pill good">MQTT 在線</span>' : '<span class="pill bad">MQTT 離線</span>';
      if (!site) {
        $('liveMeta').innerHTML = `${mqtt} 沒有案場`;
        $('liveRows').innerHTML = '';
        return;
      }
      const seen = site.seen ? '<span class="pill good">已收到資料</span>' : '<span class="pill bad">尚無資料</span>';
      const online = site.online ? '<span class="pill good">在線</span>' : '<span class="pill bad">離線/未知</span>';
      $('liveMeta').innerHTML = `${mqtt} ${seen} ${online} <span>最後=${escapeHtml(formatTs(site.last_message_ts))}</span>`;
      $('liveRows').innerHTML = site.points.map(point => `
        <tr>
          <td class="mono">${escapeHtml(point.key)}</td>
          <td>${escapeHtml(point.name)}</td>
          <td class="mono">${escapeHtml(formatValue(point.value))}</td>
          <td>${escapeHtml(point.unit)}</td>
          <td class="mono">${escapeHtml(point.dnp_value)}</td>
          <td>${escapeHtml(formatTs(point.updated_at))}</td>
        </tr>
      `).join('');
    }

    function renderPoints(data) {
      const ao = $('cmdAo');
      const old = ao.value;
      ao.innerHTML = data.ao.map(point =>
        `<option value="${point.index}">AO_${point.index} [${escapeHtml(point.command_type)}] - ${escapeHtml(point.name)}</option>`
      ).join('');
      if ([...ao.options].some(option => option.value === old)) ao.value = old;

      $('aoRows').innerHTML = data.ao.map(point => `
        <tr>
          <td class="mono">AO_${point.index}</td>
          <td>${escapeHtml(point.name)} ${point.reserved ? '<span class="pill bad">保留</span>' : ''}</td>
          <td class="mono">${escapeHtml(point.target)}</td>
          <td>${escapeHtml(point.unit)}</td>
          <td>${escapeHtml(point.value_scale)}</td>
          <td>${point.feedback_ai == null ? '-' : `AI_${point.feedback_ai}`}</td>
        </tr>
      `).join('');

      $('aiRows').innerHTML = data.ai.map(point => `
        <tr>
          <td class="mono">${escapeHtml(point.key)}</td>
          <td>${escapeHtml(point.name)} ${point.enabled ? '' : '<span class="pill bad">停用</span>'}</td>
          <td>${escapeHtml(point.unit)}</td>
          <td>${escapeHtml(point.scale)}</td>
          <td>G30V${escapeHtml(point.static_variation)}</td>
          <td>${point.event_variation == null ? '-' : `G32V${point.event_variation}`}</td>
          <td>${point.class2_enabled ? '是' : '否'}</td>
        </tr>
      `).join('');
    }

    function updateCommandPreview() {
      if (!state.points) return;
      const aoIndex = Number($('cmdAo').value || 0);
      const raw = Number($('cmdRaw').value || 0);
      const point = state.points.ao.find(item => item.index === aoIndex);
      if (!point) return;
      const value = raw * point.value_scale;
      const option = $('cmdSite').selectedOptions[0];
      const logger = option ? option.dataset.logger || option.value : '';
      const dnp3 = option ? option.dataset.dnp3 || dnp3ForLogger(logger) : '';
      const unit = point.unit === '0.01%' ? '%' : point.unit;
      const feedback = point.feedback_ai == null ? '無回饋 AI' : `AI_${point.feedback_ai}`;
      const commandKind = point.command_type === 'config_deadband' ? 'Deadband 設定' : '變流器控制';
      $('cmdPreview').value = `類型=${point.command_type}, 目標=${point.target}, 值=${value} ${unit}`;
      $('cmdHelper').innerHTML = `
        <span class="pill">${escapeHtml(commandKind)}</span>
        <span>Logger <span class="mono">${escapeHtml(logger || '-')}</span></span>
        <span>DNP3 <span class="mono">${escapeHtml(dnp3 || '-')}</span></span>
        <span>回饋 <span class="mono">${escapeHtml(feedback)}</span></span>
      `;
    }

    function formatValue(value) {
      if (value == null || Number.isNaN(Number(value))) return '-';
      const number = Number(value);
      if (Math.abs(number) >= 1000) return number.toFixed(0);
      if (Number.isInteger(number)) return String(number);
      return number.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
    }

    function formatTs(value) {
      if (!value) return '-';
      return new Date(Number(value) * 1000).toLocaleString();
    }

    async function sendCommand() {
      const button = $('sendCommand');
      const result = $('commandResult');
      if (button) button.disabled = true;
      result.className = 'result';
      result.textContent = '送出中...';
      try {
        const inverter = $('cmdInverter').value.trim();
        const option = $('cmdSite').selectedOptions[0];
        const payload = {
          site_id: $('cmdSite').value,
          logger_id: option ? option.dataset.logger || '' : '',
          ao_index: Number($('cmdAo').value),
          raw_value: Number($('cmdRaw').value),
          inverter_index: inverter ? Number(inverter) : ''
        };
        const data = await api('/api/commands', { method: 'POST', body: JSON.stringify(payload) });
        result.className = 'result ok';
        result.textContent = `已發布 ${data.topic} cmd_id=${data.payload.cmd_id}`;
        refreshCommands().catch(() => {});
      } catch (error) {
        result.className = 'result error';
        result.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    }

    async function lookupDnp3Ids() {
      const button = $('lookupDnp3Ids');
      const result = $('dnp3LookupResult');
      if (button) button.disabled = true;
      result.className = 'result';
      result.textContent = '查詢中...';
      try {
        const data = await api('/api/dnp3-id/lookup', { method: 'POST', body: JSON.stringify(dreamsApiPayload()) });
        state.dnp3Lookup = data;
        const plants = data.plants || [];
        renderLookupPlants(plants);
        if (state.status) fillBindingControls(state.status);
        selectFetchedPlant(plants[0], state.status);
        result.className = 'result ok';
        const count = uniqueDnp3Records(plants).length;
        result.textContent = count
          ? `已取得 ${count} 筆 DNP3 ID。請在下方選擇一筆並綁定到 logger_id。`
          : '已取得 0 筆 DNP3 ID。';
      } catch (error) {
        result.className = 'result error';
        result.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    }

    async function saveBinding() {
      const button = $('saveBinding');
      const result = $('bindingResult');
      button.disabled = true;
      result.className = 'result';
      result.textContent = '儲存中...';
      try {
        const selected = splitBindingSiteKey($('bindingSite').value);
        const loggerId = $('bindingLoggerId').value.trim() || selected.logger_id;
        const dnp3AddressText = $('bindingDnp3Address').value.trim();
        if (!loggerId) {
          throw new Error('需要 logger_id。');
        }
        if (!dnp3AddressText) {
          throw new Error('需要 DNP3 ID。');
        }
        const payload = {
          site_id: '*',
          logger_id: loggerId,
          dnp3_address: Number(dnp3AddressText)
        };
        const data = await api('/api/bindings', { method: 'POST', body: JSON.stringify(payload) });
        state.status = data.status;
        renderStatus(data.status);
        result.className = 'result ok';
        const activation = data.auto_reload
          ? ' Outstation 會在數秒內自動重載 DNP3 gateway。'
          : ' 請啟動 Outstation service 以套用此 DNP3 ID。';
        result.textContent = `已儲存綁定 ${payload.logger_id} -> DNP3 ${payload.dnp3_address}.${activation}`;
      } catch (error) {
        result.className = 'result error';
        result.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    }

    async function clearBinding() {
      const button = $('clearBinding');
      const result = $('bindingResult');
      const selected = splitBindingSiteKey($('bindingSite').value);
      const loggerId = $('bindingLoggerId').value.trim() || selected.logger_id;
      await clearLoggerBinding(loggerId, result, button);
    }

    async function clearLoggerBinding(loggerId, result, button = null) {
      if (!loggerId) {
        result.className = 'result error';
        result.textContent = '需要 logger_id。';
        return;
      }
      if (button) button.disabled = true;
      result.className = 'result';
      result.textContent = '清除中...';
      try {
        const data = await api('/api/bindings/clear', {
          method: 'POST',
          body: JSON.stringify({ site_id: '*', logger_id: loggerId })
        });
        state.status = data.status;
        renderStatus(data.status);
        result.className = 'result ok';
        const activation = data.auto_reload
          ? ' Outstation 會在數秒內自動重載 DNP3 gateway。'
          : ' 請啟動 Outstation service 以套用此變更。';
        result.textContent = `已清除綁定 ${loggerId}.${activation}`;
      } catch (error) {
        result.className = 'result error';
        result.textContent = error.message;
      } finally {
        if (button) button.disabled = false;
      }
    }

    function renderLookupPlants(plants) {
      const records = uniqueDnp3Records(plants);
      if (!records.length) {
        $('dnp3LookupRows').innerHTML = '<tr><td class="subtle">沒有回傳 DNP3 ID</td></tr>';
        return;
      }
      $('dnp3LookupRows').innerHTML = records.map(plant => `
        <tr>
          <td class="mono">${escapeHtml(plant.dnp3Address)}</td>
        </tr>
      `).join('');
    }

    function wireTabs() {
      document.querySelectorAll('.tab').forEach(button => {
        button.addEventListener('click', () => {
          document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
          button.classList.add('active');
          const tab = button.dataset.tab;
          $('aoTable').style.display = tab === 'ao' ? '' : 'none';
          $('aiTable').style.display = tab === 'ai' ? '' : 'none';
        });
      });
    }

    async function reloadConfig() {
      await api('/api/reload', { method: 'POST', body: '{}' });
      await refreshAll();
    }

    async function refreshAll() {
      await Promise.all([refreshStatus(), refreshPoints(), refreshLive(), refreshCommands(), refreshLogs({ force: true })]);
    }

    $('refresh').addEventListener('click', refreshAll);
    $('refreshLogs').addEventListener('click', () => refreshLogs({ force: true }));
    $('clearLogView').addEventListener('click', clearLogView);
    $('refreshCommands').addEventListener('click', refreshCommands);
    $('clearCommandFilters').addEventListener('click', () => {
      $('commandStatusFilter').value = '';
      $('commandLoggerFilter').value = '';
      $('commandIdFilter').value = '';
      renderCommandLog(state.commands?.commands || []);
    });
    $('reloadConfig').addEventListener('click', reloadConfig);
    $('sendCommand').addEventListener('click', sendCommand);
    $('lookupDnp3Ids').addEventListener('click', lookupDnp3Ids);
    $('saveBinding').addEventListener('click', saveBinding);
    $('clearBinding').addEventListener('click', clearBinding);
    $('bindingSite').addEventListener('change', () => applySelectedSiteBinding(state.status));
    $('bindingPlant').addEventListener('change', () => applySelectedPlant(state.status));
    $('cmdAo').addEventListener('change', updateCommandPreview);
    $('cmdRaw').addEventListener('input', updateCommandPreview);
    $('cmdSite').addEventListener('change', updateCommandPreview);
    $('commandStatusFilter').addEventListener('change', () => renderCommandLog(state.commands?.commands || []));
    $('commandLoggerFilter').addEventListener('input', () => renderCommandLog(state.commands?.commands || []));
    $('commandIdFilter').addEventListener('input', () => renderCommandLog(state.commands?.commands || []));
    $('liveSite').addEventListener('change', () => {
      if (state.live) renderLive(state.live);
    });
    wireTabs();
    refreshAll().catch(error => {
      $('logBox').textContent = error.message;
    });
    setInterval(() => refreshStatus().catch(() => {}), 3000);
    setInterval(() => refreshLive().catch(() => {}), 2000);
    setInterval(() => refreshCommands().catch(() => {}), 3000);
    setInterval(() => refreshLogs().catch(() => {}), 6000);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
