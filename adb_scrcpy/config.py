"""Validated TOML configuration for repeatable local runs."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RuntimeConfig:
    serial: str
    package: str
    adb_path: Path = Path("adb")
    scrcpy_path: Path = Path("scrcpy")
    video_bit_rate: str = "8M"
    max_fps: int = 60
    command_timeout: float = 10.0
    step_timeout: float = 30.0
    artifacts_dir: Path = Path("artifacts")
    retention_days: int = 30
    log_level: str = "INFO"
    allowed_packages: tuple[str, ...] = ()
    capture_actions: bool = True
    device_names: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_toml(cls, path: str | Path) -> "RuntimeConfig":
        source = Path(path)
        try:
            data = tomllib.loads(source.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Không tìm thấy config: {source}") from exc
        device = data.get("device", {})
        tools = data.get("tools", {})
        video = data.get("video", {})
        runtime = data.get("runtime", {})
        safety = data.get("safety", {})
        names = data.get("device_names", {})
        def resolve_path(value: str) -> Path:
            candidate = Path(value)
            return candidate if candidate.is_absolute() else (source.parent / candidate).resolve()
        config = cls(
            serial=str(device.get("serial", "")).strip(),
            package=str(device.get("package", "")).strip(),
            adb_path=resolve_path(str(tools.get("adb_path", "adb"))),
            scrcpy_path=resolve_path(str(tools.get("scrcpy_path", "scrcpy"))),
            video_bit_rate=str(video.get("bit_rate", "8M")),
            max_fps=int(video.get("max_fps", 60)),
            command_timeout=float(runtime.get("command_timeout", 10.0)),
            step_timeout=float(runtime.get("step_timeout", 30.0)),
            artifacts_dir=resolve_path(str(runtime.get("artifacts_dir", "artifacts"))),
            retention_days=int(runtime.get("retention_days", 30)),
            log_level=str(runtime.get("log_level", "INFO")).upper(),
            allowed_packages=tuple(str(item) for item in safety.get("allowed_packages", [])),
            capture_actions=bool(safety.get("capture_actions", True)),
            device_names={str(key): str(value) for key, value in names.items()},
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.serial or any(ch.isspace() for ch in self.serial) or self.serial.startswith("-"):
            raise ValueError("device.serial phải là serial ADB hợp lệ")
        if not self.package or any(ch.isspace() for ch in self.package):
            raise ValueError("device.package phải là package Android cụ thể")
        if self.max_fps <= 0 or self.max_fps > 240:
            raise ValueError("video.max_fps phải trong khoảng 1..240")
        if self.command_timeout <= 0 or self.step_timeout <= 0:
            raise ValueError("timeout phải lớn hơn 0")
        if self.retention_days <= 0:
            raise ValueError("runtime.retention_days phải lớn hơn 0")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("runtime.log_level không hợp lệ")
        if self.allowed_packages and self.package not in self.allowed_packages:
            raise ValueError("device.package không nằm trong safety.allowed_packages")
