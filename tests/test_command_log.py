import tempfile
import unittest
from pathlib import Path

from dreams_outstation.command_log import CommandLogStore


class CommandLogStoreTests(unittest.TestCase):
    def test_record_published_and_ack_with_dnp_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CommandLogStore(Path(tmp) / "commands.db")
            payload = {
                "cmd_id": "cmd-1",
                "type": "control",
                "target": "active_power_percent",
                "value": 50,
                "unit": "%",
                "raw_ao_index": 1,
                "raw_value": 50,
            }

            store.record_published(
                logger_key="logger-a",
                logger_id="logger-a",
                dnp3_address=520,
                source="dnp3_master",
                mqtt_topic="DREAMS/logger-a/cmd",
                payload=payload,
            )
            store.record_ack(
                logger_key="logger-a",
                logger_id="logger-a",
                payload={"cmd_id": "cmd-1", "status": "SUCCESS", "inverter_index": 1},
                dnp_values={18: 1, 19: 0, 15: 50},
            )
            row = store.get("cmd-1")

        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "SUCCESS")
        self.assertEqual(row["logger_id"], "logger-a")
        self.assertEqual(row["dnp_values"], {"18": 1, "19": 0, "15": 50})

    def test_unknown_ack_is_kept_for_debugging(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CommandLogStore(Path(tmp) / "commands.db")

            store.record_ack(
                logger_key="logger-a",
                logger_id="logger-a",
                payload={"cmd_id": "missing-cmd", "status": "FAILED", "message": "unknown"},
                error_message="No pending DNP3 command for this cmd_id",
            )
            row = store.get("missing-cmd")

        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "UNKNOWN_ACK")
        self.assertEqual(row["ack_status"], "FAILED")
        self.assertIn("No pending", row["error_message"])


if __name__ == "__main__":
    unittest.main()
