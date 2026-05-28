import unittest
from dataclasses import replace

from dreams_outstation.config import load_config
from dreams_outstation.dreams_api import (
    apply_plant_mappings_to_config,
    build_plant_dnp3_id_url,
    normalize_plant_dnp3_ids,
)
from dreams_outstation.models import SiteConfig


class DreamsApiTests(unittest.TestCase):
    def test_build_plant_dnp3_id_url(self):
        config = load_config("config/config.yaml").dreams_api

        self.assertEqual(
            build_plant_dnp3_id_url(config),
            "http://127.0.0.1:8090/api/plants/plantMeterNo/test-meter?token=test-token",
        )

    def test_normalize_plant_dnp3_ids_requires_list(self):
        with self.assertRaises(ValueError):
            normalize_plant_dnp3_ids({"plantNo": "p1"})

    def test_apply_mapping_by_plant_no(self):
        config = load_config("config/config.yaml")
        site = SiteConfig(site_id="*", logger_id="logger-a", dnp3_address=1, plant_no="plant-a")
        config = replace(config, sites=(site,))

        updated, status = apply_plant_mappings_to_config(
            config,
            [{"plantNo": "plant-a", "plantName": "Alpha", "dnp3Address": 42}],
        )

        self.assertTrue(status["applied"])
        self.assertEqual(updated.enabled_sites()[0].dnp3_address, 42)
        self.assertEqual(updated.enabled_sites()[0].plant_name, "Alpha")
        self.assertEqual(updated.enabled_sites()[0].dnp3_address_source, "dreams_api")

    def test_does_not_bind_wildcard_logger_by_order(self):
        config = load_config("config/config.yaml")

        updated, status = apply_plant_mappings_to_config(
            config,
            [{"plantNo": "plant-1", "plantName": "Plant 1", "dnp3Address": 9}],
        )

        self.assertFalse(status["applied"])
        self.assertEqual(updated.enabled_sites()[0].site_id, "*")
        self.assertEqual(updated.enabled_sites()[0].logger_id, "*")
        self.assertEqual(updated.enabled_sites()[0].dnp3_address, 1)
        self.assertEqual(len(status["unmatched_plants"]), 1)

    def test_leaves_unmatched_multiple_plants_for_single_wildcard(self):
        config = load_config("config/config.yaml")

        updated, status = apply_plant_mappings_to_config(
            config,
            [
                {"plantNo": "plant-1", "plantName": "Plant 1", "dnp3Address": 9},
                {"plantNo": "plant-2", "plantName": "Plant 2", "dnp3Address": 10},
            ],
        )

        self.assertFalse(status["applied"])
        self.assertEqual(updated.enabled_sites()[0].dnp3_address, 1)
        self.assertEqual(len(status["unmatched_plants"]), 2)


if __name__ == "__main__":
    unittest.main()
