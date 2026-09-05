"""Create a machine-readable inventory for UI migration regression checks."""

from __future__ import annotations

import argparse
import ast
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "adb_scrcpy/gui.py",
    "adb_scrcpy/adb_client.py",
    "adb_scrcpy/device_manager.py",
    "adb_scrcpy/scrcpy_manager.py",
    "adb_scrcpy/workflow.py",
    "adb_scrcpy/workflow_queue.py",
    "adb_scrcpy/workflow_spec.py",
    "adb_scrcpy/recorder.py",
    "adb_scrcpy/device_health.py",
    "adb_scrcpy/geometry.py",
    "adb_scrcpy/safety.py",
)


def module_inventory(path: Path) -> dict[str, object]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    classes: dict[str, list[str]] = {}
    functions: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes[node.name] = [item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
    return {"classes": classes, "functions": functions}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts/ui_baseline/phase0_inventory.json")
    args = parser.parse_args()
    output = ROOT / args.output
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "UI migration Phase 0 baseline; preserve public behavior while moving presentation",
        "modules": {module: module_inventory(ROOT / module) for module in MODULES},
        "artifact_paths": [
            "artifacts/.gui_state.json",
            "artifacts/device_metadata.json",
            "artifacts/<serial>/<run>/result.json",
            "artifacts/<serial>/<run>/queue_result.json",
            "artifacts/<serial>/<run>/recording_*.json",
            "artifacts/<serial>/<run>/failure.png",
            "artifacts/preview/",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Inventory written: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
