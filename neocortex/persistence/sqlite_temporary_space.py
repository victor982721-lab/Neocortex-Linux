"""Resource-backed admission for internal, minimal SQLite projections.

Public detached reads keep their explicit byte ceiling. Integrated writers
may request an automatic finite budget: free storage, tmpfs memory, cgroups
and other in-process reservations bound it, not the size of historical state.
"""

from __future__ import annotations

import shutil
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path


_CONDITION = threading.Condition()
_LIVE: dict[SQLiteTemporarySpace, tuple[int, int, int]] = {}
_WORKING_BYTES = 2 * 1024 * 1024


def sqlite_temporary_directory() -> Path:
    """Match the Unix VFS search, not Python's different TMP/TEMP fallbacks."""
    for raw in (os.environ.get("SQLITE_TMPDIR"), os.environ.get("TMPDIR"),
                "/var/tmp", "/usr/tmp", "/tmp", "."):
        if raw:
            candidate = Path(raw)
            if candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK):
                return candidate.resolve()
    raise OSError("SQLite temporary directory is unavailable")


def _available_memory() -> int | None:
    from neocortex.runtime.control.memory_runtime import posix_physical_memory_snapshot
    return posix_physical_memory_snapshot()[1]


def _memory_backed(path: Path) -> bool:
    """Match the actual mount, including paths containing escaped whitespace."""
    resolved = path.resolve()
    best = (0, False)
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if "-" not in fields:
            continue
        raw = fields[4]
        for escaped, decoded in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
            raw = raw.replace(escaped, decoded)
        mount = Path(raw)
        if resolved.is_relative_to(mount) and len(raw) >= best[0]:
            best = (len(raw), fields[fields.index("-") + 1] in {"tmpfs", "ramfs"})
    return best[1]


def _resources(path: Path) -> tuple[int, int | None, bool]:
    return shutil.disk_usage(path).free, _available_memory(), _memory_backed(path)


class SQLiteTemporarySpace:
    """Account actual retention and not-yet-written reservations per filesystem.

    Free space already excludes observed files, so only outstanding writes
    are subtracted again. Initial admission may wait cancelably. A growing
    projection never waits while retaining bytes another projection needs:
    it fails with recovery evidence instead of creating a circular wait.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.device = root.stat().st_dev
        self.observed = 0
        self.reserved = 0
        self.waits = 0
        self.owner_thread = threading.get_ident()
        with _CONDITION:
            _LIVE[self] = (self.device, 0, 0)

    def observe(self, size: int) -> None:
        with _CONDITION:
            self.observed = size
            self.reserved = max(size, self.reserved)
            _LIVE[self] = (self.device, self.observed, self.reserved)
            _CONDITION.notify_all()

    def reserve(
        self, additional: int, *, checkpoint: Callable[[], None],
        deadline: float, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        from .sqlite_read_errors import SQLiteSnapshotBudgetExceeded

        with _CONDITION:
            while True:
                checkpoint()
                free, memory, memory_backed = _resources(self.root)
                pending = sum(max(0, reserve - observed) for key, (dev, observed, reserve) in _LIVE.items()
                              if key is not self and dev == self.device)
                retained = sum(observed for dev, observed, _ in _LIVE.values() if dev == self.device)
                free_after = free - pending - additional
                memory_after = None if memory is None else memory - len(_LIVE) * _WORKING_BYTES
                reason = None
                if free_after < free // 10:
                    reason = "disk_space"
                if memory is not None and memory_after is not None and (
                    memory_after < _WORKING_BYTES
                    or (memory_backed and memory_after - pending - additional < memory // 10)
                ):
                    reason = "memory_pressure"
                if reason is None:
                    self.reserved = self.observed + additional
                    _LIVE[self] = (self.device, self.observed, self.reserved)
                    return
                remaining = deadline - clock()
                others = any(key is not self for key in _LIVE)
                same_thread_holder = any(
                    key is not self and key.owner_thread == self.owner_thread
                    for key in _LIVE
                )
                retained_holder = any(
                    key is not self and observed > 0
                    for key, (_dev, observed, _reserved) in _LIVE.items()
                )
                # A retained parent view may outlive all of its route workers.
                # Even on another thread, waiting for that parent to release
                # would be circular. Only initial external memory pressure or
                # unfinished independent reservations may wait here.
                if (
                    self.observed or same_thread_holder or retained_holder or remaining <= 0
                    or (reason == "disk_space" and not others)
                ):
                    error = SQLiteSnapshotBudgetExceeded(reason)
                    error.add_context(
                        operation="projection", temporary_root=self.root,
                        required_bytes=additional, retained_bytes=retained,
                        pending_bytes=pending, free_bytes=free,
                        available_memory_bytes="unknown" if memory is None else memory,
                        waited=self.waits,
                    )
                    raise error
                self.waits += 1
                _CONDITION.wait(min(0.05, remaining))

    def close(self) -> None:
        with _CONDITION:
            _LIVE.pop(self, None)
            _CONDITION.notify_all()
