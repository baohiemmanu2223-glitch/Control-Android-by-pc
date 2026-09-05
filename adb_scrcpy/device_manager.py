"""ADB device discovery, selection, status classification and readiness checks."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence


VALID_STATES = {"device", "offline", "unauthorized", "no permissions", "unknown"}


class AdbError(RuntimeError):
    """Raised when adb cannot be started or returns a command error."""


class DeviceNotFoundError(AdbError):
    """Raised when a requested serial is not present in adb output."""


class DeviceStateError(AdbError):
    """Raised when a device exists but is not ready for commands."""


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    product: str | None = None
    model: str | None = None
    device: str | None = None
    transport_id: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.state == "device"


class DeviceManager:
    """Small, synchronous wrapper around the adb executable.

    Every operation accepts an explicit serial or uses ``default_serial``;
    this prevents accidental commands being sent to the wrong device.
    """

    def __init__(
        self,
        adb_path: str | Path = "adb",
        default_serial: str | None = None,
        command_timeout: float = 10.0,
    ) -> None:
        self.adb_path = str(adb_path)
        self.default_serial = default_serial
        self.command_timeout = command_timeout

    def _run(self, args: Sequence[str], timeout: float | None = None) -> str:
        command = [self.adb_path, *args]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.command_timeout if timeout is None else timeout,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            raise AdbError(f"Không tìm thấy adb: {self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB timeout sau {exc.timeout}s: {' '.join(command)}") from exc

        output = (result.stdout or "").strip()
        error = (result.stderr or "").strip()
        if result.returncode != 0:
            detail = error or output or f"exit code {result.returncode}"
            raise AdbError(f"ADB command failed: {detail}")
        return output

    @staticmethod
    def _parse_device_line(line: str) -> Device | None:
        fields = line.split()
        if len(fields) < 2 or line.startswith("List of devices"):
            return None

        serial, state = fields[0], fields[1]
        # "no permissions" is represented by two columns in adb output.
        if state == "no" and len(fields) > 2 and fields[2] == "permissions":
            state = "no permissions"
            fields = [serial, state, *fields[3:]]

        metadata: dict[str, str] = {}
        for token in fields[2:]:
            if ":" in token:
                key, value = token.split(":", 1)
                metadata[key] = value

        return Device(
            serial=serial,
            state=state if state in VALID_STATES else "unknown",
            product=metadata.get("product"),
            model=metadata.get("model"),
            device=metadata.get("device"),
            transport_id=metadata.get("transport_id"),
            extra={k: v for k, v in metadata.items() if k not in {"product", "model", "device", "transport_id"}},
        )

    def list_devices(self) -> list[Device]:
        """Return all devices and classify their current ADB state."""
        output = self._run(["devices", "-l"])
        devices: list[Device] = []
        for line in output.splitlines():
            device = self._parse_device_line(line.strip())
            if device:
                devices.append(device)
        return devices

    def reconnect(self) -> str:
        """Restart only the local ADB daemon; the Android device is untouched."""
        self._run(["kill-server"])
        return self._run(["start-server"])

    def stop_server(self) -> str:
        """Stop the local ADB daemon without changing device settings."""
        return self._run(["kill-server"])

    def get(self, serial: str | None = None) -> Device:
        """Resolve one serial and fail with an actionable error otherwise."""
        target = serial or self.default_serial
        devices = self.list_devices()
        if target:
            for device in devices:
                if device.serial == target:
                    return device
            raise DeviceNotFoundError(
                f"Không tìm thấy serial {target}. Thiết bị hiện có: "
                f"{', '.join(d.serial for d in devices) or 'không có'}"
            )

        ready = [device for device in devices if device.ready]
        if len(ready) == 1:
            return ready[0]
        if not devices:
            raise DeviceNotFoundError("Không có thiết bị ADB nào được kết nối")
        if len(ready) > 1:
            raise DeviceStateError("Có nhiều thiết bị sẵn sàng; phải chỉ rõ --serial")
        states = ", ".join(f"{d.serial}={d.state}" for d in devices)
        raise DeviceStateError(f"Không có thiết bị sẵn sàng: {states}")

    def wait_for_device(self, serial: str | None = None, timeout: float = 30.0, poll_interval: float = 0.5) -> Device:
        """Wait until the selected device is present and in state ``device``."""
        target = serial or self.default_serial
        deadline = time.monotonic() + timeout
        last_state = "chưa xuất hiện"

        while time.monotonic() < deadline:
            devices = self.list_devices()
            candidate = next((d for d in devices if target and d.serial == target), None)
            if target is None:
                ready = [d for d in devices if d.ready]
                if len(ready) == 1:
                    return ready[0]
                candidate = ready[0] if len(ready) == 1 else (devices[0] if len(devices) == 1 else None)

            if candidate:
                last_state = candidate.state
                if candidate.ready:
                    return candidate

            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

        label = target or "thiết bị duy nhất"
        raise DeviceStateError(f"Timeout chờ {label} sẵn sàng sau {timeout}s (trạng thái cuối: {last_state})")

    def adb_serial_args(self, serial: str | None = None) -> list[str]:
        """Return the mandatory ``-s SERIAL`` argument for later commands."""
        device = self.get(serial)
        if not device.ready:
            raise DeviceStateError(f"Thiết bị {device.serial} đang ở trạng thái {device.state}")
        return ["-s", device.serial]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kiểm tra thiết bị Android qua ADB")
    parser.add_argument("--adb", default="adb", help="Đường dẫn tới adb.exe")
    parser.add_argument("--serial", help="Serial cần kiểm tra")
    parser.add_argument("--wait", type=float, default=0, metavar="SECONDS", help="Chờ thiết bị sẵn sàng")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    manager = DeviceManager(args.adb, default_serial=args.serial)
    try:
        device = manager.wait_for_device(args.serial, args.wait) if args.wait > 0 else manager.get(args.serial)
    except AdbError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1

    print(json.dumps({"ok": True, "device": asdict(device)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
