"""Incremental SQLite inventory and cached fingerprint storage."""
# region [00] Contexto del módulo
# Módulo: _02_Deduplicacion/inventory.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

from neocortex.platform_policy import sqlite_path_collation

from .errors import InventoryError
from .hashing import snapshot_path
from .inventory_scan import (
    DEFAULT_BATCH_SIZE as DEFAULT_BATCH_SIZE,
    DEFAULT_EXCLUDED_PATHS as DEFAULT_EXCLUDED_PATHS,
    DEFAULT_GENERATED_DIRECTORY_FRAGMENTS as DEFAULT_GENERATED_DIRECTORY_FRAGMENTS,
    DEFAULT_GENERATED_DIRECTORY_NAMES as DEFAULT_GENERATED_DIRECTORY_NAMES,
    DEFAULT_GENERATED_DIRECTORY_PREFIXES as DEFAULT_GENERATED_DIRECTORY_PREFIXES,
    DEFAULT_GENERATED_FILE_SUFFIXES as DEFAULT_GENERATED_FILE_SUFFIXES,
    DEFAULT_INVENTORY_EXCLUSION_POLICY as DEFAULT_INVENTORY_EXCLUSION_POLICY,
    FILE_ATTRIBUTE_HIDDEN as FILE_ATTRIBUTE_HIDDEN,
    FILE_ATTRIBUTE_REPARSE_POINT as FILE_ATTRIBUTE_REPARSE_POINT,
    INTERNAL_DIRECTORY_PREFIXES as INTERNAL_DIRECTORY_PREFIXES,
    InventoryExclusionPolicy as InventoryExclusionPolicy,
    InventoryScanner,
    exclusion_path_keys as exclusion_path_keys,
    id_blob as _id_blob,
    is_excluded_directory as is_excluded_directory,
    validate_inventory_root as validate_inventory_root,
)
from .inventory_schema import (
    SCHEMA_VERSION as SCHEMA_VERSION,
    _connect_existing,
    configure_inventory_connection,
    initialize_inventory_schema,
)
from .models import DuplicateGroup, FileSnapshot, InventoryCheckpoint, ScanSummary
from _03_Progreso import ProgressCallback
# endregion [01]

# region [02] Implementación

_PATH_COLLATION = sqlite_path_collation()


PRUNE_BATCH_SIZE = 1000


class DedupIndex:
    """Persistent inventory and fingerprint cache with bounded transactions."""

    def __init__(self, database: str | Path):
        self.path = Path(database)
        initialize_inventory_schema(self.path)
        self._connection = _connect_existing(self.path)
        try:
            configure_inventory_connection(self._connection)
        except BaseException:
            self._connection.close()
            raise

    def scan(
        self,
        root: str | Path,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        excluded_paths: Iterable[str | Path] | None = None,
        exclusion_policy: InventoryExclusionPolicy | None = None,
        progress: ProgressCallback | None = None,
    ) -> ScanSummary:
        """Inventory files under a legacy root list or compiled policy.

        The default policy excludes internal Neocortex work trees, dependency
        environments, generated caches, and Python bytecode. Path matching
        uses the filesystem's native path collation (case-insensitive on
        Windows, case-sensitive on Linux). Links and junctions are never
        followed. ``excluded_paths`` remains compatible; callers needing
        recursive names or file rules pass ``exclusion_policy``.
        """

        return InventoryScanner(self._connection).scan(
            root,
            batch_size=batch_size,
            excluded_paths=excluded_paths,
            exclusion_policy=exclusion_policy,
            progress=progress,
        )

    def mark_abandoned_scans(self) -> tuple[int, ...]:
        """Close unpublishable ``building`` scans while preserving their rows."""

        completed_ns = time.time_ns()
        with self._connection:
            self._connection.execute(
                """UPDATE inventory_checkpoints SET valid=0,updated_ns=?
                WHERE valid=1 AND scan_id IN(
                    SELECT scan_id FROM scans WHERE status='building'
                )""",
                (completed_ns,),
            )
            scan_ids = tuple(
                int(row[0])
                for row in self._connection.execute(
                    "SELECT scan_id FROM scans WHERE status='building' ORDER BY scan_id"
                ).fetchall()
            )
            if not scan_ids:
                return ()
            result = self._connection.execute(
                """UPDATE scans SET completed_ns=COALESCE(completed_ns,?),
                files_seen=(SELECT COUNT(*) FROM files f
                            WHERE f.scan_id=scans.scan_id),
                directories_seen=COALESCE(directories_seen,0),
                bytes_seen=(SELECT COALESCE(SUM(size),0) FROM files f
                            WHERE f.scan_id=scans.scan_id),
                skipped_links=COALESCE(skipped_links,0),
                excluded_directories=COALESCE(excluded_directories,0),
                errors=COALESCE(errors,0),status='partial'
                WHERE status='building'""",
                (completed_ns,),
            )
            if result.rowcount != len(scan_ids):
                raise InventoryError("abandoned inventory recovery changed concurrently")
        return scan_ids

    def inventory_checkpoint(self, root: str | Path) -> InventoryCheckpoint | None:
        """Return the policy-bound checkpoint, including invalid markers."""

        root_path = os.path.abspath(os.fspath(root))
        row = self._connection.execute(
            "SELECT c.scan_id,c.volume,c.journal_id,c.next_usn,c.valid,"
            "s.inventory_policy_signature FROM inventory_checkpoints c "
            "JOIN scans s ON s.scan_id=c.scan_id WHERE c.root=?",
            (root_path,),
        ).fetchone()
        if row is None:
            return None
        scan_id, volume, journal_id, next_usn, valid, policy_signature = row
        journal_values = (volume, journal_id, next_usn)
        if any(value is None for value in journal_values) and not all(
            value is None for value in journal_values
        ):
            raise InventoryError("inventory publication has a partial USN cursor")
        return InventoryCheckpoint(
            root_path,
            int(scan_id),
            None if volume is None else str(volume),
            None if journal_id is None else int(journal_id),
            None if next_usn is None else int(next_usn),
            bool(valid),
            (
                None
                if policy_signature is None
                else self._validated_inventory_policy_signature(policy_signature)
            ),
        )

    def bind_inventory_checkpoint(self, checkpoint: InventoryCheckpoint) -> None:
        """Publish a completed inventory and its exact USN boundary atomically."""

        bound_checkpoint = self._policy_bound_checkpoint(checkpoint)
        with self._connection:
            self._write_inventory_checkpoint(bound_checkpoint)

    @staticmethod
    def _validated_inventory_policy_signature(value: object) -> str:
        if not isinstance(value, str) or not value or value.strip() != value:
            raise InventoryError("inventory policy signature is missing or malformed")
        if len(value.encode("utf-8")) > 4096:
            raise InventoryError("inventory policy signature exceeds 4096 bytes")
        return value

    def scan_inventory_policy_signature(self, scan_id: int) -> str | None:
        """Return the producing policy signature, or ``None`` for legacy scans."""

        row = self._connection.execute(
            "SELECT inventory_policy_signature FROM scans WHERE scan_id=?",
            (scan_id,),
        ).fetchone()
        if row is None:
            raise InventoryError(f"unknown scan_id: {scan_id}")
        if row[0] is None:
            return None
        return self._validated_inventory_policy_signature(row[0])

    def require_scan_inventory_policy_signature(
        self,
        scan_id: int,
        expected_signature: str,
    ) -> None:
        """Fail closed unless a scan was produced by the expected policy."""

        expected = self._validated_inventory_policy_signature(expected_signature)
        observed = self.scan_inventory_policy_signature(scan_id)
        if observed != expected:
            raise InventoryError(f"scan {scan_id} inventory policy signature does not match")

    def _require_publishable_scan(self, scan_id: int) -> tuple[Path, str]:
        row = self._connection.execute(
            """SELECT root,status,completed_ns,files_seen,directories_seen,
            bytes_seen,skipped_links,excluded_directories,errors,
            (SELECT COUNT(*) FROM files f WHERE f.scan_id=scans.scan_id),
            (SELECT COALESCE(SUM(size),0) FROM files f WHERE f.scan_id=scans.scan_id),
            inventory_policy_signature
            FROM scans WHERE scan_id=?""",
            (scan_id,),
        ).fetchone()
        if (
            row is None
            or str(row[1]) != "complete"
            or row[2] is None
            or any(value is None for value in row[3:])
            or int(row[8]) != 0
            or int(row[3]) != int(row[9])
            or int(row[5]) != int(row[10])
        ):
            raise InventoryError(
                f"scan {scan_id} is not a complete, internally consistent inventory"
            )
        policy_signature = self._validated_inventory_policy_signature(row[11])
        return Path(str(row[0])), policy_signature

    def _policy_bound_checkpoint(
        self,
        checkpoint: InventoryCheckpoint,
    ) -> InventoryCheckpoint:
        journal_values = (
            checkpoint.volume,
            checkpoint.journal_id,
            checkpoint.next_usn,
        )
        if any(value is None for value in journal_values) and not all(
            value is None for value in journal_values
        ):
            raise InventoryError("inventory publication has a partial USN cursor")
        root_path = os.path.abspath(checkpoint.root)
        scan_root, scan_signature = self._require_publishable_scan(checkpoint.scan_id)
        normalized_scan_root = os.path.abspath(os.fspath(scan_root))
        if os.path.normcase(root_path) != os.path.normcase(normalized_scan_root):
            raise InventoryError(
                f"checkpoint root {root_path} does not match scan root {normalized_scan_root}"
            )
        if (
            checkpoint.inventory_policy_signature is not None
            and checkpoint.inventory_policy_signature != scan_signature
        ):
            raise InventoryError("checkpoint inventory policy signature does not match its scan")
        return InventoryCheckpoint(
            root_path,
            checkpoint.scan_id,
            checkpoint.volume,
            checkpoint.journal_id,
            checkpoint.next_usn,
            checkpoint.valid,
            scan_signature,
        )

    def _write_inventory_checkpoint(self, checkpoint: InventoryCheckpoint) -> None:
        signature = self._validated_inventory_policy_signature(
            checkpoint.inventory_policy_signature
        )
        matching_scan = self._connection.execute(
            "SELECT 1 FROM scans WHERE scan_id=? AND inventory_policy_signature=?",
            (checkpoint.scan_id, signature),
        ).fetchone()
        if matching_scan is None:
            raise InventoryError("checkpoint inventory policy signature does not match its scan")
        self._connection.execute(
            """INSERT INTO inventory_checkpoints(
                root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(root) DO UPDATE SET
                    scan_id=excluded.scan_id,
                    volume=excluded.volume,
                    journal_id=excluded.journal_id,
                    next_usn=excluded.next_usn,
                    valid=excluded.valid,
                    updated_ns=excluded.updated_ns""",
            (
                os.path.abspath(checkpoint.root),
                checkpoint.scan_id,
                checkpoint.volume,
                (None if checkpoint.journal_id is None else str(checkpoint.journal_id)),
                checkpoint.next_usn,
                int(checkpoint.valid),
                time.time_ns(),
            ),
        )

    def scan_summary(self, scan_id: int) -> ScanSummary:
        """Load the persisted summary for a reusable completed inventory."""

        row = self._connection.execute(
            "SELECT root,files_seen,directories_seen,bytes_seen,skipped_links,"
            "excluded_directories,errors FROM scans WHERE scan_id=? "
            "AND completed_ns IS NOT NULL AND status='complete' AND errors=0",
            (scan_id,),
        ).fetchone()
        if row is None or any(value is None for value in row[1:]):
            raise InventoryError(f"scan {scan_id} has no reusable completed summary")
        return ScanSummary(scan_id, str(row[0]), *(int(value) for value in row[1:]))

    def refresh_scan_aggregates(self, scan_id: int) -> None:
        """Refresh mutable file totals after applying an incremental USN window."""

        with self._connection:
            result = self._connection.execute(
                "UPDATE scans SET files_seen=(SELECT COUNT(*) FROM files WHERE scan_id=?),"
                "bytes_seen=(SELECT COALESCE(SUM(size),0) FROM files WHERE scan_id=?) "
                "WHERE scan_id=? AND completed_ns IS NOT NULL AND status='complete'",
                (scan_id, scan_id, scan_id),
            )
            if result.rowcount != 1:
                raise InventoryError(f"cannot refresh unknown completed scan {scan_id}")

    def prune_obsolete_state(
        self,
        *,
        protected_scan_ids: Iterable[int] | None = None,
    ) -> dict[str, int]:
        """Prune only after every cross-store inventory hold is supplied.

        ``None`` fails closed because this owner cannot discover framework
        references by itself. Current and previous publications are always
        retained in addition to the explicit holds.
        """

        removed = {
            "plan_members": 0,
            "plan_groups": 0,
            "plan_summaries": 0,
            "files": 0,
            "fingerprints": 0,
        }
        if protected_scan_ids is None:
            return removed
        protected = tuple(sorted(set(protected_scan_ids)))
        if any(
            isinstance(scan_id, bool) or not isinstance(scan_id, int) or scan_id < 1
            for scan_id in protected
        ):
            raise ValueError("protected inventory scan identifiers must be positive")
        explicit_holds = ",".join(str(scan_id) for scan_id in protected) or "NULL"

        retained_scans = (
            """SELECT s.scan_id FROM scans s
        WHERE s.status='building'
           OR EXISTS(SELECT 1 FROM inventory_checkpoints c
                     WHERE c.scan_id=s.scan_id AND c.valid=1)
           OR s.scan_id IN ("""
            + explicit_holds
            + """)
           OR (s.status='complete' AND s.errors=0 AND s.completed_ns IS NOT NULL
               AND s.scan_id=(SELECT MAX(previous.scan_id) FROM scans previous
                 JOIN inventory_checkpoints c ON c.root=previous.root AND c.valid=1
                 WHERE previous.root=s.root AND previous.status='complete'
                   AND previous.errors=0 AND previous.completed_ns IS NOT NULL
                   AND previous.scan_id<c.scan_id))
           OR (s.status='complete' AND s.errors=0 AND s.completed_ns IS NOT NULL
               AND s.scan_id>COALESCE((SELECT MAX(c.scan_id)
                                       FROM inventory_checkpoints c
                                       WHERE c.root=s.root AND c.valid=1),0))"""
        )

        while True:
            group_ids = self._connection.execute(
                "SELECT group_id FROM planned_duplicate_groups "
                f"WHERE scan_id NOT IN ({retained_scans}) LIMIT ?",
                (PRUNE_BATCH_SIZE,),
            ).fetchall()
            if not group_ids:
                break
            with self._connection:
                removed["plan_members"] += int(
                    self._connection.executemany(
                        "DELETE FROM planned_duplicate_members WHERE group_id=?",
                        group_ids,
                    ).rowcount
                )
                removed["plan_groups"] += int(
                    self._connection.executemany(
                        "DELETE FROM planned_duplicate_groups WHERE group_id=?",
                        group_ids,
                    ).rowcount
                )

        while True:
            scan_ids = self._connection.execute(
                "SELECT scan_id FROM duplicate_plan_summaries "
                f"WHERE scan_id NOT IN ({retained_scans}) LIMIT ?",
                (PRUNE_BATCH_SIZE,),
            ).fetchall()
            if not scan_ids:
                break
            with self._connection:
                removed["plan_summaries"] += int(
                    self._connection.executemany(
                        "DELETE FROM duplicate_plan_summaries WHERE scan_id=?",
                        scan_ids,
                    ).rowcount
                )

        while True:
            rows = self._connection.execute(
                f"SELECT scan_id,path FROM files WHERE scan_id NOT IN ({retained_scans}) LIMIT ?",
                (PRUNE_BATCH_SIZE,),
            ).fetchall()
            if not rows:
                break
            with self._connection:
                removed["files"] += int(
                    self._connection.executemany(
                        "DELETE FROM files WHERE scan_id=? AND path=?", rows
                    ).rowcount
                )

        while True:
            fingerprints = self._connection.execute(
                """SELECT volume_id,file_id,size,mtime_ns,algorithm
                FROM fingerprints WHERE NOT EXISTS(
                    SELECT 1 FROM files f
                    WHERE f.scan_id IN ("""
                + retained_scans
                + """)
                      AND f.volume_id=fingerprints.volume_id
                      AND f.file_id=fingerprints.file_id
                      AND f.size=fingerprints.size
                      AND f.mtime_ns=fingerprints.mtime_ns
                      AND f.birthtime_ns=fingerprints.birthtime_ns)
                LIMIT ?""",
                (PRUNE_BATCH_SIZE,),
            ).fetchall()
            if not fingerprints:
                break
            with self._connection:
                removed["fingerprints"] += int(
                    self._connection.executemany(
                        """DELETE FROM fingerprints WHERE volume_id=? AND file_id=?
                        AND size=? AND mtime_ns=? AND algorithm=?""",
                        fingerprints,
                    ).rowcount
                )
        return removed

    def snapshots(self, scan_id: int) -> Iterator[FileSnapshot]:
        rows = self._connection.execute(
            "SELECT path, volume_id, file_id, size, mtime_ns, birthtime_ns "
            "FROM files WHERE scan_id=? ORDER BY path",
            (scan_id,),
        )
        for path, volume, file_id, size, mtime, birth in rows:
            yield FileSnapshot(
                path,
                int.from_bytes(volume, "little"),
                int.from_bytes(file_id, "little"),
                size,
                mtime,
                birth,
            )

    def published_snapshots(self, root: str | Path) -> Iterator[FileSnapshot]:
        """Stream one atomically selected published generation for *root*.

        The checkpoint selection and file read intentionally share one SQL
        statement.  A caller must not emulate this by pairing
        :meth:`inventory_checkpoint` with :meth:`snapshots`, because a
        concurrent publisher may prune the selected generation between calls.
        """

        rows = self._connection.execute(
            """SELECT f.path,f.volume_id,f.file_id,f.size,f.mtime_ns,f.birthtime_ns
            FROM inventory_checkpoints c
            JOIN scans s ON s.scan_id=c.scan_id
            JOIN files f ON f.scan_id=c.scan_id
            WHERE c.root=? AND c.valid=1
              AND s.inventory_policy_signature IS NOT NULL
            ORDER BY f.path""",
            (os.path.abspath(os.fspath(root)),),
        )
        for path, volume, file_id, size, mtime, birth in rows:
            yield FileSnapshot(
                path,
                int.from_bytes(volume, "little"),
                int.from_bytes(file_id, "little"),
                size,
                mtime,
                birth,
            )

    def size_collision_groups(self, scan_id: int) -> Iterator[tuple[FileSnapshot, ...]]:
        sizes = self._connection.execute(
            "SELECT size FROM files WHERE scan_id=? AND size>0 "
            "GROUP BY size HAVING COUNT(*) > 1 "
            "ORDER BY size",
            (scan_id,),
        )
        for (size,) in sizes:
            rows = self._connection.execute(
                "SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns "
                "FROM files WHERE scan_id=? AND size=? ORDER BY path",
                (scan_id, size),
            )
            # Multiple names for the same file (hard links) are not duplicate
            # physical files and therefore collapse to one identity here. On
            # Windows, DirEntry.stat() deliberately exposes st_dev=st_ino=0
            # from cached directory metadata. scan() therefore stores the root
            # volume ID plus DirEntry.inode(); candidates are refreshed here to
            # detect mutations without adding one os.stat() per unique-size file.
            unique: dict[tuple[int, int], FileSnapshot] = {}
            for (
                path,
                recorded_volume,
                recorded_file_id,
                recorded_size,
                recorded_mtime,
                recorded_birthtime,
            ) in rows:
                try:
                    snapshot = snapshot_path(path)
                except OSError:
                    continue
                if (
                    snapshot.identity
                    != (
                        int.from_bytes(recorded_volume, "little"),
                        int.from_bytes(recorded_file_id, "little"),
                    )
                    or snapshot.size != recorded_size
                    or snapshot.mtime_ns != recorded_mtime
                    or snapshot.birthtime_ns != recorded_birthtime
                ):
                    # The inventory is stale; never hash the new state under an
                    # old candidate decision.
                    continue
                unique.setdefault(snapshot.identity, snapshot)
            if len(unique) > 1:
                yield tuple(unique.values())

    def cached_fingerprint(self, snapshot: FileSnapshot, algorithm: str) -> bytes | None:
        row = self._connection.execute(
            "SELECT digest FROM fingerprints WHERE volume_id=? AND file_id=? "
            "AND size=? AND mtime_ns=? AND birthtime_ns=? AND algorithm=?",
            (
                _id_blob(snapshot.volume_id),
                _id_blob(snapshot.file_id),
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                algorithm,
            ),
        ).fetchone()
        return None if row is None else bytes(row[0])

    def store_fingerprint(self, snapshot: FileSnapshot, algorithm: str, digest: bytes) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO fingerprints"
                "(volume_id, file_id, size, mtime_ns, birthtime_ns, algorithm, digest) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    _id_blob(snapshot.volume_id),
                    _id_blob(snapshot.file_id),
                    snapshot.size,
                    snapshot.mtime_ns,
                    snapshot.birthtime_ns,
                    algorithm,
                    digest,
                ),
            )

    def store_fingerprints(
        self,
        algorithm: str,
        rows: Iterable[tuple[FileSnapshot, bytes]],
    ) -> None:
        """Persist a bounded fingerprint batch in one WAL transaction."""

        with self._connection:
            self._connection.executemany(
                """INSERT OR REPLACE INTO fingerprints(
                volume_id,file_id,size,mtime_ns,birthtime_ns,algorithm,digest)
                VALUES(?,?,?,?,?,?,?)""",
                (
                    (
                        _id_blob(snapshot.volume_id),
                        _id_blob(snapshot.file_id),
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.birthtime_ns,
                        algorithm,
                        digest,
                    )
                    for snapshot, digest in rows
                ),
            )

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

        upsert_rows = list(upserts)
        path_rows = [(os.path.abspath(os.fspath(path)), scan_id) for path in remove_paths]
        identity_rows = [
            (_id_blob(volume), _id_blob(file_id), scan_id) for volume, file_id in remove_identities
        ]
        with self._connection:
            if checkpoint is not None:
                checkpoint = self._policy_bound_checkpoint(checkpoint)
            if path_rows:
                self._connection.executemany(
                    "DELETE FROM files WHERE path=? AND scan_id=?", path_rows
                )
            if identity_rows:
                self._connection.executemany(
                    "DELETE FROM files WHERE volume_id=? AND file_id=? AND scan_id=?",
                    identity_rows,
                )
            for snapshot in upsert_rows:
                volume = _id_blob(snapshot.volume_id)
                file_id = _id_blob(snapshot.file_id)
                # Refresh all hard-link aliases already known to the inventory.
                self._connection.execute(
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
                self._connection.execute(
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
            if checkpoint is not None:
                aggregate_result = self._connection.execute(
                    "UPDATE scans SET "
                    "files_seen=(SELECT COUNT(*) FROM files WHERE scan_id=?),"
                    "bytes_seen=(SELECT COALESCE(SUM(size),0) FROM files WHERE scan_id=?) "
                    "WHERE scan_id=? AND completed_ns IS NOT NULL "
                    "AND status='complete' AND errors=0",
                    (scan_id, scan_id, scan_id),
                )
                if aggregate_result.rowcount != 1:
                    raise InventoryError(f"cannot publish reconciliation for scan {scan_id}")
                self._write_inventory_checkpoint(checkpoint)

    def file_count(self, scan_id: int) -> int:
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM files WHERE scan_id=?", (scan_id,)
            ).fetchone()[0]
        )

    def scan_root(self, scan_id: int) -> Path:
        row = self._connection.execute(
            "SELECT root FROM scans WHERE scan_id=?", (scan_id,)
        ).fetchone()
        if row is None:
            raise InventoryError(f"unknown scan_id: {scan_id}")
        return Path(row[0])

    def scan_root_identity(self, scan_id: int) -> tuple[int, int, int]:
        """Return the durable volume, file and birth-time identity of a scan root."""

        row = self._connection.execute(
            "SELECT root_volume_id,root_file_id,root_birthtime_ns FROM scans WHERE scan_id=?",
            (scan_id,),
        ).fetchone()
        if row is None:
            raise InventoryError(f"unknown scan_id: {scan_id}")
        if any(value is None for value in row):
            raise InventoryError(
                f"scan {scan_id} has no durable root identity; a full rescan is required"
            )
        return (
            int.from_bytes(row[0], "little"),
            int.from_bytes(row[1], "little"),
            int(row[2]),
        )

    def contains_identity(self, scan_id: int, volume_id: int, file_id: int) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM files WHERE scan_id=? AND volume_id=? AND file_id=? LIMIT 1",
                (scan_id, _id_blob(volume_id), _id_blob(file_id)),
            ).fetchone()
            is not None
        )

    def size_candidate_file_count(self, scan_id: int) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(SUM(candidate_count), 0) FROM ("
            "SELECT COUNT(*) AS candidate_count FROM files WHERE scan_id=? AND size>0 "
            "GROUP BY size HAVING COUNT(*) > 1)",
            (scan_id,),
        ).fetchone()
        return int(row[0])

    def size_collision_sizes(self, scan_id: int) -> Iterator[tuple[int, int]]:
        """Stream size buckets without materializing their file members."""

        rows = self._connection.execute(
            "SELECT size,COUNT(*) FROM files WHERE scan_id=? AND size>0 "
            "GROUP BY size HAVING COUNT(*)>1 ORDER BY size",
            (scan_id,),
        )
        for size, count in rows:
            yield int(size), int(count)

    def begin_planning_fingerprints(self) -> None:
        """Create disk-spillable temporary tables for one bounded planning run."""

        self._connection.execute("PRAGMA temp_store=FILE")
        self._connection.execute("PRAGMA cache_size=-32768")
        self._connection.executescript(
            f"""
            DROP TABLE IF EXISTS temp.planning_seen;
            DROP TABLE IF EXISTS temp.planning_fingerprints;
            CREATE TEMP TABLE planning_seen(
                volume_id BLOB NOT NULL,
                file_id BLOB NOT NULL,
                PRIMARY KEY(volume_id,file_id)
            ) WITHOUT ROWID;
            CREATE TEMP TABLE planning_fingerprints(
                stage TEXT NOT NULL,
                digest BLOB NOT NULL,
                path TEXT NOT NULL COLLATE {_PATH_COLLATION},
                volume_id BLOB NOT NULL,
                file_id BLOB NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                PRIMARY KEY(stage,volume_id,file_id)
            ) WITHOUT ROWID;
            CREATE INDEX planning_fingerprint_collision_idx
                ON planning_fingerprints(stage,digest);
            """
        )

    def clear_planning_fingerprints(self) -> None:
        with self._connection:
            self._connection.execute("DELETE FROM planning_seen")
            self._connection.execute("DELETE FROM planning_fingerprints")

    def claim_planning_identity(self, snapshot: FileSnapshot) -> bool:
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO planning_seen VALUES(?,?)",
            (_id_blob(snapshot.volume_id), _id_blob(snapshot.file_id)),
        )
        return cursor.rowcount == 1

    def store_planning_fingerprints(
        self,
        stage: str,
        rows: Iterable[tuple[FileSnapshot, bytes]],
    ) -> None:
        with self._connection:
            self._connection.executemany(
                """INSERT OR REPLACE INTO planning_fingerprints(
                stage,digest,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    (
                        stage,
                        digest,
                        snapshot.path,
                        _id_blob(snapshot.volume_id),
                        _id_blob(snapshot.file_id),
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.birthtime_ns,
                    )
                    for snapshot, digest in rows
                ),
            )

    def planning_collision_member_count(self, stage: str) -> int:
        row = self._connection.execute(
            """SELECT COALESCE(SUM(member_count),0) FROM(
            SELECT COUNT(*) member_count FROM planning_fingerprints
            WHERE stage=? GROUP BY digest HAVING COUNT(*)>1)""",
            (stage,),
        ).fetchone()
        return int(row[0])

    def iter_planning_collision_members(self, stage: str) -> Iterator[tuple[bytes, FileSnapshot]]:
        rows = self._connection.execute(
            f"""SELECT w.digest,w.path,w.volume_id,w.file_id,w.size,w.mtime_ns,
            w.birthtime_ns FROM planning_fingerprints w JOIN(
                SELECT digest FROM planning_fingerprints WHERE stage=?
                GROUP BY digest HAVING COUNT(*)>1
            ) collisions ON collisions.digest=w.digest
            WHERE w.stage=? ORDER BY w.digest,w.mtime_ns DESC,
            w.birthtime_ns DESC,w.path COLLATE {_PATH_COLLATION} DESC""",
            (stage, stage),
        )
        for digest, path, volume, file_id, size, mtime, birth in rows:
            yield (
                bytes(digest),
                FileSnapshot(
                    path,
                    int.from_bytes(volume, "little"),
                    int.from_bytes(file_id, "little"),
                    size,
                    mtime,
                    birth,
                ),
            )

    def snapshots_by_size(self, scan_id: int, size: int) -> Iterator[FileSnapshot]:
        rows = self._connection.execute(
            "SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns "
            "FROM files WHERE scan_id=? AND size=? ORDER BY path",
            (scan_id, size),
        )
        for path, volume, file_id, item_size, mtime, birth in rows:
            yield FileSnapshot(
                path,
                int.from_bytes(volume, "little"),
                int.from_bytes(file_id, "little"),
                item_size,
                mtime,
                birth,
            )

    def file_count_by_size(self, scan_id: int, size: int) -> int:
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM files WHERE scan_id=? AND size=?",
                (scan_id, size),
            ).fetchone()[0]
        )

    def begin_duplicate_plan(self, scan_id: int) -> None:
        """Discard any incomplete prior plan for this scan."""

        with self._connection:
            self._connection.execute(
                "DELETE FROM planned_duplicate_members WHERE group_id IN "
                "(SELECT group_id FROM planned_duplicate_groups WHERE scan_id=?)",
                (scan_id,),
            )
            self._connection.execute(
                "DELETE FROM planned_duplicate_groups WHERE scan_id=?", (scan_id,)
            )
            self._connection.execute(
                "DELETE FROM duplicate_plan_summaries WHERE scan_id=?", (scan_id,)
            )

    def store_duplicate_groups(self, scan_id: int, groups: Iterable[DuplicateGroup]) -> None:
        """Persist a bounded group batch and its immutable file snapshots."""

        with self._connection:
            for group in groups:
                result = self._connection.execute(
                    "INSERT INTO planned_duplicate_groups"
                    "(scan_id,size,keep_path,redundant_count,reclaimable_bytes,full_fingerprint) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        scan_id,
                        group.size,
                        group.keep.path,
                        len(group.redundant),
                        group.reclaimable_bytes,
                        group.full_fingerprint,
                    ),
                )
                if result.lastrowid is None:
                    raise InventoryError("SQLite did not return a duplicate-group identifier")
                group_id = int(result.lastrowid)
                members = (group.keep, *group.redundant)
                self._connection.executemany(
                    "INSERT INTO planned_duplicate_members"
                    "(group_id,member_order,role,path,volume_id,file_id,size,mtime_ns,birthtime_ns) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        (
                            group_id,
                            order,
                            "keep" if order == 0 else "redundant",
                            member.path,
                            _id_blob(member.volume_id),
                            _id_blob(member.file_id),
                            member.size,
                            member.mtime_ns,
                            member.birthtime_ns,
                        )
                        for order, member in enumerate(members)
                    ),
                )

    def complete_duplicate_plan(
        self,
        scan_id: int,
        *,
        group_count: int,
        redundant_files: int,
        reclaimable_bytes: int,
    ) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO duplicate_plan_summaries VALUES(?,?,?,?,?)",
                (
                    scan_id,
                    group_count,
                    redundant_files,
                    reclaimable_bytes,
                    time.time_ns(),
                ),
            )

    def iter_duplicate_groups(self, scan_id: int) -> Iterator[DuplicateGroup]:
        """Stream a persisted plan in descending reclaimable-byte order."""

        rows = self._connection.execute(
            "SELECT g.group_id,g.size,g.full_fingerprint,m.member_order,m.path,"
            "m.volume_id,m.file_id,m.size,m.mtime_ns,m.birthtime_ns "
            "FROM planned_duplicate_groups g JOIN planned_duplicate_members m "
            "ON m.group_id=g.group_id WHERE g.scan_id=? "
            f"ORDER BY g.reclaimable_bytes DESC,g.keep_path COLLATE {_PATH_COLLATION},"
            "g.group_id,m.member_order",
            (scan_id,),
        )
        current_group: int | None = None
        group_size = 0
        fingerprint = ""
        members: list[FileSnapshot] = []
        for (
            group_id,
            size,
            digest,
            _order,
            path,
            volume,
            file_id,
            member_size,
            mtime,
            birth,
        ) in rows:
            if current_group is not None and group_id != current_group:
                yield DuplicateGroup(group_size, members[0], tuple(members[1:]), fingerprint)
                members = []
            current_group = group_id
            group_size = size
            fingerprint = digest
            members.append(
                FileSnapshot(
                    path,
                    int.from_bytes(volume, "little"),
                    int.from_bytes(file_id, "little"),
                    member_size,
                    mtime,
                    birth,
                )
            )
        if current_group is not None:
            yield DuplicateGroup(group_size, members[0], tuple(members[1:]), fingerprint)

    def snapshots_excluding_planned_redundant(self, scan_id: int) -> Iterator[FileSnapshot]:
        """Stream files that would survive the persisted dry-run plan."""

        rows = self._connection.execute(
            "SELECT f.path,f.volume_id,f.file_id,f.size,f.mtime_ns,f.birthtime_ns "
            "FROM files f WHERE f.scan_id=? AND NOT EXISTS("
            "SELECT 1 FROM planned_duplicate_groups g "
            "JOIN planned_duplicate_members m ON m.group_id=g.group_id "
            "WHERE g.scan_id=f.scan_id AND m.role='redundant' "
            "AND m.path=f.path) ORDER BY f.path",
            (scan_id,),
        )
        for path, volume, file_id, size, mtime, birth in rows:
            yield FileSnapshot(
                path,
                int.from_bytes(volume, "little"),
                int.from_bytes(file_id, "little"),
                size,
                mtime,
                birth,
            )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "DedupIndex":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


# endregion [02]
