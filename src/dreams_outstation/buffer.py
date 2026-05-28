from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import BufferedEvent


class EventBufferStore:
    def __init__(self, db_path: str | Path, per_site_limit: int = 1024):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.per_site_limit = per_site_limit
        self.init_db()

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS event_buffer (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_id TEXT NOT NULL,
                    event_class INTEGER NOT NULL,
                    msg_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    priority INTEGER DEFAULT 0
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_event_buffer_site_id ON event_buffer(site_id, id)")

    def push(self, event: BufferedEvent) -> int:
        payload_json = json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM event_buffer WHERE site_id = ?",
                (event.site_id,),
            ).fetchone()[0]
            if count >= self.per_site_limit:
                conn.execute(
                    """
                    DELETE FROM event_buffer
                    WHERE id = (
                        SELECT id FROM event_buffer
                        WHERE site_id = ?
                        ORDER BY id ASC
                        LIMIT 1
                    )
                    """,
                    (event.site_id,),
                )
            cursor = conn.execute(
                """
                INSERT INTO event_buffer (site_id, event_class, msg_type, payload, created_at, priority)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event.site_id, event.event_class, event.msg_type, payload_json, time.time(), event.priority),
            )
            return int(cursor.lastrowid)

    def peek(self, site_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, site_id, event_class, msg_type, payload, created_at, priority
                FROM event_buffer
                WHERE site_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (site_id, limit),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def peek_all(self, limit_per_site: int = 100) -> list[dict[str, Any]]:
        site_ids = self.site_ids()
        rows: list[dict[str, Any]] = []
        for site_id in site_ids:
            rows.extend(self.peek(site_id, limit_per_site))
        return sorted(rows, key=lambda row: row["id"])

    def ack(self, event_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM event_buffer WHERE id = ?", (event_id,))

    def count(self, site_id: str | None = None) -> int:
        with self._connect() as conn:
            if site_id is None:
                return int(conn.execute("SELECT COUNT(*) FROM event_buffer").fetchone()[0])
            return int(
                conn.execute("SELECT COUNT(*) FROM event_buffer WHERE site_id = ?", (site_id,)).fetchone()[0]
            )

    def site_ids(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT site_id FROM event_buffer ORDER BY site_id").fetchall()
        return [str(row[0]) for row in rows]

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
            "id": int(row["id"]),
            "site_id": str(row["site_id"]),
            "event_class": int(row["event_class"]),
            "msg_type": str(row["msg_type"]),
            "payload": json.loads(row["payload"]),
            "created_at": float(row["created_at"]),
            "priority": int(row["priority"]),
        }
