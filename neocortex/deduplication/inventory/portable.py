"""Portable metadata observation and immutable publication reuse."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from neocortex.progress import ProgressCallback, ProgressEvent, emit_progress
from neocortex.persistence.sqlite_cancellation import SQLiteCancellationBridge, sqlite_cancellation_scope

from ..domain.errors import InventoryError
from ..domain.models import InventoryCheckpoint, ScanSummary
from .generation import portable_observation_content_digest
from .policy import InventoryExclusionPolicy
from .scanner import DEFAULT_BATCH_SIZE, InventoryWorkBudget, _InventoryWorkState, id_blob
from .traversal import FileObservation, InventoryTraversal, RootIdentity

if TYPE_CHECKING:
    from .index import DedupIndex


@dataclass(frozen=True, slots=True)
class PortableInventoryResult:
    scan: ScanSummary
    observed_files: int
    changed_files: int
    persistent_file_rows_written: int
    reused_generation: bool


class _ObservationBatch:
    """Spill one observation to TEMP without rewriting the published payload."""

    def __init__(self, connection: sqlite3.Connection, work: _InventoryWorkState) -> None:
        self.connection = connection
        self.work = work
        self.rows: list[tuple[object, ...]] = []

    @property
    def full(self) -> bool:
        return len(self.rows) >= DEFAULT_BATCH_SIZE

    def append(self, item: FileObservation) -> None:
        self.rows.append((item.path, id_blob(item.volume_id), id_blob(item.file_id),
                          item.size, item.mtime_ns, item.birthtime_ns, item.ctime_ns))

    def flush(self) -> None:
        if self.rows:
            self.work.check()
            with self.connection:
                self.connection.executemany(
                    "INSERT INTO portable_observed_files VALUES(?,?,?,?,?,?,?)", self.rows,
                )
            self.rows.clear()
            self.work.check()


def _compatible_checkpoint(
    index: DedupIndex, root: RootIdentity, policy: InventoryExclusionPolicy,
) -> InventoryCheckpoint | None:
    checkpoint = index.inventory_checkpoint(root.path)
    if checkpoint is None or not checkpoint.valid or checkpoint.inventory_policy_signature != policy.signature:
        return None
    if index.scan_root_identity(checkpoint.scan_id) != (root.volume_id, root.file_id, root.birthtime_ns):
        return None
    index.scan_content_digest(checkpoint.scan_id)  # Operational freshness remains mandatory.
    index.scan_summary(checkpoint.scan_id)
    missing = index._connection.execute(
        "SELECT 1 FROM files f LEFT JOIN inventory_file_change_versions v "
        "ON v.scan_id=f.scan_id AND v.path=f.path WHERE f.scan_id=? AND v.ctime_ns IS NULL LIMIT 1",
        (checkpoint.scan_id,),
    ).fetchone()
    return checkpoint if missing is None else None


def _begin_publication(connection: sqlite3.Connection, work: _InventoryWorkState) -> None:
    """Acquire the writer lock without hiding cancellation in SQLite's wait.

    SQLite's busy handler does not invoke the SQL progress handler. Short
    attempts retain the owner's original total wait limit while admitting
    cancellation/deadline checks between attempts; the connection's setting
    is restored even if acquisition or admission fails.
    """

    original_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    started = time.monotonic()
    try:
        while True:
            work.check()
            elapsed_ms = int((time.monotonic() - started) * 1000)
            remaining_ms = max(0, original_timeout - elapsed_ms)
            connection.execute(f"PRAGMA busy_timeout={min(100, remaining_ms)}")
            try:
                connection.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                work.check()
                error_code = getattr(exc, "sqlite_errorcode", None)
                if (
                    error_code is None
                    or error_code & 0xFF != sqlite3.SQLITE_BUSY
                    or (time.monotonic() - started) * 1000 >= original_timeout
                ):
                    raise
    finally:
        connection.execute(f"PRAGMA busy_timeout={original_timeout}")


def prepare_portable_inventory(
    index: DedupIndex,
    root: Path,
    *,
    exclusion_policy: InventoryExclusionPolicy,
    progress: ProgressCallback | None = None,
    work_budget: InventoryWorkBudget | None = None,
) -> PortableInventoryResult:
    """Observe every file, reusing only an unchanged compatible publication.

    Change times reject metadata replay after a same-stat rewrite; they do
    not authorize content-cache hits. Content owners retain their own checks.
    Changed generations retain a complete immutable snapshot. They materialize
    the current observation once, without copying obsolete rows first; their
    physical file-row writes are still proportional to the current inventory.
    """

    budget = InventoryWorkBudget() if work_budget is None else work_budget
    if not isinstance(budget, InventoryWorkBudget):
        raise TypeError("work_budget must be an InventoryWorkBudget")
    work = _InventoryWorkState(budget, files=0, bytes_seen=0)
    connection = index._connection
    if connection.in_transaction:
        raise InventoryError("portable inventory requires its own transaction")
    work.check()
    source_data_version = int(connection.execute("PRAGMA data_version").fetchone()[0])
    identity = RootIdentity.capture(root)
    if exclusion_policy.excludes_directory(identity.path):
        raise InventoryError("inventory root is excluded by its inventory policy")
    checkpoint = _compatible_checkpoint(index, identity, exclusion_policy)
    if checkpoint is None:
        scan = index.scan(root, exclusion_policy=exclusion_policy, progress=progress, work_budget=work_budget)
        return PortableInventoryResult(scan, scan.files_seen, scan.files_seen, scan.files_seen, False)

    connection.execute("DROP TABLE IF EXISTS temp.portable_observed_files")
    connection.execute("DROP TABLE IF EXISTS temp.portable_file_delta")
    connection.execute("CREATE TEMP TABLE portable_observed_files("
                       "path TEXT PRIMARY KEY COLLATE BINARY,volume_id BLOB NOT NULL,file_id BLOB NOT NULL,"
                       "size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,birthtime_ns INTEGER NOT NULL,ctime_ns INTEGER"
                       ") WITHOUT ROWID")
    previous = index.scan_summary(checkpoint.scan_id)
    previous_digest = index.scan_content_digest(checkpoint.scan_id)
    try:
        emit_progress(progress, ProgressEvent("dedup", "inventory", "Observando cambios del inventario", 0, unit="archivos"))
        traversal = InventoryTraversal(
            identity, row_sink=_ObservationBatch(connection, work), exclusion_policy=exclusion_policy,
            progress=progress, work_check=work.check, file_work_check=work.check_file,
        )
        counters = traversal.run()
        identity.verify_unchanged()
        work.check()
        if counters.errors:
            raise InventoryError(f"portable inventory observation had {counters.errors} traversal errors")
        # This owner has exclusive use of its inventory connection here.
        # The transaction is inside the bridge so interrupted writes roll back
        # before the original cancellation/deadline is re-raised.
        with sqlite_cancellation_scope(connection, SQLiteCancellationBridge(work.check)):
            if connection.execute("SELECT 1 FROM portable_observed_files WHERE ctime_ns IS NULL OR ctime_ns<0 LIMIT 1").fetchone():
                raise InventoryError("portable inventory observation lacks a valid change version")
            connection.execute(
                "CREATE TEMP TABLE portable_file_delta AS SELECT o.* FROM portable_observed_files o "
                "LEFT JOIN files f ON f.scan_id=? AND f.path=o.path "
                "LEFT JOIN inventory_file_change_versions v ON v.scan_id=f.scan_id AND v.path=f.path "
                "WHERE f.path IS NULL OR o.volume_id!=f.volume_id OR o.file_id!=f.file_id "
                "OR o.size!=f.size OR o.mtime_ns!=f.mtime_ns OR o.birthtime_ns!=f.birthtime_ns "
                "OR v.ctime_ns IS NULL OR o.ctime_ns!=v.ctime_ns",
                (checkpoint.scan_id,),
            )
            upserts = int(connection.execute("SELECT COUNT(*) FROM portable_file_delta").fetchone()[0])
            removals = int(connection.execute(
                "SELECT COUNT(*) FROM files f WHERE f.scan_id=? "
                "AND NOT EXISTS(SELECT 1 FROM portable_observed_files o WHERE o.path=f.path)",
                (checkpoint.scan_id,),
            ).fetchone()[0])
            observed = counters.summary(checkpoint.scan_id, identity.path)
            unchanged = upserts == removals == 0 and observed == previous
            prepared_digest = (
                previous_digest if unchanged
                else portable_observation_content_digest(connection, work_check=work.check)
            )
            with connection:
                _begin_publication(connection, work)
                work.check()
                identity.verify_unchanged()
                current = index.inventory_checkpoint(identity.path)
                current_data_version = int(connection.execute("PRAGMA data_version").fetchone()[0])
                if (
                    current_data_version != source_data_version
                    or current != checkpoint
                    or index.scan_summary(checkpoint.scan_id) != previous
                    or index.scan_content_digest(checkpoint.scan_id) != previous_digest
                ):
                    raise InventoryError("portable inventory publication changed during observation")
                if unchanged:
                    result = PortableInventoryResult(previous, counters.files_seen, 0, 0, True)
                else:
                    successor = index._create_inventory_successor(
                        checkpoint.scan_id, reason="portable-metadata-delta", copy_files=False,
                    )
                    written = connection.execute(
                        "INSERT INTO files(scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns) "
                        "SELECT ?,path,volume_id,file_id,size,mtime_ns,birthtime_ns "
                        "FROM portable_observed_files ORDER BY path", (successor,),
                    ).rowcount
                    if written != counters.files_seen:
                        raise InventoryError("portable inventory materialization count changed")
                    connection.execute(
                        "INSERT INTO inventory_file_change_versions(scan_id,path,ctime_ns) "
                        "SELECT ?,path,ctime_ns FROM portable_observed_files ORDER BY path", (successor,),
                    )
                    connection.execute(
                        "UPDATE scans SET completed_ns=?,files_seen=?,directories_seen=?,bytes_seen=?,"
                        "skipped_links=?,excluded_directories=?,errors=? WHERE scan_id=?",
                        (time.time_ns(), counters.files_seen, counters.directories_seen, counters.bytes_seen,
                         counters.skipped_links, counters.excluded_directories, counters.errors, successor),
                    )
                    connection.execute(
                        "UPDATE inventory_generation_heads SET content_digest=? WHERE scan_id=?",
                        (prepared_digest, successor),
                    )
                    index._write_inventory_checkpoint(replace(checkpoint, scan_id=successor))
                    work.check()
                    result = PortableInventoryResult(counters.summary(successor, identity.path),
                        counters.files_seen, upserts + removals, written, False)
        emit_progress(progress, ProgressEvent("dedup", "inventory", "Inventario observado y publicado",
                                              counters.files_seen, counters.files_seen, "archivos", True))
        return result
    finally:
        connection.execute("DROP TABLE IF EXISTS temp.portable_file_delta")
        connection.execute("DROP TABLE IF EXISTS temp.portable_observed_files")
