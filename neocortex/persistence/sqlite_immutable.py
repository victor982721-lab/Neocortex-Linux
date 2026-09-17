"""Fenced SQLite reads that never modify a published owner.

The public read surfaces must not use SQLite's usual ``mode=ro`` URI directly:
even a read-only connection may create ``-shm``/``-wal`` files, and a close may
checkpoint a writer-owned WAL.  :class:`SQLiteReadSession` centralizes the
two safe read strategies used by the product:

* ``immutable_strict`` reads an already quiescent owner with SQLite's
  ``immutable=1`` flag and a before/after filesystem fence.  In addition to an
  owner with no sidecars, the kernel accepts SQLite's closed-WAL residual
  layout (an empty WAL and 32 KiB SHM) only after a no-byte-mutation WAL lock
  probe proves that no writer/checkpoint/recovery lock is held.  The strict
  session retains OFD shared guards over the owner control locks until close,
  including the sidecar-free rollback layout, so a writer cannot start in the
  gap between the probe and the immutable open.
* ``snapshot_temp`` copies a bounded, stable set of main/sidecar bytes into a
  system temporary directory and reads the copy.  This is the only supported
  read strategy when a live WAL or rollback journal exists.

``writer_coordinated`` is represented in the mode enum for callers that need
to make the boundary explicit, but is intentionally not opened by this
read-only class; a writer must supply its own transaction/lock owner.
"""

from __future__ import annotations
import errno
import fcntl
import sqlite3
import stat
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import math
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Iterator, Literal

from neocortex.persistence.sqlite_paths import readonly_sqlite_uri


class ImmutableSQLiteUnavailable(RuntimeError):
    """A database cannot be proven safe for an immutable read."""


class _SQLiteSnapshotSidecarRace(ImmutableSQLiteUnavailable):
    """A captured sidecar disappeared before its bounded copy completed."""


class SQLiteSnapshotBudgetExceeded(ImmutableSQLiteUnavailable):
    """A detached SQLite snapshot exceeded a bounded preparation budget."""

    def __init__(self, reason: str) -> None:
        if reason not in {"temporary_bytes", "prepare_time", "cancelled"}:
            raise ValueError(f"unsupported SQLite snapshot budget reason: {reason}")
        self.reason = reason
        super().__init__(f"SQLite snapshot {reason.replace('_', ' ')} budget exhausted")


DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES = 256 * 1024 * 1024
DEFAULT_SQLITE_SNAPSHOT_PREPARE_TIMEOUT_SECONDS = 60.0
DEFAULT_SQLITE_SNAPSHOT_BLOCK_BYTES = 1024 * 1024

# SQLite's Linux locking contract uses these fixed control bytes.  The first
# three WAL-index bytes are write/checkpoint/recovery locks (wal.h's
# WALINDEX_LOCK_OFFSET + 0..2); the rollback VFS reserves PENDING_BYTE and
# RESERVED_BYTE in the main file.  The Linux guard below acquires shared OFD
# locks in a short-lived helper process and retains them for the strict read;
# no owner bytes or sidecars are changed.  Keep the residual SHM size named
# here rather than duplicating a magic number in planner/reader paths.
SQLITE_WAL_EMPTY_BYTES = 0
SQLITE_WAL_SHM_RESIDUAL_BYTES = 32 * 1024
SQLITE_WAL_WRITE_LOCK_OFFSET = 120
SQLITE_WAL_CHECKPOINT_LOCK_OFFSET = SQLITE_WAL_WRITE_LOCK_OFFSET + 1
SQLITE_WAL_RECOVERY_LOCK_OFFSET = SQLITE_WAL_WRITE_LOCK_OFFSET + 2
_SQLITE_WAL_CONTROL_LOCK_OFFSETS = (
    SQLITE_WAL_WRITE_LOCK_OFFSET,
    SQLITE_WAL_CHECKPOINT_LOCK_OFFSET,
    SQLITE_WAL_RECOVERY_LOCK_OFFSET,
)
SQLITE_ROLLBACK_PENDING_LOCK_OFFSET = 0x40000000
SQLITE_ROLLBACK_RESERVED_LOCK_OFFSET = SQLITE_ROLLBACK_PENDING_LOCK_OFFSET + 1
_SQLITE_ROLLBACK_CONTROL_LOCK_OFFSETS = (
    SQLITE_ROLLBACK_PENDING_LOCK_OFFSET,
    SQLITE_ROLLBACK_RESERVED_LOCK_OFFSET,
)
_SQLITE_FLOCK_FORMAT = "@hhqqi"
_SQLITE_KNOWN_SIDECAR_SUFFIXES = frozenset({"-journal", "-wal", "-shm"})
_SQLITE_ACTIVITY_MAX_FDS = 4096


@dataclass(frozen=True, slots=True)
class SQLiteSnapshotBudget:
    """Bound preparation of a detached SQLite view without touching its owner.

    The byte limit measures the high-water mark of files below the temporary
    snapshot directory.  ``cancellation_check`` is sampled before and after
    each bounded copy/backup block, before materialization steps, and during
    integrity checks.  A generation may reuse a prepared view without paying
    the preparation budget again; callers still own the view lifetime.
    """

    max_temporary_bytes: int = DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES
    prepare_timeout_seconds: float = DEFAULT_SQLITE_SNAPSHOT_PREPARE_TIMEOUT_SECONDS
    cancellation_check: Callable[[], bool | None] | None = None
    monotonic_clock: Callable[[], float] = time.monotonic
    block_bytes: int = DEFAULT_SQLITE_SNAPSHOT_BLOCK_BYTES

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_temporary_bytes, bool)
            or not isinstance(self.max_temporary_bytes, int)
            or self.max_temporary_bytes <= 0
        ):
            raise ValueError("max_temporary_bytes must be a positive integer")
        if (
            isinstance(self.prepare_timeout_seconds, bool)
            or not isinstance(self.prepare_timeout_seconds, (int, float))
            or not math.isfinite(float(self.prepare_timeout_seconds))
            or float(self.prepare_timeout_seconds) <= 0
        ):
            raise ValueError("prepare_timeout_seconds must be finite and positive")
        if (
            isinstance(self.block_bytes, bool)
            or not isinstance(self.block_bytes, int)
            or not 1 <= self.block_bytes <= 16 * 1024 * 1024
        ):
            raise ValueError("block_bytes must be between 1 and 16777216")
        if self.cancellation_check is not None and not callable(self.cancellation_check):
            raise TypeError("cancellation_check must be callable or None")
        if not callable(self.monotonic_clock):
            raise TypeError("monotonic_clock must be callable")
        object.__setattr__(self, "prepare_timeout_seconds", float(self.prepare_timeout_seconds))


@dataclass(slots=True)
class SQLiteSnapshotMetrics:
    """Bounded preparation evidence for one snapshot session or generation."""

    temporary_bytes: int = 0
    attempts: int = 0
    prepare_time_seconds: float = 0.0
    reused_views: int = 0
    generation: object | None = None
    cancelled: bool = False

    @property
    def bytes_temporary(self) -> int:
        """Compatibility alias for callers naming the byte metric explicitly."""

        return self.temporary_bytes

    @property
    def prepare_time_ns(self) -> int:
        """Return preparation time in integer nanoseconds for telemetry sinks."""

        return max(0, round(self.prepare_time_seconds * 1_000_000_000))


@dataclass(slots=True)
class SQLiteSnapshotOperationMetrics:
    """Preparation totals and live temporary retention for one reuse cache."""

    attempts: int = 0
    prepared_views: int = 0
    reused_views: int = 0
    invalidated_views: int = 0
    retained_views: int = 0
    retained_temporary_bytes: int = 0
    peak_temporary_bytes: int = 0
    prepare_time_seconds: float = 0.0
    cancelled: bool = False


def _check_snapshot_cancellation(
    budget: SQLiteSnapshotBudget,
    metrics: SQLiteSnapshotMetrics | SQLiteSnapshotOperationMetrics,
) -> None:
    callback = budget.cancellation_check
    if callback is None:
        return
    try:
        decision = callback()
    except BaseException:
        metrics.cancelled = True
        raise
    if decision is not None and decision is not False:
        metrics.cancelled = True
        raise SQLiteSnapshotBudgetExceeded("cancelled")


@dataclass(slots=True)
class _ReusableSnapshot:
    session: "SQLiteReadSession"
    references: int = 0
    temporary_bytes: int = 0
    invalidated: bool = False


class SQLiteSnapshotReuseCache:
    """Reuse one fenced snapshot within a caller-owned logical operation.

    The cache is deliberately ephemeral: entries are keyed by the source fence
    and caller generation, and ``close`` releases every session.  It never
    survives a process or becomes a second durable state store. The aggregate
    byte limit covers all retained views plus preparation of the next view,
    without increasing an individual snapshot's budget. Idle views may be
    evicted for space; active leases remain counted until they are released.
    """

    def __init__(
        self,
        *,
        max_entries: int = 32,
        max_temporary_bytes: int = DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES,
    ) -> None:
        if type(max_entries) is not int or not 1 <= max_entries <= 256:
            raise ValueError("max_entries must be between 1 and 256")
        if type(max_temporary_bytes) is not int or max_temporary_bytes <= 0:
            raise ValueError("max_temporary_bytes must be a positive integer")
        self._entries: dict[tuple[object, ...], _ReusableSnapshot] = {}
        self._lock = threading.RLock()
        self._owner_thread: int | None = None
        self._max_entries = max_entries
        self._max_temporary_bytes = max_temporary_bytes
        self._metrics = SQLiteSnapshotOperationMetrics()

    @property
    def snapshot_metrics(self) -> SQLiteSnapshotOperationMetrics:
        """Return independent metrics suitable for ``dataclasses.asdict``.

        Peak bytes include retained views plus a candidate's preparation
        high-water mark; current bytes count only live prepared snapshots.
        Invalidations include stale, errored, and capacity-evicted views.
        """

        with self._lock:
            return replace(self._metrics)

    @property
    def remaining_temporary_bytes(self) -> int:
        """Return the aggregate byte allowance not held by prepared views."""

        with self._lock:
            return self._max_temporary_bytes - self._metrics.retained_temporary_bytes

    def _discard(self, key: tuple[object, ...], entry: _ReusableSnapshot) -> None:
        if self._entries.get(key) is not entry:
            return
        del self._entries[key]
        try:
            entry.session.close()
        finally:
            self._metrics.retained_views -= 1
            self._metrics.retained_temporary_bytes -= entry.temporary_bytes

    def _invalidate(self, key: tuple[object, ...], entry: _ReusableSnapshot) -> None:
        if not entry.invalidated:
            entry.invalidated = True
            self._metrics.invalidated_views += 1
        if entry.references == 0:
            self._discard(key, entry)

    def _evict_idle(self) -> bool:
        for key, entry in self._entries.items():
            if entry.references == 0:
                self._invalidate(key, entry)
                return True
        return False

    @staticmethod
    def _budget_key(budget: SQLiteSnapshotBudget | None) -> object:
        if budget is None:
            return None
        return (
            budget.max_temporary_bytes,
            budget.prepare_timeout_seconds,
            budget.block_bytes,
            id(budget.cancellation_check),
            id(budget.monotonic_clock),
        )

    @contextmanager
    def acquire(
        self,
        path: str | Path,
        *,
        generation: object,
        mode: SQLiteReadMode | str = "snapshot_temp",
        timeout_seconds: float = 60.0,
        temp_root: str | Path | None = None,
        budget: SQLiteSnapshotBudget | None = None,
    ) -> Iterator[sqlite3.Connection]:
        try:
            hash(generation)
        except TypeError as exc:
            raise ValueError("snapshot generation must be hashable") from exc
        selected = Path(path).absolute()
        selected_mode = SQLiteReadMode(mode).value
        selected_temp_root = None if temp_root is None else str(Path(temp_root).absolute())
        selected_budget = _coerce_snapshot_budget(budget, timeout_seconds=timeout_seconds)
        key: tuple[object, ...]
        with self._lock:
            current_thread = threading.get_ident()
            if self._owner_thread is None:
                self._owner_thread = current_thread
            elif self._owner_thread != current_thread:
                raise RuntimeError("SQLite snapshot reuse cache is thread-affine")
            fence = capture_sqlite_read_fence(selected)
            key = (
                str(selected),
                generation,
                fence,
                selected_mode,
                float(timeout_seconds),
                selected_temp_root,
                self._budget_key(budget),
            )
            # Detached readers can finish their current lease after a publish,
            # but a new lease must never rediscover an older fence/generation.
            for old_key, old_entry in tuple(self._entries.items()):
                if old_key[0] == str(selected) and old_key != key:
                    self._invalidate(old_key, old_entry)
            entry = self._entries.get(key)
            try:
                _check_snapshot_cancellation(selected_budget, self._metrics)
            except BaseException as exc:
                if entry is not None:
                    try:
                        self._invalidate(key, entry)
                    except BaseException as cleanup_error:
                        exc.add_note(f"cancelled SQLite snapshot cleanup failed: {cleanup_error}")
                raise
            if entry is not None:
                if entry.invalidated:
                    raise ImmutableSQLiteUnavailable(
                        "SQLite snapshot view was invalidated while still in use"
                    )
                try:
                    # Accessing this local property detects an explicitly
                    # closed borrowed handle without querying the source.
                    _ = entry.session.connection.in_transaction
                except sqlite3.Error:
                    self._invalidate(key, entry)
                    if entry.references:
                        raise
                    entry = None
            if entry is None:
                while len(self._entries) >= self._max_entries:
                    if not self._evict_idle():
                        raise ImmutableSQLiteUnavailable(
                            "SQLite snapshot reuse cache capacity is exhausted"
                        )
                preparation_budget = selected_budget
                if selected_mode == SQLiteReadMode.SNAPSHOT_TEMP.value:
                    source_bytes = _sqlite_fence_bytes(fence)
                    # Do not evict usable views for a candidate that cannot
                    # fit even in an otherwise empty operation.
                    limit = min(selected_budget.max_temporary_bytes, self._max_temporary_bytes)
                    if source_bytes <= limit:
                        while source_bytes > self.remaining_temporary_bytes:
                            if not self._evict_idle():
                                break
                    remaining = self.remaining_temporary_bytes
                    if remaining <= 0:
                        raise SQLiteSnapshotBudgetExceeded("temporary_bytes")
                    preparation_budget = replace(
                        selected_budget,
                        max_temporary_bytes=min(selected_budget.max_temporary_bytes, remaining),
                    )
                session = SQLiteReadSession(
                    selected,
                    mode=mode,
                    timeout_seconds=timeout_seconds,
                    temp_root=temp_root,
                    budget=preparation_budget,
                    generation=generation,
                )
                try:
                    session.open()
                finally:
                    self._metrics.attempts += session.metrics.attempts
                    self._metrics.prepare_time_seconds += session.metrics.prepare_time_seconds
                    self._metrics.cancelled |= session.metrics.cancelled
                    self._metrics.peak_temporary_bytes = max(
                        self._metrics.peak_temporary_bytes,
                        self._metrics.retained_temporary_bytes + session.metrics.temporary_bytes,
                    )
                # The session may have retried a source race. Its successful
                # fence, not the cache preflight fence, owns the copied bytes.
                key = (*key[:2], session.source_fence, *key[3:])
                temporary_database = session.temporary_database
                try:
                    if key in self._entries:
                        # A retry may rediscover a fence whose invalidated
                        # view still has active leases. Never overwrite its
                        # ownership/accounting with the new candidate.
                        raise ImmutableSQLiteUnavailable(
                            "SQLite snapshot fence collided with a retained view"
                        )
                    temporary_bytes = (
                        0 if temporary_database is None else temporary_database.stat().st_size
                    )
                    if temporary_bytes > self.remaining_temporary_bytes:
                        raise SQLiteSnapshotBudgetExceeded("temporary_bytes")
                except BaseException as exc:
                    try:
                        session.close()
                    except BaseException as cleanup_error:
                        exc.add_note(f"unretained SQLite snapshot cleanup failed: {cleanup_error}")
                    raise
                entry = _ReusableSnapshot(session, temporary_bytes=temporary_bytes)
                self._entries[key] = entry
                self._metrics.prepared_views += 1
                self._metrics.retained_views += 1
                self._metrics.retained_temporary_bytes += temporary_bytes
            else:
                entry.session.metrics.reused_views += 1
                self._metrics.reused_views += 1
            entry.references += 1
            connection = entry.session.connection
        primary: BaseException | None = None
        try:
            yield connection
        except BaseException as exc:
            primary = exc
            raise
        finally:
            with self._lock:
                entry.references -= 1
                try:
                    if primary is not None:
                        self._invalidate(key, entry)
                    elif entry.invalidated and entry.references == 0:
                        self._discard(key, entry)
                except BaseException as cleanup_error:
                    if primary is None:
                        raise
                    primary.add_note(f"reused SQLite snapshot cleanup failed: {cleanup_error}")

    def close(self) -> None:
        primary: BaseException | None = None
        with self._lock:
            current_thread = threading.get_ident()
            if self._owner_thread is not None and self._owner_thread != current_thread:
                raise RuntimeError("SQLite snapshot reuse cache is thread-affine")
            for key, entry in tuple(self._entries.items()):
                try:
                    self._discard(key, entry)
                except BaseException as exc:
                    if primary is None:
                        primary = exc
                    else:
                        primary.add_note(f"reused SQLite snapshot cleanup failed: {exc}")
            self._owner_thread = None
        if primary is not None:
            raise primary

    def __enter__(self) -> "SQLiteSnapshotReuseCache":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> Literal[False]:
        try:
            self.close()
        except BaseException as cleanup_error:
            if exc_value is not None:
                exc_value.add_note(
                    "reused SQLite snapshot cache cleanup failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            else:
                raise
        return False


class _SnapshotBudgetState:
    """Mutable accounting shared by one bounded snapshot preparation."""

    __slots__ = ("_last_tree_bytes", "budget", "deadline", "metrics", "temporary_root")

    def __init__(
        self,
        budget: SQLiteSnapshotBudget,
        *,
        metrics: SQLiteSnapshotMetrics,
        temporary_root: Path,
        deadline: float | None = None,
    ) -> None:
        self.budget = budget
        self.deadline = (
            budget.monotonic_clock() + budget.prepare_timeout_seconds
            if deadline is None
            else deadline
        )
        self.metrics = metrics
        self.temporary_root = temporary_root
        self._last_tree_bytes = 0

    def _tree_bytes(self) -> int:
        total = 0
        try:
            entries = self.temporary_root.rglob("*")
            for entry in entries:
                try:
                    if entry.is_file() and not entry.is_symlink():
                        total += entry.stat().st_size
                except FileNotFoundError:
                    continue
        except OSError as exc:
            raise ImmutableSQLiteUnavailable(
                "SQLite snapshot temporary directory cannot be measured"
            ) from exc
        return total

    def checkpoint(self) -> None:
        _check_snapshot_cancellation(self.budget, self.metrics)
        if self.budget.monotonic_clock() >= self.deadline:
            raise SQLiteSnapshotBudgetExceeded("prepare_time")
        observed = self._tree_bytes()
        if self.budget.monotonic_clock() >= self.deadline:
            raise SQLiteSnapshotBudgetExceeded("prepare_time")
        if observed > self.budget.max_temporary_bytes:
            raise SQLiteSnapshotBudgetExceeded("temporary_bytes")
        self._last_tree_bytes = observed
        self.metrics.temporary_bytes = max(self.metrics.temporary_bytes, observed)

    def before_write(self, size: int) -> None:
        if size < 0:
            raise ValueError("SQLite snapshot block size cannot be negative")
        self.checkpoint()
        if self._last_tree_bytes + size > self.budget.max_temporary_bytes:
            raise SQLiteSnapshotBudgetExceeded("temporary_bytes")

    def record_prepare_time(self, started: float) -> None:
        elapsed = self.budget.monotonic_clock() - started
        self.metrics.prepare_time_seconds = max(0.0, float(elapsed))


def _coerce_snapshot_budget(
    budget: SQLiteSnapshotBudget | None,
    *,
    timeout_seconds: float,
    cancellation_check: Callable[[], bool | None] | None = None,
    max_temporary_bytes: int | None = None,
) -> SQLiteSnapshotBudget:
    if budget is not None and not isinstance(budget, SQLiteSnapshotBudget):
        raise TypeError("budget must be a SQLiteSnapshotBudget or None")
    if budget is not None:
        if cancellation_check is not None or max_temporary_bytes is not None:
            raise ValueError("budget cannot be combined with budget override arguments")
        return budget
    timeout = float(timeout_seconds)
    if isinstance(timeout_seconds, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("SQLite snapshot timeout must be finite and positive")
    return SQLiteSnapshotBudget(
        max_temporary_bytes=(
            DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES
            if max_temporary_bytes is None
            else max_temporary_bytes
        ),
        prepare_timeout_seconds=timeout,
        cancellation_check=cancellation_check,
    )


class SQLiteReadMode(str, Enum):
    """Explicit ownership strategy for a read-only SQLite session."""

    IMMUTABLE_STRICT = "immutable_strict"
    SNAPSHOT_TEMP = "snapshot_temp"
    WRITER_COORDINATED = "writer_coordinated"


@dataclass(frozen=True, slots=True)
class SQLiteFileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class SQLiteImmutableFence:
    main: SQLiteFileIdentity
    sidecars: tuple[tuple[str, SQLiteFileIdentity], ...]


def _sqlite_fence_bytes(fence: SQLiteImmutableFence) -> int:
    return fence.main.size + sum(identity.size for _suffix, identity in fence.sidecars)


def _identity_from_stat(
    value: os.stat_result,
    *,
    label: str,
    name: str,
    allow_empty: bool = False,
) -> SQLiteFileIdentity:
    """Build a fence identity from an already acquired ``stat`` result."""

    if stat.S_ISLNK(value.st_mode):
        raise ImmutableSQLiteUnavailable(
            f"{label} is a symlink and not a stable regular file: {name}"
        )
    if not stat.S_ISREG(value.st_mode) or (value.st_size <= 0 and not allow_empty):
        raise ImmutableSQLiteUnavailable(f"{label} is not a stable regular file: {name}")
    return SQLiteFileIdentity(
        device=int(value.st_dev),
        inode=int(value.st_ino),
        mode=int(value.st_mode),
        size=int(value.st_size),
        mtime_ns=int(value.st_mtime_ns),
        ctime_ns=int(value.st_ctime_ns),
    )


def _file_identity(
    path: Path,
    *,
    label: str,
    allow_empty: bool = False,
) -> SQLiteFileIdentity:
    try:
        # ``lstat`` is deliberate: following an endpoint symlink would make
        # the fence refer to a file outside the published owner root.
        value = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ImmutableSQLiteUnavailable(f"{label} cannot be inspected: {path.name}") from exc
    return _identity_from_stat(
        value,
        label=label,
        name=path.name,
        allow_empty=allow_empty,
    )


def _reject_unexpected_sqlite_sidecars(path: Path) -> None:
    """Reject SQLite-looking sibling files outside the known sidecar set.

    SQLite's owner contract has exactly three sidecar names.  An unknown
    ``<owner>-*`` sibling is an ambiguous journal/coordination artifact, not
    evidence that the owner is quiescent.  Failing here also prevents a later
    immutable open from silently ignoring a newly-created sidecar.
    """

    prefix = f"{path.name}-"
    try:
        with os.scandir(path.parent) as entries:
            for entry in entries:
                if not entry.name.startswith(prefix):
                    continue
                suffix = entry.name[len(path.name) :]
                if suffix not in _SQLITE_KNOWN_SIDECAR_SUFFIXES:
                    raise ImmutableSQLiteUnavailable(
                        f"SQLite owner has an unexpected sidecar: {entry.name}"
                    )
    except ImmutableSQLiteUnavailable:
        raise
    except OSError as exc:
        raise ImmutableSQLiteUnavailable(
            f"SQLite owner sidecar directory cannot be inspected: {path.parent}"
        ) from exc


def _sqlite_owner_process_is_open(path: Path, fence: SQLiteImmutableFence) -> bool:
    """Return whether this process still owns an SQLite file descriptor.

    Lock state alone is not sufficient: POSIX SQLite locks are released when
    *any* descriptor for the inode is closed by a process, so an incidental
    byte read in a cooperating thread can make a live transaction temporarily
    invisible to ``F_GETLK``.  A read-only ``/proc/self/fd`` scan supplies the
    complementary same-process liveness evidence without walking every host
    process (the isolated OFD-lock guard covers external owners).  It
    never opens the owner or sidecars.
    """

    if sys.platform != "linux":
        raise ImmutableSQLiteUnavailable("SQLite owner activity cannot be verified outside Linux")
    expected = {(fence.main.device, fence.main.inode)}
    expected.update((identity.device, identity.inode) for _suffix, identity in fence.sidecars)
    targets = {
        os.path.abspath(os.fspath(path)),
        *(os.path.abspath(f"{path}{suffix}") for suffix in _SQLITE_KNOWN_SIDECAR_SUFFIXES),
    }
    fd_directory = Path("/proc/self/fd")
    try:
        descriptors = tuple(sorted(fd_directory.iterdir(), key=os.fspath))
    except OSError as exc:
        raise ImmutableSQLiteUnavailable(
            "SQLite owner activity cannot be inspected through /proc/self"
        ) from exc
    if len(descriptors) > _SQLITE_ACTIVITY_MAX_FDS:
        raise ImmutableSQLiteUnavailable("SQLite owner activity exceeds its descriptor bound")
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
        except FileNotFoundError:
            continue
        except OSError:
            continue
        target = target.removesuffix(" (deleted)")
        if os.path.abspath(target) in targets:
            return True
        try:
            metadata = os.stat(descriptor)
        except OSError:
            continue
        if (int(metadata.st_dev), int(metadata.st_ino)) in expected:
            return True
    return False


def _sqlite_owner_process_has_writer_lock(fence: SQLiteImmutableFence) -> bool:
    """Detect this process's rollback writer lock without opening SQLite.

    ``F_GETLK`` and OFD locks do not reliably report a POSIX lock held by the
    same process.  Linux exposes the owning PID and byte range in
    ``/proc/locks``; only a WRITE lock on the rollback PENDING/RESERVED bytes
    is considered.  Ordinary same-process readers therefore remain eligible
    for strict reads while an in-process ``BEGIN IMMEDIATE`` remains
    fail-closed.
    """

    if sys.platform != "linux":
        raise ImmutableSQLiteUnavailable("SQLite owner locks cannot be verified outside Linux")
    device = f"{os.major(fence.main.device):02x}:{os.minor(fence.main.device):02x}"
    identity = f"{device}:{fence.main.inode}"
    try:
        lines = Path("/proc/locks").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise ImmutableSQLiteUnavailable(
            "SQLite owner locks cannot be inspected through /proc/locks"
        ) from exc
    pid = str(os.getpid())
    for line in lines:
        fields = line.split()
        if len(fields) < 8 or fields[1] != "POSIX" or fields[3] != "WRITE":
            continue
        if fields[4] != pid or fields[5] != identity:
            continue
        try:
            start = int(fields[6])
            end = int(fields[7]) if fields[7] != "EOF" else sys.maxsize
        except ValueError as exc:
            raise ImmutableSQLiteUnavailable(
                "SQLite owner lock record has an invalid byte range"
            ) from exc
        if any(start <= offset <= end for offset in _SQLITE_ROLLBACK_CONTROL_LOCK_OFFSETS):
            return True
    return False


@dataclass(slots=True)
class _SQLiteWriterLockGuard:
    """A shared OFD guard over SQLite writer-control lock bytes."""

    descriptors: tuple[int, ...]
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        primary: BaseException | None = None
        for descriptor in reversed(self.descriptors):
            try:
                os.close(descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF and primary is None:
                    primary = ImmutableSQLiteUnavailable(
                        "SQLite writer lock guard could not be released"
                    )
                    primary.__cause__ = exc
        if primary is not None:
            raise primary


def _sqlite_lock_targets(
    path: Path,
    fence: SQLiteImmutableFence,
) -> tuple[tuple[Path, tuple[int, ...], SQLiteFileIdentity], ...]:
    sidecars = dict(fence.sidecars)
    targets: list[tuple[Path, tuple[int, ...], SQLiteFileIdentity]] = [
        (path, _SQLITE_ROLLBACK_CONTROL_LOCK_OFFSETS, fence.main)
    ]
    if set(sidecars) == {"-wal", "-shm"}:
        targets.append((Path(f"{path}-shm"), _SQLITE_WAL_CONTROL_LOCK_OFFSETS, sidecars["-shm"]))
    return tuple(targets)


def _acquire_sqlite_writer_lock_guard(
    path: Path,
    fence: SQLiteImmutableFence,
) -> _SQLiteWriterLockGuard:
    """Hold shared SQLite control locks for the complete strict read.

    A point-in-time ``GETLK`` result is not enough: a writer could begin after
    the probe and before the immutable connection opens.  The guard acquires
    shared ``F_OFD_SETLK`` locks and retains the descriptors until the caller
    releases them.  This blocks a future SQLite writer/checkpoint/recovery lock
    without opening or mutating the owner in the reader process.  If a writer
    already owns a lock, acquisition fails and callers fall back to the
    bounded snapshot strategy.
    """

    if sys.platform != "linux":
        raise ImmutableSQLiteUnavailable(
            "SQLite owner writer-lock state cannot be verified outside Linux"
        )
    setlk = getattr(fcntl, "F_OFD_SETLK", None)
    if setlk is None:
        raise ImmutableSQLiteUnavailable(
            "SQLite writer lock guard cannot verify same-process locks"
        )
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ImmutableSQLiteUnavailable(
            "SQLite writer lock guard is unavailable on this Linux runtime"
        )
    try:
        struct.calcsize(_SQLITE_FLOCK_FORMAT)
        int(fcntl.F_RDLCK)
    except (AttributeError, struct.error, TypeError, ValueError) as exc:
        raise ImmutableSQLiteUnavailable(
            "SQLite writer lock guard has an unsupported flock ABI"
        ) from exc

    # A same-process WAL connection can release a POSIX SHM lock when an
    # incidental descriptor is closed, making the lock probe look quiescent
    # while a transaction remains open.  The residual WAL/SHM shape therefore
    # needs the complementary descriptor-liveness check.  Sidecar-free
    # rollback owners are covered by the actual OFD lock acquisition below;
    # ordinary same-process readers do not hold RESERVED/PENDING bytes.
    sidecar_names = set(dict(fence.sidecars))
    if sidecar_names == {"-wal", "-shm"} and _sqlite_owner_process_is_open(path, fence):
        raise ImmutableSQLiteUnavailable(
            "SQLite owner process is active; sidecars are not proven inactive"
        )
    if not sidecar_names and _sqlite_owner_process_has_writer_lock(fence):
        raise ImmutableSQLiteUnavailable(
            "SQLite owner process holds a rollback writer lock; sidecars are not proven inactive"
        )

    targets = _sqlite_lock_targets(path, fence)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow
    descriptors: list[int] = []
    try:
        for target, offsets, expected in targets:
            descriptor = os.open(os.fspath(target), flags)
            descriptors.append(descriptor)
            observed = _identity_from_stat(
                os.fstat(descriptor),
                label="SQLite writer-lock target",
                name=target.name,
                allow_empty=True,
            )
            if observed != expected:
                raise ImmutableSQLiteUnavailable(
                    "SQLite writer-lock target changed during guard acquisition"
                )
            for offset in offsets:
                request = struct.pack(
                    _SQLITE_FLOCK_FORMAT,
                    int(fcntl.F_RDLCK),
                    int(os.SEEK_SET),
                    int(offset),
                    1,
                    0,
                )
                try:
                    fcntl.fcntl(descriptor, setlk, request)
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN}:
                        raise ImmutableSQLiteUnavailable(
                            "SQLite owner writer lock is active; sidecars are not proven inactive"
                        ) from exc
                    raise ImmutableSQLiteUnavailable(
                        "SQLite writer lock guard could not verify control locks"
                    ) from exc
        return _SQLiteWriterLockGuard(descriptors=tuple(descriptors))
    except BaseException:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _probe_sqlite_writer_locks(
    path: Path,
    fence: SQLiteImmutableFence,
) -> None:
    """Probe writer locks without retaining a guard for the caller."""

    guard = _acquire_sqlite_writer_lock_guard(path, fence)
    guard.close()


def capture_sqlite_read_fence(path: Path) -> SQLiteImmutableFence:
    """Capture main/sidecar identities without opening SQLite.

    Unlike :func:`capture_sqlite_immutable_fence`, this primitive accepts an
    active sidecar set so the snapshot strategy can copy it.  It still rejects
    symlinks, non-regular files, and inaccessible entries.
    """

    selected = Path(path)
    _reject_unexpected_sqlite_sidecars(selected)
    main = _file_identity(selected, label="SQLite owner")
    sidecars: list[tuple[str, SQLiteFileIdentity]] = []
    for suffix in ("-journal", "-wal", "-shm"):
        candidate = Path(f"{selected}{suffix}")
        try:
            identity = _file_identity(
                candidate,
                label=f"SQLite sidecar {suffix}",
                allow_empty=True,
            )
        except FileNotFoundError:
            continue
        sidecars.append((suffix, identity))
    return SQLiteImmutableFence(main=main, sidecars=tuple(sidecars))


def capture_sqlite_immutable_fence(path: Path) -> SQLiteImmutableFence:
    """Capture a quiescent owner fence without opening SQLite."""

    selected = Path(path)
    fence = capture_sqlite_read_fence(selected)
    require_inactive_sqlite_sidecars(fence, path=selected)
    return fence


def require_inactive_sqlite_sidecars(
    fence: SQLiteImmutableFence,
    *,
    path: str | Path | None = None,
) -> None:
    """Require a canonical inactive sidecar layout and, when possible, locks.

    The accepted layouts are either no sidecars or exactly one regular empty
    WAL plus SQLite's residual 32 KiB SHM file.  Empty/isolated/extra sidecars
    remain ambiguous.  A path is required so the Linux writer-lock probe can
    establish quiescence; a detached fence is never accepted.  The final
    immutable fence is still checked at
    close, so this probe is evidence, not a replacement for the before/after
    identity fence.
    """

    _require_inactive_sqlite_layout(fence)
    if path is None:
        raise ImmutableSQLiteUnavailable(
            "SQLite sidecar quiescence requires the owner path for lock verification"
        )
    _probe_sqlite_writer_locks(Path(path), fence)


def _require_inactive_sqlite_layout(fence: SQLiteImmutableFence) -> None:
    """Validate the inactive sidecar shape without probing owner locks."""

    sidecars = dict(fence.sidecars)
    journal = sidecars.get("-journal")
    wal = sidecars.get("-wal")
    shm = sidecars.get("-shm")
    if journal is not None and journal.size > 0:
        raise ImmutableSQLiteUnavailable("SQLite owner has a non-empty rollback journal")
    if wal is not None and wal.size > 0:
        raise ImmutableSQLiteUnavailable("SQLite owner has a non-empty WAL")
    inactive_layout = not sidecars or (
        set(sidecars) == {"-wal", "-shm"}
        and wal is not None
        and wal.size == SQLITE_WAL_EMPTY_BYTES
        and shm is not None
        and shm.size == SQLITE_WAL_SHM_RESIDUAL_BYTES
    )
    if not inactive_layout:
        raise ImmutableSQLiteUnavailable("SQLite owner sidecars are not proven inactive")


def preferred_sqlite_read_mode(path: str | Path) -> SQLiteReadMode:
    """Choose strict or temporary-copy mode from filesystem-only evidence."""

    selected = Path(path)
    fence = capture_sqlite_read_fence(selected)
    try:
        require_inactive_sqlite_sidecars(fence, path=selected)
    except ImmutableSQLiteUnavailable:
        return SQLiteReadMode.SNAPSHOT_TEMP
    return SQLiteReadMode.IMMUTABLE_STRICT


def _configure_read_connection(
    connection: sqlite3.Connection,
    *,
    timeout_seconds: float,
    label: str,
) -> sqlite3.Connection:
    """Apply and verify connection-local read safeguards."""

    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={max(1, round(timeout_seconds * 1000))}")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    safeguards = (
        connection.execute("PRAGMA foreign_keys").fetchone(),
        connection.execute("PRAGMA query_only").fetchone(),
        connection.execute("PRAGMA trusted_schema").fetchone(),
    )
    if (
        safeguards[0] is None
        or int(safeguards[0][0]) != 1
        or safeguards[1] is None
        or int(safeguards[1][0]) != 1
        or safeguards[2] is None
        or int(safeguards[2][0]) != 0
    ):
        raise ImmutableSQLiteUnavailable(f"{label} safeguards are unavailable")
    return connection


def _verify_immutable_source(
    path: Path,
    fence: SQLiteImmutableFence,
    *,
    verify_locks: bool = True,
) -> None:
    try:
        after = capture_sqlite_read_fence(path)
    except (OSError, ImmutableSQLiteUnavailable) as exc:
        raise ImmutableSQLiteUnavailable("SQLite owner changed during immutable read") from exc
    if after != fence:
        raise ImmutableSQLiteUnavailable("SQLite owner changed during immutable read")
    # Recheck writer/control locks after closing the reader.  A writer that
    # appeared after the initial probe must not turn a strict read into a
    # false quiescence claim merely because its bytes have not changed yet.
    # The OFD guard is still active in the bare connection wrapper, so that
    # wrapper asks only for the identity fence; the owning session performs the
    # final lock check after releasing the guard.
    if verify_locks:
        require_inactive_sqlite_sidecars(after, path=path)


class _FencedImmutableConnection(sqlite3.Connection):
    """Keep the after-read fence even for factories returning a bare handle."""

    _source_path: Path | None = None
    _source_fence: SQLiteImmutableFence | None = None
    _writer_lock_guard: _SQLiteWriterLockGuard | None = None

    def close(self) -> None:
        path, fence = self._source_path, self._source_fence
        guard = self._writer_lock_guard
        self._source_path = None
        self._source_fence = None
        self._writer_lock_guard = None
        primary: BaseException | None = None
        try:
            super().close()
        except BaseException as exc:
            primary = exc
        if path is not None and fence is not None:
            try:
                if guard is None:
                    _verify_immutable_source(path, fence)
                else:
                    try:
                        _verify_immutable_source(path, fence, verify_locks=False)
                    except TypeError as exc:
                        # Preserve compatibility with injected legacy test
                        # seams that accepted only ``(path, fence)``.
                        if "verify_locks" not in str(exc):
                            raise
                        _verify_immutable_source(path, fence)
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(f"SQLite final source fence failed: {exc}")
        if guard is not None:
            try:
                guard.close()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(f"SQLite writer lock guard cleanup failed: {exc}")
        if primary is not None:
            raise primary


def open_immutable_sqlite_connection(
    path: str | Path,
    *,
    timeout_seconds: float = 60.0,
) -> sqlite3.Connection:
    """Open one immutable connection with a source fence verified at close.

    Legacy connection factories must explicitly close the returned handle;
    SQLite's transaction context manager does not close a connection.  New
    code should prefer :class:`SQLiteReadSession` for an owned read lifecycle.
    """

    selected = Path(path).absolute()
    if isinstance(timeout_seconds, bool) or float(timeout_seconds) <= 0:
        raise ValueError("immutable SQLite timeout must be positive")
    fence = capture_sqlite_read_fence(selected)
    require_inactive_sqlite_sidecars(fence, path=selected)
    # Retain the same OFD guard for both the residual WAL/SHM and sidecar-free
    # rollback layouts.  Without this, a writer could begin after the point
    # probe and before the immutable handle is opened.
    guard: _SQLiteWriterLockGuard | None = _acquire_sqlite_writer_lock_guard(
        selected, fence
    )
    connection: sqlite3.Connection | None = None
    try:
        # The guard blocks a writer from acquiring a control lock while this
        # second fence is captured and while SQLite opens the immutable view.
        confirmed = capture_sqlite_read_fence(selected)
        if confirmed != fence:
            raise ImmutableSQLiteUnavailable("SQLite owner changed before immutable read")
        _require_inactive_sqlite_layout(confirmed)
        fence = confirmed
        connection = sqlite3.connect(
            f"{readonly_sqlite_uri(selected)}&immutable=1",
            uri=True,
            timeout=float(timeout_seconds),
            factory=_FencedImmutableConnection,
        )
        _configure_read_connection(
            connection,
            timeout_seconds=float(timeout_seconds),
            label="SQLite immutable read",
        )
        assert isinstance(connection, _FencedImmutableConnection)
        connection._source_path = selected
        connection._source_fence = fence
        connection._writer_lock_guard = guard
        guard = None
        return connection
    except BaseException as exc:
        if connection is not None:
            try:
                connection.close()
            except BaseException as cleanup_error:
                # Preserve the configuration/open failure as the primary error;
                # connection cleanup is diagnostic only.
                exc.add_note(
                    "SQLite immutable connection cleanup failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
        if guard is not None:
            try:
                guard.close()
            except BaseException as cleanup_error:
                exc.add_note(
                    "SQLite writer lock guard cleanup failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
        raise


def _copy_regular_file(
    source: Path,
    destination: Path,
    *,
    budget_state: _SnapshotBudgetState | None = None,
) -> None:
    """Copy one already-fenced file without following a changed symlink."""

    source_fd = os.open(os.fspath(source), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with (
            os.fdopen(source_fd, "rb", closefd=True) as source_stream,
            destination.open("wb") as destination_stream,
        ):
            if budget_state is None:
                shutil.copyfileobj(source_stream, destination_stream, length=1024 * 1024)
            else:
                while True:
                    chunk = source_stream.read(budget_state.budget.block_bytes)
                    if not chunk:
                        break
                    budget_state.before_write(len(chunk))
                    destination_stream.write(chunk)
                    budget_state.checkpoint()
            destination_stream.flush()
            os.fsync(destination_stream.fileno())
            if budget_state is not None:
                budget_state.checkpoint()
    except BaseException:
        # ``fdopen`` owns the descriptor after construction; this is only a
        # best-effort guard for an error before that hand-off.
        try:
            os.close(source_fd)
        except OSError:
            pass
        destination.unlink(missing_ok=True)
        raise


def _copy_regular_file_budgeted(
    source: Path,
    destination: Path,
    budget_state: _SnapshotBudgetState,
) -> None:
    """Invoke the copy seam while retaining compatibility with old test hooks."""

    try:
        _copy_regular_file(source, destination, budget_state=budget_state)
    except TypeError as exc:
        # Older injected seams accepted only ``(source, destination)``.  Keep
        # those bounded fixtures usable while still checkpointing after the
        # delegated copy; unrelated TypeErrors must propagate unchanged.
        if "budget_state" not in str(exc) or "unexpected keyword" not in str(exc):
            raise
        _copy_regular_file(source, destination)
        budget_state.checkpoint()


def _materialize_temporary_database(
    database: Path,
    *,
    timeout_seconds: float,
    budget_state: _SnapshotBudgetState | None = None,
) -> None:
    """Recover copied journals into one standalone main database.

    A byte-for-byte copy of a live WAL or rollback journal is not itself an
    immutable SQLite owner: opening it read-only still depends on sidecars and
    may try to create or recover them.  The copy is therefore opened writable
    *inside the temporary directory*, SQLite is asked to use DELETE journaling
    (which checkpoints WAL frames and recovers a hot rollback journal), and all
    remaining sidecars are removed before the immutable read is opened.  The
    source owner is never opened or changed by this operation.
    """

    connection: sqlite3.Connection | None = None
    primary: BaseException | None = None
    budget_error: BaseException | None = None
    try:
        if budget_state is not None:
            budget_state.checkpoint()
        connection = sqlite3.connect(database, timeout=timeout_seconds)
        connection.execute(f"PRAGMA busy_timeout={max(1, round(timeout_seconds * 1000))}")
        if budget_state is not None:
            budget_state.checkpoint()
        journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        if journal_mode is None or str(journal_mode[0]).lower() != "delete":
            raise ImmutableSQLiteUnavailable(
                "temporary SQLite snapshot could not be materialized as DELETE journal"
            )
        if budget_state is not None:

            def progress() -> int:
                nonlocal budget_error
                try:
                    budget_state.checkpoint()
                except BaseException as exc:
                    budget_error = exc
                    return 1
                return 0

            connection.set_progress_handler(progress, 1000)
        try:
            integrity = connection.execute("PRAGMA quick_check").fetchall()
        except sqlite3.Error as exc:
            if budget_state is not None and budget_error is not None:
                raise budget_error from exc
            raise
        finally:
            if budget_state is not None:
                connection.set_progress_handler(None, 0)
        if budget_state is not None:
            budget_state.checkpoint()
        if integrity != [("ok",)]:
            raise ImmutableSQLiteUnavailable(
                "temporary SQLite snapshot integrity check failed during materialization"
            )
        connection.commit()
        if budget_state is not None:
            budget_state.checkpoint()
    except BaseException as exc:
        primary = exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(
                        "temporary SQLite materialization close failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
    if primary is not None:
        if isinstance(primary, ImmutableSQLiteUnavailable):
            raise primary
        raise ImmutableSQLiteUnavailable(
            f"temporary SQLite snapshot could not be materialized: {database.name}"
        ) from primary

    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(f"{database}{suffix}")
        try:
            if budget_state is not None:
                budget_state.checkpoint()
            sidecar.unlink(missing_ok=True)
        except OSError as exc:
            raise ImmutableSQLiteUnavailable(
                f"temporary SQLite snapshot sidecar could not be removed: {sidecar.name}"
            ) from exc
    capture_sqlite_immutable_fence(database)
    if budget_state is not None:
        budget_state.checkpoint()


class SQLiteReadSession:
    """One fenced, sidecar-safe read session.

    The object supports both context-manager and explicit ``open``/``close``
    lifecycles, which lets legacy facades retain their connection injection
    seams while using the same kernel.  ``snapshot_temp`` owns a temporary
    directory and removes it after the connection closes.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        mode: SQLiteReadMode | str = SQLiteReadMode.IMMUTABLE_STRICT,
        timeout_seconds: float = 60.0,
        temp_root: str | Path | None = None,
        max_attempts: int = 2,
        budget: SQLiteSnapshotBudget | None = None,
        max_temporary_bytes: int | None = None,
        cancellation_check: Callable[[], bool | None] | None = None,
        generation: object | None = None,
    ) -> None:
        self.path = Path(path)
        try:
            self.mode = mode if isinstance(mode, SQLiteReadMode) else SQLiteReadMode(mode)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported SQLite read mode: {mode!r}") from exc
        if isinstance(timeout_seconds, bool) or float(timeout_seconds) <= 0:
            raise ValueError("SQLite read timeout must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self.temp_root = None if temp_root is None else Path(temp_root)
        if type(max_attempts) is not int or not 1 <= max_attempts <= 8:
            raise ValueError("SQLite read max_attempts must be from 1 to 8")
        self.max_attempts = max_attempts
        self.budget = _coerce_snapshot_budget(
            budget,
            timeout_seconds=self.timeout_seconds,
            cancellation_check=cancellation_check,
            max_temporary_bytes=max_temporary_bytes,
        )
        self._metrics = SQLiteSnapshotMetrics(generation=generation)
        self._connection: sqlite3.Connection | None = None
        self._source_fence: SQLiteImmutableFence | None = None
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._temporary_database: Path | None = None
        self._opened = False
        self._prepare_deadline: float | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the live connection, or fail before ``open``."""

        if self._connection is None:
            raise RuntimeError("SQLite read session is not open")
        return self._connection

    @property
    def source_fence(self) -> SQLiteImmutableFence:
        """Return the source fence captured at session open."""

        if self._source_fence is None:
            raise RuntimeError("SQLite read session is not open")
        return self._source_fence

    @property
    def temporary_database(self) -> Path | None:
        """Return the copied database path for a temporary snapshot."""

        return self._temporary_database

    @property
    def metrics(self) -> SQLiteSnapshotMetrics:
        """Return bounded preparation metrics for this session."""

        return self._metrics

    def __enter__(self) -> sqlite3.Connection:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> Literal[False]:
        try:
            self.close()
        except BaseException as cleanup_error:
            # A body exception is the primary failure.  Close/fence/temp
            # cleanup remains observable as a note rather than replacing it.
            if exc_value is not None:
                exc_value.add_note(
                    "SQLite read session cleanup failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            else:
                raise
        return False

    def open(self) -> sqlite3.Connection:
        """Open a fenced read connection exactly once."""

        if self._opened:
            raise RuntimeError("SQLite read session cannot be opened twice")
        self._opened = True
        if self.mode is SQLiteReadMode.WRITER_COORDINATED:
            raise ImmutableSQLiteUnavailable(
                "writer_coordinated requires an owner transaction and lock"
            )
        if self.temp_root is not None:
            try:
                root_stat = self.temp_root.lstat()
            except FileNotFoundError as exc:
                raise ImmutableSQLiteUnavailable("SQLite snapshot temp root is missing") from exc
            if not stat.S_ISDIR(root_stat.st_mode) or self.temp_root.is_symlink():
                raise ImmutableSQLiteUnavailable(
                    "SQLite snapshot temp root is not a real directory"
                )

        last_error: BaseException | None = None
        operation_started = self.budget.monotonic_clock()
        self._prepare_deadline = operation_started + self.budget.prepare_timeout_seconds
        for _attempt in range(self.max_attempts):
            self._metrics.attempts += 1
            attempt_started = self.budget.monotonic_clock()
            temporary_directory: tempfile.TemporaryDirectory[str] | None = None
            budget_state: _SnapshotBudgetState | None = None
            try:
                source_fence = capture_sqlite_read_fence(self.path)
                self._source_fence = source_fence
                _check_snapshot_cancellation(self.budget, self._metrics)
                if self.budget.monotonic_clock() >= self._prepare_deadline:
                    raise SQLiteSnapshotBudgetExceeded("prepare_time")
                if self.mode is SQLiteReadMode.IMMUTABLE_STRICT:
                    require_inactive_sqlite_sidecars(source_fence, path=self.path)
                    self._connection = open_immutable_sqlite_connection(
                        self.path,
                        timeout_seconds=min(
                            self.timeout_seconds,
                            self.budget.prepare_timeout_seconds,
                        ),
                    )
                    opened_fence = getattr(self._connection, "_source_fence", source_fence)
                    if opened_fence != source_fence:
                        connection = self._connection
                        self._connection = None
                        if connection is not None:
                            connection.close()
                        raise ImmutableSQLiteUnavailable(
                            "SQLite owner changed before immutable read"
                        )
                    self._source_fence = opened_fence
                    if self.budget.monotonic_clock() >= self._prepare_deadline:
                        connection = self._connection
                        self._connection = None
                        if connection is not None:
                            connection.close()
                        raise SQLiteSnapshotBudgetExceeded("prepare_time")
                    return self._connection

                # The complete fenced input is a lower bound on the copy's
                # footprint. Reject it before creating a directory or opening
                # a destination, not after writing a budget-sized prefix.
                if _sqlite_fence_bytes(source_fence) > self.budget.max_temporary_bytes:
                    raise SQLiteSnapshotBudgetExceeded("temporary_bytes")
                temporary_directory = tempfile.TemporaryDirectory(
                    prefix="neocortex-sqlite-read-",
                    dir=None if self.temp_root is None else os.fspath(self.temp_root),
                )
                budget_state = _SnapshotBudgetState(
                    self.budget,
                    metrics=self._metrics,
                    temporary_root=Path(temporary_directory.name),
                    deadline=self._prepare_deadline,
                )
                budget_state.checkpoint()
                temporary_database = Path(temporary_directory.name) / self.path.name
                _copy_regular_file_budgeted(self.path, temporary_database, budget_state)
                for suffix, _identity in source_fence.sidecars:
                    source_sidecar = Path(f"{self.path}{suffix}")
                    try:
                        _copy_regular_file_budgeted(
                            source_sidecar,
                            Path(f"{temporary_database}{suffix}"),
                            budget_state,
                        )
                    except FileNotFoundError as exc:
                        if exc.errno != errno.ENOENT or exc.filename != os.fspath(source_sidecar):
                            raise
                        # A sidecar may disappear after the preflight fence
                        # (for example, a writer checkpoints its WAL).  Do
                        # not let the caller misclassify that normal race as
                        # a missing main database: discard this candidate and
                        # recapture a fresh fence on the bounded retry.
                        raise _SQLiteSnapshotSidecarRace(
                            "SQLite owner sidecar disappeared while creating "
                            f"a temporary snapshot: {source_sidecar.name}"
                        ) from exc
                if source_fence != capture_sqlite_read_fence(self.path):
                    raise ImmutableSQLiteUnavailable(
                        f"SQLite owner changed while creating temporary snapshot: {self.path}"
                    )
                _materialize_temporary_database(
                    temporary_database,
                    timeout_seconds=min(
                        self.timeout_seconds,
                        self.budget.prepare_timeout_seconds,
                    ),
                    budget_state=budget_state,
                )
                budget_state.checkpoint()
                self._connection = open_immutable_sqlite_connection(
                    temporary_database,
                    timeout_seconds=min(
                        self.timeout_seconds,
                        self.budget.prepare_timeout_seconds,
                    ),
                )
                budget_state.checkpoint()
                self._temporary_directory = temporary_directory
                self._temporary_database = temporary_database
                return self._connection
            except BaseException as exc:
                last_error = exc
                # A snapshot connection can already exist when the final
                # preparation checkpoint rejects the candidate.  Close it
                # before removing its temporary owner directory; otherwise
                # the fenced connection retains a handle to a path that has
                # already disappeared, and a failed ``__enter__`` cannot
                # reach ``__exit__`` to perform the cleanup later.
                if self._connection is not None:
                    connection = self._connection
                    self._connection = None
                    try:
                        connection.close()
                    except BaseException as cleanup_error:
                        exc.add_note(
                            "temporary SQLite snapshot connection cleanup failed: "
                            f"{type(cleanup_error).__name__}: {cleanup_error}"
                        )
                if temporary_directory is not None:
                    try:
                        temporary_directory.cleanup()
                    except BaseException as cleanup_error:
                        exc.add_note(
                            "temporary SQLite snapshot cleanup failed: "
                            f"{type(cleanup_error).__name__}: {cleanup_error}"
                        )
                # Retry only a fence race.  An active WAL, a symlink, an
                # invalid owner, and every other deterministic safety failure
                # must retain its actionable reason on the first attempt.
                if isinstance(exc, _SQLiteSnapshotSidecarRace) or (
                    isinstance(exc, ImmutableSQLiteUnavailable)
                    and (
                        "changed before immutable read" in str(exc)
                        or "changed while creating temporary snapshot" in str(exc)
                    )
                ):
                    time.sleep(0.01)
                    continue
                raise
            finally:
                elapsed = self.budget.monotonic_clock() - attempt_started
                self._metrics.prepare_time_seconds += max(0.0, float(elapsed))
        assert last_error is not None
        raise ImmutableSQLiteUnavailable(
            f"SQLite owner changed while creating a stable temporary snapshot: {self.path}"
        ) from last_error

    def close(self) -> None:
        """Close and verify the source fence, then remove temp snapshot bytes."""

        connection = self._connection
        self._connection = None
        primary_error: BaseException | None = None
        try:
            if connection is not None:
                try:
                    connection.close()
                except BaseException as exc:
                    primary_error = exc
            # A temporary snapshot is intentionally detached from subsequent
            # source-owner writes; only strict immutable readers require the
            # source fence to remain unchanged through close.
            if (
                connection is not None
                and self._source_fence is not None
                and self.mode is SQLiteReadMode.IMMUTABLE_STRICT
            ):
                try:
                    _verify_immutable_source(self.path, self._source_fence)
                except BaseException as exc:
                    if primary_error is None:
                        primary_error = exc
                    elif exc is not primary_error:
                        primary_error.add_note(f"SQLite final source fence failed: {exc}")
        finally:
            temporary_directory = self._temporary_directory
            self._temporary_directory = None
            self._temporary_database = None
            if temporary_directory is not None:
                try:
                    temporary_directory.cleanup()
                except BaseException as cleanup_error:
                    if primary_error is None:
                        primary_error = cleanup_error
                    else:
                        primary_error.add_note(
                            "temporary SQLite snapshot cleanup failed: "
                            f"{type(cleanup_error).__name__}: {cleanup_error}"
                        )
        if primary_error is not None:
            raise primary_error


class _OwnedSnapshotConnection(sqlite3.Connection):
    """Connection facade that owns the temporary snapshot session it reads."""

    _owner_session: SQLiteReadSession | None = None

    def close(self) -> None:
        session = self._owner_session
        self._owner_session = None
        primary: BaseException | None = None
        try:
            super().close()
        except BaseException as exc:
            primary = exc
        if session is not None:
            try:
                session.close()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(
                        f"temporary SQLite snapshot cleanup failed: {type(exc).__name__}: {exc}"
                    )
        if primary is not None:
            raise primary


def open_sidecar_safe_sqlite_connection(
    path: str | Path,
    *,
    timeout_seconds: float = 60.0,
    max_attempts: int = 2,
    budget: SQLiteSnapshotBudget | None = None,
    max_temporary_bytes: int | None = None,
    cancellation_check: Callable[[], bool | None] | None = None,
    force_snapshot: bool = False,
) -> sqlite3.Connection:
    """Return a bare connection while retaining safe snapshot ownership.

    A few legacy factories expose a connection rather than a context manager.
    Strict owners retain their final fence on close; any sidecars are copied to a
    temporary owner and the returned connection closes that session together
    with its own SQLite handle.  New code should prefer ``sqlite_read_session``
    when it can own the context explicitly.  ``force_snapshot`` keeps a
    caller that knows an owner may change between coordination heartbeats on
    the detached snapshot path even when no sidecar is visible at selection.
    """

    if not isinstance(force_snapshot, bool):
        raise TypeError("force_snapshot must be a boolean")
    selected = Path(path)
    mode = SQLiteReadMode.SNAPSHOT_TEMP if force_snapshot else preferred_sqlite_read_mode(selected)
    if mode is SQLiteReadMode.IMMUTABLE_STRICT:
        # Reuse the same session kernel for strict owners so cancellation and
        # the preparation deadline cannot be bypassed by the legacy bare-
        # connection facade.  The fenced connection owns its source fence.
        session = SQLiteReadSession(
            selected,
            mode=SQLiteReadMode.IMMUTABLE_STRICT,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            budget=budget,
            max_temporary_bytes=max_temporary_bytes,
            cancellation_check=cancellation_check,
        )
        return session.open()
    session = SQLiteReadSession(
        selected,
        mode=SQLiteReadMode.SNAPSHOT_TEMP,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        budget=budget,
        max_temporary_bytes=max_temporary_bytes,
        cancellation_check=cancellation_check,
    )
    session.open()
    temporary = session.temporary_database
    if temporary is None:
        session.close()
        raise ImmutableSQLiteUnavailable("temporary SQLite snapshot path is unavailable")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{readonly_sqlite_uri(temporary)}&immutable=1",
            uri=True,
            timeout=float(timeout_seconds),
            factory=_OwnedSnapshotConnection,
        )
        _configure_read_connection(
            connection,
            timeout_seconds=float(timeout_seconds),
            label="SQLite temporary snapshot read",
        )
        assert isinstance(connection, _OwnedSnapshotConnection)
        connection._owner_session = session
        return connection
    except BaseException as exc:
        if connection is not None:
            try:
                connection.close()
            except BaseException as cleanup_error:
                exc.add_note(f"SQLite snapshot handle cleanup failed: {cleanup_error}")
        try:
            session.close()
        except BaseException as cleanup_error:
            exc.add_note(f"SQLite snapshot session cleanup failed: {cleanup_error}")
        raise


@contextmanager
def sqlite_read_session(
    path: str | Path,
    *,
    mode: SQLiteReadMode | str = SQLiteReadMode.IMMUTABLE_STRICT,
    timeout_seconds: float = 60.0,
    temp_root: str | Path | None = None,
    max_attempts: int = 2,
    budget: SQLiteSnapshotBudget | None = None,
    max_temporary_bytes: int | None = None,
    cancellation_check: Callable[[], bool | None] | None = None,
    generation: object | None = None,
) -> Iterator[sqlite3.Connection]:
    """Convenience context manager backed by :class:`SQLiteReadSession`."""

    with SQLiteReadSession(
        path,
        mode=mode,
        timeout_seconds=timeout_seconds,
        temp_root=temp_root,
        max_attempts=max_attempts,
        budget=budget,
        max_temporary_bytes=max_temporary_bytes,
        cancellation_check=cancellation_check,
        generation=generation,
    ) as connection:
        yield connection


@contextmanager
def immutable_sqlite_database(
    path: Path,
    *,
    timeout_seconds: float = 60.0,
) -> Iterator[sqlite3.Connection]:
    """Read one stable owner without creating, deleting, or touching sidecars."""

    with sqlite_read_session(
        path,
        mode=SQLiteReadMode.IMMUTABLE_STRICT,
        timeout_seconds=timeout_seconds,
    ) as connection:
        yield connection


__all__ = [
    "DEFAULT_SQLITE_SNAPSHOT_BLOCK_BYTES",
    "DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES",
    "DEFAULT_SQLITE_SNAPSHOT_PREPARE_TIMEOUT_SECONDS",
    "SQLITE_ROLLBACK_PENDING_LOCK_OFFSET",
    "SQLITE_ROLLBACK_RESERVED_LOCK_OFFSET",
    "SQLITE_WAL_CHECKPOINT_LOCK_OFFSET",
    "SQLITE_WAL_EMPTY_BYTES",
    "SQLITE_WAL_RECOVERY_LOCK_OFFSET",
    "SQLITE_WAL_SHM_RESIDUAL_BYTES",
    "SQLITE_WAL_WRITE_LOCK_OFFSET",
    "ImmutableSQLiteUnavailable",
    "SQLiteFileIdentity",
    "SQLiteImmutableFence",
    "SQLiteReadMode",
    "SQLiteReadSession",
    "SQLiteSnapshotBudget",
    "SQLiteSnapshotBudgetExceeded",
    "SQLiteSnapshotMetrics",
    "SQLiteSnapshotOperationMetrics",
    "SQLiteSnapshotReuseCache",
    "capture_sqlite_immutable_fence",
    "capture_sqlite_read_fence",
    "immutable_sqlite_database",
    "open_immutable_sqlite_connection",
    "open_sidecar_safe_sqlite_connection",
    "preferred_sqlite_read_mode",
    "require_inactive_sqlite_sidecars",
    "sqlite_read_session",
]
