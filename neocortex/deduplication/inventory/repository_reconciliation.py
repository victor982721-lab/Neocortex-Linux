"""Atomic path/identity reconciliation for a published inventory generation."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from ..domain.errors import InventoryError
from ..domain.models import FileSnapshot, InventoryCheckpoint
from .repository_scans import ScanCheckpointRepositoryMixin
from .generation import inventory_content_digest
from .scan import id_blob as _id_blob


@contextmanager
def _sqlite_work_checkpoint(
    connection: sqlite3.Connection,
    work_check: Callable[[], None] | None,
):
    """Bridge cooperative cancellation through SQLite's VM progress handler."""

    if work_check is None:
        yield
        return
    pending: list[BaseException | None] = [None]

    def progress() -> int:
        try:
            work_check()
        except BaseException as exc:  # SQLite reports this as ``interrupted``.
            pending[0] = exc
            return 1
        return 0

    connection.set_progress_handler(progress, 1_000)
    try:
        yield
    except sqlite3.OperationalError as exc:
        if pending[0] is not None:
            raise pending[0] from exc
        raise
    finally:
        connection.set_progress_handler(None, 0)


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
    # FileSnapshot intentionally has no change version. An observed update
    # must not retain an older ctime observation as if it were fresh.
    connection.execute(
        "DELETE FROM inventory_file_change_versions WHERE scan_id=? AND (path=? OR path IN ("
        "SELECT path FROM files WHERE scan_id=? AND volume_id=? AND file_id=?))",
        (scan_id, snapshot.path, scan_id, volume, file_id),
    )
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
    """Apply one bounded identity/path reconciliation in one transaction."""

    def apply_reconciliation(
        self,
        scan_id: int,
        *,
        upserts: Iterable[FileSnapshot] = (),
        remove_paths: Iterable[str | Path] = (),
        remove_identities: Iterable[tuple[int, int]] = (),
        checkpoint: InventoryCheckpoint | None = None,
        work_check: Callable[[], None] | None = None,
    ) -> None:
        """Apply observed path/identity changes and optionally publish a checkpoint."""

        if checkpoint is not None and checkpoint.scan_id != scan_id:
            raise InventoryError("checkpoint scan_id does not match reconciliation scan")

        upsert_rows = tuple(upserts)
        path_rows = _reconciliation_path_rows(remove_paths, scan_id)
        identity_rows = _reconciliation_identity_rows(remove_identities, scan_id)
        with _sqlite_work_checkpoint(self._connection, work_check):
            with self._connection:
                current_scan_id = self.current_scan_id(scan_id)
                if checkpoint is not None:
                    checkpoint = self._policy_bound_checkpoint(checkpoint)
                if upsert_rows or path_rows or identity_rows:
                    current_scan_id = self._create_inventory_successor(
                        current_scan_id,
                        reason="observed-reconciliation",
                        work_check=work_check,
                    )
                    if checkpoint is not None:
                        checkpoint = replace(checkpoint, scan_id=current_scan_id)
                path_rows = [(path, current_scan_id) for path, _scan in path_rows]
                identity_rows = [
                    (volume, file_id, current_scan_id)
                    for volume, file_id, _scan in identity_rows
                ]
                _remove_reconciled_rows(self._connection, path_rows, identity_rows)
                for index, snapshot in enumerate(upsert_rows):
                    if work_check is not None and index % 128 == 0:
                        work_check()
                    _upsert_reconciled_snapshot(self._connection, current_scan_id, snapshot)
                if upsert_rows or path_rows or identity_rows:
                    digest = inventory_content_digest(
                        self._connection,
                        current_scan_id,
                        work_check=work_check,
                    )
                    self._connection.execute(
                        "UPDATE inventory_generation_heads SET content_digest=? "
                        "WHERE scan_id=?",
                        (digest, current_scan_id),
                    )
                if checkpoint is not None:
                    _refresh_reconciliation_aggregates(self._connection, current_scan_id)
                    self._write_inventory_checkpoint(checkpoint)


__all__ = ["ReconciliationRepositoryMixin"]
