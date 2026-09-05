"""Command-line entry point for device discovery and readiness checks."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from .adb_client import AdbClient, AdbCommandError
from .config import RuntimeConfig
from .device_manager import AdbError, Device, DeviceManager
from .scrcpy_manager import ScrcpyError, ScrcpyManager
from .workflow import WorkflowContext, WorkflowError, WorkflowRunner
from .workflow_spec import build_steps, has_mutating_actions, load_spec
from .device_health import DeviceHealthMonitor
from .geometry import GeometryProvider


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DEVICE_ERROR = 3


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--adb", help="Đường dẫn tới adb.exe (ưu tiên hơn config)")
    parser.add_argument("--serial", help="Serial ADB cần chọn")
    parser.add_argument("--config", help="File TOML cấu hình")
    parser.add_argument("--json", action="store_true", help="In JSON thay vì text")


def _input_arguments(parser: argparse.ArgumentParser) -> None:
    _common_arguments(parser)
    parser.add_argument("--confirm", action="store_true", help="Xác nhận gửi thao tác tới thiết bị")
    parser.add_argument("--dry-run", action="store_true", help="Chỉ hiển thị lệnh, không gửi tới thiết bị")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m adb_scrcpy.cli", description="Quản lý thiết bị Android qua ADB")
    subparsers = parser.add_subparsers(dest="command", required=True)
    devices = subparsers.add_parser("devices", help="Liệt kê thiết bị")
    _common_arguments(devices)
    check = subparsers.add_parser("check", help="Kiểm tra một thiết bị sẵn sàng")
    _common_arguments(check)
    check.add_argument("--wait", type=float, default=0.0, metavar="SECONDS", help="Chờ thiết bị sẵn sàng")
    tap = subparsers.add_parser("tap", help="Chạm vào tọa độ")
    _input_arguments(tap)
    tap.add_argument("x", type=int)
    tap.add_argument("y", type=int)
    swipe = subparsers.add_parser("swipe", help="Vuốt giữa hai tọa độ")
    _input_arguments(swipe)
    swipe.add_argument("x1", type=int)
    swipe.add_argument("y1", type=int)
    swipe.add_argument("x2", type=int)
    swipe.add_argument("y2", type=int)
    swipe.add_argument("--duration", type=int, default=300)
    keyevent = subparsers.add_parser("keyevent", help="Gửi Android keyevent")
    _input_arguments(keyevent)
    keyevent.add_argument("key")
    text = subparsers.add_parser("text", help="Nhập text")
    _input_arguments(text)
    text.add_argument("value")
    shell = subparsers.add_parser("shell", help="Chạy lệnh shell đọc trạng thái")
    _common_arguments(shell)
    shell.add_argument("shell_args", nargs=argparse.REMAINDER)
    screenshot = subparsers.add_parser("screenshot", help="Chụp màn hình")
    _common_arguments(screenshot)
    screenshot.add_argument("--out", required=True, help="Đường dẫn file PNG")
    scrcpy = subparsers.add_parser("scrcpy", help="Mở phiên điều khiển scrcpy")
    _common_arguments(scrcpy)
    scrcpy.add_argument("--profile", choices=("manual", "low-latency", "recording"), default="manual")
    scrcpy.add_argument("--record", help="Đường dẫn video khi dùng profile recording")
    scrcpy.add_argument("--no-audio", action="store_true")
    scrcpy.add_argument("--no-clipboard-autosync", action="store_true")
    scrcpy.add_argument("--rotation", type=int, choices=(0, 1, 2, 3))
    scrcpy.add_argument("--lock-video-orientation", type=int, choices=(-1, 0, 1, 2, 3))
    scrcpy.add_argument("--turn-screen-off", action="store_true")
    scrcpy.add_argument("--stay-awake", action="store_true")
    scrcpy.add_argument("--detach", action="store_true", help="Khởi động rồi trả về, không chờ cửa sổ đóng")
    stop = subparsers.add_parser("stop", help="Dừng phiên scrcpy theo serial")
    _common_arguments(stop)
    workflow = subparsers.add_parser("workflow", help="Chạy workflow JSON")
    _input_arguments(workflow)
    workflow.add_argument("workflow_file", help="Đường dẫn file workflow JSON")
    return parser


def _device_json(device: Device) -> dict[str, object]:
    return asdict(device)


def _load_options(args: argparse.Namespace) -> tuple[str, str | None]:
    adb_path = args.adb or "adb"
    serial = args.serial
    if args.config:
        config = RuntimeConfig.from_toml(args.config)
        adb_path = args.adb or str(config.adb_path)
        serial = args.serial or config.serial
    return adb_path, serial


def _print(payload: object, as_json: bool, text: str = "") -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        print(text)


def _run_devices(args: argparse.Namespace) -> int:
    adb_path, _ = _load_options(args)
    manager = DeviceManager(adb_path)
    devices = manager.list_devices()
    if args.json:
        _print({"ok": True, "devices": [_device_json(device) for device in devices]}, True)
    else:
        if devices:
            print("SERIAL\tSTATE\tMODEL\tPRODUCT")
            for device in devices:
                print(f"{device.serial}\t{device.state}\t{device.model or '-'}\t{device.product or '-'}")
        else:
            print("Không có thiết bị ADB nào được kết nối")
    return EXIT_OK


def _run_check(args: argparse.Namespace) -> int:
    adb_path, serial = _load_options(args)
    manager = DeviceManager(adb_path, default_serial=serial)
    device = manager.wait_for_device(serial, args.wait) if args.wait > 0 else manager.get(serial)
    client = AdbClient(device.serial, adb_path=adb_path)
    release = client.shell("getprop", "ro.build.version.release").strip()
    sdk = client.shell("getprop", "ro.build.version.sdk").strip()
    payload = {"ok": True, "device": _device_json(device), "android_version": release, "sdk": sdk}
    text = f"OK: {device.serial} {device.model or ''} Android {release} SDK {sdk}".strip()
    _print(payload, args.json, text)
    return EXIT_OK


def _session_dir(args: argparse.Namespace, serial: str) -> Path:
    root = Path("artifacts")
    if args.config:
        root = RuntimeConfig.from_toml(args.config).artifacts_dir
    session = root / serial / time.strftime("%Y%m%d_%H%M%S")
    session.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=session / "run.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    return session


def _resolve_client(args: argparse.Namespace) -> tuple[AdbClient, DeviceManager]:
    adb_path, serial = _load_options(args)
    manager = DeviceManager(adb_path, default_serial=serial)
    device = manager.get(serial)
    return AdbClient(device.serial, adb_path=adb_path), manager


def _input_guard(args: argparse.Namespace, command: list[str]) -> int | None:
    if args.dry_run:
        _print({"ok": True, "dry_run": True, "command": command}, args.json, "DRY-RUN: " + " ".join(command))
        return EXIT_OK
    if not args.confirm:
        _print({"ok": False, "error": "Thiếu --confirm; dùng --dry-run để xem trước"}, args.json, "Lỗi: cần --confirm (hoặc --dry-run)")
        return EXIT_USAGE
    return None


def _run_input(args: argparse.Namespace) -> int:
    client, _ = _resolve_client(args)
    _session_dir(args, client.serial)
    if args.command == "tap":
        command = client._argv(("shell", "input", "tap", str(args.x), str(args.y)))
        guarded = _input_guard(args, command)
        if guarded is not None:
            return guarded
        result = client.tap(args.x, args.y)
    elif args.command == "swipe":
        command = client._argv(("shell", "input", "swipe", str(args.x1), str(args.y1), str(args.x2), str(args.y2), str(args.duration)))
        guarded = _input_guard(args, command)
        if guarded is not None:
            return guarded
        result = client.swipe(args.x1, args.y1, args.x2, args.y2, args.duration)
    elif args.command == "keyevent":
        command = client._argv(("shell", "input", "keyevent", args.key))
        guarded = _input_guard(args, command)
        if guarded is not None:
            return guarded
        result = client.keyevent(args.key)
    else:
        encoded = args.value.replace(" ", "%s")
        command = client._argv(("shell", "input", "text", encoded))
        guarded = _input_guard(args, command)
        if guarded is not None:
            return guarded
        result = client.text(args.value)
    payload = {"ok": result.ok, "returncode": result.returncode, "elapsed_seconds": result.elapsed_seconds}
    _print(payload, args.json, f"OK: {args.command}")
    return EXIT_OK if result.ok else EXIT_DEVICE_ERROR


def _run_shell(args: argparse.Namespace) -> int:
    client, _ = _resolve_client(args)
    _session_dir(args, client.serial)
    if not args.shell_args:
        raise ValueError("shell yêu cầu ít nhất một argument")
    result = client.run("shell", *args.shell_args, retries=2, check=False)
    payload = {"ok": result.ok, "stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}
    _print(payload, args.json, str(result.stdout).strip())
    return EXIT_OK if result.ok else EXIT_DEVICE_ERROR


def _run_screenshot(args: argparse.Namespace) -> int:
    client, _ = _resolve_client(args)
    session = _session_dir(args, client.serial)
    requested = Path(args.out)
    output = requested if requested.is_absolute() else session / requested
    path = client.save_screenshot(output)
    payload = {"ok": True, "path": str(path), "bytes": path.stat().st_size, "serial": client.serial}
    _print(payload, args.json, f"Saved: {path}")
    return EXIT_OK


def _scrcpy_registry(args: argparse.Namespace, serial: str) -> Path:
    root = Path("artifacts")
    if args.config:
        root = RuntimeConfig.from_toml(args.config).artifacts_dir
    directory = root / serial
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "scrcpy-session.json"


def _run_scrcpy(args: argparse.Namespace) -> int:
    adb_path, serial = _load_options(args)
    manager = DeviceManager(adb_path, default_serial=serial)
    device = manager.get(serial)
    scrcpy_path = RuntimeConfig.from_toml(args.config).scrcpy_path if args.config else "scrcpy"
    controller = ScrcpyManager(scrcpy_path)
    registry = _scrcpy_registry(args, device.serial)
    record_path = args.record
    session = controller.start(
        device.serial,
        args.profile,
        record_path=record_path,
        audio=False if args.no_audio else None,
        clipboard_autosync=False if args.no_clipboard_autosync else None,
        rotation=args.rotation,
        lock_video_orientation=args.lock_video_orientation,
        turn_screen_off=args.turn_screen_off,
        stay_awake=args.stay_awake,
    )
    registry.write_text(json.dumps({"serial": device.serial, "pid": session.process.pid, "command": list(session.command)}), encoding="utf-8")
    payload = {"ok": True, "serial": device.serial, "pid": session.process.pid, "profile": args.profile, "registry": str(registry)}
    if args.detach:
        _print(payload, args.json, f"scrcpy started pid={session.process.pid}")
        return EXIT_OK
    try:
        while session.running:
            time.sleep(0.25)
        if session.process.returncode not in (0, None):
            raise ScrcpyError(f"scrcpy thoát với mã {session.process.returncode}")
    except KeyboardInterrupt:
        controller.stop(device.serial)
        payload["stopped_by"] = "Ctrl+C"
    finally:
        if registry.exists():
            registry.unlink()
    _print(payload, args.json, f"scrcpy stopped serial={device.serial}")
    return EXIT_OK


def _run_stop(args: argparse.Namespace) -> int:
    adb_path, serial = _load_options(args)
    if not serial:
        raise ValueError("stop yêu cầu --serial hoặc config có device.serial")
    registry = _scrcpy_registry(args, serial)
    if not registry.exists():
        _print({"ok": True, "stopped": False, "reason": "not_started", "serial": serial}, args.json, f"Không có phiên scrcpy cho {serial}")
        return EXIT_OK
    info = json.loads(registry.read_text(encoding="utf-8"))
    pid = int(info["pid"])
    # Windows taskkill targets this exact PID and its scrcpy child process only.
    result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, shell=False, check=False)
    registry.unlink(missing_ok=True)
    not_found = "not found" in result.stderr.lower() or "không tìm thấy" in result.stderr.lower()
    stopped = result.returncode == 0 or not_found
    payload = {"ok": stopped, "stopped": stopped, "serial": serial, "pid": pid, "stderr": result.stderr.strip()}
    _print(payload, args.json, f"scrcpy stop serial={serial} rc={result.returncode}")
    return EXIT_OK if stopped else EXIT_DEVICE_ERROR


def _workflow_result_json(result) -> dict[str, object]:
    return {
        "status": result.status,
        "ok": result.ok,
        "steps": [
            {
                "name": step.name,
                "kind": step.kind,
                "status": step.status,
                "attempts": step.attempts,
                "elapsed_seconds": step.elapsed_seconds,
                "artifact": str(step.artifact) if step.artifact else None,
                "error": step.error,
            }
            for step in result.steps
        ],
    }


def _run_workflow(args: argparse.Namespace) -> int:
    spec = load_spec(args.workflow_file)
    if has_mutating_actions(spec) and not args.dry_run and not args.confirm:
        raise ValueError("Workflow có action thay đổi trạng thái; thêm --confirm hoặc --dry-run")
    client, _ = _resolve_client(args)
    session = _session_dir(args, client.serial)
    config = RuntimeConfig.from_toml(args.config) if args.config else None
    context = WorkflowContext(
        client=client,
        artifacts_dir=session,
        data={"dry_run": args.dry_run, "workflow_base_dir": spec["_base_dir"], "capture_actions": bool(config.capture_actions if config else True)},
    )
    try:
        context.data["geometry"] = GeometryProvider(client).read()
    except Exception as exc:
        context.data["geometry_error"] = str(exc)
    if not args.dry_run:
        context.data["health_guard"] = DeviceHealthMonitor(client, config.package if config else None)
    result = WorkflowRunner(context).run(build_steps(spec))
    payload = _workflow_result_json(result)
    payload.update({"serial": client.serial, "dry_run": args.dry_run, "workflow": str(Path(args.workflow_file))})
    if result.status == "failed":
        failure_path = session / "failure.png"
        try:
            payload["failure_screenshot"] = str(client.save_screenshot(failure_path))
        except Exception as exc:
            payload["failure_screenshot_error"] = str(exc)
    report = session / "result.json"
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["report"] = str(report)
    _print(payload, args.json, f"workflow {result.status}: {report}")
    return EXIT_OK if result.ok else EXIT_DEVICE_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "devices":
            return _run_devices(args)
        if args.command == "check":
            return _run_check(args)
        if args.command in {"tap", "swipe", "keyevent", "text"}:
            return _run_input(args)
        if args.command == "shell":
            return _run_shell(args)
        if args.command == "screenshot":
            return _run_screenshot(args)
        if args.command == "scrcpy":
            return _run_scrcpy(args)
        if args.command == "stop":
            return _run_stop(args)
        if args.command == "workflow":
            return _run_workflow(args)
        parser.error(f"command không hỗ trợ: {args.command}")
    except (ValueError, FileNotFoundError, AdbError, AdbCommandError, ScrcpyError, TimeoutError, json.JSONDecodeError, WorkflowError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"Lỗi: {exc}", file=sys.stderr)
        return EXIT_DEVICE_ERROR
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
