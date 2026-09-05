"""Bounded, observable workflow queue for explicitly selected devices."""

from __future__ import annotations

import concurrent.futures
import threading
from dataclasses import dataclass
from typing import Callable

from .workflow import WorkflowContext


@dataclass
class QueueItem:
    serial: str
    status: str = "queued"
    error: str | None = None
    report: str | None = None


class QueueControl:
    def __init__(self) -> None:
        self._paused = False
        self._stopped = False
        self._contexts: list[WorkflowContext] = []
        self._lock = threading.Lock()

    def register(self, context: WorkflowContext) -> None:
        with self._lock:
            self._contexts.append(context)
            context.pause_requested = self._paused
            context.stop_requested = self._stopped

    def pause(self) -> None:
        with self._lock:
            self._paused = True
            for context in self._contexts:
                context.pause_requested = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False
            for context in self._contexts:
                context.pause_requested = False

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            for context in self._contexts:
                context.stop_requested = True
                context.pause_requested = False

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def paused(self) -> bool:
        return self._paused


class WorkflowQueue:
    """Run one job per serial with max concurrency and status callbacks."""

    def __init__(self, max_concurrency: int = 2) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency phải lớn hơn 0")
        self.max_concurrency = max_concurrency
        self.control = QueueControl()
        self.items: dict[str, QueueItem] = {}

    def run(self, serials: list[str], run_job: Callable[[str, QueueControl], str], on_update: Callable[[QueueItem], None] | None = None) -> None:
        self.items = {serial: QueueItem(serial) for serial in serials}
        def update(item: QueueItem) -> None:
            if on_update:
                on_update(item)
        def worker(serial: str) -> QueueItem:
            item = self.items[serial]
            if self.control.stopped:
                item.status = "stopped"
                update(item)
                return item
            item.status = "running"
            update(item)
            try:
                item.report = run_job(serial, self.control)
                item.status = "stopped" if self.control.stopped else "passed"
            except Exception as exc:
                item.status = "failed"
                item.error = str(exc)
            update(item)
            return item
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_concurrency) as executor:
            futures = [executor.submit(worker, serial) for serial in serials]
            for future in futures:
                future.result()

    def pause(self) -> None:
        self.control.pause()

    def resume(self) -> None:
        self.control.resume()

    def stop(self) -> None:
        self.control.stop()
