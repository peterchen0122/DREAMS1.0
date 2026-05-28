import tempfile
import unittest
import sqlite3
from dataclasses import replace
from pathlib import Path

from dreams_outstation.config import load_config
from dreams_outstation.site_bindings import SiteBindingStore, apply_stored_bindings


class SiteBindingStoreTests(unittest.TestCase):
    def test_upsert_binding_applies_to_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config("config/config.yaml")
            config = replace(config, runtime=replace(config.runtime, sqlite_path=str(Path(tmp) / "bindings.db")))
            store = SiteBindingStore(config.runtime.sqlite_path)

            store.upsert_binding(
                site_id="*",
                logger_id="logger-a",
                plant_no="plant-a",
                plant_name="Alpha",
                dnp3_address=12,
                source="database",
                updated_by="test",
            )
            updated, status = apply_stored_bindings(config, store)

        site = updated.enabled_sites()[0]
        self.assertEqual(status["count"], 1)
        self.assertEqual(site.site_id, "*")
        self.assertEqual(site.logger_id, "logger-a")
        self.assertEqual(site.plant_no, "plant-a")
        self.assertEqual(site.plant_name, "Alpha")
        self.assertEqual(site.dnp3_address, 12)
        self.assertEqual(site.dnp3_address_source, "database")

    def test_api_plant_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SiteBindingStore(Path(tmp) / "bindings.db")
            store.save_api_plants(
                [
                    {"plantNo": "plant-1", "plantName": "Plant 1", "dnp3Address": 3},
                    {"plantNo": "plant-2", "plantName": "Plant 2", "dnp3Address": 4},
                ]
            )
            plants = store.list_api_plants()

        self.assertEqual([plant["plantNo"] for plant in plants], ["plant-1", "plant-2"])
        self.assertEqual([plant["dnp3Address"] for plant in plants], [3, 4])

    def test_concrete_binding_materializes_site_from_wildcard_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config("config/config.yaml")
            config = replace(config, runtime=replace(config.runtime, sqlite_path=str(Path(tmp) / "bindings.db")))
            store = SiteBindingStore(config.runtime.sqlite_path)

            store.upsert_binding(
                site_id="site_test00",
                logger_id="logger_test01",
                plant_no="plant_1",
                plant_name="Plant 1",
                dnp3_address=9,
                source="dreams_api",
                updated_by="test",
            )
            updated, status = apply_stored_bindings(config, store)

        sites = updated.enabled_sites()
        self.assertEqual(status["count"], 1)
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0].site_id, "*")
        self.assertEqual(sites[0].logger_id, "logger_test01")
        self.assertEqual(sites[0].dnp3_address, 9)
        self.assertEqual(sites[0].dnp3_address_source, "dreams_api")

    def test_dnp3_address_cannot_bind_to_multiple_loggers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SiteBindingStore(Path(tmp) / "bindings.db")

            store.upsert_binding(
                site_id="*",
                logger_id="logger_test01",
                plant_no="plant_1",
                plant_name="Plant 1",
                dnp3_address=9,
                source="database",
                updated_by="test",
            )
            with self.assertRaises(ValueError):
                store.upsert_binding(
                    site_id="*",
                    logger_id="logger_test02",
                    plant_no="plant_2",
                    plant_name="Plant 2",
                    dnp3_address=9,
                    source="database",
                    updated_by="test",
                )

    def test_bindings_are_stored_by_logger_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bindings.db"
            store = SiteBindingStore(db_path)

            store.upsert_binding(
                site_id="legacy-site",
                logger_id="logger_test01",
                plant_no="",
                plant_name="",
                dnp3_address=9,
                source="database",
                updated_by="test",
            )
            with sqlite3.connect(db_path) as conn:
                logger_rows = conn.execute("SELECT logger_id, dnp3_address FROM logger_bindings").fetchall()
                site_rows = conn.execute("SELECT logger_id, dnp3_address FROM site_bindings").fetchall()

        self.assertEqual(logger_rows, [("logger_test01", 9)])
        self.assertEqual(site_rows, [])


if __name__ == "__main__":
    unittest.main()
