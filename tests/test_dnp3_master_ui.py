import unittest
from pathlib import Path
from unittest.mock import patch

from tools.dnp3_master_ui import (
    INDEX_HTML,
    _check_tcp_endpoint,
    _monitor_addresses,
    _parse_ai_range,
    _parse_command_result,
    _parse_monitor_events,
    _parse_ui_command_status,
    _run_multi_poll,
)


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

    def test_monitor_events_skip_restart_and_intermediate_rows(self):
        events = _parse_monitor_events(
            [
                {
                    "seq": 1,
                    "ts": "2026-05-28 13:31:00",
                    "text": "[7] : INTERMEDIATE : 2 : 0",
                    "dnp3_address": 410,
                    "logger_id": "logger_test02",
                },
                {
                    "seq": 2,
                    "ts": "2026-05-28 13:31:01",
                    "text": "[7] : 0 : 2 : 0",
                    "dnp3_address": 410,
                    "logger_id": "logger_test02",
                },
                {
                    "seq": 3,
                    "ts": "2026-05-28 13:31:02",
                    "text": "[7] : 1250 : 129 : 2026-05-28 13:31:02",
                    "dnp3_address": 410,
                    "logger_id": "logger_test02",
                },
            ]
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["raw_value"], 1250.0)
        self.assertEqual(events[0]["flags"], "129")

    def test_dnp_zero_timestamp_is_treated_as_missing_time(self):
        points = _parse_ai_range(
            "\n".join(
                [
                    "[task] scan AI range started",
                    "[7] : 1250 : 129 : 0",
                    "[task] scan AI range completed",
                ]
            ),
            7,
            7,
        )
        events = _parse_monitor_events(
            [
                {
                    "seq": 3,
                    "ts": "2026-06-17 11:03:35",
                    "text": "[7] : 1250 : 129 : 0",
                    "dnp3_address": 410,
                    "logger_id": "logger_test02",
                }
            ]
        )

        self.assertEqual(points[0]["time"], "")
        self.assertEqual(events[0]["time"], "")
        self.assertEqual(events[0]["received_at"], "2026-06-17 11:03:35")

    def test_multi_poll_runs_each_selected_dnp3_id(self):
        defaults = {
            "host": "127.0.0.1",
            "port": 20000,
            "master_address": 100,
            "outstation_address": 520,
        }
        calls = []

        def fake_run(_simulator_path, args, timeout=30):
            calls.append(args)
            address = int(args[args.index("--outstation-address") + 1])
            return {
                "returncode": 0,
                "stdout": (
                    "[task] scan AI range started\n"
                    f"[0] : {address} : 129 : 2026-05-28 13:30:00\n"
                    "[task] scan AI range completed\n"
                ),
                "stderr": "",
                "duration_seconds": 0.01,
                "transmission": [{"outstation_address": address}],
            }

        with patch("tools.dnp3_master_ui._run_simulator_unlocked", side_effect=fake_run):
            result = _run_multi_poll(
                Path("simulator.py"),
                {"host": "127.0.0.1", "port": 20000, "master_address": 100},
                defaults,
                [520, 521],
                {520: "logger_a", 521: "logger_b"},
                ["range", "0", "0", "--wait", "8"],
            )

        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["target_count"], 2)
        self.assertEqual([item["dnp3_address"] for item in result["results"]], [520, 521])
        self.assertEqual([item["outstation_address"] for item in result["transmission"]], [520, 521])
        self.assertIn("--- DNP3 520 logger_a ---", result["stdout"])
        self.assertEqual(
            [int(args[args.index("--outstation-address") + 1]) for args in calls],
            [520, 521],
        )

    def test_command_result_parser_marks_non_success_summary_failed(self):
        result = _parse_command_result("[command] summary=FAILURE_NO_COMMS results=1\n")

        self.assertEqual(result["summary"], "FAILURE_NO_COMMS")
        self.assertEqual(result["results"], 1)
        self.assertFalse(result["success"])

    def test_command_result_parser_marks_success_summary_ok(self):
        result = _parse_command_result("[command] summary=SUCCESS results=1\n")

        self.assertEqual(result["summary"], "SUCCESS")
        self.assertEqual(result["results"], 1)
        self.assertTrue(result["success"])

    def test_ui_command_status_parser_uses_last_matching_status(self):
        result = _parse_ui_command_status(
            "\n".join(
                [
                    '[ui-command] {"request_id":"abc","status":"started","command":"ao"}',
                    '[ui-command] {"request_id":"other","status":"completed","command":"ao"}',
                    '[ui-command] {"request_id":"abc","status":"completed","command":"ao"}',
                ]
            ),
            "abc",
        )

        self.assertEqual(result["status"], "completed")

    def test_simulator_exposes_check_command(self):
        simulator_source = Path("tools/dnp3_master_simulator.py").read_text()

        self.assertIn('subparsers.add_parser("check"', simulator_source)
        self.assertIn('if args.command == "check"', simulator_source)

    def test_endpoint_check_uses_tcp_socket(self):
        with patch("tools.dnp3_master_ui.socket.create_connection") as create_connection:
            result = _check_tcp_endpoint("127.0.0.1", 20000, 8)

        create_connection.assert_called_once_with(("127.0.0.1", 20000), timeout=8)
        self.assertTrue(result["connected"])
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["check_type"], "tcp")

    def test_endpoint_check_reports_tcp_failure(self):
        with patch("tools.dnp3_master_ui.socket.create_connection", side_effect=OSError("connection refused")):
            result = _check_tcp_endpoint("127.0.0.1", 20000, 8)

        self.assertFalse(result["connected"])
        self.assertEqual(result["returncode"], 1)
        self.assertEqual(result["stderr"], "connection refused")

    def test_html_uses_operator_flow_sections(self):
        self.assertIn("DNP3 Outstation 連線", INDEX_HTML)
        self.assertIn("Outstation IP", INDEX_HTML)
        self.assertIn("Outstation Port", INDEX_HTML)
        self.assertIn("檢查 TCP 端點", INDEX_HTML)
        self.assertIn("TCP 端點狀態：未檢查", INDEX_HTML)
        self.assertIn("/api/endpoint/check", INDEX_HTML)
        self.assertIn("endpointReady", INDEX_HTML)
        self.assertIn("resetEndpointStatus", INDEX_HTML)
        self.assertIn("目前 AO 目標 DNP3 ID", INDEX_HTML)
        self.assertIn('<select id="outstationAddress"></select>', INDEX_HTML)
        self.assertIn("aoTargetOptions", INDEX_HTML)
        self.assertIn("renderAoTargetOptions", INDEX_HTML)
        self.assertIn("機器狀態與內容", INDEX_HTML)
        self.assertIn("上線時間", INDEX_HTML)
        self.assertIn("斷線時間", INDEX_HTML)
        self.assertIn("開始監控選取", INDEX_HTML)
        self.assertIn("停止監控", INDEX_HTML)
        self.assertIn("輪詢目標", INDEX_HTML)
        self.assertIn("全選", INDEX_HTML)
        self.assertIn("清除選取", INDEX_HTML)
        self.assertIn("monitorSelectedAddresses", INDEX_HTML)
        self.assertIn("selectableMonitorAddresses", INDEX_HTML)
        self.assertIn("monitor-dnp3-address", INDEX_HTML)
        self.assertIn("machine-filter-bar", INDEX_HTML)
        self.assertIn("machine-status-strip", INDEX_HTML)
        self.assertIn("machine-result-strip", INDEX_HTML)
        self.assertIn("machine-actions-cell", INDEX_HTML)
        self.assertIn("setAddressSummary", INDEX_HTML)
        self.assertIn("addressPreview", INDEX_HTML)
        self.assertIn("overflow-x: hidden", INDEX_HTML)
        self.assertIn("width: min(1320px, 100%)", INDEX_HTML)
        self.assertIn("grid-template-columns: minmax(280px, 320px) minmax(0, 1fr)", INDEX_HTML)
        self.assertIn("content > section", INDEX_HTML)
        self.assertIn("機器內容：", INDEX_HTML)
        self.assertIn("最新內容", INDEX_HTML)
        self.assertIn("machineLinkTimes", INDEX_HTML)
        self.assertIn("rememberMachinePoints", INDEX_HTML)
        self.assertIn("rememberMachineOnline", INDEX_HTML)
        self.assertIn("rememberMonitorLinkTimes", INDEX_HTML)
        self.assertIn("formatEpochSeconds", INDEX_HTML)
        self.assertIn("MACHINE_SUMMARY_POINTS = [7", INDEX_HTML)
        self.assertNotIn("rememberMachineStatusPoint", INDEX_HTML)
        self.assertNotIn("mqtt_status", INDEX_HTML)
        self.assertIn("machineSummary", INDEX_HTML)
        self.assertIn("machineSearch", INDEX_HTML)
        self.assertIn("machineStatusFilter", INDEX_HTML)
        self.assertIn("machineStatusCounts", INDEX_HTML)
        self.assertIn("updateMachineStatusCounts", INDEX_HTML)
        self.assertIn('<option value="down">離線</option>', INDEX_HTML)
        self.assertIn('<option value="unknown">未知</option>', INDEX_HTML)
        self.assertIn("狀態 監控中", INDEX_HTML)
        self.assertIn("離線", INDEX_HTML)
        self.assertIn("未知", INDEX_HTML)
        self.assertNotIn('<option value="selected">已選</option>', INDEX_HTML)
        self.assertNotIn('<option value="idle">未監控</option>', INDEX_HTML)
        self.assertIn("MACHINE_ROW_LIMIT", INDEX_HTML)
        self.assertIn("查看 / AO 目標", INDEX_HTML)
        self.assertIn("查看內容", INDEX_HTML)
        self.assertIn("設為 AO 目標", INDEX_HTML)
        self.assertIn("查看中", INDEX_HTML)
        self.assertIn("目前 AO 目標：DNP3", INDEX_HTML)
        self.assertIn("目前機器操作（AO）", INDEX_HTML)
        self.assertIn("<th style=\"width:92px;\">來源</th>", INDEX_HTML)
        self.assertIn("<table class=\"event-table\">", INDEX_HTML)
        self.assertIn("<th class=\"time-col\">DNP 時間</th>", INDEX_HTML)
        self.assertIn("<th class=\"time-col\">接收時間</th>", INDEX_HTML)
        self.assertIn("timestamp-col", INDEX_HTML)
        self.assertIn("timestamp-cell", INDEX_HTML)
        self.assertIn("timestampText", INDEX_HTML)
        self.assertIn("font-variant-numeric: tabular-nums", INDEX_HTML)
        self.assertIn("name-col", INDEX_HTML)
        self.assertIn("name-cell", INDEX_HTML)
        self.assertIn("min-width: 360px", INDEX_HTML)
        self.assertIn("overflow-wrap: anywhere", INDEX_HTML)
        self.assertIn("目前 AO 目標：", INDEX_HTML)
        self.assertIn("Outstation 未回 SUCCESS", INDEX_HTML)
        self.assertIn("monitorTargetReady", INDEX_HTML)
        self.assertIn("監控中只能操作正在監控的 DNP3 ID", INDEX_HTML)
        self.assertIn("outstation_addresses: selectedMonitorAddresses()", INDEX_HTML)
        self.assertIn("MONITOR_STATUS_REFRESH_MS = 3000", INDEX_HTML)
        self.assertNotIn("setInterval(() => refreshMonitorStatus", INDEX_HTML)
        self.assertNotIn("<h2>DNP3 ID API</h2>", INDEX_HTML)
        self.assertNotIn("<h2>機器清單</h2>", INDEX_HTML)
        self.assertNotIn("<h2>全部機器監控</h2>", INDEX_HTML)
        self.assertNotIn("<h2>Master Poll（多 ID 輪詢）</h2>", INDEX_HTML)
        self.assertNotIn("<h2>Analog Output</h2>", INDEX_HTML)
        self.assertNotIn("操作目標 DNP3 ID", INDEX_HTML)
        self.assertNotIn("送出目標：", INDEX_HTML)
        self.assertNotIn('<input id="outstationAddress"', INDEX_HTML)

    def test_monitor_status_records_link_times(self):
        source = Path("tools/dnp3_master_ui.py").read_text()

        self.assertIn('"started_at": self.started_at', source)
        self.assertIn('"stopped_at": None', source)
        self.assertIn('"stopped_by_user": False', source)
        self.assertIn('self.targets[address]["stopped_by_user"] = True', source)
        self.assertIn('self.targets[address]["stopped_at"] = time.time()', source)

    def test_master_ui_monitor_does_not_auto_poll(self):
        source = Path("tools/dnp3_master_ui.py").read_text()

        self.assertIn('"--enable-unsolicited"', source)
        self.assertNotIn('"--poll"', source)


if __name__ == "__main__":
    unittest.main()
