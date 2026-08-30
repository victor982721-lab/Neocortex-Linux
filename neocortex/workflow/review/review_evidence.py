"""Materialized, traceable evidence derived from append-only review decisions.

The normalized outcomes in this module are evaluation inputs only.  They never
authorize file actions and they do not establish statistical calibration by
themselves.
"""

from __future__ import annotations
# region [01] Public evidence and metric models

import json
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple

from neocortex.workflow.review.review import (
    MAX_IDENTIFIER_CHARS,
    MAX_NOTE_BYTES,
    MAX_PROVENANCE_BYTES,
    REVIEW_DECISION_STATUSES,
    REVIEW_RECOMMENDATIONS,
    ReviewDecisionStatus,
    ReviewRecommendation,
    serialized_evidence,
)
from neocortex.persistence.framework_connection import connect_existing_framework


ReviewEvidenceOutcome = Literal["accepted", "rejected", "abstained"]
ReviewEvidenceEvaluationStatus = Literal[
    "no_materialized_examples",
    "descriptive_review_outcomes",
]

EVIDENCE_SCHEMA_VERSION = 1
MAX_MATERIALIZATION_BATCH_SIZE = 256
MAX_EVIDENCE_QUERY_LIMIT = 10_000
MAX_EVIDENCE_PATH_CHARS = 32_767
_MATERIALIZATION_PIPELINE_KEY = "review-decisions-v1"

_OUTCOME_BY_STATUS: dict[ReviewDecisionStatus, ReviewEvidenceOutcome] = {
    "confirmed": "accepted",
    "dismissed": "rejected",
    "deferred": "abstained",
}


@dataclass(frozen=True, slots=True)
class ReviewEvidenceExample:
    """One immutable human-feedback example with its candidate context."""

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
    target_recommendation: ReviewRecommendation | None
    retryable: bool | None
    confidence: float | None
    candidate_evidence: dict[str, object] | None
    detector_version: str | None
    decision_status: ReviewDecisionStatus
    outcome: ReviewEvidenceOutcome
    actor: str
    provenance: dict[str, object]
    note: str | None
    decided_ns: int
    recorded_ns: int
    candidate_evidence_complete: bool
    evidence_schema_version: int
    materialized_ns: int

    @property
    def feedback_reference(self) -> str:
        """Return the stable reference expected by semantic evidence records."""

        return f"review-decision:{self.decision_id}"


@dataclass(frozen=True, slots=True)
class ReviewEvidenceSyncResult:
    """Result of one bounded, resumable materialization transaction."""

    scanned_decisions: int
    materialized_examples: int
    last_decision_id: int | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class ReviewEvidenceMetrics:
    """Descriptive review outcomes with explicit coverage and limitations."""

    total_decisions: int
    materialized_examples: int
    confirmed_examples: int
    dismissed_examples: int
    deferred_examples: int
    decisive_examples: int
    complete_candidate_evidence: int
    materialization_coverage: float | None
    decisive_label_coverage: float | None
    candidate_evidence_coverage: float | None
    acceptance_rate: float | None
    rejection_rate: float | None
    abstention_rate: float | None
    evaluation_status: ReviewEvidenceEvaluationStatus
    calibration_status: Literal["not_established"]
    calibration_reason: str


# endregion [01]


# region [02] Canonical materialization


class _MaterializedValues(NamedTuple):
    decision_id: int
    idempotency_key: str
    route_name: str
    volume_id: str
    file_id: str
    path: str
    size: int
    mtime_ns: int
    birthtime_ns: int
    reason_code: str
    candidate_generation: int
    source_status: str | None
    target_recommendation: str | None
    retryable: int | None
    confidence: float | None
    evidence_json: str | None
    detector_version: str | None
    decision_status: str
    actor: str
    provenance_json: str
    note: str | None
    decided_ns: int
    recorded_ns: int
    outcome: str
    candidate_evidence_complete: int
    evidence_schema_version: int
    materialized_ns: int


_SOURCE_DECISION_COLUMNS = """decision_id,idempotency_key,route_name,volume_id,
file_id,path,size,mtime_ns,birthtime_ns,reason_code,candidate_generation,
source_status,recommendation,retryable,confidence,evidence_json,detector_version,
status,actor,provenance_json,note,decided_ns,recorded_ns"""

_MATERIALIZED_COLUMNS = """decision_id,idempotency_key,route_name,volume_id,
file_id,path,size,mtime_ns,birthtime_ns,reason_code,candidate_generation,
source_status,target_recommendation,retryable,confidence,evidence_json,
detector_version,decision_status,actor,provenance_json,note,decided_ns,
recorded_ns,outcome,candidate_evidence_complete,evidence_schema_version,
materialized_ns"""

_INSERT_MATERIALIZED_SQL = (
    "INSERT INTO review_evidence_examples(" + _MATERIALIZED_COLUMNS + ") "
    "VALUES(" + ",".join("?" for _ in range(27)) + ")"
)


def _validated_identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise sqlite3.DatabaseError(
            f"review evidence {field_name} must be non-empty and trimmed"
        )
    if len(value) > MAX_IDENTIFIER_CHARS:
        raise sqlite3.DatabaseError(
            f"review evidence {field_name} exceeds {MAX_IDENTIFIER_CHARS} characters"
        )
    return value


def _positive_integer(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise sqlite3.DatabaseError(
            f"review evidence {field_name} must be a positive integer"
        )
    try:
        result = int(value)
    except ValueError as exc:
        raise sqlite3.DatabaseError(
            f"review evidence {field_name} must be a positive integer"
        ) from exc
    if result <= 0:
        raise sqlite3.DatabaseError(
            f"review evidence {field_name} must be a positive integer"
        )
    return result


def _non_negative_integer(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise sqlite3.DatabaseError(
            f"review evidence {field_name} must be a non-negative integer"
        )
    try:
        result = int(value)
    except ValueError as exc:
        raise sqlite3.DatabaseError(
            f"review evidence {field_name} must be a non-negative integer"
        ) from exc
    if result < 0:
        raise sqlite3.DatabaseError(
            f"review evidence {field_name} must be a non-negative integer"
        )
    return result


def _integer(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise sqlite3.DatabaseError(f"review evidence {field_name} must be an integer")
    try:
        return int(value)
    except ValueError as exc:
        raise sqlite3.DatabaseError(
            f"review evidence {field_name} must be an integer"
        ) from exc


def _json_object(raw_value: object, *, field_name: str) -> dict[str, object]:
    try:
        value = json.loads(str(raw_value))
    except (TypeError, ValueError) as exc:
        raise sqlite3.DatabaseError(
            f"review evidence {field_name} must be valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise sqlite3.DatabaseError(
            f"review evidence {field_name} must be a JSON object"
        )
    return value


def _validated_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise sqlite3.DatabaseError("review evidence path must be non-empty")
    if len(value) > MAX_EVIDENCE_PATH_CHARS:
        raise sqlite3.DatabaseError(
            f"review evidence path exceeds {MAX_EVIDENCE_PATH_CHARS} characters"
        )
    return value


def _materialized_values(
    source: tuple[object, ...],
    *,
    materialized_ns: int,
) -> _MaterializedValues:
    if len(source) != 23:  # pragma: no cover - internal query invariant
        raise AssertionError("review decision query returned an unexpected shape")

    decision_id = _positive_integer(source[0], field_name="decision_id")
    idempotency_key = _validated_identifier(source[1], field_name="idempotency_key")
    route_name = _validated_identifier(source[2], field_name="route_name")
    volume_id = _validated_identifier(source[3], field_name="volume_id")
    file_id = _validated_identifier(source[4], field_name="file_id")
    try:
        if int(volume_id, 16) < 0 or int(file_id, 16) < 0:
            raise ValueError
    except ValueError as exc:
        raise sqlite3.DatabaseError(
            "review evidence file identity must contain non-negative hexadecimal values"
        ) from exc
    path = _validated_path(source[5])
    size = _non_negative_integer(source[6], field_name="size")
    mtime_ns = _integer(source[7], field_name="mtime_ns")
    birthtime_ns = _integer(source[8], field_name="birthtime_ns")
    reason_code = _validated_identifier(source[9], field_name="reason_code")
    candidate_generation = _non_negative_integer(
        source[10], field_name="candidate_generation"
    )

    candidate_values = source[11:17]
    if all(value is None for value in candidate_values):
        source_status = None
        target_recommendation = None
        retryable = None
        confidence = None
        evidence_json = None
        detector_version = None
        candidate_evidence_complete = 0
    elif any(value is None for value in candidate_values):
        raise sqlite3.DatabaseError(
            "review decision candidate evidence is partially populated"
        )
    else:
        source_status = _validated_identifier(source[11], field_name="source_status")
        target_recommendation = str(source[12])
        if target_recommendation not in REVIEW_RECOMMENDATIONS:
            raise sqlite3.DatabaseError(
                "review evidence target recommendation is invalid"
            )
        retryable_value = source[13]
        if isinstance(retryable_value, bool) or not isinstance(
            retryable_value, (int, str)
        ):
            raise sqlite3.DatabaseError("review evidence retryable is invalid")
        retryable = int(retryable_value)
        if retryable not in (0, 1):
            raise sqlite3.DatabaseError("review evidence retryable is invalid")
        confidence_value = source[14]
        if not isinstance(confidence_value, (int, float, str)) or isinstance(
            confidence_value, bool
        ):
            raise sqlite3.DatabaseError("review evidence confidence is invalid")
        confidence = float(confidence_value)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise sqlite3.DatabaseError("review evidence confidence is invalid")
        evidence_json = str(source[15])
        candidate_evidence = _json_object(
            evidence_json, field_name="candidate evidence"
        )
        if serialized_evidence(candidate_evidence) != evidence_json:
            raise sqlite3.DatabaseError(
                "review candidate evidence must be canonical and bounded"
            )
        detector_version = _validated_identifier(
            source[16], field_name="detector_version"
        )
        candidate_evidence_complete = 1

    decision_status = str(source[17])
    if decision_status not in REVIEW_DECISION_STATUSES:
        raise sqlite3.DatabaseError("review evidence decision status is invalid")
    outcome = _OUTCOME_BY_STATUS[decision_status]  # type: ignore[index]
    actor = _validated_identifier(source[18], field_name="actor")
    provenance_json = str(source[19])
    if len(provenance_json.encode("utf-8")) > MAX_PROVENANCE_BYTES:
        raise sqlite3.DatabaseError("review evidence provenance is too large")
    _json_object(provenance_json, field_name="provenance")
    note = None if source[20] is None else str(source[20])
    if note is not None and len(note.encode("utf-8")) > MAX_NOTE_BYTES:
        raise sqlite3.DatabaseError("review evidence note is too large")
    decided_ns = _positive_integer(source[21], field_name="decided_ns")
    recorded_ns = _positive_integer(source[22], field_name="recorded_ns")
    materialized_ns = _positive_integer(materialized_ns, field_name="materialized_ns")

    return _MaterializedValues(
        decision_id,
        idempotency_key,
        route_name,
        volume_id,
        file_id,
        path,
        size,
        mtime_ns,
        birthtime_ns,
        reason_code,
        candidate_generation,
        source_status,
        target_recommendation,
        retryable,
        confidence,
        evidence_json,
        detector_version,
        decision_status,
        actor,
        provenance_json,
        note,
        decided_ns,
        recorded_ns,
        outcome,
        candidate_evidence_complete,
        EVIDENCE_SCHEMA_VERSION,
        materialized_ns,
    )


def _materialize_review_decision(
    connection: sqlite3.Connection,
    decision_id: int,
    *,
    materialized_ns: int | None = None,
) -> bool:
    """Materialize one decision inside the caller's write transaction."""

    source = connection.execute(
        "SELECT " + _SOURCE_DECISION_COLUMNS + " FROM review_decisions "
        "WHERE decision_id=?",
        (decision_id,),
    ).fetchone()
    if source is None:
        raise sqlite3.DatabaseError(
            f"review decision {decision_id} disappeared before materialization"
        )
    values = _materialized_values(
        tuple(source),
        materialized_ns=time.time_ns() if materialized_ns is None else materialized_ns,
    )
    existing_rows = connection.execute(
        "SELECT " + _MATERIALIZED_COLUMNS + " FROM review_evidence_examples "
        "WHERE decision_id=? OR idempotency_key=? ORDER BY decision_id LIMIT 2",
        (values.decision_id, values.idempotency_key),
    ).fetchall()
    if existing_rows:
        if len(existing_rows) != 1:
            raise sqlite3.DatabaseError(
                "materialized review evidence has conflicting identities"
            )
        _require_matching_materialization(values, tuple(existing_rows[0]))
        return False

    cursor = connection.execute(_INSERT_MATERIALIZED_SQL, values)
    if cursor.rowcount != 1:  # pragma: no cover - SQLite insert invariant
        raise sqlite3.DatabaseError("review evidence example was not materialized")
    return True


def _require_matching_materialization(
    values: _MaterializedValues,
    existing: tuple[object, ...],
) -> None:
    if tuple(existing[:-1]) != tuple(values[:-1]):
        raise sqlite3.DatabaseError(
            "materialized review evidence conflicts with its append-only decision"
        )


def _pending_materializations(
    connection: sqlite3.Connection,
    values: list[_MaterializedValues],
) -> list[_MaterializedValues]:
    """Filter an already-bounded batch with two set-based identity lookups."""

    if not values:
        return []
    placeholders = ",".join("?" for _ in values)
    existing_rows = connection.execute(
        "SELECT "
        + _MATERIALIZED_COLUMNS
        + " FROM review_evidence_examples WHERE decision_id IN ("
        + placeholders
        + ") OR idempotency_key IN ("
        + placeholders
        + ")",
        tuple(value.decision_id for value in values)
        + tuple(value.idempotency_key for value in values),
    ).fetchall()
    by_decision_id = {int(row[0]): tuple(row) for row in existing_rows}
    by_idempotency_key = {str(row[1]): tuple(row) for row in existing_rows}
    pending: list[_MaterializedValues] = []
    for value in values:
        by_id = by_decision_id.get(value.decision_id)
        by_key = by_idempotency_key.get(value.idempotency_key)
        if by_id is not None and by_key is not None and by_id != by_key:
            raise sqlite3.DatabaseError(
                "materialized review evidence has conflicting identities"
            )
        existing = by_id if by_id is not None else by_key
        if existing is None:
            pending.append(value)
            continue
        _require_matching_materialization(value, existing)
    return pending


def materialize_review_evidence(
    database: str | Path,
    *,
    batch_size: int = 128,
) -> ReviewEvidenceSyncResult:
    """Scan the next durable decision range in one bounded transaction.

    Repeated calls resume from ``review_evidence_progress`` without rescanning
    prior ranges. Existing examples remain idempotent when the writer already
    materialized them atomically.
    """

    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= MAX_MATERIALIZATION_BATCH_SIZE
    ):
        raise ValueError(
            "review evidence batch_size must be between 1 and "
            f"{MAX_MATERIALIZATION_BATCH_SIZE}"
        )

    connection = connect_existing_framework(
        Path(database), readonly=False, timeout_seconds=60
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        materialized_ns = time.time_ns()
        connection.execute(
            """INSERT OR IGNORE INTO review_evidence_progress(
            pipeline_key,last_scanned_decision_id,updated_ns) VALUES(?,0,?)""",
            (_MATERIALIZATION_PIPELINE_KEY, materialized_ns),
        )
        progress = connection.execute(
            """SELECT last_scanned_decision_id FROM review_evidence_progress
            WHERE pipeline_key=?""",
            (_MATERIALIZATION_PIPELINE_KEY,),
        ).fetchone()
        if progress is None:  # pragma: no cover - insert/select invariant
            raise sqlite3.DatabaseError(
                "review evidence materialization progress is unavailable"
            )
        last_scanned_decision_id = _non_negative_integer(
            progress[0], field_name="last_scanned_decision_id"
        )
        rows = connection.execute(
            "SELECT " + _SOURCE_DECISION_COLUMNS + " FROM review_decisions "
            """WHERE decision_id>?
            ORDER BY decision_id
            LIMIT ?""",
            (last_scanned_decision_id, batch_size + 1),
        ).fetchall()
        selected = rows[:batch_size]
        values = [
            _materialized_values(tuple(row), materialized_ns=materialized_ns)
            for row in selected
        ]
        pending = _pending_materializations(connection, values)
        connection.executemany(
            _INSERT_MATERIALIZED_SQL,
            pending,
        )
        materialized = len(pending)
        if selected:
            updated = connection.execute(
                """UPDATE review_evidence_progress
                SET last_scanned_decision_id=?,updated_ns=? WHERE pipeline_key=?""",
                (
                    int(selected[-1][0]),
                    materialized_ns,
                    _MATERIALIZATION_PIPELINE_KEY,
                ),
            )
            if updated.rowcount != 1:  # pragma: no cover - transaction invariant
                raise sqlite3.DatabaseError(
                    "review evidence materialization progress was not updated"
                )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    return ReviewEvidenceSyncResult(
        scanned_decisions=len(selected),
        materialized_examples=materialized,
        last_decision_id=None if not selected else int(selected[-1][0]),
        has_more=len(rows) > batch_size,
    )


# endregion [02]


# region [03] Bounded consumption and descriptive evaluation


def _query_identifier(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    try:
        return _validated_identifier(value, field_name=field_name)
    except sqlite3.DatabaseError as exc:
        raise ValueError(str(exc)) from exc


def _query_filters(
    *,
    table_alias: str,
    route_name: str | None,
    reason_code: str | None,
    target_recommendation: ReviewRecommendation | None,
    detector_version: str | None,
    actor: str | None,
) -> tuple[list[str], list[object]]:
    route_name = _query_identifier(route_name, field_name="route_name")
    reason_code = _query_identifier(reason_code, field_name="reason_code")
    detector_version = _query_identifier(
        detector_version, field_name="detector_version"
    )
    actor = _query_identifier(actor, field_name="actor")
    if (
        target_recommendation is not None
        and target_recommendation not in REVIEW_RECOMMENDATIONS
    ):
        raise ValueError(
            f"invalid review target recommendation: {target_recommendation}"
        )

    clauses: list[str] = []
    parameters: list[object] = []
    for column, value in (
        ("route_name", route_name),
        ("reason_code", reason_code),
        ("target_recommendation", target_recommendation),
        ("detector_version", detector_version),
        ("actor", actor),
    ):
        if value is not None:
            clauses.append(f"{table_alias}.{column}=?")
            parameters.append(value)
    return clauses, parameters


def _review_evidence_example(row: sqlite3.Row) -> ReviewEvidenceExample:
    columns = tuple(column.strip() for column in _MATERIALIZED_COLUMNS.split(","))
    values = tuple(row[column] for column in columns)
    candidate_values = values[11:17]
    candidate_evidence_complete = int(values[24])
    if candidate_evidence_complete not in (0, 1):
        raise sqlite3.DatabaseError(
            "review candidate evidence completeness flag is invalid"
        )
    if candidate_evidence_complete:
        if any(value is None for value in candidate_values):
            raise sqlite3.DatabaseError("review candidate evidence is incomplete")
        source_status = str(values[11])
        target_recommendation = str(values[12])
        if target_recommendation not in REVIEW_RECOMMENDATIONS:
            raise sqlite3.DatabaseError(
                "review evidence target recommendation is invalid"
            )
        retryable_value = int(values[13])
        if retryable_value not in (0, 1):
            raise sqlite3.DatabaseError("review evidence retryable is invalid")
        retryable = bool(retryable_value)
        confidence = float(values[14])
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise sqlite3.DatabaseError("review evidence confidence is invalid")
        candidate_evidence = _json_object(values[15], field_name="candidate evidence")
        detector_version = str(values[16])
    else:
        if any(value is not None for value in candidate_values):
            raise sqlite3.DatabaseError(
                "legacy review candidate evidence must be entirely unavailable"
            )
        source_status = None
        target_recommendation = None
        retryable = None
        confidence = None
        candidate_evidence = None
        detector_version = None

    decision_status = str(values[17])
    outcome = str(values[23])
    if decision_status not in REVIEW_DECISION_STATUSES:
        raise sqlite3.DatabaseError("review evidence decision status is invalid")
    if _OUTCOME_BY_STATUS[decision_status] != outcome:  # type: ignore[index]
        raise sqlite3.DatabaseError(
            "review evidence outcome does not match its decision status"
        )
    provenance = _json_object(values[19], field_name="provenance")
    evidence_schema_version = int(values[25])
    if evidence_schema_version != EVIDENCE_SCHEMA_VERSION:
        raise sqlite3.DatabaseError(
            "review evidence schema version is unsupported by this reader"
        )
    return ReviewEvidenceExample(
        decision_id=int(values[0]),
        idempotency_key=str(values[1]),
        route_name=str(values[2]),
        volume_id=int(str(values[3]), 16),
        file_id=int(str(values[4]), 16),
        path=str(values[5]),
        size=int(values[6]),
        mtime_ns=int(values[7]),
        birthtime_ns=int(values[8]),
        reason_code=str(values[9]),
        candidate_generation=int(values[10]),
        source_status=source_status,
        target_recommendation=target_recommendation,  # type: ignore[arg-type]
        retryable=retryable,
        confidence=confidence,
        candidate_evidence=candidate_evidence,
        detector_version=detector_version,
        decision_status=decision_status,  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
        actor=str(values[18]),
        provenance=provenance,
        note=None if values[20] is None else str(values[20]),
        decided_ns=int(values[21]),
        recorded_ns=int(values[22]),
        candidate_evidence_complete=bool(candidate_evidence_complete),
        evidence_schema_version=evidence_schema_version,
        materialized_ns=int(values[26]),
    )


def list_review_evidence(
    database: str | Path,
    *,
    limit: int,
    decision_status: ReviewDecisionStatus | None = None,
    route_name: str | None = None,
    reason_code: str | None = None,
    target_recommendation: ReviewRecommendation | None = None,
    detector_version: str | None = None,
    actor: str | None = None,
    require_complete_candidate_evidence: bool | None = None,
) -> list[ReviewEvidenceExample]:
    """Read a bounded, stable evidence view without creating or migrating state."""

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_EVIDENCE_QUERY_LIMIT
    ):
        raise ValueError(
            f"review evidence limit must be between 1 and {MAX_EVIDENCE_QUERY_LIMIT}"
        )
    if decision_status is not None and decision_status not in REVIEW_DECISION_STATUSES:
        raise ValueError(f"invalid review decision status: {decision_status}")
    if require_complete_candidate_evidence is not None and not isinstance(
        require_complete_candidate_evidence, bool
    ):
        raise TypeError("review evidence completeness filter must be a boolean")
    clauses, parameters = _query_filters(
        table_alias="examples",
        route_name=route_name,
        reason_code=reason_code,
        target_recommendation=target_recommendation,
        detector_version=detector_version,
        actor=actor,
    )
    if decision_status is not None:
        clauses.append("examples.decision_status=?")
        parameters.append(decision_status)
    if require_complete_candidate_evidence is not None:
        clauses.append("examples.candidate_evidence_complete=?")
        parameters.append(int(require_complete_candidate_evidence))
    parameters.append(limit)

    connection = connect_existing_framework(
        Path(database), readonly=True, timeout_seconds=60
    )
    try:
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        rows = connection.execute(
            "SELECT " + _MATERIALIZED_COLUMNS + " FROM review_evidence_examples "
            "AS examples" + where + " ORDER BY examples.decision_id LIMIT ?",
            parameters,
        ).fetchall()
    finally:
        connection.close()
    return [_review_evidence_example(row) for row in rows]


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def review_evidence_metrics(
    database: str | Path,
    *,
    route_name: str | None = None,
    reason_code: str | None = None,
    target_recommendation: ReviewRecommendation | None = None,
    detector_version: str | None = None,
    actor: str | None = None,
) -> ReviewEvidenceMetrics:
    """Aggregate honest review rates; never infer calibration or file policy."""

    clauses, parameters = _query_filters(
        table_alias="decisions",
        route_name=route_name,
        reason_code=reason_code,
        target_recommendation=target_recommendation,
        detector_version=detector_version,
        actor=actor,
    )
    # The source table calls the proposed target ``recommendation``.
    clauses = [
        clause.replace("decisions.target_recommendation", "decisions.recommendation")
        for clause in clauses
    ]
    where = "" if not clauses else " WHERE " + " AND ".join(clauses)
    connection = connect_existing_framework(
        Path(database), readonly=True, timeout_seconds=60
    )
    try:
        row = connection.execute(
            """SELECT COUNT(decisions.decision_id),
            COUNT(examples.decision_id),
            COALESCE(SUM(examples.decision_status='confirmed'),0),
            COALESCE(SUM(examples.decision_status='dismissed'),0),
            COALESCE(SUM(examples.decision_status='deferred'),0),
            COALESCE(SUM(examples.candidate_evidence_complete=1),0)
            FROM review_decisions AS decisions
            LEFT JOIN review_evidence_examples AS examples
              ON examples.decision_id=decisions.decision_id"""
            + where,
            parameters,
        ).fetchone()
    finally:
        connection.close()
    if row is None:  # pragma: no cover - aggregate query invariant
        raise sqlite3.DatabaseError("review evidence metrics query returned no row")

    total_decisions = int(row[0])
    materialized_examples = int(row[1])
    confirmed_examples = int(row[2])
    dismissed_examples = int(row[3])
    deferred_examples = int(row[4])
    complete_candidate_evidence = int(row[5])
    decisive_examples = confirmed_examples + dismissed_examples
    if materialized_examples == 0:
        evaluation_status: ReviewEvidenceEvaluationStatus = "no_materialized_examples"
        calibration_reason = "no materialized human-review examples are available"
    elif decisive_examples == 0:
        evaluation_status = "descriptive_review_outcomes"
        calibration_reason = (
            "deferred reviews record abstention but provide no accepted or rejected "
            "candidate labels"
        )
    else:
        evaluation_status = "descriptive_review_outcomes"
        calibration_reason = (
            "review outcomes are not an independent representative ground-truth set"
        )

    return ReviewEvidenceMetrics(
        total_decisions=total_decisions,
        materialized_examples=materialized_examples,
        confirmed_examples=confirmed_examples,
        dismissed_examples=dismissed_examples,
        deferred_examples=deferred_examples,
        decisive_examples=decisive_examples,
        complete_candidate_evidence=complete_candidate_evidence,
        materialization_coverage=_rate(materialized_examples, total_decisions),
        decisive_label_coverage=_rate(decisive_examples, materialized_examples),
        candidate_evidence_coverage=_rate(
            complete_candidate_evidence, materialized_examples
        ),
        acceptance_rate=_rate(confirmed_examples, materialized_examples),
        rejection_rate=_rate(dismissed_examples, materialized_examples),
        abstention_rate=_rate(deferred_examples, materialized_examples),
        evaluation_status=evaluation_status,
        calibration_status="not_established",
        calibration_reason=calibration_reason,
    )


# endregion [03]
