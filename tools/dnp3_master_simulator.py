#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    from dreams_outstation.points import AO_POINTS
except Exception:
    AO_POINTS = {}

try:
    from pydnp3 import asiodnp3, asiopal, opendnp3, openpal
except ImportError as exc:
    raise SystemExit(
        "pydnp3 is required. Use the DNP3 virtualenv, for example:\n"
        "  DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib "
        ".venv-dnp3-py310/bin/python tools/dnp3_master_simulator.py --help"
    ) from exc


AO_VARIATIONS = {
    "int16": {
        "object": 41,
        "variation": 2,
        "description": "16-bit Analog Output Block",
    },
    "int32": {
        "object": 41,
        "variation": 1,
        "description": "32-bit Analog Output Block",
    },
    "float32": {
        "object": 41,
        "variation": 3,
        "description": "Single-precision Analog Output Block",
    },
    "double64": {
        "object": 41,
        "variation": 4,
        "description": "Double-precision Analog Output Block",
    },
}

AO_MODES = {
    "direct": {
        "function_codes": [5],
        "function": "DIRECT_OPERATE",
        "description": "Direct Operate",
        "expects_response": True,
        "supported": True,
    },
    "sbo": {
        "function_codes": [3, 4],
        "function": "SELECT_AND_OPERATE",
        "description": "Select / Operate",
        "expects_response": True,
        "supported": True,
    },
    "direct-no-ack": {
        "function_codes": [6],
        "function": "DIRECT_OPERATE_NR",
        "description": "Direct Operate No ACK",
        "expects_response": False,
        "supported": False,
    },
}

TASK_CALLBACK_REFS: list[Any] = []
TASK_CALLBACK_REFS_LOCK = threading.Lock()


def _print_tx(payload: dict[str, Any]) -> None:
    print(f"[tx] {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}", flush=True)


def _link_payload(args: argparse.Namespace | None) -> dict[str, Any]:
    if args is None:
        return {}
    return {
        "transport": "tcp",
        "host": args.host,
        "port": args.port,
        "master_address": args.master_address,
        "outstation_address": args.outstation_address,
    }


class ChannelListener(asiodnp3.IChannelListener):
    def __init__(self, verbose: bool = False):
        super().__init__()
        self.verbose = verbose
        self.opened = threading.Event()
        self.closed = threading.Event()

    def OnStateChange(self, state: Any) -> None:
        state_text = opendnp3.ChannelStateToString(state)
        if self.verbose:
            print(f"[channel] {state_text}", flush=True)
        if state == opendnp3.ChannelState.OPEN:
            self.opened.set()
            self.closed.clear()
        elif state in (opendnp3.ChannelState.CLOSED, opendnp3.ChannelState.SHUTDOWN):
            self.closed.set()
            if state == opendnp3.ChannelState.SHUTDOWN:
                self.opened.clear()


class TaskWaiter(opendnp3.ITaskCallback):
    def __init__(self, name: str):
        super().__init__()
        self.name = name
        self.done = threading.Event()
        self.result: Any | None = None

    def OnStart(self) -> None:
        print(f"[task] {self.name} started", flush=True)

    def OnComplete(self, result: Any) -> None:
        self.result = result
        print(f"[task] {self.name} completed: {opendnp3.TaskCompletionToString(result)}", flush=True)
        self.done.set()

    def OnDestroyed(self) -> None:
        self.done.set()


def _task_config(name: str) -> tuple[opendnp3.TaskConfig, TaskWaiter]:
    waiter = TaskWaiter(name)
    with TASK_CALLBACK_REFS_LOCK:
        TASK_CALLBACK_REFS.append(waiter)
    return opendnp3.TaskConfig.With(waiter), waiter


def _ao_command(value: float, variation: str) -> Any:
    if variation == "int16":
        return opendnp3.AnalogOutputInt16(int(value))
    if variation == "int32":
        return opendnp3.AnalogOutputInt32(int(value))
    if variation == "float32":
        return opendnp3.AnalogOutputFloat32(float(value))
    if variation == "double64":
        return opendnp3.AnalogOutputDouble64(float(value))
    raise ValueError(f"Unsupported AO variation: {variation}")


def _command_callback(done: threading.Event) -> Any:
    def callback(result: Any) -> None:
        summary = opendnp3.TaskCompletionToString(result.summary)
        try:
            count = result.Count()
            print(f"[command] summary={summary} results={count}", flush=True)
        except Exception:
            print(f"[command] summary={summary}", flush=True)
        finally:
            done.set()

    return callback


def build_master(args: argparse.Namespace) -> tuple[Any, Any, ChannelListener, Any, list[Any]]:
    levels = opendnp3.levels.NORMAL if args.verbose else opendnp3.levels.NOTHING
    manager = asiodnp3.DNP3Manager(1)
    listener = ChannelListener(args.verbose)
    channel = manager.AddTCPClient(
        "dreams-master-sim",
        levels,
        asiopal.ChannelRetry.Default(),
        args.host,
        "0.0.0.0",
        args.port,
        listener,
    )

    stack_config = asiodnp3.MasterStackConfig()
    stack_config.link.LocalAddr = args.master_address
    stack_config.link.RemoteAddr = args.outstation_address
    stack_config.master.responseTimeout = openpal.TimeDuration.Seconds(args.response_timeout)
    if getattr(args, "enable_unsolicited", False):
        stack_config.master.disableUnsolOnStartup = False
        stack_config.master.unsolClassMask = opendnp3.ClassField.AllEventClasses()
        stack_config.master.eventScanOnEventsAvailableClassMask = opendnp3.ClassField.AllEventClasses()
    if not args.startup_integrity:
        stack_config.master.startupIntegrityClassMask = getattr(opendnp3.ClassField, "None")()

    soe = asiodnp3.PrintingSOEHandler.Create()
    application = asiodnp3.DefaultMasterApplication.Create()
    master = channel.AddMaster(
        "dreams-master",
        soe,
        application,
        stack_config,
    )
    master.Enable()
    return manager, master, listener, soe, [listener, soe, application]


def shutdown_manager(manager: Any | None, exit_code: int) -> None:
    if manager is None:
        return

    finished = threading.Event()

    def run_shutdown() -> None:
        try:
            manager.Shutdown()
        finally:
            finished.set()

    thread = threading.Thread(target=run_shutdown, daemon=True)
    thread.start()
    if not finished.wait(1):
        os._exit(exit_code)


def wait_for_open(listener: ChannelListener, timeout: float) -> None:
    if not listener.opened.wait(timeout):
        raise TimeoutError("DNP3 channel did not open before timeout")


def scan(master: Any, classes: str, timeout: float, args: argparse.Namespace | None = None) -> None:
    if classes == "events":
        field = opendnp3.ClassField.AllEventClasses()
        variations = [{"object": 60, "variation": 2}, {"object": 60, "variation": 3}, {"object": 60, "variation": 4}]
    elif classes == "class0":
        field = opendnp3.ClassField(True, False, False, False)
        variations = [{"object": 60, "variation": 1}]
    elif classes == "all":
        field = opendnp3.ClassField.AllClasses()
        variations = [
            {"object": 60, "variation": 1},
            {"object": 60, "variation": 2},
            {"object": 60, "variation": 3},
            {"object": 60, "variation": 4},
        ]
    else:
        raise ValueError(f"Unsupported scan classes: {classes}")

    _print_tx(
        {
            "direction": "master_to_outstation",
            "operation": f"class scan {classes}",
            "function_code": 1,
            "function": "READ",
            "objects": variations,
            "qualifier": "all objects (0x06)",
            **_link_payload(args),
        }
    )
    config, waiter = _task_config(f"scan {classes}")
    master.ScanClasses(field, config)
    waiter.done.wait(timeout)


def scan_range(master: Any, start: int, stop: int, timeout: float, args: argparse.Namespace | None = None) -> None:
    _print_tx(
        {
            "direction": "master_to_outstation",
            "operation": "analog input range",
            "function_code": 1,
            "function": "READ",
            "object": 30,
            "variation": 0,
            "description": "Analog Input, any variation selected by outstation",
            "start": start,
            "stop": stop,
            "qualifier": "start-stop range",
            **_link_payload(args),
        }
    )
    config, waiter = _task_config(f"scan AI range {start}-{stop}")
    master.ScanRange(opendnp3.GroupVariationID(30, 0), start, stop, config)
    waiter.done.wait(timeout)


def set_unsolicited(master: Any, enabled: bool, timeout: float, args: argparse.Namespace | None = None) -> None:
    classes = [
        opendnp3.Header.From(opendnp3.PointClass.Class1),
        opendnp3.Header.From(opendnp3.PointClass.Class2),
        opendnp3.Header.From(opendnp3.PointClass.Class3),
    ]
    _print_tx(
        {
            "direction": "master_to_outstation",
            "operation": "enable unsolicited" if enabled else "disable unsolicited",
            "function_code": 20 if enabled else 21,
            "function": "ENABLE_UNSOLICITED" if enabled else "DISABLE_UNSOLICITED",
            "objects": [
                {"object": 60, "variation": 2},
                {"object": 60, "variation": 3},
                {"object": 60, "variation": 4},
            ],
            "qualifier": "all class event objects",
            **_link_payload(args),
        }
    )
    config, waiter = _task_config("enable unsolicited" if enabled else "disable unsolicited")
    function = opendnp3.FunctionCode.ENABLE_UNSOLICITED if enabled else opendnp3.FunctionCode.DISABLE_UNSOLICITED
    master.PerformFunction("enable-unsolicited" if enabled else "disable-unsolicited", function, classes, config)
    waiter.done.wait(timeout)


def operate_ao(
    master: Any,
    index: int,
    value: float,
    variation: str,
    mode: str,
    timeout: float,
    args: argparse.Namespace | None = None,
) -> None:
    label = AO_POINTS.get(index).name if index in AO_POINTS else ""
    if label:
        print(f"[ao] AO_{index} {label} raw={value}", flush=True)
    else:
        print(f"[ao] AO_{index} raw={value}", flush=True)

    variation_info = AO_VARIATIONS[variation]
    mode_info = AO_MODES[mode]
    _print_tx(
        {
            "direction": "master_to_outstation",
            "operation": "analog output command",
            "mode": mode,
            "function_codes": mode_info["function_codes"],
            "function": mode_info["function"],
            "function_description": mode_info["description"],
            "expects_response": mode_info["expects_response"],
            "object": variation_info["object"],
            "variation": variation_info["variation"],
            "description": variation_info["description"],
            "qualifier": "indexed command request (0x17/0x28)",
            "index": index,
            "point": f"AO_{index}",
            "point_name": label,
            "raw_value": value,
            **_link_payload(args),
        }
    )
    if not mode_info["supported"]:
        raise NotImplementedError(
            "Direct Operate No ACK (function 6) is listed in the DREAMS profile, "
            "but pydnp3 0.1.0 does not expose an AO command API for it."
        )

    done = threading.Event()
    command = _ao_command(value, variation)
    callback = _command_callback(done)
    if mode == "sbo":
        master.SelectAndOperate(command, index, callback)
    else:
        master.DirectOperate(command, index, callback)
    if not done.wait(timeout):
        print("[command] timeout waiting for command result", flush=True)


def _print_ui_command_status(request_id: str, status: str, **fields: Any) -> None:
    payload = {"request_id": request_id, "status": status, **fields}
    print(f"[ui-command] {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}", flush=True)


def _run_ui_command(master: Any, payload: dict[str, Any], args: argparse.Namespace | None = None) -> None:
    request_id = str(payload.get("request_id") or "")
    command = str(payload.get("command") or "")
    _print_ui_command_status(request_id, "started", command=command)
    started = time.time()
    try:
        if command == "ao":
            variation = str(payload.get("variation") or "int16")
            mode = str(payload.get("mode") or "direct")
            if variation not in AO_VARIATIONS:
                raise ValueError(f"Unsupported AO variation: {variation}")
            if mode not in AO_MODES:
                raise ValueError(f"Unsupported AO mode: {mode}")
            operate_ao(
                master,
                int(payload["index"]),
                float(payload["value"]),
                variation,
                mode,
                float(payload.get("wait") or 10),
                args,
            )
        else:
            raise ValueError(f"Unsupported UI command: {command}")
    except Exception as exc:
        _print_ui_command_status(
            request_id,
            "error",
            command=command,
            error=str(exc),
            duration_seconds=round(time.time() - started, 3),
        )
        return
    _print_ui_command_status(
        request_id,
        "completed",
        command=command,
        duration_seconds=round(time.time() - started, 3),
    )


def _start_ui_command_reader(master: Any, args: argparse.Namespace | None = None) -> threading.Event:
    stop = threading.Event()

    def read_commands() -> None:
        for raw in sys.stdin:
            text = raw.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
                if not isinstance(payload, dict):
                    raise ValueError("UI command payload must be an object")
                if payload.get("command") in {"quit", "exit"}:
                    stop.set()
                    return
                _run_ui_command(master, payload, args)
            except Exception as exc:
                print(f"error: {exc}", flush=True)

    threading.Thread(target=read_commands, name="dnp3-master-ui-command-reader", daemon=True).start()
    return stop


def monitor(
    master: Any,
    interval: int,
    duration: int,
    enable_unsolicited: bool,
    poll: bool,
    args: argparse.Namespace | None = None,
) -> None:
    if enable_unsolicited:
        set_unsolicited(master, True, 10, args)
    if poll:
        class0_scan = master.AddClassScan(
            opendnp3.ClassField(True, False, False, False),
            openpal.TimeDuration.Seconds(interval),
        )
        event_scan = master.AddClassScan(
            opendnp3.ClassField.AllEventClasses(),
            openpal.TimeDuration.Seconds(max(1, min(interval, 5))),
        )
        class0_scan.Demand()
        event_scan.Demand()
        print(f"[monitor] polling class0 every {interval}s and events every {max(1, min(interval, 5))}s", flush=True)
    else:
        print("[monitor] listening for unsolicited events without polling", flush=True)
    ui_stop = _start_ui_command_reader(master, args)
    end = None if duration <= 0 else time.time() + duration
    try:
        while not ui_stop.is_set() and (end is None or time.time() < end):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[monitor] stopped", flush=True)


def interactive(master: Any) -> None:
    print("Commands: scan, events, range START STOP, ao INDEX VALUE, sbo INDEX VALUE, noack INDEX VALUE, quit")
    while True:
        try:
            raw = input("dnp3> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not raw:
            continue
        parts = raw.split()
        command = parts[0].lower()
        try:
            if command in {"quit", "exit"}:
                return
            if command == "scan":
                scan(master, "all", 10)
            elif command == "events":
                scan(master, "events", 10)
            elif command == "range" and len(parts) == 3:
                scan_range(master, int(parts[1]), int(parts[2]), 10)
            elif command in {"ao", "sbo", "noack"} and len(parts) == 3:
                mode = {"ao": "direct", "sbo": "sbo", "noack": "direct-no-ack"}[command]
                operate_ao(master, int(parts[1]), float(parts[2]), "int16", mode, 10)
            else:
                print("Unknown command. Try: scan, events, range 0 32, ao 1 50, sbo 1 50, noack 1 50, quit")
        except Exception as exc:
            print(f"error: {exc}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DREAMS DNP3 Master simulator")
    parser.add_argument("--host", default="127.0.0.1", help="Outstation host")
    parser.add_argument("--port", type=int, default=20000, help="Outstation TCP port")
    parser.add_argument("--master-address", type=int, default=1, help="DNP3 master link address")
    parser.add_argument("--outstation-address", type=int, default=1, help="DNP3 outstation link address")
    parser.add_argument("--connect-timeout", type=float, default=10, help="Seconds to wait for TCP/DNP3 open")
    parser.add_argument("--response-timeout", type=int, default=5, help="DNP3 task response timeout seconds")
    parser.add_argument("--verbose", action="store_true", help="Print channel state changes")
    parser.add_argument("--startup-integrity", action="store_true", help="Let the master run its startup integrity scan")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="Open the DNP3 endpoint and exit after the channel is connected")
    subparsers.add_parser("interactive", help="Open a small command prompt")

    scan_parser = subparsers.add_parser("scan", help="Run one class scan")
    scan_parser.add_argument("--classes", choices=["class0", "events", "all"], default="all")
    scan_parser.add_argument("--wait", type=float, default=10)

    range_parser = subparsers.add_parser("range", help="Read analog input range using Group 30")
    range_parser.add_argument("start", type=int)
    range_parser.add_argument("stop", type=int)
    range_parser.add_argument("--wait", type=float, default=10)

    ao_parser = subparsers.add_parser("ao", help="Send an analog output command")
    ao_parser.add_argument("index", type=int, help="AO index")
    ao_parser.add_argument("value", type=float, help="Raw AO value")
    ao_parser.add_argument("--variation", choices=["int16", "int32", "float32", "double64"], default="int16")
    ao_parser.add_argument("--mode", choices=["direct", "sbo", "direct-no-ack"], default="direct")
    ao_parser.add_argument("--sbo", action="store_true", help="Use select-before-operate (legacy alias for --mode sbo)")
    ao_parser.add_argument("--wait", type=float, default=10)

    monitor_parser = subparsers.add_parser("monitor", help="Poll class0/events until stopped")
    monitor_parser.add_argument("--interval", type=int, default=15)
    monitor_parser.add_argument("--duration", type=int, default=0, help="Seconds to run; 0 means forever")
    monitor_parser.add_argument("--enable-unsolicited", action="store_true", help="Enable and accept Class 1/2/3 unsolicited events")
    monitor_parser.add_argument("--poll", action="store_true", help="Also run periodic class0/event scans while monitoring")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    manager = None
    exit_code = 1
    try:
        manager, master, listener, _soe, _refs = build_master(args)
        wait_for_open(listener, args.connect_timeout)
        print(
            f"[connected] master={args.master_address} outstation={args.outstation_address} "
            f"{args.host}:{args.port}",
            flush=True,
        )

        if args.command == "check":
            pass
        elif args.command == "interactive":
            interactive(master)
        elif args.command == "scan":
            scan(master, args.classes, args.wait, args)
        elif args.command == "range":
            scan_range(master, args.start, args.stop, args.wait, args)
        elif args.command == "ao":
            mode = "sbo" if args.sbo else args.mode
            operate_ao(master, args.index, args.value, args.variation, mode, args.wait, args)
        elif args.command == "monitor":
            monitor(master, args.interval, args.duration, args.enable_unsolicited, args.poll, args)
        exit_code = 0
        return exit_code
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exit_code
    finally:
        shutdown_manager(manager, exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
