"""Concurrent route view and review-evidence repository."""
# region [00] Contexto del módulo
# Módulo: neocortex/framework_route_state.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations
import json
import sqlite3
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from neocortex.deduplication import FileSnapshot

from neocortex.safety.corpus_access import CorpusMutationGuard
from neocortex.persistence.framework_state_common import (
    FileActionSpec,
    begin_file_actions,
    confirm_file_actions_applied,
    corpus_mutation_guard,
    finish_file_actions,
    mark_file_actions_applying,
)
from neocortex.workflow.review.review import (
    MAX_RECONCILIATION_REASONS,
    ReviewCandidate,
    ReviewDecision,
    serialized_evidence,
    serialized_provenance,
    validated_reason_codes,
)
from neocortex.workflow.review.review_evidence import _materialize_review_decision
from neocortex.safety.route_filters import CandidateSelection, framework_selection_predicate
from neocortex.persistence.sqlite_paths import existing_sqlite_uri, readonly_sqlite_uri
# endregion [01]

# region [02] Implementación


REVIEW_RECONCILIATION_BATCH_SIZE = 256


def _bounded_review_reason_codes(reason_codes: object) -> tuple[str, ...]:
    """Validate a reason iterable without materializing an unbounded source."""

    if isinstance(reason_codes, (str, bytes)):
        return validated_reason_codes(reason_codes)
    try:
        iterator = iter(reason_codes)  # type: ignore[call-overload]
    except TypeError as exc:
        raise TypeError("review reason codes must be an iterable of strings") from exc
    bounded: list[object] = []
    for value in iterator:
        if len(bounded) >= MAX_RECONCILIATION_REASONS:
            raise ValueError(
                f"review reconciliation exceeds {MAX_RECONCILIATION_REASONS} reason codes"
            )
        bounded.append(value)
    return validated_reason_codes(bounded)


@dataclass(frozen=True, slots=True)
class ReviewCandidateReconciliation:
    """One bounded detector-generation reconciliation request."""

    snapshot: FileSnapshot
    resolution_note: str
    evaluated_reason_codes: Iterable[str]
    active_reason_codes: Iterable[str] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, FileSnapshot):
            raise TypeError("review reconciliation snapshot must be a FileSnapshot")
        note = self.resolution_note
        if not note or note.strip() != note:
            raise ValueError("review resolution_note must be non-empty and trimmed")
        if len(note.encode("utf-8")) > 8 * 1024:
            raise ValueError("review resolution_note exceeds the 8192-byte limit")
        evaluated = _bounded_review_reason_codes(self.evaluated_reason_codes)
        active = _bounded_review_reason_codes(self.active_reason_codes)
        active_set = frozenset(active)
        evaluated_set = frozenset(evaluated)
        if not active_set <= evaluated_set:
            unexpected = ", ".join(sorted(active_set - evaluated_set))
            raise ValueError("active review reasons were not evaluated: " + unexpected)
        object.__setattr__(self, "evaluated_reason_codes", evaluated)
        object.__setattr__(self, "active_reason_codes", active)


class FrameworkRouteState:
    """Open short independent connections for concurrent route operations."""

    CANDIDATE_BATCH_SIZE = 1000

    def __init__(self, database: str | Path):
        self.path = Path(database)

    def _connect(self, *, readonly: bool) -> sqlite3.Connection:
        if readonly:
            connection = sqlite3.connect(
                readonly_sqlite_uri(self.path),
                uri=True,
                timeout=60,
            )
        else:
            connection = sqlite3.connect(
                existing_sqlite_uri(self.path),
                uri=True,
                timeout=60,
            )
        try:
            connection.execute("PRAGMA busy_timeout=60000")
            connection.execute("PRAGMA foreign_keys=ON")
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                raise RuntimeError("framework route state could not enable foreign keys")
            if readonly:
                connection.execute("PRAGMA query_only=ON")
                if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
                    raise RuntimeError("framework route state could not enforce query-only mode")
        except BaseException:
            connection.close()
            raise
        return connection

    def iter_route_candidates(self, run_id: int, mime: str):
        last_path = ""
        while True:
            connection = self._connect(readonly=True)
            try:
                rows = connection.execute(
                    """SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns
                    FROM route_candidates WHERE run_id=? AND mime=? AND path>?
                    ORDER BY path LIMIT ?""",
                    (run_id, mime, last_path, self.CANDIDATE_BATCH_SIZE),
                ).fetchall()
            finally:
                connection.close()
            if not rows:
                return
            for path, volume_id, file_id, size, mtime_ns, birthtime_ns in rows:
                yield FileSnapshot(
                    path,
                    int(volume_id, 16),
                    int(file_id, 16),
                    int(size),
                    int(mtime_ns),
                    int(birthtime_ns),
                )
            last_path = str(rows[-1][0])

    def iter_selected_route_candidates(
        self,
        run_id: int,
        mime: str,
        route_name: str,
        selection: CandidateSelection,
    ):
        """Stream path/review-filtered candidates without materializing an allow-list."""

        predicate, predicate_parameters = framework_selection_predicate(
            selection,
            route_name=route_name,
            candidate_alias="c",
        )
        last_path = ""
        while True:
            connection = self._connect(readonly=True)
            try:
                rows = connection.execute(
                    f"""SELECT c.path,c.volume_id,c.file_id,c.size,c.mtime_ns,
                    c.birthtime_ns FROM route_candidates c
                    WHERE c.run_id=? AND c.mime=? AND c.path>? AND {predicate}
                    ORDER BY c.path LIMIT ?""",
                    (
                        run_id,
                        mime,
                        last_path,
                        *predicate_parameters,
                        self.CANDIDATE_BATCH_SIZE,
                    ),
                ).fetchall()
            finally:
                connection.close()
            if not rows:
                return
            for path, volume_id, file_id, size, mtime_ns, birthtime_ns in rows:
                yield FileSnapshot(
                    path,
                    int(volume_id, 16),
                    int(file_id, 16),
                    int(size),
                    int(mtime_ns),
                    int(birthtime_ns),
                )
            last_path = str(rows[-1][0])

    def iter_route_candidates_by_prefix(self, run_id: int, mime_prefix: str):
        last_path = ""
        while True:
            connection = self._connect(readonly=True)
            try:
                rows = connection.execute(
                    """SELECT mime,path,volume_id,file_id,size,mtime_ns,birthtime_ns
                    FROM route_candidates WHERE run_id=? AND mime LIKE ? AND path>?
                    ORDER BY path LIMIT ?""",
                    (
                        run_id,
                        f"{mime_prefix}%",
                        last_path,
                        self.CANDIDATE_BATCH_SIZE,
                    ),
                ).fetchall()
            finally:
                connection.close()
            if not rows:
                return
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
            last_path = str(rows[-1][1])

    def iter_selected_route_candidates_by_prefix(
        self,
        run_id: int,
        mime_prefix: str,
        route_name: str,
        selection: CandidateSelection,
    ):
        predicate, predicate_parameters = framework_selection_predicate(
            selection,
            route_name=route_name,
            candidate_alias="c",
        )
        last_path = ""
        while True:
            connection = self._connect(readonly=True)
            try:
                rows = connection.execute(
                    f"""SELECT c.mime,c.path,c.volume_id,c.file_id,c.size,
                    c.mtime_ns,c.birthtime_ns FROM route_candidates c
                    WHERE c.run_id=? AND c.mime LIKE ? AND c.path>? AND {predicate}
                    ORDER BY c.path LIMIT ?""",
                    (
                        run_id,
                        f"{mime_prefix}%",
                        last_path,
                        *predicate_parameters,
                        self.CANDIDATE_BATCH_SIZE,
                    ),
                ).fetchall()
            finally:
                connection.close()
            if not rows:
                return
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
            last_path = str(rows[-1][1])

    def selected_route_candidate_counts(
        self,
        run_id: int,
        mime: str,
        max_file_bytes: int | None,
        route_name: str,
        selection: CandidateSelection,
    ) -> tuple[int, int]:
        predicate, predicate_parameters = framework_selection_predicate(
            selection,
            route_name=route_name,
            candidate_alias="c",
        )
        connection = self._connect(readonly=True)
        try:
            total = int(
                connection.execute(
                    f"""SELECT COUNT(*) FROM route_candidates c
                    WHERE c.run_id=? AND c.mime=? AND {predicate}""",
                    (run_id, mime, *predicate_parameters),
                ).fetchone()[0]
            )
            if max_file_bytes is None:
                return total, total
            eligible = int(
                connection.execute(
                    f"""SELECT COUNT(*) FROM route_candidates c
                    WHERE c.run_id=? AND c.mime=? AND c.size<=? AND {predicate}""",
                    (
                        run_id,
                        mime,
                        max_file_bytes,
                        *predicate_parameters,
                    ),
                ).fetchone()[0]
            )
            return total, eligible
        finally:
            connection.close()

    def completed_route_phases(
        self,
        run_id: int,
        route_name: str,
    ) -> frozenset[str]:
        connection = self._connect(readonly=True)
        try:
            rows = connection.execute(
                """SELECT phase_name FROM route_phase_runs
                WHERE run_id=? AND route_name=? AND status='completed'""",
                (run_id, route_name),
            ).fetchall()
            return frozenset(str(row[0]) for row in rows)
        finally:
            connection.close()

    def begin_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        *,
        source_run_id: int | None = None,
    ) -> None:
        now = time.time_ns()
        connection = self._connect(readonly=False)
        try:
            with connection:
                connection.execute(
                    """INSERT INTO route_phase_runs(
                    run_id,route_name,phase_name,status,started_ns,heartbeat_ns,
                    source_run_id)
                    VALUES(?,?,?,'running',?,?,?)
                    ON CONFLICT(run_id,route_name,phase_name) DO UPDATE SET
                    status='running',started_ns=excluded.started_ns,
                    completed_ns=NULL,heartbeat_ns=excluded.heartbeat_ns,
                    source_run_id=excluded.source_run_id,summary_json=NULL,
                    error_type=NULL,error_message=NULL""",
                    (
                        run_id,
                        route_name,
                        phase_name,
                        now,
                        now,
                        source_run_id,
                    ),
                )
                connection.execute(
                    """UPDATE route_runs SET current_phase=?,heartbeat_ns=?
                    WHERE run_id=? AND route_name=? AND status='running'""",
                    (phase_name, now, run_id, route_name),
                )
        finally:
            connection.close()

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
        connection = self._connect(readonly=False)
        try:
            with connection:
                connection.execute(
                    """UPDATE route_phase_runs SET status='completed',
                    completed_ns=?,heartbeat_ns=?,summary_json=?,
                    error_type=NULL,error_message=NULL
                    WHERE run_id=? AND route_name=? AND phase_name=?""",
                    (now, now, payload, run_id, route_name, phase_name),
                )
        finally:
            connection.close()

    def fail_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        exc: BaseException,
    ) -> None:
        now = time.time_ns()
        connection = self._connect(readonly=False)
        try:
            with connection:
                connection.execute(
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
        finally:
            connection.close()

    def record_event(
        self,
        run_id: int,
        level: str,
        phase: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if level not in {"debug", "info", "warning", "error"}:
            raise ValueError(f"invalid event level: {level}")
        payload = (
            None
            if details is None
            else json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        )
        connection = self._connect(readonly=False)
        try:
            with connection:
                connection.execute(
                    """INSERT INTO run_events(
                    run_id,occurred_ns,level,phase,message,details_json)
                    VALUES(?,?,?,?,?,?)""",
                    (run_id, time.time_ns(), level, phase, message, payload),
                )
        finally:
            connection.close()

    def corpus_mutation_guard(self, run_id: int) -> CorpusMutationGuard:
        """Return one durable run guard without retaining the read connection."""

        connection = self._connect(readonly=True)
        try:
            return corpus_mutation_guard(connection, run_id)
        finally:
            connection.close()

    def begin_file_actions(
        self,
        run_id: int,
        actions: Iterable[FileActionSpec],
    ) -> list[int]:
        """Record a route action batch without sharing a SQLite connection."""

        connection = self._connect(readonly=False)
        try:
            return begin_file_actions(connection, run_id, actions)
        finally:
            connection.close()

    def finish_file_actions(
        self,
        action_ids: Iterable[int],
        status: str,
        detail: str | None = None,
    ) -> None:
        """Complete a route action batch without sharing a SQLite connection."""

        connection = self._connect(readonly=False)
        try:
            finish_file_actions(connection, action_ids, status, detail)
        finally:
            connection.close()

    def mark_file_actions_applying(
        self,
        actions: Iterable[tuple[int, str]],
    ) -> None:
        """Persist expected identities before a route-owned filesystem syscall."""

        connection = self._connect(readonly=False)
        try:
            mark_file_actions_applying(connection, actions)
        finally:
            connection.close()

    def confirm_file_actions_applied(
        self,
        actions: Iterable[tuple[int, str]],
    ) -> None:
        """Store route-owned syscall receipts through an applying-state CAS."""

        connection = self._connect(readonly=False)
        try:
            confirm_file_actions_applied(connection, actions)
        finally:
            connection.close()

    def require_file_action_recovery(
        self,
        action_ids: Iterable[int],
        detail: str,
    ) -> None:
        """Record uncertain route-owned effects without repeating mutations."""

        connection = self._connect(readonly=False)
        try:
            finish_file_actions(
                connection,
                action_ids,
                "recovery_required",
                detail,
            )
        finally:
            connection.close()

    def store_review_candidates(
        self,
        run_id: int,
        candidates: Iterable[ReviewCandidate],
    ) -> None:
        """Upsert a bounded evidence batch without authorizing file actions."""

        detected_ns = time.time_ns()
        connection = self._connect(readonly=False)
        try:
            with connection:
                connection.executemany(
                    """INSERT INTO review_candidates(
                    route_name,volume_id,file_id,reason_code,path,size,mtime_ns,
                    birthtime_ns,source_status,recommendation,retryable,confidence,
                    evidence_json,detector_version,status,first_detected_ns,
                    last_detected_ns,last_seen_run_id,resolved_ns,resolved_run_id,
                    resolution_note)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?,NULL,NULL,NULL)
                    ON CONFLICT(route_name,volume_id,file_id,reason_code) DO UPDATE SET
                    path=excluded.path,size=excluded.size,mtime_ns=excluded.mtime_ns,
                    birthtime_ns=excluded.birthtime_ns,
                    source_status=excluded.source_status,
                    recommendation=excluded.recommendation,
                    retryable=excluded.retryable,confidence=excluded.confidence,
                    evidence_json=excluded.evidence_json,
                    detector_version=excluded.detector_version,status='open',
                    last_detected_ns=excluded.last_detected_ns,
                    last_seen_run_id=excluded.last_seen_run_id,
                    resolved_ns=NULL,resolved_run_id=NULL,resolution_note=NULL""",
                    (
                        (
                            candidate.route_name,
                            f"{candidate.snapshot.volume_id:x}",
                            f"{candidate.snapshot.file_id:x}",
                            candidate.reason_code,
                            candidate.snapshot.path,
                            candidate.snapshot.size,
                            candidate.snapshot.mtime_ns,
                            candidate.snapshot.birthtime_ns,
                            candidate.source_status,
                            candidate.recommendation,
                            int(candidate.retryable),
                            candidate.confidence,
                            serialized_evidence(candidate.evidence),
                            candidate.detector_version,
                            detected_ns,
                            detected_ns,
                            run_id,
                        )
                        for candidate in candidates
                    ),
                )
        finally:
            connection.close()

    def resolve_review_candidates(
        self,
        run_id: int,
        route_name: str,
        snapshot: FileSnapshot,
        resolution_note: str,
    ) -> int:
        """Resolve old findings for one identity, guarded by generation."""

        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 0:
            raise ValueError("review generation must be a non-negative integer")
        if not resolution_note or resolution_note.strip() != resolution_note:
            raise ValueError("review resolution_note must be non-empty and trimmed")
        if len(resolution_note.encode("utf-8")) > 8 * 1024:
            raise ValueError("review resolution_note exceeds the 8192-byte limit")

        resolved_ns = time.time_ns()
        connection = self._connect(readonly=False)
        try:
            with connection:
                cursor = connection.execute(
                    """UPDATE review_candidates SET status='resolved',resolved_ns=?,
                    resolution_note=?,resolved_run_id=?,path=?,size=?,mtime_ns=?,
                    birthtime_ns=? WHERE route_name=? AND volume_id=? AND file_id=?
                    AND status='open' AND last_seen_run_id<?""",
                    (
                        resolved_ns,
                        resolution_note,
                        run_id,
                        snapshot.path,
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.birthtime_ns,
                        route_name,
                        f"{snapshot.volume_id:x}",
                        f"{snapshot.file_id:x}",
                        run_id,
                    ),
                )
                return int(cursor.rowcount)
        finally:
            connection.close()

    def reconcile_review_candidates(
        self,
        run_id: int,
        route_name: str,
        snapshot: FileSnapshot,
        resolution_note: str,
        *,
        evaluated_reason_codes: Iterable[str],
        active_reason_codes: Iterable[str],
    ) -> int:
        """Resolve only evaluated reasons absent from a newer generation."""

        reconciliation = ReviewCandidateReconciliation(
            snapshot=snapshot,
            resolution_note=resolution_note,
            evaluated_reason_codes=evaluated_reason_codes,
            active_reason_codes=active_reason_codes,
        )
        return self.reconcile_review_candidates_batch(
            run_id,
            route_name,
            (reconciliation,),
        )

    def resolve_review_candidate_generation(
        self,
        candidate_generation: int,
        route_name: str,
        snapshot: FileSnapshot,
        reason_code: str,
        resolution_note: str,
    ) -> int:
        """Resolve exactly one reason from exactly one observed generation."""

        if (
            isinstance(candidate_generation, bool)
            or not isinstance(candidate_generation, int)
            or candidate_generation < 0
        ):
            raise ValueError("review generation must be a non-negative integer")
        if not route_name or route_name.strip() != route_name:
            raise ValueError("review route_name must be non-empty and trimmed")
        reconciliation = ReviewCandidateReconciliation(
            snapshot=snapshot,
            resolution_note=resolution_note,
            evaluated_reason_codes=(reason_code,),
        )
        validated_reason = next(iter(reconciliation.evaluated_reason_codes))
        resolved_ns = time.time_ns()
        connection = self._connect(readonly=False)
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """UPDATE review_candidates SET status='resolved',resolved_ns=?,
                    resolution_note=?,resolved_run_id=?,path=?,size=?,mtime_ns=?,
                    birthtime_ns=? WHERE route_name=? AND volume_id=? AND file_id=?
                    AND reason_code=? AND status='open' AND last_seen_run_id=?""",
                    (
                        resolved_ns,
                        reconciliation.resolution_note,
                        candidate_generation,
                        snapshot.path,
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.birthtime_ns,
                        route_name,
                        f"{snapshot.volume_id:x}",
                        f"{snapshot.file_id:x}",
                        validated_reason,
                        candidate_generation,
                    ),
                )
                return int(cursor.rowcount)
        finally:
            connection.close()

    def reconcile_review_candidates_batch(
        self,
        run_id: int,
        route_name: str,
        reconciliations: Iterable[ReviewCandidateReconciliation],
    ) -> int:
        """Reconcile at most 256 identities in one short transaction."""

        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 0:
            raise ValueError("review generation must be a non-negative integer")
        if not route_name or route_name.strip() != route_name:
            raise ValueError("review route_name must be non-empty and trimmed")

        batch: list[ReviewCandidateReconciliation] = []
        for reconciliation in reconciliations:
            if not isinstance(reconciliation, ReviewCandidateReconciliation):
                raise TypeError(
                    "review reconciliation batch must contain ReviewCandidateReconciliation values"
                )
            if len(batch) >= REVIEW_RECONCILIATION_BATCH_SIZE:
                raise ValueError(
                    "review reconciliation batch exceeds "
                    f"{REVIEW_RECONCILIATION_BATCH_SIZE} identities"
                )
            batch.append(reconciliation)
        if not batch:
            return 0

        resolved_ns = time.time_ns()
        resolved = 0
        connection = self._connect(readonly=False)
        try:
            with connection:
                for reconciliation in batch:
                    active = frozenset(reconciliation.active_reason_codes)
                    stale = tuple(
                        reason
                        for reason in reconciliation.evaluated_reason_codes
                        if reason not in active
                    )
                    if not stale:
                        continue
                    placeholders = ",".join("?" for _ in stale)
                    snapshot = reconciliation.snapshot
                    cursor = connection.execute(
                        f"""UPDATE review_candidates SET status='resolved',resolved_ns=?,
                        resolution_note=?,resolved_run_id=?,path=?,size=?,mtime_ns=?,
                        birthtime_ns=? WHERE route_name=? AND volume_id=? AND file_id=?
                        AND status='open' AND last_seen_run_id<?
                        AND reason_code IN ({placeholders})""",
                        (
                            resolved_ns,
                            reconciliation.resolution_note,
                            run_id,
                            snapshot.path,
                            snapshot.size,
                            snapshot.mtime_ns,
                            snapshot.birthtime_ns,
                            route_name,
                            f"{snapshot.volume_id:x}",
                            f"{snapshot.file_id:x}",
                            run_id,
                            *stale,
                        ),
                    )
                    resolved += int(cursor.rowcount)
            return resolved
        finally:
            connection.close()

    def record_review_decision(self, decision: ReviewDecision) -> int:
        """Persist one human judgment with strong idempotency semantics."""

        provenance_json = serialized_provenance(decision.provenance)
        evidence_json = serialized_evidence(decision.evidence)
        identity = (
            decision.route_name,
            f"{decision.snapshot.volume_id:x}",
            f"{decision.snapshot.file_id:x}",
            decision.reason_code,
            decision.candidate_generation,
            decision.snapshot.path,
            decision.snapshot.size,
            decision.snapshot.mtime_ns,
            decision.snapshot.birthtime_ns,
            decision.status,
            decision.actor,
            provenance_json,
            decision.note,
            decision.decided_ns,
        )
        expected_snapshot = (
            decision.source_status,
            decision.recommendation,
            decision.retryable,
            decision.confidence,
            evidence_json,
            decision.detector_version,
        )
        connection = self._connect(readonly=False)
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """SELECT decision_id,route_name,volume_id,file_id,reason_code,
                    candidate_generation,path,size,mtime_ns,birthtime_ns,status,actor,
                    provenance_json,note,decided_ns,source_status,recommendation,
                    retryable,confidence,evidence_json,detector_version
                    FROM review_decisions WHERE idempotency_key=?""",
                    (decision.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if tuple(existing[1:15]) != identity:
                        raise ValueError(
                            "review idempotency_key already identifies a different decision"
                        )
                    stored_snapshot_values = tuple(existing[15:])
                    if all(value is None for value in stored_snapshot_values):
                        _materialize_review_decision(connection, int(existing[0]))
                        return int(existing[0])
                    if any(value is None for value in stored_snapshot_values):
                        raise sqlite3.DatabaseError(
                            "review decision candidate snapshot is incomplete"
                        )
                    stored_retryable = int(stored_snapshot_values[2])
                    if stored_retryable not in (0, 1):
                        raise sqlite3.DatabaseError("review decision candidate snapshot is invalid")
                    stored_snapshot = (
                        str(stored_snapshot_values[0]),
                        str(stored_snapshot_values[1]),
                        bool(stored_retryable),
                        float(stored_snapshot_values[3]),
                        str(stored_snapshot_values[4]),
                        str(stored_snapshot_values[5]),
                    )
                    if stored_snapshot != expected_snapshot:
                        raise ValueError(
                            "review idempotency_key already identifies a different "
                            "decision candidate snapshot"
                        )
                    _materialize_review_decision(connection, int(existing[0]))
                    return int(existing[0])

                candidate = connection.execute(
                    """SELECT path,size,mtime_ns,birthtime_ns,last_seen_run_id,status,
                    source_status,recommendation,retryable,confidence,evidence_json,
                    detector_version FROM review_candidates WHERE route_name=?
                    AND volume_id=? AND file_id=? AND reason_code=?""",
                    identity[:4],
                ).fetchone()
                expected_candidate = (
                    decision.snapshot.path,
                    decision.snapshot.size,
                    decision.snapshot.mtime_ns,
                    decision.snapshot.birthtime_ns,
                    decision.candidate_generation,
                )
                if candidate is None:
                    raise ValueError("review decision does not identify a finding")
                if tuple(candidate[:5]) != expected_candidate:
                    raise ValueError("review decision is stale; refresh the finding generation")
                if str(candidate[5]) != "open":
                    raise ValueError("review decision finding is no longer open; refresh it")
                retryable = int(candidate[8])
                if retryable not in (0, 1):
                    raise ValueError("review decision finding has invalid retryable state")
                try:
                    candidate_evidence = json.loads(str(candidate[10]))
                except (TypeError, ValueError) as exc:
                    raise ValueError("review decision finding has invalid evidence") from exc
                if not isinstance(candidate_evidence, dict):
                    raise ValueError("review decision finding evidence must be a JSON object")
                candidate_snapshot = (
                    str(candidate[6]),
                    str(candidate[7]),
                    bool(retryable),
                    float(candidate[9]),
                    serialized_evidence(candidate_evidence),
                    str(candidate[11]),
                )
                if candidate_snapshot != expected_snapshot:
                    raise ValueError(
                        "review decision candidate snapshot changed; refresh the finding"
                    )

                recorded_ns = time.time_ns()
                cursor = connection.execute(
                    """INSERT INTO review_decisions(
                    idempotency_key,route_name,volume_id,file_id,reason_code,
                    candidate_generation,path,size,mtime_ns,birthtime_ns,
                    source_status,recommendation,retryable,confidence,evidence_json,
                    detector_version,status,actor,provenance_json,note,decided_ns,
                    recorded_ns)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        decision.idempotency_key,
                        *identity[:9],
                        *expected_snapshot,
                        *identity[9:],
                        recorded_ns,
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("SQLite did not return a review-decision identifier")
                decision_id = int(cursor.lastrowid)
                _materialize_review_decision(
                    connection,
                    decision_id,
                    materialized_ns=recorded_ns,
                )
                return decision_id
        finally:
            connection.close()


# endregion [02]
