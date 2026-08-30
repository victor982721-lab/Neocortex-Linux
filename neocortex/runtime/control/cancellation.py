"""Cooperative cancellation shared by framework routes and resource waits."""

from __future__ import annotations

import threading
# region [01] Cancellation contract


class CancellationRequested(Exception):
    """Stop current work after leaving persistent state transactionally valid."""


class CancellationToken:
    """Thread-safe one-way cancellation signal with interruptible waiting."""

    def __init__(self):
        self._event = threading.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def checkpoint(self) -> None:
        if self._event.is_set():
            raise CancellationRequested("framework cancellation requested")
# endregion [01]
