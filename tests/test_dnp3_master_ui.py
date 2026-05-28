import unittest

from tools.dnp3_master_ui import INDEX_HTML, _monitor_addresses, _parse_monitor_events


class Dnp3MasterUiTests(unittest.TestCase):
    def test_monitor_addresses_accepts_multiple_unique_ids(self):
        defaults = {"master_address": 100, "outstation_address": 1}

        addresses = _monitor_addresses(
            {"master_address": 100, "outstation_addresses": [1, "410", 1]},
            defaults,
        )

        self.assertEqual(addresses, [1, 410])

    def test_monitor_addresses_rejects_empty_selection(self):
        defaults = {"master_address": 100, "outstation_address": 1}

        with self.assertRaises(ValueError):
            _monitor_addresses({"master_address": 100, "outstation_addresses": []}, defaults)

    def test_monitor_events_include_dnp3_address_and_logger(self):
        events = _parse_monitor_events(
            [
                {
                    "seq": 7,
                    "ts": "2026-05-28 13:30:00",
                    "text": "[7] : 1250 : 129 : 2026-05-28 13:30:00",
                    "dnp3_address": 410,
                    "logger_id": "logger_test02",
                }
            ]
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["dnp3_address"], 410)
        self.assertEqual(events[0]["logger_id"], "logger_test02")
        self.assertEqual(events[0]["key"], "AI_7")
        self.assertEqual(events[0]["kind"], "periodic")
        self.assertEqual(events[0]["source"], "snapshot")

    def test_monitor_events_infer_cmd_ack_source_from_success_bitmask_batch(self):
        events = _parse_monitor_events(
            [
                {
                    "seq": 1,
                    "ts": "2026-05-28 13:31:00",
                    "text": "[18] : 1 : 1 : 2026-05-28 13:31:00",
                    "dnp3_address": 410,
                    "logger_id": "logger_test02",
                },
                {
                    "seq": 2,
                    "ts": "2026-05-28 13:31:00",
                    "text": "[15] : 50 : 1 : 2026-05-28 13:31:00",
                    "dnp3_address": 410,
                    "logger_id": "logger_test02",
                },
                {
                    "seq": 3,
                    "ts": "2026-05-28 13:32:00",
                    "text": "[7] : 50000 : 1 : 2026-05-28 13:32:00",
                    "dnp3_address": 410,
                    "logger_id": "logger_test02",
                },
            ]
        )

        self.assertEqual([event["source"] for event in events], ["cmd_ack", "cmd_ack", "event"])

    def test_html_uses_operator_flow_sections(self):
        self.assertIn("Registered DNP3 IDs", INDEX_HTML)
        self.assertIn("Multi-ID Monitor", INDEX_HTML)
        self.assertIn("Single-ID Commands", INDEX_HTML)
        self.assertIn("<th style=\"width:92px;\">Source</th>", INDEX_HTML)
        self.assertIn("Command target:", INDEX_HTML)
        self.assertIn("MONITOR_STATUS_REFRESH_MS = 3000", INDEX_HTML)
        self.assertNotIn("setInterval(() => refreshMonitorStatus", INDEX_HTML)
        self.assertNotIn("<h2>DNP3 ID API</h2>", INDEX_HTML)
        self.assertNotIn("<h2>Analog Output</h2>", INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
