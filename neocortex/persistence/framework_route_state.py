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
from neocortex.workflow.findings import (
    MAX_RECONCILIATION_REASONS,
    ReviewCandidate,
    serialized_evidence,
    validated_reason_codes,
)
from neocortex.safety.route_filters import CandidateSelection, framework_selection_predicate
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    open_immutable_sqlite_connection,
    open_sidecar_safe_sqlite_connection,
)
from neocortex.persistence.sqlite_paths import existing_sqlite_uri
# endregion [01]

# region [02] Implementación


REVIEW_RECONCILIATION_BATCH_SIZE = 256


def store_findings_compat(
    framework_state: Any,
    run_id: int,
    candidates: Iterable[ReviewCandidate],
) -> None:
    """Persist findings through the current API or a legacy route double.

    The route state API was renamed from ``*_review_candidates`` to the more
    precise ``*_findings`` names.  A few deliberately minimal route fixtures
    (and downstream integrations) still expose the old spelling, so keep the
    compatibility boundary in one place rather than making every route know
    about both contracts.
    """

    store = getattr(framework_state, "store_findings", None)
    if store is None:
        store = getattr(framework_state, "store_review_candidates", None)
    if store is None:
        raise AttributeError("framework route state does not expose findings storage")
    store(run_id, candidates)


def reconcile_findings_batch_compat(
    framework_state: Any,
    run_id: int,
    route_name: str,
    reconciliations: Iterable["ReviewCandidateReconciliation"],
) -> int:
    """Reconcile findings using the current API or legacy route doubles."""

    batch = tuple(reconciliations)
    reconcile = getattr(framework_state, "reconcile_findings_batch", None)
    if reconcile is not None:
        return int(reconcile(run_id, route_name, batch))

    reconcile = getattr(framework_state, "reconcile_review_candidates_batch", None)
    if reconcile is not None:
        result = reconcile(run_id, route_name, batch)
        return 0 if result is None else int(result)

    reconcile_one = getattr(framework_state, "reconcile_review_candidates", None)
    if reconcile_one is None:
        raise AttributeError("framework route state does not expose findings reconciliation")
    resolved = 0
    for item in batch:
        result = reconcile_one(
            run_id,
            route_name,
            item.snapshot,
            item.resolution_note,
            evaluated_reason_codes=item.evaluated_reason_codes,
            active_reason_codes=item.active_reason_codes,
        )
        if result is not None:
            resolved += int(result)
    return resolved


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

    def __init__(
        self,
        database: str | Path,
        *,
        candidate_database: Path | None = None,
        resume_source_run_id: int | None = None,
    ):
        if resume_source_run_id is not None and (
            type(resume_source_run_id) is not int or resume_source_run_id <= 0
        ):
            raise ValueError("resume source run must be a positive integer")
        self.path = Path(database)
        # Candidate inputs and explicitly bound terminal resume evidence may
        # use the published view. Current lifecycle, authorizations and effects
        # retain the original live owner.
        self.candidate_database = candidate_database
        self.resume_source_run_id = resume_source_run_id

    def _connect_candidates(self) -> sqlite3.Connection:
        if self.candidate_database is None:
            return self._connect(readonly=True)
        return open_immutable_sqlite_connection(self.candidate_database)

    def _connect(self, *, readonly: bool) -> sqlite3.Connection:
        if readonly:
            return open_sidecar_safe_sqlite_connection(
                self.path,
                timeout_seconds=60.0,
                max_attempts=8,
            )
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
        except BaseException:
            connection.close()
            raise
        return connection

    def iter_route_candidates(self, run_id: int, mime: str):
        last_path = ""
        while True:
            connection = self._connect_candidates()
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
            connection = self._connect_candidates()
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
            connection = self._connect_candidates()
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
            connection = self._connect_candidates()
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
        connection = self._connect_candidates()
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
        historical_input = (
            self.candidate_database is not None and run_id == self.resume_source_run_id
        )
        connection = (
            self._connect_candidates() if historical_input else self._connect(readonly=True)
        )
        try:
            if historical_input:
                source = connection.execute(
                    "SELECT status FROM initial_runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if source is None or str(source[0]) not in {
                    "completed",
                    "failed",
                    "cancelled",
                    "interrupted",
                }:
                    raise ImmutableSQLiteUnavailable(
                        "resume phase snapshot requires an explicitly bound terminal source run"
                    )
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

    def store_findings(
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
                    """INSERT INTO findings(
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

    def resolve_findings(
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
                    """UPDATE findings SET status='resolved',resolved_ns=?,
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

    def reconcile_findings(
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
        return self.reconcile_findings_batch(
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
                    """UPDATE findings SET status='resolved',resolved_ns=?,
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

    def reconcile_findings_batch(
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
                        f"""UPDATE findings SET status='resolved',resolved_ns=?,
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

# endregion [02]
