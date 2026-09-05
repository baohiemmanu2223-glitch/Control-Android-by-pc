"""Console diagnostic bundled with the portable distribution."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from adb_scrcpy.adb_client import AdbClient
from adb_scrcpy.config import RuntimeConfig
from adb_scrcpy.device_manager import DeviceManager


def default_config() -> Path:
    root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return root / "config" / "config.toml"


def main() -> int:
    parser = argparse.ArgumentParser(description="Portable ADB diagnostic")
    parser.add_argument("--config", default=str(default_config()))
    args = parser.parse_args()
    config = RuntimeConfig.from_toml(args.config)
    device = DeviceManager(config.adb_path, default_serial=config.serial).get()
    client = AdbClient(device.serial, adb_path=config.adb_path)
    release = client.shell("getprop", "ro.build.version.release").strip()
    sdk = client.shell("getprop", "ro.build.version.sdk").strip()
    print(f"OK serial={device.serial} model={device.model} android={release} sdk={sdk}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
