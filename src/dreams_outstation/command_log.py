from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class CommandLogStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS command_log (
                    cmd_id TEXT PRIMARY KEY,
                    logger_key TEXT,
                    logger_id TEXT NOT NULL,
                    dnp3_address INTEGER,
                    source TEXT NOT NULL,
                    ao_index INTEGER,
                    command_type TEXT,
                    target TEXT,
                    raw_value REAL,
                    value REAL,
                    unit TEXT,
                    inverter_index INTEGER,
                    mqtt_topic TEXT,
                    command_payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    ack_status TEXT,
                    ack_payload TEXT,
                    dnp_values TEXT,
                    error_message TEXT,
                    created_at REAL NOT NULL,
                    published_at REAL,
                    acked_at REAL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_command_log_updated_at ON command_log(updated_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_command_log_logger_id ON command_log(logger_id, updated_at DESC)")

    def record_published(
        self,
        *,
        logger_key: str,
        logger_id: str,
        dnp3_address: int | None,
        source: str,
        mqtt_topic: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        now = time.time()
        cmd_id = _cmd_id(payload)
        command_payload = _json(payload)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO command_log (
                    cmd_id, logger_key, logger_id, dnp3_address, source, ao_index,
                    command_type, target, raw_value, value, unit, inverter_index,
                    mqtt_topic, command_payload, status, created_at, published_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PUBLISHED', ?, ?, ?)
                ON CONFLICT(cmd_id) DO UPDATE SET
                    logger_key = excluded.logger_key,
                    logger_id = excluded.logger_id,
                    dnp3_address = excluded.dnp3_address,
                    source = excluded.source,
                    ao_index = excluded.ao_index,
                    command_type = excluded.command_type,
                    target = excluded.target,
                    raw_value = excluded.raw_value,
                    value = excluded.value,
                    unit = excluded.unit,
                    inverter_index = excluded.inverter_index,
                    mqtt_topic = excluded.mqtt_topic,
                    command_payload = excluded.command_payload,
                    status = 'PUBLISHED',
                    published_at = excluded.published_at,
                    updated_at = excluded.updated_at
                """,
                (
                    cmd_id,
                    logger_key,
                    logger_id,
                    dnp3_address,
                    source,
                    _optional_int(payload.get("raw_ao_index")),
                    _optional_str(payload.get("type")),
                    _optional_str(payload.get("target")),
                    _optional_float(payload.get("raw_value")),
                    _optional_float(payload.get("value")),
                    _optional_str(payload.get("unit")),
                    _optional_int(payload.get("inverter_index")),
                    mqtt_topic,
                    command_payload,
                    now,
                    now,
                    now,
                ),
            )
        return self.get(cmd_id) or {}

    def record_publish_failed(
        self,
        *,
        logger_key: str,
        logger_id: str,
        dnp3_address: int | None,
        source: str,
        mqtt_topic: str | None,
        payload: dict[str, Any],
        error_message: str,
    ) -> dict[str, Any]:
        now = time.time()
        cmd_id = _cmd_id(payload)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO command_log (
                    cmd_id, logger_key, logger_id, dnp3_address, source, ao_index,
                    command_type, target, raw_value, value, unit, inverter_index,
                    mqtt_topic, command_payload, status, error_message,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PUBLISH_FAILED', ?, ?, ?)
                ON CONFLICT(cmd_id) DO UPDATE SET
                    status = 'PUBLISH_FAILED',
                    error_message = excluded.error_message,
                    updated_at = excluded.updated_at
                """,
                (
                    cmd_id,
                    logger_key,
                    logger_id,
                    dnp3_address,
                    source,
                    _optional_int(payload.get("raw_ao_index")),
                    _optional_str(payload.get("type")),
                    _optional_str(payload.get("target")),
                    _optional_float(payload.get("raw_value")),
                    _optional_float(payload.get("value")),
                    _optional_str(payload.get("unit")),
                    _optional_int(payload.get("inverter_index")),
                    mqtt_topic,
                    _json(payload),
                    error_message,
                    now,
                    now,
                ),
            )
        return self.get(cmd_id) or {}

    def record_ack(
        self,
        *,
        logger_key: str,
        logger_id: str,
        payload: dict[str, Any],
        dnp_values: dict[int, int] | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        cmd_id = _cmd_id(payload)
        ack_status = _optional_str(payload.get("status"))
        status = "ACK_ERROR" if error_message else (ack_status or "ACK_RECEIVED").upper()
        ack_payload = _json(payload)
        dnp_json = _json({str(index): value for index, value in (dnp_values or {}).items()}) if dnp_values is not None else None
        with self._connect() as conn:
            existing = conn.execute("SELECT dnp_values FROM command_log WHERE cmd_id = ?", (cmd_id,)).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO command_log (
                        cmd_id, logger_key, logger_id, source, command_payload, status,
                        ack_status, ack_payload, dnp_values, error_message,
                        created_at, acked_at, updated_at
                    )
                    VALUES (?, ?, ?, 'external_ack', '{}', 'UNKNOWN_ACK', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (cmd_id, logger_key, logger_id, ack_status, ack_payload, dnp_json, error_message, now, now, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE command_log
                    SET logger_key = COALESCE(NULLIF(?, ''), logger_key),
                        logger_id = COALESCE(NULLIF(?, ''), logger_id),
                        status = ?,
                        ack_status = ?,
                        ack_payload = ?,
                        dnp_values = ?,
                        error_message = ?,
                        acked_at = ?,
                        updated_at = ?
                    WHERE cmd_id = ?
                    """,
                    (
                        logger_key,
                        logger_id,
                        status,
                        ack_status,
                        ack_payload,
                        dnp_json if dnp_json is not None else existing["dnp_values"],
                        error_message,
                        now,
                        now,
                        cmd_id,
                    ),
                )
        return self.get(cmd_id) or {}

    def get(self, cmd_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM command_log WHERE cmd_id = ?", (cmd_id,)).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM command_log
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "cmd_id": str(row["cmd_id"]),
            "logger_key": str(row["logger_key"] or ""),
            "logger_id": str(row["logger_id"] or ""),
            "dnp3_address": row["dnp3_address"] if row["dnp3_address"] is None else int(row["dnp3_address"]),
            "source": str(row["source"]),
            "ao_index": row["ao_index"] if row["ao_index"] is None else int(row["ao_index"]),
            "command_type": str(row["command_type"] or ""),
            "target": str(row["target"] or ""),
            "raw_value": row["raw_value"],
            "value": row["value"],
            "unit": str(row["unit"] or ""),
            "inverter_index": row["inverter_index"] if row["inverter_index"] is None else int(row["inverter_index"]),
            "mqtt_topic": str(row["mqtt_topic"] or ""),
            "command_payload": _parse_json(row["command_payload"], {}),
            "status": str(row["status"]),
            "ack_status": str(row["ack_status"] or ""),
            "ack_payload": _parse_json(row["ack_payload"], {}),
            "dnp_values": _parse_json(row["dnp_values"], {}),
            "error_message": str(row["error_message"] or ""),
            "created_at": float(row["created_at"]),
            "published_at": row["published_at"] if row["published_at"] is None else float(row["published_at"]),
            "acked_at": row["acked_at"] if row["acked_at"] is None else float(row["acked_at"]),
            "updated_at": float(row["updated_at"]),
        }


def _cmd_id(payload: dict[str, Any]) -> str:
    cmd_id = str(payload.get("cmd_id") or "").strip()
    if not cmd_id:
        raise ValueError("cmd_id is required")
    return cmd_id


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse_json(value: Any, default: Any) -> Any:
    if value in {None, ""}:
        return default
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def _optional_str(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
