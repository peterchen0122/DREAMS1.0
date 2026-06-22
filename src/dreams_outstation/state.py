from __future__ import annotations

import threading
import time
from typing import Any

from .points import AI_POINTS, enabled_ai_points, normalize_ai_key


class SiteState:
    def __init__(self, logger_key: str, include_spare_point_31: bool = False):
        self.logger_key = logger_key
        self.include_spare_point_31 = include_spare_point_31
        self._lock = threading.RLock()
        self._values: dict[int, float] = {
            index: point.default
            for index, point in enabled_ai_points(include_spare_point_31).items()
        }
        self._values[32] = int(time.time())
        self.last_snapshot_ts: int | None = None
        self.last_event_ts: int | None = None
        self.online = False

    def apply_snapshot(self, payload: dict[str, Any]) -> dict[int, float]:
        ts = int(payload.get("ts") or time.time())
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise ValueError("snapshot payload.data must be an object")

        with self._lock:
            changed = self._apply_data(data)
            self._values[32] = ts
            self.last_snapshot_ts = ts
            self.online = True
            return changed

    def apply_event(self, payload: dict[str, Any]) -> dict[int, float]:
        ts = int(payload.get("ts") or time.time())
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise ValueError("event payload.data must be an object")

        with self._lock:
            changed = self._apply_data(data)
            self._values[32] = ts
            self.last_event_ts = ts
            self.online = True
            return changed

    def apply_status(self, payload: dict[str, Any]) -> bool | None:
        raw_status = payload.get("status")
        if raw_status is None:
            raw_status = payload.get("online")
        if raw_status is None:
            raw_status = payload.get("state")
        status = str(raw_status).lower()
        ts = int(payload.get("ts") or time.time())
        with self._lock:
            if status in {"online", "up", "connected", "1", "true"}:
                self.online = True
            elif status in {"offline", "down", "disconnected", "0", "false"}:
                self.online = False
            else:
                return None
            self._values[32] = ts
            return self.online

    def update_ai(self, index: int, value: float) -> None:
        if index not in AI_POINTS:
            raise KeyError(f"Unknown AI index {index}")
        with self._lock:
            self._values[index] = float(value)
            self._values[32] = int(time.time())

    def reset_control_success(self) -> None:
        with self._lock:
            self._values[18] = 0
            self._values[19] = 0
            self._values[32] = int(time.time())

    def mark_control_success(self, inverter_index: int = 1) -> None:
        if inverter_index < 1 or inverter_index > 50:
            raise ValueError("inverter_index must be 1..50")
        with self._lock:
            if inverter_index <= 25:
                self._values[18] = int(self._values.get(18, 0)) | (1 << (inverter_index - 1))
            else:
                self._values[19] = int(self._values.get(19, 0)) | (1 << (inverter_index - 26))
            self._values[32] = int(time.time())

    def snapshot_engineering(self) -> dict[int, float]:
        with self._lock:
            return dict(self._values)

    def snapshot_dnp(self) -> dict[int, int]:
        with self._lock:
            values: dict[int, int] = {}
            for index, point in enabled_ai_points(self.include_spare_point_31).items():
                values[index] = point.to_dnp_value(self._values.get(index, point.default))
            return values

    def dnp_values_for_changed(self, changed: dict[int, float]) -> dict[int, int]:
        values: dict[int, int] = {}
        for index, value in changed.items():
            point = AI_POINTS.get(index)
            if point is None or not point.enabled or not point.class2_enabled:
                continue
            values[index] = point.to_dnp_value(value)
        return values

    def _apply_data(self, data: dict[str, Any]) -> dict[int, float]:
        changed: dict[int, float] = {}
        for raw_key, raw_value in data.items():
            index = normalize_ai_key(raw_key)
            point = AI_POINTS.get(index)
            if point is None:
                raise KeyError(f"Unknown AI key {raw_key}")
            if not point.enabled and not (index == 31 and self.include_spare_point_31):
                continue
            value = float(raw_value)
            self._values[index] = value
            changed[index] = value
        return changed
