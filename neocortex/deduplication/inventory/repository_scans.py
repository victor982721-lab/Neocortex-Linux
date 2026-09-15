"""Scan generations, publication checkpoints, and retention for the inventory."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from neocortex.progress import ProgressCallback

from ..domain.errors import InventoryError
from ..domain.models import InventoryCheckpoint, ScanSummary
from .generation import inventory_content_digest as _inventory_content_digest
from .scan import (
    DEFAULT_BATCH_SIZE,
    InventoryExclusionPolicy,
    InventoryScanner,
    InventoryWorkBudget,
)


PRUNE_BATCH_SIZE = 1000


def resolve_scan_id(connection: sqlite3.Connection, scan_id: int) -> int:
    """Resolve a legacy scan identifier to its immutable current successor."""

    if isinstance(scan_id, bool) or not isinstance(scan_id, int) or scan_id < 1:
        raise ValueError("scan_id must be a positive integer")
    current = scan_id
    seen: set[int] = set()
    while current not in seen:
        seen.add(current)
        row = connection.execute(
            "SELECT successor_scan_id FROM inventory_scan_successors "
            "WHERE predecessor_scan_id=?",
            (current,),
        ).fetchone()
        if row is None:
            return current
        current = int(row[0])
    raise InventoryError("inventory scan successor cycle detected")


def scan_inventory(
    connection: sqlite3.Connection,
    root: str | Path,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    excluded_paths: Iterable[str | Path] | None = None,
    exclusion_policy: InventoryExclusionPolicy | None = None,
    progress: ProgressCallback | None = None,
    checkpoint_path: str | Path | None = None,
    resume: bool = False,
    deterministic: bool = False,
    work_budget: InventoryWorkBudget | None = None,
    scanner_type: type[InventoryScanner] = InventoryScanner,
) -> ScanSummary:
    """Run the canonical scanner against an already configured repository."""

    return scanner_type(connection).scan(
        root,
        batch_size=batch_size,
        excluded_paths=excluded_paths,
        exclusion_policy=exclusion_policy,
        progress=progress,
        checkpoint_path=checkpoint_path,
        resume=resume,
        deterministic=deterministic,
        work_budget=work_budget,
    )


def _delete_batches(
    connection: sqlite3.Connection,
    *,
    select_sql: str,
    delete_sql: str,
) -> int:
    removed = 0
    while True:
        rows = connection.execute(select_sql, (PRUNE_BATCH_SIZE,)).fetchall()
        if not rows:
            return removed
        with connection:
            removed += int(connection.executemany(delete_sql, rows).rowcount)


def _delete_duplicate_group_batches(
    connection: sqlite3.Connection,
    retained_scans: str,
) -> tuple[int, int]:
    members_removed = 0
    groups_removed = 0
    while True:
        group_ids = connection.execute(
            "SELECT group_id FROM planned_duplicate_groups "
            f"WHERE scan_id NOT IN ({retained_scans}) LIMIT ?",
            (PRUNE_BATCH_SIZE,),
        ).fetchall()
        if not group_ids:
            return members_removed, groups_removed
        with connection:
            members_removed += int(
                connection.executemany(
                    "DELETE FROM planned_duplicate_members WHERE group_id=?",
                    group_ids,
                ).rowcount
            )
            groups_removed += int(
                connection.executemany(
                    "DELETE FROM planned_duplicate_groups WHERE group_id=?",
                    group_ids,
                ).rowcount
            )


def _validate_retention_references(
    connection: sqlite3.Connection,
    protected_scan_ids: tuple[int, ...],
) -> None:
    """Refuse pruning when the owner cannot account for a durable reference.

    The legacy plan tables predate the v13 foreign keys and therefore need an
    explicit consistency check here.  A plan row, a checkpoint, or a
    successor is evidence, not disposable cache; silently pruning around an
    orphan would make the remaining evidence impossible to interpret.
    """

    try:
        foreign_keys_enabled = int(
            connection.execute("PRAGMA foreign_keys").fetchone()[0]
        )
        if foreign_keys_enabled != 1:
            raise InventoryError("inventory retention requires foreign keys to be enabled")
        foreign_key_violation = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise InventoryError("inventory retention could not validate foreign keys") from exc
    if foreign_key_violation is not None:
        raise InventoryError("inventory retention requires a consistent foreign-key graph")

    successor_map = {
        int(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT predecessor_scan_id,successor_scan_id FROM inventory_scan_successors"
        ).fetchall()
    }
    visited: set[int] = set()
    for start in successor_map:
        if start in visited:
            continue
        path: set[int] = set()
        current = start
        while current in successor_map:
            if current in path:
                raise InventoryError("inventory retention found a successor cycle")
            if current in visited:
                break
            path.add(current)
            visited.add(current)
            current = successor_map[current]

    if protected_scan_ids:
        placeholders = ",".join("?" for _ in protected_scan_ids)
        present = {
            int(row[0])
            for row in connection.execute(
                f"SELECT scan_id FROM scans WHERE scan_id IN ({placeholders})",
                protected_scan_ids,
            ).fetchall()
        }
        missing_id = next(
            (scan_id for scan_id in protected_scan_ids if scan_id not in present),
            None,
        )
        if missing_id is not None:
            raise InventoryError(f"inventory retention protected scan {missing_id} is missing")

    # ``duplicate_plan_summaries`` and ``planned_duplicate_groups`` retain
    # historical shapes without a scan FK.  Their source scan is still a
    # durable ownership edge, so do not delete any payload while that edge is
    # unresolved.  The same guard also makes orphan members fail closed.
    for table in ("duplicate_plan_summaries", "planned_duplicate_groups"):
        unresolved = connection.execute(
            f"SELECT {table}.scan_id FROM {table} "
            f"LEFT JOIN scans ON scans.scan_id={table}.scan_id "
            f"WHERE scans.scan_id IS NULL LIMIT 1"
        ).fetchone()
        if unresolved is not None:
            raise InventoryError(
                f"inventory retention found an unresolved {table} reference"
            )
    orphan_member = connection.execute(
        """SELECT 1 FROM planned_duplicate_members m
        LEFT JOIN planned_duplicate_groups g ON g.group_id=m.group_id
        WHERE g.group_id IS NULL LIMIT 1"""
    ).fetchone()
    if orphan_member is not None:
        raise InventoryError("inventory retention found an unresolved plan member reference")


def _retained_scan_query(protected_scan_ids: tuple[int, ...]) -> str:
    """Return a bounded recursive query for payloads still reachable.

    Successor edges are retained as metadata for the lifetime of the owner;
    when one endpoint is still reachable, retaining the connected generation
    payload is necessary for historical readers and for replaying a durable
    plan/checkpoint without silently replacing its source with a newer scan.
    """

    explicit = ",".join(str(scan_id) for scan_id in protected_scan_ids) or "NULL"
    return (
        "WITH RECURSIVE seed(scan_id) AS ("
        "SELECT s.scan_id FROM scans s WHERE s.status='building' "
        "OR EXISTS(SELECT 1 FROM inventory_checkpoints c WHERE c.scan_id=s.scan_id) "
        f"OR s.scan_id IN ({explicit}) "
        "OR EXISTS(SELECT 1 FROM duplicate_plan_summaries p WHERE p.scan_id=s.scan_id) "
        "OR EXISTS(SELECT 1 FROM planned_duplicate_groups g WHERE g.scan_id=s.scan_id) "
        "OR EXISTS(SELECT 1 FROM duplicate_plan_heads h WHERE h.scan_id=s.scan_id) "
        "OR (s.status='complete' AND s.errors=0 AND s.completed_ns IS NOT NULL "
        "AND s.scan_id=(SELECT MAX(previous.scan_id) FROM scans previous "
        "WHERE previous.root=s.root AND previous.status='complete' "
        "AND previous.errors=0 AND previous.completed_ns IS NOT NULL "
        "AND previous.scan_id<COALESCE((SELECT MAX(c.scan_id) "
        "FROM inventory_checkpoints c WHERE c.root=s.root AND c.valid=1),0))) "
        "OR (s.status='complete' AND s.errors=0 AND s.completed_ns IS NOT NULL "
        "AND s.scan_id>COALESCE((SELECT MAX(c.scan_id) FROM inventory_checkpoints c "
        "WHERE c.root=s.root AND c.valid=1),0))),"
        "reachable(scan_id) AS ("
        "SELECT scan_id FROM seed "
        "UNION "
        "SELECT edge.predecessor_scan_id FROM inventory_scan_successors edge "
        "JOIN reachable ON reachable.scan_id=edge.successor_scan_id "
        "UNION "
        "SELECT edge.successor_scan_id FROM inventory_scan_successors edge "
        "JOIN reachable ON reachable.scan_id=edge.predecessor_scan_id) "
        "SELECT scan_id FROM reachable"
    )


class ScanCheckpointRepositoryMixin:
    """Persist and publish inventory scan generations and their checkpoints."""

    _connection: sqlite3.Connection

    def current_scan_id(self, scan_id: int) -> int:
        """Return the current immutable generation for a legacy scan handle."""

        return resolve_scan_id(self._connection, scan_id)

    def scan_content_digest(self, scan_id: int) -> bytes:
        """Return the canonical content identity of a complete scan head."""

        return self._ensure_inventory_content_digest(scan_id)

    def _ensure_inventory_content_digest(self, scan_id: int) -> bytes:
        """Load or derive the content identity for one complete generation."""

        current = resolve_scan_id(self._connection, scan_id)
        row = self._connection.execute(
            "SELECT content_digest FROM inventory_generation_heads WHERE scan_id=?",
            (current,),
        ).fetchone()
        if row is not None:
            return bytes(row[0])
        status = self._connection.execute(
            "SELECT status FROM scans WHERE scan_id=?", (current,)
        ).fetchone()
        if status is None:
            raise InventoryError(f"unknown scan_id: {scan_id}")
        if str(status[0]) != "complete":
            raise InventoryError(f"scan {current} has no complete inventory content identity")
        digest = _inventory_content_digest(self._connection, current)
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO inventory_generation_heads("
                "scan_id,content_digest,created_ns) VALUES(?,?,?)",
                (current, digest, time.time_ns()),
            )
        row = self._connection.execute(
            "SELECT content_digest FROM inventory_generation_heads WHERE scan_id=?",
            (current,),
        ).fetchone()
        if row is None:
            raise InventoryError(f"cannot publish inventory content identity for scan {current}")
        return bytes(row[0])

    def _create_inventory_successor(self, scan_id: int, *, reason: str) -> int:
        """Copy one complete generation before applying an incremental change."""

        source_id = resolve_scan_id(self._connection, scan_id)
        existing = self._connection.execute(
            "SELECT successor_scan_id FROM inventory_scan_successors "
            "WHERE predecessor_scan_id=?",
            (source_id,),
        ).fetchone()
        if existing is not None:
            return resolve_scan_id(self._connection, int(existing[0]))
        source = self._connection.execute(
            """SELECT root,root_volume_id,root_file_id,root_birthtime_ns,
            started_ns,completed_ns,files_seen,directories_seen,bytes_seen,
            skipped_links,excluded_directories,errors,status,inventory_policy_signature
            FROM scans WHERE scan_id=?""",
            (source_id,),
        ).fetchone()
        if source is None or str(source[12]) != "complete" or source[5] is None:
            raise InventoryError("incremental reconciliation requires a complete inventory scan")
        # Ensure the source has an identity before any successor can become
        # visible.  The original rows remain untouched and are available for
        # historical audit until the normal retention owner prunes them.
        source_digest = self._ensure_inventory_content_digest(source_id)
        now = time.time_ns()
        cursor = self._connection.execute(
            """INSERT INTO scans(
            root,root_volume_id,root_file_id,root_birthtime_ns,started_ns,
            completed_ns,files_seen,directories_seen,bytes_seen,skipped_links,
            excluded_directories,errors,status,inventory_policy_signature)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                source[0],
                source[1],
                source[2],
                source[3],
                now,
                now,
                source[6],
                source[7],
                source[8],
                source[9],
                source[10],
                source[11],
                "complete",
                source[13],
            ),
        )
        if cursor.lastrowid is None:
            raise InventoryError("SQLite did not return an inventory successor identifier")
        successor_id = int(cursor.lastrowid)
        self._connection.execute(
            """INSERT INTO files(
            scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
            SELECT ?,path,volume_id,file_id,size,mtime_ns,birthtime_ns
            FROM files WHERE scan_id=?""",
            (successor_id, source_id),
        )
        self._connection.execute(
            "INSERT INTO inventory_generation_heads(scan_id,content_digest,created_ns) "
            "VALUES(?,?,?)",
            (successor_id, source_digest, now),
        )
        self._connection.execute(
            "INSERT INTO inventory_scan_successors("
            "predecessor_scan_id,successor_scan_id,created_ns,reason) "
            "VALUES(?,?,?,?)",
            (source_id, successor_id, now, reason),
        )
        self._connection.execute(
            "UPDATE duplicate_plan_heads SET status='superseded' WHERE scan_id=?",
            (source_id,),
        )
        return successor_id

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
            resolve_scan_id(self._connection, int(scan_id)),
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

        scan_id = resolve_scan_id(self._connection, scan_id)
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
        scan_id = resolve_scan_id(self._connection, scan_id)
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
        current_scan_id = resolve_scan_id(self._connection, checkpoint.scan_id)
        scan_root, scan_signature = self._require_publishable_scan(current_scan_id)
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
            current_scan_id,
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
        scan_id = resolve_scan_id(self._connection, checkpoint.scan_id)
        matching_scan = self._connection.execute(
            "SELECT 1 FROM scans WHERE scan_id=? AND inventory_policy_signature=?",
            (scan_id, signature),
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
                scan_id,
                checkpoint.volume,
                (None if checkpoint.journal_id is None else str(checkpoint.journal_id)),
                checkpoint.next_usn,
                int(checkpoint.valid),
                time.time_ns(),
            ),
        )

    def scan_summary(self, scan_id: int) -> ScanSummary:
        """Load the persisted summary for a reusable completed inventory."""

        scan_id = resolve_scan_id(self._connection, scan_id)
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

        scan_id = resolve_scan_id(self._connection, scan_id)
        with self._connection:
            result = self._connection.execute(
                "UPDATE scans SET files_seen=(SELECT COUNT(*) FROM files WHERE scan_id=?),"
                "bytes_seen=(SELECT COALESCE(SUM(size),0) FROM files WHERE scan_id=?) "
                "WHERE scan_id=? AND completed_ns IS NOT NULL AND status='complete'",
                (scan_id, scan_id, scan_id),
            )
            if result.rowcount != 1:
                raise InventoryError(f"cannot refresh unknown completed scan {scan_id}")

    def scan_root(self, scan_id: int) -> Path:
        scan_id = resolve_scan_id(self._connection, scan_id)
        row = self._connection.execute(
            "SELECT root FROM scans WHERE scan_id=?", (scan_id,)
        ).fetchone()
        if row is None:
            raise InventoryError(f"unknown scan_id: {scan_id}")
        return Path(row[0])

    def scan_root_identity(self, scan_id: int) -> tuple[int, int, int]:
        """Return the durable volume, file and birth-time identity of a scan root."""

        scan_id = resolve_scan_id(self._connection, scan_id)
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

    def prune_obsolete_state(
        self,
        *,
        protected_scan_ids: Iterable[int] | None = None,
    ) -> dict[str, int]:
        """Prune disposable payload only after all inventory holds are known.

        ``None`` fails closed because this owner cannot discover framework
        references by itself. Current and previous publications are always
        retained in addition to explicit holds. Checkpoints, durable plans,
        and the complete successor component of any retained scan are also
        payload roots. Scan metadata and publication/reference rows are never
        removed here: their foreign keys are the durable audit trail.
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
        requested_protected = tuple(protected_scan_ids)
        if any(
            isinstance(scan_id, bool) or not isinstance(scan_id, int) or scan_id < 1
            for scan_id in requested_protected
        ):
            raise ValueError("protected inventory scan identifiers must be positive")
        protected = tuple(sorted(set(requested_protected)))

        # Validate before the first batch.  In particular, the plan tables
        # intentionally retain legacy shapes without scan FKs; deleting
        # around an unresolved row would destroy the only durable provenance
        # available for that row.
        _validate_retention_references(self._connection, protected)
        retained_scans = _retained_scan_query(protected)

        removed["plan_members"], removed["plan_groups"] = _delete_duplicate_group_batches(
            self._connection,
            retained_scans,
        )
        removed["plan_summaries"] = _delete_batches(
            self._connection,
            select_sql=(
                "SELECT scan_id FROM duplicate_plan_summaries "
                f"WHERE scan_id NOT IN ({retained_scans}) LIMIT ?"
            ),
            delete_sql="DELETE FROM duplicate_plan_summaries WHERE scan_id=?",
        )
        removed["files"] = _delete_batches(
            self._connection,
            select_sql=(
                f"SELECT scan_id,path FROM files WHERE scan_id NOT IN ({retained_scans}) LIMIT ?"
            ),
            delete_sql="DELETE FROM files WHERE scan_id=? AND path=?",
        )
        removed["fingerprints"] = _delete_batches(
            self._connection,
            select_sql=(
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
                LIMIT ?"""
            ),
            delete_sql=(
                """DELETE FROM fingerprints WHERE volume_id=? AND file_id=?
                AND size=? AND mtime_ns=? AND algorithm=?"""
            ),
        )
        return removed


__all__ = ["PRUNE_BATCH_SIZE", "ScanCheckpointRepositoryMixin", "scan_inventory"]
