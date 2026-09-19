"""Serialized, reversible native BLAS limits shared by every retrieval owner."""

from __future__ import annotations

import sys
import threading
from importlib import import_module
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .global_resources import CoordinatedMemoryGate, ResourceGrant

_NATIVE_LIBRARY_LOCK = threading.RLock()
_CONTROLLER: Any = None
_LOADED_NATIVE_MODULES: tuple[str, ...] | None = None


class NativeLibraryControlUnavailable(RuntimeError):
    """The optional numerical path cannot enforce its native execution grant."""


def _controller():
    global _CONTROLLER, _LOADED_NATIVE_MODULES
    try:
        controller_type = import_module("threadpoolctl").ThreadpoolController
    except ImportError as exc:
        raise NativeLibraryControlUnavailable("native BLAS control requires threadpoolctl") from exc
    # A later NumPy/ORT extension import can load another native runtime.  A
    # cached controller from before that import must not silently omit it.
    native = tuple(sorted({
        path for module in tuple(sys.modules.values())
        if isinstance(path := getattr(module, "__file__", None), str)
        and (path.endswith(".so") or ".so." in path)
    }))
    if _CONTROLLER is None or native != _LOADED_NATIVE_MODULES:
        _CONTROLLER = controller_type()
        _LOADED_NATIVE_MODULES = native
    return _CONTROLLER


@contextmanager
def native_library_budget(grant: ResourceGrant) -> Iterator[None]:
    """Apply the lease's BLAS limit and restore it on every exit path.

    BLAS thread controls affect their native library across this process, so
    callers share this lock.  Prefer ``native_library_operation`` when the
    admission can be acquired here, avoiding CPU held while waiting for it.
    """

    while not _NATIVE_LIBRARY_LOCK.acquire(timeout=0.05):
        grant.check_cancellation()
    try:
        threads = grant.native_threads
        if threads < 1:
            raise ValueError("BLAS work requires a positive native-thread grant")
        with _controller().limit(limits=threads, user_api="blas"):
            yield
    finally:
        _NATIVE_LIBRARY_LOCK.release()


@contextmanager
def native_library_operation(
    gate: CoordinatedMemoryGate, estimated_bytes: int, *,
    max_threads: int | None = None, cancellation: Any = None,
) -> Iterator[ResourceGrant]:
    """Wait for the library lock before consuming the shared execution budget."""

    cancellation = cancellation if cancellation is not None else gate.cancellation
    while not _NATIVE_LIBRARY_LOCK.acquire(timeout=0.05):
        gate.coordinator.checkpoint()
        if cancellation is not None:
            cancellation.checkpoint()
    try:
        with gate.native_budget(
            estimated_bytes, max_threads=max_threads,
            phase="vector-score", cancellation=cancellation,
        ) as grant:
            with native_library_budget(grant):
                yield grant
    finally:
        _NATIVE_LIBRARY_LOCK.release()
