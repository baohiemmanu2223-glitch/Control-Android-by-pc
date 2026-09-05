"""Safe mouse recorder for a scrcpy window and workflow JSON serializer."""

from __future__ import annotations

import ctypes
import json
import math
import os
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .geometry import ScreenGeometry


@dataclass(frozen=True)
class MouseGesture:
    kind: str
    x: int
    y: int
    x2: int | None = None
    y2: int | None = None
    duration_ms: int = 0


@dataclass
class RecordedWorkflow:
    """Build a replayable workflow while preserving pauses between events."""

    steps: list[dict[str, Any]] = field(default_factory=list)
    last_event_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def _wait_before(self, now: float | None = None, minimum: float = 0.25) -> None:
        timestamp = time.monotonic() if now is None else now
        if self.last_event_at is not None:
            delay = timestamp - self.last_event_at
            if delay >= minimum:
                self.steps.append({"name": f"wait_{len(self.steps) + 1}", "kind": "wait", "timeout": max(1.0, delay + 1), "condition": {"type": "delay", "seconds": round(delay, 3)}})
        self.last_event_at = timestamp

    def add_gesture(self, gesture: MouseGesture, now: float | None = None) -> None:
        self._wait_before(now)
        action: dict[str, Any]
        if gesture.kind == "tap":
            action = {"type": "tap", "x": gesture.x, "y": gesture.y}
        elif gesture.kind in {"swipe", "long_press"}:
            action = {
                "type": "swipe",
                "x1": gesture.x,
                "y1": gesture.y,
                "x2": gesture.x2 if gesture.x2 is not None else gesture.x,
                "y2": gesture.y2 if gesture.y2 is not None else gesture.y,
                "duration_ms": gesture.duration_ms,
            }
        else:
            raise ValueError(f"gesture không hỗ trợ: {gesture.kind}")
        self.steps.append({"name": f"{gesture.kind}_{len(self.steps) + 1}", "kind": "action", "action": action})

    def add_text(self, value: str, now: float | None = None) -> None:
        if not value:
            raise ValueError("text không được rỗng")
        self._wait_before(now)
        self.steps.append({"name": f"text_{len(self.steps) + 1}", "kind": "action", "action": {"type": "text", "value": value}})

    def add_checkpoint(self, name: str | None = None, now: float | None = None) -> None:
        self._wait_before(now)
        checkpoint = name or f"checkpoint_{len(self.steps) + 1}"
        self.steps.append({"name": checkpoint, "kind": "screenshot", "screenshot_name": checkpoint})

    def save(self, path: str | Path, package: str | None = None) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "package": package,
            "recording": self.metadata,
            "steps": self.steps,
        }
        destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return destination


def classify_gesture(start: tuple[int, int], end: tuple[int, int], duration_ms: int, threshold_px: int = 10) -> MouseGesture:
    distance = math.hypot(end[0] - start[0], end[1] - start[1])
    if distance <= threshold_px and duration_ms < 450:
        return MouseGesture("tap", start[0], start[1])
    if distance <= threshold_px:
        return MouseGesture("long_press", start[0], start[1], duration_ms=duration_ms)
    return MouseGesture("swipe", start[0], start[1], end[0], end[1], duration_ms=max(1, duration_ms))


class MouseRecorder:
    """Poll the foreground scrcpy window; never installs a global keyboard hook."""

    def __init__(self, process_id: int, geometry: ScreenGeometry, on_gesture: Callable[[MouseGesture], None] | None = None) -> None:
        self.process_id = int(process_id)
        self.geometry = geometry
        self.on_gesture = on_gesture
        self.running = False
        self._pressed = False
        self._started_at = 0.0
        self._start_point: tuple[int, int] | None = None

    def start(self) -> None:
        if os.name != "nt":
            raise RuntimeError("MouseRecorder MVP hiện chỉ hỗ trợ Windows")
        self.running = True

    def stop(self) -> None:
        self.running = False
        self._pressed = False
        self._start_point = None

    @staticmethod
    def _win32():
        user32 = ctypes.windll.user32
        return user32

    def _foreground_matches(self) -> bool:
        user32 = self._win32()
        hwnd = user32.GetForegroundWindow()
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        return process_id.value == self.process_id

    def _cursor_device_point(self) -> tuple[int, int] | None:
        user32 = self._win32()
        hwnd = user32.GetForegroundWindow()
        rect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        origin = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(origin))
        client_width, client_height = rect.right - rect.left, rect.bottom - rect.top
        if client_width <= 0 or client_height <= 0:
            return None
        local_x, local_y = point.x - origin.x, point.y - origin.y
        scale = min(client_width / self.geometry.width, client_height / self.geometry.height)
        view_width, view_height = self.geometry.width * scale, self.geometry.height * scale
        offset_x, offset_y = (client_width - view_width) / 2, (client_height - view_height) / 2
        if not (offset_x <= local_x <= offset_x + view_width and offset_y <= local_y <= offset_y + view_height):
            return None
        x = round((local_x - offset_x) / scale)
        y = round((local_y - offset_y) / scale)
        return max(0, min(self.geometry.width, x)), max(0, min(self.geometry.height, y))

    def sample(self) -> MouseGesture | None:
        if not self.running:
            return None
        user32 = self._win32()
        down = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
        point = self._cursor_device_point() if (down or self._pressed) and self._foreground_matches() else None
        now = time.monotonic()
        if down and not self._pressed and point is not None:
            self._pressed = True
            self._started_at = now
            self._start_point = point
            return None
        if not down and self._pressed:
            self._pressed = False
            if self._start_point is None or point is None:
                self._start_point = None
                return None
            duration = round((now - self._started_at) * 1000)
            gesture = classify_gesture(self._start_point, point, duration)
            self._start_point = None
            if self.on_gesture:
                self.on_gesture(gesture)
            return gesture
        return None
