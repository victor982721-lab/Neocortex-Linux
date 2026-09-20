"""Durable review findings and append-only human feedback."""

from __future__ import annotations

# region [01] Models and validation

import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping

from neocortex.deduplication import FileSnapshot
from neocortex.persistence.framework_connection import connect_existing_framework
from neocortex.workflow.findings import (
    FINDING_RECOMMENDATIONS,
    ReviewRecommendation,
    serialized_evidence,
)

ReviewStatus = Literal["open", "resolved"]
ReviewDecisionStatus = Literal["confirmed", "dismissed", "deferred"]

REVIEW_RECOMMENDATIONS = FINDING_RECOMMENDATIONS
REVIEW_STATUSES = frozenset({"open", "resolved"})
REVIEW_DECISION_STATUSES = frozenset({"confirmed", "dismissed", "deferred"})
MAX_EVIDENCE_BYTES = 32 * 1024
MAX_PROVENANCE_BYTES = 32 * 1024
MAX_NOTE_BYTES = 8 * 1024
MAX_IDENTIFIER_CHARS = 256
MAX_RECONCILIATION_REASONS = 256


@dataclass(frozen=True, slots=True)
class ReviewCandidateRecord:
    route_name: str
    path: str
    volume_id: int
    file_id: int
    size: int
    mtime_ns: int
    birthtime_ns: int
    reason_code: str
    source_status: str
    recommendation: ReviewRecommendation
    retryable: bool
    confidence: float
    evidence: Mapping[str, object]
    detector_version: str
    status: ReviewStatus
    first_detected_ns: int
    last_detected_ns: int
    last_detected_generation: int
    resolved_ns: int | None
    resolved_generation: int | None
    resolution_note: str | None


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """One idempotent, append-only human judgment about a finding generation."""

    idempotency_key: str
    route_name: str
    snapshot: FileSnapshot
    reason_code: str
    candidate_generation: int
    source_status: str
    recommendation: ReviewRecommendation
    retryable: bool
    confidence: float
    evidence: Mapping[str, object]
    detector_version: str
    status: ReviewDecisionStatus
    actor: str
    provenance: Mapping[str, object]
    note: str | None = None
    decided_ns: int = field(default_factory=time.time_ns)

    def __post_init__(self) -> None:
        for field_name, value in (
            ("idempotency_key", self.idempotency_key),
            ("route_name", self.route_name),
            ("reason_code", self.reason_code),
            ("source_status", self.source_status),
            ("detector_version", self.detector_version),
            ("actor", self.actor),
        ):
            _validated_identifier(value, field_name=field_name)
        if isinstance(self.candidate_generation, bool) or self.candidate_generation < 0:
            raise ValueError("review candidate_generation must be a non-negative integer")
        if self.status not in REVIEW_DECISION_STATUSES:
            raise ValueError(f"invalid review decision status: {self.status}")
        if self.recommendation not in REVIEW_RECOMMENDATIONS:
            raise ValueError(f"invalid review recommendation: {self.recommendation}")
        if not isinstance(self.retryable, bool):
            raise TypeError("review retryable must be a boolean")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("review confidence must be finite and between zero and one")
        if isinstance(self.decided_ns, bool) or self.decided_ns <= 0:
            raise ValueError("review decided_ns must be a positive integer")
        if self.note is not None:
            if not self.note or self.note.strip() != self.note:
                raise ValueError("review note must be non-empty and trimmed when present")
            if len(self.note.encode("utf-8")) > MAX_NOTE_BYTES:
                raise ValueError(f"review note exceeds the {MAX_NOTE_BYTES}-byte limit")
        if not self.provenance:
            raise ValueError("review provenance must not be empty")
        serialized_evidence(self.evidence)
        serialized_provenance(self.provenance)


@dataclass(frozen=True, slots=True)
class ReviewDecisionRecord:
    decision_id: int
    idempotency_key: str
    route_name: str
    path: str
    volume_id: int
    file_id: int
    size: int
    mtime_ns: int
    birthtime_ns: int
    reason_code: str
    candidate_generation: int
    source_status: str | None
    recommendation: ReviewRecommendation | None
    retryable: bool | None
    confidence: float | None
    evidence: Mapping[str, object] | None
    detector_version: str | None
    status: ReviewDecisionStatus
    actor: str
    provenance: Mapping[str, object]
    note: str | None
    decided_ns: int
    recorded_ns: int


def _validated_identifier(value: str, *, field_name: str) -> str:
    if not value or value.strip() != value:
        raise ValueError(f"review {field_name} must be non-empty and trimmed")
    if len(value) > MAX_IDENTIFIER_CHARS:
        raise ValueError(f"review {field_name} exceeds {MAX_IDENTIFIER_CHARS} characters")
    return value


def validated_reason_codes(reason_codes: object) -> tuple[str, ...]:
    """Return a stable, bounded reason set suitable for a SQL predicate."""

    if isinstance(reason_codes, (str, bytes)):
        raise TypeError("review reason codes must be an iterable of strings")
    try:
        values: tuple[object, ...] = tuple(reason_codes)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError("review reason codes must be an iterable of strings") from exc
    if len(values) > MAX_RECONCILIATION_REASONS:
        raise ValueError(f"review reconciliation exceeds {MAX_RECONCILIATION_REASONS} reason codes")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise TypeError("review reason codes must contain only strings")
        normalized.add(_validated_identifier(value, field_name="reason_code"))
    return tuple(sorted(normalized))


def _serialized_mapping(
    value: Mapping[str, object],
    *,
    field_name: str,
    maximum_bytes: int,
) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(payload.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"review {field_name} exceeds the {maximum_bytes}-byte limit")
    return payload


def serialized_provenance(provenance: Mapping[str, object]) -> str:
    """Serialize bounded decision provenance canonically for idempotency."""

    return _serialized_mapping(
        provenance,
        field_name="provenance",
        maximum_bytes=MAX_PROVENANCE_BYTES,
    )


# endregion [01]


# region [02] Bounded read-only queries


_REVIEW_CANDIDATE_COLUMNS = """route_name,path,volume_id,file_id,size,mtime_ns,
birthtime_ns,reason_code,source_status,
recommendation,retryable,confidence,evidence_json,detector_version,status,
first_detected_ns,last_detected_ns,last_seen_run_id,resolved_ns,
resolved_run_id,resolution_note"""


def _review_candidate_record(row: sqlite3.Row) -> ReviewCandidateRecord:
    evidence = json.loads(str(row["evidence_json"]))
    if not isinstance(evidence, dict):
        raise sqlite3.DatabaseError("review evidence must be a JSON object")
    return ReviewCandidateRecord(
        route_name=str(row["route_name"]),
        path=str(row["path"]),
        volume_id=int(str(row["volume_id"]), 16),
        file_id=int(str(row["file_id"]), 16),
        size=int(row["size"]),
        mtime_ns=int(row["mtime_ns"]),
        birthtime_ns=int(row["birthtime_ns"]),
        reason_code=str(row["reason_code"]),
        source_status=str(row["source_status"]),
        recommendation=str(row["recommendation"]),
        retryable=bool(row["retryable"]),
        confidence=float(row["confidence"]),
        evidence=evidence,
        detector_version=str(row["detector_version"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        first_detected_ns=int(row["first_detected_ns"]),
        last_detected_ns=int(row["last_detected_ns"]),
        last_detected_generation=int(row["last_seen_run_id"]),
        resolved_ns=None if row["resolved_ns"] is None else int(row["resolved_ns"]),
        resolved_generation=(
            None if row["resolved_run_id"] is None else int(row["resolved_run_id"])
        ),
        resolution_note=(None if row["resolution_note"] is None else str(row["resolution_note"])),
    )


def list_review_candidates(
    database: str | Path,
    *,
    limit: int,
    route_name: str | None = None,
    recommendation: ReviewRecommendation | None = None,
    status: ReviewStatus = "open",
) -> list[ReviewCandidateRecord]:
    """Return a bounded stable view without creating or migrating state."""

    if not 1 <= limit <= 10_000:
        raise ValueError("review limit must be between 1 and 10000")
    if recommendation is not None and recommendation not in REVIEW_RECOMMENDATIONS:
        raise ValueError(f"invalid review recommendation: {recommendation}")
    if status not in REVIEW_STATUSES:
        raise ValueError(f"invalid review status: {status}")

    connection = connect_existing_framework(Path(database), readonly=True, timeout_seconds=60)
    try:
        clauses = ["status=?"]
        parameters: list[object] = [status]
        if route_name is not None:
            clauses.append("route_name=?")
            parameters.append(route_name)
        if recommendation is not None:
            clauses.append("recommendation=?")
            parameters.append(recommendation)
        parameters.append(limit)
        rows = connection.execute(
            "SELECT "
            + _REVIEW_CANDIDATE_COLUMNS
            + " FROM findings WHERE "
            + " AND ".join(clauses)
            + """ ORDER BY
            CASE recommendation
                WHEN 'deletion_candidate' THEN 0
                WHEN 'manual_review' THEN 1
                WHEN 'keep_protected' THEN 2
                ELSE 3
            END,
            confidence DESC,route_name,path,reason_code LIMIT ?""",
            parameters,
        ).fetchall()
    finally:
        connection.close()

    return [_review_candidate_record(row) for row in rows]


def get_review_candidate(
    database: str | Path,
    *,
    route_name: str,
    volume_id: int,
    file_id: int,
    reason_code: str,
) -> ReviewCandidateRecord | None:
    """Read one finding by its durable identity and reason without migration."""

    _validated_identifier(route_name, field_name="route_name")
    _validated_identifier(reason_code, field_name="reason_code")
    for field_name, value in (("volume_id", volume_id), ("file_id", file_id)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"review {field_name} must be a non-negative integer")

    connection = connect_existing_framework(Path(database), readonly=True, timeout_seconds=60)
    try:
        row = connection.execute(
            "SELECT "
            + _REVIEW_CANDIDATE_COLUMNS
            + """ FROM findings WHERE route_name=? AND volume_id=?
            AND file_id=? AND reason_code=?""",
            (route_name, f"{volume_id:x}", f"{file_id:x}", reason_code),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else _review_candidate_record(row)


_REVIEW_DECISION_COLUMNS = """decision_id,idempotency_key,route_name,path,
volume_id,file_id,size,mtime_ns,birthtime_ns,reason_code,candidate_generation,
source_status,recommendation,retryable,confidence,evidence_json,detector_version,
status,actor,provenance_json,note,decided_ns,recorded_ns"""


def _review_decision_record(row: sqlite3.Row) -> ReviewDecisionRecord:
    provenance = json.loads(str(row["provenance_json"]))
    if not isinstance(provenance, dict):
        raise sqlite3.DatabaseError("review provenance must be a JSON object")
    snapshot_columns = (
        "source_status",
        "recommendation",
        "retryable",
        "confidence",
        "evidence_json",
        "detector_version",
    )
    snapshot_values = tuple(row[column] for column in snapshot_columns)
    if all(value is None for value in snapshot_values):
        source_status = None
        recommendation = None
        retryable = None
        confidence = None
        evidence = None
        detector_version = None
    else:
        if any(value is None for value in snapshot_values):
            raise sqlite3.DatabaseError("review decision candidate snapshot is incomplete")
        source_status = str(row["source_status"])
        recommendation = str(row["recommendation"])
        if recommendation not in REVIEW_RECOMMENDATIONS:
            raise sqlite3.DatabaseError("review decision recommendation is invalid")
        retryable_value = int(row["retryable"])
        if retryable_value not in (0, 1):
            raise sqlite3.DatabaseError("review decision retryable is invalid")
        retryable = bool(retryable_value)
        confidence = float(row["confidence"])
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise sqlite3.DatabaseError("review decision confidence is invalid")
        raw_evidence = str(row["evidence_json"])
        evidence_value = json.loads(raw_evidence)
        if not isinstance(evidence_value, dict):
            raise sqlite3.DatabaseError("review decision evidence must be a JSON object")
        if serialized_evidence(evidence_value) != raw_evidence:
            raise sqlite3.DatabaseError("review decision evidence must be canonical and bounded")
        evidence = evidence_value
        detector_version = str(row["detector_version"])
    return ReviewDecisionRecord(
        decision_id=int(row["decision_id"]),
        idempotency_key=str(row["idempotency_key"]),
        route_name=str(row["route_name"]),
        path=str(row["path"]),
        volume_id=int(str(row["volume_id"]), 16),
        file_id=int(str(row["file_id"]), 16),
        size=int(row["size"]),
        mtime_ns=int(row["mtime_ns"]),
        birthtime_ns=int(row["birthtime_ns"]),
        reason_code=str(row["reason_code"]),
        candidate_generation=int(row["candidate_generation"]),
        source_status=source_status,
        recommendation=recommendation,
        retryable=retryable,
        confidence=confidence,
        evidence=evidence,
        detector_version=detector_version,
        status=str(row["status"]),  # type: ignore[arg-type]
        actor=str(row["actor"]),
        provenance=provenance,
        note=None if row["note"] is None else str(row["note"]),
        decided_ns=int(row["decided_ns"]),
        recorded_ns=int(row["recorded_ns"]),
    )


def _validate_review_decision_filters(
    *,
    limit: int,
    route_name: str | None,
    reason_code: str | None,
    status: ReviewDecisionStatus | None,
    volume_id: int | None,
    file_id: int | None,
    candidate_generation: int | None,
) -> None:
    if not 1 <= limit <= 10_000:
        raise ValueError("review decision limit must be between 1 and 10000")
    if route_name is not None:
        _validated_identifier(route_name, field_name="route_name")
    if reason_code is not None:
        _validated_identifier(reason_code, field_name="reason_code")
    if status is not None and status not in REVIEW_DECISION_STATUSES:
        raise ValueError(f"invalid review decision status: {status}")
    if (volume_id is None) != (file_id is None):
        raise ValueError("review identity filtering requires volume_id and file_id")
    if candidate_generation is not None and (
        isinstance(candidate_generation, bool) or candidate_generation < 0
    ):
        raise ValueError("review candidate_generation must be non-negative")


def _review_decision_query_filters(
    *,
    limit: int,
    route_name: str | None,
    reason_code: str | None,
    status: ReviewDecisionStatus | None,
    volume_id: int | None,
    file_id: int | None,
    candidate_generation: int | None,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    parameters: list[object] = []
    for column, value in (
        ("route_name", route_name),
        ("reason_code", reason_code),
        ("status", status),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            parameters.append(value)
    if volume_id is not None and file_id is not None:
        clauses.extend(("volume_id=?", "file_id=?"))
        parameters.extend((f"{volume_id:x}", f"{file_id:x}"))
    if candidate_generation is not None:
        clauses.append("candidate_generation=?")
        parameters.append(candidate_generation)
    parameters.append(limit)
    where = "" if not clauses else " WHERE " + " AND ".join(clauses)
    return where, parameters


def list_review_decisions(
    database: str | Path,
    *,
    limit: int,
    route_name: str | None = None,
    reason_code: str | None = None,
    status: ReviewDecisionStatus | None = None,
    volume_id: int | None = None,
    file_id: int | None = None,
    candidate_generation: int | None = None,
) -> list[ReviewDecisionRecord]:
    """Read a bounded decision history without creating or migrating state."""

    _validate_review_decision_filters(
        limit=limit,
        route_name=route_name,
        reason_code=reason_code,
        status=status,
        volume_id=volume_id,
        file_id=file_id,
        candidate_generation=candidate_generation,
    )
    where, parameters = _review_decision_query_filters(
        limit=limit,
        route_name=route_name,
        reason_code=reason_code,
        status=status,
        volume_id=volume_id,
        file_id=file_id,
        candidate_generation=candidate_generation,
    )

    connection = connect_existing_framework(Path(database), readonly=True, timeout_seconds=60)
    try:
        rows = connection.execute(
            "SELECT "
            + _REVIEW_DECISION_COLUMNS
            + " FROM review_decisions"
            + where
            + " ORDER BY recorded_ns DESC,decision_id DESC LIMIT ?",
            parameters,
        ).fetchall()
    finally:
        connection.close()

    return [_review_decision_record(row) for row in rows]


def get_review_decision_by_key(
    database: str | Path,
    idempotency_key: str,
) -> ReviewDecisionRecord | None:
    """Read one decision retry key exactly without scanning history."""

    _validated_identifier(idempotency_key, field_name="idempotency_key")
    connection = connect_existing_framework(Path(database), readonly=True, timeout_seconds=60)
    try:
        row = connection.execute(
            "SELECT " + _REVIEW_DECISION_COLUMNS + " FROM review_decisions WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else _review_decision_record(row)


# endregion [02]
