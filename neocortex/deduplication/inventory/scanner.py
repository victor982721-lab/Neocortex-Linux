"""SQLite-backed execution and publication of filesystem inventory scans."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from neocortex.progress import ProgressCallback, ProgressEvent, emit_progress

from ..domain.errors import InventoryError
from ..domain.models import ScanSummary
from .policy import InventoryExclusionPolicy, resolve_inventory_exclusion_policy
from .traversal import FileObservation, InventoryTraversal, RootIdentity, ScanCounters


DEFAULT_BATCH_SIZE = 5000
FILE_UPSERT_SQL = """
    INSERT INTO files(path, volume_id, file_id, size, mtime_ns, birthtime_ns, scan_id)
    VALUES(?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(scan_id, path) DO UPDATE SET
        volume_id=excluded.volume_id,
        file_id=excluded.file_id,
        size=excluded.size,
        mtime_ns=excluded.mtime_ns,
        birthtime_ns=excluded.birthtime_ns
"""

type InventoryRow = tuple[str, bytes, bytes, int, int, int, int]


def id_blob(value: int) -> bytes:
    """Encode an unsigned filesystem identity for the SQLite schema."""

    if value < 0 or value.bit_length() > 128:
        raise InventoryError("filesystem identity does not fit an unsigned 128-bit value")
    return value.to_bytes(16, "little")


class InventoryBatch:
    """Commit at most ``batch_size`` file rows in each transaction."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        scan_id: int,
        volume_id: int,
        batch_size: int,
    ) -> None:
        self._connection = connection
        self._scan_id = scan_id
        self._volume_blob = id_blob(volume_id)
        self._batch_size = batch_size
        self._rows: list[InventoryRow] = []

    def append(self, observation: FileObservation) -> None:
        self._rows.append(
            (
                observation.path,
                self._volume_blob,
                id_blob(observation.file_id),
                observation.size,
                observation.mtime_ns,
                observation.birthtime_ns,
                self._scan_id,
            )
        )

    @property
    def full(self) -> bool:
        return len(self._rows) >= self._batch_size

    def flush(self) -> None:
        if not self._rows:
            return
        with self._connection:
            self._connection.executemany(FILE_UPSERT_SQL, self._rows)
        self._rows.clear()


class InventoryScanner:
    """Publish one complete, root-identity-bound inventory scan."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def scan(
        self,
        root: str | Path,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        excluded_paths: Iterable[str | Path] | None = None,
        exclusion_policy: InventoryExclusionPolicy | None = None,
        progress: ProgressCallback | None = None,
    ) -> ScanSummary:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        effective_policy = resolve_inventory_exclusion_policy(
            excluded_paths,
            exclusion_policy,
        )
        root_identity = RootIdentity.capture(root)
        scan_id = self._begin_scan(root_identity, effective_policy.signature)
        batch = InventoryBatch(
            self._connection,
            scan_id=scan_id,
            volume_id=root_identity.volume_id,
            batch_size=batch_size,
        )
        traversal = InventoryTraversal(
            root_identity,
            row_sink=batch,
            exclusion_policy=effective_policy,
            progress=progress,
        )
        try:
            self._emit_started(progress)
            counters = traversal.run()
            root_identity.verify_unchanged()
            self._complete_scan(scan_id, counters)
        except BaseException as exc:
            try:
                self._complete_interrupted_scan(scan_id, traversal.counters)
            except Exception as recovery_error:
                exc.add_note(
                    "inventory interruption could not finalize its scan: "
                    f"{type(recovery_error).__name__}: {recovery_error}"
                )
            raise
        if counters.errors:
            raise InventoryError(
                f"inventory scan {scan_id} was partial with "
                f"{counters.errors} traversal errors; it was not published"
            )
        self._emit_completed(progress, counters.files_seen)
        return counters.summary(scan_id, root_identity.path)

    def _begin_scan(
        self,
        root: RootIdentity,
        inventory_policy_signature: str,
    ) -> int:
        cursor = self._connection.execute(
            """INSERT INTO scans(
            root,root_volume_id,root_file_id,root_birthtime_ns,started_ns,
            inventory_policy_signature)
            VALUES(?,?,?,?,?,?)""",
            (
                root.path,
                id_blob(root.volume_id),
                id_blob(root.file_id),
                root.birthtime_ns,
                time.time_ns(),
                inventory_policy_signature,
            ),
        )
        if cursor.lastrowid is None:
            raise InventoryError("SQLite did not return a scan identifier")
        self._connection.commit()
        return int(cursor.lastrowid)

    def _complete_scan(self, scan_id: int, counters: ScanCounters) -> None:
        status = "complete" if counters.errors == 0 else "partial"
        with self._connection:
            self._connection.execute(
                "UPDATE scans SET completed_ns=?,files_seen=?,directories_seen=?,"
                "bytes_seen=?,skipped_links=?,excluded_directories=?,errors=?,status=? "
                "WHERE scan_id=?",
                (
                    time.time_ns(),
                    counters.files_seen,
                    counters.directories_seen,
                    counters.bytes_seen,
                    counters.skipped_links,
                    counters.excluded_directories,
                    counters.errors,
                    status,
                    scan_id,
                ),
            )

    def _complete_interrupted_scan(
        self,
        scan_id: int,
        counters: ScanCounters,
    ) -> None:
        with self._connection:
            result = self._connection.execute(
                """UPDATE scans SET completed_ns=?,
                files_seen=(SELECT COUNT(*) FROM files WHERE scan_id=?),
                directories_seen=?,
                bytes_seen=(SELECT COALESCE(SUM(size),0) FROM files WHERE scan_id=?),
                skipped_links=?,excluded_directories=?,errors=?,status='partial'
                WHERE scan_id=? AND completed_ns IS NULL AND status='building'""",
                (
                    time.time_ns(),
                    scan_id,
                    counters.directories_seen,
                    scan_id,
                    counters.skipped_links,
                    counters.excluded_directories,
                    counters.errors,
                    scan_id,
                ),
            )
            if result.rowcount != 1:
                raise InventoryError(f"cannot finalize interrupted inventory scan {scan_id}")

    @staticmethod
    def _emit_started(progress: ProgressCallback | None) -> None:
        emit_progress(
            progress,
            ProgressEvent("dedup", "inventory", "Inventariando archivos", 0, unit="archivos"),
        )

    @staticmethod
    def _emit_completed(progress: ProgressCallback | None, files_seen: int) -> None:
        emit_progress(
            progress,
            ProgressEvent(
                "dedup",
                "inventory",
                "Inventario completado",
                files_seen,
                files_seen,
                "archivos",
                True,
            ),
        )


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "FILE_UPSERT_SQL",
    "InventoryBatch",
    "InventoryRow",
    "InventoryScanner",
    "id_blob",
]
