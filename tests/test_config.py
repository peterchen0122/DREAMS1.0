import unittest
import tempfile
from pathlib import Path

from dreams_outstation.config import load_config


class ConfigTests(unittest.TestCase):
    def test_load_default_config(self):
        config = load_config("config/config.yaml")
        self.assertEqual(config.mqtt.host, "34.80.23.92")
        self.assertEqual(config.mqtt.port, 1883)
        self.assertEqual(config.mqtt.username, "dev")
        self.assertEqual(config.dnp3.port, 20000)
        self.assertFalse(config.dreams_api.enabled)
        self.assertEqual(config.dreams_api.base_url, "http://127.0.0.1:8090")
        self.assertEqual(config.dreams_api.plant_meter_no, "test-meter")
        self.assertEqual(config.dreams_api.site_token, "test-token")
        self.assertEqual([site.dnp3_address for site in config.enabled_sites()], [1])
        self.assertEqual(config.enabled_sites()[0].site_id, "*")
        self.assertEqual(config.enabled_sites()[0].logger_id, "*")

    def test_site_id_and_logger_id_default_to_wildcards(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                """
sites:
  - dnp3_address: 7
""",
                encoding="utf-8",
            )

            config = load_config(path)

        self.assertEqual(config.enabled_sites()[0].site_id, "*")
        self.assertEqual(config.enabled_sites()[0].logger_id, "*")
        self.assertEqual(config.enabled_sites()[0].dnp3_address, 7)
        self.assertEqual(config.enabled_sites()[0].dnp3_address_source, "config")


if __name__ == "__main__":
    unittest.main()
