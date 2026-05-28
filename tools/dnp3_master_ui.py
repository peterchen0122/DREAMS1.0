#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from dreams_outstation.config import load_config
from dreams_outstation.points import AI_POINTS, AO_POINTS
from dreams_outstation.site_bindings import load_effective_config


DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
DEFAULT_SIMULATOR_PATH = PROJECT_ROOT / "tools" / "dnp3_master_simulator.py"
DEFAULT_VENV_PYTHON = PROJECT_ROOT / ".venv-dnp3-py310" / "bin" / "python"
MEASUREMENT_LINE = re.compile(r"^\[(\d+)\]\s*:\s*([^:]+)\s*:\s*([^:]+)\s*:\s*(.*)$")
TRANSMISSION_LINE = re.compile(r"^\[tx\]\s*(\{.*\})$")
OPERATION_LOCK = threading.Lock()


def _default_settings(config_path: Path) -> dict[str, Any]:
    defaults = {
        "host": "127.0.0.1",
        "port": 20000,
        "master_address": 100,
        "outstation_address": 1,
    }
    try:
        config, _status = load_effective_config(load_config(config_path))
    except Exception:
        return defaults

    defaults["port"] = config.dnp3.port
    defaults["master_address"] = config.dnp3.master_address
    enabled_sites = config.enabled_sites()
    if enabled_sites:
        defaults["outstation_address"] = enabled_sites[0].dnp3_address
    return defaults


def _id_api_settings(
    config_path: Path,
    api_base_url: str,
    plant_meter_no: str | None,
    site_token: str | None,
) -> dict[str, Any]:
    try:
        config, _status = load_effective_config(load_config(config_path))
        plants = [
            {
                "plantNo": site.plant_no or (site.site_id if not _is_wildcard(site.site_id) else f"plant_{site.dnp3_address}"),
                "plantName": site.plant_name or site.plant_no or (site.site_id if not _is_wildcard(site.site_id) else f"Plant {site.dnp3_address}"),
                "loggerId": "" if _is_wildcard(site.logger_id) else site.logger_id,
                "dnp3Address": site.dnp3_address,
            }
            for site in config.enabled_sites()
        ]
        meter_no = plant_meter_no or config.dreams_api.plant_meter_no or "test-meter"
        token = site_token if site_token is not None else (config.dreams_api.site_token or "test-token")
    except Exception:
        plants = [{"plantNo": "plant_1", "plantName": "Plant 1", "loggerId": "", "dnp3Address": 1}]
        meter_no = plant_meter_no or "test-meter"
        token = site_token if site_token is not None else "test-token"

    endpoint = f"{api_base_url.rstrip('/')}/api/plants/plantMeterNo/{meter_no}"
    if token:
        endpoint = f"{endpoint}?token={token}"
    return {
        "plant_meter_no": meter_no,
        "site_token": token,
        "site_token_masked": "********" if token else "",
        "endpoint": endpoint,
        "plants": plants,
    }


def _simulated_plants_for_meter(meter_no: str, configured_meter_no: str, configured_plants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not meter_no:
        return []
    if not configured_meter_no or configured_meter_no == "*" or meter_no == configured_meter_no:
        return configured_plants

    seed = sum((index + 1) * ord(char) for index, char in enumerate(meter_no))
    count = seed % 3 + 1
    base_address = 10 + (seed % 200) * 10
    return [
        {
            "plantNo": f"{meter_no}_plant_{index + 1}",
            "plantName": f"{meter_no} Plant {index + 1}",
            "loggerId": "",
            "dnp3Address": base_address + index,
        }
        for index in range(count)
    ]


def _is_wildcard(value: str) -> bool:
    return value.strip() in {"", "*"}


def _point_payload() -> dict[str, list[dict[str, Any]]]:
    ai = [
        {
            "index": point.index,
            "key": point.key,
            "name": point.name,
            "unit": point.unit,
            "scale": point.scale,
            "enabled": point.enabled,
        }
        for _, point in sorted(AI_POINTS.items())
    ]
    ao = [
        {
            "index": point.index,
            "name": point.name,
            "unit": point.unit,
            "target": point.target,
            "type": point.command_type,
            "reserved": point.reserved,
        }
        for _, point in sorted(AO_POINTS.items())
    ]
    return {"ai": ai, "ao": ao}


def _float_or_text(value: str) -> float | str:
    try:
        return float(value.strip())
    except ValueError:
        return value.strip()


def _format_engineering(index: int, raw_value: float | str) -> str:
    point = AI_POINTS.get(index)
    if point is None or isinstance(raw_value, str):
        return str(raw_value)
    value = raw_value / point.scale
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.6g}"


def _parse_ai_range(output: str, start_index: int | None = None, stop_index: int | None = None) -> list[dict[str, Any]]:
    started = False
    values: dict[int, dict[str, Any]] = {}
    for line in output.splitlines():
        if line.startswith("[task] scan AI range") and "started" in line:
            started = True
            continue
        if started and line.startswith("[task]") and "completed" in line:
            break
        if not started:
            continue
        match = MEASUREMENT_LINE.match(line.strip())
        if not match:
            continue
        index = int(match.group(1))
        if start_index is not None and index < start_index:
            continue
        if stop_index is not None and index > stop_index:
            continue
        point = AI_POINTS.get(index)
        if point is None:
            continue
        raw_value = _float_or_text(match.group(2))
        values[index] = {
            "index": index,
            "key": point.key,
            "name": point.name,
            "raw_value": raw_value,
            "engineering_value": _format_engineering(index, raw_value),
            "unit": point.unit,
            "flags": match.group(3).strip(),
            "time": match.group(4).strip(),
        }
    return [values[index] for index in sorted(values)]


def _parse_transmission(output: str) -> list[dict[str, Any]]:
    transmissions: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = TRANSMISSION_LINE.match(line.strip())
        if not match:
            continue
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            transmissions.append(payload)
    return transmissions


def _parse_monitor_events(lines: list[dict[str, Any]], limit: int = 200) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in lines:
        match = MEASUREMENT_LINE.match(str(item.get("text", "")).strip())
        if not match:
            continue
        index = int(match.group(1))
        point = AI_POINTS.get(index)
        if point is None:
            continue
        raw_value = _float_or_text(match.group(2))
        flags = match.group(3).strip()
        flag_int = None
        try:
            flag_int = int(float(flags))
        except ValueError:
            pass
        events.append(
            {
                "seq": item.get("seq"),
                "received_at": item.get("ts"),
                "dnp3_address": item.get("dnp3_address"),
                "logger_id": item.get("logger_id") or "",
                "index": index,
                "key": point.key,
                "name": point.name,
                "raw_value": raw_value,
                "engineering_value": _format_engineering(index, raw_value),
                "unit": point.unit,
                "flags": flags,
                "time": match.group(4).strip(),
                "kind": "periodic" if flag_int is not None and flag_int & 0x80 else "event",
            }
        )
    cmd_ack_batches = {
        (event["dnp3_address"], event["time"])
        for event in events
        if event["kind"] == "event" and event["index"] in {18, 19}
    }
    for event in events:
        if event["kind"] == "periodic":
            event["source"] = "snapshot"
        elif (event["dnp3_address"], event["time"]) in cmd_ack_batches:
            event["source"] = "cmd_ack"
        else:
            event["source"] = "event"
    return events[-limit:]


def _validated_int(data: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(data.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer")
    if value < low or value > high:
        raise ValueError(f"{key} must be between {low} and {high}")
    return value


def _validated_float(data: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(data.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number")


def _base_simulator_args(data: dict[str, Any], defaults: dict[str, Any]) -> list[str]:
    host = str(data.get("host") or defaults["host"]).strip()
    if not host:
        raise ValueError("host is required")
    port = _validated_int(data, "port", int(defaults["port"]), 1, 65535)
    master_address = _validated_int(data, "master_address", int(defaults["master_address"]), 0, 65535)
    outstation_address = _validated_int(data, "outstation_address", int(defaults["outstation_address"]), 0, 65535)
    if master_address == outstation_address:
        raise ValueError("master_address and outstation_address must be different")
    return [
        "--host",
        host,
        "--port",
        str(port),
        "--master-address",
        str(master_address),
        "--outstation-address",
        str(outstation_address),
    ]


def _monitor_addresses(data: dict[str, Any], defaults: dict[str, Any]) -> list[int]:
    raw = data.get("outstation_addresses")
    if raw is None:
        raw_items: list[Any] = [data.get("outstation_address", defaults["outstation_address"])]
    elif isinstance(raw, str):
        raw_items = [item.strip() for item in raw.split(",") if item.strip()]
    elif isinstance(raw, list):
        raw_items = raw
    else:
        raise ValueError("outstation_addresses must be an array")

    addresses: list[int] = []
    seen: set[int] = set()
    master_address = _validated_int(data, "master_address", int(defaults["master_address"]), 0, 65535)
    for item in raw_items:
        try:
            address = int(item)
        except (TypeError, ValueError):
            raise ValueError("outstation_addresses must contain integers")
        if address < 0 or address > 65535:
            raise ValueError("outstation_addresses values must be between 0 and 65535")
        if address == master_address:
            raise ValueError("master_address and outstation_address must be different")
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    if not addresses:
        raise ValueError("Select at least one DNP3 ID")
    return addresses


def _logger_by_dnp3_address(id_api: dict[str, Any]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for plant in id_api.get("plants") or []:
        try:
            address = int(plant.get("dnp3Address"))
        except (TypeError, ValueError):
            continue
        logger_id = str(plant.get("loggerId") or "")
        if logger_id:
            mapping[address] = logger_id
    return mapping


def _simulator_env() -> dict[str, str]:
    env = os.environ.copy()
    expat_path = Path("/opt/homebrew/opt/expat/lib")
    if not env.get("DYLD_LIBRARY_PATH") and expat_path.exists():
        env["DYLD_LIBRARY_PATH"] = str(expat_path)
    return env


def _simulator_command(simulator_path: Path, args: list[str]) -> list[str]:
    python = DEFAULT_VENV_PYTHON if DEFAULT_VENV_PYTHON.exists() else Path(sys.executable)
    return [str(python), str(simulator_path), *args]


def _run_simulator_unlocked(simulator_path: Path, args: list[str], timeout: float = 30) -> dict[str, Any]:
    start = time.time()
    process = subprocess.run(
        _simulator_command(simulator_path, args),
        cwd=PROJECT_ROOT,
        env=_simulator_env(),
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return {
        "returncode": process.returncode,
        "stdout": process.stdout,
        "stderr": process.stderr,
        "duration_seconds": round(time.time() - start, 3),
        "transmission": _parse_transmission(process.stdout),
    }


def _run_simulator(simulator_path: Path, args: list[str], timeout: float = 30) -> dict[str, Any]:
    if not OPERATION_LOCK.acquire(blocking=False):
        raise RuntimeError("Another DNP3 operation is already running")
    try:
        return _run_simulator_unlocked(simulator_path, args, timeout)
    finally:
        OPERATION_LOCK.release()


def _run_multi_poll(
    simulator_path: Path,
    data: dict[str, Any],
    defaults: dict[str, Any],
    addresses: list[int],
    logger_by_address: dict[int, str],
    command_args: list[str],
) -> dict[str, Any]:
    if not OPERATION_LOCK.acquire(blocking=False):
        raise RuntimeError("Another DNP3 operation is already running")

    started = time.time()
    results: list[dict[str, Any]] = []
    transmissions: list[dict[str, Any]] = []
    try:
        for address in addresses:
            target_data = {**data, "outstation_address": address}
            result = _run_simulator_unlocked(
                simulator_path,
                [
                    *_base_simulator_args(target_data, defaults),
                    *command_args,
                ],
            )
            result["dnp3_address"] = address
            result["logger_id"] = logger_by_address.get(address, "")
            results.append(result)
            transmissions.extend(result.get("transmission") or [])
    finally:
        OPERATION_LOCK.release()

    stdout = "\n".join(_target_output(result, "stdout") for result in results if result.get("stdout")).strip()
    stderr = "\n".join(_target_output(result, "stderr") for result in results if result.get("stderr")).strip()
    returncodes = [int(result.get("returncode", 1)) for result in results]
    returncode = next((code for code in returncodes if code != 0), 0 if returncodes else 1)
    return {
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "duration_seconds": round(time.time() - started, 3),
        "transmission": transmissions,
        "results": results,
        "target_count": len(addresses),
    }


def _target_output(result: dict[str, Any], key: str) -> str:
    text = str(result.get(key) or "").strip()
    if not text:
        return ""
    logger = f" {result['logger_id']}" if result.get("logger_id") else ""
    return f"--- DNP3 {result['dnp3_address']}{logger} ---\n{text}"


class MultiMonitorProcess:
    def __init__(self, simulator_path: Path):
        self.simulator_path = simulator_path
        self.lock = threading.RLock()
        self.processes: dict[int, subprocess.Popen[str]] = {}
        self.targets: dict[int, dict[str, Any]] = {}
        self.lines: deque[dict[str, Any]] = deque(maxlen=2000)
        self.sequence = 0
        self.started_at: float | None = None
        self.returncode: int | None = None
        self.commands: dict[int, list[str]] = {}

    def start(self, specs: list[dict[str, Any]]) -> None:
        if not specs:
            raise ValueError("Select at least one DNP3 ID to monitor")
        with self.lock:
            if any(process.poll() is None for process in self.processes.values()):
                raise RuntimeError("DNP3 monitor is already running")
            self.processes.clear()
            self.targets.clear()
            self.commands.clear()
            self.lines.clear()
            self.sequence = 0
            self.started_at = time.time()
            self.returncode = None
            for spec in specs:
                address = int(spec["address"])
                logger_id = str(spec.get("logger_id") or "")
                args = list(spec["args"])
                command = _simulator_command(self.simulator_path, args)
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=PROJECT_ROOT,
                        env=_simulator_env(),
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        bufsize=1,
                    )
                except Exception:
                    self._terminate_all_locked()
                    raise
                self.processes[address] = process
                self.targets[address] = {
                    "dnp3_address": address,
                    "logger_id": logger_id,
                    "started_at": self.started_at,
                    "returncode": None,
                }
                self.commands[address] = command
                self._append_line("[monitor-ui] started persistent DNP3 master", address, logger_id)
                threading.Thread(
                    target=self._reader,
                    args=(address, logger_id, process),
                    name=f"dnp3-master-monitor-reader-{address}",
                    daemon=True,
                ).start()

    def stop(self) -> None:
        with self.lock:
            live = [(address, process) for address, process in self.processes.items() if process.poll() is None]
            if not live:
                self._append_line("[monitor-ui] monitor is not running")
                return
            for address, process in live:
                logger_id = str(self.targets.get(address, {}).get("logger_id") or "")
                self._append_line("[monitor-ui] stopping persistent DNP3 master", address, logger_id)
                process.terminate()
        for _address, process in live:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def status(self, since: int = 0) -> dict[str, Any]:
        with self.lock:
            monitors = []
            running_addresses = []
            for address, target in sorted(self.targets.items()):
                process = self.processes.get(address)
                is_running = process is not None and process.poll() is None
                if is_running:
                    running_addresses.append(address)
                monitors.append(
                    {
                        **target,
                        "running": is_running,
                        "returncode": None if process is None else process.poll(),
                    }
                )
            running = bool(running_addresses)
            lines = list(self.lines)
            new_lines = [line for line in lines if int(line["seq"]) > since]
            stdout = "\n".join(line["text"] for line in new_lines)
            full_stdout = "\n".join(line["text"] for line in lines)
            return {
                "running": running,
                "running_addresses": running_addresses,
                "monitors": monitors,
                "started_at": self.started_at,
                "returncode": self.returncode,
                "last_seq": self.sequence,
                "lines": new_lines,
                "stdout": stdout,
                "events": _parse_monitor_events(lines),
                "transmission": _parse_transmission(full_stdout),
            }

    def _reader(self, address: int, logger_id: str, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            self._append_line(line.rstrip("\n"), address, logger_id)
        returncode = process.wait()
        with self.lock:
            if address in self.targets:
                self.targets[address]["returncode"] = returncode
            if not any(item.poll() is None for item in self.processes.values()):
                self.returncode = 0 if all((item.poll() or 0) == 0 for item in self.processes.values()) else returncode
        self._append_line(f"[monitor-ui] exited returncode={returncode}", address, logger_id)

    def _append_line(self, text: str, dnp3_address: int | None = None, logger_id: str = "") -> None:
        with self.lock:
            self.sequence += 1
            self.lines.append(
                {
                    "seq": self.sequence,
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                    "text": text,
                    "dnp3_address": dnp3_address,
                    "logger_id": logger_id,
                }
            )

    def _terminate_all_locked(self) -> None:
        for process in self.processes.values():
            if process.poll() is None:
                process.terminate()
        for process in self.processes.values():
            if process.poll() is None:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        self.processes.clear()


class MasterUiState:
    def __init__(
        self,
        config_path: Path,
        simulator_path: Path,
        api_base_url: str,
        plant_meter_no: str | None,
        site_token: str | None,
    ):
        self.config_path = config_path
        self.simulator_path = simulator_path
        self.api_base_url = api_base_url
        self.plant_meter_no = plant_meter_no
        self.site_token = site_token
        self.config_lock = threading.RLock()
        self.defaults: dict[str, Any] = {}
        self.id_api: dict[str, Any] = {}
        self.points = _point_payload()
        self.monitor = MultiMonitorProcess(simulator_path)
        self.refresh_config()

    def refresh_config(self) -> dict[str, Any]:
        with self.config_lock:
            self.defaults = _default_settings(self.config_path)
            self.id_api = _id_api_settings(
                self.config_path,
                self.api_base_url,
                self.plant_meter_no,
                self.site_token,
            )
            return {"defaults": self.defaults, "points": self.points, "id_api": self.id_api}


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class MasterUiHandler(BaseHTTPRequestHandler):
    state: MasterUiState

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(INDEX_HTML)
        elif parsed.path == "/api/config":
            self._send_json(self.state.refresh_config())
        elif parsed.path.startswith("/api/plants/plantMeterNo/"):
            self._handle_plant_id_lookup(parsed)
        elif parsed.path == "/api/monitor/status":
            since_raw = parse_qs(parsed.query).get("since", ["0"])[0]
            try:
                since = int(since_raw)
            except ValueError:
                since = 0
            self._send_json(self.state.monitor.status(since))
        else:
            self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def _handle_plant_id_lookup(self, parsed: Any) -> None:
        prefix = "/api/plants/plantMeterNo/"
        meter_no = unquote(parsed.path[len(prefix) :])
        id_api = self.state.refresh_config()["id_api"]
        expected_meter_no = str(id_api.get("plant_meter_no") or "")

        expected_token = id_api.get("site_token") or ""
        token = parse_qs(parsed.query).get("token", [""])[0]
        if expected_token and token != expected_token:
            self._send_json({"error": "Invalid site token"}, HTTPStatus.UNAUTHORIZED)
            return
        self._send_json(
            _simulated_plants_for_meter(
                meter_no,
                expected_meter_no,
                id_api.get("plants") or [],
            )
        )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            data = self._read_json()
            if parsed.path == "/api/range":
                self._handle_range(data)
            elif parsed.path == "/api/scan":
                self._handle_scan(data)
            elif parsed.path == "/api/ao":
                self._handle_ao(data)
            elif parsed.path == "/api/monitor/start":
                self._handle_monitor_start(data)
            elif parsed.path == "/api/monitor/stop":
                self._handle_monitor_stop()
            else:
                self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except subprocess.TimeoutExpired:
            self._send_json({"error": "DNP3 operation timed out"}, HTTPStatus.GATEWAY_TIMEOUT)
        except (RuntimeError, ValueError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_range(self, data: dict[str, Any]) -> None:
        start = _validated_int(data, "start", 0, 0, 65535)
        stop = _validated_int(data, "stop", 32, start, 65535)
        config_payload = self.state.refresh_config()
        defaults = config_payload["defaults"]
        logger_by_address = _logger_by_dnp3_address(config_payload["id_api"])
        addresses = _monitor_addresses(data, defaults)
        result = _run_multi_poll(
            self.state.simulator_path,
            data,
            defaults,
            addresses,
            logger_by_address,
            [
                "range",
                str(start),
                str(stop),
                "--wait",
                str(_validated_int(data, "wait", 8, 1, 120)),
            ],
        )
        points: list[dict[str, Any]] = []
        for item in result["results"]:
            address = int(item["dnp3_address"])
            logger_id = str(item.get("logger_id") or "")
            points.extend(
                {
                    **point,
                    "dnp3_address": address,
                    "logger_id": logger_id,
                }
                for point in _parse_ai_range(str(item.get("stdout") or ""), start, stop)
            )
        result["points"] = points
        self._send_json(result)

    def _handle_scan(self, data: dict[str, Any]) -> None:
        classes = str(data.get("classes") or "events")
        if classes not in {"events", "class0", "all"}:
            raise ValueError("classes must be events, class0, or all")
        config_payload = self.state.refresh_config()
        defaults = config_payload["defaults"]
        result = _run_multi_poll(
            self.state.simulator_path,
            data,
            defaults,
            _monitor_addresses(data, defaults),
            _logger_by_dnp3_address(config_payload["id_api"]),
            [
                "scan",
                "--classes",
                classes,
                "--wait",
                str(_validated_int(data, "wait", 8, 1, 120)),
            ],
        )
        self._send_json(result)

    def _handle_ao(self, data: dict[str, Any]) -> None:
        index = _validated_int(data, "index", 0, 0, 65535)
        if index not in AO_POINTS:
            raise ValueError(f"Unsupported AO index: {index}")
        value = _validated_float(data, "value", 0)
        variation = str(data.get("variation") or "int16")
        if variation not in {"int16", "int32", "float32", "double64"}:
            raise ValueError("variation must be int16, int32, float32, or double64")
        mode = str(data.get("mode") or ("sbo" if data.get("sbo") else "direct"))
        if mode not in {"direct", "sbo", "direct-no-ack"}:
            raise ValueError("mode must be direct, sbo, or direct-no-ack")
        args = [
            *_base_simulator_args(data, self.state.defaults),
            "ao",
            str(index),
            str(value),
            "--variation",
            variation,
            "--mode",
            mode,
            "--wait",
            str(_validated_int(data, "wait", 8, 1, 120)),
        ]
        result = _run_simulator(self.state.simulator_path, args)
        self._send_json(result)

    def _handle_monitor_start(self, data: dict[str, Any]) -> None:
        config_payload = self.state.refresh_config()
        defaults = config_payload["defaults"]
        id_api = config_payload["id_api"]
        logger_by_address = _logger_by_dnp3_address(id_api)
        specs = []
        for address in _monitor_addresses(data, defaults):
            target_data = {**data, "outstation_address": address}
            specs.append(
                {
                    "address": address,
                    "logger_id": logger_by_address.get(address, ""),
                    "args": [
                        *_base_simulator_args(target_data, defaults),
                        "monitor",
                        "--enable-unsolicited",
                    ],
                }
            )
        self.state.monitor.start(specs)
        self._send_json(self.state.monitor.status())

    def _handle_monitor_stop(self) -> None:
        self.state.monitor.stop()
        self._send_json(self.state.monitor.status())

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON payload must be an object")
        return data

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = _json_bytes(payload)
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DNP3 Master Simulator</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --surface: #ffffff;
      --line: #d8dde5;
      --line-strong: #b7c0cf;
      --text: #1d2430;
      --muted: #5d6675;
      --accent: #0f766e;
      --accent-dark: #0b5e58;
      --danger: #b42318;
      --ok: #18794e;
      --warn: #946200;
      --code: #101828;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      letter-spacing: 0;
    }
    header {
      background: var(--surface);
      border-bottom: 1px solid var(--line);
      padding: 16px 22px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      position: sticky;
      top: 0;
      z-index: 4;
    }
    h1 { font-size: 20px; margin: 0; font-weight: 700; }
    .header-main {
      display: grid;
      gap: 6px;
      min-width: 0;
    }
    .header-summary {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 13px;
    }
    .summary-chip {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 8px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #f9fafb;
      max-width: min(560px, 80vw);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    main {
      width: min(1440px, 100%);
      margin: 0 auto;
      padding: 18px 22px 28px;
      display: grid;
      grid-template-columns: 380px minmax(0, 1fr);
      gap: 18px;
    }
    section {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .panel-head {
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .panel-head h2 { font-size: 15px; margin: 0; }
    .panel-body { padding: 16px; }
    .stack { display: grid; gap: 14px; }
    .grid-2 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    label {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }
    input, select {
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      padding: 8px 10px;
      font: inherit;
      font-size: 14px;
    }
    .button-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    button {
      min-height: 38px;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      font: inherit;
      font-size: 14px;
      font-weight: 700;
      padding: 8px 12px;
      cursor: pointer;
    }
    button.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }
    button.primary:hover { background: var(--accent-dark); }
    button:disabled {
      opacity: .55;
      cursor: wait;
    }
    button.small {
      min-height: 30px;
      padding: 4px 8px;
      font-size: 12px;
    }
    button.current {
      border-color: var(--accent);
      color: var(--accent);
      background: #edf7f5;
    }
    .toggle {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 38px;
      color: var(--text);
      font-size: 14px;
      font-weight: 650;
    }
    .toggle input {
      width: 16px;
      min-height: 16px;
    }
    .table-checkbox {
      width: 16px;
      min-height: 16px;
      padding: 0;
      margin: 0;
    }
    .target-summary {
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #f9fafb;
      color: var(--text);
      padding: 9px 10px;
      font-size: 13px;
    }
    .subsection {
      display: grid;
      gap: 10px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
    }
    .subsection:first-child {
      padding-top: 0;
      border-top: 0;
    }
    .section-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    tr.command-target-row {
      background: #eef8f6;
    }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 6px 10px;
      background: #fff;
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--warn);
    }
    .status.ok .dot { background: var(--ok); }
    .status.err .dot { background: var(--danger); }
    .content {
      display: grid;
      grid-template-rows: auto auto auto minmax(220px, 1fr);
      gap: 18px;
      min-width: 0;
    }
    .table-wrap {
      overflow: auto;
      max-height: calc(100vh - 230px);
    }
    .table-wrap.compact {
      max-height: 180px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 13px;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 9px 10px;
      text-align: left;
      vertical-align: middle;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    th {
      position: sticky;
      top: 0;
      z-index: 2;
      background: #f9fafb;
      color: var(--muted);
      font-size: 12px;
    }
    .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      color: var(--code);
    }
    .console {
      min-height: 180px;
      max-height: 320px;
      overflow: auto;
      background: #101828;
      color: #eef4ff;
      border-radius: 0 0 8px 8px;
      padding: 12px 14px;
      font-size: 12px;
      line-height: 1.5;
      white-space: pre-wrap;
    }
    .empty {
      padding: 32px 16px;
      text-align: center;
      color: var(--muted);
    }
    .result-line {
      color: var(--muted);
      font-size: 13px;
      min-height: 20px;
    }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; padding: 14px; }
      header { padding: 14px; align-items: flex-start; }
      .grid-2 { grid-template-columns: 1fr; }
      .table-wrap { max-height: none; }
    }
  </style>
</head>
<body>
  <header>
    <div class="header-main">
      <h1>DNP3 Master Simulator</h1>
      <div class="header-summary">
        <span id="headerMonitor" class="summary-chip">Monitor: stopped</span>
        <span id="headerCommand" class="summary-chip">AO: not set</span>
      </div>
    </div>
    <div id="status" class="status"><span class="dot"></span><span>Idle</span></div>
  </header>
  <main>
    <div class="stack">
      <section>
        <div class="panel-head"><h2>DNP3 Endpoint</h2></div>
        <div class="panel-body stack">
          <div class="grid-2">
            <label>Host
              <input id="host" value="127.0.0.1" autocomplete="off">
            </label>
            <label>Port
              <input id="port" type="number" min="1" max="65535" value="20000">
            </label>
          </div>
          <label>Analog Output DNP3 ID
            <input id="outstationAddress" type="number" min="0" max="65535" value="1">
          </label>
          <input id="wait" type="hidden" value="8">
          <input id="masterAddress" type="hidden" value="100">
          <div id="targetResult" class="target-summary"></div>
        </div>
      </section>

      <section>
        <div class="panel-head"><h2>Registered DNP3 IDs</h2></div>
        <div class="panel-body">
          <div id="idApiCount" class="result-line"></div>
        </div>
        <div class="table-wrap compact">
          <table>
            <thead>
              <tr>
                <th style="width:82px;">Monitor</th>
                <th>Logger</th>
                <th style="width:92px;">DNP3 ID</th>
                <th style="width:92px;">AO Target</th>
              </tr>
            </thead>
            <tbody id="idApiPlantRows">
              <tr><td colspan="4" class="empty">No DNP3 IDs configured</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <div class="panel-head"><h2>Multi-ID Monitor</h2></div>
        <div class="panel-body stack">
          <div id="monitorTargets" class="result-line">0 DNP3 ID selected</div>
          <div class="button-row">
            <button id="startMonitor" class="primary">Start Monitor</button>
            <button id="stopMonitor">Stop</button>
          </div>
          <div id="monitorResult" class="result-line"></div>
        </div>
      </section>

      <section>
        <div class="panel-head"><h2>Master Poll</h2></div>
        <div class="panel-body stack">
          <div id="pollTargets" class="result-line">0 DNP3 ID selected</div>
          <div class="button-row">
            <button id="readAi" class="primary">Read AI</button>
            <button id="scanEvents">Scan Events</button>
          </div>
          <div id="pollResult" class="result-line"></div>
        </div>
      </section>

      <section>
        <div class="panel-head"><h2>Single-ID Analog Output</h2></div>
        <div class="panel-body stack">
          <label>AO Point
            <select id="aoPoint"></select>
          </label>
          <label>Raw Value
            <input id="aoValue" type="number" step="any" value="50">
          </label>
          <input id="variation" type="hidden" value="int16">
          <input id="operationMode" type="hidden" value="direct">
          <div class="button-row">
            <button id="sendAo" class="primary">Operate</button>
          </div>
          <div id="aoResult" class="result-line"></div>
        </div>
      </section>
    </div>

    <div class="content">
      <section>
        <div class="panel-head">
          <h2>Polled AI</h2>
          <span id="pointCount" class="result-line">0 values</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th style="width:92px;">DNP3 ID</th>
                <th style="width:130px;">Logger</th>
                <th style="width:76px;">Point</th>
                <th>Name</th>
                <th style="width:120px;">Raw</th>
                <th style="width:120px;">Value</th>
                <th style="width:110px;">Unit</th>
                <th style="width:90px;">Flags</th>
              </tr>
            </thead>
            <tbody id="aiRows">
              <tr><td colspan="8" class="empty">No AI scan yet</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <div class="panel-head">
          <h2>DNP3 Messages</h2>
          <span id="txCount" class="result-line">0 frames</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th style="width:170px;">Message</th>
                <th style="width:150px;">Function</th>
                <th style="width:130px;">Object</th>
                <th style="width:110px;">Point</th>
                <th style="width:110px;">Value</th>
                <th style="width:120px;">Address</th>
                <th>Qualifier</th>
              </tr>
            </thead>
            <tbody id="txRows">
              <tr><td colspan="7" class="empty">No transmission yet</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <div class="panel-head">
          <h2>Received Events</h2>
          <span id="eventCount" class="result-line">0 events</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th style="width:92px;">Source</th>
                <th style="width:92px;">DNP3 Type</th>
                <th style="width:92px;">DNP3 ID</th>
                <th style="width:130px;">Logger</th>
                <th style="width:76px;">Point</th>
                <th>Name</th>
                <th style="width:120px;">Raw</th>
                <th style="width:120px;">Value</th>
                <th style="width:110px;">Unit</th>
                <th style="width:90px;">Flags</th>
                <th style="width:150px;">DNP Time</th>
                <th style="width:150px;">Received</th>
              </tr>
            </thead>
            <tbody id="eventRows">
              <tr><td colspan="12" class="empty">No unsolicited/event data yet</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <div class="panel-head">
          <h2>Monitor Console</h2>
          <button id="clearConsole">Clear</button>
        </div>
        <pre id="console" class="console"></pre>
      </section>
    </div>
  </main>

  <script>
    const $ = id => document.getElementById(id);
    const MONITOR_STATUS_REFRESH_MS = 3000;
    const state = { points: null, busy: false, monitor: false, monitorSeq: 0, monitorTimer: null, idApiPlants: [], runningAddresses: [] };

    function setStatus(text, kind = '') {
      const el = $('status');
      el.className = `status ${kind}`;
      el.querySelector('span:last-child').textContent = text;
    }

    function settings() {
      return {
        host: $('host').value.trim(),
        port: Number($('port').value),
        master_address: Number($('masterAddress').value),
        outstation_address: Number($('outstationAddress').value),
        wait: Number($('wait').value)
      };
    }

    function commandTargetAddress() {
      const value = Number($('outstationAddress').value);
      return Number.isInteger(value) && value >= 0 && value <= 65535 ? value : null;
    }

    function commandTargetPlant() {
      const address = commandTargetAddress();
      if (address === null) return null;
      return (state.idApiPlants || []).find(plant => Number(plant.dnp3Address) === address) || null;
    }

    function commandTargetText() {
      const address = commandTargetAddress();
      if (address === null) return 'AO target is not set';
      const plant = commandTargetPlant();
      if (plant) {
        const logger = plant.loggerId || '-';
        return `AO target: ${logger} / DNP3 ${address}`;
      }
      return `AO target: DNP3 ${address}`;
    }

    function commandTargetHeaderText() {
      return commandTargetText().replace('AO target', 'AO');
    }

    function updateCommandButtons() {
      const pollDisabled = state.busy || selectedMonitorAddresses().length === 0;
      for (const id of ['readAi', 'scanEvents']) {
        $(id).disabled = pollDisabled;
      }
      const aoDisabled = state.busy || commandTargetAddress() === null;
      for (const id of ['sendAo']) {
        $(id).disabled = aoDisabled;
      }
    }

    function selectedAddressPayload() {
      return { ...settings(), outstation_addresses: selectedMonitorAddresses() };
    }

    function pollTargetText(data) {
      const count = Number(data?.target_count || selectedMonitorAddresses().length || 0);
      return `${count} ${count === 1 ? 'DNP3 ID' : 'DNP3 IDs'}`;
    }

    function updatePollTargets(addresses) {
      const label = addresses.length === 1 ? 'DNP3 ID' : 'DNP3 IDs';
      $('pollTargets').textContent = `${addresses.length} ${label} selected${addresses.length ? `: ${addresses.join(', ')}` : ''}`;
      const disabled = state.busy || addresses.length === 0;
      for (const id of ['readAi', 'scanEvents']) {
        $(id).disabled = disabled;
      }
    }

    function renderCommandTarget() {
      const address = commandTargetAddress();
      $('targetResult').textContent = commandTargetText();
      $('headerCommand').textContent = commandTargetHeaderText();
      document.querySelectorAll('#idApiPlantRows tr[data-address]').forEach(row => {
        row.classList.toggle('command-target-row', Number(row.dataset.address) === address);
      });
      document.querySelectorAll('.use-dnp3-address').forEach(button => {
        const isCurrent = Number(button.dataset.address) === address;
        button.textContent = isCurrent ? 'Current' : 'Use';
        button.classList.toggle('current', isCurrent);
      });
      updateCommandButtons();
    }

    function updateHeaderMonitor() {
      const addresses = state.monitor ? (state.runningAddresses || []) : selectedMonitorAddresses();
      if (state.monitor) {
        $('headerMonitor').textContent = `Monitoring: ${addresses.length ? addresses.join(', ') : '-'}`;
      } else {
        $('headerMonitor').textContent = `Monitor selected: ${addresses.length ? addresses.join(', ') : '-'}`;
      }
    }

    function setBusy(busy) {
      state.busy = busy;
      updateCommandButtons();
      updateMonitorTargets();
      if (busy) setStatus('Running');
    }

    function setMonitorRunning(running) {
      state.monitor = running;
      $('startMonitor').disabled = running || state.busy;
      $('stopMonitor').disabled = !running;
      updateCommandButtons();
      document.querySelectorAll('.monitor-dnp3-address').forEach(input => { input.disabled = running; });
      updateMonitorTargets();
      updateHeaderMonitor();
    }

    async function api(path, body) {
      const response = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    }

    async function apiGet(path) {
      const response = await fetch(path);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    }

    function appendConsole(title, data) {
      const stamp = new Date().toLocaleTimeString();
      const lines = [`[${stamp}] ${title}`];
      if (data.stdout) lines.push(data.stdout.trimEnd());
      if (data.stderr) lines.push('[stderr]', data.stderr.trimEnd());
      lines.push(`returncode=${data.returncode} duration=${data.duration_seconds}s`);
      const current = $('console').textContent;
      $('console').textContent = `${lines.join('\n')}\n\n${current}`.trimStart();
    }

    function appendConsoleText(title, text) {
      if (!text) return;
      const stamp = new Date().toLocaleTimeString();
      const current = $('console').textContent;
      $('console').textContent = `[${stamp}] ${title}\n${text.trimEnd()}\n\n${current}`.trimStart();
    }

    function appendMonitorConsole(lines) {
      const formatted = formatMonitorConsole(lines);
      if (formatted) appendConsoleText('Monitor', formatted);
    }

    function appendMonitorData(data) {
      const currentSeq = Number(state.monitorSeq || 0);
      let lines = (data.lines || [])
        .filter(line => Number(line.seq || 0) > currentSeq);
      if (!lines.length && !currentSeq && data.stdout) {
        lines = String(data.stdout).split(/\r?\n/);
      }
      appendMonitorConsole(lines);
      state.monitorSeq = Number(data.last_seq || state.monitorSeq);
    }

    function formatMonitorConsole(lines) {
      const items = Array.isArray(lines) ? lines : String(lines || '').split(/\r?\n/);
      return items
        .map(formatMonitorLine)
        .filter(Boolean)
        .join('\n');
    }

    function monitorSource(line) {
      if (!line || typeof line !== 'object' || line.dnp3_address === null || line.dnp3_address === undefined) return '';
      const logger = line.logger_id ? ` ${line.logger_id}` : '';
      return `DNP3 ${line.dnp3_address}${logger}`;
    }

    function formatMonitorLine(line) {
      const text = String(line && typeof line === 'object' ? line.text || '' : line || '').trim();
      const source = monitorSource(line);
      const prefix = source ? `${source} ` : '';
      if (!text) return '';

      const point = text.match(/^\[(\d+)\]\s*:\s*([^:]+)\s*:\s*([^:]+)\s*:\s*(.*)$/);
      if (point) {
        const index = Number(point[1]);
        const raw = point[2].trim();
        const flags = point[3].trim();
        const timestamp = point[4].trim();
        const meta = pointMeta(index);
        const value = monitorEngineeringValue(meta, raw);
        const kind = monitorKind(flags);
        const name = meta ? meta.name : `AI_${index}`;
        const unit = meta && meta.unit && meta.unit !== '-' ? ` ${meta.unit}` : '';
        return `RX ${prefix}${kind} AI_${index} ${name}: value=${value}${unit} raw=${raw} flags=${flags} time=${timestamp}`;
      }

      if (text.startsWith('[tx]')) {
        return formatMonitorTx(text.slice(4).trim());
      }

      const connected = text.match(/^\[connected\]\s+master=(\d+)\s+outstation=(\d+)\s+(.+)$/);
      if (connected) {
        return `LINK open master=${connected[1]} outstation=${connected[2]} ${connected[3]}`;
      }

      const task = text.match(/^\[task\]\s+(.+)$/);
      if (task) return `TASK ${task[1]}`;

      if (text === '[monitor-ui] started persistent DNP3 master') return `MONITOR ${prefix}started`.trim();
      if (text === '[monitor-ui] stopping persistent DNP3 master') return `MONITOR ${prefix}stopping`.trim();
      if (text === '[monitor-ui] monitor is not running') return 'MONITOR not running';
      if (text.startsWith('[monitor-ui] exited')) return `MONITOR ${prefix}${text.replace('[monitor-ui]', '').trim()}`.trim();
      if (text === '[monitor] listening for unsolicited events without polling') return `MONITOR ${prefix}listening for unsolicited Class 1/2/3 events`.trim();
      if (text.startsWith('[monitor] polling')) return `MONITOR ${prefix}${text.replace('[monitor]', '').trim()}`.trim();
      if (text === '[monitor] stopped') return `MONITOR ${prefix}stopped`.trim();
      if (text.startsWith('error:')) return `ERROR ${text.slice(6).trim()}`;
      return text;
    }

    function formatMonitorTx(jsonText) {
      try {
        const tx = JSON.parse(jsonText);
        const title = tx.operation || tx.function || 'frame';
        const pieces = [
          functionText(tx),
          objectText(tx),
          addressText(tx)
        ].filter(Boolean);
        return `TX ${title}${pieces.length ? ` | ${pieces.join(' | ')}` : ''}`;
      } catch {
        return `TX ${jsonText}`;
      }
    }

    function pointMeta(index) {
      return (state.points?.ai || []).find(point => Number(point.index) === Number(index));
    }

    function monitorEngineeringValue(meta, raw) {
      if (!meta) return raw;
      const number = Number(raw);
      if (!Number.isFinite(number) || !meta.scale) return raw;
      const value = number / Number(meta.scale);
      return Math.abs(value - Math.round(value)) < 1e-9 ? String(Math.round(value)) : String(Number(value.toPrecision(6)));
    }

    function monitorKind(flags) {
      const value = Number(flags);
      if (!Number.isFinite(value)) return 'event';
      return (value & 0x80) ? 'periodic' : 'event';
    }

    function renderAi(points) {
      $('pointCount').textContent = `${points.length} ${points.length === 1 ? 'value' : 'values'}`;
      if (!points.length) {
        $('aiRows').innerHTML = '<tr><td colspan="8" class="empty">No AI values returned</td></tr>';
        return;
      }
      $('aiRows').innerHTML = points.map(point => `
        <tr>
          <td class="mono">${escapeHtml(point.dnp3_address ?? '')}</td>
          <td class="mono">${escapeHtml(point.logger_id || '')}</td>
          <td class="mono">${point.key}</td>
          <td title="${escapeHtml(point.name)}">${escapeHtml(point.name)}</td>
          <td class="mono">${escapeHtml(String(point.raw_value))}</td>
          <td class="mono">${escapeHtml(String(point.engineering_value))}</td>
          <td>${escapeHtml(point.unit)}</td>
          <td class="mono">${escapeHtml(point.flags)}</td>
        </tr>
      `).join('');
    }

    function objectText(tx) {
      if (Array.isArray(tx.objects)) {
        return tx.objects.map(item => `Obj${item.object} Var${item.variation}`).join(', ');
      }
      if (tx.object !== undefined && tx.variation !== undefined) {
        return `Obj${tx.object} Var${tx.variation}`;
      }
      return '';
    }

    function functionText(tx) {
      if (Array.isArray(tx.function_codes)) {
        return `${tx.function} / F${tx.function_codes.join('+F')}`;
      }
      if (tx.function_code !== undefined) {
        return `${tx.function} / F${tx.function_code}`;
      }
      return tx.function || '';
    }

    function pointText(tx) {
      if (tx.point) return tx.point;
      if (tx.start !== undefined && tx.stop !== undefined) return `${tx.start}..${tx.stop}`;
      return '';
    }

    function valueText(tx) {
      if (tx.raw_value !== undefined) return String(tx.raw_value);
      return '';
    }

    function addressText(tx) {
      if (tx.master_address !== undefined && tx.outstation_address !== undefined) {
        return `${tx.master_address} -> ${tx.outstation_address}`;
      }
      return '';
    }

    function renderTransmission(transmissions) {
      $('txCount').textContent = `${transmissions.length} ${transmissions.length === 1 ? 'frame' : 'frames'}`;
      if (!transmissions.length) {
        $('txRows').innerHTML = '<tr><td colspan="7" class="empty">No transmission returned</td></tr>';
        return;
      }
      $('txRows').innerHTML = transmissions.map(tx => `
        <tr>
          <td title="${escapeHtml(tx.operation || '')}">${escapeHtml(tx.operation || '')}</td>
          <td class="mono" title="${escapeHtml(functionText(tx))}">${escapeHtml(functionText(tx))}</td>
          <td class="mono" title="${escapeHtml(objectText(tx))}">${escapeHtml(objectText(tx))}</td>
          <td class="mono">${escapeHtml(pointText(tx))}</td>
          <td class="mono">${escapeHtml(valueText(tx))}</td>
          <td class="mono">${escapeHtml(addressText(tx))}</td>
          <td title="${escapeHtml(tx.qualifier || '')}">${escapeHtml(tx.qualifier || '')}</td>
        </tr>
      `).join('');
    }

    function renderEvents(events) {
      $('eventCount').textContent = `${events.length} ${events.length === 1 ? 'event' : 'events'}`;
      if (!events.length) {
        $('eventRows').innerHTML = '<tr><td colspan="12" class="empty">No unsolicited/event data yet</td></tr>';
        return;
      }
      $('eventRows').innerHTML = events.slice().reverse().map(event => `
        <tr>
          <td>${escapeHtml(event.source || '')}</td>
          <td>${escapeHtml(event.kind || '')}</td>
          <td class="mono">${escapeHtml(event.dnp3_address ?? '')}</td>
          <td class="mono">${escapeHtml(event.logger_id || '')}</td>
          <td class="mono">${escapeHtml(event.key || '')}</td>
          <td title="${escapeHtml(event.name || '')}">${escapeHtml(event.name || '')}</td>
          <td class="mono">${escapeHtml(String(event.raw_value))}</td>
          <td class="mono">${escapeHtml(String(event.engineering_value))}</td>
          <td>${escapeHtml(event.unit || '')}</td>
          <td class="mono">${escapeHtml(event.flags || '')}</td>
          <td class="mono">${escapeHtml(event.time || '')}</td>
          <td class="mono">${escapeHtml(event.received_at || '')}</td>
        </tr>
      `).join('');
    }

    function renderIdApi(api) {
      const plants = api.plants || [];
      const existingInputs = [...document.querySelectorAll('.monitor-dnp3-address')];
      const knownAddresses = new Set(existingInputs.map(input => String(input.dataset.address || '')));
      const checkedAddresses = new Set(existingInputs.filter(input => input.checked).map(input => String(input.dataset.address || '')));
      state.idApiPlants = plants;
      $('idApiCount').textContent = `${plants.length} DNP3 ID record(s)`;
      if (!plants.length) {
        $('idApiPlantRows').innerHTML = '<tr><td colspan="4" class="empty">No DNP3 IDs configured</td></tr>';
        updateMonitorTargets();
        renderCommandTarget();
        return;
      }
      $('idApiPlantRows').innerHTML = plants.map(plant => `
        <tr data-address="${escapeHtml(plant.dnp3Address)}">
          <td><input class="table-checkbox monitor-dnp3-address" type="checkbox" data-address="${escapeHtml(plant.dnp3Address)}"${monitorAddressCheckedAttr(plant.dnp3Address, knownAddresses, checkedAddresses)}${state.monitor ? ' disabled' : ''}></td>
          <td class="mono">${escapeHtml(plant.loggerId || '-')}</td>
          <td class="mono">${escapeHtml(plant.dnp3Address)}</td>
          <td><button class="small use-dnp3-address" data-address="${escapeHtml(plant.dnp3Address)}">Use</button></td>
        </tr>
      `).join('');
      wireDnp3AddressButtons();
      wireMonitorAddressInputs();
      updateMonitorTargets();
      renderCommandTarget();
    }

    function monitorAddressCheckedAttr(address, knownAddresses, checkedAddresses) {
      const key = String(address);
      const shouldCheck = !knownAddresses.size || checkedAddresses.has(key) || !knownAddresses.has(key);
      return shouldCheck ? ' checked' : '';
    }

    function wireDnp3AddressButtons() {
      document.querySelectorAll('.use-dnp3-address').forEach(button => {
        button.addEventListener('click', () => {
          $('outstationAddress').value = button.dataset.address || '';
          renderCommandTarget();
        });
      });
    }

    function wireMonitorAddressInputs() {
      document.querySelectorAll('.monitor-dnp3-address').forEach(input => {
        input.addEventListener('change', updateMonitorTargets);
      });
    }

    function selectedMonitorAddresses() {
      return [...document.querySelectorAll('.monitor-dnp3-address:checked')]
        .map(input => Number(input.dataset.address))
        .filter(Number.isFinite);
    }

    function updateMonitorTargets() {
      const addresses = selectedMonitorAddresses();
      const label = addresses.length === 1 ? 'DNP3 ID' : 'DNP3 IDs';
      $('monitorTargets').textContent = `${addresses.length} ${label} selected${addresses.length ? `: ${addresses.join(', ')}` : ''}`;
      $('startMonitor').disabled = state.monitor || state.busy || addresses.length === 0;
      updatePollTargets(addresses);
      updateHeaderMonitor();
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    async function readAi() {
      setBusy(true);
      $('pollResult').textContent = '';
      try {
        const data = await api('/api/range', { ...selectedAddressPayload(), start: 0, stop: 32 });
        renderAi(data.points || []);
        renderTransmission(data.transmission || []);
        appendConsole(`Read AI_0..AI_32 (${pollTargetText(data)})`, data);
        $('pollResult').textContent = data.returncode === 0 ? `AI scan completed for ${pollTargetText(data)}` : 'AI scan returned an error';
        setStatus(data.returncode === 0 ? 'Last scan OK' : 'Scan error', data.returncode === 0 ? 'ok' : 'err');
      } catch (error) {
        $('pollResult').textContent = error.message;
        setStatus('Error', 'err');
      } finally {
        setBusy(false);
      }
    }

    async function scanEvents() {
      setBusy(true);
      $('pollResult').textContent = '';
      try {
        const data = await api('/api/scan', { ...selectedAddressPayload(), classes: 'events' });
        renderTransmission(data.transmission || []);
        appendConsole(`Scan Events (${pollTargetText(data)})`, data);
        $('pollResult').textContent = data.returncode === 0 ? `Event scan completed for ${pollTargetText(data)}` : 'Event scan returned an error';
        setStatus(data.returncode === 0 ? 'Last scan OK' : 'Scan error', data.returncode === 0 ? 'ok' : 'err');
      } catch (error) {
        $('pollResult').textContent = error.message;
        setStatus('Error', 'err');
      } finally {
        setBusy(false);
      }
    }

    async function sendAo() {
      setBusy(true);
      $('aoResult').textContent = '';
      try {
        const data = await api('/api/ao', {
          ...settings(),
          index: Number($('aoPoint').value),
          value: Number($('aoValue').value),
          variation: $('variation').value,
          mode: $('operationMode').value
        });
        renderTransmission(data.transmission || []);
        appendConsole(`Operate AO_${$('aoPoint').value}`, data);
        $('aoResult').textContent = data.returncode === 0 ? 'AO command completed' : 'AO command returned an error';
        setStatus(data.returncode === 0 ? 'Last AO OK' : 'AO error', data.returncode === 0 ? 'ok' : 'err');
      } catch (error) {
        $('aoResult').textContent = error.message;
        setStatus('Error', 'err');
      } finally {
        setBusy(false);
      }
    }

    function monitorPayload() {
      return selectedAddressPayload();
    }

    function monitorStatusText(data) {
      const addresses = data.running_addresses || [];
      if (!data.running) return 'Monitor stopped';
      return `Monitoring ${addresses.length} ${addresses.length === 1 ? 'DNP3 ID' : 'DNP3 IDs'}${addresses.length ? `: ${addresses.join(', ')}` : ''}`;
    }

    async function startMonitor() {
      $('monitorResult').textContent = '';
      try {
        const data = await api('/api/monitor/start', monitorPayload());
        state.monitorSeq = 0;
        renderEvents(data.events || []);
        renderTransmission(data.transmission || []);
        appendMonitorData(data);
        state.runningAddresses = data.running_addresses || [];
        setMonitorRunning(Boolean(data.running));
        $('monitorResult').textContent = monitorStatusText(data);
        setStatus(data.running ? 'Monitoring' : 'Ready', data.running ? 'ok' : 'ok');
        scheduleMonitorPoll();
      } catch (error) {
        $('monitorResult').textContent = error.message;
        setStatus('Error', 'err');
      }
    }

    async function stopMonitor() {
      $('monitorResult').textContent = '';
      clearMonitorPoll();
      try {
        const data = await api('/api/monitor/stop', {});
        renderEvents(data.events || []);
        renderTransmission(data.transmission || []);
        appendMonitorData(data);
        state.runningAddresses = data.running_addresses || [];
        setMonitorRunning(Boolean(data.running));
        $('monitorResult').textContent = monitorStatusText(data);
        setStatus(data.running ? 'Monitoring' : 'Ready', data.running ? 'ok' : 'ok');
      } catch (error) {
        $('monitorResult').textContent = error.message;
        setStatus('Error', 'err');
      }
    }

    async function refreshMonitorStatus() {
      try {
        const data = await apiGet(`/api/monitor/status?since=${state.monitorSeq}`);
        appendMonitorData(data);
        renderEvents(data.events || []);
        renderTransmission(data.transmission || []);
        state.runningAddresses = data.running_addresses || [];
        setMonitorRunning(Boolean(data.running));
        if (data.running) {
          $('monitorResult').textContent = monitorStatusText(data);
          setStatus('Monitoring', 'ok');
        } else if (data.returncode !== null) {
          $('monitorResult').textContent = 'Monitor stopped';
          setStatus('Ready', 'ok');
        }
        if (data.running) {
          scheduleMonitorPoll();
        } else {
          clearMonitorPoll();
        }
      } catch (error) {
        $('monitorResult').textContent = error.message;
      }
    }

    function clearMonitorPoll() {
      if (!state.monitorTimer) return;
      clearTimeout(state.monitorTimer);
      state.monitorTimer = null;
    }

    function scheduleMonitorPoll() {
      clearMonitorPoll();
      if (!state.monitor) return;
      state.monitorTimer = setTimeout(() => {
        state.monitorTimer = null;
        refreshMonitorStatus().catch(error => {
          $('monitorResult').textContent = error.message;
          clearMonitorPoll();
        });
      }, MONITOR_STATUS_REFRESH_MS);
    }

    async function refreshIdApi() {
      if (state.busy || state.monitor) return;
      const data = await apiGet('/api/config');
      state.points = data.points || state.points;
      renderIdApi(data.id_api || {});
    }

    function scheduleIdApiRefresh() {
      setInterval(() => refreshIdApi().catch(() => {}), 5000);
    }

    async function loadConfig() {
      const response = await fetch('/api/config');
      const data = await response.json();
      state.points = data.points;
      $('host').value = data.defaults.host;
      $('port').value = data.defaults.port;
      $('masterAddress').value = data.defaults.master_address;
      $('outstationAddress').value = data.defaults.outstation_address;
      renderIdApi(data.id_api || {});
      $('aoPoint').innerHTML = data.points.ao.map(point => {
        const tag = point.reserved ? ' reserved' : '';
        return `<option value="${point.index}">AO_${point.index} [${escapeHtml(point.type)}] - ${escapeHtml(point.name)}${tag}</option>`;
      }).join('');
      if (data.points.ao.some(point => point.index === 1)) {
        $('aoPoint').value = '1';
      }
      setStatus('Ready', 'ok');
    }

    $('readAi').addEventListener('click', readAi);
    $('scanEvents').addEventListener('click', scanEvents);
    $('sendAo').addEventListener('click', sendAo);
    $('startMonitor').addEventListener('click', startMonitor);
    $('stopMonitor').addEventListener('click', stopMonitor);
    $('outstationAddress').addEventListener('input', renderCommandTarget);
    $('clearConsole').addEventListener('click', () => { $('console').textContent = ''; });
    setMonitorRunning(false);
    scheduleIdApiRefresh();
    loadConfig()
      .then(() => refreshMonitorStatus())
      .catch(error => setStatus(error.message, 'err'));
  </script>
</body>
</html>
"""


def run_server(
    host: str,
    port: int,
    config_path: Path,
    simulator_path: Path,
    plant_meter_no: str | None,
    site_token: str | None,
) -> None:
    handler = type("ConfiguredMasterUiHandler", (MasterUiHandler,), {})
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    handler.state = MasterUiState(
        config_path=config_path,
        simulator_path=simulator_path,
        api_base_url=f"http://{display_host}:{port}",
        plant_meter_no=plant_meter_no,
        site_token=site_token,
    )
    server = ThreadingHTTPServer((host, port), handler)
    print(f"DNP3 Master UI listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DNP3 Master simulator Web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--simulator", type=Path, default=DEFAULT_SIMULATOR_PATH)
    parser.add_argument("--plant-meter-no", default=os.getenv("DREAMS_SIMULATOR_PLANT_METER_NO"))
    parser.add_argument("--site-token", default=os.getenv("DREAMS_SIMULATOR_SITE_TOKEN"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_server(args.host, args.port, args.config, args.simulator, args.plant_meter_no, args.site_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
