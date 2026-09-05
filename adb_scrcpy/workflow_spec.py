"""JSON workflow specification loader and adapters for WorkflowRunner."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .recognition import TemplateMatcher, UiAutomatorAdapter
from .recognition_pipeline import RecognitionPipeline
from .workflow import WorkflowContext, WorkflowError, WorkflowStep


MUTATING_ACTIONS = {"tap", "swipe", "keyevent", "text", "launch"}
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key in variables:
                return variables[key]
            if key in os.environ:
                return os.environ[key]
            raise WorkflowError(f"Không có biến workflow hoặc environment: {key}")
        return _VARIABLE.sub(replace, value)
    if isinstance(value, list):
        return [_expand(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, variables) for key, item in value.items()}
    return value


def load_spec(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkflowError(f"Không tìm thấy workflow: {source}") from exc
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"Workflow JSON không hợp lệ: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
        raise WorkflowError("Workflow phải là object JSON có mảng steps")
    variables = {key: str(value) for key, value in raw.get("variables", {}).items()}
    expanded = _expand(raw, variables)
    expanded["_base_dir"] = str(source.parent.resolve())
    return expanded


def has_mutating_actions(spec: dict[str, Any]) -> bool:
    return any(
        step.get("kind") == "action" and isinstance(step.get("action"), dict) and step["action"].get("type") in MUTATING_ACTIONS
        for step in spec["steps"]
    )


def _action(spec: dict[str, Any]):
    action_type = spec.get("type")
    if action_type not in MUTATING_ACTIONS:
        raise WorkflowError(f"action không hỗ trợ: {action_type}")

    def execute(context: WorkflowContext) -> None:
        if context.data.get("dry_run"):
            context.data.setdefault("dry_run_actions", []).append(spec)
            return
        if action_type != "launch" and context.data.get("health_guard") is not None:
            context.data["health_guard"].ensure_safe()
        client = context.client
        artifact_dir = context.artifacts_dir if context.data.get("capture_actions") else None
        action_name = str(spec.get("name") or action_type)
        before = after = None
        if artifact_dir is not None:
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", action_name).strip("._") or action_type
            stamp = f"{time.strftime('%H%M%S')}_{time.time_ns() % 1_000_000_000:09d}"
            before = client.save_screenshot(artifact_dir / f"{safe_name}_{stamp}_before.png")
        if action_type == "tap":
            client.tap(int(spec["x"]), int(spec["y"]))
        elif action_type == "swipe":
            client.swipe(int(spec["x1"]), int(spec["y1"]), int(spec["x2"]), int(spec["y2"]), int(spec.get("duration_ms", 300)))
        elif action_type == "keyevent":
            client.keyevent(str(spec["key"]))
        elif action_type == "text":
            client.text(str(spec["value"]))
        else:
            package = str(spec["package"])
            client.run("shell", "monkey", "-p", package, "1", retries=0)
        if artifact_dir is not None:
            after = client.save_screenshot(artifact_dir / f"{safe_name}_{stamp}_after.png")
        context.data.setdefault("action_artifacts", []).append({"action": action_name, "before": str(before) if before else None, "after": str(after) if after else None})
    return execute


def _condition(spec: dict[str, Any]):
    condition_type = spec.get("type")
    if condition_type == "delay":
        seconds = float(spec.get("seconds", 0))
        if seconds < 0:
            raise WorkflowError("delay.seconds không được âm")
        started: list[float | None] = [None]
        def delayed(_context: WorkflowContext) -> bool:
            if started[0] is None:
                started[0] = time.monotonic()
            return time.monotonic() - started[0] >= seconds
        return delayed
    if condition_type == "shell_equals":
        command = [str(arg) for arg in spec.get("args", [])]
        expected = str(spec["equals"])
        if not command:
            raise WorkflowError("shell_equals yêu cầu args")
        return lambda context: context.client.shell(*command).strip() == expected
    if condition_type == "package_installed":
        package = str(spec["package"])
        return lambda context: bool(context.client.shell("pm", "path", package).strip())
    if condition_type == "template_present":
        relative = Path(str(spec["template"]))
        threshold = float(spec.get("threshold", 0.85))
        return lambda context: RecognitionPipeline(context.client, context.data.get("geometry")).template(
            Path(context.data["workflow_base_dir"]) / relative, threshold
        ).found
    if condition_type == "ui_exists":
        selector = spec.get("selector")
        if not isinstance(selector, dict):
            raise WorkflowError("ui_exists yêu cầu selector object")
        return lambda context: UiAutomatorAdapter(context.client.serial).exists(**selector)
    raise WorkflowError(f"condition không hỗ trợ: {condition_type}")


def build_steps(spec: dict[str, Any]) -> list[WorkflowStep]:
    steps: list[WorkflowStep] = []
    for index, item in enumerate(spec["steps"], start=1):
        if not isinstance(item, dict):
            raise WorkflowError(f"steps[{index}] phải là object")
        kind = item.get("kind")
        name = str(item.get("name") or f"step_{index}")
        common = {
            "name": name,
            "kind": kind,
            "timeout": float(item.get("timeout", 10.0)),
            "retries": int(item.get("retries", 0)),
            "poll_interval": float(item.get("poll_interval", 0.25)),
        }
        if kind == "action":
            steps.append(WorkflowStep(**common, action=_action(item.get("action", {}))))
        elif kind in {"wait", "assert"}:
            steps.append(WorkflowStep(**common, condition=_condition(item.get("condition", {}))))
        elif kind == "screenshot":
            steps.append(WorkflowStep(**common, screenshot_name=item.get("screenshot_name")))
        elif kind == "stop":
            steps.append(WorkflowStep(**common))
        else:
            raise WorkflowError(f"steps[{index}] kind không hỗ trợ: {kind}")
    return steps
