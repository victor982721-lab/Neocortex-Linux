"""Snapshot-bound pagination of persisted, non-authorizing findings."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from neocortex.persistence.framework_connection import connect_existing_framework
from neocortex.persistence.framework_schema import SCHEMA_VERSION
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    capture_sqlite_read_fence,
)
from neocortex.persistence.sqlite_schema_contract import read_application_schema_version

from neocortex.workflow.findings import (
    FINDING_RECOMMENDATIONS,
    ReviewRecommendation,
)

@dataclass(frozen=True, slots=True)
class FindingRecord:
    """One persisted route finding, kept separate from any human workflow."""

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
    evidence: dict[str, object]
    detector_version: str
    status: str
    first_detected_ns: int
    last_detected_ns: int
    last_detected_generation: int
    resolved_ns: int | None
    resolved_generation: int | None
    resolution_note: str | None


_FINDING_COLUMNS = """route_name,path,volume_id,file_id,size,mtime_ns,
birthtime_ns,reason_code,source_status,
recommendation,retryable,confidence,evidence_json,detector_version,status,
first_detected_ns,last_detected_ns,last_seen_run_id,resolved_ns,
resolved_run_id,resolution_note"""


def _finding_record(row) -> FindingRecord:
    import json
    evidence = json.loads(str(row["evidence_json"]))
    if not isinstance(evidence, dict):
        raise sqlite3.DatabaseError("finding evidence must be a JSON object")
    return FindingRecord(
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
        status=str(row["status"]),
        first_detected_ns=int(row["first_detected_ns"]),
        last_detected_ns=int(row["last_detected_ns"]),
        last_detected_generation=int(row["last_seen_run_id"]),
        resolved_ns=None if row["resolved_ns"] is None else int(row["resolved_ns"]),
        resolved_generation=(None if row["resolved_run_id"] is None else int(row["resolved_run_id"])),
        resolution_note=(None if row["resolution_note"] is None else str(row["resolution_note"])),
    )

FINDING_STATUSES = frozenset({"open", "resolved"})
FindingStatus = str


def list_findings(
    database: str | Path,
    *,
    limit: int,
    route_name: str | None = None,
    recommendation: ReviewRecommendation | None = None,
    status: FindingStatus = "open",
) -> list[FindingRecord]:
    """Return a bounded, read-only view of persisted route findings."""

    if not 1 <= limit <= 10_000:
        raise ValueError("finding limit must be between 1 and 10000")
    if recommendation is not None and recommendation not in FINDING_RECOMMENDATIONS:
        raise ValueError(f"invalid finding recommendation: {recommendation}")
    if status not in FINDING_STATUSES:
        raise ValueError(f"invalid finding status: {status}")
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
            + _FINDING_COLUMNS
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
    return [_finding_record(row) for row in rows]



_RANK_SQL = """CASE recommendation WHEN 'deletion_candidate' THEN 0
WHEN 'manual_review' THEN 1 WHEN 'keep_protected' THEN 2 ELSE 3 END"""


def _digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def finding_projection(
    record: FindingRecord, *, snapshot_id: str,
) -> dict[str, object]:
    """Preserve a legacy finding without upgrading its label into actionability."""

    payload = asdict(record)
    actions = {
        "retry": ("review_retry_conditions", ("processing_retry_safety",)),
        "manual_review": ("inspect_published_evidence", ("current_source_outcome",)),
        "keep_protected": ("retain_protected_source", ("authorized_content_inspection",)),
        "deletion_candidate": (
            "review_disposal_preconditions", ("independent_disposability", "retention_and_dependency_review"),
        ),
    }
    action, missing = actions[record.recommendation]
    preconditions = (
        ("explicit_mutation_authorization", "physical_revision_revalidation")
        if record.recommendation == "deletion_candidate" else
        ("bounded_read_only_inspection", "source_snapshot_revalidation")
    )
    payload["recommendation_detail"] = {
        "action": action,
        "basis": "legacy_owner_recommendation",
        "evidence_refs": [{
            "owner": "framework",
            "record_id": (f"review-candidate:{record.route_name}:{record.volume_id}:"
                          f"{record.file_id}:{record.reason_code}"),
            "resource_id": f"resource:file:{record.volume_id}:{record.file_id}:{record.birthtime_ns}",
            "snapshot_id": snapshot_id,
            "projection_digest": _digest(payload),
            "generation": record.last_detected_generation,
        }],
        "missing_checks": list(missing),
        "preconditions": list(preconditions),
        "executable": False,
        "execution_scope": "advisory_projection_not_an_execution_plan",
        "mutation_authorized": False,
        "score_semantics": "legacy_heuristic_not_calibrated_probability",
    }
    return payload


@dataclass(frozen=True, slots=True)
class FindingListCursor:
    snapshot_id: str
    query_signature: str
    rank: int
    confidence: float
    route_name: str
    path: str
    reason_code: str
    volume_id: str
    file_id: str

    def __post_init__(self) -> None:
        for digest in (self.snapshot_id, self.query_signature):
            if not isinstance(digest, str) or len(digest) != 71 or not digest.startswith("sha256:") or any(
                char not in "0123456789abcdef" for char in digest[7:]
            ):
                raise ValueError("cursor snapshot/query signature must be SHA-256")
        if type(self.rank) is not int or not 0 <= self.rank <= 3:
            raise ValueError("cursor rank must be between 0 and 3")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (float, int)) or not (
            math.isfinite(self.confidence) and 0 <= self.confidence <= 1
        ):
            raise ValueError("cursor confidence must be finite and bounded")
        for value in (self.route_name, self.path, self.reason_code, self.volume_id, self.file_id):
            if not isinstance(value, str) or not value or len(value) > 32768:
                raise ValueError("cursor keys must be bounded non-empty text")
        for value in (self.volume_id, self.file_id):
            if any(char not in "0123456789abcdef" for char in value):
                raise ValueError("cursor physical identities must be canonical hexadecimal")

    def to_token(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def from_token(cls, token: str) -> FindingListCursor:
        if not isinstance(token, str) or not 1 <= len(token) <= 65536:
            raise ValueError("finding cursor token must be bounded non-empty text")
        try:
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("cursor must be an object")
            cursor = cls(**value)
            if cursor.to_token() != token:
                raise ValueError("cursor token must be canonical")
            return cursor
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid finding cursor token") from exc


@dataclass(frozen=True, slots=True)
class FindingRecordPage:
    items: tuple[FindingRecord, ...]
    availability: Literal["ready", "absent", "failed", "snapshot_changed"]
    snapshot_id: str | None
    total_matching: int | None
    next_cursor: FindingListCursor | None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or len(self.items) > 10000 or any(
            not isinstance(item, FindingRecord) for item in self.items
        ):
            raise ValueError("items must be a bounded typed immutable tuple")
        if self.availability not in {"ready", "absent", "failed", "snapshot_changed"}:
            raise ValueError("unsupported finding page availability")
        if self.next_cursor is not None and not isinstance(self.next_cursor, FindingListCursor):
            raise ValueError("next_cursor must be a typed finding cursor")
        if self.availability == "ready":
            if self.snapshot_id is None or type(self.total_matching) is not int or (
                self.total_matching < len(self.items)
            ) or self.reason_code is not None:
                raise ValueError("ready requires a snapshot, exact count, and no failure reason")
        elif self.items or self.next_cursor is not None or self.total_matching is not None or not self.reason_code:
            raise ValueError("unavailable pages cannot claim rows, a count, or a continuation")
        if self.next_cursor is not None and self.next_cursor.snapshot_id != self.snapshot_id:
            raise ValueError("cursor belongs to another snapshot")

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "neocortex.findings-page/v1",
            "items": [finding_projection(item, snapshot_id=self.snapshot_id or "unavailable")
                      for item in self.items],
            "availability": self.availability,
            "snapshot_id": self.snapshot_id,
            "returned": len(self.items),
            "total_matching": self.total_matching,
            "total_exact": self.total_matching is not None,
            "has_more": self.has_more,
            "next_cursor": None if self.next_cursor is None else self.next_cursor.to_token(),
            "complete": self.availability == "ready" and self.total_matching == len(self.items),
            "reason_code": self.reason_code,
            "scope": "findings_matching_filters",
            "owner": "framework",
            "surface": "findings",
            "read_only": True,
            "advisory_only": True,
            "mutation_authorized": False,
            "score_semantics": "legacy_heuristic_not_calibrated_probability_or_authority",
        }


def list_findings_page(
    database: str | Path,
    *,
    limit: int,
    route_name: str | None = None,
    recommendation: ReviewRecommendation | None = None,
    status: FindingStatus = "open",
    after: FindingListCursor | str | None = None,
) -> FindingRecordPage:
    """Count and page one immutable logical view, never mask absence as zero."""

    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError("finding limit must be between 1 and 10000")
    if recommendation is not None and recommendation not in FINDING_RECOMMENDATIONS:
        raise ValueError("invalid finding recommendation")
    if status not in FINDING_STATUSES:
        raise ValueError("invalid finding status")
    if route_name is not None and (
        not isinstance(route_name, str) or not route_name.strip() or len(route_name) > 256
    ):
        raise ValueError("route_name must be bounded non-blank text")
    cursor = FindingListCursor.from_token(after) if isinstance(after, str) else after
    if cursor is not None and not isinstance(cursor, FindingListCursor):
        raise ValueError("after must be a FindingListCursor or its token")
    signature = _digest({"route_name": route_name, "recommendation": recommendation, "status": status})
    if cursor is not None and cursor.query_signature != signature:
        raise ValueError("finding cursor belongs to different query filters")
    selected = Path(database)
    try:
        fence = capture_sqlite_read_fence(selected)
        snapshot_id = _digest(asdict(fence))
        if cursor is not None and cursor.snapshot_id != snapshot_id:
            return FindingRecordPage((), "snapshot_changed", snapshot_id, None, None,
                                             "finding_source_snapshot_changed")
        connection = connect_existing_framework(selected, readonly=True)
        try:
            connection.execute("BEGIN")
            version = read_application_schema_version(connection, label="framework")
            if version != SCHEMA_VERSION:
                return FindingRecordPage((), "failed", snapshot_id, None, None,
                                                 "finding_owner_schema_incompatible")
            clauses = ["status=?"]
            parameters: list[object] = [status]
            for field, value in (("route_name", route_name), ("recommendation", recommendation)):
                if value is not None:
                    clauses.append(field + "=?")
                    parameters.append(value)
            where = " AND ".join(clauses)
            count = int(connection.execute("SELECT COUNT(*) FROM findings WHERE " + where,
                                           parameters).fetchone()[0])
            if cursor is not None:
                where += " AND (" + _RANK_SQL + ", -confidence,route_name COLLATE BINARY," \
                    "path COLLATE BINARY,reason_code COLLATE BINARY,volume_id,file_id) > (?,?,?,?,?,?,?)"
                parameters.extend((cursor.rank, -cursor.confidence, cursor.route_name, cursor.path,
                                   cursor.reason_code, cursor.volume_id, cursor.file_id))
            rows = connection.execute(
                "SELECT " + _FINDING_COLUMNS + "," + _RANK_SQL + " AS priority_rank"
                " FROM findings WHERE " + where + " ORDER BY priority_rank,confidence DESC,"
                "route_name COLLATE BINARY,path COLLATE BINARY,reason_code COLLATE BINARY,"
                "volume_id,file_id LIMIT ?", (*parameters, limit + 1),
            ).fetchall()
            items = tuple(_finding_record(row) for row in rows[:limit])
        finally:
            connection.close()
        if capture_sqlite_read_fence(selected) != fence:
            return FindingRecordPage((), "snapshot_changed", None, None, None,
                                             "finding_source_snapshot_changed")
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = FindingListCursor(
                snapshot_id, signature, int(last["priority_rank"]), float(last["confidence"]),
                str(last["route_name"]), str(last["path"]), str(last["reason_code"]),
                str(last["volume_id"]), str(last["file_id"]),
            )
        return FindingRecordPage(items, "ready", snapshot_id, count, next_cursor)
    except FileNotFoundError:
        return FindingRecordPage((), "absent", None, None, None, "finding_owner_absent")
    except (ImmutableSQLiteUnavailable, sqlite3.Error, RuntimeError, ValueError):
        return FindingRecordPage((), "failed", None, None, None, "finding_owner_read_failed")


__all__ = [
    "FindingListCursor",
    "FindingRecord",
    "FindingRecordPage",
    "finding_projection",
    "list_findings",
    "list_findings_page",
]
