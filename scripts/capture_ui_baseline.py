"""Capture baseline screenshots of the current Tkinter UI at agreed widths."""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import ImageGrab

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from adb_scrcpy.gui import DeviceDashboard


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = ctypes.wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError("GetWindowRect failed")
    return rect.left, rect.top, rect.right, rect.bottom


def capture_one(output: Path, width: int, height: int, settle_ms: int, route: str | None = None, theme: str | None = None) -> dict[str, object]:
    app = DeviceDashboard()
    app.geometry(f"{width}x{height}+0+0")
    if theme:
        app.theme_var.set(theme)
    if route:
        app._select_route(route)
    app.attributes("-topmost", True)
    app.update_idletasks()
    app.lift()
    app.focus_force()
    deadline = time.monotonic() + max(0, settle_ms) / 1000
    while time.monotonic() < deadline:
        app.update()
        time.sleep(0.03)
    rect = window_rect(app.winfo_id())
    image = ImageGrab.grab(bbox=rect)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)
    app._on_close()
    return {"requested_width": width, "requested_height": height, "actual_rect": rect, "image": str(output)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture current UI baseline screenshots")
    parser.add_argument("--output", default="artifacts/ui_baseline")
    parser.add_argument("--settle-ms", type=int, default=1200)
    parser.add_argument("--route", choices=("Dashboard", "Devices", "Automation", "Files & APK", "Recorder", "Logs", "Settings"))
    parser.add_argument("--theme", choices=("Light", "Dark"))
    args = parser.parse_args()
    root = Path(args.output)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    captures = []
    for width in (1100, 1280, 1440):
        captures.append(capture_one(root / f"baseline_{width}_{stamp}.png", width, 720, args.settle_ms, args.route, args.theme))
    metadata = {"captured_at_utc": stamp, "source": "current Tkinter UI", "captures": captures}
    (root / f"baseline_{stamp}.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
