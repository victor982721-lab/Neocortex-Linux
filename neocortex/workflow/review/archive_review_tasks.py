"""Archive issues in the existing explicit Review refresh/publication lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3

from .review_relationships import (
    ARCHIVE_REVIEW_PRODUCER, ARCHIVE_REVIEW_TASK_TYPE, ArchiveReviewIssueSource,
    ArchiveReviewSource, read_archive_review_source,
)
from .review_task_contracts import (
    CanonicalJsonObject, ReviewTaskCoverage, ReviewTaskDraft, ReviewTaskPublication,
    ReviewTaskPublicationResult, ReviewTaskSourceFence, ReviewTaskState,
)


@dataclass(frozen=True, slots=True)
class ArchiveReviewRefreshResult:
    status: str
    reason: str | None = None
    publication: ReviewTaskPublicationResult | None = None
    wrote_state: bool = False
    source_snapshot_fingerprint: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"owner": "archive", "task_type": ARCHIVE_REVIEW_TASK_TYPE,
                "status": self.status, "reason": self.reason, "wrote_state": self.wrote_state,
                "source_snapshot_fingerprint": self.source_snapshot_fingerprint,
                "batch_id": None if self.publication is None else self.publication.batch_id,
                "task_ids": [] if self.publication is None else list(self.publication.task_ids),
                "scanned_count": None if self.publication is None else self.publication.progress.scanned_count,
                "selected_count": None if self.publication is None else self.publication.progress.selected_count,
                "complete": self.status == "complete", "advisory_only": True,
                "mutation_authorized": False}


def _key(scope: str, issue: ArchiveReviewIssueSource) -> str:
    values = issue.projection.to_dict()
    # Owner keys/explicit member chain, not a filename or parsed virtual path.
    raw = CanonicalJsonObject.from_mapping({"scope": scope, "container_key": values["container_key"],
                                          "member_chain": values["issue_member_chain"],
                                          "reason": issue.reason_code}).payload_json
    return "archive-review:" + hashlib.sha256(raw.encode()).hexdigest()


def _draft(
    issue: ArchiveReviewIssueSource, fence: ReviewTaskSourceFence, *, version: int,
    supersedes: str | None, now_ns: int,
) -> ReviewTaskDraft:
    logical_key = _key(fence.scope, issue)
    task_digest = hashlib.sha256((logical_key + issue.source.fingerprint +
                                  fence.source_snapshot_fingerprint + str(version)).encode()).hexdigest()
    return ReviewTaskDraft(
        task_id="review-task:archive:" + task_digest, logical_key=logical_key, task_version=version,
        task_type=ARCHIVE_REVIEW_TASK_TYPE, scope=fence.scope, source_kind="archive",
        source_input_id=issue.source.input_id, snapshot=issue.projection, evidence=issue.evidence,
        reason_code=issue.reason_code,
        uncertainty_detail=CanonicalJsonObject.from_mapping({
            "not_a_probability": True, "risk_score_not_assessed": True,
            "relationship_proved": issue.relationship_id is not None,
            "source": "published_archive_issue", "mutation_authorized": False,
        }),
        impact=0.0, uncertainty=1.0, irreversibility=0.0,
        suggestions=("inspect_published_archive_issue",), supersedes_task_id=supersedes, created_ns=now_ns,
    )


def _refresh_archive_review_tasks(
    database: Path, archive: Path, *, scope: str, now_ns: int,
    cancellation_check: Callable[[], None] | None = None,
) -> ArchiveReviewRefreshResult:
    """Advance one actual-issue page through the standard Framework publisher.

    This helper is invoked by the existing explicit Value/Review refresh; normal
    queries never call it. It does not generate findings for unflagged components.
    """

    from .review_task_repository import (
        ReviewTaskCASConflict, lookup_review_task_version_heads, publish_review_task_page,
        read_current_review_task_source_publication, read_review_task_progress,
    )
    from .value_review_tasks import _read_framework_version, _terminal_scope_expired
    from neocortex.persistence.framework_schema import SCHEMA_VERSION
    from neocortex.persistence.framework_state_writer import FrameworkState
    from neocortex.runtime.control.locking import FrameworkRunLock

    def checkpoint() -> None:
        if cancellation_check is not None:
            cancellation_check()

    checkpoint()
    try:
        initial = read_archive_review_source(archive, issue_ids=())
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return ArchiveReviewRefreshResult("unavailable", "archive_review_source_unavailable")
    fence = initial.fence(scope)
    version = _read_framework_version(database)
    if version is not None and version > SCHEMA_VERSION:
        return ArchiveReviewRefreshResult("unavailable", "framework_state_future")
    progress = None if version != SCHEMA_VERSION else read_review_task_progress(database, fence,
                                                                                 cancellation_check=cancellation_check)
    if progress is not None and progress.complete:
        if read_current_review_task_source_publication(database, fence,
                                                       cancellation_check=cancellation_check) is None:
            return ArchiveReviewRefreshResult("unavailable", "archive_review_source_publication_missing")
        return ArchiveReviewRefreshResult("complete", source_snapshot_fingerprint=fence.source_snapshot_fingerprint)
    after = 0
    if progress is not None and progress.cursor is not None:
        payload = progress.cursor.to_dict()
        if set(payload) != {"archive_issue_id"} or type(payload["archive_issue_id"]) is not int:
            return ArchiveReviewRefreshResult("unavailable", "archive_review_cursor_invalid")
        after = int(payload["archive_issue_id"])
    try:
        source = read_archive_review_source(archive, after_issue_id=after)
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return ArchiveReviewRefreshResult("unavailable", "archive_review_source_unavailable")
    if source.owner_snapshot_id != initial.owner_snapshot_id:
        return ArchiveReviewRefreshResult("snapshot_changed", "archive_review_source_changed")
    checkpoint()
    with FrameworkRunLock(database.parent / "framework.lock"):
        try:
            current = read_archive_review_source(archive, issue_ids=())
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            return ArchiveReviewRefreshResult("snapshot_changed", "archive_review_source_unavailable")
        if current.owner_snapshot_id != source.owner_snapshot_id:
            return ArchiveReviewRefreshResult("snapshot_changed", "archive_review_source_changed")
        with FrameworkState(database):
            pass
        primary = tuple(item for item in source.issues if item.projection.to_dict()["primary_issue"] == 1)
        heads = lookup_review_task_version_heads(database, tuple(_key(scope, item) for item in primary),
                                                  scope=scope, task_type=ARCHIVE_REVIEW_TASK_TYPE,
                                                  cancellation_check=cancellation_check)
        previous = {head.logical_key: head for head in heads}
        tasks: list[ReviewTaskDraft] = []
        for issue in primary:
            head = previous.get(_key(scope, issue))
            if head is not None and head.state in {ReviewTaskState.RESOLVED, ReviewTaskState.DISMISSED} and not (
                _terminal_scope_expired(head, source=issue.source, fence=fence)
            ):
                continue
            tasks.append(_draft(issue, fence, version=1 if head is None else head.task_version + 1,
                                supersedes=None if head is None else head.task_id, now_ns=now_ns))
        publication = _publication(source, fence, tuple(tasks), after=after, now_ns=now_ns)
        try:
            result = publish_review_task_page(database, publication,
                                              expected_progress_revision=None if progress is None else progress.revision,
                                              cancellation_check=cancellation_check)
        except ReviewTaskCASConflict:
            return ArchiveReviewRefreshResult("snapshot_changed", "archive_review_progress_changed")
    return ArchiveReviewRefreshResult("complete" if result.progress.complete else "partial", publication=result,
                                      wrote_state=not result.idempotent,
                                      source_snapshot_fingerprint=fence.source_snapshot_fingerprint)


def _publication(
    source: ArchiveReviewSource, fence: ReviewTaskSourceFence, tasks: tuple[ReviewTaskDraft, ...],
    *, after: int, now_ns: int,
) -> ReviewTaskPublication:
    page_id = hashlib.sha256((fence.source_snapshot_fingerprint + str(after)).encode()).hexdigest()
    return ReviewTaskPublication(
        batch_id="review-archive-batch:" + page_id, batch_key="review-archive-page:" + page_id,
        fence=fence,
        cursor_before=None if after == 0 else CanonicalJsonObject.from_mapping({"archive_issue_id": after}),
        cursor_after=None if not source.has_more else CanonicalJsonObject.from_mapping({
            "archive_issue_id": source.issues[-1].issue_id,
        }),
        inputs=tuple(issue.source for issue in source.issues), tasks=tasks,
        coverage=ReviewTaskCoverage.PARTIAL if source.has_more else ReviewTaskCoverage.COMPLETE,
        producer_signature=ARCHIVE_REVIEW_PRODUCER, confirmed_ns=now_ns,
    )


__all__ = ["ArchiveReviewRefreshResult"]
