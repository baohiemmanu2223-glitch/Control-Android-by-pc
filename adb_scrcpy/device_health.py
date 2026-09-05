"""Read-only device/app health checks and fail-closed guards."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class DeviceHealthError(RuntimeError):
    """Raised when a device is unsafe for an operation."""


@dataclass(frozen=True)
class HealthReport:
    serial: str
    adb_state: str
    boot_completed: bool | None
    screen_locked: bool | None
    app_running: bool | None
    permission_popup: bool | None
    blockers: tuple[str, ...]

    @property
    def safe(self) -> bool:
        return not self.blockers


class DeviceHealthMonitor:
    """Use only read-only ADB queries to identify unsafe workflow states."""

    def __init__(self, client: Any, package: str | None = None):
        self.client = client
        self.package = package

    def assess(self) -> HealthReport:
        blockers: list[str] = []
        try:
            boot = self.client.shell("getprop", "sys.boot_completed").strip() == "1"
        except Exception:
            boot = None
            blockers.append("adb_unreachable")
        if boot is False:
            blockers.append("boot_incomplete")

        try:
            windows = self.client.shell("dumpsys", "window", "windows")
            policy = self.client.shell("dumpsys", "window", "policy")
            locked = bool(re.search(r"isStatusBarKeyguard=true|mShowingLockscreen=true|mDreamingLockscreen=true", windows + policy, re.I))
            permission_popup = bool(re.search(r"(?:com\.google\.android|com\.android)\.permissioncontroller|permissioncontroller", windows, re.I))
        except Exception:
            locked = None
            permission_popup = None
            blockers.append("screen_state_unknown")
        if locked:
            blockers.append("screen_locked")
        if permission_popup:
            blockers.append("permission_popup")

        running: bool | None = None
        if self.package:
            try:
                pid_output = self.client.shell("pidof", self.package).strip()
                running = bool(pid_output)
            except Exception:
                # Some Android builds do not expose pidof consistently. Use the
                # foreground activity as a read-only fallback before blocking.
                try:
                    activity = self.client.shell("dumpsys", "activity", "activities")
                    running = self.package in activity and bool(re.search(r"(?:mResumedActivity|topResumedActivity)=.*" + re.escape(self.package), activity))
                except Exception:
                    running = None
                    # An intermittent process query must not discard a valid
                    # replay. Definite states (not running/locked/popup) still block.
            if running is False:
                blockers.append("app_not_running")

        return HealthReport(
            serial=self.client.serial,
            adb_state="device" if not any(item in blockers for item in ("adb_unreachable",)) else "unknown",
            boot_completed=boot,
            screen_locked=locked,
            app_running=running,
            permission_popup=permission_popup,
            blockers=tuple(blockers),
        )

    def ensure_safe(self) -> HealthReport:
        report = self.assess()
        if not report.safe:
            raise DeviceHealthError(f"Thiết bị không an toàn: {', '.join(report.blockers)}")
        return report
