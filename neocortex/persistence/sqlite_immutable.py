"""Fenced SQLite reads that never modify a published owner.

The public read surfaces must not use SQLite's usual ``mode=ro`` URI directly:
even a read-only connection may create ``-shm``/``-wal`` files, and a close may
checkpoint a writer-owned WAL.  :class:`SQLiteReadSession` centralizes the
two safe read strategies used by the product:

* ``immutable_strict`` reads an already quiescent owner with SQLite's
  ``immutable=1`` flag and a before/after filesystem fence.
* ``snapshot_temp`` copies a bounded, stable set of main/sidecar bytes into a
  system temporary directory and reads the copy.  This is the only supported
  read strategy when a live WAL or rollback journal exists.

``writer_coordinated`` is represented in the mode enum for callers that need
to make the boundary explicit, but is intentionally not opened by this
read-only class; a writer must supply its own transaction/lock owner.
"""

from __future__ import annotations
import sqlite3
import stat
import os
import shutil
import tempfile
import time
import math
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator, Literal

from neocortex.persistence.sqlite_paths import readonly_sqlite_uri


class ImmutableSQLiteUnavailable(RuntimeError):
    """A database cannot be proven safe for an immutable read."""


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
class _ReusableSnapshot:
    session: "SQLiteReadSession"
    references: int = 0


class SQLiteSnapshotReuseCache:
    """Reuse one fenced snapshot within a caller-owned logical operation.

    The cache is deliberately ephemeral: entries are keyed by the source fence
    and caller generation, and ``close`` releases every session.  It never
    survives a process or becomes a second durable state store.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, object, SQLiteImmutableFence], _ReusableSnapshot] = {}

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
        selected = Path(path).absolute()
        fence = capture_sqlite_read_fence(selected)
        key = (str(selected), generation, fence)
        entry = self._entries.get(key)
        if entry is None:
            session = SQLiteReadSession(
                selected,
                mode=mode,
                timeout_seconds=timeout_seconds,
                temp_root=temp_root,
                budget=budget,
                generation=generation,
            )
            session.open()
            entry = _ReusableSnapshot(session)
            self._entries[key] = entry
        else:
            entry.session.metrics.reused_views += 1
        entry.references += 1
        try:
            yield entry.session.connection
        finally:
            entry.references -= 1

    def close(self) -> None:
        primary: BaseException | None = None
        for entry in tuple(self._entries.values()):
            try:
                entry.session.close()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(f"reused SQLite snapshot cleanup failed: {exc}")
        self._entries.clear()
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
    ) -> None:
        self.budget = budget
        self.deadline = budget.monotonic_clock() + budget.prepare_timeout_seconds
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
        callback = self.budget.cancellation_check
        if callback is not None:
            try:
                decision = callback()
            except BaseException:
                self.metrics.cancelled = True
                raise
            if decision is not None and decision is not False:
                self.metrics.cancelled = True
                raise SQLiteSnapshotBudgetExceeded("cancelled")
        if self.budget.monotonic_clock() >= self.deadline:
            raise SQLiteSnapshotBudgetExceeded("prepare_time")
        observed = self._tree_bytes()
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
    if not stat.S_ISREG(value.st_mode) or (value.st_size <= 0 and not allow_empty):
        raise ImmutableSQLiteUnavailable(f"{label} is not a stable regular file: {path.name}")
    return SQLiteFileIdentity(
        device=int(value.st_dev),
        inode=int(value.st_ino),
        mode=int(value.st_mode),
        size=int(value.st_size),
        mtime_ns=int(value.st_mtime_ns),
        ctime_ns=int(value.st_ctime_ns),
    )


def capture_sqlite_read_fence(path: Path) -> SQLiteImmutableFence:
    """Capture main/sidecar identities without opening SQLite.

    Unlike :func:`capture_sqlite_immutable_fence`, this primitive accepts an
    active sidecar set so the snapshot strategy can copy it.  It still rejects
    symlinks, non-regular files, and inaccessible entries.
    """

    selected = Path(path)
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

    fence = capture_sqlite_read_fence(path)
    require_inactive_sqlite_sidecars(fence)
    return fence


def require_inactive_sqlite_sidecars(fence: SQLiteImmutableFence) -> None:
    """Require no sidecars; sizes alone cannot establish writer quiescence.

    A writer holding ``BEGIN IMMEDIATE`` can have an empty WAL and a 32 KiB
    SHM.  Such owners need a temporary snapshot, not an immutable source read.
    This filesystem preflight is not a replacement for owner coordination.
    """

    sidecars = dict(fence.sidecars)
    journal = sidecars.get("-journal")
    wal = sidecars.get("-wal")
    if journal is not None and journal.size > 0:
        raise ImmutableSQLiteUnavailable("SQLite owner has a non-empty rollback journal")
    if wal is not None and wal.size > 0:
        raise ImmutableSQLiteUnavailable("SQLite owner has a non-empty WAL")
    if not sidecars:
        return
    raise ImmutableSQLiteUnavailable("SQLite owner sidecars are not proven inactive")


def preferred_sqlite_read_mode(path: str | Path) -> SQLiteReadMode:
    """Choose strict or temporary-copy mode from filesystem-only evidence."""

    fence = capture_sqlite_read_fence(Path(path))
    try:
        require_inactive_sqlite_sidecars(fence)
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


def _verify_immutable_source(path: Path, fence: SQLiteImmutableFence) -> None:
    try:
        after = capture_sqlite_read_fence(path)
    except (OSError, ImmutableSQLiteUnavailable) as exc:
        raise ImmutableSQLiteUnavailable(
            "SQLite owner changed during immutable read"
        ) from exc
    if after != fence:
        raise ImmutableSQLiteUnavailable("SQLite owner changed during immutable read")


class _FencedImmutableConnection(sqlite3.Connection):
    """Keep the after-read fence even for factories returning a bare handle."""

    _source_path: Path | None = None
    _source_fence: SQLiteImmutableFence | None = None

    def close(self) -> None:
        path, fence = self._source_path, self._source_fence
        self._source_path = None
        self._source_fence = None
        primary: BaseException | None = None
        try:
            super().close()
        except BaseException as exc:
            primary = exc
        if path is not None and fence is not None:
            try:
                _verify_immutable_source(path, fence)
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(f"SQLite final source fence failed: {exc}")
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
    fence = capture_sqlite_immutable_fence(selected)
    if fence != capture_sqlite_immutable_fence(selected):
        raise ImmutableSQLiteUnavailable("SQLite owner changed before immutable read")
    connection = sqlite3.connect(
        f"{readonly_sqlite_uri(selected)}&immutable=1",
        uri=True,
        timeout=float(timeout_seconds),
        factory=_FencedImmutableConnection,
    )
    try:
        _configure_read_connection(
            connection,
            timeout_seconds=float(timeout_seconds),
            label="SQLite immutable read",
        )
        assert isinstance(connection, _FencedImmutableConnection)
        connection._source_path = selected
        connection._source_fence = fence
        return connection
    except BaseException as exc:
        try:
            connection.close()
        except BaseException as cleanup_error:
            # Preserve the configuration/open failure as the primary error;
            # connection cleanup is diagnostic only.
            exc.add_note(
                "SQLite immutable connection cleanup failed: "
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
        with os.fdopen(source_fd, "rb", closefd=True) as source_stream, destination.open(
            "wb"
        ) as destination_stream:
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
            budget_error: BaseException | None = None

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
                raise ImmutableSQLiteUnavailable("SQLite snapshot temp root is not a real directory")

        last_error: BaseException | None = None
        for _attempt in range(self.max_attempts):
            self._metrics.attempts += 1
            attempt_started = self.budget.monotonic_clock()
            temporary_directory: tempfile.TemporaryDirectory[str] | None = None
            budget_state: _SnapshotBudgetState | None = None
            try:
                source_fence = capture_sqlite_read_fence(self.path)
                self._source_fence = source_fence
                if self.mode is SQLiteReadMode.IMMUTABLE_STRICT:
                    if self.budget.cancellation_check is not None:
                        # Strict reads do not allocate temporary bytes, but a
                        # caller's operation-level cancellation still applies.
                        callback_result = self.budget.cancellation_check()
                        if callback_result is not None and callback_result is not False:
                            self._metrics.cancelled = True
                            raise SQLiteSnapshotBudgetExceeded("cancelled")
                    require_inactive_sqlite_sidecars(source_fence)
                    self._connection = open_immutable_sqlite_connection(
                        self.path,
                        timeout_seconds=min(
                            self.timeout_seconds,
                            self.budget.prepare_timeout_seconds,
                        ),
                    )
                    return self._connection

                temporary_directory = tempfile.TemporaryDirectory(
                    prefix="neocortex-sqlite-read-",
                    dir=None if self.temp_root is None else os.fspath(self.temp_root),
                )
                budget_state = _SnapshotBudgetState(
                    self.budget,
                    metrics=self._metrics,
                    temporary_root=Path(temporary_directory.name),
                )
                budget_state.checkpoint()
                temporary_database = Path(temporary_directory.name) / self.path.name
                _copy_regular_file_budgeted(self.path, temporary_database, budget_state)
                for suffix, _identity in source_fence.sidecars:
                    _copy_regular_file_budgeted(
                        Path(f"{self.path}{suffix}"),
                        Path(f"{temporary_database}{suffix}"),
                        budget_state,
                    )
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
                self._temporary_directory = temporary_directory
                self._temporary_database = temporary_database
                return self._connection
            except BaseException as exc:
                last_error = exc
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
                if isinstance(exc, ImmutableSQLiteUnavailable) and (
                    "changed before immutable read" in str(exc)
                    or "changed while creating temporary snapshot" in str(exc)
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
                        "temporary SQLite snapshot cleanup failed: "
                        f"{type(exc).__name__}: {exc}"
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
) -> sqlite3.Connection:
    """Return a bare connection while retaining safe snapshot ownership.

    A few legacy factories expose a connection rather than a context manager.
    Strict owners retain their final fence on close; any sidecars are copied to a
    temporary owner and the returned connection closes that session together
    with its own SQLite handle.  New code should prefer ``sqlite_read_session``
    when it can own the context explicitly.
    """

    selected = Path(path)
    mode = preferred_sqlite_read_mode(selected)
    if mode is SQLiteReadMode.IMMUTABLE_STRICT:
        return open_immutable_sqlite_connection(selected, timeout_seconds=timeout_seconds)
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
    "ImmutableSQLiteUnavailable",
    "SQLiteFileIdentity",
    "SQLiteImmutableFence",
    "SQLiteReadMode",
    "SQLiteReadSession",
    "SQLiteSnapshotBudget",
    "SQLiteSnapshotBudgetExceeded",
    "SQLiteSnapshotMetrics",
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
