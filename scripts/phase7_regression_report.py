"""Run a synthetic grid regression and write a small Phase 7 report."""

from __future__ import annotations

import argparse
import json
import sys
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adb_scrcpy.device_manager import Device
from adb_scrcpy.gui import DeviceDashboard


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts/phase7/regression_report.json")
    args = parser.parse_args()
    rows = []
    with patch.object(DeviceDashboard, "_poll_devices"), patch.object(DeviceDashboard, "_poll_grid_thumbnails"):
        window = DeviceDashboard(str(ROOT / "adb_scrcpy" / "config.example.toml"))
        try:
            for count in (1, 4, 8, 16):
                devices = [Device(f"SYNTH-{index:02d}", "device", model="Synthetic") for index in range(count)]
                tracemalloc.start()
                started = time.perf_counter()
                window._update_devices(devices)
                elapsed_ms = (time.perf_counter() - started) * 1000
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                rows.append({"devices": count, "visible": len(window._visible_devices()), "columns": window._grid_columns(count), "render_ms": round(elapsed_ms, 2), "peak_kb": round(peak / 1024, 1)})
        finally:
            window._on_close()
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "kind": "synthetic_ui_regression",
        "note": "Synthetic Tkinter grid data; physical 16-device CPU/RAM remains manual acceptance.",
        "profiles": rows,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
