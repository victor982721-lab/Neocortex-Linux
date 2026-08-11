"""Single-writer lifecycle, routing snapshot, and cache repository."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/framework_state_writer.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from _01_Enumeracion import JournalCursor
from _02_Deduplicacion import FileSnapshot, InventoryExclusionPolicy

from .content_types import DetectedType
from .corpus_access import (
    CorpusAccessPolicy,
    CorpusMutationGuard,
    path_trees_intersect,
)
from .file_action_reconciliation_store import (
    RecordedFileActionReconciliation,
    record_file_action_reconciliation,
)
from .file_action_recovery import FileActionReconciliation
from .framework_schema import initialize_framework_schema
from .framework_state_common import (
    CACHE_PRUNE_BATCH_SIZE,
    FileActionSpec,
    begin_file_actions,
    confirm_file_actions_applied,
    corpus_mutation_guard,
    finish_file_actions,
    mark_file_actions_applying,
)
from .self_analysis import (
    SELF_ANALYSIS_MANIFEST_MESSAGE,
    SELF_ANALYSIS_MANIFEST_PHASE,
)
from .self_analysis_finalization import (
    CompletedCodeRoute,
    SelfAnalysisCompletionEvidence,
    SelfAnalysisRunEvidence,
    SelfAnalysisSafetyCounts,
)
from .sqlite_paths import existing_sqlite_uri
# endregion [01]

# region [02] Implementación

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
    from .self_analysis_status import quiescent_sqlite_database

    with quiescent_sqlite_database(database_path, timeout_seconds=60) as connection:
        try:
            row = connection.execute(
                """SELECT run_id,scan_id,corpus_access_mode,
                inventory_policy_signature,journal_volume,journal_id,end_usn,
                root,root_device_id_hex,root_file_id_hex,root_birthtime_ns
                FROM initial_runs
                WHERE root=? COLLATE NOCASE AND status='completed'
                AND scan_id IS NOT NULL
                AND run_kind IN ('initial','self_analysis')
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


class FrameworkState:
    """Own the long-lived writer connection for one orchestration run."""

    def __init__(
        self,
        database: str | Path,
        *,
        existing_only: bool = False,
    ):
        self.path = Path(database)
        target = existing_sqlite_uri(self.path) if existing_only else self.path
        self._connection = sqlite3.connect(
            target,
            uri=existing_only,
            timeout=60,
        )
        try:
            self._connection.execute("PRAGMA busy_timeout=60000")
            self._connection.execute("PRAGMA foreign_keys=ON")
            if int(self._connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                raise RuntimeError("framework state could not enable foreign keys")
            self._initialize()
        except BaseException:
            self._connection.close()
            raise

    def _initialize(self) -> None:
        initialize_framework_schema(self._connection, self._backfill_route_phases)

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

    def begin_self_analysis_run(
        self,
        policy: CorpusAccessPolicy,
        cursor: JournalCursor | None,
        *,
        state_directory: Path,
        inventory_policy_signature: str,
    ) -> int:
        """Start one root-identity-bound analyze-only inventory run."""

        if policy.mode != "analyze_only":
            raise ValueError("self-analysis requires analyze_only corpus access")
        policy.verify_root_identity()
        signature = inventory_policy_signature
        if not signature or signature.strip() != signature or len(signature.encode("utf-8")) > 4096:
            raise ValueError("inventory policy signature must be trimmed and bounded")
        owner_state = Path(os.path.abspath(os.path.realpath(self.path.parent)))
        try:
            owner_intersects_request = path_trees_intersect(
                state_directory,
                owner_state,
            )
            requested_metadata = os.stat(state_directory)
            owner_metadata = os.stat(owner_state)
        except (OSError, ValueError) as exc:
            raise ValueError("self-analysis state ownership cannot be verified") from exc
        if not owner_intersects_request or (
            int(requested_metadata.st_dev),
            int(requested_metadata.st_ino),
        ) != (
            int(owner_metadata.st_dev),
            int(owner_metadata.st_ino),
        ):
            raise ValueError("self-analysis state directory does not own this database")
        requested_state = owner_state
        try:
            intersects = path_trees_intersect(policy.root, requested_state)
        except (OSError, ValueError) as exc:
            raise ValueError("self-analysis root/state boundary cannot be verified") from exc
        if intersects:
            raise ValueError("self-analysis root and state directory must be disjoint")
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
                VALUES(?,?,'running','self_analysis','prepare',?,?, ?,?,?, ?,?,?,?,?,?)""",
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
                    str(requested_state),
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
            """SELECT run_id,scan_id,corpus_access_mode,
            inventory_policy_signature,journal_volume,journal_id,end_usn
            FROM initial_runs
            WHERE root=? COLLATE NOCASE AND status='completed'
            AND scan_id IS NOT NULL
            AND run_kind IN ('initial','self_analysis')
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
            if str(run_kind) not in {"initial", "self_analysis"} or str(status) != "running":
                raise ValueError(f"run {run_id} cannot bind inventory while {run_kind}/{status}")
            if str(run_kind) == "self_analysis" and candidate_rows != 0:
                raise ValueError("self-analysis cannot publish MIME route candidates")
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
                AND run_kind IN ('initial','self_analysis')
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

    def begin_route_runs(self, run_id: int, route_names: Iterable[str]) -> None:
        now = time.time_ns()
        routes = tuple(route_names)
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
                VALUES(?,?,'running',?,'route_start',?,?)""",
                ((run_id, route_name, now, now, source_run_id) for route_name in routes),
            )

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
            self._connection.execute(
                """INSERT INTO route_phase_runs(
                run_id,route_name,phase_name,status,started_ns,heartbeat_ns,
                source_run_id)
                VALUES(?,?,?,'running',?,?,?)
                ON CONFLICT(run_id,route_name,phase_name) DO UPDATE SET
                status='running',started_ns=excluded.started_ns,completed_ns=NULL,
                heartbeat_ns=excluded.heartbeat_ns,source_run_id=excluded.source_run_id,
                summary_json=NULL,error_type=NULL,error_message=NULL""",
                (
                    run_id,
                    route_name,
                    phase_name,
                    now,
                    now,
                    source_run_id,
                ),
            )
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
    ) -> None:
        now = time.time_ns()
        payload = (
            None
            if summary is None
            else json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        )
        with self._connection:
            self._connection.execute(
                """UPDATE route_phase_runs SET status='completed',completed_ns=?,
                heartbeat_ns=?,summary_json=?,error_type=NULL,error_message=NULL
                WHERE run_id=? AND route_name=? AND phase_name=?""",
                (now, now, payload, run_id, route_name, phase_name),
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
    ) -> None:
        payload = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        with self._connection:
            self._connection.execute(
                """UPDATE route_runs SET status='completed',completed_ns=?,
                current_phase='completed',heartbeat_ns=?,summary_json=?,
                error_type=NULL,error_message=NULL
                WHERE run_id=? AND route_name=? AND status='running'""",
                (time.time_ns(), time.time_ns(), payload, run_id, route_name),
            )

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
        return int(result.rowcount)

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

    def complete_self_analysis_run(
        self,
        run_id: int,
        cursor: JournalCursor | None,
        *,
        inventory_policy: InventoryExclusionPolicy,
        code_processing_signature: str,
        commands: Mapping[str, Sequence[str]],
    ) -> dict[str, object]:
        """Publish the protected completion manifest and status atomically."""

        if self._connection.in_transaction:
            raise RuntimeError("self-analysis finalization requires transaction ownership")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                """SELECT root,status,run_kind,corpus_access_mode,
                root_device_id_hex,root_file_id_hex,root_birthtime_ns,
                state_directory,inventory_policy_signature,scan_id,
                journal_volume,journal_id,start_usn,reconciliation_records,
                inventory_attempts,inventory_mode
                FROM initial_runs WHERE run_id=?""",
                (run_id,),
            ).fetchone()
            run_evidence = SelfAnalysisRunEvidence.decode(run_id, row)
            run_evidence.validate_inventory_boundary(cursor, inventory_policy)
            self._validate_inventory_binding(*run_evidence.inventory_binding())
            run_evidence.access_policy().verify_root_identity()

            snapshot_markers = self._connection.execute(
                """SELECT event_id FROM run_events WHERE run_id=?
                AND phase='routing-snapshot'
                AND message='Snapshot de rutas publicado'
                ORDER BY event_id LIMIT 2""",
                (run_id,),
            ).fetchall()
            route_rows = self._connection.execute(
                """SELECT route_name,status,summary_json,error_type
                FROM route_runs WHERE run_id=? ORDER BY route_name LIMIT 2""",
                (run_id,),
            ).fetchall()
            code_evidence = CompletedCodeRoute.decode(
                snapshot_markers,
                route_rows,
                code_processing_signature,
            )

            safety_counts = SelfAnalysisSafetyCounts(
                route_candidates=int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM route_candidates WHERE run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                ),
                file_actions=int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM file_actions WHERE run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                ),
                run_actions=int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM run_actions WHERE run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                ),
                organization_events=int(
                    self._connection.execute(
                        """SELECT COUNT(*) FROM run_events WHERE run_id=?
                        AND phase IN ('document-organization-plan',
                        'document-organization-apply')""",
                        (run_id,),
                    ).fetchone()[0]
                ),
            )
            safety_counts.validate()
            existing_manifest = self._connection.execute(
                """SELECT event_id FROM run_events WHERE run_id=? AND phase=?
                AND message=? ORDER BY event_id LIMIT 1""",
                (
                    run_id,
                    SELF_ANALYSIS_MANIFEST_PHASE,
                    SELF_ANALYSIS_MANIFEST_MESSAGE,
                ),
            ).fetchone()
            if existing_manifest is not None:
                raise ValueError("self-analysis manifest already exists")

            evidence = SelfAnalysisCompletionEvidence(
                run_evidence,
                cursor,
                code_evidence,
                safety_counts,
            )
            manifest, manifest_json = evidence.build_manifest(
                inventory_policy=inventory_policy,
                commands=commands,
            )
            now = time.time_ns()
            updated = self._connection.execute(
                """UPDATE initial_runs SET completed_ns=?,status='completed',
                current_phase='completed',heartbeat_ns=?,end_usn=?
                WHERE run_id=? AND status='running'
                AND run_kind='self_analysis' AND corpus_access_mode='analyze_only'
                AND scan_id=?""",
                (
                    now,
                    now,
                    None if cursor is None else cursor.next_usn,
                    run_id,
                    run_evidence.scan_id,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("self-analysis completion lost its owner row")
            self._connection.execute(
                """INSERT INTO run_events(
                run_id,occurred_ns,level,phase,message,details_json)
                VALUES(?,?,'info',?,?,?)""",
                (
                    run_id,
                    now,
                    SELF_ANALYSIS_MANIFEST_PHASE,
                    SELF_ANALYSIS_MANIFEST_MESSAGE,
                    manifest_json,
                ),
            )
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        return manifest

    def complete_initial_run(
        self,
        run_id: int,
        scan_id: int,
        cursor: JournalCursor | None,
        reconciliation_records: int,
        inventory_attempts: int,
        inventory_mode: str,
    ) -> None:
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
                raise RuntimeError(f"run {run_id} cannot complete without its published snapshot")

    def complete_operational_run(self, run_id: int) -> None:
        with self._connection:
            result = self._connection.execute(
                """UPDATE initial_runs SET completed_ns=?,status='completed',
                current_phase='completed',heartbeat_ns=?
                WHERE run_id=? AND status='running'
                AND run_kind IN ('route_only','resume')""",
                (time.time_ns(), time.time_ns(), run_id),
            )
            if result.rowcount != 1:
                raise RuntimeError(f"run {run_id} is not a running operational execution")

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
