from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    username: str | None
    password: str | None
    client_id: str
    root_topic: str
    keepalive_seconds: int
    qos: int


@dataclass(frozen=True)
class Dnp3Config:
    bind: str
    port: int
    master_address: int
    application_fragment_size: int
    link_frame_size: int
    application_confirm_timeout_seconds: int
    enforce_data_send_seconds: float
    site_buffer_limit: int
    include_spare_point_31: bool
    ai_event_class: str


@dataclass(frozen=True)
class RuntimeConfig:
    sqlite_path: str
    log_path: str
    periodic_seconds: int
    timezone: str
    dnp3_backend: str


@dataclass(frozen=True)
class DreamsApiConfig:
    enabled: bool
    base_url: str
    plant_meter_no: str
    site_token: str | None
    timeout_seconds: float
    verify_tls: bool
    apply_to_sites: bool


@dataclass(frozen=True)
class SiteConfig:
    site_id: str
    logger_id: str
    dnp3_address: int
    enabled: bool = True
    plant_no: str | None = None
    plant_name: str | None = None
    dnp3_address_source: str = "config"

    @property
    def key(self) -> str:
        logger_id = self.logger_id.strip()
        if logger_id and logger_id != "*":
            return logger_id
        return self.site_id


@dataclass(frozen=True)
class AppConfig:
    mqtt: MqttConfig
    dnp3: Dnp3Config
    runtime: RuntimeConfig
    dreams_api: DreamsApiConfig
    sites: tuple[SiteConfig, ...]

    def enabled_sites(self) -> tuple[SiteConfig, ...]:
        return tuple(site for site in self.sites if site.enabled)


@dataclass(frozen=True)
class AiPoint:
    index: int
    name: str
    unit: str
    scale: float
    static_variation: int
    event_variation: int | None
    deadband_trigger_raw: int | None
    default: float = 0.0
    enabled: bool = True
    class2_enabled: bool = True

    @property
    def key(self) -> str:
        return f"AI_{self.index}"

    def to_dnp_value(self, value: float | int) -> int:
        return int(round(float(value) * self.scale))


@dataclass(frozen=True)
class AoPoint:
    index: int
    name: str
    unit: str
    command_type: str
    target: str
    feedback_ai: int | None
    value_scale: float = 1.0
    reserved: bool = False

    def engineering_value(self, raw_value: float | int) -> float:
        return float(raw_value) * self.value_scale


@dataclass(frozen=True)
class MqttTopic:
    logger_id: str
    suffix: str
    site_id: str = "*"


@dataclass(frozen=True)
class BufferedEvent:
    site_id: str
    event_class: int
    msg_type: str
    payload: dict[str, Any]
    priority: int = 0
