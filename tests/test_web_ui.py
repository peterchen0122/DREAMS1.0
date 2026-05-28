import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dreams_outstation.config import load_config
from dreams_outstation.web_ui import (
    LivePointMonitor,
    WebUiState,
    _dreams_api_from_request,
    _optional_int,
    _parse_cookies,
    _parse_mqtt_topic,
    _read_pid,
    _session_cookie_header,
    _tail_lines,
    build_status_payload,
    build_points_payload,
)


class WebUiTests(unittest.TestCase):
    def test_points_payload_contains_ai_and_ao_tables(self):
        payload = build_points_payload(load_config("config/config.yaml"))

        self.assertGreaterEqual(len(payload["ai"]), 32)
        self.assertGreaterEqual(len(payload["ao"]), 16)
        self.assertEqual(payload["ao"][12]["target"], "Deadband_AI_7")

    def test_tail_lines_handles_missing_and_existing_file(self):
        self.assertEqual(_tail_lines("/tmp/does-not-exist-dreams.log", 5), [])

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            path.write_text("a\nb\nc\n", encoding="utf-8")
            self.assertEqual(_tail_lines(path, 2), ["b", "c"])

    def test_optional_int(self):
        self.assertIsNone(_optional_int(""))
        self.assertIsNone(_optional_int(None))
        self.assertEqual(_optional_int("12"), 12)

    def test_read_pid_uses_first_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "service.pid"
            path.write_text("123\n456\n", encoding="utf-8")
            self.assertEqual(_read_pid(path), 123)

    def test_status_does_not_probe_dnp3_tcp_port(self):
        state = WebUiState("config/config.yaml", "admin", "secret")
        calls = []

        def fake_tcp_check(host, port, timeout):
            calls.append((host, port, timeout))
            return True

        with patch("dreams_outstation.web_ui._tcp_check", side_effect=fake_tcp_check):
            payload = build_status_payload(state)

        checked_ports = [port for _host, port, _timeout in calls]
        self.assertEqual(payload["dnp3"]["status_source"], "service_pid")
        self.assertNotIn(state.config.dnp3.port, checked_ports)
        self.assertIn(state.config.mqtt.port, checked_ports)

    def test_auth_session_lifecycle(self):
        state = WebUiState("config/config.yaml", "admin", "secret")

        self.assertTrue(state.authenticate("admin", "secret"))
        self.assertFalse(state.authenticate("admin", "bad"))
        self.assertFalse(state.authenticate("admin", "ｄreams"))
        self.assertFalse(state.authenticate("管理員", "secret"))
        token = state.create_session()
        self.assertTrue(state.validate_session(token))
        state.clear_session(token)
        self.assertFalse(state.validate_session(token))

    def test_cookie_helpers(self):
        header = _session_cookie_header("token-1")
        cookies = _parse_cookies(f"{header}; theme=light")

        self.assertEqual(cookies["dreams_ui_session"], "token-1")
        self.assertEqual(cookies["theme"], "light")

    def test_parse_mqtt_topic(self):
        parsed = _parse_mqtt_topic("DREAMS", "DREAMS/logger1/snapshot")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.logger_id, "logger1")
        self.assertEqual(parsed.site_id, "*")
        self.assertEqual(parsed.suffix, "snapshot")

    def test_legacy_site_logger_topic_is_not_supported(self):
        self.assertIsNone(_parse_mqtt_topic("DREAMS", "DREAMS/site_test00/logger1/snapshot"))

    def test_alarm_topic_is_not_supported(self):
        self.assertIsNone(_parse_mqtt_topic("DREAMS", "DREAMS/logger1/alarm"))
        self.assertIsNone(_parse_mqtt_topic("DREAMS", "DREAMS/site_test00/logger1/alarm"))

    def test_dreams_api_request_overrides_meter_and_token(self):
        config = load_config("config/config.yaml").dreams_api

        updated = _dreams_api_from_request(
            config,
            {
                "base_url": "http://example.test/api/",
                "plant_meter_no": "meter-2",
                "site_token": "token-2",
            },
        )

        self.assertEqual(updated.base_url, "http://example.test/api")
        self.assertEqual(updated.plant_meter_no, "meter-2")
        self.assertEqual(updated.site_token, "token-2")

    def test_dreams_api_request_keeps_config_token_when_masked(self):
        config = load_config("config/config.yaml").dreams_api

        updated = _dreams_api_from_request(config, {"plant_meter_no": "meter-2", "site_token": "********"})

        self.assertEqual(updated.plant_meter_no, "meter-2")
        self.assertEqual(updated.site_token, config.site_token)

    def test_live_monitor_applies_snapshot(self):
        config = load_config("config/config.yaml")
        monitor = LivePointMonitor(config)

        monitor.apply_mqtt_message(
            "DREAMS/logger_test00/snapshot",
            {"ts": 123, "data": {"AI_7": 1250, "AI_10": 60}},
        )
        payload = monitor.snapshot()
        site = next(row for row in payload["sites"] if row["logger_id"] == "logger_test00")
        active_power = next(point for point in site["points"] if point["index"] == 7)

        self.assertTrue(site["seen"])
        self.assertEqual(site["actual_logger_id"], "logger_test00")
        self.assertEqual(site["last_snapshot_ts"], 123)
        self.assertEqual(active_power["value"], 1250)
        self.assertEqual(active_power["dnp_value"], 1250)

    def test_live_monitor_tracks_multiple_wildcard_loggers(self):
        config = load_config("config/config.yaml")
        monitor = LivePointMonitor(config)

        monitor.apply_mqtt_message(
            "DREAMS/logger_test01/status",
            {"ts": 123, "status": "online"},
        )
        monitor.apply_mqtt_message(
            "DREAMS/logger_test02/status",
            {"ts": 124, "status": "online"},
        )
        loggers = {row["logger_id"] for row in monitor.snapshot()["sites"]}

        self.assertIn("logger_test01", loggers)
        self.assertIn("logger_test02", loggers)


if __name__ == "__main__":
    unittest.main()
