"""Snapshot-bound pagination of the existing legacy Review candidate owner."""

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

from .review import (
    REVIEW_RECOMMENDATIONS,
    REVIEW_STATUSES,
    ReviewCandidateRecord,
    ReviewRecommendation,
    ReviewStatus,
    _REVIEW_CANDIDATE_COLUMNS,
    _review_candidate_record,
)


_RANK_SQL = """CASE recommendation WHEN 'deletion_candidate' THEN 0
WHEN 'manual_review' THEN 1 WHEN 'keep_protected' THEN 2 ELSE 3 END"""


def _digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def review_candidate_projection(
    record: ReviewCandidateRecord, *, snapshot_id: str,
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
class ReviewCandidateListCursor:
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
    def from_token(cls, token: str) -> ReviewCandidateListCursor:
        if not isinstance(token, str) or not 1 <= len(token) <= 65536:
            raise ValueError("review cursor token must be bounded non-empty text")
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
            raise ValueError("invalid review cursor token") from exc


@dataclass(frozen=True, slots=True)
class ReviewCandidateRecordPage:
    items: tuple[ReviewCandidateRecord, ...]
    availability: Literal["ready", "absent", "failed", "snapshot_changed"]
    snapshot_id: str | None
    total_matching: int | None
    next_cursor: ReviewCandidateListCursor | None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or len(self.items) > 10000 or any(
            not isinstance(item, ReviewCandidateRecord) for item in self.items
        ):
            raise ValueError("items must be a bounded typed immutable tuple")
        if self.availability not in {"ready", "absent", "failed", "snapshot_changed"}:
            raise ValueError("unsupported review page availability")
        if self.next_cursor is not None and not isinstance(self.next_cursor, ReviewCandidateListCursor):
            raise ValueError("next_cursor must be a typed review cursor")
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
            "schema": "neocortex.review-candidates-page/v1",
            "items": [review_candidate_projection(item, snapshot_id=self.snapshot_id or "unavailable")
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
            "scope": "legacy_findings_matching_filters",
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
    status: ReviewStatus = "open",
    after: ReviewCandidateListCursor | str | None = None,
) -> ReviewCandidateRecordPage:
    """Count and page one immutable logical view, never mask absence as zero."""

    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError("review limit must be between 1 and 10000")
    if recommendation is not None and recommendation not in REVIEW_RECOMMENDATIONS:
        raise ValueError("invalid review recommendation")
    if status not in REVIEW_STATUSES:
        raise ValueError("invalid review status")
    if route_name is not None and (
        not isinstance(route_name, str) or not route_name.strip() or len(route_name) > 256
    ):
        raise ValueError("route_name must be bounded non-blank text")
    cursor = ReviewCandidateListCursor.from_token(after) if isinstance(after, str) else after
    if cursor is not None and not isinstance(cursor, ReviewCandidateListCursor):
        raise ValueError("after must be a ReviewCandidateListCursor or its token")
    signature = _digest({"route_name": route_name, "recommendation": recommendation, "status": status})
    if cursor is not None and cursor.query_signature != signature:
        raise ValueError("review cursor belongs to different query filters")
    selected = Path(database)
    try:
        fence = capture_sqlite_read_fence(selected)
        snapshot_id = _digest(asdict(fence))
        if cursor is not None and cursor.snapshot_id != snapshot_id:
            return ReviewCandidateRecordPage((), "snapshot_changed", snapshot_id, None, None,
                                             "review_source_snapshot_changed")
        connection = connect_existing_framework(selected, readonly=True)
        try:
            connection.execute("BEGIN")
            version = read_application_schema_version(connection, label="framework")
            if version != SCHEMA_VERSION:
                return ReviewCandidateRecordPage((), "failed", snapshot_id, None, None,
                                                 "review_owner_schema_incompatible")
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
                "SELECT " + _REVIEW_CANDIDATE_COLUMNS + "," + _RANK_SQL + " AS priority_rank"
                " FROM findings WHERE " + where + " ORDER BY priority_rank,confidence DESC,"
                "route_name COLLATE BINARY,path COLLATE BINARY,reason_code COLLATE BINARY,"
                "volume_id,file_id LIMIT ?", (*parameters, limit + 1),
            ).fetchall()
            items = tuple(_review_candidate_record(row) for row in rows[:limit])
        finally:
            connection.close()
        if capture_sqlite_read_fence(selected) != fence:
            return ReviewCandidateRecordPage((), "snapshot_changed", None, None, None,
                                             "review_source_snapshot_changed")
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = ReviewCandidateListCursor(
                snapshot_id, signature, int(last["priority_rank"]), float(last["confidence"]),
                str(last["route_name"]), str(last["path"]), str(last["reason_code"]),
                str(last["volume_id"]), str(last["file_id"]),
            )
        return ReviewCandidateRecordPage(items, "ready", snapshot_id, count, next_cursor)
    except FileNotFoundError:
        return ReviewCandidateRecordPage((), "absent", None, None, None, "review_owner_absent")
    except (ImmutableSQLiteUnavailable, sqlite3.Error, RuntimeError, ValueError):
        return ReviewCandidateRecordPage((), "failed", None, None, None, "review_owner_read_failed")


__all__ = [
    "ReviewCandidateListCursor", "ReviewCandidateRecordPage", "list_findings_page",
    "review_candidate_projection",
]
