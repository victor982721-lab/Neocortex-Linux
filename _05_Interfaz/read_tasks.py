"""Asynchronous, cancellable execution for bounded read-only UI requests."""

from __future__ import annotations

from threading import Event

from PySide6.QtCore import (  # type: ignore[import-not-found]
    QObject,
    QRunnable,
    QThreadPool,
    Signal,
    Slot,
)

from .read_client import ReadClient, ReadRequest, present_read_payload


class _ReadTaskSignals(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)
    finished = Signal(int)


class _ReadTask(QRunnable):
    def __init__(
        self,
        request_id: int,
        request: ReadRequest,
        client: ReadClient,
        cancellation: Event,
    ) -> None:
        super().__init__()
        self.request_id = request_id
        self.request = request
        self.client = client
        self.cancellation = cancellation
        self.signals = _ReadTaskSignals()

    @Slot()
    def run(self) -> None:
        try:
            if self.cancellation.is_set():
                self.signals.cancelled.emit(self.request_id)
                return
            payload = self.client.execute(self.request)
            presentation = present_read_payload(self.request, payload)
            if self.cancellation.is_set():
                self.signals.cancelled.emit(self.request_id)
            else:
                self.signals.succeeded.emit(self.request_id, presentation)
        except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            if self.cancellation.is_set():
                self.signals.cancelled.emit(self.request_id)
            else:
                detail = " ".join(str(exc).split())[:800] or type(exc).__name__
                self.signals.failed.emit(self.request_id, detail)
        finally:
            self.signals.finished.emit(self.request_id)


class ReadTaskController(QObject):
    """Run at most one UI read without blocking the Qt event loop."""

    succeeded = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)
    finished = Signal(int)

    def __init__(
        self,
        client: ReadClient,
        parent: QObject | None = None,
        *,
        pool: QThreadPool | None = None,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._pool = pool or QThreadPool.globalInstance()
        self._next_request_id = 1
        self._active: dict[int, Event] = {}
        self._outcomes: dict[int, tuple[str, object]] = {}

    @property
    def is_running(self) -> bool:
        return bool(self._active)

    def start(self, request: ReadRequest) -> int:
        if self._active:
            raise RuntimeError("a read-only UI request is already running")
        request_id = self._next_request_id
        self._next_request_id += 1
        cancellation = Event()
        task = _ReadTask(request_id, request.validated(), self._client, cancellation)
        task.signals.succeeded.connect(self._task_succeeded)
        task.signals.failed.connect(self._task_failed)
        task.signals.cancelled.connect(self._task_cancelled)
        task.signals.finished.connect(self._task_finished)
        self._active[request_id] = cancellation
        self._pool.start(task)
        return request_id

    def cancel(self, request_id: int) -> bool:
        cancellation = self._active.get(request_id)
        if cancellation is None:
            return False
        cancellation.set()
        return True

    @Slot(int, object)
    def _task_succeeded(self, request_id: int, presentation: object) -> None:
        self._outcomes[request_id] = ("succeeded", presentation)

    @Slot(int, str)
    def _task_failed(self, request_id: int, detail: str) -> None:
        self._outcomes[request_id] = ("failed", detail)

    @Slot(int)
    def _task_cancelled(self, request_id: int) -> None:
        self._outcomes[request_id] = ("cancelled", "")

    @Slot(int)
    def _task_finished(self, request_id: int) -> None:
        cancellation = self._active.pop(request_id, None)
        outcome, value = self._outcomes.pop(
            request_id,
            ("failed", "La consulta terminó sin un resultado estructurado."),
        )
        if cancellation is not None and cancellation.is_set():
            outcome = "cancelled"
        if outcome == "succeeded":
            self.succeeded.emit(request_id, value)
        elif outcome == "failed":
            self.failed.emit(request_id, str(value))
        else:
            self.cancelled.emit(request_id)
        self.finished.emit(request_id)


__all__ = ("ReadTaskController",)
