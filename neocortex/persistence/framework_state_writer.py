"""Single-writer lifecycle, routing snapshot, and cache repository."""
# region [00] Contexto del módulo
# Módulo: neocortex/framework_state_writer.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from neocortex.persistence.operational_freshness import operational_identity_floor
import json
import os
import sqlite3
import stat
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, closing
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from neocortex.deduplication import FileSnapshot
from neocortex.platform.policy import sqlite_path_collation

from neocortex.platform.content_types import DetectedType
from neocortex.safety.corpus_access import (
    CorpusAccessPolicy,
)
from neocortex.persistence.framework_schema import initialize_framework_schema
from neocortex.persistence.sqlite_immutable import (
    DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES,
    DEFAULT_SQLITE_SNAPSHOT_PREPARE_TIMEOUT_SECONDS,
    ImmutableSQLiteUnavailable,
    SQLiteSnapshotBudget,
)
from neocortex.persistence.sqlite_writer_snapshot import (
    SQLiteProgressConnection,
    writer_coordinated_sqlite_snapshot,
)
from neocortex.persistence.sqlite_paths import existing_sqlite_uri
from neocortex.persistence.sqlite_connection import (
    STATE_FILE_MODE,
    ensure_private_sqlite_sidecars,
    ensure_private_state_directory,
    private_state_creation,
)
from neocortex.persistence.framework_state_types import (
    CONTENT_TYPE_CACHE_LOOKUP_BATCH_SIZE,
    CONTENT_TYPE_CACHE_WRITE_BATCH_SIZE,
    DurableInventoryBinding as _DurableInventoryBinding,
    DurableInventoryOwner as _DurableInventoryOwner,
    InventoryRunEvidence as _InventoryRunEvidence,
    RunBudgetExceeded as _RunBudgetExceeded,
    bounded_lifecycle_name as _bounded_lifecycle_name,
)
# endregion [01]

# region [02] Implementación

_PATH_COLLATION = sqlite_path_collation()
_ROUTE_SNAPSHOT_BATCH_SIZE = 256
# Keep cache lookups below SQLite's smallest commonly-supported variable limit
# while still making one bounded lookup for the action inventory page.  The
# query uses two variables per identity plus the detector version.
_CONTENT_TYPE_CACHE_LOOKUP_BATCH_SIZE = CONTENT_TYPE_CACHE_LOOKUP_BATCH_SIZE
_CONTENT_TYPE_CACHE_WRITE_BATCH_SIZE = CONTENT_TYPE_CACHE_WRITE_BATCH_SIZE


class _RouteSnapshotBudget(Protocol):
    """Small budget surface used by the bounded route projection."""

    def checkpoint(self) -> None: ...

    def before_write(self, size: int) -> None: ...


_ROUTE_CANDIDATE_PROJECTION_TABLE = f"""
CREATE TABLE route_candidates (
    run_id INTEGER NOT NULL,
    mime TEXT NOT NULL,
    path TEXT NOT NULL COLLATE {_PATH_COLLATION},
    volume_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    birthtime_ns INTEGER NOT NULL,
    PRIMARY KEY(run_id, path)
) WITHOUT ROWID
"""

_ROUTE_CANDIDATE_PROJECTION_INDEX = """
CREATE INDEX route_candidates_mime_idx
    ON route_candidates(run_id, mime, path)
"""

_ROUTE_REVIEW_PROJECTION_TABLE = """
CREATE TABLE findings (
    route_name TEXT NOT NULL,
    volume_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    status TEXT NOT NULL,
    recommendation TEXT NOT NULL
)
"""

_ROUTE_REVIEW_PROJECTION_INDEX = """
CREATE INDEX findings_status_idx
    ON findings(status, recommendation, route_name)
"""

_ROUTE_REVIEW_PROJECTION_IDENTITY_INDEX = """
CREATE INDEX findings_identity_idx
    ON findings(route_name, volume_id, file_id, status, recommendation)
"""

_ROUTE_INITIAL_RUN_PROJECTION_SCHEMA = """
CREATE TABLE initial_runs (
    run_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL
);
"""

_ROUTE_PHASE_PROJECTION_SCHEMA = """
CREATE TABLE route_phase_runs (
    run_id INTEGER NOT NULL,
    route_name TEXT NOT NULL,
    phase_name TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY(run_id, route_name, phase_name)
) WITHOUT ROWID;
"""


def _sqlite_projection_value_bytes(value: object) -> int:
    """Return a conservative bounded estimate for one SQLite value."""

    if value is None:
        return 16
    if isinstance(value, bytes):
        return len(value) + 32
    if isinstance(value, str):
        return len(value.encode("utf-8")) + 32
    if isinstance(value, (int, float)):
        return 32
    return len(str(value).encode("utf-8")) + 32


def _copy_route_projection_rows(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    budget: _RouteSnapshotBudget,
    *,
    select_sql: str,
    select_parameters: tuple[object, ...],
    insert_sql: str,
) -> None:
    """Copy one projection stream without materializing its complete result."""

    budget.checkpoint()
    with closing(source.execute(select_sql, select_parameters)) as rows:
        while True:
            budget.checkpoint()
            batch = rows.fetchmany(_ROUTE_SNAPSHOT_BATCH_SIZE)
            if not batch:
                return
            # The target is journal-free, but SQLite still allocates complete pages
            # for a write.  Reserve a conservative payload estimate before each
            # bounded batch; the max_page_count guard in the caller is the hard
            # ceiling and the following checkpoint verifies the actual footprint.
            estimate = sum(
                _sqlite_projection_value_bytes(value)
                for row in batch
                for value in row
            )
            budget.before_write(max(4096, (estimate * 2) + 8192))
            target.executemany(insert_sql, batch)
            budget.checkpoint()


def _project_route_candidate_view(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    budget: _RouteSnapshotBudget,
    *,
    run_id: int,
) -> None:
    """Build the minimal immutable database consumed by concurrent routes.

    The Framework owner also stores historical events, caches, recovery data,
    and review evidence.  None of those bytes are route input.  This projection
    retains only the current run's candidates, open review recommendations,
    and the terminal phase rows needed by an explicitly bound resume source.
    The source connection is the already-owned, pinned writer view; no source
    reader or corpus access is opened here.
    """

    target.execute(_ROUTE_CANDIDATE_PROJECTION_TABLE)
    target.execute(_ROUTE_CANDIDATE_PROJECTION_INDEX)
    target.execute(_ROUTE_REVIEW_PROJECTION_TABLE)
    target.execute(_ROUTE_REVIEW_PROJECTION_INDEX)
    target.execute(_ROUTE_REVIEW_PROJECTION_IDENTITY_INDEX)
    target.execute(_ROUTE_INITIAL_RUN_PROJECTION_SCHEMA)
    target.execute(_ROUTE_PHASE_PROJECTION_SCHEMA)
    budget.checkpoint()

    _copy_route_projection_rows(
        source,
        target,
        budget,
        select_sql="""
            SELECT run_id,mime,path,volume_id,file_id,size,mtime_ns,birthtime_ns
            FROM route_candidates
            WHERE run_id=?
            ORDER BY path
        """,
        select_parameters=(run_id,),
        insert_sql="""
            INSERT INTO route_candidates(
                run_id,mime,path,volume_id,file_id,size,mtime_ns,birthtime_ns
            ) VALUES(?,?,?,?,?,?,?,?)
        """,
    )

    # Only open rows bound to a candidate in this run can satisfy the route
    # selection EXISTS predicate.  Joining against the current generation is
    # important: review history may be much larger than the route view and
    # must not silently consume the bounded snapshot budget.
    _copy_route_projection_rows(
        source,
        target,
        budget,
        select_sql="""
            SELECT r.route_name,r.volume_id,r.file_id,r.status,r.recommendation
            FROM findings r
            WHERE r.status='open' AND EXISTS(
                SELECT 1 FROM route_candidates c
                WHERE c.run_id=?
                  AND c.volume_id=r.volume_id
                  AND c.file_id=r.file_id
            )
            ORDER BY route_name,volume_id,file_id,recommendation
        """,
        select_parameters=(run_id,),
        insert_sql="""
            INSERT INTO findings(
                route_name,volume_id,file_id,status,recommendation
            ) VALUES(?,?,?,?,?)
        """,
    )

    # A resume route may need to inspect the terminal source run through the
    # detached candidate database.  Discover and copy only that source row and
    # its completed phase evidence; a malformed source id is fail-closed.
    source_row = source.execute(
        "SELECT source_run_id FROM initial_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    source_run_id = None
    if source_row is not None and source_row[0] is not None:
        try:
            source_run_id = int(source_row[0])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ImmutableSQLiteUnavailable(
                "framework route snapshot source run identity is invalid"
            ) from exc
        if source_run_id <= 0:
            raise ImmutableSQLiteUnavailable(
                "framework route snapshot source run identity is invalid"
            )
    run_ids = (run_id,) if source_run_id is None else (run_id, source_run_id)
    placeholders = ",".join("?" for _ in run_ids)
    _copy_route_projection_rows(
        source,
        target,
        budget,
        select_sql=(
            "SELECT run_id,status FROM initial_runs "
            f"WHERE run_id IN ({placeholders}) ORDER BY run_id"
        ),
        select_parameters=run_ids,
        insert_sql="INSERT INTO initial_runs(run_id,status) VALUES(?,?)",
    )
    _copy_route_projection_rows(
        source,
        target,
        budget,
        select_sql=(
            "SELECT run_id,route_name,phase_name,status "
            "FROM route_phase_runs "
            f"WHERE run_id IN ({placeholders}) ORDER BY run_id,route_name,phase_name"
        ),
        select_parameters=run_ids,
        insert_sql=(
            "INSERT INTO route_phase_runs(run_id,route_name,phase_name,status) "
            "VALUES(?,?,?,?)"
        ),
    )


RunBudgetExceeded = _RunBudgetExceeded
RunBudgetExceeded.__module__ = __name__
_bounded_lifecycle_name = _bounded_lifecycle_name

if TYPE_CHECKING:

    class ActionSummary(Protocol):
        """Read-only action counters persisted by the state writer."""

        @property
        def apply_actions(self) -> bool: ...

        @property
        def duplicate_candidates(self) -> int: ...

        @property
        def duplicates_trashed(self) -> int: ...

        @property
        def duplicate_skips(self) -> int: ...

        @property
        def files_checked(self) -> int: ...

        @property
        def types_detected(self) -> int: ...

        @property
        def extensions_matching(self) -> int: ...

        @property
        def unknown_types(self) -> int: ...

        @property
        def type_cache_hits(self) -> int: ...

        @property
        def type_cache_misses(self) -> int: ...

        @property
        def type_cache_pruned(self) -> int: ...

        @property
        def stale_inventory(self) -> int: ...

        @property
        def rename_candidates(self) -> int: ...

        @property
        def files_renamed(self) -> int: ...

        @property
        def rename_skips(self) -> int: ...

        @property
        def empty_directory_candidates(self) -> int: ...

        @property
        def empty_directories_trashed(self) -> int: ...

        @property
        def empty_directory_skips(self) -> int: ...

        @property
        def errors(self) -> int: ...

        @property
        def third_party_candidates(self) -> int: ...

        @property
        def third_party_trashed(self) -> int: ...

        @property
        def third_party_skips(self) -> int: ...


InventoryRunEvidence = _InventoryRunEvidence
InventoryRunEvidence.__module__ = __name__
DurableInventoryBinding = _DurableInventoryBinding
DurableInventoryBinding.__module__ = __name__
DurableInventoryOwner = _DurableInventoryOwner
DurableInventoryOwner.__module__ = __name__


def read_latest_durable_inventory_owner(
    database: str | Path,
    root: Path,
) -> DurableInventoryOwner | None:
    """Read one quiescent owner without creating SQLite WAL sidecars."""

    database_path = Path(database)
    if not database_path.is_file():
        return None
    # A normal ``mode=ro`` connection to a sidecar-free WAL database recreates
    # an empty ``-wal`` plus ``-shm`` on Windows.  The watcher calls this reader
    # between integrated runs, so use the fenced immutable snapshot contract:
    # it both abstains from active state and preserves quiescence after reading.
    from neocortex.persistence.sqlite_immutable import immutable_sqlite_database

    with immutable_sqlite_database(database_path, timeout_seconds=60) as connection:
        try:
            row = connection.execute(
                f"""SELECT run_id,scan_id,corpus_access_mode,
                inventory_policy_signature,journal_volume,journal_id,end_usn,
                root,root_device_id_hex,root_file_id_hex,root_birthtime_ns
                FROM initial_runs
                WHERE root=? COLLATE {_PATH_COLLATION} AND status='completed'
                AND scan_id IS NOT NULL
                AND run_kind='initial' AND run_id>?
                ORDER BY run_id DESC LIMIT 1""",
                (str(Path(os.path.abspath(os.path.realpath(root)))), operational_identity_floor(connection, "framework")),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            detail = str(exc).casefold()
            if "no such table" in detail or "no such column" in detail:
                return None
            raise
    if row is None:
        return None
    return DurableInventoryOwner(
        DurableInventoryBinding(
            run_id=int(row[0]),
            scan_id=int(row[1]),
            corpus_access_mode=str(row[2]),
            inventory_policy_signature=(None if row[3] is None else str(row[3])),
            end_cursor=None,
        ),
        CorpusAccessPolicy.from_storage(
            str(row[2]),
            str(row[7]),
            None if row[8] is None else str(row[8]),
            None if row[9] is None else str(row[9]),
            None if row[10] is None else int(row[10]),
        ),
    )


def _acquire_framework_writer(
    path: Path, *, existing_only: bool
) -> tuple[sqlite3.Connection, tuple[int, int] | None]:
    """Bind SQLite acquisition to an existing or exclusively created inode."""

    if str(path) == ":memory:" and not existing_only:
        return sqlite3.connect(path, timeout=60, factory=SQLiteProgressConnection), None
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            if existing_only:
                raise
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                STATE_FILE_MODE,
            )
    except OSError as exc:
        raise sqlite3.OperationalError(f"unable to open database file: {path}") from exc
    connection: sqlite3.Connection | None = None
    try:
        owner = os.fstat(descriptor)
        if not stat.S_ISREG(owner.st_mode):
            raise ImmutableSQLiteUnavailable("framework SQLite owner is not a regular file")
        target = existing_sqlite_uri(path) if existing_only else path
        connection = sqlite3.connect(
            target, uri=existing_only, timeout=60, factory=SQLiteProgressConnection
        )
        current = path.lstat()
        # No initialization writes have occurred yet. Compare the full file
        # metadata (except atime) against the descriptor held across connect.
        keys = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(current, key) != getattr(owner, key) for key in keys):
            raise ImmutableSQLiteUnavailable("framework SQLite owner changed during connection acquisition")
        return connection, (owner.st_dev, owner.st_ino)
    except BaseException as exc:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error as cleanup:
                exc.add_note(f"framework SQLite acquisition cleanup failed: {cleanup}")
        raise
    finally:
        os.close(descriptor)


from neocortex.persistence.framework_state_content import FrameworkStateContentMixin  # noqa: E402
from neocortex.persistence.framework_state_runs import FrameworkStateRunsMixin  # noqa: E402
from neocortex.persistence.framework_state_routes import FrameworkStateRoutesMixin  # noqa: E402
from neocortex.persistence.framework_state_actions import FrameworkStateActionsMixin  # noqa: E402

class FrameworkState(FrameworkStateContentMixin, FrameworkStateRunsMixin, FrameworkStateRoutesMixin, FrameworkStateActionsMixin):
    """Own the long-lived writer connection for one orchestration run."""

    def __init__(
        self,
        database: str | Path,
        *,
        existing_only: bool = False,
    ):
        self.path = Path(database)
        with private_state_creation():
            if not existing_only and str(self.path) != ":memory:":
                ensure_private_state_directory(self.path)
            self._connection, self._connection_owner_identity = _acquire_framework_writer(
                self.path, existing_only=existing_only
            )
            try:
                if str(self.path) != ":memory:":
                    ensure_private_sqlite_sidecars(self.path)
                self._connection.execute("PRAGMA busy_timeout=60000")
                self._connection.execute("PRAGMA foreign_keys=ON")
                if int(self._connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                    raise RuntimeError("framework state could not enable foreign keys")
                self._initialize()
                if self._connection_owner_identity is not None:
                    owner = self.path.lstat()
                    if (
                        not stat.S_ISREG(owner.st_mode)
                        or (owner.st_dev, owner.st_ino) != self._connection_owner_identity
                    ):
                        raise ImmutableSQLiteUnavailable("framework SQLite owner changed during initialization")
            except BaseException:
                self._connection.close()
                raise

    def _initialize(self) -> None:
        initialize_framework_schema(self._connection, self._backfill_route_phases)

    def route_candidate_snapshot(
        self,
        *,
        run_id: int | None = None,
        generation: object | None = None,
        cancellation_check: Callable[[], bool | None] | None = None,
    ) -> AbstractContextManager[Path]:
        """Lend the owned connection to publish one input view before workers.

        When a run is supplied, the snapshot deadline is the remaining durable
        run deadline rather than a fresh independent timeout.  The caller's
        cancellation token is sampled by the existing snapshot kernel; no
        second lifecycle store is created.
        """

        if self._connection_owner_identity is None:
            raise ImmutableSQLiteUnavailable("route snapshot requires a durable SQLite owner")
        budget: SQLiteSnapshotBudget | None = None
        if run_id is not None:
            durable = self.read_run_budget(run_id)
            timeout = DEFAULT_SQLITE_SNAPSHOT_PREPARE_TIMEOUT_SECONDS
            if durable is not None and durable.get("deadline_ns") is not None:
                remaining = (int(durable["deadline_ns"]) - time.time_ns()) / 1_000_000_000
                timeout = max(0.001, min(timeout, remaining))
            budget = SQLiteSnapshotBudget(
                max_temporary_bytes=DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES,
                prepare_timeout_seconds=timeout,
                cancellation_check=cancellation_check,
            )
        return writer_coordinated_sqlite_snapshot(
            self._connection,
            self.path,
            owner_identity=self._connection_owner_identity,
            projection=(
                None
                if run_id is None
                else lambda source, target, snapshot_budget: _project_route_candidate_view(
                    source,
                    target,
                    snapshot_budget,
                    run_id=run_id,
                )
            ),
            budget=budget,
            generation=run_id if generation is None else generation,
        )

    def _backfill_route_phases(self) -> None:
        """Preserve resumability for phase events written before schema 13."""

        mappings = (
            ("pdf-extraction", "extraction"),
            ("pdf-text-dedup", "text_dedup"),
            ("pdf-derived", "derived"),
        )
        for event_phase, route_phase in mappings:
            self._connection.execute(
                """INSERT OR IGNORE INTO route_phase_runs(
                run_id,route_name,phase_name,status,started_ns,completed_ns,
                heartbeat_ns,summary_json)
                SELECT run_id,'pdf',?,'completed',occurred_ns,occurred_ns,
                occurred_ns,details_json FROM run_events WHERE phase=?""",
                (route_phase, event_phase),
            )

    @staticmethod
    def _content_type_cache_key(
        snapshot: FileSnapshot,
    ) -> tuple[int, int, int, int, int]:
        """Return the complete identity used by the content cache owner."""

        return (
            int(snapshot.volume_id),
            int(snapshot.file_id),
            int(snapshot.size),
            int(snapshot.mtime_ns),
            int(snapshot.birthtime_ns),
        )

    @staticmethod
    def _decode_content_type_cache_row(row: sqlite3.Row | tuple[object, ...]) -> DetectedType | None:
        if row[5] == "unknown":
            return None
        return DetectedType(
            str(row[6]),
            str(row[7]),
            frozenset(json.loads(str(row[8]))),
            str(row[9]),
        )




































    # ------------------------------------------------------------------
    # Durable lifecycle budget
    # ------------------------------------------------------------------









    # A descriptive alias keeps the writer API discoverable for callers that
    # model the ledger as a per-stage view rather than a run-wide budget.
    run_stage_budget = FrameworkStateRunsMixin.read_run_stage_budget

















































    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "FrameworkState":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


# endregion [02]
