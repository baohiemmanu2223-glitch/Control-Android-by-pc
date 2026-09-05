"""Latency and FPS measurement primitives for device sessions."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class LatencySample:
    elapsed_ms: float
    changed: bool


@dataclass
class FpsMeter:
    timestamps: list[float] = field(default_factory=list)

    def frame(self, timestamp: float | None = None) -> None:
        self.timestamps.append(time.monotonic() if timestamp is None else timestamp)

    def fps(self) -> float:
        if len(self.timestamps) < 2:
            return 0.0
        elapsed = self.timestamps[-1] - self.timestamps[0]
        return (len(self.timestamps) - 1) / elapsed if elapsed > 0 else 0.0


class LatencyProbe:
    """Measure until a supplied observation changes or timeout expires."""

    def __init__(self, observe: Callable[[], object], equal: Callable[[object, object], bool] | None = None):
        self.observe = observe
        self.equal = equal or (lambda before, after: before == after)

    def measure(self, trigger: Callable[[], object], timeout: float = 2.0, poll_interval: float = 0.05) -> LatencySample:
        before = self.observe()
        started = time.monotonic()
        trigger()
        while time.monotonic() - started < timeout:
            after = self.observe()
            if not self.equal(before, after):
                return LatencySample((time.monotonic() - started) * 1000, True)
            time.sleep(poll_interval)
        return LatencySample((time.monotonic() - started) * 1000, False)
