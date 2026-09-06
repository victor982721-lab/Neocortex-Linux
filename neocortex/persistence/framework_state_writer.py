"""Single-writer lifecycle, routing snapshot, and cache repository."""
# region [00] Contexto del módulo
# Módulo: neocortex/framework_state_writer.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations
import json
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from neocortex.enumeration import JournalCursor
from neocortex.deduplication import FileSnapshot
from neocortex.platform.policy import sqlite_path_collation

from neocortex.platform.content_types import DetectedType
from neocortex.safety.corpus_access import (
    CorpusAccessPolicy,
    CorpusMutationGuard,
)
from neocortex.workflow.actions.file_action_reconciliation_store import (
    RecordedFileActionReconciliation,
    record_file_action_reconciliation,
)
from neocortex.workflow.actions.file_action_recovery import FileActionReconciliation
from neocortex.persistence.framework_schema import initialize_framework_schema
from neocortex.persistence.sqlite_immutable import (
    DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES,
    DEFAULT_SQLITE_SNAPSHOT_PREPARE_TIMEOUT_SECONDS,
    ImmutableSQLiteUnavailable,
    SQLiteSnapshotBudget,
)
from neocortex.persistence.sqlite_writer_snapshot import writer_coordinated_sqlite_snapshot
from neocortex.persistence.framework_state_common import (
    CACHE_PRUNE_BATCH_SIZE,
    FileActionSpec,
    begin_file_actions,
    confirm_file_actions_applied,
    corpus_mutation_guard,
    finish_file_actions,
    mark_file_actions_applying,
)
from neocortex.persistence.sqlite_paths import existing_sqlite_uri
from neocortex.persistence.framework_connection import connect_existing_framework
from neocortex.runtime.orchestration.run_manifest import (
    RUN_BUDGET_SCHEMA,
    RUN_STAGE_SCHEMA,
    RunBudget,
    verify_event_payload,
)
# endregion [01]

# region [02] Implementación

_PATH_COLLATION = sqlite_path_collation()


class RunBudgetExceeded(RuntimeError):
    """A durable run budget rejected additional work."""

    def __init__(self, reason: str, snapshot: Mapping[str, Any] | None = None):
        self.reason = reason
        self.snapshot = None if snapshot is None else dict(snapshot)
        super().__init__(f"run budget exceeded: {reason}")

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


@dataclass(frozen=True, slots=True)
class InventoryRunEvidence:
    """Validated durable event proving which inventory an initial run prepared."""

    event_id: int
    scan_id: int
    files: int
    reconciliation_records: int
    inventory_attempts: int
    inventory_mode: str


@dataclass(frozen=True, slots=True)
class DurableInventoryBinding:
    """Newest completed inventory owner and its published USN boundary."""

    run_id: int
    scan_id: int
    corpus_access_mode: str
    inventory_policy_signature: str | None
    end_cursor: JournalCursor | None


@dataclass(frozen=True, slots=True)
class DurableInventoryOwner:
    """Read-only watcher view of one binding and its persisted root identity."""

    binding: DurableInventoryBinding
    access_policy: CorpusAccessPolicy


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
                AND run_kind='initial'
                ORDER BY run_id DESC LIMIT 1""",
                (str(Path(os.path.abspath(os.path.realpath(root)))),),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            detail = str(exc).casefold()
            if "no such table" in detail or "no such column" in detail:
                return None
            raise
    if row is None:
        return None
    end_cursor = None
    if all(value is not None for value in row[4:7]):
        end_cursor = JournalCursor(str(row[4]), int(row[5]), int(row[6]))
    return DurableInventoryOwner(
        DurableInventoryBinding(
            run_id=int(row[0]),
            scan_id=int(row[1]),
            corpus_access_mode=str(row[2]),
            inventory_policy_signature=(None if row[3] is None else str(row[3])),
            end_cursor=end_cursor,
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
        return sqlite3.connect(path, timeout=60), None
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            if existing_only:
                raise
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
    except OSError as exc:
        raise sqlite3.OperationalError(f"unable to open database file: {path}") from exc
    connection: sqlite3.Connection | None = None
    try:
        owner = os.fstat(descriptor)
        if not stat.S_ISREG(owner.st_mode):
            raise ImmutableSQLiteUnavailable("framework SQLite owner is not a regular file")
        target = existing_sqlite_uri(path) if existing_only else path
        connection = sqlite3.connect(target, uri=existing_only, timeout=60)
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


class FrameworkState:
    """Own the long-lived writer connection for one orchestration run."""

    def __init__(
        self,
        database: str | Path,
        *,
        existing_only: bool = False,
    ):
        self.path = Path(database)
        self._connection, self._connection_owner_identity = _acquire_framework_writer(
            self.path, existing_only=existing_only
        )
        try:
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

    def get_content_type_cache(
        self, snapshot: FileSnapshot, detector_version: str
    ) -> tuple[bool, DetectedType | None]:
        """Return a metadata-valid detection, including cached unknown results."""

        row = self._connection.execute(
            """SELECT size,mtime_ns,birthtime_ns,status,mime,canonical_extension,
            accepted_extensions_json,evidence FROM content_type_cache
            WHERE volume_id=? AND file_id=? AND detector_version=?""",
            (f"{snapshot.volume_id:x}", f"{snapshot.file_id:x}", detector_version),
        ).fetchone()
        if (
            row is None
            or int(row[0]) != snapshot.size
            or int(row[1]) != snapshot.mtime_ns
            or int(row[2]) != snapshot.birthtime_ns
        ):
            return False, None
        if row[3] == "unknown":
            return True, None
        return True, DetectedType(
            str(row[4]),
            str(row[5]),
            frozenset(json.loads(str(row[6]))),
            str(row[7]),
        )

    def store_content_type_cache(
        self,
        snapshot: FileSnapshot,
        detector_version: str,
        detected: DetectedType | None,
        run_id: int,
    ) -> None:
        """Persist one reusable detector result without reading the file again."""

        self.store_content_type_cache_batch(((snapshot, detected),), detector_version, run_id)

    def store_content_type_cache_batch(
        self,
        rows: Iterable[tuple[FileSnapshot, DetectedType | None]],
        detector_version: str,
        run_id: int,
    ) -> None:
        """Upsert a bounded hit/miss batch in one transaction."""

        updated_ns = time.time_ns()
        with self._connection:
            self._connection.executemany(
                """INSERT OR REPLACE INTO content_type_cache(
                volume_id,file_id,size,mtime_ns,birthtime_ns,detector_version,status,mime,
                canonical_extension,accepted_extensions_json,evidence,last_seen_run_id,
                updated_ns) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    (
                        f"{snapshot.volume_id:x}",
                        f"{snapshot.file_id:x}",
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.birthtime_ns,
                        detector_version,
                        "unknown" if detected is None else "detected",
                        None if detected is None else detected.mime,
                        None if detected is None else detected.canonical_extension,
                        None
                        if detected is None
                        else json.dumps(
                            sorted(detected.accepted_extensions), separators=(",", ":")
                        ),
                        None if detected is None else detected.evidence,
                        run_id,
                        updated_ns,
                    )
                    for snapshot, detected in rows
                ),
            )

    def prune_route_candidates(
        self,
        keep_run_ids: Iterable[int] = (),
    ) -> int:
        """Remove old routing snapshots while preserving explicitly resumable runs."""

        keep = tuple(sorted({int(value) for value in keep_run_ids}))
        removed = 0
        while True:
            if keep:
                placeholders = ",".join("?" for _ in keep)
                rows = self._connection.execute(
                    f"""SELECT run_id,path FROM route_candidates
                    WHERE run_id NOT IN ({placeholders})
                    ORDER BY run_id,path LIMIT 1000""",
                    keep,
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """SELECT run_id,path FROM route_candidates
                    ORDER BY run_id,path LIMIT 1000"""
                ).fetchall()
            if not rows:
                return removed
            with self._connection:
                removed += int(
                    self._connection.executemany(
                        "DELETE FROM route_candidates WHERE run_id=? AND path=?", rows
                    ).rowcount
                )

    def latest_route_candidate_run(self) -> int | None:
        row = self._connection.execute("SELECT MAX(run_id) FROM route_candidates").fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def route_candidate_run_count(self, run_id: int) -> int:
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM route_candidates WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )

    def route_candidate_workload(self, run_id: int) -> tuple[int, int]:
        """Return bounded item/byte counts for a retained route snapshot."""

        row = self._connection.execute(
            """SELECT COUNT(*),COALESCE(SUM(size),0)
            FROM route_candidates WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        return int(row[0]), int(row[1])

    def route_run_count(self, run_id: int) -> int:
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM route_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )

    def has_durable_routing_snapshot(self, run_id: int) -> bool:
        """Return whether a bound scan crossed a durable publication boundary."""

        row = self._connection.execute(
            "SELECT status,scan_id FROM initial_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None or row[1] is None:
            return False
        if str(row[0]) == "completed":
            return True
        marker = self._connection.execute(
            """SELECT 1 FROM run_events WHERE run_id=? AND (
            (phase='routing-snapshot' AND message='Snapshot de rutas publicado') OR
            (phase='inventory-recovery' AND message='Vínculo de inventario recuperado'))
            LIMIT 1""",
            (run_id,),
        ).fetchone()
        if marker is not None:
            return True
        return self.route_run_count(run_id) > 0

    def source_run_scan_id(self, run_id: int) -> int:
        _, scan_id = self.source_run_inventory(run_id)
        if scan_id is None:
            raise ValueError(f"source run {run_id} has no reusable scan")
        return scan_id

    def source_run_inventory(self, run_id: int) -> tuple[Path, int | None]:
        row = self._connection.execute(
            "SELECT root,scan_id FROM initial_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"source run {run_id} does not exist")
        return Path(str(row[0])), None if row[1] is None else int(row[1])

    def source_inventory_policy_signature(self, run_id: int) -> str | None:
        """Return the effective inventory boundary persisted by one source run."""

        row = self._connection.execute(
            "SELECT inventory_policy_signature FROM initial_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"source run {run_id} does not exist")
        return None if row[0] is None else str(row[0])

    def recorded_inventory_evidence(self, run_id: int) -> InventoryRunEvidence:
        """Recover one unambiguous inventory checkpoint from append-only events."""

        rows = self._connection.execute(
            """SELECT event_id,details_json FROM run_events
            WHERE run_id=? AND phase='inventory'
            AND message='Inventario preparado' AND details_json IS NOT NULL
            ORDER BY event_id DESC LIMIT 101""",
            (run_id,),
        ).fetchall()
        if len(rows) > 100:
            raise ValueError(f"source run {run_id} has too many inventory evidence events")

        def strict_integer(details: Mapping[str, Any], name: str) -> int:
            value = details[name]
            if type(value) is not int:
                raise ValueError(f"inventory evidence {name} is not an integer")
            return value

        evidence: list[InventoryRunEvidence] = []
        for event_id, details_json in rows:
            try:
                details = json.loads(str(details_json))
                if not isinstance(details, dict):
                    raise ValueError("inventory evidence is not an object")
                schema = details.get("schema")
                if schema not in {None, "neocortex.inventory-prepared/v1"}:
                    raise ValueError(f"unsupported inventory evidence schema: {schema}")
                scan_id = strict_integer(details, "scan_id")
                files = strict_integer(details, "files")
                reconciliation_records = strict_integer(details, "reconciliation_records")
                inventory_attempts = strict_integer(details, "attempts")
                inventory_mode_value = details["mode"]
                if not isinstance(inventory_mode_value, str):
                    raise ValueError("inventory evidence mode is not a string")
                inventory_mode = inventory_mode_value
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"source run {run_id} has malformed inventory event {event_id}"
                ) from exc
            if (
                scan_id <= 0
                or files < 0
                or reconciliation_records < 0
                or inventory_attempts < 0
                or inventory_mode not in {"full", "incremental"}
            ):
                raise ValueError(f"source run {run_id} has invalid inventory event {event_id}")
            evidence.append(
                InventoryRunEvidence(
                    int(event_id),
                    scan_id,
                    files,
                    reconciliation_records,
                    inventory_attempts,
                    inventory_mode,
                )
            )
        if not evidence:
            raise ValueError(f"source run {run_id} has no validated inventory event evidence")
        evidence_values = {
            (
                item.scan_id,
                item.files,
                item.reconciliation_records,
                item.inventory_attempts,
                item.inventory_mode,
            )
            for item in evidence
        }
        if len(evidence_values) != 1:
            raise ValueError(f"source run {run_id} has ambiguous inventory event evidence")
        return evidence[0]

    def resumable_route_names(self, run_id: int) -> tuple[str, ...]:
        """Return incomplete routes in their original stable order."""

        rows = self._connection.execute(
            """SELECT route_name FROM route_runs WHERE run_id=?
            AND status IN ('running','interrupted','failed','cancelled')
            ORDER BY started_ns,route_name""",
            (run_id,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def run_recovery_plan(self, run_id: int) -> dict[str, Any]:
        """Describe safe recovery inputs without starting another worker.

        Completed routes are explicitly ``skipped``.  Routes left in a
        non-terminal state are candidates for a new run only when their
        retained route inputs still exist; otherwise they are reported as
        ``non_replayable`` and the caller must abstain rather than repeat an
        uncertain effect.
        """

        row = self._connection.execute(
            """SELECT status,run_kind,source_run_id FROM initial_runs
            WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"run {run_id} does not exist")
        routes = self._connection.execute(
            """SELECT route_name,status FROM route_runs
            WHERE run_id=? ORDER BY started_ns,route_name""",
            (run_id,),
        ).fetchall()
        candidate_rows, candidate_bytes = self.route_candidate_workload(run_id)
        route_capabilities = self.read_run_route_capabilities(run_id)
        route_input_sources = self.read_route_input_sources(run_id)
        start_event = self._connection.execute(
            """SELECT details_json FROM run_events
            WHERE run_id=? AND phase='run'
            AND message='Ejecución aislada de rutas iniciada'
            ORDER BY event_id DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        if not route_input_sources and start_event is not None and start_event[0] is not None:
            try:
                details = json.loads(str(start_event[0]))
            except (TypeError, json.JSONDecodeError):
                details = None
            if isinstance(details, Mapping) and isinstance(
                details.get("route_input_sources"), Mapping
            ):
                route_input_sources = {
                    str(name): str(source)
                    for name, source in details["route_input_sources"].items()
                }
        skipped = tuple(str(name) for name, status in routes if str(status) == "completed")
        pending = tuple(
            str(name)
            for name, status in routes
            if str(status) in {"running", "interrupted", "failed", "cancelled"}
        )
        non_replayable = tuple(
            str(name)
            for name, status in routes
            if str(status) in {"failed", "cancelled", "interrupted"}
            and (
                (
                    candidate_rows == 0
                    and route_input_sources.get(str(name), "route_candidates")
                    != "inventory_snapshot"
                )
                or route_capabilities.get(str(name), "safe_replay") == "not_resumable"
            )
        )
        return {
            "run_id": run_id,
            "status": str(row[0]),
            "run_kind": str(row[1]),
            "source_run_id": None if row[2] is None else int(row[2]),
            "resumed": str(row[1]) == "resume",
            "recoverable": str(row[0]) == "interrupted"
            or bool(pending),
            "replayed": bool(skipped),
            "skipped": list(skipped),
            "pending": list(pending),
            "non_replayable": list(non_replayable),
            "route_input_sources": route_input_sources,
            "route_capabilities": route_capabilities,
            "candidate_rows": candidate_rows,
            "candidate_bytes": candidate_bytes,
            "candidates_retained": candidate_rows > 0,
        }

    def copy_route_candidates(self, source_run_id: int, target_run_id: int) -> int:
        """Copy one immutable routing snapshot without walking the filesystem."""

        with self._connection:
            result = self._connection.execute(
                """INSERT OR REPLACE INTO route_candidates(
                run_id,mime,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
                SELECT ?,mime,path,volume_id,file_id,size,mtime_ns,birthtime_ns
                FROM route_candidates WHERE run_id=?""",
                (target_run_id, source_run_id),
            )
        return int(result.rowcount)

    def prune_content_type_cache(self, run_id: int, detector_version: str) -> int:
        """Remove stale detector rows in bounded transactions."""

        removed = 0
        while True:
            rows = self._connection.execute(
                """SELECT volume_id,file_id,detector_version
                FROM content_type_cache
                WHERE detector_version<>? OR last_seen_run_id<>?
                ORDER BY volume_id,file_id,detector_version LIMIT ?""",
                (detector_version, run_id, CACHE_PRUNE_BATCH_SIZE),
            ).fetchall()
            if not rows:
                return removed
            with self._connection:
                removed += int(
                    self._connection.executemany(
                        """DELETE FROM content_type_cache
                        WHERE volume_id=? AND file_id=? AND detector_version=?""",
                        rows,
                    ).rowcount
                )

    def store_route_candidates(
        self,
        run_id: int,
        candidates: Iterable[tuple[str, FileSnapshot]],
    ) -> None:
        """Persist already-detected route inputs in bounded caller batches."""

        with self._connection:
            self._connection.executemany(
                """INSERT OR REPLACE INTO route_candidates(
                    run_id,mime,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
                    VALUES(?,?,?,?,?,?,?,?)""",
                (
                    (
                        run_id,
                        mime,
                        snapshot.path,
                        f"{snapshot.volume_id:x}",
                        f"{snapshot.file_id:x}",
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.birthtime_ns,
                    )
                    for mime, snapshot in candidates
                ),
            )

    def iter_route_candidates(self, run_id: int, mime: str):
        rows = self._connection.execute(
            """SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns
            FROM route_candidates WHERE run_id=? AND mime=? ORDER BY path""",
            (run_id, mime),
        )
        for path, volume_id, file_id, size, mtime_ns, birthtime_ns in rows:
            yield FileSnapshot(
                path,
                int(volume_id, 16),
                int(file_id, 16),
                int(size),
                int(mtime_ns),
                int(birthtime_ns),
            )

    def iter_route_candidates_by_prefix(self, run_id: int, mime_prefix: str):
        """Stream detected route inputs for a MIME family in stable path order."""

        rows = self._connection.execute(
            """SELECT mime,path,volume_id,file_id,size,mtime_ns,birthtime_ns
            FROM route_candidates WHERE run_id=? AND mime LIKE ? ORDER BY path""",
            (run_id, f"{mime_prefix}%"),
        )
        for mime, path, volume_id, file_id, size, mtime_ns, birthtime_ns in rows:
            yield (
                str(mime),
                FileSnapshot(
                    path,
                    int(volume_id, 16),
                    int(file_id, 16),
                    int(size),
                    int(mtime_ns),
                    int(birthtime_ns),
                ),
            )

    def begin_initial_run(
        self,
        root: Path,
        cursor: JournalCursor | None,
        *,
        inventory_policy_signature: str | None = None,
    ) -> int:
        policy = CorpusAccessPolicy.capture("normal", root)
        signature = inventory_policy_signature
        if signature is not None and (
            not signature or signature.strip() != signature or len(signature.encode("utf-8")) > 4096
        ):
            raise ValueError("inventory policy signature must be trimmed and bounded")
        now = time.time_ns()
        journal_values = (
            (None, None, None)
            if cursor is None
            else (cursor.volume, str(cursor.journal_id), cursor.next_usn)
        )
        with self._connection:
            result = self._connection.execute(
                """INSERT INTO initial_runs(
                root,started_ns,status,run_kind,current_phase,owner_pid,heartbeat_ns,
                journal_volume,journal_id,start_usn,corpus_access_mode,
                root_device_id_hex,root_file_id_hex,root_birthtime_ns,state_directory,
                inventory_policy_signature)
                VALUES(?,?,'running','initial','prepare',?,?, ?,?,?, ?,?,?,?,?,?)""",
                (
                    str(policy.root),
                    now,
                    os.getpid(),
                    now,
                    *journal_values,
                    policy.mode,
                    policy.root_device_id_hex,
                    policy.root_file_id_hex,
                    policy.root_birthtime_ns,
                    str(Path(os.path.realpath(self.path.parent))),
                    signature,
                ),
            )
        if result.lastrowid is None:
            raise RuntimeError("SQLite did not return a framework run identifier")
        return int(result.lastrowid)


    def begin_operational_run(
        self,
        root: Path,
        *,
        run_kind: str,
        source_run_id: int,
    ) -> int:
        """Start a route-only or resumed run without inventory side effects."""

        if run_kind not in {"route_only", "resume"}:
            raise ValueError(f"invalid operational run kind: {run_kind}")
        source_status = self._connection.execute(
            "SELECT status FROM initial_runs WHERE run_id=?",
            (source_run_id,),
        ).fetchone()
        if source_status is None:
            raise ValueError(f"source run {source_run_id} does not exist")
        if str(source_status[0]) == "running":
            raise ValueError(f"source run {source_run_id} is still running")
        now = time.time_ns()
        with self._connection:
            result = self._connection.execute(
                """INSERT INTO initial_runs(
                root,started_ns,status,run_kind,source_run_id,current_phase,
                owner_pid,heartbeat_ns,scan_id,journal_volume,journal_id,start_usn,
                end_usn,reconciliation_records,inventory_attempts,inventory_mode,
                corpus_access_mode,root_device_id_hex,root_file_id_hex,
                root_birthtime_ns,state_directory,inventory_policy_signature)
                SELECT ?,?,'running',?,?, 'route_prepare',?,?,scan_id,journal_volume,
                journal_id,start_usn,end_usn,0,0,'reused',corpus_access_mode,
                root_device_id_hex,root_file_id_hex,root_birthtime_ns,state_directory,
                inventory_policy_signature
                FROM initial_runs WHERE run_id=?""",
                (
                    str(root),
                    now,
                    run_kind,
                    source_run_id,
                    os.getpid(),
                    now,
                    source_run_id,
                ),
            )
        if result.lastrowid is None or result.rowcount != 1:
            raise ValueError(f"source run {source_run_id} does not exist")
        return int(result.lastrowid)

    def corpus_mutation_guard(self, run_id: int) -> CorpusMutationGuard:
        """Return the immutable corpus mutation guard for one durable run."""

        return corpus_mutation_guard(self._connection, run_id)

    def latest_durable_inventory_binding(
        self,
        root: Path,
        *,
        corpus_access_mode: str | None = None,
        inventory_policy_signature: str | None = None,
    ) -> DurableInventoryBinding | None:
        """Return the newest completed owner with its exact published cursor."""

        if corpus_access_mode not in {None, "normal", "analyze_only"}:
            raise ValueError("invalid corpus access mode filter")
        row = self._connection.execute(
            f"""SELECT run_id,scan_id,corpus_access_mode,
            inventory_policy_signature,journal_volume,journal_id,end_usn
            FROM initial_runs
            WHERE root=? COLLATE {_PATH_COLLATION} AND status='completed'
            AND scan_id IS NOT NULL
            AND run_kind='initial'
            ORDER BY run_id DESC LIMIT 1""",
            (str(Path(os.path.abspath(os.path.realpath(root)))),),
        ).fetchone()
        if row is None:
            return None
        if corpus_access_mode is not None and str(row[2]) != corpus_access_mode:
            return None
        if inventory_policy_signature is not None and row[3] != inventory_policy_signature:
            return None
        end_cursor = None
        if all(value is not None for value in row[4:7]):
            end_cursor = JournalCursor(str(row[4]), int(row[5]), int(row[6]))
        return DurableInventoryBinding(
            run_id=int(row[0]),
            scan_id=int(row[1]),
            corpus_access_mode=str(row[2]),
            inventory_policy_signature=(None if row[3] is None else str(row[3])),
            end_cursor=end_cursor,
        )

    def latest_durable_inventory_run(
        self,
        root: Path,
        *,
        corpus_access_mode: str | None = None,
        inventory_policy_signature: str | None = None,
    ) -> tuple[int, int] | None:
        """Retain the historical run/scan API over the stronger binding."""

        binding = self.latest_durable_inventory_binding(
            root,
            corpus_access_mode=corpus_access_mode,
            inventory_policy_signature=inventory_policy_signature,
        )
        if binding is None:
            return None
        return binding.run_id, binding.scan_id

    def set_run_phase(self, run_id: int, phase: str) -> None:
        with self._connection:
            self._connection.execute(
                """UPDATE initial_runs SET current_phase=?,heartbeat_ns=?
                WHERE run_id=? AND status='running'""",
                (phase, time.time_ns(), run_id),
            )

    def update_run_start_cursor(
        self,
        run_id: int,
        cursor: JournalCursor | None,
    ) -> None:
        """Persist the cursor that actually bounded inventory preparation."""

        journal_values = (
            (None, None, None)
            if cursor is None
            else (cursor.volume, str(cursor.journal_id), cursor.next_usn)
        )
        with self._connection:
            updated = self._connection.execute(
                "UPDATE initial_runs SET journal_volume=?,journal_id=?,start_usn=? "
                "WHERE run_id=? AND status='running'",
                (*journal_values, run_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"run {run_id} cannot update its effective inventory cursor")

    @staticmethod
    def _validate_inventory_binding(
        scan_id: int,
        reconciliation_records: int,
        inventory_attempts: int,
        inventory_mode: str,
        candidate_rows: int,
    ) -> None:
        if (
            type(scan_id) is not int
            or scan_id <= 0
            or type(reconciliation_records) is not int
            or reconciliation_records < 0
            or type(inventory_attempts) is not int
            or inventory_attempts < 0
            or inventory_mode not in {"full", "incremental"}
            or type(candidate_rows) is not int
            or candidate_rows < 0
        ):
            raise ValueError("invalid initial routing snapshot")

    def publish_initial_routing_snapshot(
        self,
        run_id: int,
        scan_id: int,
        reconciliation_records: int,
        inventory_attempts: int,
        inventory_mode: str,
        candidate_rows: int,
    ) -> bool:
        """Atomically publish a complete inventory and routing candidate snapshot."""

        self._validate_inventory_binding(
            scan_id,
            reconciliation_records,
            inventory_attempts,
            inventory_mode,
            candidate_rows,
        )
        with self._connection:
            row = self._connection.execute(
                """SELECT status,run_kind,scan_id,reconciliation_records,
                inventory_attempts,inventory_mode FROM initial_runs WHERE run_id=?""",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"initial run {run_id} does not exist")
            status, run_kind, current_scan_id, *metadata = row
            if str(run_kind) != "initial" or str(status) != "running":
                raise ValueError(f"run {run_id} cannot bind inventory while {run_kind}/{status}")
            actual_candidates = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM route_candidates WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            if actual_candidates != candidate_rows:
                raise ValueError(f"run {run_id} routing candidate count changed before publication")
            if current_scan_id is not None:
                persisted = (int(current_scan_id), *(metadata))
                requested = (
                    scan_id,
                    reconciliation_records,
                    inventory_attempts,
                    inventory_mode,
                )
                if persisted != requested:
                    raise ValueError(f"run {run_id} has conflicting routing snapshot metadata")
                marker = self._connection.execute(
                    """SELECT 1 FROM run_events WHERE run_id=?
                    AND phase='routing-snapshot'
                    AND message='Snapshot de rutas publicado' LIMIT 1""",
                    (run_id,),
                ).fetchone()
                if marker is None:
                    raise ValueError(
                        f"run {run_id} inventory is bound without publication evidence"
                    )
                return False
            now = time.time_ns()
            result = self._connection.execute(
                """UPDATE initial_runs SET scan_id=?,reconciliation_records=?,
                inventory_attempts=?,inventory_mode=?,heartbeat_ns=?
                WHERE run_id=? AND status='running'
                AND run_kind='initial'
                AND scan_id IS NULL""",
                (
                    scan_id,
                    reconciliation_records,
                    inventory_attempts,
                    inventory_mode,
                    now,
                    run_id,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError(f"run {run_id} routing snapshot was not published")
            details_json = json.dumps(
                {
                    "schema": "neocortex.routing-snapshot/v1",
                    "scan_id": scan_id,
                    "candidate_rows": candidate_rows,
                    "reconciliation_records": reconciliation_records,
                    "attempts": inventory_attempts,
                    "mode": inventory_mode,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self._connection.execute(
                """INSERT INTO run_events(
                run_id,occurred_ns,level,phase,message,details_json)
                VALUES(?,?,'info','routing-snapshot',
                'Snapshot de rutas publicado',?)""",
                (run_id, now, details_json),
            )
        return True

    def recover_initial_routing_snapshot(
        self,
        run_id: int,
        evidence: InventoryRunEvidence,
        candidate_rows: int,
    ) -> bool:
        """Recover only a legacy snapshot already proven complete by route work."""

        self._validate_inventory_binding(
            evidence.scan_id,
            evidence.reconciliation_records,
            evidence.inventory_attempts,
            evidence.inventory_mode,
            candidate_rows,
        )
        with self._connection:
            actual_candidates = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM route_candidates WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            route_runs = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM route_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            if actual_candidates != candidate_rows or route_runs <= 0:
                raise ValueError(f"source run {run_id} has no complete legacy routing snapshot")
            row = self._connection.execute(
                """SELECT status,run_kind,scan_id,reconciliation_records,
                inventory_attempts,inventory_mode FROM initial_runs WHERE run_id=?""",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"initial run {run_id} does not exist")
            status, run_kind, current_scan_id, *metadata = row
            if str(run_kind) != "initial" or str(status) != "interrupted":
                raise ValueError(f"run {run_id} cannot recover inventory while {run_kind}/{status}")
            requested = (
                evidence.scan_id,
                evidence.reconciliation_records,
                evidence.inventory_attempts,
                evidence.inventory_mode,
            )
            if current_scan_id is not None:
                if (int(current_scan_id), *metadata) != requested:
                    raise ValueError(f"run {run_id} has conflicting recovered snapshot metadata")
                return False
            now = time.time_ns()
            result = self._connection.execute(
                """UPDATE initial_runs SET scan_id=?,reconciliation_records=?,
                inventory_attempts=?,inventory_mode=?,heartbeat_ns=?
                WHERE run_id=? AND status='interrupted' AND run_kind='initial'
                AND scan_id IS NULL""",
                (*requested, now, run_id),
            )
            if result.rowcount != 1:
                raise RuntimeError(f"run {run_id} inventory recovery lost its CAS")
            details_json = json.dumps(
                {
                    "schema": "neocortex.inventory-recovery/v1",
                    "scan_id": evidence.scan_id,
                    "inventory_event_id": evidence.event_id,
                    "files": evidence.files,
                    "candidate_rows": candidate_rows,
                    "validation": "complete_scan_root_identity_file_and_route_counts",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self._connection.execute(
                """INSERT INTO run_events(
                run_id,occurred_ns,level,phase,message,details_json)
                VALUES(?,?,'info','inventory-recovery',
                'Vínculo de inventario recuperado',?)""",
                (run_id, now, details_json),
            )
        return True

    def record_event(
        self,
        run_id: int,
        level: str,
        phase: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Append one structured operational event without external log artifacts."""

        if level not in {"debug", "info", "warning", "error"}:
            raise ValueError(f"invalid event level: {level}")
        details_json = (
            None
            if details is None
            else json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        )
        with self._connection:
            self._connection.execute(
                "INSERT INTO run_events(run_id,occurred_ns,level,phase,message,details_json) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, time.time_ns(), level, phase, message, details_json),
            )

    # ------------------------------------------------------------------
    # Durable lifecycle budget
    # ------------------------------------------------------------------

    @staticmethod
    def _budget_configuration(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
        value = manifest.get("budget", {})
        if not isinstance(value, Mapping):
            raise ValueError("run manifest budget is not an object")
        durable = value.get("durable")
        if durable is not None:
            if not isinstance(durable, Mapping):
                raise ValueError("run manifest durable budget is not an object")
            return durable
        return value

    def _append_lifecycle_event_once(
        self,
        run_id: int,
        *,
        level: str,
        phase: str,
        message: str,
        idempotency_key: str,
        details: Mapping[str, Any],
    ) -> bool:
        """Append one lifecycle event exactly once under the writer lock."""

        payload = dict(details)
        payload["idempotency_key"] = idempotency_key
        details_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        rows = self._connection.execute(
            """SELECT details_json FROM run_events
            WHERE run_id=? AND phase=? AND message=?
            ORDER BY event_id""",
            (run_id, phase, message),
        ).fetchall()
        for row in rows:
            try:
                existing = json.loads(str(row[0]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"run {run_id} has malformed {phase} lifecycle event"
                ) from exc
            if isinstance(existing, Mapping) and existing.get("idempotency_key") == idempotency_key:
                if str(row[0]) != details_json:
                    raise RuntimeError(
                        f"run {run_id} has a conflicting lifecycle event {idempotency_key}"
                    )
                return False
        self._connection.execute(
            """INSERT INTO run_events(
            run_id,occurred_ns,level,phase,message,details_json)
            VALUES(?,?,?,?,?,?)""",
            (run_id, time.time_ns(), level, phase, message, details_json),
        )
        return True

    def _ensure_run_budget_event(
        self,
        run_id: int,
        budget: Mapping[str, Any] | None,
        *,
        manifest_digest: str | None = None,
    ) -> bool:
        """Create the immutable budget baseline without changing the schema."""

        normalized = RunBudget.from_mapping(budget)
        rows = self._connection.execute(
            """SELECT details_json FROM run_events
            WHERE run_id=? AND phase='lifecycle-budget'
            AND message='Run budget initialized' ORDER BY event_id DESC LIMIT 2""",
            (run_id,),
        ).fetchall()
        if len(rows) > 1:
            raise RuntimeError(f"run {run_id} has duplicate lifecycle budgets")
        if rows:
            try:
                existing = json.loads(str(rows[0][0]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"run {run_id} lifecycle budget is malformed") from exc
            if not isinstance(existing, Mapping):
                raise RuntimeError(f"run {run_id} lifecycle budget is not an object")
            for key, expected in normalized.payload().items():
                if existing.get(key) != expected:
                    raise RuntimeError(f"run {run_id} has a conflicting lifecycle budget")
            if manifest_digest is not None:
                existing_digest = existing.get("manifest_digest")
                if existing_digest not in {None, manifest_digest}:
                    raise RuntimeError(
                        f"run {run_id} lifecycle budget is bound to another manifest"
                    )
                if existing_digest is None:
                    self._append_lifecycle_event_once(
                        run_id,
                        level="info",
                        phase="lifecycle-budget",
                        message="Run budget bound",
                        idempotency_key="manifest-binding",
                        details={
                            "schema": RUN_BUDGET_SCHEMA,
                            "kind": "bound",
                            "manifest_digest": manifest_digest,
                        },
                    )
            return False

        now = time.time_ns()
        deadline_ns = (
            None
            if normalized.max_duration_seconds is None
            else now + int(float(normalized.max_duration_seconds) * 1_000_000_000)
        )
        details = {
            **normalized.payload(),
            "manifest_digest": manifest_digest,
            "started_ns": now,
            "deadline_ns": deadline_ns,
            "consumed_items": 0,
            "consumed_bytes": 0,
            "cancel_requested": False,
            "cancel_reason": None,
            "reservation_count": 0,
            "idempotency_key": "baseline",
        }
        self._connection.execute(
            """INSERT INTO run_events(
            run_id,occurred_ns,level,phase,message,details_json)
            VALUES(?,?,'info','lifecycle-budget','Run budget initialized',?)""",
            (run_id, now, json.dumps(details, ensure_ascii=False, separators=(",", ":"))),
        )
        return True

    def _budget_rows(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT event_id,details_json FROM run_events
            WHERE run_id=? AND phase='lifecycle-budget'
            ORDER BY event_id""",
            (run_id,),
        ).fetchall()
        parsed: list[dict[str, Any]] = []
        for event_id, details_json in rows:
            try:
                value = json.loads(str(details_json))
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"run {run_id} lifecycle budget is malformed") from exc
            if not isinstance(value, dict) or value.get("schema") != RUN_BUDGET_SCHEMA:
                raise RuntimeError(f"run {run_id} lifecycle budget schema is unsupported")
            value["event_id"] = int(event_id)
            parsed.append(value)
        return parsed

    def _read_run_budget_locked(self, run_id: int) -> dict[str, Any] | None:
        rows = self._budget_rows(run_id)
        if not rows:
            return None
        baseline = rows[0]
        state: dict[str, Any] = {
            "schema": RUN_BUDGET_SCHEMA,
            "manifest_digest": baseline.get("manifest_digest"),
            "max_items": baseline.get("max_items"),
            "max_bytes": baseline.get("max_bytes"),
            "max_duration_seconds": baseline.get("max_duration_seconds"),
            "started_ns": baseline.get("started_ns"),
            "deadline_ns": baseline.get("deadline_ns"),
            "consumed_items": int(baseline.get("consumed_items", 0)),
            "consumed_bytes": int(baseline.get("consumed_bytes", 0)),
            "cancel_requested": bool(baseline.get("cancel_requested", False)),
            "cancel_reason": baseline.get("cancel_reason"),
            "reservations": {},
            "last_event_id": int(baseline["event_id"]),
        }
        for event in rows[1:]:
            state["last_event_id"] = int(event["event_id"])
            kind = event.get("kind")
            if kind == "consumed":
                reservation_id = str(event.get("reservation_id", ""))
                if reservation_id in state["reservations"]:
                    # A duplicate reservation is not a second effect.
                    continue
                items = int(event.get("items", 0))
                byte_count = int(event.get("bytes", 0))
                state["consumed_items"] += items
                state["consumed_bytes"] += byte_count
                state["reservations"][reservation_id] = {
                    "items": items,
                    "bytes": byte_count,
                    "worker": event.get("worker"),
                    "event_id": int(event["event_id"]),
                }
            elif kind == "cancelled":
                state["cancel_requested"] = True
                state["cancel_reason"] = event.get("reason")
            elif kind == "bound":
                state["manifest_digest"] = event.get("manifest_digest")
        now = time.time_ns()
        terminal = self._connection.execute(
            "SELECT completed_ns FROM initial_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        completed_ns = None if terminal is None or terminal[0] is None else int(terminal[0])
        elapsed_until_ns = now if completed_ns is None else completed_ns
        deadline_ns = state["deadline_ns"]
        state["elapsed_ns"] = max(0, elapsed_until_ns - int(state["started_ns"]))
        state["elapsed_seconds"] = state["elapsed_ns"] / 1_000_000_000
        state["elapsed_until_ns"] = elapsed_until_ns
        state["elapsed_scope"] = (
            "budget_start_to_observation"
            if completed_ns is None
            else "budget_start_to_run_completion"
        )
        state["consumed_bytes_kind"] = "reserved_input_bytes_not_physical_io"
        state["expired"] = deadline_ns is not None and elapsed_until_ns >= int(deadline_ns)
        state["remaining_items"] = (
            None
            if state["max_items"] is None
            else max(0, int(state["max_items"]) - state["consumed_items"])
        )
        state["remaining_bytes"] = (
            None
            if state["max_bytes"] is None
            else max(0, int(state["max_bytes"]) - state["consumed_bytes"])
        )
        state["reservation_count"] = len(state["reservations"])
        return state

    def publish_run_budget(
        self,
        run_id: int,
        budget: Mapping[str, Any] | None = None,
        *,
        manifest_digest: str | None = None,
    ) -> bool:
        """Persist an immutable budget baseline, linked to the manifest."""

        with self._connection:
            return self._ensure_run_budget_event(
                run_id,
                budget,
                manifest_digest=manifest_digest,
            )

    def read_run_budget(self, run_id: int) -> dict[str, Any] | None:
        """Read the current budget snapshot from the writer owner."""

        return self._read_run_budget_locked(run_id)

    def run_budget(self, run_id: int) -> dict[str, Any] | None:
        """Compatibility alias for callers that treat budgets as run state."""

        return self.read_run_budget(run_id)

    def reserve_run_budget(
        self,
        run_id: int,
        reservation_id: str,
        *,
        items: int = 0,
        bytes: int = 0,
        worker: str | None = None,
        item_count: int | None = None,
        byte_count: int | None = None,
    ) -> dict[str, Any]:
        """Atomically reserve durable work for one worker.

        The reservation id is the effect boundary. Repeating it returns the
        same durable reservation and never increments the counters again.
        Reservations are deliberately not refunded after a worker failure: a
        retry must use a new run or an explicitly supported route replay.
        """

        if item_count is not None:
            if items != 0 and items != item_count:
                raise ValueError("items and item_count disagree")
            items = item_count
        if byte_count is not None:
            if bytes != 0 and bytes != byte_count:
                raise ValueError("bytes and byte_count disagree")
            bytes = byte_count
        if not reservation_id or len(reservation_id) > 256:
            raise ValueError("reservation_id must be non-empty and bounded")
        if type(items) is not int or items < 0 or type(bytes) is not int or bytes < 0:
            raise ValueError("budget reservation items and bytes must be non-negative integers")
        with self._connection:
            run = self._connection.execute(
                "SELECT status FROM initial_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f"run {run_id} does not exist")
            snapshot = self._read_run_budget_locked(run_id)
            if snapshot is None:
                self._ensure_run_budget_event(run_id, None)
                snapshot = self._read_run_budget_locked(run_id)
            assert snapshot is not None
            existing = snapshot["reservations"].get(reservation_id)
            if existing is not None:
                if existing["items"] != items or existing["bytes"] != bytes:
                    raise ValueError(f"run {run_id} reservation {reservation_id} conflicts")
                replay = dict(snapshot)
                replay["replayed"] = True
                replay["reservation"] = dict(existing)
                return replay
            if str(run[0]) != "running":
                raise RunBudgetExceeded("terminal", snapshot)
            reason = None
            if snapshot["cancel_requested"]:
                reason = "cancelled"
            elif snapshot["expired"]:
                reason = "time"
            elif (
                snapshot["max_items"] is not None
                and snapshot["consumed_items"] + items > int(snapshot["max_items"])
            ):
                reason = "items"
            elif (
                snapshot["max_bytes"] is not None
                and snapshot["consumed_bytes"] + bytes > int(snapshot["max_bytes"])
            ):
                reason = "bytes"
            if reason is not None:
                raise RunBudgetExceeded(reason, snapshot)
            details = {
                "schema": RUN_BUDGET_SCHEMA,
                "kind": "consumed",
                "manifest_digest": snapshot["manifest_digest"],
                "reservation_id": reservation_id,
                "worker": worker,
                "items": items,
                "bytes": bytes,
                "consumed_items": snapshot["consumed_items"] + items,
                "consumed_bytes": snapshot["consumed_bytes"] + bytes,
                "idempotency_key": f"reservation:{reservation_id}",
            }
            self._connection.execute(
                """INSERT INTO run_events(
                run_id,occurred_ns,level,phase,message,details_json)
                VALUES(?,?,'info','lifecycle-budget','Run budget consumed',?)""",
                (run_id, time.time_ns(), json.dumps(details, ensure_ascii=False, separators=(",", ":"))),
            )
            result = self._read_run_budget_locked(run_id)
            assert result is not None
            result["replayed"] = False
            result["reservation"] = {
                "items": items,
                "bytes": bytes,
                "worker": worker,
            }
            return result

    def consume_run_budget(
        self,
        run_id: int,
        reservation_id: str,
        *,
        items: int = 0,
        bytes: int = 0,
        worker: str | None = None,
        item_count: int | None = None,
        byte_count: int | None = None,
    ) -> dict[str, Any]:
        """Record committed work using the same idempotent reservation ledger."""

        return self.reserve_run_budget(
            run_id,
            reservation_id,
            items=items,
            bytes=bytes,
            worker=worker,
            item_count=item_count,
            byte_count=byte_count,
        )

    def request_run_cancellation(self, run_id: int, reason: str = "user") -> bool:
        """Persist cancellation once so a later process observes the request."""

        if not reason or len(reason) > 512:
            raise ValueError("cancellation reason must be non-empty and bounded")
        with self._connection:
            if self._connection.execute(
                "SELECT 1 FROM initial_runs WHERE run_id=?", (run_id,)
            ).fetchone() is None:
                raise ValueError(f"run {run_id} does not exist")
            snapshot = self._read_run_budget_locked(run_id)
            if snapshot is None:
                self._ensure_run_budget_event(run_id, None)
                snapshot = self._read_run_budget_locked(run_id)
            assert snapshot is not None
            if snapshot["cancel_requested"]:
                if snapshot["cancel_reason"] != reason:
                    raise ValueError(f"run {run_id} has a conflicting cancellation reason")
                return False
            details = {
                "schema": RUN_BUDGET_SCHEMA,
                "kind": "cancelled",
                "manifest_digest": snapshot["manifest_digest"],
                "reason": reason,
                "idempotency_key": "cancellation",
            }
            self._connection.execute(
                """INSERT INTO run_events(
                run_id,occurred_ns,level,phase,message,details_json)
                VALUES(?,?,'warning','lifecycle-budget','Run cancellation requested',?)""",
                (run_id, time.time_ns(), json.dumps(details, ensure_ascii=False, separators=(",", ":"))),
            )
            return True

    @staticmethod
    def request_run_cancellation_external(
        database: str | Path,
        run_id: int,
        reason: str = "user",
    ) -> bool:
        """Persist cancellation from a signal/UI thread without sharing SQLite objects."""

        if not reason or len(reason) > 512:
            raise ValueError("cancellation reason must be non-empty and bounded")
        connection = connect_existing_framework(
            Path(database), readonly=False, timeout_seconds=10
        )
        try:
            with connection:
                if connection.execute(
                    "SELECT 1 FROM initial_runs WHERE run_id=?", (run_id,)
                ).fetchone() is None:
                    raise ValueError(f"run {run_id} does not exist")
                row = connection.execute(
                    """SELECT details_json FROM run_events
                    WHERE run_id=? AND phase='lifecycle-budget'
                    AND message='Run budget initialized'
                    ORDER BY event_id DESC LIMIT 1""",
                    (run_id,),
                ).fetchone()
                manifest_digest = None
                if row is not None and row[0] is not None:
                    try:
                        baseline = json.loads(str(row[0]))
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise RuntimeError(f"run {run_id} lifecycle budget is malformed") from exc
                    if isinstance(baseline, Mapping):
                        manifest_digest = baseline.get("manifest_digest")
                existing = connection.execute(
                    """SELECT details_json FROM run_events
                    WHERE run_id=? AND phase='lifecycle-budget'
                    AND message='Run cancellation requested'
                    ORDER BY event_id DESC LIMIT 1""",
                    (run_id,),
                ).fetchone()
                if existing is not None:
                    return False
                if row is None:
                    now = time.time_ns()
                    baseline = {
                        **RunBudget().payload(),
                        "manifest_digest": None,
                        "started_ns": now,
                        "deadline_ns": None,
                        "consumed_items": 0,
                        "consumed_bytes": 0,
                        "cancel_requested": False,
                        "cancel_reason": None,
                        "reservation_count": 0,
                        "idempotency_key": "baseline",
                    }
                    connection.execute(
                        """INSERT INTO run_events(
                        run_id,occurred_ns,level,phase,message,details_json)
                        VALUES(?,?,'info','lifecycle-budget',
                        'Run budget initialized',?)""",
                        (
                            run_id,
                            now,
                            json.dumps(baseline, ensure_ascii=False, separators=(",", ":")),
                        ),
                    )
                details = {
                    "schema": RUN_BUDGET_SCHEMA,
                    "kind": "cancelled",
                    "manifest_digest": manifest_digest,
                    "reason": reason,
                    "idempotency_key": "cancellation",
                }
                connection.execute(
                    """INSERT INTO run_events(
                    run_id,occurred_ns,level,phase,message,details_json)
                    VALUES(?,?,'warning','lifecycle-budget',
                    'Run cancellation requested',?)""",
                    (
                        run_id,
                        time.time_ns(),
                        json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                return True
        finally:
            connection.close()

    def run_cancellation_requested(self, run_id: int) -> bool:
        snapshot = self._read_run_budget_locked(run_id)
        return bool(snapshot and snapshot["cancel_requested"])

    def check_run_budget(self, run_id: int) -> dict[str, Any]:
        """Return a live snapshot or raise before a worker crosses its frontier."""

        snapshot = self._read_run_budget_locked(run_id)
        if snapshot is None:
            raise ValueError(f"run {run_id} has no durable lifecycle budget")
        if snapshot["elapsed_scope"] == "budget_start_to_run_completion":
            raise RunBudgetExceeded("terminal", snapshot)
        if snapshot["cancel_requested"]:
            raise RunBudgetExceeded("cancelled", snapshot)
        if snapshot["expired"]:
            raise RunBudgetExceeded("time", snapshot)
        return snapshot

    def publish_run_manifest(self, run_id: int, manifest: Mapping[str, Any]) -> bool:
        """Publish one immutable lifecycle manifest idempotently as an event.

        The existing framework schema deliberately remains at v22; the
        manifest is an append-only, schema-tagged event so older databases can
        read it without an unsafe migration while the run tables remain the
        authoritative lifecycle state.
        """

        verified = verify_event_payload(manifest)
        payload_json = json.dumps(verified, ensure_ascii=False, separators=(",", ":"))
        with self._connection:
            rows = self._connection.execute(
                """SELECT details_json FROM run_events
                WHERE run_id=? AND phase='lifecycle-manifest'
                AND message='Run manifest published' ORDER BY event_id DESC LIMIT 2""",
                (run_id,),
            ).fetchall()
            if len(rows) > 1:
                raise RuntimeError(f"run {run_id} has duplicate lifecycle manifests")
            if rows:
                existing = rows[0][0]
                if existing != payload_json:
                    raise RuntimeError(f"run {run_id} has a conflicting lifecycle manifest")
                self._ensure_run_budget_event(
                    run_id,
                    self._budget_configuration(verified),
                    manifest_digest=str(verified["digest"]),
                )
                return False
            self._connection.execute(
                """INSERT INTO run_events(
                run_id,occurred_ns,level,phase,message,details_json)
                VALUES(?,?,'info','lifecycle-manifest','Run manifest published',?)""",
                (run_id, time.time_ns(), payload_json),
            )
            self._ensure_run_budget_event(
                run_id,
                self._budget_configuration(verified),
                manifest_digest=str(verified["digest"]),
            )
        return True

    def read_run_manifest(self, run_id: int) -> dict[str, Any] | None:
        """Read and validate the immutable manifest for one run."""

        row = self._connection.execute(
            """SELECT details_json FROM run_events
            WHERE run_id=? AND phase='lifecycle-manifest'
            AND message='Run manifest published' ORDER BY event_id DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        try:
            payload = json.loads(str(row[0]))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"run {run_id} lifecycle manifest is malformed") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"run {run_id} lifecycle manifest is not an object")
        return verify_event_payload(payload)

    def read_run_route_capabilities(self, run_id: int) -> dict[str, str]:
        """Return the immutable replay capability declared by each route."""

        manifest = self.read_run_manifest(run_id)
        if manifest is None:
            return {}
        value = manifest.get("route_capabilities", {})
        if not isinstance(value, Mapping):
            raise RuntimeError(f"run {run_id} route capabilities are invalid")
        capabilities = {str(name): str(capability) for name, capability in value.items()}
        if any(
            capability not in {"phase_resume", "safe_replay", "not_resumable"}
            for capability in capabilities.values()
        ):
            raise RuntimeError(f"run {run_id} route capability is unsupported")
        return capabilities

    def publish_run_stage(
        self,
        run_id: int,
        stage: str,
        status: str,
        *,
        details: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> bool:
        """Append one bounded lifecycle stage transition idempotently.

        Stages are kept in the Framework owner as metadata only.  A stage may
        describe work performed by another owner (for example Semantic), but
        this method never opens or mutates that owner.  The run manifest digest
        is copied into every event so a reader can reject a stage detached
        from its immutable input boundary.
        """

        if not isinstance(stage, str) or not stage or len(stage) > 128:
            raise ValueError("lifecycle stage must be non-empty and bounded")
        if not isinstance(status, str) or status not in {
            "pending",
            "running",
            "completed",
            "partial",
            "failed",
            "interrupted",
            "skipped",
        }:
            raise ValueError("unsupported lifecycle stage status")
        selected_details: dict[str, Any] = {} if details is None else dict(details)
        encoded = json.dumps(
            selected_details,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > 256 * 1024:
            raise ValueError("lifecycle stage details exceed the durable limit")
        key = idempotency_key or f"{stage}:{status}"
        if not isinstance(key, str) or not key or len(key) > 256:
            raise ValueError("lifecycle stage idempotency key is invalid")
        with self._connection:
            manifest = self.read_run_manifest(run_id)
            if manifest is None:
                raise ValueError(f"run {run_id} has no lifecycle manifest")
            payload = {
                "schema": RUN_STAGE_SCHEMA,
                "run_id": run_id,
                "manifest_digest": manifest["digest"],
                "stage": stage,
                "status": status,
                "details": selected_details,
            }
            return self._append_lifecycle_event_once(
                run_id,
                level="error" if status == "failed" else "warning" if status in {"partial", "interrupted"} else "info",
                phase="lifecycle-stage",
                message="Lifecycle stage transitioned",
                idempotency_key=key,
                details=payload,
            )

    def read_run_stages(self, run_id: int) -> tuple[dict[str, Any], ...]:
        """Read and validate bounded lifecycle stage events for one run."""

        rows = self._connection.execute(
            """SELECT event_id,details_json FROM run_events
            WHERE run_id=? AND phase='lifecycle-stage'
            AND message='Lifecycle stage transitioned'
            ORDER BY event_id LIMIT 65""",
            (run_id,),
        ).fetchall()
        if len(rows) > 64:
            raise RuntimeError(f"run {run_id} has too many lifecycle stages")
        manifest = self.read_run_manifest(run_id)
        if manifest is None and rows:
            raise RuntimeError(f"run {run_id} lifecycle stages have no manifest")
        result: list[dict[str, Any]] = []
        for event_id, details_json in rows:
            try:
                payload = json.loads(str(details_json))
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"run {run_id} lifecycle stage is malformed") from exc
            if not isinstance(payload, dict) or payload.get("schema") != RUN_STAGE_SCHEMA:
                raise RuntimeError(f"run {run_id} lifecycle stage schema is unsupported")
            if payload.get("run_id") != run_id:
                raise RuntimeError(f"run {run_id} lifecycle stage has an invalid owner")
            if manifest is not None and payload.get("manifest_digest") != manifest.get("digest"):
                raise RuntimeError(f"run {run_id} lifecycle stage is detached from its manifest")
            if not isinstance(payload.get("details"), dict):
                raise RuntimeError(f"run {run_id} lifecycle stage details are invalid")
            payload["event_id"] = int(event_id)
            result.append(payload)
        return tuple(result)

    def resumable_route_candidate_run_ids(self) -> tuple[int, ...]:
        """Return runs whose route inputs remain needed for recovery/replay."""

        rows = self._connection.execute(
            """SELECT DISTINCT run_id FROM route_runs
            WHERE status IN ('running','interrupted','failed','cancelled')
            ORDER BY run_id"""
        ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def begin_route_runs(
        self,
        run_id: int,
        route_names: Iterable[str],
        *,
        route_input_sources: Mapping[str, str] | None = None,
    ) -> None:
        now = time.time_ns()
        routes = tuple(route_names)
        input_sources = {
            str(name): str(source)
            for name, source in (route_input_sources or {}).items()
        }
        if input_sources and set(input_sources) != set(routes):
            raise ValueError("route input sources must cover exactly the selected routes")
        with self._connection:
            source_row = self._connection.execute(
                """SELECT source_run_id FROM initial_runs
                WHERE run_id=? AND status='running' AND scan_id IS NOT NULL""",
                (run_id,),
            ).fetchone()
            if source_row is None:
                raise ValueError(f"run {run_id} cannot start routes before snapshot publication")
            source_run_id = source_row[0]
            self._connection.executemany(
                """INSERT INTO route_runs(
                run_id,route_name,status,started_ns,current_phase,heartbeat_ns,
                source_run_id)
                VALUES(?,?,'running',?,'route_start',?,?)
                ON CONFLICT(run_id,route_name) DO NOTHING""",
                ((run_id, route_name, now, now, source_run_id) for route_name in routes),
            )
            if input_sources:
                self._append_lifecycle_event_once(
                    run_id,
                    level="info",
                    phase="route-inputs",
                    message="Route input sources bound",
                    idempotency_key="route-input-sources",
                    details={
                        "schema": "neocortex.route-input-sources/v1",
                        "route_input_sources": input_sources,
                    },
                )

    def read_route_input_sources(self, run_id: int) -> dict[str, str]:
        """Read the immutable route-input map bound before route workers."""

        row = self._connection.execute(
            """SELECT details_json FROM run_events
            WHERE run_id=? AND phase='route-inputs'
            AND message='Route input sources bound'
            ORDER BY event_id DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return {}
        try:
            payload = json.loads(str(row[0]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"run {run_id} route input sources are malformed") from exc
        sources = payload.get("route_input_sources") if isinstance(payload, Mapping) else None
        if not isinstance(sources, Mapping):
            raise RuntimeError(f"run {run_id} route input sources are invalid")
        return {str(name): str(source) for name, source in sources.items()}

    def begin_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        *,
        source_run_id: int | None = None,
    ) -> None:
        now = time.time_ns()
        with self._connection:
            inserted = self._connection.execute(
                """INSERT INTO route_phase_runs(
                run_id,route_name,phase_name,status,started_ns,heartbeat_ns,
                source_run_id)
                VALUES(?,?,?,'running',?,?,?)
                ON CONFLICT(run_id,route_name,phase_name) DO NOTHING""",
                (
                    run_id,
                    route_name,
                    phase_name,
                    now,
                    now,
                    source_run_id,
                ),
            )
            if inserted.rowcount != 1:
                return
            self._connection.execute(
                """UPDATE route_runs SET current_phase=?,heartbeat_ns=?
                WHERE run_id=? AND route_name=? AND status='running'""",
                (phase_name, now, run_id, route_name),
            )

    def complete_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        summary: Mapping[str, Any] | None = None,
    ) -> bool:
        now = time.time_ns()
        payload = (
            None
            if summary is None
            else json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        )
        with self._connection:
            updated = self._connection.execute(
                """UPDATE route_phase_runs SET status='completed',completed_ns=?,
                heartbeat_ns=?,summary_json=?,error_type=NULL,error_message=NULL
                WHERE run_id=? AND route_name=? AND phase_name=? AND status='running'""",
                (now, now, payload, run_id, route_name, phase_name),
            )
            if updated.rowcount == 1:
                return True
            status = self._connection.execute(
                """SELECT status FROM route_phase_runs
                WHERE run_id=? AND route_name=? AND phase_name=?""",
                (run_id, route_name, phase_name),
            ).fetchone()
            if status is not None and str(status[0]) == "completed":
                return False
            raise RuntimeError(
                f"route phase {run_id}/{route_name}/{phase_name} is not running"
            )

    def fail_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        exc: BaseException,
    ) -> None:
        now = time.time_ns()
        with self._connection:
            self._connection.execute(
                """UPDATE route_phase_runs SET status='failed',completed_ns=?,
                heartbeat_ns=?,error_type=?,error_message=?
                WHERE run_id=? AND route_name=? AND phase_name=?""",
                (
                    now,
                    now,
                    type(exc).__name__,
                    str(exc)[:8192],
                    run_id,
                    route_name,
                    phase_name,
                ),
            )

    def complete_route_run(
        self,
        run_id: int,
        route_name: str,
        summary: Mapping[str, Any],
    ) -> bool:
        payload = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        with self._connection:
            updated = self._connection.execute(
                """UPDATE route_runs SET status='completed',completed_ns=?,
                current_phase='completed',heartbeat_ns=?,summary_json=?,
                error_type=NULL,error_message=NULL
                WHERE run_id=? AND route_name=? AND status='running'""",
                (time.time_ns(), time.time_ns(), payload, run_id, route_name),
            )
            if updated.rowcount == 1:
                return True
            status = self._connection.execute(
                "SELECT status FROM route_runs WHERE run_id=? AND route_name=?",
                (run_id, route_name),
            ).fetchone()
            if status is not None and str(status[0]) == "completed":
                return False
            raise RuntimeError(f"route {run_id}/{route_name} is not running")

    def fail_route_run(
        self,
        run_id: int,
        route_name: str,
        exc: BaseException,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """UPDATE route_phase_runs SET status='failed',completed_ns=?,
                heartbeat_ns=?,error_type=?,error_message=?
                WHERE run_id=? AND route_name=? AND status='running'""",
                (
                    time.time_ns(),
                    time.time_ns(),
                    type(exc).__name__,
                    str(exc)[:8192],
                    run_id,
                    route_name,
                ),
            )
            self._connection.execute(
                """UPDATE route_runs SET status='failed',completed_ns=?,
                current_phase='failed',heartbeat_ns=?,error_type=?,error_message=?
                WHERE run_id=? AND route_name=? AND status='running'""",
                (
                    time.time_ns(),
                    time.time_ns(),
                    type(exc).__name__,
                    str(exc)[:8192],
                    run_id,
                    route_name,
                ),
            )

    def mark_abandoned_runs(self) -> int:
        """Close runs left active after an unclean process termination."""

        with self._connection:
            active = self._connection.execute(
                """SELECT run_id FROM initial_runs
                WHERE status='running' ORDER BY run_id"""
            ).fetchall()
            active_ids = tuple(int(row[0]) for row in active)
            status_rows = self._connection.execute(
                "SELECT run_id,status FROM initial_runs"
            ).fetchall()
            run_statuses = {int(row[0]): str(row[1]) for row in status_rows}
            latest_stage_events: dict[tuple[int, str], Mapping[str, Any]] = {}
            for stage_row in self._connection.execute(
                """SELECT run_id,details_json FROM run_events
                WHERE phase='lifecycle-stage'
                AND message='Lifecycle stage transitioned'
                ORDER BY event_id"""
            ):
                try:
                    stage_payload = json.loads(str(stage_row[1]))
                except (TypeError, json.JSONDecodeError) as exc:
                    raise RuntimeError("lifecycle stage recovery event is malformed") from exc
                if isinstance(stage_payload, Mapping):
                    stage_name = stage_payload.get("stage")
                    if isinstance(stage_name, str):
                        latest_stage_events[(int(stage_row[0]), stage_name)] = stage_payload
            stage_only_ids = {
                run_id
                for (run_id, _stage_name), stage_payload in latest_stage_events.items()
                if stage_payload.get("status") == "running"
                and run_statuses.get(run_id) not in {None, "running"}
            }
            recovery_ids = tuple(sorted(set(active_ids) | stage_only_ids))
            self._connection.execute(
                """UPDATE route_phase_runs SET status='interrupted',completed_ns=?,
                heartbeat_ns=?,error_type='InterruptedRun',
                error_message='framework phase was interrupted'
                WHERE status='running' AND run_id IN(
                SELECT run_id FROM initial_runs WHERE status='running')""",
                (time.time_ns(), time.time_ns()),
            )
            self._connection.execute(
                """UPDATE route_runs SET status='interrupted',completed_ns=?,
                current_phase='interrupted',heartbeat_ns=?,
                error_type='InterruptedRun',error_message='framework run was interrupted'
                WHERE status='running' AND run_id IN(
                    SELECT run_id FROM initial_runs WHERE status='running')""",
                (time.time_ns(), time.time_ns()),
            )
            result = self._connection.execute(
                """UPDATE initial_runs SET completed_ns=?,status='interrupted',
                current_phase='interrupted',heartbeat_ns=?
                WHERE status='running'""",
                (time.time_ns(), time.time_ns()),
            )
            for active_id in recovery_ids:
                route_names = tuple(
                    str(row[0])
                    for row in self._connection.execute(
                        """SELECT route_name FROM route_runs
                        WHERE run_id=? AND status='interrupted'
                        ORDER BY route_name""",
                        (active_id,),
                    )
                )
                candidate_rows, candidate_bytes = self.route_candidate_workload(active_id)
                route_input_sources = self.read_route_input_sources(active_id)
                start_event = self._connection.execute(
                    """SELECT details_json FROM run_events
                    WHERE run_id=? AND phase='run'
                    AND message='Ejecución aislada de rutas iniciada'
                    ORDER BY event_id DESC LIMIT 1""",
                    (active_id,),
                ).fetchone()
                if not route_input_sources and start_event is not None and start_event[0] is not None:
                    try:
                        details = json.loads(str(start_event[0]))
                    except (TypeError, json.JSONDecodeError):
                        details = None
                    if isinstance(details, Mapping) and isinstance(
                        details.get("route_input_sources"), Mapping
                    ):
                        route_input_sources = {
                            str(name): str(source)
                            for name, source in details["route_input_sources"].items()
                        }
                budget = self._read_run_budget_locked(active_id)
                if (
                    active_id in active_ids
                    and budget is not None
                    and not budget["cancel_requested"]
                ):
                    self._connection.execute(
                        """INSERT INTO run_events(
                        run_id,occurred_ns,level,phase,message,details_json)
                        VALUES(?,?,'warning','lifecycle-budget',
                        'Run cancellation requested',?)""",
                        (
                            active_id,
                            time.time_ns(),
                            json.dumps(
                                {
                                    "schema": RUN_BUDGET_SCHEMA,
                                    "kind": "cancelled",
                                    "manifest_digest": budget["manifest_digest"],
                                    "reason": "abrupt_termination",
                                    "idempotency_key": "cancellation",
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        ),
                    )
                self._append_lifecycle_event_once(
                    active_id,
                    level="warning",
                    phase="lifecycle-recovery",
                    message="Run abandoned after abrupt termination",
                    idempotency_key="abandoned",
                    details={
                        "status": "interrupted",
                        "routes": list(route_names),
                        "candidate_rows": candidate_rows,
                        "candidate_bytes": candidate_bytes,
                        "route_input_sources": route_input_sources,
                    },
                )
                manifest = self.read_run_manifest(active_id)
                if manifest is not None:
                    latest_stages: dict[str, dict[str, Any]] = {}
                    for stage_event in self.read_run_stages(active_id):
                        latest_stages[str(stage_event["stage"])] = stage_event
                    for stage_name, stage_event in latest_stages.items():
                        if stage_event.get("status") != "running":
                            continue
                        prior_details = stage_event.get("details")
                        details = dict(prior_details) if isinstance(prior_details, Mapping) else {}
                        details.update(
                            {
                                "reason": "abrupt_termination",
                                "previous_status": "running",
                            }
                        )
                        self._append_lifecycle_event_once(
                            active_id,
                            level="warning",
                            phase="lifecycle-stage",
                            message="Lifecycle stage transitioned",
                            idempotency_key=f"{stage_name}:abrupt-termination",
                            details={
                                "schema": RUN_STAGE_SCHEMA,
                                "run_id": active_id,
                                "manifest_digest": manifest["digest"],
                                "stage": stage_name,
                                "status": "interrupted",
                                "details": details,
                            },
                        )
        return int(result.rowcount) + len(stage_only_ids)

    def mark_abandoned_actions(self) -> int:
        """Distinguish abandoned intent from a crossed mutation frontier."""

        started_ids = tuple(
            int(row[0])
            for row in self._connection.execute(
                "SELECT action_id FROM file_actions WHERE status='started' ORDER BY action_id"
            )
        )
        applying_ids = tuple(
            int(row[0])
            for row in self._connection.execute(
                "SELECT action_id FROM file_actions WHERE status='applying' ORDER BY action_id"
            )
        )
        if started_ids:
            finish_file_actions(
                self._connection,
                started_ids,
                "failed",
                "framework interrupted before the mutation frontier; no "
                "filesystem effect was attempted",
            )
        if applying_ids:
            finish_file_actions(
                self._connection,
                applying_ids,
                "recovery_required",
                "framework interrupted after the mutation frontier; the "
                "filesystem effect is uncertain and requires reconciliation",
            )
        return len(started_ids) + len(applying_ids)

    def referenced_inventory_scan_ids(self) -> tuple[int, ...]:
        """Return every inventory generation referenced by durable run history."""

        return tuple(
            int(row[0])
            for row in self._connection.execute(
                "SELECT DISTINCT scan_id FROM initial_runs "
                "WHERE scan_id IS NOT NULL ORDER BY scan_id"
            )
        )


    def complete_initial_run(
        self,
        run_id: int,
        scan_id: int,
        cursor: JournalCursor | None,
        reconciliation_records: int,
        inventory_attempts: int,
        inventory_mode: str,
    ) -> bool:
        self._validate_inventory_binding(
            scan_id,
            reconciliation_records,
            inventory_attempts,
            inventory_mode,
            0,
        )
        if cursor is None and (
            inventory_mode != "full" or reconciliation_records != 0 or inventory_attempts != 1
        ):
            raise ValueError("portable inventory must publish one unreconciled full scan")
        with self._connection:
            result = self._connection.execute(
                "UPDATE initial_runs SET completed_ns=?, status='completed', "
                "current_phase='completed',heartbeat_ns=?,end_usn=? "
                "WHERE run_id=? AND status='running' AND scan_id=? "
                "AND run_kind='initial' "
                "AND reconciliation_records=? AND inventory_attempts=? "
                "AND inventory_mode=?",
                (
                    time.time_ns(),
                    time.time_ns(),
                    None if cursor is None else cursor.next_usn,
                    run_id,
                    scan_id,
                    reconciliation_records,
                    inventory_attempts,
                    inventory_mode,
                ),
            )
            if result.rowcount != 1:
                status = self._connection.execute(
                    "SELECT status FROM initial_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if status is not None and str(status[0]) == "completed":
                    return False
                raise RuntimeError(
                    f"run {run_id} cannot complete without its published snapshot"
                )
            self._append_lifecycle_event_once(
                run_id,
                level="info",
                phase="lifecycle-transition",
                message="Run transitioned",
                idempotency_key="status:completed",
                details={"status": "completed"},
            )
            return True

    def complete_operational_run(self, run_id: int) -> bool:
        with self._connection:
            result = self._connection.execute(
                """UPDATE initial_runs SET completed_ns=?,status='completed',
                current_phase='completed',heartbeat_ns=?
                WHERE run_id=? AND status='running'
                AND run_kind IN ('route_only','resume')""",
                (time.time_ns(), time.time_ns(), run_id),
            )
            if result.rowcount != 1:
                status = self._connection.execute(
                    "SELECT status FROM initial_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if status is not None and str(status[0]) == "completed":
                    return False
                raise RuntimeError(f"run {run_id} is not a running operational execution")
            self._append_lifecycle_event_once(
                run_id,
                level="info",
                phase="lifecycle-transition",
                message="Run transitioned",
                idempotency_key="status:completed",
                details={"status": "completed"},
            )
            return True

    def fail_initial_run(self, run_id: int) -> bool:
        with self._connection:
            now = time.time_ns()
            transitioned = self._connection.execute(
                """UPDATE initial_runs SET completed_ns=?,status='failed',
                current_phase='failed',heartbeat_ns=?
                WHERE run_id=? AND status='running'""",
                (now, now, run_id),
            )
            if transitioned.rowcount != 1:
                return False
            self._connection.execute(
                """UPDATE route_phase_runs SET status='failed',completed_ns=?,
                heartbeat_ns=?,error_type=COALESCE(error_type,'FrameworkRunFailed'),
                error_message=COALESCE(error_message,'framework run failed')
                WHERE run_id=? AND status='running'""",
                (now, now, run_id),
            )
            self._connection.execute(
                """UPDATE route_runs SET status='failed',completed_ns=?,
                current_phase='failed',heartbeat_ns=?,
                error_type=COALESCE(error_type,'FrameworkRunFailed'),
                error_message=COALESCE(error_message,'framework run failed')
                WHERE run_id=? AND status='running'""",
                (now, now, run_id),
            )
            self._append_lifecycle_event_once(
                run_id,
                level="error",
                phase="lifecycle-transition",
                message="Run transitioned",
                idempotency_key="status:failed",
                details={"status": "failed"},
            )
            return True

    def cancel_initial_run(self, run_id: int) -> bool:
        with self._connection:
            now = time.time_ns()
            transitioned = self._connection.execute(
                """UPDATE initial_runs SET completed_ns=?,status='cancelled',
                current_phase='cancelled',heartbeat_ns=?
                WHERE run_id=? AND status='running'""",
                (now, now, run_id),
            )
            if transitioned.rowcount != 1:
                return False
            self._connection.execute(
                """UPDATE route_phase_runs SET status='cancelled',completed_ns=?,
                heartbeat_ns=?,error_type='KeyboardInterrupt',
                error_message='framework run cancelled'
                WHERE run_id=? AND status='running'""",
                (now, now, run_id),
            )
            self._connection.execute(
                """UPDATE route_runs SET status='cancelled',completed_ns=?,
                current_phase='cancelled',heartbeat_ns=?,
                error_type='KeyboardInterrupt',error_message='framework run cancelled'
                WHERE run_id=? AND status='running'""",
                (now, now, run_id),
            )
            self._append_lifecycle_event_once(
                run_id,
                level="warning",
                phase="lifecycle-transition",
                message="Run transitioned",
                idempotency_key="status:cancelled",
                details={"status": "cancelled"},
            )
            return True

    def begin_file_action(
        self,
        run_id: int,
        action_type: str,
        source_path: str,
        target_path: str | None,
        detected_mime: str | None,
        evidence: str | None,
        apply_requested: bool,
    ) -> int:
        return self.begin_file_actions(
            run_id,
            (
                (
                    action_type,
                    source_path,
                    target_path,
                    detected_mime,
                    evidence,
                    apply_requested,
                ),
            ),
        )[0]

    def begin_file_actions(
        self,
        run_id: int,
        actions: Iterable[FileActionSpec],
    ) -> list[int]:
        """Insert a bounded action batch in one transaction."""

        return begin_file_actions(self._connection, run_id, actions)

    def finish_file_action(self, action_id: int, status: str, detail: str | None = None) -> None:
        self.finish_file_actions((action_id,), status, detail)

    def finish_file_actions(
        self,
        action_ids: Iterable[int],
        status: str,
        detail: str | None = None,
    ) -> None:
        """Complete a bounded action batch in one transaction."""

        finish_file_actions(self._connection, action_ids, status, detail)

    def mark_file_actions_applying(
        self,
        actions: Iterable[tuple[int, str]],
    ) -> None:
        """Persist expected identities before any filesystem syscall."""

        mark_file_actions_applying(self._connection, actions)

    def confirm_file_actions_applied(
        self,
        actions: Iterable[tuple[int, str]],
    ) -> None:
        """Store successful syscall receipts through an applying-state CAS."""

        confirm_file_actions_applied(self._connection, actions)

    def require_file_action_recovery(
        self,
        action_ids: Iterable[int],
        detail: str,
    ) -> None:
        """Preserve an uncertain post-frontier effect without retrying it."""

        finish_file_actions(
            self._connection,
            action_ids,
            "recovery_required",
            detail,
        )

    def record_file_action_reconciliation(
        self,
        reconciliation: FileActionReconciliation,
        *,
        actor: str,
        provenance_json: str,
        expected_previous_event_id: int | None,
        observed_ns: int | None = None,
    ) -> RecordedFileActionReconciliation:
        """Append read-only observation evidence; never retry the action."""

        return record_file_action_reconciliation(
            self._connection,
            reconciliation,
            actor=actor,
            provenance_json=provenance_json,
            expected_previous_event_id=expected_previous_event_id,
            observed_ns=observed_ns,
        )

    def store_action_summary(self, run_id: int, summary: ActionSummary) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO run_actions("
                "run_id,apply_actions,duplicate_candidates,duplicates_trashed,"
                "duplicate_skips,files_checked,types_detected,extensions_matching,"
                "unknown_types,type_cache_hits,type_cache_misses,type_cache_pruned,"
                "stale_inventory,"
                "rename_candidates,files_renamed,rename_skips,"
                "empty_directory_candidates,empty_directories_trashed,"
                "empty_directory_skips,errors) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    int(summary.apply_actions),
                    summary.duplicate_candidates,
                    summary.duplicates_trashed,
                    summary.duplicate_skips,
                    summary.files_checked,
                    summary.types_detected,
                    summary.extensions_matching,
                    summary.unknown_types,
                    summary.type_cache_hits,
                    summary.type_cache_misses,
                    summary.type_cache_pruned,
                    summary.stale_inventory,
                    summary.rename_candidates,
                    summary.files_renamed,
                    summary.rename_skips,
                    summary.empty_directory_candidates,
                    summary.empty_directories_trashed,
                    summary.empty_directory_skips,
                    summary.errors,
                ),
            )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "FrameworkState":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


# endregion [02]
