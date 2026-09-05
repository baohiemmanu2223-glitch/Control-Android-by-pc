"""Deterministic workflow steps with bounded waits, retries and artifacts."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from .adb_client import AdbClient


StepKind = Literal["action", "wait", "assert", "screenshot", "stop"]
Action = Callable[["WorkflowContext"], Any]
Condition = Callable[["WorkflowContext"], bool]


class WorkflowError(RuntimeError):
    """Raised when a workflow step cannot complete successfully."""


class WorkflowStopped(WorkflowError):
    """Raised internally when a stop step requests a controlled halt."""


@dataclass(frozen=True)
class WorkflowStep:
    name: str
    kind: StepKind
    action: Action | None = None
    condition: Condition | None = None
    timeout: float = 10.0
    retries: int = 0
    poll_interval: float = 0.25
    screenshot_name: str | None = None

    def __post_init__(self) -> None:
        if self.timeout <= 0:
            raise ValueError("timeout phải lớn hơn 0")
        if self.retries < 0:
            raise ValueError("retries không được âm")
        if self.kind == "action" and self.action is None:
            raise ValueError("step action yêu cầu action")
        if self.kind in {"wait", "assert"} and self.condition is None:
            raise ValueError(f"step {self.kind} yêu cầu condition")


@dataclass
class WorkflowContext:
    client: AdbClient
    artifacts_dir: Path | None = None
    data: dict[str, Any] = field(default_factory=dict)
    stop_requested: bool = False
    pause_requested: bool = False


@dataclass(frozen=True)
class StepResult:
    name: str
    kind: StepKind
    status: str
    attempts: int
    elapsed_seconds: float
    artifact: Path | None = None
    error: str | None = None


@dataclass(frozen=True)
class WorkflowResult:
    status: str
    steps: tuple[StepResult, ...]

    @property
    def ok(self) -> bool:
        return self.status in {"passed", "stopped"}


class WorkflowRunner:
    """Execute a sequence of steps and preserve an auditable result."""

    def __init__(
        self,
        context: WorkflowContext,
        logger: Any | None = None,
        on_step_start: Callable[[WorkflowStep, int, int], None] | None = None,
        on_step_result: Callable[[WorkflowStep, StepResult, int, int], None] | None = None,
    ) -> None:
        self.context = context
        self.logger = logger
        self.on_step_start = on_step_start
        self.on_step_result = on_step_result

    def run(self, steps: Iterable[WorkflowStep]) -> WorkflowResult:
        results: list[StepResult] = []
        step_list = list(steps)
        total = len(step_list)
        for index, step in enumerate(step_list, start=1):
            if self.context.stop_requested:
                break
            self._wait_until_resumed()
            if self.context.stop_requested:
                break
            if self.on_step_start:
                self.on_step_start(step, index, total)
            result = self._run_step(step)
            results.append(result)
            if self.on_step_result:
                self.on_step_result(step, result, index, total)
            if result.status == "failed":
                return WorkflowResult("failed", tuple(results))
            if result.status == "stopped":
                return WorkflowResult("stopped", tuple(results))
        return WorkflowResult("passed", tuple(results))

    def _wait_until_resumed(self) -> None:
        while self.context.pause_requested and not self.context.stop_requested:
            time.sleep(0.05)

    def _run_step(self, step: WorkflowStep) -> StepResult:
        started = time.monotonic()
        last_error: str | None = None
        artifact: Path | None = None
        for attempt in range(1, step.retries + 2):
            try:
                artifact = self._execute_once(step)
                status = "stopped" if step.kind == "stop" else "passed"
                return StepResult(step.name, step.kind, status, attempt, time.monotonic() - started, artifact)
            except WorkflowStopped:
                return StepResult(step.name, step.kind, "stopped", attempt, time.monotonic() - started, artifact)
            except Exception as exc:  # step errors are captured for the workflow report
                last_error = str(exc)
                if attempt <= step.retries:
                    time.sleep(min(step.poll_interval, 1.0))
        return StepResult(step.name, step.kind, "failed", step.retries + 1, time.monotonic() - started, artifact, last_error)

    def _execute_once(self, step: WorkflowStep) -> Path | None:
        if step.kind == "action":
            assert step.action is not None
            self._wait_until_resumed()
            if self.context.stop_requested:
                raise WorkflowStopped(f"workflow dừng tại {step.name}")
            started = time.monotonic()
            step.action(self.context)
            if time.monotonic() - started > step.timeout:
                raise WorkflowError(f"action {step.name} vượt timeout {step.timeout}s")
            return None
        if step.kind == "wait":
            assert step.condition is not None
            deadline = time.monotonic() + step.timeout
            while time.monotonic() < deadline:
                if self.context.stop_requested:
                    raise WorkflowStopped(f"workflow dừng tại {step.name}")
                self._wait_until_resumed()
                if step.condition(self.context):
                    return None
                time.sleep(min(step.poll_interval, max(0.0, deadline - time.monotonic())))
            raise WorkflowError(f"wait {step.name} timeout sau {step.timeout}s")
        if step.kind == "assert":
            assert step.condition is not None
            if not step.condition(self.context):
                raise WorkflowError(f"assert {step.name} thất bại")
            return None
        if step.kind == "screenshot":
            if self.context.artifacts_dir is None:
                raise WorkflowError("screenshot yêu cầu context.artifacts_dir")
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", step.screenshot_name or step.name).strip("._") or "screen"
            stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{time.time_ns() % 1_000_000_000:09d}"
            path = self.context.artifacts_dir / f"{safe_name}_{stamp}.png"
            return self.context.client.save_screenshot(path)
        if step.kind == "stop":
            self.context.stop_requested = True
            raise WorkflowStopped(f"workflow dừng tại {step.name}")
        raise WorkflowError(f"step kind không hỗ trợ: {step.kind}")
