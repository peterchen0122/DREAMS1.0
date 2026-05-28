from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

from .dreams_api import apply_dnp3_id_lookup
from .models import AppConfig, SiteConfig


class SiteBindingStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS logger_bindings (
                    logger_id TEXT PRIMARY KEY,
                    plant_no TEXT,
                    plant_name TEXT,
                    dnp3_address INTEGER NOT NULL,
                    dnp3_address_source TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    updated_by TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS site_bindings (
                    site_id TEXT NOT NULL,
                    logger_id TEXT NOT NULL,
                    plant_no TEXT,
                    plant_name TEXT,
                    dnp3_address INTEGER NOT NULL,
                    dnp3_address_source TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    updated_by TEXT NOT NULL,
                    PRIMARY KEY (site_id, logger_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dreams_api_plants (
                    plant_no TEXT PRIMARY KEY,
                    plant_name TEXT,
                    dnp3_address INTEGER NOT NULL,
                    fetched_at REAL NOT NULL
                )
                """
            )
            self._migrate_legacy_site_bindings(conn)

    def _migrate_legacy_site_bindings(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT site_id, logger_id, plant_no, plant_name, dnp3_address,
                   dnp3_address_source, updated_at, updated_by
            FROM site_bindings
            """
        ).fetchall()
        for row in rows:
            logger_id = str(row["logger_id"] or "")
            if _is_wildcard(logger_id):
                conn.execute("DELETE FROM site_bindings WHERE site_id = ? AND logger_id = ?", (row["site_id"], row["logger_id"]))
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO logger_bindings (
                    logger_id, plant_no, plant_name, dnp3_address,
                    dnp3_address_source, updated_at, updated_by
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["logger_id"],
                    row["plant_no"],
                    row["plant_name"],
                    row["dnp3_address"],
                    row["dnp3_address_source"],
                    row["updated_at"],
                    row["updated_by"],
                ),
            )
        conn.execute("DELETE FROM site_bindings")

    def list_bindings(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT logger_id, plant_no, plant_name, dnp3_address,
                       dnp3_address_source, updated_at, updated_by
                FROM logger_bindings
                ORDER BY logger_id
                """
            ).fetchall()
        return [self._binding_row(row) for row in rows]

    def get_binding(self, site_id: str, logger_id: str) -> dict[str, Any] | None:
        logger_key = logger_id.strip()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT logger_id, plant_no, plant_name, dnp3_address,
                       dnp3_address_source, updated_at, updated_by
                FROM logger_bindings
                WHERE logger_id = ?
                """,
                (logger_key,),
            ).fetchone()
        return self._binding_row(row) if row is not None else None

    def upsert_binding(
        self,
        site_id: str,
        logger_id: str,
        plant_no: str | None,
        plant_name: str | None,
        dnp3_address: int,
        source: str,
        updated_by: str,
    ) -> dict[str, Any]:
        now = time.time()
        logger_key = logger_id.strip()
        with self._connect() as conn:
            duplicate = conn.execute(
                """
                SELECT logger_id
                FROM logger_bindings
                WHERE dnp3_address = ?
                  AND logger_id != ?
                LIMIT 1
                """,
                (int(dnp3_address), logger_key),
            ).fetchone()
            if duplicate is not None:
                raise ValueError(
                    f"DNP3 address {int(dnp3_address)} is already bound to logger_id={duplicate['logger_id']}"
                )
            conn.execute(
                """
                INSERT INTO logger_bindings (
                    logger_id, plant_no, plant_name, dnp3_address,
                    dnp3_address_source, updated_at, updated_by
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(logger_id) DO UPDATE SET
                    plant_no = excluded.plant_no,
                    plant_name = excluded.plant_name,
                    dnp3_address = excluded.dnp3_address,
                    dnp3_address_source = excluded.dnp3_address_source,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (
                    logger_key,
                    plant_no or None,
                    plant_name or None,
                    int(dnp3_address),
                    source,
                    now,
                    updated_by,
                ),
            )
        binding = self.get_binding("*", logger_key)
        if binding is None:
            raise RuntimeError("Failed to save site binding")
        return binding

    def clear_binding(self, site_id: str, logger_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM logger_bindings WHERE logger_id = ?", (logger_id.strip(),))

    def save_api_plants(self, plants: list[dict[str, Any]]) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM dreams_api_plants")
            for index, plant in enumerate(plants):
                plant_no = str(plant.get("plantNo") or f"plant_{index + 1}")
                conn.execute(
                    """
                    INSERT INTO dreams_api_plants (plant_no, plant_name, dnp3_address, fetched_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(plant_no) DO UPDATE SET
                        plant_name = excluded.plant_name,
                        dnp3_address = excluded.dnp3_address,
                        fetched_at = excluded.fetched_at
                    """,
                    (
                        plant_no,
                        str(plant.get("plantName") or ""),
                        int(plant["dnp3Address"]),
                        now,
                    ),
                )

    def list_api_plants(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT plant_no, plant_name, dnp3_address, fetched_at
                FROM dreams_api_plants
                ORDER BY plant_no
                """
            ).fetchall()
        return [
            {
                "plantNo": str(row["plant_no"]),
                "plantName": str(row["plant_name"] or ""),
                "dnp3Address": int(row["dnp3_address"]),
                "fetched_at": float(row["fetched_at"]),
            }
            for row in rows
        ]

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
    def _binding_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "site_id": "*",
            "binding_key": str(row["logger_id"]),
            "logger_id": str(row["logger_id"]),
            "plant_no": str(row["plant_no"] or ""),
            "plant_name": str(row["plant_name"] or ""),
            "dnp3_address": int(row["dnp3_address"]),
            "dnp3_address_source": str(row["dnp3_address_source"]),
            "updated_at": float(row["updated_at"]),
            "updated_by": str(row["updated_by"]),
        }


def load_effective_config(config: AppConfig) -> tuple[AppConfig, dict[str, Any]]:
    store = SiteBindingStore(config.runtime.sqlite_path)
    config, binding_status = apply_stored_bindings(config, store)
    config, api_status = apply_dnp3_id_lookup(config)
    if api_status.get("plants"):
        store.save_api_plants(api_status["plants"])
    for match in api_status.get("matches") or []:
        store.upsert_binding(
            site_id="*",
            logger_id=str(match["logger_id"]),
            plant_no=str(match.get("plantNo") or ""),
            plant_name=str(match.get("plantName") or ""),
            dnp3_address=int(match["dnp3Address"]),
            source="dreams_api",
            updated_by="dreams_api",
        )
    if api_status.get("applied"):
        config, binding_status = apply_stored_bindings(config, store)
    return config, {"bindings": binding_status, "dreams_api": api_status}


def apply_stored_bindings(config: AppConfig, store: SiteBindingStore | None = None) -> tuple[AppConfig, dict[str, Any]]:
    binding_store = store or SiteBindingStore(config.runtime.sqlite_path)
    binding_rows = binding_store.list_bindings()
    bindings = {binding["logger_id"]: binding for binding in binding_rows if not _is_wildcard(binding["logger_id"])}
    concrete_bindings = [binding for binding in binding_rows if not _is_wildcard(binding["logger_id"])]
    wildcard_template = next((site for site in config.enabled_sites() if _is_wildcard(site.site_id)), None)
    sites: list[SiteConfig] = []
    applied: list[dict[str, Any]] = []
    materialized: list[dict[str, Any]] = []
    materialized_keys: set[tuple[str, str]] = set()
    for site in config.sites:
        binding = bindings.get(site.logger_id)
        if binding is not None:
            updated = _site_from_binding(site, binding)
            sites.append(updated)
            applied.append(binding)
            continue
        if _is_wildcard(site.site_id) and _is_wildcard(site.logger_id) and concrete_bindings:
            continue
        sites.append(site)

    if wildcard_template is not None:
        configured_loggers = {site.logger_id for site in config.sites if not _is_wildcard(site.logger_id)}
        for binding in concrete_bindings:
            if binding["logger_id"] in configured_loggers:
                continue
            site = _site_from_binding(
                replace(wildcard_template, site_id="*", logger_id=binding["logger_id"]),
                binding,
            )
            sites.append(site)
            materialized.append(binding)
            materialized_keys.add(("*", binding["logger_id"]))

    unmaterialized = [
        binding
        for binding in concrete_bindings
        if ("*", binding["logger_id"]) not in materialized_keys
        and binding["logger_id"] not in {site.logger_id for site in config.sites if not _is_wildcard(site.logger_id)}
    ]
    return replace(config, sites=tuple(sites)), {
        "applied": applied,
        "materialized": materialized,
        "unmaterialized": unmaterialized,
        "count": len(applied) + len(materialized),
    }


def _site_from_binding(site: SiteConfig, binding: dict[str, Any]) -> SiteConfig:
    return replace(
        site,
        plant_no=binding["plant_no"] or None,
        plant_name=binding["plant_name"] or None,
        dnp3_address=int(binding["dnp3_address"]),
        dnp3_address_source=binding["dnp3_address_source"],
    )


def _is_wildcard(value: str) -> bool:
    return value.strip() in {"", "*"}


def _binding_site_id(_site_id: str | None = None) -> str:
    return "*"


def save_matches_as_bindings(store: SiteBindingStore, matches: list[dict[str, Any]], updated_by: str) -> list[dict[str, Any]]:
    saved: list[dict[str, Any]] = []
    for match in matches:
        saved.append(
            store.upsert_binding(
                site_id="*",
                logger_id=str(match["logger_id"]),
                plant_no=str(match.get("plantNo") or ""),
                plant_name=str(match.get("plantName") or ""),
                dnp3_address=int(match["dnp3Address"]),
                source="dreams_api",
                updated_by=updated_by,
            )
        )
    return saved
