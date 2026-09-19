"""Spawn and supervise bounded Linux worker processes.

NeoCortex targets Linux/Kubuntu. Workers get their own process group and an
optional ``RLIMIT_AS`` ceiling before user/content code starts; termination is
scoped to that group and never relies on a platform-specific compatibility
layer.
"""

from __future__ import annotations

import importlib
import math
import multiprocessing
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

_TERMINATION_GRACE_SECONDS = 0.5


def _process_session_identity(process_id: int) -> tuple[int, int, int] | None:
    """Read group, session and birth ticks without confusing a reused PID."""

    try:
        stat = Path(f"/proc/{process_id}/stat").read_bytes()
    except FileNotFoundError:
        return None
    fields = stat.rpartition(b")")[2].split()
    return int(fields[2]), int(fields[3]), int(fields[19])


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
    session_identity,
) -> None:
    """Create the child session and limits before invoking untrusted work."""

    os.setsid()
    from .worker_priority import configure_worker_priority

    configure_worker_priority(private_session=True)
    process_id = os.getpid()
    identity = _process_session_identity(process_id)
    if identity is None or identity[:2] != (process_id, process_id):
        raise RuntimeError("isolated POSIX process could not establish its own session")
    session_identity[1] = identity[2]
    # Publish PID last: a zero value means no user/content code has started.
    session_identity[0] = process_id
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
    context = multiprocessing.get_context("spawn")
    session_identity = context.RawArray("Q", 2)
    process = context.Process(
        target=_posix_isolated_target,
        args=(target, args, memory_limit_bytes, session_identity),
        daemon=daemon,
    )
    managed_process = cast(Any, process)
    managed_process._neocortex_session_identity = session_identity
    managed_process._neocortex_group_closed = False
    return process


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
    _soft, hard = cast(tuple[int, int], prlimit(process.pid, resource.RLIMIT_AS))
    requested = hard if memory_limit_bytes is None else memory_limit_bytes
    if hard != resource.RLIM_INFINITY and requested > hard:
        raise RuntimeError(
            "posix_memory_containment_unavailable: requested limit exceeds hard RLIMIT_AS"
        )
    prlimit(process.pid, resource.RLIMIT_AS, (requested, hard))


def _signal_owned_group(process_id: int, birth_ticks: int, event: int) -> bool:
    """Signal a wrapper-owned group, refusing an observable replacement leader."""

    identity = _process_session_identity(process_id)
    if identity is not None and identity != (process_id, process_id, birth_ticks):
        raise RuntimeError("refusing to signal a replaced isolated POSIX process group")
    try:
        os.killpg(process_id, event)
    except ProcessLookupError:
        return False
    return True


def terminate_isolated_process(process, timeout_seconds: float = 5.0) -> None:
    """Stop the wrapper-owned session's group within a bounded cleanup window.

    A reaped leader does not imply its group has exited. Check the bootstrap
    identity before group signals and escalate even if SIGTERM stopped only the
    leader. Processes not created by this wrapper never authorize ``killpg``.
    """

    if not math.isfinite(timeout_seconds):
        raise ValueError("isolated process termination timeout must be finite")
    timeout_seconds = max(0.0, timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    terminate_tree = getattr(process, "terminate_tree", None)
    if callable(terminate_tree):
        terminate_tree()
        process.join(timeout=max(0.0, deadline - time.monotonic()))
        return
    session_identity = getattr(process, "_neocortex_session_identity", None)
    if session_identity is None:
        if not process.is_alive():
            return
        raise RuntimeError("POSIX process group is not owned by isolated_spawn_process")
    if process._neocortex_group_closed:
        return
    process_group_id = process.pid
    if process_group_id is None:
        return
    if process_group_id <= 1:
        raise RuntimeError("isolated POSIX process has no safe process group")
    if process_group_id == os.getpgrp():
        raise RuntimeError("refusing to signal the supervisor process group")
    grace = min(timeout_seconds / 2, _TERMINATION_GRACE_SECONDS)
    grace_deadline = min(deadline, time.monotonic() + grace)
    if not session_identity[0]:
        # Cancellation may precede setsid. Never signal the supervisor's group,
        # and re-read readiness in case bootstrap raced the direct-child signal.
        if process.is_alive():
            process.terminate()
        process.join(timeout=max(0.0, grace_deadline - time.monotonic()))
        if not session_identity[0]:
            if process.is_alive():
                process.kill()
            process.join(timeout=max(0.0, deadline - time.monotonic()))
            if process.is_alive():
                raise RuntimeError("isolated POSIX process did not terminate before deadline")
            process._neocortex_group_closed = True
            return
    if session_identity[0] != process_group_id:
        raise RuntimeError("isolated POSIX process bootstrap identity does not match its PID")
    birth_ticks = session_identity[1]
    group_exists = _signal_owned_group(process_group_id, birth_ticks, signal.SIGTERM)
    while group_exists and time.monotonic() < grace_deadline:
        wait = min(0.02, max(0.0, grace_deadline - time.monotonic()))
        if process.is_alive():
            process.join(timeout=wait)
        else:
            time.sleep(wait)
        group_exists = _signal_owned_group(process_group_id, birth_ticks, 0)
    if group_exists:
        _signal_owned_group(process_group_id, birth_ticks, signal.SIGKILL)
    process.join(timeout=max(0.0, deadline - time.monotonic()))
    if process.is_alive():
        raise RuntimeError("isolated POSIX process did not terminate before deadline")
    process._neocortex_group_closed = True


def close_isolated_process(process) -> None:
    """Clean any owned group before releasing a stopped worker's handles.

    Graceful consumers may only join the leader before calling this function.
    Preserve its identity until descendants are signalled, even when the leader
    has already exited normally; never terminate an active or generic process.
    """

    try:
        if process.is_alive():
            return
    except (AttributeError, ValueError):
        return
    if (
        getattr(process, "_neocortex_session_identity", None) is not None
        and not process._neocortex_group_closed
    ):
        # Ownership/cleanup errors must propagate without discarding handles.
        terminate_isolated_process(process)
    try:
        process.close()
    except (AttributeError, ValueError):
        return


__all__ = [
    "close_isolated_process",
    "isolated_spawn_process",
    "set_isolated_process_memory_limit",
    "terminate_isolated_process",
]
