"""Spawn and supervise bounded Linux worker processes.

NeoCortex targets Linux/Kubuntu. Workers get their own process group and an
optional ``RLIMIT_AS`` ceiling before user/content code starts; termination is
scoped to that group and never relies on a platform-specific compatibility
layer.
"""

from __future__ import annotations

import importlib
import multiprocessing
import os
import signal
from collections.abc import Callable

_TERMINATION_GRACE_SECONDS = 0.5

def _posix_resource_module():
    try:
        return importlib.import_module("resource")
    except ImportError as exc:  # pragma: no cover - Linux always provides it
        raise RuntimeError(
            "posix_memory_containment_unavailable: resource module is required"
        ) from exc


def _validate_posix_memory_limit(memory_limit_bytes: int | None) -> None:
    if memory_limit_bytes is None:
        return
    resource = _posix_resource_module()
    if not hasattr(resource, "RLIMIT_AS"):
        raise RuntimeError("posix_memory_containment_unavailable: RLIMIT_AS is required")
    _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if hard != resource.RLIM_INFINITY and memory_limit_bytes > hard:
        raise RuntimeError(
            "posix_memory_containment_unavailable: requested limit exceeds hard RLIMIT_AS"
        )


def _posix_isolated_target(
    target: Callable[..., object],
    args: tuple,
    memory_limit_bytes: int | None,
) -> None:
    """Create the child session and limits before invoking untrusted work."""

    os.setsid()
    if memory_limit_bytes is not None:
        resource = _posix_resource_module()
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, hard))
    target(*args)


def isolated_spawn_process(
    *,
    target: Callable[..., object],
    args: tuple,
    daemon: bool = False,
    memory_limit_bytes: int | None = None,
):
    """Return a spawn process in a dedicated POSIX session."""

    if memory_limit_bytes is not None and memory_limit_bytes < 1:
        raise ValueError("isolated process memory limit must be positive")
    _validate_posix_memory_limit(memory_limit_bytes)
    return multiprocessing.get_context("spawn").Process(
        target=_posix_isolated_target,
        args=(target, args, memory_limit_bytes),
        daemon=daemon,
    )


def set_isolated_process_memory_limit(
    process,
    memory_limit_bytes: int | None,
) -> None:
    """Adjust the exact worker ``RLIMIT_AS`` before its next bounded task."""

    if memory_limit_bytes is not None and memory_limit_bytes < 1:
        raise ValueError("isolated process memory limit must be positive")
    _validate_posix_memory_limit(memory_limit_bytes)
    if process.pid is None or not process.is_alive():
        raise RuntimeError("isolated POSIX process is not running")
    resource = _posix_resource_module()
    prlimit = getattr(resource, "prlimit", None)
    if not callable(prlimit):
        raise RuntimeError("posix_memory_containment_unavailable: resource.prlimit is required")
    _soft, hard = prlimit(process.pid, resource.RLIMIT_AS)
    requested = hard if memory_limit_bytes is None else memory_limit_bytes
    if hard != resource.RLIM_INFINITY and requested > hard:
        raise RuntimeError(
            "posix_memory_containment_unavailable: requested limit exceeds hard RLIMIT_AS"
        )
    prlimit(process.pid, resource.RLIMIT_AS, (requested, hard))


def terminate_isolated_process(process, timeout_seconds: float = 5.0) -> None:
    """Terminate only the supplied supervised child and its descendants."""

    if not process.is_alive():
        return
    terminate_tree = getattr(process, "terminate_tree", None)
    if callable(terminate_tree):
        terminate_tree()
        process.join(timeout=max(0.0, timeout_seconds))
        return
    process_group_id = process.pid
    if process_group_id is None or process_group_id <= 1:
        raise RuntimeError("isolated POSIX process has no safe process group")
    if process_group_id == os.getpgrp():
        raise RuntimeError("refusing to signal the supervisor process group")
    grace = min(max(0.0, timeout_seconds), _TERMINATION_GRACE_SECONDS)
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        process.terminate()
    process.join(timeout=grace)
    if process.is_alive():
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            process.kill()
        process.join(timeout=grace)


def close_isolated_process(process) -> None:
    """Release process resources after a supervised worker has stopped."""

    try:
        if not process.is_alive():
            process.close()
    except (AttributeError, ValueError):
        return


__all__ = [
    "close_isolated_process",
    "isolated_spawn_process",
    "set_isolated_process_memory_limit",
    "terminate_isolated_process",
]
