#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import socket
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
COMMAND_RESULT_LINE = re.compile(r"^\[command\]\s+summary=([A-Z0-9_]+)(?:\s+results=(\d+))?")
COMMAND_TIMEOUT_LINE = re.compile(r"^\[command\]\s+timeout waiting for command result$")
UI_COMMAND_LINE = re.compile(r"^\[ui-command\]\s*(\{.*\})$")
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


def _normalize_dnp_time(value: str) -> str:
    text = str(value or "").strip()
    if text in {"0", "0.0", "0.000", "0.000000"}:
        return ""
    return text


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
            "time": _normalize_dnp_time(match.group(4)),
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


def _parse_command_result(output: str) -> dict[str, Any] | None:
    for line in output.splitlines():
        text = line.strip()
        match = COMMAND_RESULT_LINE.match(text)
        if match:
            summary = match.group(1)
            results = match.group(2)
            return {
                "summary": summary,
                "results": int(results) if results is not None else None,
                "success": summary == "SUCCESS",
            }
        if COMMAND_TIMEOUT_LINE.match(text):
            return {"summary": "TIMEOUT", "results": None, "success": False}
    return None


def _parse_ui_command_status(output: str, request_id: str) -> dict[str, Any] | None:
    status_payload = None
    for line in output.splitlines():
        match = UI_COMMAND_LINE.match(line.strip())
        if not match:
            continue
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("request_id") == request_id:
            status_payload = payload
    return status_payload


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
        if isinstance(raw_value, str) and raw_value.upper() == "INTERMEDIATE":
            continue
        if flag_int is not None and not (flag_int & 0x01):
            continue
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
                "time": _normalize_dnp_time(match.group(4)),
                "kind": "periodic" if flag_int is not None and flag_int & 0x80 else "event",
            }
        )
    cmd_ack_batches = {
        (event["dnp3_address"], event["time"] or event["received_at"])
        for event in events
        if event["kind"] == "event" and event["index"] in {18, 19}
    }
    for event in events:
        if event["kind"] == "periodic":
            event["source"] = "snapshot"
        elif (event["dnp3_address"], event["time"] or event["received_at"]) in cmd_ack_batches:
            event["source"] = "cmd_ack"
        else:
            event["source"] = "event"
    return events[-limit:]


def _validated_int(data: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(data.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key} 必須是整數")
    if value < low or value > high:
        raise ValueError(f"{key} 必須介於 {low} 到 {high}")
    return value


def _validated_float(data: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(data.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key} 必須是數字")


def _base_simulator_args(data: dict[str, Any], defaults: dict[str, Any]) -> list[str]:
    host = str(data.get("host") or defaults["host"]).strip()
    if not host:
        raise ValueError("host 必填")
    port = _validated_int(data, "port", int(defaults["port"]), 1, 65535)
    master_address = _validated_int(data, "master_address", int(defaults["master_address"]), 0, 65535)
    outstation_address = _validated_int(data, "outstation_address", int(defaults["outstation_address"]), 0, 65535)
    if master_address == outstation_address:
        raise ValueError("master_address 與 outstation_address 不可相同")
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
        raise ValueError("outstation_addresses 必須是陣列")

    addresses: list[int] = []
    seen: set[int] = set()
    master_address = _validated_int(data, "master_address", int(defaults["master_address"]), 0, 65535)
    for item in raw_items:
        try:
            address = int(item)
        except (TypeError, ValueError):
            raise ValueError("outstation_addresses 只能包含整數")
        if address < 0 or address > 65535:
            raise ValueError("outstation_addresses 的值必須介於 0 到 65535")
        if address == master_address:
            raise ValueError("master_address 與 outstation_address 不可相同")
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    if not addresses:
        raise ValueError("請至少選擇一個 DNP3 ID")
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
        "command_result": _parse_command_result(process.stdout),
    }


def _run_simulator(simulator_path: Path, args: list[str], timeout: float = 30) -> dict[str, Any]:
    if not OPERATION_LOCK.acquire(blocking=False):
        raise RuntimeError("已有另一個 DNP3 操作執行中")
    try:
        return _run_simulator_unlocked(simulator_path, args, timeout)
    finally:
        OPERATION_LOCK.release()


def _check_tcp_endpoint(host: str, port: int, timeout: float) -> dict[str, Any]:
    start = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as exc:
        return {
            "returncode": 1,
            "connected": False,
            "stdout": "",
            "stderr": str(exc),
            "duration_seconds": round(time.time() - start, 3),
            "host": host,
            "port": port,
            "check_type": "tcp",
        }
    return {
        "returncode": 0,
        "connected": True,
        "stdout": f"[tcp] connected {host}:{port}",
        "stderr": "",
        "duration_seconds": round(time.time() - start, 3),
        "host": host,
        "port": port,
        "check_type": "tcp",
    }


def _run_multi_poll(
    simulator_path: Path,
    data: dict[str, Any],
    defaults: dict[str, Any],
    addresses: list[int],
    logger_by_address: dict[int, str],
    command_args: list[str],
) -> dict[str, Any]:
    if not OPERATION_LOCK.acquire(blocking=False):
        raise RuntimeError("已有另一個 DNP3 操作執行中")

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
        self.condition = threading.Condition(self.lock)
        self.processes: dict[int, subprocess.Popen[str]] = {}
        self.targets: dict[int, dict[str, Any]] = {}
        self.lines: deque[dict[str, Any]] = deque(maxlen=2000)
        self.sequence = 0
        self.command_sequence = 0
        self.started_at: float | None = None
        self.returncode: int | None = None
        self.commands: dict[int, list[str]] = {}

    def start(self, specs: list[dict[str, Any]]) -> None:
        if not specs:
            raise ValueError("請至少選擇一個要監看的 DNP3 ID")
        with self.lock:
            if any(process.poll() is None for process in self.processes.values()):
                raise RuntimeError("DNP3 監看已在執行中")
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
                        stdin=subprocess.PIPE,
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
                    "stopped_at": None,
                    "returncode": None,
                    "stopped_by_user": False,
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
                if address in self.targets:
                    self.targets[address]["stopped_by_user"] = True
                process.terminate()
        for address, process in live:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            with self.lock:
                if address in self.targets:
                    returncode = process.poll()
                    self.targets[address]["returncode"] = 0 if self.targets[address].get("stopped_by_user") else returncode
                    if not self.targets[address].get("stopped_at"):
                        self.targets[address]["stopped_at"] = time.time()

    def operate_ao(
        self,
        address: int,
        index: int,
        value: float,
        variation: str,
        mode: str,
        timeout: float,
    ) -> dict[str, Any]:
        if not OPERATION_LOCK.acquire(blocking=False):
            raise RuntimeError("已有另一個 DNP3 操作執行中")

        started = time.time()
        try:
            with self.condition:
                process = self.processes.get(address)
                target = self.targets.get(address) or {}
                logger_id = str(target.get("logger_id") or "")
                if process is None or process.poll() is not None:
                    raise RuntimeError(f"DNP3 {address} 目前不在監控中，無法透過監控連線送出 AO")
                if process.stdin is None:
                    raise RuntimeError("監控行程沒有可用的命令通道")
                self.command_sequence += 1
                request_id = f"{address}-{self.command_sequence}-{int(time.time() * 1000)}"
                start_seq = self.sequence
                self._append_line(
                    f"[monitor-ui] queued AO_{index} raw={value} request_id={request_id}",
                    address,
                    logger_id,
                )

            payload = {
                "request_id": request_id,
                "command": "ao",
                "index": index,
                "value": value,
                "variation": variation,
                "mode": mode,
                "wait": timeout,
            }
            try:
                process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                raise RuntimeError("監控行程已停止，無法送出 AO")

            deadline = time.time() + timeout + 2
            final_status: dict[str, Any] | None = None
            with self.condition:
                while True:
                    command_lines = self._command_lines_locked(start_seq, address)
                    stdout = "\n".join(line["text"] for line in command_lines)
                    status = _parse_ui_command_status(stdout, request_id)
                    if status and status.get("status") in {"completed", "error"}:
                        final_status = status
                        break
                    if process.poll() is not None:
                        break
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    self.condition.wait(remaining)

                command_lines = self._command_lines_locked(start_seq, address)

            stdout = "\n".join(line["text"] for line in command_lines)
            command_result = _parse_command_result(stdout)
            if command_result is None:
                if final_status and final_status.get("status") == "error":
                    command_result = {"summary": "UI_ERROR", "results": None, "success": False}
                elif final_status is None:
                    command_result = {"summary": "UI_TIMEOUT", "results": None, "success": False}

            returncode = 0 if command_result and command_result.get("success") else 1
            stderr = ""
            if final_status and final_status.get("status") == "error":
                stderr = str(final_status.get("error") or "")
            elif final_status is None:
                stderr = "等待監控命令回覆逾時"

            return {
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
                "duration_seconds": round(time.time() - started, 3),
                "transmission": _parse_transmission(stdout),
                "command_result": command_result,
                "monitor_command": True,
                "dnp3_address": address,
                "logger_id": logger_id,
            }
        finally:
            OPERATION_LOCK.release()

    def status(self, since: int = 0) -> dict[str, Any]:
        with self.lock:
            monitors = []
            running_addresses = []
            for address, target in sorted(self.targets.items()):
                process = self.processes.get(address)
                is_running = process is not None and process.poll() is None
                returncode = None if process is None else process.poll()
                if not is_running and target.get("returncode") is not None:
                    returncode = target.get("returncode")
                if is_running:
                    running_addresses.append(address)
                monitors.append(
                    {
                        **target,
                        "running": is_running,
                        "returncode": returncode,
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
                stopped_by_user = bool(self.targets[address].get("stopped_by_user"))
                effective_returncode = 0 if stopped_by_user else returncode
                self.targets[address]["returncode"] = effective_returncode
                if not self.targets[address].get("stopped_at"):
                    self.targets[address]["stopped_at"] = time.time()
            if not any(item.poll() is None for item in self.processes.values()):
                self.returncode = 0 if all((target.get("returncode") or 0) == 0 for target in self.targets.values()) else returncode
        self._append_line(f"[monitor-ui] exited returncode={returncode}", address, logger_id)

    def _append_line(self, text: str, dnp3_address: int | None = None, logger_id: str = "") -> None:
        with self.condition:
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
            self.condition.notify_all()

    def _command_lines_locked(self, start_seq: int, address: int) -> list[dict[str, Any]]:
        return [
            line
            for line in self.lines
            if int(line["seq"]) > start_seq and int(line.get("dnp3_address") or -1) == address
        ]

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
            self._send_json({"error": "找不到資源"}, HTTPStatus.NOT_FOUND)

    def _handle_plant_id_lookup(self, parsed: Any) -> None:
        prefix = "/api/plants/plantMeterNo/"
        meter_no = unquote(parsed.path[len(prefix) :])
        id_api = self.state.refresh_config()["id_api"]
        expected_meter_no = str(id_api.get("plant_meter_no") or "")

        expected_token = id_api.get("site_token") or ""
        token = parse_qs(parsed.query).get("token", [""])[0]
        if expected_token and token != expected_token:
            self._send_json({"error": "site token 無效"}, HTTPStatus.UNAUTHORIZED)
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
            elif parsed.path == "/api/endpoint/check":
                self._handle_endpoint_check(data)
            elif parsed.path == "/api/monitor/start":
                self._handle_monitor_start(data)
            elif parsed.path == "/api/monitor/stop":
                self._handle_monitor_stop()
            else:
                self._send_json({"error": "找不到資源"}, HTTPStatus.NOT_FOUND)
        except subprocess.TimeoutExpired:
            self._send_json({"error": "DNP3 操作逾時"}, HTTPStatus.GATEWAY_TIMEOUT)
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
            raise ValueError("classes 必須是 events、class0 或 all")
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
            raise ValueError(f"不支援的 AO 點位：{index}")
        value = _validated_float(data, "value", 0)
        variation = str(data.get("variation") or "int16")
        if variation not in {"int16", "int32", "float32", "double64"}:
            raise ValueError("variation 必須是 int16、int32、float32 或 double64")
        mode = str(data.get("mode") or ("sbo" if data.get("sbo") else "direct"))
        if mode not in {"direct", "sbo", "direct-no-ack"}:
            raise ValueError("mode 必須是 direct、sbo 或 direct-no-ack")
        wait = _validated_int(data, "wait", 8, 1, 120)
        target_address = _validated_int(
            data,
            "outstation_address",
            int(self.state.defaults["outstation_address"]),
            0,
            65535,
        )
        monitor_status = self.state.monitor.status()
        if monitor_status.get("running"):
            running_addresses = [int(address) for address in monitor_status.get("running_addresses") or []]
            if target_address not in running_addresses:
                running_text = ", ".join(str(address) for address in running_addresses) or "-"
                raise RuntimeError(
                    f"DNP3 {target_address} 目前不在監控清單中；監控中只能操作正在監控的 ID（目前：{running_text}）。"
                )
            result = self.state.monitor.operate_ao(target_address, index, value, variation, mode, wait)
            self._send_json(result)
            return
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
            str(wait),
        ]
        result = _run_simulator(self.state.simulator_path, args)
        self._send_json(result)

    def _handle_endpoint_check(self, data: dict[str, Any]) -> None:
        defaults = self.state.refresh_config()["defaults"]
        wait = _validated_int(data, "wait", 8, 1, 30)
        host = str(data.get("host") or defaults["host"])
        port = _validated_int(data, "port", int(defaults["port"]), 1, 65535)
        result = _check_tcp_endpoint(host, port, wait)
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
            raise ValueError("JSON payload 必須是物件")
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
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DNP3 Master 模擬器</title>
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
	      overflow-x: hidden;
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
	      width: min(1320px, 100%);
	      margin: 0 auto;
	      padding: 16px 18px 26px;
	      display: grid;
	      grid-template-columns: minmax(280px, 320px) minmax(0, 1fr);
	      gap: 14px;
	    }
	    section {
	      background: var(--surface);
	      border: 1px solid var(--line);
	      border-radius: 8px;
	      min-width: 0;
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
	    .panel-body { padding: 14px; }
	    .stack { display: grid; gap: 14px; min-width: 0; }
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
	    .target-summary.ok {
	      border-color: #a8d5c3;
	      background: #edf7f2;
	      color: var(--ok);
	    }
	    .target-summary.err {
	      border-color: #e7aaa4;
	      background: #fff1f0;
	      color: var(--danger);
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
	    tr.machine-selected-row {
	      background: #f4f8fb;
	    }
	    .machine-table {
	      min-width: 1320px;
	    }
	    .machine-detail-table {
	      min-width: 1120px;
	    }
	    .machine-controls {
	      display: grid;
	      gap: 12px;
	      border-bottom: 1px solid var(--line);
	    }
	    .machine-filter-bar {
	      display: grid;
	      grid-template-columns: minmax(260px, 1fr) 180px;
	      gap: 12px;
	      align-items: end;
	    }
	    .machine-actions {
	      display: flex;
	      justify-content: space-between;
	      align-items: flex-start;
	      gap: 12px;
	      flex-wrap: wrap;
	      padding-top: 12px;
	      border-top: 1px solid var(--line);
	    }
	    .machine-action-group {
	      display: flex;
	      flex-wrap: wrap;
	      gap: 8px;
	    }
	    .machine-action-group button {
	      white-space: nowrap;
	    }
	    .machine-status-strip,
	    .machine-result-strip {
	      display: grid;
	      gap: 8px;
	    }
		    .machine-status-strip {
		      grid-template-columns: repeat(4, minmax(0, 1fr));
		    }
	    .machine-result-strip {
	      grid-template-columns: repeat(2, minmax(0, 1fr));
	    }
	    .machine-status-line {
	      min-height: 30px;
	      border: 1px solid var(--line);
	      border-radius: 6px;
	      background: #f9fafb;
	      padding: 6px 8px;
	      overflow: hidden;
	      text-overflow: ellipsis;
	      white-space: nowrap;
	    }
	    .machine-detail-head {
	      margin-top: 10px;
	      background: #fbfcfd;
	    }
	    .machine-actions-cell {
	      display: flex;
	      gap: 6px;
	    }
	    .state-pill {
	      display: inline-flex;
	      align-items: center;
	      justify-content: center;
	      min-height: 24px;
	      padding: 3px 8px;
	      border-radius: 999px;
	      border: 1px solid var(--line);
	      background: #f9fafb;
	      color: var(--muted);
	      font-size: 12px;
	      font-weight: 700;
	    }
	    .state-pill.ok {
	      border-color: #a8d5c3;
	      background: #edf7f2;
	      color: var(--ok);
	    }
	    .state-pill.warn {
	      border-color: #dfca93;
	      background: #fff7e6;
	      color: var(--warn);
	    }
	    .state-pill.err {
	      border-color: #e7aaa4;
	      background: #fff1f0;
	      color: var(--danger);
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
	      grid-template-rows: auto auto auto auto minmax(220px, 1fr);
	      gap: 18px;
	      min-width: 0;
	    }
	    .content > section {
	      min-width: 0;
	      overflow: hidden;
	    }
	    .table-wrap {
	      width: 100%;
	      max-width: 100%;
	      min-width: 0;
	      overflow-x: auto;
	      overflow-y: auto;
	      max-height: calc(100vh - 230px);
	    }
    .table-wrap.compact {
      max-height: 180px;
    }
	    .event-table {
	      min-width: 1440px;
	    }
	    .event-table .time-col {
	      width: 210px;
	    }
	    .timestamp-col {
	      width: 210px;
	    }
	    .timestamp-cell {
	      font-variant-numeric: tabular-nums;
	      min-width: 190px;
	      overflow: visible;
	      text-overflow: clip;
	    }
	    .name-col {
	      width: 360px;
	      min-width: 360px;
	    }
	    .name-cell {
	      white-space: normal;
	      overflow: visible;
	      text-overflow: clip;
	      line-height: 1.35;
	      overflow-wrap: anywhere;
	      max-height: 4.05em;
	    }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 13px;
    }
	    th, td {
	      border-bottom: 1px solid var(--line);
	      padding: 8px 10px;
	      text-align: left;
	      vertical-align: top;
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
	      main { grid-template-columns: 1fr; padding: 12px; }
	      header { padding: 14px; align-items: flex-start; }
	      .grid-2 { grid-template-columns: 1fr; }
	      .machine-filter-bar,
	      .machine-status-strip,
	      .machine-result-strip { grid-template-columns: 1fr; }
	      .table-wrap { max-height: none; }
	    }
  </style>
</head>
<body>
  <header>
    <div class="header-main">
      <h1>DNP3 Master 模擬器</h1>
      <div class="header-summary">
        <span id="headerMonitor" class="summary-chip">監控：已停止</span>
        <span id="headerCommand" class="summary-chip">AO：未設定</span>
      </div>
    </div>
    <div id="status" class="status"><span class="dot"></span><span>閒置</span></div>
  </header>
  <main>
    <div class="stack">
      <section>
        <div class="panel-head"><h2>DNP3 Outstation 連線</h2></div>
        <div class="panel-body stack">
          <div class="grid-2">
            <label>Outstation IP
              <input id="host" value="127.0.0.1" autocomplete="off">
            </label>
            <label>Outstation Port
              <input id="port" type="number" min="1" max="65535" value="20000">
            </label>
          </div>
          <input id="wait" type="hidden" value="8">
          <input id="masterAddress" type="hidden" value="100">
          <div class="button-row">
            <button id="checkEndpoint" class="primary">檢查 TCP 端點</button>
          </div>
          <div id="endpointResult" class="target-summary">TCP 端點狀態：未檢查</div>
        </div>
      </section>

      <section>
        <div class="panel-head"><h2>目前機器操作（AO）</h2></div>
        <div class="panel-body stack">
          <label>目前 AO 目標 DNP3 ID
            <select id="outstationAddress"></select>
          </label>
          <div id="targetResult" class="target-summary"></div>
          <label>AO 點位
            <select id="aoPoint"></select>
          </label>
          <label>Raw 值
            <input id="aoValue" type="number" step="any" value="50">
          </label>
          <input id="variation" type="hidden" value="int16">
          <input id="operationMode" type="hidden" value="direct">
          <div class="button-row">
            <button id="sendAo" class="primary">執行</button>
          </div>
          <div id="aoResult" class="result-line"></div>
        </div>
      </section>
    </div>

	    <div class="content">
	      <section>
		        <div class="panel-head">
		          <h2>機器狀態與內容</h2>
		          <span id="machineCount" class="result-line">0 台機器</span>
		        </div>
			        <div class="panel-body machine-controls">
			          <div class="machine-filter-bar">
			            <label>搜尋機器
			              <input id="machineSearch" autocomplete="off" placeholder="Logger / DNP3 ID / 內容">
			            </label>
			            <label>狀態
				              <select id="machineStatusFilter">
					                <option value="all">全部</option>
					                <option value="running">監控中</option>
					                <option value="down">離線</option>
				                <option value="unknown">未知</option>
				                <option value="data">有資料</option>
				              </select>
				            </label>
			          </div>
			          <div class="machine-actions">
			            <div class="machine-action-group">
			              <button id="selectAllMonitor">全選</button>
			              <button id="clearMonitorSelection">清除選取</button>
			            </div>
			            <div class="machine-action-group">
			              <button id="startMonitor" class="primary">開始監控選取</button>
			              <button id="stopMonitor">停止監控</button>
			              <button id="readAi" class="primary">讀取 AI</button>
			              <button id="scanEvents">掃描事件</button>
			            </div>
			          </div>
				          <div class="machine-status-strip">
				            <div id="machineFilterResult" class="result-line machine-status-line"></div>
				            <div id="machineStatusCounts" class="result-line machine-status-line">狀態 監控中 0 / 離線 0 / 未知 0</div>
				            <div id="monitorTargets" class="result-line machine-status-line">監控目標 0 個 DNP3 ID</div>
				            <div id="pollTargets" class="result-line machine-status-line">輪詢目標 0 個 DNP3 ID</div>
				          </div>
			          <div class="machine-result-strip">
			            <div id="monitorResult" class="result-line machine-status-line"></div>
			            <div id="pollResult" class="result-line machine-status-line"></div>
			          </div>
			        </div>
			        <div class="table-wrap">
	          <table class="machine-table">
	            <thead>
	              <tr>
	                <th style="width:62px;">監控</th>
	                <th style="width:86px;">狀態</th>
	                <th class="timestamp-col">上線時間</th>
	                <th class="timestamp-col">斷線時間</th>
	                <th style="width:116px;">Logger</th>
	                <th style="width:84px;">DNP3 ID</th>
	                <th class="timestamp-col">最後更新</th>
		                <th>最新內容</th>
	                <th style="width:170px;">查看 / AO 目標</th>
	              </tr>
		            </thead>
		            <tbody id="machineRows">
		              <tr><td colspan="9" class="empty">尚無機器資料</td></tr>
		            </tbody>
		          </table>
	        </div>
		        <div class="panel-head machine-detail-head">
	          <h2 id="machineDetailTitle">機器內容</h2>
	          <span id="machineDetailCount" class="result-line">0 筆點位</span>
	        </div>
	        <div class="table-wrap">
	          <table class="machine-detail-table">
	            <thead>
	              <tr>
	                <th style="width:78px;">點位</th>
		                <th class="name-col">名稱</th>
	                <th style="width:120px;">Raw</th>
	                <th style="width:120px;">值</th>
	                <th style="width:110px;">單位</th>
	                <th style="width:90px;">旗標</th>
		                <th class="timestamp-col">DNP 時間</th>
		                <th class="timestamp-col">接收時間</th>
	              </tr>
	            </thead>
	            <tbody id="machineDetailRows">
	              <tr><td colspan="8" class="empty">請選擇一台機器查看內容</td></tr>
	            </tbody>
	          </table>
	        </div>
	      </section>

	      <section>
	        <div class="panel-head">
	          <h2>輪詢讀取 AI</h2>
          <span id="pointCount" class="result-line">0 筆值</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th style="width:92px;">DNP3 ID</th>
                <th style="width:130px;">Logger</th>
                <th style="width:76px;">點位</th>
	                <th class="name-col">名稱</th>
                <th style="width:120px;">Raw</th>
                <th style="width:120px;">值</th>
                <th style="width:110px;">單位</th>
                <th style="width:90px;">旗標</th>
              </tr>
            </thead>
            <tbody id="aiRows">
              <tr><td colspan="8" class="empty">尚未掃描 AI</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <div class="panel-head">
          <h2>DNP3 訊息</h2>
          <span id="txCount" class="result-line">0 個訊框</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th style="width:170px;">訊息</th>
                <th style="width:150px;">功能</th>
                <th style="width:130px;">物件</th>
                <th style="width:110px;">點位</th>
                <th style="width:110px;">值</th>
                <th style="width:120px;">位址</th>
                <th>限定符</th>
              </tr>
            </thead>
            <tbody id="txRows">
              <tr><td colspan="7" class="empty">尚無傳輸紀錄</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <div class="panel-head">
          <h2>收到的事件</h2>
          <span id="eventCount" class="result-line">0 筆事件</span>
        </div>
        <div class="table-wrap">
          <table class="event-table">
            <thead>
              <tr>
                <th style="width:92px;">來源</th>
                <th style="width:92px;">DNP3 類型</th>
                <th style="width:92px;">DNP3 ID</th>
                <th style="width:130px;">Logger</th>
                <th style="width:76px;">點位</th>
	                <th class="name-col">名稱</th>
                <th style="width:120px;">Raw</th>
                <th style="width:120px;">值</th>
                <th style="width:110px;">單位</th>
                <th style="width:90px;">旗標</th>
                <th class="time-col">DNP 時間</th>
                <th class="time-col">接收時間</th>
              </tr>
            </thead>
            <tbody id="eventRows">
              <tr><td colspan="12" class="empty">尚無 unsolicited/event 資料</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <div class="panel-head">
          <h2>監控主控台</h2>
          <button id="clearConsole">清除</button>
        </div>
        <pre id="console" class="console"></pre>
      </section>
    </div>
  </main>

  <script>
	    const $ = id => document.getElementById(id);
	    const MONITOR_STATUS_REFRESH_MS = 3000;
	    const MACHINE_SUMMARY_POINTS = [7, 9, 15, 16, 18, 19];
	    const MACHINE_ROW_LIMIT = 200;
	    const state = {
	      points: null,
	      busy: false,
	      endpointStatus: 'unchecked',
	      endpointSignature: '',
	      monitor: false,
	      monitorSeq: 0,
	      monitorTimer: null,
		      idApiPlants: [],
		      monitorSelectedAddresses: new Set(),
		      knownMonitorAddresses: new Set(),
		      monitorSelectionInitialized: false,
		      runningAddresses: [],
	      monitorDetails: [],
	      machinePoints: {},
	      machineLinkTimes: {},
	      selectedMachineAddress: null,
	      machineFilterText: '',
	      machineStatusFilter: 'all'
	    };

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
	      if (address === null) return '目前 AO 目標尚未設定';
	      const plant = commandTargetPlant();
	      if (plant) {
	        const logger = plant.loggerId || '-';
	        return `目前 AO 目標：${logger} / DNP3 ${address}`;
	      }
	      return `目前 AO 目標：DNP3 ${address}`;
	    }

	    function commandTargetHeaderText() {
	      const text = commandTargetText();
	      if (text === '目前 AO 目標尚未設定') return 'AO：未設定';
	      return text.replace('目前 AO 目標：', 'AO：');
	    }

	    function updateCommandButtons() {
	      const endpointOk = endpointReady();
	      const pollDisabled = state.busy || !endpointOk || selectedMonitorAddresses().length === 0;
	      for (const id of ['readAi', 'scanEvents']) {
	        $(id).disabled = pollDisabled;
	      }
	      const targetAddress = commandTargetAddress();
	      const monitorTargetReady = !state.monitor || (state.runningAddresses || []).includes(targetAddress);
	      const aoDisabled = state.busy || !endpointOk || targetAddress === null || !monitorTargetReady;
	      for (const id of ['sendAo']) {
	        $(id).disabled = aoDisabled;
	      }
	      if (state.monitor && !monitorTargetReady) {
	        $('aoResult').textContent = '監控中只能操作正在監控的 DNP3 ID，請停止監控後調整清單。';
	      }
	      updateEndpointControls();
	    }

    function selectedAddressPayload() {
      return { ...settings(), outstation_addresses: selectedMonitorAddresses() };
    }

    function pollTargetText(data) {
      const count = Number(data?.target_count || selectedMonitorAddresses().length || 0);
      return `${count} 個 DNP3 ID`;
    }

	    function updatePollTargets(addresses) {
	      setAddressSummary('pollTargets', '輪詢目標', addresses);
	      const disabled = state.busy || !endpointReady() || addresses.length === 0;
	      for (const id of ['readAi', 'scanEvents']) {
	        $(id).disabled = disabled;
	      }
	    }

    function addressPreview(addresses, limit = 8) {
      if (!addresses.length) return '';
      const shown = addresses.slice(0, limit).join(', ');
      return addresses.length > limit ? `${shown}，另 ${addresses.length - limit} 個` : shown;
    }

    function setAddressSummary(id, label, addresses) {
      const el = $(id);
      const preview = addressPreview(addresses);
      el.textContent = `${label} ${addresses.length} 個 DNP3 ID${preview ? `：${preview}` : ''}`;
      el.title = addresses.length ? `${label}：${addresses.join(', ')}` : `${label}：未選取`;
    }

	    function renderCommandTarget() {
	      const address = commandTargetAddress();
	      $('targetResult').textContent = commandTargetText();
	      $('headerCommand').textContent = commandTargetHeaderText();
      document.querySelectorAll('#machineRows tr[data-machine-address]').forEach(row => {
        row.classList.toggle('command-target-row', Number(row.dataset.machineAddress) === address);
      });
	      updateCommandButtons();
	      renderMachineStatus();
	    }

	    function updateHeaderMonitor() {
	      const addresses = state.monitor ? (state.runningAddresses || []) : selectedMonitorAddresses();
	      if (state.monitor) {
	        $('headerMonitor').textContent = `監控中：${addresses.length ? addresses.join(', ') : '-'}`;
	      } else {
	        $('headerMonitor').textContent = `監控已選：${addresses.length ? addresses.join(', ') : '-'}`;
	      }
	    }

	    function plantForAddress(address) {
	      return (state.idApiPlants || []).find(plant => Number(plant.dnp3Address) === Number(address)) || null;
	    }

	    function machineAddressList() {
	      const addresses = new Set();
	      for (const plant of state.idApiPlants || []) {
	        const address = Number(plant.dnp3Address);
	        if (Number.isFinite(address)) addresses.add(address);
	      }
	      for (const address of selectedMonitorAddresses()) addresses.add(address);
	      for (const address of state.runningAddresses || []) addresses.add(Number(address));
	      for (const address of Object.keys(state.machinePoints || {})) addresses.add(Number(address));
	      for (const address of Object.keys(state.machineLinkTimes || {})) addresses.add(Number(address));
	      return [...addresses].filter(Number.isFinite).sort((a, b) => a - b);
	    }

	    function localTimestamp() {
	      return new Date().toLocaleString();
	    }

	    function formatEpochSeconds(value) {
	      const number = Number(value);
	      if (!Number.isFinite(number) || number <= 0) return '';
	      return new Date(number * 1000).toLocaleString();
	    }

	    function ensureMachineLinkTimes(address) {
	      const key = String(address);
	      if (!state.machineLinkTimes[key]) {
	        state.machineLinkTimes[key] = { online_at: '', offline_at: '', last_running: false };
	      }
	      return state.machineLinkTimes[key];
	    }

	    function rememberMachineOnline(address, timestamp = localTimestamp()) {
	      const times = ensureMachineLinkTimes(address);
	      if (!times.online_at || times.offline_at) {
	        times.online_at = timestamp;
	      }
	      times.offline_at = '';
	    }

	    function rememberMonitorLinkTimes(monitors = state.monitorDetails || []) {
	      const now = localTimestamp();
	      for (const monitor of monitors || []) {
	        const address = Number(monitor.dnp3_address);
	        if (!Number.isFinite(address)) continue;
	        const times = ensureMachineLinkTimes(address);
	        const onlineAt = formatEpochSeconds(monitor.started_at);
	        const offlineAt = formatEpochSeconds(monitor.stopped_at);
	        const running = Boolean(monitor.running);
	        if (running) {
	          if (!times.online_at || !times.last_running) {
	            times.online_at = onlineAt || now;
	          }
	          times.offline_at = '';
	        } else if (monitor.returncode !== null && monitor.returncode !== undefined) {
	          if (!times.online_at && onlineAt) times.online_at = onlineAt;
	          if (!times.offline_at) times.offline_at = offlineAt || now;
	        }
	        times.last_running = running;
	      }
	    }

	    function ensureMachineSnapshot(address, loggerId = '') {
	      const key = String(address);
	      if (!state.machinePoints[key]) {
	        state.machinePoints[key] = { address: Number(address), logger_id: loggerId || '', last_received_at: '', points: {} };
	      }
	      if (loggerId && !state.machinePoints[key].logger_id) {
	        state.machinePoints[key].logger_id = loggerId;
	      }
	      return state.machinePoints[key];
	    }

	    function rememberMachinePoints(points) {
	      const receivedAt = new Date().toLocaleString();
	      for (const point of points || []) {
	        const address = Number(point.dnp3_address ?? point.outstation_address);
	        if (!Number.isFinite(address)) continue;
	        const index = Number(point.index ?? String(point.key || '').replace(/^AI_/, ''));
	        if (!Number.isFinite(index)) continue;
	        const plant = plantForAddress(address);
	        const loggerId = point.logger_id || plant?.loggerId || '';
	        const snapshot = ensureMachineSnapshot(address, loggerId);
	        const key = point.key || `AI_${index}`;
	        const record = {
	          ...point,
	          index,
	          key,
	          logger_id: loggerId,
	          received_at: point.received_at || receivedAt
	        };
	        snapshot.points[index] = record;
	        snapshot.last_received_at = record.received_at;
	        rememberMachineOnline(address, record.received_at);
	      }
	    }

	    function machineStatus(address) {
	      const monitor = (state.monitorDetails || []).find(item => Number(item.dnp3_address) === Number(address));
	      if (monitor?.running) return { key: 'running', label: '監控中', kind: 'ok' };
	      if (monitor?.stopped_by_user) return { key: 'unknown', label: '未知', kind: 'warn' };
	      if (monitor && monitor.returncode !== null && monitor.returncode !== undefined && monitor.returncode !== 0) {
	        return { key: 'down', label: '離線', kind: 'err' };
	      }
	      return { key: 'unknown', label: '未知', kind: 'warn' };
	    }

	    function summaryPoint(snapshot, index) {
	      return snapshot?.points?.[index] || null;
	    }

	    function pointValueText(point) {
	      if (!point) return '';
	      const value = point.engineering_value ?? point.raw_value ?? '';
	      const unit = point.unit && point.unit !== '-' ? point.unit : '';
	      return `${value}${unit ? ` ${unit}` : ''}`;
	    }

	    function machineSummary(snapshot) {
	      if (!snapshot || !Object.keys(snapshot.points || {}).length) return '尚無內容';
	      const parts = MACHINE_SUMMARY_POINTS
	        .map(index => {
	          const point = summaryPoint(snapshot, index);
	          return point ? `${point.key || `AI_${index}`}=${pointValueText(point)}` : '';
	        })
	        .filter(Boolean);
	      if (parts.length) return parts.join('；');
	      return Object.values(snapshot.points)
	        .sort((a, b) => Number(a.index) - Number(b.index))
	        .slice(0, 4)
	        .map(point => `${point.key || `AI_${point.index}`}=${pointValueText(point)}`)
	        .join('；') || '尚無內容';
	    }

	    function machineSearchBlob(address, plant, snapshot, summary) {
	      return [
	        address,
	        plant?.loggerId,
	        plant?.plantNo,
	        plant?.plantName,
	        snapshot?.logger_id,
	        summary
	      ].filter(Boolean).join(' ').toLowerCase();
	    }

	    function machineViewModel(address) {
	      const plant = plantForAddress(address);
	      const snapshot = state.machinePoints[String(address)] || null;
	      const logger = snapshot?.logger_id || plant?.loggerId || '-';
	      const hasContent = Boolean(snapshot && Object.keys(snapshot.points || {}).length);
	      const status = machineStatus(address);
	      const isSelected = Number(state.selectedMachineAddress) === Number(address);
	      const isCommandTarget = Number(commandTargetAddress()) === Number(address);
	      const linkTimes = state.machineLinkTimes[String(address)] || null;
	      const summary = machineSummary(snapshot);
	      return { address, plant, snapshot, logger, hasContent, status, isSelected, isCommandTarget, linkTimes, summary };
	    }

	    function machineMatchesFilters(view) {
	      const statusFilter = state.machineStatusFilter || 'all';
	      if (statusFilter === 'data') {
	        if (!view.hasContent) return false;
	      } else if (statusFilter !== 'all' && view.status.key !== statusFilter) {
	        return false;
	      }
	      const query = (state.machineFilterText || '').trim().toLowerCase();
	      if (!query) return true;
	      return machineSearchBlob(view.address, view.plant, view.snapshot, view.summary).includes(query);
	    }

	    function updateMachineStatusCounts(views) {
	      const counts = { running: 0, down: 0, unknown: 0 };
	      for (const view of views || []) {
	        if (Object.prototype.hasOwnProperty.call(counts, view.status.key)) {
	          counts[view.status.key] += 1;
	        }
	      }
	      const text = `狀態 監控中 ${counts.running} / 離線 ${counts.down} / 未知 ${counts.unknown}`;
	      $('machineStatusCounts').textContent = text;
	      $('machineStatusCounts').title = `監控中 ${counts.running} 台，離線 ${counts.down} 台，未知 ${counts.unknown} 台`;
	    }

	    function renderMachineStatus() {
	      const addresses = machineAddressList();
	      const views = addresses.map(machineViewModel);
	      updateMachineStatusCounts(views);
	      const filteredViews = views.filter(machineMatchesFilters);
	      const visibleViews = filteredViews.slice(0, MACHINE_ROW_LIMIT);
	      $('machineCount').textContent = `${addresses.length} 台機器`;
	      if (!addresses.length) {
	        $('machineRows').innerHTML = '<tr><td colspan="9" class="empty">尚無機器資料</td></tr>';
	        $('machineFilterResult').textContent = '';
	        renderMachineDetail();
	        return;
	      }
	      if (state.selectedMachineAddress === null || !addresses.includes(Number(state.selectedMachineAddress))) {
	        const target = commandTargetAddress();
	        state.selectedMachineAddress = target !== null && addresses.includes(target) ? target : addresses[0];
	      }
		      const limitText = filteredViews.length > MACHINE_ROW_LIMIT ? `，先顯示前 ${MACHINE_ROW_LIMIT} 台` : '';
		      $('machineFilterResult').textContent = `符合 ${filteredViews.length} / ${addresses.length} 台${limitText}`;
		      $('machineFilterResult').title = `目前篩選符合 ${filteredViews.length} 台，總計 ${addresses.length} 台`;
		      if (!visibleViews.length) {
			        $('machineRows').innerHTML = '<tr><td colspan="9" class="empty">沒有符合條件的機器</td></tr>';
		        renderMachineDetail();
		        return;
		      }
		      $('machineRows').innerHTML = visibleViews.map(view => {
			        const { address, snapshot, logger, status, isSelected, isCommandTarget, linkTimes, summary } = view;
		        const monitorSelected = state.monitorSelectedAddresses.has(Number(address));
		        return `
		          <tr data-machine-address="${escapeHtml(address)}" class="${isSelected ? 'machine-selected-row' : ''}">
			            <td><input class="table-checkbox monitor-dnp3-address" type="checkbox" data-address="${escapeHtml(address)}"${monitorSelected ? ' checked' : ''}${state.monitor ? ' disabled' : ''}></td>
			            <td><span class="state-pill ${escapeHtml(status.kind)}">${escapeHtml(status.label)}</span></td>
			            <td class="mono timestamp-cell" title="${escapeHtml(linkTimes?.online_at || '')}">${escapeHtml(timestampText(linkTimes?.online_at))}</td>
			            <td class="mono timestamp-cell" title="${escapeHtml(linkTimes?.offline_at || '')}">${escapeHtml(timestampText(linkTimes?.offline_at))}</td>
			            <td class="mono">${escapeHtml(logger || '-')}</td>
		            <td class="mono">${escapeHtml(address)}</td>
	            <td class="mono timestamp-cell" title="${escapeHtml(snapshot?.last_received_at || '')}">${escapeHtml(snapshot?.last_received_at || '-')}</td>
	            <td title="${escapeHtml(summary)}">${escapeHtml(summary)}</td>
		            <td><div class="machine-actions-cell">
		              <button class="small inspect-machine ${isSelected ? 'current' : ''}" data-address="${escapeHtml(address)}">${isSelected ? '查看中' : '查看內容'}</button>
		              <button class="small operate-machine ${isCommandTarget ? 'current' : ''}" data-address="${escapeHtml(address)}">${isCommandTarget ? 'AO 目標' : '設為 AO 目標'}</button>
		            </div></td>
	          </tr>
	        `;
		      }).join('');
		      wireMachineButtons();
		      wireMonitorAddressInputs();
		      renderMachineDetail();
		    }

	    function renderMachineDetail() {
	      const address = Number(state.selectedMachineAddress);
	      if (!Number.isFinite(address)) {
	        $('machineDetailTitle').textContent = '機器內容';
	        $('machineDetailCount').textContent = '0 筆點位';
	        $('machineDetailRows').innerHTML = '<tr><td colspan="8" class="empty">請選擇一台機器查看內容</td></tr>';
	        return;
	      }
	      const plant = plantForAddress(address);
	      const snapshot = state.machinePoints[String(address)] || null;
	      const logger = snapshot?.logger_id || plant?.loggerId || '-';
	      const points = Object.values(snapshot?.points || {}).sort((a, b) => Number(a.index) - Number(b.index));
	      $('machineDetailTitle').textContent = `機器內容：${logger || '-'} / DNP3 ${address}`;
	      $('machineDetailCount').textContent = `${points.length} 筆點位`;
	      if (!points.length) {
	        $('machineDetailRows').innerHTML = '<tr><td colspan="8" class="empty">尚未收到這台機器的 AI 內容</td></tr>';
	        return;
	      }
	      $('machineDetailRows').innerHTML = points.map(point => `
	        <tr>
	          <td class="mono">${escapeHtml(point.key || `AI_${point.index}`)}</td>
		          <td class="name-cell" title="${escapeHtml(point.name || '')}">${escapeHtml(point.name || '')}</td>
	          <td class="mono">${escapeHtml(String(point.raw_value ?? ''))}</td>
	          <td class="mono">${escapeHtml(String(point.engineering_value ?? point.raw_value ?? ''))}</td>
	          <td>${escapeHtml(point.unit || '')}</td>
	          <td class="mono">${escapeHtml(point.flags || '')}</td>
			          <td class="mono timestamp-cell">${escapeHtml(timestampText(point.time))}</td>
			          <td class="mono timestamp-cell">${escapeHtml(timestampText(point.received_at))}</td>
	        </tr>
	      `).join('');
	    }

	    function wireMachineButtons() {
	      const rows = $('machineRows');
	      rows.onclick = event => {
	        const inspect = event.target.closest('.inspect-machine');
	        const operate = event.target.closest('.operate-machine');
	        const button = inspect || operate;
	        if (!button) return;
	        const address = Number(button.dataset.address);
	        if (!Number.isFinite(address)) return;
	        state.selectedMachineAddress = address;
	        if (inspect) {
	          renderMachineStatus();
	          setStatus(`已顯示 DNP3 ${address} 內容`, 'ok');
	          $('machineDetailTitle').scrollIntoView({ block: 'nearest' });
	          return;
	        }
	        renderAoTargetOptions(address);
	        renderCommandTarget();
	        setStatus(`目前 AO 目標：DNP3 ${address}`, 'ok');
	        $('aoResult').textContent = `目前 AO 目標已切換為 DNP3 ${address}`;
	      };
	    }

    function endpointSignature() {
      const host = $('host').value.trim();
      const port = Number($('port').value);
      return `${host}:${port}`;
    }

    function endpointReady() {
      return state.endpointStatus === 'ok' && state.endpointSignature === endpointSignature();
    }

    function setEndpointStatus(kind, message) {
      state.endpointStatus = kind;
      state.endpointSignature = kind === 'ok' ? endpointSignature() : '';
      const el = $('endpointResult');
      el.className = `target-summary ${kind === 'ok' ? 'ok' : kind === 'err' ? 'err' : ''}`.trim();
      el.textContent = message;
      updateCommandButtons();
      updateMonitorTargets();
    }

    function resetEndpointStatus() {
      if (state.endpointStatus === 'unchecked' && !state.endpointSignature) return;
      setEndpointStatus('unchecked', 'TCP 端點狀態：未檢查');
    }

    function updateEndpointControls() {
      $('checkEndpoint').disabled = state.busy || state.monitor;
    }

	    function setBusy(busy) {
	      state.busy = busy;
      updateCommandButtons();
      updateMonitorTargets();
      if (busy) setStatus('執行中');
    }

	    function setMonitorRunning(running) {
	      state.monitor = running;
	      if (running) {
	        state.endpointStatus = 'ok';
	        state.endpointSignature = endpointSignature();
	        $('endpointResult').className = 'target-summary ok';
	        $('endpointResult').textContent = `TCP 端點狀態：監控中（${endpointSignature()}）`;
	      }
	      $('startMonitor').disabled = running || state.busy;
	      $('stopMonitor').disabled = !running;
	      updateCommandButtons();
	      document.querySelectorAll('.monitor-dnp3-address').forEach(input => { input.disabled = running; });
	      updateMonitorTargets();
	      updateHeaderMonitor();
	      renderMachineStatus();
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
      if (formatted) appendConsoleText('監控', formatted);
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
        return `RX ${prefix}${kind} AI_${index} ${name}: 值=${value}${unit} raw=${raw} flags=${flags} 時間=${timestamp}`;
      }

      if (text.startsWith('[tx]')) {
        return formatMonitorTx(text.slice(4).trim());
      }

      const connected = text.match(/^\[connected\]\s+master=(\d+)\s+outstation=(\d+)\s+(.+)$/);
      if (connected) {
        return `LINK 已連線 master=${connected[1]} outstation=${connected[2]} ${connected[3]}`;
      }

      const task = text.match(/^\[task\]\s+(.+)$/);
      if (task) return `TASK ${task[1]}`;

      if (text === '[monitor-ui] started persistent DNP3 master') return `MONITOR ${prefix}已啟動`.trim();
      if (text === '[monitor-ui] stopping persistent DNP3 master') return `MONITOR ${prefix}停止中`.trim();
      if (text === '[monitor-ui] monitor is not running') return 'MONITOR 未執行';
      if (text.startsWith('[monitor-ui] queued AO_')) return `MONITOR ${prefix}${text.replace('[monitor-ui] queued ', '排入 ')}`.trim();
      if (text.startsWith('[ui-command]')) return `MONITOR ${prefix}${formatUiCommandLine(text)}`.trim();
      if (text.startsWith('[monitor-ui] exited')) return `MONITOR ${prefix}${text.replace('[monitor-ui]', '').trim()}`.trim();
      if (text === '[monitor] listening for unsolicited events without polling') return `MONITOR ${prefix}正在接收 unsolicited Class 1/2/3 事件`.trim();
      if (text.startsWith('[monitor] polling')) return `MONITOR ${prefix}${text.replace('[monitor]', '').trim()}`.trim();
      if (text === '[monitor] stopped') return `MONITOR ${prefix}已停止`.trim();
      if (text.startsWith('error:')) return `錯誤 ${text.slice(6).trim()}`;
      return text;
    }

    function formatMonitorTx(jsonText) {
      try {
        const tx = JSON.parse(jsonText);
        const title = tx.operation || tx.function || '訊框';
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

    function formatUiCommandLine(text) {
      try {
        const payload = JSON.parse(text.replace('[ui-command]', '').trim());
        const labels = { started: '命令開始', completed: '命令完成', error: '命令錯誤' };
        const status = labels[payload.status] || payload.status || '命令';
        return `${status}${payload.error ? `：${payload.error}` : ''}`;
      } catch {
        return text;
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
      if (!Number.isFinite(value)) return '事件';
      return (value & 0x80) ? '週期' : '事件';
    }

	    function renderAi(points) {
	      rememberMachinePoints(points);
	      renderMachineStatus();
	      $('pointCount').textContent = `${points.length} 筆值`;
	      if (!points.length) {
	        $('aiRows').innerHTML = '<tr><td colspan="8" class="empty">沒有回傳 AI 值</td></tr>';
        return;
      }
      $('aiRows').innerHTML = points.map(point => `
        <tr>
          <td class="mono">${escapeHtml(point.dnp3_address ?? '')}</td>
          <td class="mono">${escapeHtml(point.logger_id || '')}</td>
          <td class="mono">${point.key}</td>
          <td class="name-cell" title="${escapeHtml(point.name)}">${escapeHtml(point.name)}</td>
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
      $('txCount').textContent = `${transmissions.length} 個訊框`;
      if (!transmissions.length) {
        $('txRows').innerHTML = '<tr><td colspan="7" class="empty">沒有回傳傳輸紀錄</td></tr>';
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
	      rememberMachinePoints(events);
	      renderMachineStatus();
	      $('eventCount').textContent = `${events.length} 筆事件`;
	      if (!events.length) {
	        $('eventRows').innerHTML = '<tr><td colspan="12" class="empty">尚無 unsolicited/event 資料</td></tr>';
        return;
      }
      $('eventRows').innerHTML = events.slice().reverse().map(event => `
        <tr>
          <td>${escapeHtml(eventSourceText(event.source))}</td>
          <td>${escapeHtml(eventKindText(event.kind))}</td>
          <td class="mono">${escapeHtml(event.dnp3_address ?? '')}</td>
          <td class="mono">${escapeHtml(event.logger_id || '')}</td>
          <td class="mono">${escapeHtml(event.key || '')}</td>
          <td class="name-cell" title="${escapeHtml(event.name || '')}">${escapeHtml(event.name || '')}</td>
          <td class="mono">${escapeHtml(String(event.raw_value))}</td>
          <td class="mono">${escapeHtml(String(event.engineering_value))}</td>
          <td>${escapeHtml(event.unit || '')}</td>
          <td class="mono">${escapeHtml(event.flags || '')}</td>
          <td class="mono timestamp-cell">${escapeHtml(timestampText(event.time))}</td>
          <td class="mono timestamp-cell">${escapeHtml(timestampText(event.received_at))}</td>
        </tr>
      `).join('');
    }

    function eventSourceText(source) {
      const labels = { snapshot: '週期快照', cmd_ack: '命令回饋', event: '事件' };
      return labels[source] || source || '';
    }

    function eventKindText(kind) {
      const labels = { periodic: '週期', event: '事件' };
      return labels[kind] || kind || '';
    }

    function timestampText(value) {
      return value ? String(value) : '-';
    }

    function aoTargetOptions() {
      const options = new Map();
      for (const plant of state.idApiPlants || []) {
        const address = Number(plant.dnp3Address);
        if (!Number.isFinite(address)) continue;
        const logger = plant.loggerId || '-';
        options.set(address, `${logger} / DNP3 ${address}`);
      }
      for (const address of machineAddressList()) {
        const value = Number(address);
        if (Number.isFinite(value) && !options.has(value)) options.set(value, `DNP3 ${value}`);
      }
      for (const address of state.runningAddresses || []) {
        const value = Number(address);
        if (Number.isFinite(value) && !options.has(value)) options.set(value, `DNP3 ${value}`);
      }
      for (const address of Object.keys(state.machinePoints || {})) {
        const value = Number(address);
        if (Number.isFinite(value) && !options.has(value)) options.set(value, `DNP3 ${value}`);
      }
      return [...options.entries()].sort((a, b) => a[0] - b[0]);
    }

    function renderAoTargetOptions(preferredAddress = commandTargetAddress()) {
      const select = $('outstationAddress');
      const selected = Number.isFinite(Number(preferredAddress)) ? Number(preferredAddress) : null;
      const options = aoTargetOptions();
      if (selected !== null && !options.some(([address]) => address === selected)) {
        options.push([selected, `DNP3 ${selected}（未在清單）`]);
        options.sort((a, b) => a[0] - b[0]);
      }
      if (!options.length) {
        const fallback = selected ?? 1;
        options.push([fallback, `DNP3 ${fallback}（未在清單）`]);
      }
      select.innerHTML = options.map(([address, label]) => (
        `<option value="${escapeHtml(address)}">${escapeHtml(label)}</option>`
      )).join('');
      const next = selected !== null ? selected : Number(options[0][0]);
      select.value = String(next);
    }

    function renderIdApi(api, preferredAddress = commandTargetAddress()) {
      const plants = api.plants || [];
      state.idApiPlants = plants;
      syncMonitorSelection(plants);
      renderAoTargetOptions(preferredAddress);
	      renderCommandTarget();
	      updateMonitorTargets();
	    }

    function plantAddressList(plants = state.idApiPlants) {
      const addresses = new Set();
      for (const plant of plants || []) {
        const address = Number(plant.dnp3Address);
        if (Number.isFinite(address)) addresses.add(address);
      }
      return [...addresses].sort((a, b) => a - b);
    }

    function selectableMonitorAddresses() {
      const addresses = new Set(plantAddressList());
      for (const address of state.runningAddresses || []) {
        const value = Number(address);
        if (Number.isFinite(value)) addresses.add(value);
      }
      for (const address of Object.keys(state.machinePoints || {})) {
        const value = Number(address);
        if (Number.isFinite(value)) addresses.add(value);
      }
      return [...addresses].sort((a, b) => a - b);
    }

    function syncMonitorSelection(plants) {
      const configured = plantAddressList(plants);
      const currentKnown = new Set(selectableMonitorAddresses());
      if (!state.monitorSelectionInitialized) {
        state.monitorSelectedAddresses = new Set(configured.length ? configured : [...currentKnown]);
        state.monitorSelectionInitialized = true;
      } else {
        for (const address of configured) {
          if (!state.knownMonitorAddresses.has(address)) state.monitorSelectedAddresses.add(address);
        }
        for (const address of [...state.monitorSelectedAddresses]) {
          if (!currentKnown.has(address)) state.monitorSelectedAddresses.delete(address);
        }
      }
      state.knownMonitorAddresses = currentKnown;
    }

    function wireMonitorAddressInputs() {
      document.querySelectorAll('.monitor-dnp3-address').forEach(input => {
        input.addEventListener('change', () => {
          const address = Number(input.dataset.address);
          if (!Number.isFinite(address)) return;
          if (input.checked) {
            state.monitorSelectedAddresses.add(address);
          } else {
            state.monitorSelectedAddresses.delete(address);
          }
          updateMonitorTargets();
        });
      });
    }

    function selectedMonitorAddresses() {
      return [...state.monitorSelectedAddresses].filter(Number.isFinite).sort((a, b) => a - b);
    }

	    function updateMonitorTargets() {
	      const addresses = selectedMonitorAddresses();
	      setAddressSummary('monitorTargets', '監控目標', addresses);
	      $('startMonitor').disabled = state.monitor || state.busy || !endpointReady() || addresses.length === 0;
	      $('selectAllMonitor').disabled = state.monitor || state.busy || selectableMonitorAddresses().length === 0;
	      $('clearMonitorSelection').disabled = state.monitor || state.busy || addresses.length === 0;
	      updatePollTargets(addresses);
	      updateHeaderMonitor();
	      renderMachineStatus();
	    }

    function selectAllMonitorAddresses() {
      if (state.monitor || state.busy) return;
      state.monitorSelectedAddresses = new Set(selectableMonitorAddresses());
      updateMonitorTargets();
    }

    function clearMonitorSelection() {
      if (state.monitor || state.busy) return;
      state.monitorSelectedAddresses = new Set();
      updateMonitorTargets();
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function endpointFailureText(data) {
      const stderr = String(data?.stderr || '').trim();
      const stdout = String(data?.stdout || '').trim();
      const source = stderr || stdout || `returncode=${data?.returncode ?? '-'}`;
      return source.split(/\r?\n/).slice(-1)[0] || '端點檢查失敗';
    }

    async function checkEndpoint() {
      setBusy(true);
      $('endpointResult').className = 'target-summary';
	      $('endpointResult').textContent = `TCP 端點檢查中：${endpointSignature()}`;
      try {
        const data = await api('/api/endpoint/check', settings());
        appendConsole(`檢查 TCP 端點 ${endpointSignature()}`, data);
        if (data.connected) {
          setEndpointStatus('ok', `TCP 端點狀態：可連線（${endpointSignature()}）`);
          setStatus('端點可連線', 'ok');
        } else {
          setEndpointStatus('err', `TCP 端點狀態：連線失敗（${endpointFailureText(data)}）`);
          setStatus('端點連線失敗', 'err');
        }
      } catch (error) {
        setEndpointStatus('err', `TCP 端點狀態：連線失敗（${error.message}）`);
        setStatus('端點連線失敗', 'err');
      } finally {
        setBusy(false);
      }
    }

    async function readAi() {
      setBusy(true);
      $('pollResult').textContent = '';
      try {
        const data = await api('/api/range', { ...selectedAddressPayload(), start: 0, stop: 32 });
        renderAi(data.points || []);
        renderTransmission(data.transmission || []);
        appendConsole(`讀取 AI_0..AI_32（${pollTargetText(data)}）`, data);
        $('pollResult').textContent = data.returncode === 0 ? `AI 掃描完成：${pollTargetText(data)}` : 'AI 掃描回傳錯誤';
        setStatus(data.returncode === 0 ? '上次掃描正常' : '掃描錯誤', data.returncode === 0 ? 'ok' : 'err');
      } catch (error) {
        $('pollResult').textContent = error.message;
        setStatus('錯誤', 'err');
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
        appendConsole(`掃描事件（${pollTargetText(data)}）`, data);
        $('pollResult').textContent = data.returncode === 0 ? `事件掃描完成：${pollTargetText(data)}` : '事件掃描回傳錯誤';
        setStatus(data.returncode === 0 ? '上次掃描正常' : '掃描錯誤', data.returncode === 0 ? 'ok' : 'err');
      } catch (error) {
        $('pollResult').textContent = error.message;
        setStatus('錯誤', 'err');
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
        appendConsole(`執行 AO_${$('aoPoint').value}`, data);
        const command = data.command_result || null;
        const commandOk = command ? Boolean(command.success) : data.returncode === 0;
        $('aoResult').textContent = aoResultText(data);
        setStatus(commandOk ? '上次 AO 正常' : 'AO 錯誤', commandOk ? 'ok' : 'err');
      } catch (error) {
        $('aoResult').textContent = error.message;
        setStatus('錯誤', 'err');
      } finally {
        setBusy(false);
      }
    }

    function aoResultText(data) {
      const tx = (data.transmission || []).find(item => item.operation === 'analog output command');
      const target = tx ? `DNP3 ${tx.outstation_address} / ${tx.point || `AO_${$('aoPoint').value}`}` : `DNP3 ${$('outstationAddress').value}`;
      const command = data.command_result || null;
      if (command) {
        return command.success
          ? `DNP3 AO 已送出並成功：${target}`
          : `DNP3 AO 已送出，但 Outstation 未回 SUCCESS：${command.summary}`;
      }
      return data.returncode === 0
        ? `DNP3 AO 已送出：${target}`
        : `DNP3 AO 命令回傳錯誤：${target}`;
    }

    function monitorPayload() {
      return selectedAddressPayload();
    }

    function monitorStatusText(data) {
      const addresses = data.running_addresses || [];
      if (!data.running) return '監控已停止';
      return `正在監控 ${addresses.length} 個 DNP3 ID${addresses.length ? `：${addresses.join(', ')}` : ''}`;
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
	        state.monitorDetails = data.monitors || [];
	        rememberMonitorLinkTimes(state.monitorDetails);
	        setMonitorRunning(Boolean(data.running));
        $('monitorResult').textContent = monitorStatusText(data);
        setStatus(data.running ? '監控中' : '就緒', data.running ? 'ok' : 'ok');
        scheduleMonitorPoll();
      } catch (error) {
        $('monitorResult').textContent = error.message;
        setStatus('錯誤', 'err');
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
	        state.monitorDetails = data.monitors || [];
	        rememberMonitorLinkTimes(state.monitorDetails);
	        setMonitorRunning(Boolean(data.running));
        $('monitorResult').textContent = monitorStatusText(data);
        setStatus(data.running ? '監控中' : '就緒', data.running ? 'ok' : 'ok');
      } catch (error) {
        $('monitorResult').textContent = error.message;
        setStatus('錯誤', 'err');
      }
    }

    async function refreshMonitorStatus() {
      try {
        const data = await apiGet(`/api/monitor/status?since=${state.monitorSeq}`);
        appendMonitorData(data);
        renderEvents(data.events || []);
	        renderTransmission(data.transmission || []);
	        state.runningAddresses = data.running_addresses || [];
	        state.monitorDetails = data.monitors || [];
	        rememberMonitorLinkTimes(state.monitorDetails);
	        setMonitorRunning(Boolean(data.running));
        if (data.running) {
          $('monitorResult').textContent = monitorStatusText(data);
          setStatus('監控中', 'ok');
        } else if (data.returncode !== null) {
          $('monitorResult').textContent = '監控已停止';
          setStatus('就緒', 'ok');
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
      renderIdApi(data.id_api || {}, Number(data.defaults.outstation_address));
      $('aoPoint').innerHTML = data.points.ao.map(point => {
        const tag = point.reserved ? ' 保留' : '';
        return `<option value="${point.index}">AO_${point.index} [${escapeHtml(point.type)}] - ${escapeHtml(point.name)}${tag}</option>`;
      }).join('');
      if (data.points.ao.some(point => point.index === 1)) {
        $('aoPoint').value = '1';
      }
      setEndpointStatus('unchecked', 'TCP 端點狀態：未檢查');
      setStatus('就緒', 'ok');
    }

	    $('checkEndpoint').addEventListener('click', checkEndpoint);
	    $('readAi').addEventListener('click', readAi);
	    $('scanEvents').addEventListener('click', scanEvents);
	    $('sendAo').addEventListener('click', sendAo);
	    $('selectAllMonitor').addEventListener('click', selectAllMonitorAddresses);
	    $('clearMonitorSelection').addEventListener('click', clearMonitorSelection);
		    $('startMonitor').addEventListener('click', startMonitor);
	    $('stopMonitor').addEventListener('click', stopMonitor);
	    $('outstationAddress').addEventListener('change', renderCommandTarget);
	    $('host').addEventListener('input', resetEndpointStatus);
	    $('port').addEventListener('input', resetEndpointStatus);
	    $('machineSearch').addEventListener('input', () => {
	      state.machineFilterText = $('machineSearch').value;
	      renderMachineStatus();
	    });
	    $('machineStatusFilter').addEventListener('change', () => {
	      state.machineStatusFilter = $('machineStatusFilter').value;
	      renderMachineStatus();
	    });
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
