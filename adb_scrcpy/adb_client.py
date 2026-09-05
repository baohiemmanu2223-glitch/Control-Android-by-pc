"""Safe, logged ADB command client for one explicitly selected device."""

from __future__ import annotations

import logging
import hashlib
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class AdbResult:
    """Outcome of one ADB invocation."""

    command: tuple[str, ...]
    stdout: str | bytes
    stderr: str
    returncode: int
    elapsed_seconds: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class AdbCommandError(RuntimeError):
    """Raised when an ADB command cannot complete successfully."""

    def __init__(self, result: AdbResult):
        detail = result.stderr or (result.stdout.decode(errors="replace") if isinstance(result.stdout, bytes) else result.stdout)
        super().__init__(f"ADB failed (exit {result.returncode}): {detail.strip() or 'unknown error'}")
        self.result = result


class AdbClient:
    """Run commands for exactly one serial without invoking a shell.

    ``retries`` should only be used for idempotent/read operations. Input
    actions deliberately default to zero retries to avoid duplicate taps.
    """

    _RETRYABLE_MARKERS = (
        "device offline",
        "device unauthorized",
        "no devices/emulators found",
        "cannot connect",
        "closed",
        "transport",
    )

    def __init__(
        self,
        serial: str,
        adb_path: str | Path = "adb",
        command_timeout: float = 10.0,
        logger: logging.Logger | None = None,
    ) -> None:
        if not serial or serial.startswith("-") or any(ch.isspace() for ch in serial):
            raise ValueError("serial phải là giá trị không rỗng và không chứa khoảng trắng")
        self.serial = serial
        self.adb_path = str(adb_path)
        self.command_timeout = command_timeout
        self.logger = logger or logging.getLogger("adb_scrcpy.adb")

    def _argv(self, args: Sequence[str]) -> list[str]:
        return [self.adb_path, "-s", self.serial, *[str(arg) for arg in args]]

    @staticmethod
    def _display_argv(argv: Sequence[str]) -> str:
        # Do not put text typed into the device into logs.
        safe: list[str] = []
        redact_next = False
        for arg in argv:
            if redact_next:
                safe.append("<redacted>")
                redact_next = False
            else:
                safe.append(arg)
                if arg == "text":
                    redact_next = True
        return " ".join(safe)

    @classmethod
    def _is_retryable(cls, result: AdbResult) -> bool:
        if result.returncode == 0:
            return False
        haystack = f"{result.stderr} {result.stdout if isinstance(result.stdout, str) else result.stdout.decode(errors='replace')}".lower()
        return any(marker in haystack for marker in cls._RETRYABLE_MARKERS)

    def run(
        self,
        *args: str,
        timeout: float | None = None,
        retries: int = 0,
        retry_delay: float = 0.4,
        check: bool = True,
        binary: bool = False,
    ) -> AdbResult:
        """Execute one command with bounded retries and structured outcome."""
        if retries < 0:
            raise ValueError("retries không được âm")
        argv = self._argv(args)
        attempts = retries + 1
        last_result: AdbResult | None = None

        for attempt in range(attempts):
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    argv,
                    capture_output=True,
                    text=not binary,
                    encoding=None if binary else "utf-8",
                    errors=None if binary else "replace",
                    timeout=self.command_timeout if timeout is None else timeout,
                    check=False,
                    shell=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except FileNotFoundError as exc:
                raise FileNotFoundError(f"Không tìm thấy adb: {self.adb_path}") from exc
            except subprocess.TimeoutExpired as exc:
                elapsed = time.monotonic() - started
                self.logger.error("ADB timeout command=%s elapsed=%.3fs", self._display_argv(argv), elapsed)
                raise TimeoutError(f"ADB timeout sau {exc.timeout}s: {self._display_argv(argv)}") from exc

            stdout = completed.stdout or (b"" if binary else "")
            stderr = completed.stderr or ""
            elapsed = time.monotonic() - started
            last_result = AdbResult(tuple(argv), stdout, stderr, completed.returncode, elapsed)
            self.logger.info(
                "ADB command=%s rc=%s elapsed=%.3fs attempt=%s/%s",
                self._display_argv(argv), completed.returncode, elapsed, attempt + 1, attempts,
            )

            if completed.returncode == 0 or not self._is_retryable(last_result) or attempt == attempts - 1:
                break
            time.sleep(retry_delay)

        assert last_result is not None
        if check and not last_result.ok:
            raise AdbCommandError(last_result)
        return last_result

    def shell(self, *args: str, retries: int = 2, check: bool = True) -> str:
        """Run a shell command; retries are intended for read-only commands."""
        result = self.run("shell", *args, retries=retries, check=check)
        return str(result.stdout)

    def tap(self, x: int, y: int) -> AdbResult:
        return self.run("shell", "input", "tap", str(int(x)), str(int(y)), retries=0)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> AdbResult:
        if duration_ms < 0:
            raise ValueError("duration_ms không được âm")
        return self.run(
            "shell", "input", "swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(duration_ms)), retries=0
        )

    def keyevent(self, key: str | int) -> AdbResult:
        return self.run("shell", "input", "keyevent", str(key), retries=0)

    def text(self, value: str) -> AdbResult:
        if not isinstance(value, str):
            raise TypeError("value phải là chuỗi")
        # Android input text uses %s for spaces; arguments remain separate.
        encoded = value.replace(" ", "%s")
        return self.run("shell", "input", "text", encoded, retries=0)

    def screencap(self, retries: int = 2) -> bytes:
        result = self.run("exec-out", "screencap", "-p", retries=retries, binary=True)
        assert isinstance(result.stdout, bytes)
        return result.stdout

    def save_screenshot(self, path: str | Path, retries: int = 2) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.screencap(retries=retries))
        return destination

    def push(self, source: str | Path, destination: str, *, check: bool = True) -> AdbResult:
        source_path = Path(source)
        if not source_path.is_file():
            raise FileNotFoundError(f"Không tìm thấy file nguồn: {source_path}")
        if not destination or not destination.startswith("/"):
            raise ValueError("destination Android phải là đường dẫn tuyệt đối")
        return self.run("push", str(source_path), destination, timeout=max(self.command_timeout, 60), retries=0, check=check)

    def pull(self, source: str, destination: str | Path, *, check: bool = True) -> AdbResult:
        if not source or not source.startswith("/"):
            raise ValueError("source Android phải là đường dẫn tuyệt đối")
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        return self.run("pull", source, str(destination_path), timeout=max(self.command_timeout, 60), retries=0, check=check)

    def pull_bytes(self, source: str) -> bytes:
        """Read one shared-storage file for preview without changing the device."""
        if not source or not source.startswith("/"):
            raise ValueError("source Android phải là đường dẫn tuyệt đối")
        result = self.run("exec-out", "cat", source, retries=0, binary=True)
        return result.stdout if isinstance(result.stdout, bytes) else result.stdout.encode()

    @staticmethod
    def sha256_file(path: str | Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def install(self, apk: str | Path, *, replace: bool = True, check: bool = True) -> AdbResult:
        apk_path = Path(apk)
        if apk_path.suffix.lower() != ".apk" or not apk_path.is_file():
            raise FileNotFoundError(f"Không tìm thấy APK hợp lệ: {apk_path}")
        args = ["install"] + (["-r"] if replace else []) + [str(apk_path)]
        return self.run(*args, timeout=max(self.command_timeout, 120), retries=0, check=check)

    def list_files(self, roots: tuple[str, ...] = ("/sdcard/DCIM", "/sdcard/Pictures", "/sdcard/Download", "/sdcard/Movies", "/sdcard/Documents"), max_files: int = 500) -> list[str]:
        """List a bounded set of regular files under common shared-storage folders."""
        if max_files < 1:
            raise ValueError("max_files phải lớn hơn 0")
        files: list[str] = []
        per_root = max(1, max_files // max(1, len(roots)))
        for root in roots:
            result = self.run("shell", "find", root, "-type", "f", "-print", "|", "head", "-n", str(per_root), retries=0, check=False)
            if result.ok:
                files.extend(line.strip() for line in str(result.stdout).splitlines() if line.strip().startswith("/"))
            if len(files) >= max_files:
                break
        return list(dict.fromkeys(files))[:max_files]
