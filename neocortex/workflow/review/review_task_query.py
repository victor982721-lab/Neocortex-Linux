"""Typed, bounded queries over the existing published ReviewTask repository."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path

from neocortex.persistence.sqlite_immutable import capture_sqlite_read_fence

from .review_task_contracts import (
    MAX_REVIEW_TASK_READ_PAGE,
    ReviewTaskListCursor,
    ReviewTaskRecordPage,
    ReviewTaskState,
)
from .review_relationships import ReviewRelationshipResolution, resolve_review_relationships
from .review_task_grouping import group_review_task_page


@dataclass(frozen=True, slots=True)
class ReviewTaskReadQuery:
    limit: int = 20
    scope: str | None = None
    task_type: str | None = None
    states: tuple[ReviewTaskState, ...] = (ReviewTaskState.OPEN, ReviewTaskState.IN_REVIEW)
    after: ReviewTaskListCursor | None = None

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not (
            1 <= self.limit <= MAX_REVIEW_TASK_READ_PAGE
        ):
            raise ValueError(f"limit must be between 1 and {MAX_REVIEW_TASK_READ_PAGE}")
        for label, value in (("scope", self.scope), ("task_type", self.task_type)):
            if value is not None and (
                not isinstance(value, str) or not value.strip() or len(value) > 128
            ):
                raise ValueError(f"{label} must be bounded non-blank text")
        if not isinstance(self.states, tuple) or not self.states or any(
            not isinstance(item, ReviewTaskState) for item in self.states
        ) or len(set(self.states)) != len(self.states):
            raise ValueError("states must be unique typed values in an immutable tuple")
        if self.after is not None and not isinstance(self.after, ReviewTaskListCursor):
            raise ValueError("after must be a ReviewTaskListCursor")


def review_task_cursor_payload(cursor: ReviewTaskListCursor | None) -> dict[str, object] | None:
    if cursor is None:
        return None
    return {"priority": cursor.priority, "created_ns": cursor.created_ns, "task_id": cursor.task_id}


@dataclass(frozen=True, slots=True)
class ReviewTaskReadResult:
    query: ReviewTaskReadQuery
    page: ReviewTaskRecordPage
    relationship_resolution: ReviewRelationshipResolution | None = None
    review_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.query, ReviewTaskReadQuery) or not isinstance(
            self.page, ReviewTaskRecordPage
        ):
            raise ValueError("read result requires a typed query and page")
        if len(self.page.items) > self.query.limit:
            raise ValueError("page exceeds the query limit")
        if self.relationship_resolution is not None and not isinstance(self.relationship_resolution, ReviewRelationshipResolution):
            raise ValueError("relationship_resolution must be typed")

    @property
    def total_matching(self) -> int | None:
        # A suffix page, including an empty suffix, never proves an empty queue.
        incomplete_source = self.relationship_resolution is not None and any(
            reason in {"archive_review_source_staged", "archive_review_source_publication_missing",
                       "archive_relationship_source_stale", "archive_relationship_source_unavailable",
                       "review_task_snapshot_changed"}
            for _, reason in self.relationship_resolution.unresolved
        )
        if self.query.after is not None or self.page.has_more or incomplete_source:
            return None
        return len(self.page.items)

    def to_dict(self) -> dict[str, object]:
        items: list[dict[str, object]] = []
        unresolved = {} if self.relationship_resolution is None else dict(self.relationship_resolution.unresolved)
        verified = () if self.relationship_resolution is None else self.relationship_resolution.verified_task_ids
        for record in self.page.items:
            items.append({
                "task": record.task.to_dict(),
                "source": record.source.to_dict(),
                "state": record.state.value,
                "current_event": record.current_event.to_dict(),
                "source_snapshot_fingerprint": record.source_snapshot_fingerprint,
                "batch_id": record.batch_id,
                "selector_signature": record.selector_signature,
                "score_semantics": "review_priority_not_probability_or_authority",
                "relationship_evidence_state": (
                    "staged" if unresolved.get(record.task.task_id) == "archive_review_source_staged"
                    else "stale" if unresolved.get(record.task.task_id) in {
                        "archive_relationship_source_stale", "review_task_snapshot_changed",
                    }
                    else "published_verified" if record.task.task_id in verified
                    else "not_verified"
                ),
            })
        states = Counter(item.state.value for item in self.page.items)
        reasons = Counter(item.task.reason_code for item in self.page.items)
        resolution = self.relationship_resolution or ReviewRelationshipResolution(unresolved=tuple(
            (record.task.task_id, "relationship_source_not_queried") for record in self.page.items
        ))
        groups = group_review_task_page(self.page, relationships=resolution.relationships,
                                        complete_scope=self.total_matching is not None)
        coverage_reason = None
        if self.total_matching is None:
            coverage_reason = (
                "bounded_page_not_total_queue" if self.query.after is not None or self.page.has_more
                else next(iter(sorted(set(unresolved.values()))), "source_coverage_unavailable")
            )
        return {
            "schema": "neocortex.review-task-query/v2",
            "review_snapshot_id": self.review_snapshot_id,
            "read_only": True,
            "advisory_only": True,
            "mutation_authorized": False,
            "items": items,
            "groups": [group.to_dict() for group in groups],
            "relationship_coverage": resolution.to_dict(),
            "counts": {
                "returned": len(self.page.items),
                "total_matching": self.total_matching,
                "total_exact": self.total_matching is not None,
                "by_state_in_page": dict(sorted(states.items())),
                "by_reason_in_page": dict(sorted(reasons.items())),
                "scope": "typed_tasks_matching_query",
                "groups_in_page": len(groups),
                "grouped_members_in_page": sum(len(group.members) for group in groups if len(group.members) > 1),
                "singleton_members_in_page": sum(len(group.members) for group in groups if len(group.members) == 1),
            },
            "query": {
                "scope": self.query.scope,
                "task_type": self.query.task_type,
                "states": [state.value for state in self.query.states],
                "limit": self.query.limit,
                "after": review_task_cursor_payload(self.query.after),
            },
            "has_more": self.page.has_more,
            "next_cursor": review_task_cursor_payload(self.page.next_cursor),
            "complete": self.total_matching is not None,
            "coverage_reason": coverage_reason,
        }


def query_current_review_tasks(
    database: str | Path,
    query: ReviewTaskReadQuery,
    *,
    cancellation_check: Callable[[], None] | None = None,
    archive_path: Path | None = None,
) -> ReviewTaskReadResult:
    """Read typed tasks independently of the legacy candidate queue's size."""

    if not isinstance(query, ReviewTaskReadQuery):
        raise TypeError("query must be a ReviewTaskReadQuery")
    from .review_task_repository import list_current_review_tasks

    before = capture_sqlite_read_fence(Path(database))
    page = list_current_review_tasks(
            database,
            limit=query.limit,
            scope=query.scope,
            task_type=query.task_type,
            states=query.states,
            after=query.after,
            cancellation_check=cancellation_check,
        )
    resolution = resolve_review_relationships(
        Path(database).parent / "archive.sqlite3" if archive_path is None else archive_path, page,
        database=Path(database),
    )
    if capture_sqlite_read_fence(Path(database)) != before:
        resolution = ReviewRelationshipResolution(unresolved=tuple(
            (record.task.task_id, "review_task_snapshot_changed") for record in page.items
        ))
    snapshot_id = "sha256:" + hashlib.sha256(json.dumps(asdict(before), sort_keys=True,
                                                         separators=(",", ":")).encode()).hexdigest()
    return ReviewTaskReadResult(query, page, resolution, snapshot_id)


__all__ = [
    "ReviewTaskReadQuery", "ReviewTaskReadResult", "query_current_review_tasks",
    "review_task_cursor_payload",
]
