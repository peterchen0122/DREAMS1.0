from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .models import AppConfig, Dnp3Config, DreamsApiConfig, MqttConfig, RuntimeConfig, SiteConfig


def _get(mapping: dict[str, Any], key: str, default: Any = None) -> Any:
    return mapping[key] if key in mapping else default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    mqtt_raw = raw.get("mqtt", {})
    dnp3_raw = raw.get("dnp3", {})
    runtime_raw = raw.get("runtime", {})
    dreams_api_raw = raw.get("dreams_api", {})
    sites_raw = raw.get("sites", [])

    mqtt = MqttConfig(
        host=os.getenv("DREAMS_MQTT_HOST", _get(mqtt_raw, "host", "127.0.0.1")),
        port=int(os.getenv("DREAMS_MQTT_PORT", _get(mqtt_raw, "port", 1883))),
        username=os.getenv("DREAMS_MQTT_USERNAME", _get(mqtt_raw, "username")),
        password=os.getenv("DREAMS_MQTT_PASSWORD", _get(mqtt_raw, "password")),
        client_id=os.getenv("DREAMS_MQTT_CLIENT_ID", _get(mqtt_raw, "client_id", "dreams-outstation")),
        root_topic=_get(mqtt_raw, "root_topic", "DREAMS").strip("/"),
        keepalive_seconds=int(_get(mqtt_raw, "keepalive_seconds", 60)),
        qos=int(_get(mqtt_raw, "qos", 1)),
    )

    dnp3 = Dnp3Config(
        bind=_get(dnp3_raw, "bind", "0.0.0.0"),
        port=int(_get(dnp3_raw, "port", 20000)),
        master_address=int(_get(dnp3_raw, "master_address", 1)),
        application_fragment_size=int(_get(dnp3_raw, "application_fragment_size", 2048)),
        link_frame_size=int(_get(dnp3_raw, "link_frame_size", 292)),
        application_confirm_timeout_seconds=int(_get(dnp3_raw, "application_confirm_timeout_seconds", 5)),
        enforce_data_send_seconds=float(_get(dnp3_raw, "enforce_data_send_seconds", 0.5)),
        site_buffer_limit=int(_get(dnp3_raw, "site_buffer_limit", 1024)),
        include_spare_point_31=bool(_get(dnp3_raw, "include_spare_point_31", False)),
        ai_event_class=str(_get(dnp3_raw, "ai_event_class", "class1")).lower(),
    )

    runtime = RuntimeConfig(
        sqlite_path=_get(runtime_raw, "sqlite_path", "data/dnp3_buffer.db"),
        log_path=_get(runtime_raw, "log_path", "logs/dreams-outstation.log"),
        periodic_seconds=int(_get(runtime_raw, "periodic_seconds", 900)),
        timezone=_get(runtime_raw, "timezone", "Asia/Taipei"),
        dnp3_backend=str(_get(runtime_raw, "dnp3_backend", "auto")).lower(),
    )

    dreams_api = DreamsApiConfig(
        enabled=_bool(os.getenv("DREAMS_API_ENABLED", _get(dreams_api_raw, "enabled", False))),
        base_url=str(os.getenv("DREAMS_API_BASE_URL", _get(dreams_api_raw, "base_url", "http://127.0.0.1:8090"))).rstrip("/"),
        plant_meter_no=str(os.getenv("DREAMS_API_PLANT_METER_NO", _get(dreams_api_raw, "plant_meter_no", ""))),
        site_token=os.getenv("DREAMS_API_SITE_TOKEN", _get(dreams_api_raw, "site_token", "")) or None,
        timeout_seconds=float(_get(dreams_api_raw, "timeout_seconds", 10)),
        verify_tls=_bool(_get(dreams_api_raw, "verify_tls", True)),
        apply_to_sites=_bool(_get(dreams_api_raw, "apply_to_sites", True)),
    )

    sites = tuple(
        SiteConfig(
            site_id=str(site.get("site_id", "*")),
            logger_id=str(site.get("logger_id", "*")),
            dnp3_address=int(site["dnp3_address"]),
            enabled=bool(site.get("enabled", True)),
            plant_no=str(site["plant_no"]) if site.get("plant_no") is not None else None,
            plant_name=str(site["plant_name"]) if site.get("plant_name") is not None else None,
            dnp3_address_source=str(site.get("dnp3_address_source", "config")),
        )
        for site in sites_raw
    )

    if not sites:
        raise ValueError("At least one site must be configured.")

    return AppConfig(mqtt=mqtt, dnp3=dnp3, runtime=runtime, dreams_api=dreams_api, sites=sites)
