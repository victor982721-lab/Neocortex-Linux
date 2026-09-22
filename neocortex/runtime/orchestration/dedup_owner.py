"""Process-local ownership for route-held deduplication writers.

PDF and image routes both use the shared inventory database as a small
fingerprint cache.  Their route workers are independent, but the
``DedupIndex`` lifecycle starts with a read-only schema probe.  If two workers
open the owner at the same time, the first writer can publish a WAL before the
second probe and force a full temporary snapshot of the inventory database.

The lock below serializes the *entire* route-held owner lifetime per physical
database path.  It does not enlarge a SQLite snapshot budget and does not make
a SQLite connection cross-thread; each route still creates and closes its own
connection on its worker thread after acquiring the lease.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from neocortex.runtime.control.cancellation import CancellationRequested


@dataclass(slots=True)
class _OwnerLock:
    lock: threading.Lock
    users: int = 0


_REGISTRY_LOCK = threading.Lock()
_OWNER_LOCKS: dict[str, _OwnerLock] = {}


def _check_cancellation(cancellation_check: Callable[[], object] | None) -> None:
    """Run one owner-wait checkpoint and accept bool-style callbacks too."""

    if cancellation_check is None:
        return
    if cancellation_check():
        raise CancellationRequested("dedup owner wait cancelled")


def _owner_key(path: str | Path) -> str:
    """Return one key for path aliases to the same physical owner."""

    candidate = Path(path).expanduser()
    try:
        metadata = candidate.stat()
    except OSError:
        # The first opener may create the database.  Resolve symlink aliases
        # for that short pre-creation window; existing owners use the inode
        # identity below, which also covers hardlink aliases.
        return f"path:{candidate.resolve(strict=False)}"
    return f"inode:{metadata.st_dev}:{metadata.st_ino}"


@contextmanager
def dedup_owner_lock(
    path: str | Path,
    *,
    cancellation_check: Callable[[], object] | None = None,
) -> Iterator[None]:
    """Exclusively lease one route-owned deduplication database.

    The registry counts waiting threads as users, so an entry cannot be
    removed while another route is queued on the same owner.  Entries for
    independent database paths do not contend and are discarded after the
    final lease, avoiding a process-lifetime path registry.
    """

    if cancellation_check is not None and not callable(cancellation_check):
        raise TypeError("cancellation_check must be callable or None")
    _check_cancellation(cancellation_check)
    key = _owner_key(path)
    with _REGISTRY_LOCK:
        owner = _OWNER_LOCKS.get(key)
        if owner is None:
            owner = _OwnerLock(threading.Lock())
            _OWNER_LOCKS[key] = owner
        owner.users += 1
    acquired = False
    try:
        # ``Lock.acquire()`` without a timeout cannot observe route
        # cancellation.  Poll in a bounded interval so a queued PDF/Image
        # owner never opens SQLite after its run has been cancelled.
        while not acquired:
            _check_cancellation(cancellation_check)
            acquired = owner.lock.acquire(timeout=0.1)
        _check_cancellation(cancellation_check)
        yield
    finally:
        if acquired:
            owner.lock.release()
        with _REGISTRY_LOCK:
            owner.users -= 1
            if owner.users == 0 and _OWNER_LOCKS.get(key) is owner:
                del _OWNER_LOCKS[key]


__all__ = ["dedup_owner_lock"]
