"""Cooperative cancellation shared by framework routes and resource waits."""

from __future__ import annotations

import threading
import time
# region [01] Cancellation contract


class CancellationRequested(Exception):
    """Stop current work after leaving persistent state transactionally valid."""


class CancellationToken:
    """Thread-safe one-way cancellation signal with interruptible waiting."""

    def __init__(self, parent: CancellationToken | None = None):
        self._event = threading.Event()
        self._parent = parent

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set() or (
            self._parent is not None and self._parent.is_cancelled
        )

    def cancel(self) -> None:
        self._event.set()

    def wait(self, timeout: float | None = None) -> bool:
        if self._parent is None:
            return self._event.wait(timeout)
        # A child observes its owner without registering another long-lived
        # callback/thread or letting local cancellation escape to siblings.
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_cancelled:
            remaining = 0.1 if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                return self.is_cancelled
            self._event.wait(min(0.1, remaining))
        return True

    def checkpoint(self) -> None:
        if self.is_cancelled:
            raise CancellationRequested("framework cancellation requested")
# endregion [01]
