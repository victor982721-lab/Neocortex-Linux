"""Atomic incremental reconciliation for a published inventory generation."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from ..domain.errors import InventoryError
from ..domain.models import FileSnapshot, InventoryCheckpoint
from .repository_scans import ScanCheckpointRepositoryMixin
from .scan import id_blob as _id_blob


def _reconciliation_path_rows(
    paths: Iterable[str | Path],
    scan_id: int,
) -> list[tuple[str, int]]:
    return [(os.path.abspath(os.fspath(path)), scan_id) for path in paths]


def _reconciliation_identity_rows(
    identities: Iterable[tuple[int, int]],
    scan_id: int,
) -> list[tuple[bytes, bytes, int]]:
    return [(_id_blob(volume), _id_blob(file_id), scan_id) for volume, file_id in identities]


def _remove_reconciled_rows(
    connection: sqlite3.Connection,
    path_rows: list[tuple[str, int]],
    identity_rows: list[tuple[bytes, bytes, int]],
) -> None:
    if path_rows:
        connection.executemany("DELETE FROM files WHERE path=? AND scan_id=?", path_rows)
    if identity_rows:
        connection.executemany(
            "DELETE FROM files WHERE volume_id=? AND file_id=? AND scan_id=?",
            identity_rows,
        )


def _upsert_reconciled_snapshot(
    connection: sqlite3.Connection,
    scan_id: int,
    snapshot: FileSnapshot,
) -> None:
    volume = _id_blob(snapshot.volume_id)
    file_id = _id_blob(snapshot.file_id)
    connection.execute(
        "UPDATE files SET size=?, mtime_ns=?, birthtime_ns=? "
        "WHERE volume_id=? AND file_id=? AND scan_id=?",
        (
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            volume,
            file_id,
            scan_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO files(path, volume_id, file_id, size, mtime_ns, birthtime_ns, scan_id)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(scan_id, path) DO UPDATE SET
            volume_id=excluded.volume_id,
            file_id=excluded.file_id,
            size=excluded.size,
            mtime_ns=excluded.mtime_ns,
            birthtime_ns=excluded.birthtime_ns
        """,
        (
            snapshot.path,
            volume,
            file_id,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            scan_id,
        ),
    )


def _refresh_reconciliation_aggregates(
    connection: sqlite3.Connection,
    scan_id: int,
) -> None:
    result = connection.execute(
        "UPDATE scans SET "
        "files_seen=(SELECT COUNT(*) FROM files WHERE scan_id=?),"
        "bytes_seen=(SELECT COALESCE(SUM(size),0) FROM files WHERE scan_id=?) "
        "WHERE scan_id=? AND completed_ns IS NOT NULL "
        "AND status='complete' AND errors=0",
        (scan_id, scan_id, scan_id),
    )
    if result.rowcount != 1:
        raise InventoryError(f"cannot publish reconciliation for scan {scan_id}")


class ReconciliationRepositoryMixin(ScanCheckpointRepositoryMixin):
    """Apply one bounded USN window and checkpoint it in one transaction."""

    def apply_reconciliation(
        self,
        scan_id: int,
        *,
        upserts: Iterable[FileSnapshot] = (),
        remove_paths: Iterable[str | Path] = (),
        remove_identities: Iterable[tuple[int, int]] = (),
        checkpoint: InventoryCheckpoint | None = None,
    ) -> None:
        """Apply one USN batch and optionally advance its checkpoint atomically."""

        if checkpoint is not None and checkpoint.scan_id != scan_id:
            raise InventoryError("checkpoint scan_id does not match reconciliation scan")

        upsert_rows = tuple(upserts)
        path_rows = _reconciliation_path_rows(remove_paths, scan_id)
        identity_rows = _reconciliation_identity_rows(remove_identities, scan_id)
        with self._connection:
            if checkpoint is not None:
                checkpoint = self._policy_bound_checkpoint(checkpoint)
            _remove_reconciled_rows(self._connection, path_rows, identity_rows)
            for snapshot in upsert_rows:
                _upsert_reconciled_snapshot(self._connection, scan_id, snapshot)
            if checkpoint is not None:
                _refresh_reconciliation_aggregates(self._connection, scan_id)
                self._write_inventory_checkpoint(checkpoint)


__all__ = ["ReconciliationRepositoryMixin"]
