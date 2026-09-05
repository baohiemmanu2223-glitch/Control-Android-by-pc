"""Dry-run and emergency-stop guards for operational workflows."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .adb_client import AdbClient, AdbResult


class EmergencyStop(RuntimeError):
    """Raised when an operator has requested an immediate stop."""


@dataclass(frozen=True)
class DryRunResult:
    command: tuple[str, ...]
    skipped: bool = True


class SafetyController:
    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self._stop = threading.Event()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def ensure_running(self) -> None:
        if self.stopped:
            raise EmergencyStop("Đã yêu cầu dừng khẩn cấp")


class SafeAdbClient(AdbClient):
    """AdbClient that blocks mutating input in dry-run or after stop."""

    def __init__(self, *args, safety: SafetyController | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.safety = safety or SafetyController()

    def _mutating(self, command: tuple[str, ...], call) -> AdbResult | DryRunResult:
        self.safety.ensure_running()
        if self.safety.dry_run:
            return DryRunResult(command)
        return call()

    def tap(self, x: int, y: int):
        command = tuple(self._argv(("shell", "input", "tap", str(int(x)), str(int(y)))))
        return self._mutating(command, lambda: super().tap(x, y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300):
        command = tuple(self._argv(("shell", "input", "swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(duration_ms)))))
        return self._mutating(command, lambda: super().swipe(x1, y1, x2, y2, duration_ms))

    def keyevent(self, key: str | int):
        command = tuple(self._argv(("shell", "input", "keyevent", str(key))))
        return self._mutating(command, lambda: super().keyevent(key))

    def text(self, value: str):
        encoded = value.replace(" ", "%s")
        command = tuple(self._argv(("shell", "input", "text", encoded)))
        return self._mutating(command, lambda: super().text(value))
