"""Bridge Windows console control events into framework cancellation."""

from __future__ import annotations

import _thread
import ctypes
import os
import threading
from collections.abc import Callable
from typing import Any

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module


# region [01] Console control bridge

CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1

_FAILED_UNREGISTRATIONS: dict[int, tuple[Any, Any]] = {}
_FAILED_UNREGISTRATIONS_LOCK = threading.Lock()


def _retain_failed_unregistration(callback: Any, kernel32: Any) -> None:
    """Keep native handler targets alive when Windows could still call them."""

    with _FAILED_UNREGISTRATIONS_LOCK:
        _FAILED_UNREGISTRATIONS[id(callback)] = (callback, kernel32)


def _release_failed_unregistration(callback: Any) -> None:
    with _FAILED_UNREGISTRATIONS_LOCK:
        _FAILED_UNREGISTRATIONS.pop(id(callback), None)


class ConsoleCancellationBridge:
    """Cancel once and forward every interactive interrupt to the main thread."""

    def __init__(
        self,
        cancel: Callable[[], None],
        *,
        interrupt_main: Callable[[], None] = _thread.interrupt_main,
    ):
        self._cancel = cancel
        self._interrupt_main = interrupt_main
        self._registered = False
        self._callback: Any | None = None
        self._kernel32: Any | None = None
        self._request_lock = threading.Lock()
        self._requested = False

    def handle_event(self, event: int) -> bool:
        """Handle only interactive interruption events; ignore console shutdown."""

        if event not in {CTRL_C_EVENT, CTRL_BREAK_EVENT}:
            return False
        with self._request_lock:
            first_request = not self._requested
            self._requested = True
        if first_request:
            self._cancel()
        # A second Ctrl+C must not be swallowed while the main thread is
        # waiting for cooperative route shutdown. Re-interrupt it so the user
        # retains an explicit escalation path without killing unrelated PIDs.
        self._interrupt_main()
        return True

    def __enter__(self) -> "ConsoleCancellationBridge":
        if self._registered:
            raise RuntimeError("console control handler is still registered")
        with self._request_lock:
            self._requested = False
        if os.name != "nt":
            return self
        handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint32)
        self._callback = handler_type(self.handle_event)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.SetConsoleCtrlHandler.argtypes = [handler_type, ctypes.c_bool]
        self._kernel32.SetConsoleCtrlHandler.restype = ctypes.c_bool
        if not self._kernel32.SetConsoleCtrlHandler(self._callback, True):
            error_code = ctypes.get_last_error()
            self._callback = None
            self._kernel32 = None
            raise OSError(error_code, "SetConsoleCtrlHandler registration failed")
        self._registered = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self._registered:
            self._callback = None
            self._kernel32 = None
            return
        if self._kernel32 is None or self._callback is None:
            raise RuntimeError("registered console handler has no native callback")

        callback = self._callback
        kernel32 = self._kernel32
        try:
            removed = kernel32.SetConsoleCtrlHandler(callback, False)
        except BaseException:
            _retain_failed_unregistration(callback, kernel32)
            raise
        if not removed:
            error_code = ctypes.get_last_error()
            _retain_failed_unregistration(callback, kernel32)
            failure = OSError(
                error_code,
                "SetConsoleCtrlHandler unregistration failed",
            )
            if exc is not None:
                raise failure from exc
            raise failure

        _release_failed_unregistration(callback)
        self._registered = False
        self._callback = None
        self._kernel32 = None


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.console_cancellation")


# endregion [01]
