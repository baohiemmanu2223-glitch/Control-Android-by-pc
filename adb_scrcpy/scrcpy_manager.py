"""Lifecycle management for scrcpy sessions bound to explicit ADB serials."""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class ScrcpyError(RuntimeError):
    """Base error for scrcpy lifecycle and configuration failures."""


class ScrcpyAlreadyRunningError(ScrcpyError):
    """Raised when a second session is requested for the same serial."""


class ScrcpyProcessError(ScrcpyError):
    """Raised when scrcpy exits before a session becomes usable."""


@dataclass(frozen=True)
class ScrcpySession:
    serial: str
    profile: str
    command: tuple[str, ...]
    process: subprocess.Popen
    started_at: float

    @property
    def running(self) -> bool:
        return self.process.poll() is None


PROFILES: dict[str, tuple[str, ...]] = {
    "manual": ("--max-size=1280", "--video-bit-rate=8M", "--max-fps=60"),
    "low-latency": ("--no-audio", "--max-size=1280", "--video-bit-rate=8M", "--max-fps=60"),
    "recording": ("--max-size=1920", "--video-bit-rate=12M", "--max-fps=60"),
}


class ScrcpyManager:
    """Start, inspect and stop at most one scrcpy process per serial."""

    def __init__(
        self,
        scrcpy_path: str | Path = "scrcpy",
        startup_timeout: float = 5.0,
        stop_timeout: float = 3.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self.scrcpy_path = str(scrcpy_path)
        self.startup_timeout = startup_timeout
        self.stop_timeout = stop_timeout
        self.logger = logger or logging.getLogger("adb_scrcpy.scrcpy")
        self._sessions: dict[str, ScrcpySession] = {}

    @staticmethod
    def _validate_serial(serial: str) -> None:
        if not serial or serial.startswith("-") or any(ch.isspace() for ch in serial):
            raise ValueError("serial phải là giá trị không rỗng và không chứa khoảng trắng")

    def build_command(
        self,
        serial: str,
        profile: str = "manual",
        *,
        record_path: str | Path | None = None,
        audio: bool | None = None,
        clipboard_autosync: bool | None = None,
        rotation: int | None = None,
        lock_video_orientation: int | None = None,
        turn_screen_off: bool = False,
        stay_awake: bool = False,
        extra_args: Sequence[str] = (),
    ) -> list[str]:
        """Build an argv list; no shell interpolation is ever performed."""
        self._validate_serial(serial)
        if profile not in PROFILES:
            raise ValueError(f"profile không hợp lệ: {profile}; chọn {', '.join(PROFILES)}")
        if profile == "recording" and record_path is None:
            raise ValueError("profile recording yêu cầu record_path")
        command = [self.scrcpy_path, "-s", serial, *PROFILES[profile]]
        if record_path is not None:
            command += ["--record", str(Path(record_path))]
        if audio is False:
            command.append("--no-audio")
        if clipboard_autosync is False:
            command.append("--no-clipboard-autosync")
        if rotation is not None:
            if rotation not in (0, 1, 2, 3):
                raise ValueError("rotation phải là 0, 1, 2 hoặc 3")
            command.append(f"--rotation={rotation}")
        if lock_video_orientation is not None:
            if lock_video_orientation not in (-1, 0, 1, 2, 3):
                raise ValueError("lock_video_orientation phải là -1 hoặc 0..3")
            command.append(f"--lock-video-orientation={lock_video_orientation}")
        if turn_screen_off:
            command.append("--turn-screen-off")
        if stay_awake:
            command.append("--stay-awake")
        command.extend(str(arg) for arg in extra_args)
        return command

    def start(
        self,
        serial: str,
        profile: str = "manual",
        *,
        record_path: str | Path | None = None,
        audio: bool | None = None,
        clipboard_autosync: bool | None = None,
        rotation: int | None = None,
        lock_video_orientation: int | None = None,
        turn_screen_off: bool = False,
        stay_awake: bool = False,
        extra_args: Sequence[str] = (),
        reuse_existing: bool = True,
    ) -> ScrcpySession:
        command = self.build_command(
            serial,
            profile,
            record_path=record_path,
            audio=audio,
            clipboard_autosync=clipboard_autosync,
            rotation=rotation,
            lock_video_orientation=lock_video_orientation,
            turn_screen_off=turn_screen_off,
            stay_awake=stay_awake,
            extra_args=extra_args,
        )
        existing = self._sessions.get(serial)
        if existing and existing.running:
            if reuse_existing:
                return existing
            raise ScrcpyAlreadyRunningError(f"scrcpy đã chạy cho serial {serial}")
        if existing:
            self._sessions.pop(serial, None)

        started = time.monotonic()
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                creationflags=creationflags,
            )
        except FileNotFoundError as exc:
            raise ScrcpyError(f"Không tìm thấy scrcpy: {self.scrcpy_path}") from exc

        session = ScrcpySession(serial, profile, tuple(command), process, started)
        self._sessions[serial] = session
        deadline = started + self.startup_timeout
        while True:
            if process.poll() is not None:
                self._sessions.pop(serial, None)
                raise ScrcpyProcessError(f"scrcpy thoát sớm cho {serial}, exit code {process.returncode}")
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        self.logger.info("scrcpy started serial=%s profile=%s pid=%s", serial, profile, process.pid)
        return session

    def status(self, serial: str) -> str:
        session = self._sessions.get(serial)
        if not session:
            return "not_started"
        return "running" if session.running else f"exited:{session.process.returncode}"

    def stop(self, serial: str, timeout: float | None = None) -> bool:
        session = self._sessions.pop(serial, None)
        if not session:
            return False
        process = session.process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=self.stop_timeout if timeout is None else timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        self.logger.info("scrcpy stopped serial=%s exit=%s", serial, process.returncode)
        return True

    def stop_all(self) -> None:
        for serial in list(self._sessions):
            self.stop(serial)

    def sessions(self) -> tuple[ScrcpySession, ...]:
        """Return tracked sessions and remove ones that have exited."""
        for serial, session in list(self._sessions.items()):
            if not session.running:
                self._sessions.pop(serial, None)
        return tuple(self._sessions.values())
