import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from dreams_outstation.config import load_config
from dreams_outstation.models import MqttTopic
from dreams_outstation.service import DreamsOutstationService
from dreams_outstation.site_bindings import SiteBindingStore


class ServiceTests(unittest.TestCase):
    def test_ao_command_publishes_mqtt_command(self):
        with TemporaryDirectory() as tmp:
            config = _test_config(tmp)
            service = DreamsOutstationService(config)
            service.mqtt.publish_command = Mock()
            service.handle_mqtt_message(MqttTopic("logger_test00", "status"), {"status": "online"})

            ok = service.handle_ao_command("*", 12, 250)

        self.assertTrue(ok)
        service.mqtt.publish_command.assert_called_once()
        logger_id, payload = service.mqtt.publish_command.call_args.args
        self.assertEqual(logger_id, "logger_test00")
        self.assertEqual(payload["type"], "config_deadband")
        self.assertEqual(payload["target"], "Deadband_AI_7")
        self.assertEqual(payload["value"], 2.5)

    def test_ao_command_and_ack_are_recorded_in_command_log(self):
        with TemporaryDirectory() as tmp:
            config = _test_config(tmp)
            service = DreamsOutstationService(config)
            service.mqtt.publish_command = Mock()
            service.handle_mqtt_message(MqttTopic("logger_test00", "status"), {"status": "online"})

            ok = service.handle_ao_command("*", 12, 250)
            cmd_id = service.mqtt.publish_command.call_args.args[1]["cmd_id"]
            service.handle_command_ack(
                "*",
                {"cmd_id": cmd_id, "status": "SUCCESS", "inverter_index": 2},
                logger_id="logger_test00",
            )
            row = service.command_log.get(cmd_id)

        self.assertTrue(ok)
        self.assertEqual(row["status"], "SUCCESS")
        self.assertEqual(row["logger_id"], "logger_test00")
        self.assertEqual(row["command_type"], "config_deadband")
        self.assertEqual(row["dnp_values"], {"18": 2, "19": 0, "27": 250})

    def test_wildcard_site_accepts_unconfigured_mqtt_topic(self):
        with TemporaryDirectory() as tmp:
            config = _test_config(tmp)
            service = DreamsOutstationService(config)

            service.handle_mqtt_message(
                MqttTopic("logger_test00", "event"),
                {"ts": 123, "data": {"AI_7": 1250}},
            )

            self.assertEqual(service.states["*"].snapshot_engineering()[7], 1250)
            self.assertEqual(service.last_mqtt_targets["*"], "logger_test00")

    def test_status_offline_disables_dnp3_site_until_snapshot_returns(self):
        with TemporaryDirectory() as tmp:
            config = _test_config(tmp)
            service = DreamsOutstationService(config)
            service.dnp3.set_site_online = Mock()

            service.handle_mqtt_message(
                MqttTopic("logger_test00", "status"),
                {"ts": 123, "status": "offline"},
            )
            service.handle_mqtt_message(
                MqttTopic("logger_test00", "snapshot"),
                {"ts": 124, "reason": "startup", "data": {"AI_7": 1250}},
            )

        self.assertEqual(
            [call.args for call in service.dnp3.set_site_online.call_args_list],
            [("*", False), ("*", True)],
        )

    def test_status_updates_dnp3_availability_without_sending_measurement(self):
        with TemporaryDirectory() as tmp:
            config = _test_config(tmp)
            config = replace(config, dnp3=replace(config.dnp3, include_spare_point_31=True))
            service = DreamsOutstationService(config)
            service.dnp3.available = True
            service.dnp3.send_measurements = Mock()
            service.dnp3.set_site_online = Mock()

            service.handle_mqtt_message(
                MqttTopic("logger_test00", "status"),
                {"ts": 123, "status": "offline"},
            )

        service.dnp3.send_measurements.assert_not_called()
        service.dnp3.set_site_online.assert_called_once_with("*", False)

    def test_status_online_enables_dnp3_site(self):
        with TemporaryDirectory() as tmp:
            config = _test_config(tmp)
            service = DreamsOutstationService(config)
            service.dnp3.set_site_online = Mock()

            service.handle_mqtt_message(
                MqttTopic("logger_test00", "status"),
                {"ts": 123, "status": "online"},
            )

        service.dnp3.set_site_online.assert_called_once_with("*", True)

    def test_unknown_status_payload_does_not_change_dnp3_site_availability(self):
        with TemporaryDirectory() as tmp:
            config = _test_config(tmp)
            service = DreamsOutstationService(config)
            service.dnp3.set_site_online = Mock()

            service.handle_mqtt_message(
                MqttTopic("logger_test00", "status"),
                {"ts": 123, "message": "heartbeat"},
            )

        service.dnp3.set_site_online.assert_not_called()

    def test_periodic_snapshot_skips_offline_logger(self):
        with TemporaryDirectory() as tmp:
            config = _test_config(tmp)
            service = DreamsOutstationService(config)
            service.dnp3.send_measurements = Mock()
            service.handle_mqtt_message(
                MqttTopic("logger_test00", "status"),
                {"ts": 123, "status": "offline"},
            )

            service.send_periodic_snapshot("*")

        service.dnp3.send_measurements.assert_not_called()

    def test_periodic_snapshot_waits_for_full_snapshot(self):
        with TemporaryDirectory() as tmp:
            config = _test_config(tmp)
            service = DreamsOutstationService(config)
            service.dnp3.send_measurements = Mock()
            service.handle_mqtt_message(
                MqttTopic("logger_test00", "status"),
                {"ts": 123, "status": "online"},
            )

            service.send_periodic_snapshot("*")

        service.dnp3.send_measurements.assert_not_called()

    def test_binding_change_auto_reloads_effective_dnp3_config(self):
        with TemporaryDirectory() as tmp:
            config = _test_config(tmp)
            service = DreamsOutstationService(config)
            service.handle_mqtt_message(
                MqttTopic("logger_test02", "snapshot"),
                {"ts": 123, "data": {"AI_7": 88}},
            )
            store = SiteBindingStore(config.runtime.sqlite_path)
            store.upsert_binding(
                site_id="*",
                logger_id="logger_test02",
                plant_no="",
                plant_name="",
                dnp3_address=520,
                source="database",
                updated_by="test",
            )

            reloaded = service.reload_bindings_if_changed(force=True)

        self.assertTrue(reloaded)
        self.assertIn("logger_test02", service.sites_by_id)
        self.assertEqual(service.sites_by_id["logger_test02"].dnp3_address, 520)
        self.assertEqual(service.states["logger_test02"].snapshot_engineering()[7], 88)


def _test_config(tmp: str):
    config = load_config("config/config.yaml")
    return replace(
        config,
        runtime=replace(
            config.runtime,
            dnp3_backend="null",
            sqlite_path=str(Path(tmp) / "test.db"),
        ),
    )


if __name__ == "__main__":
    unittest.main()
