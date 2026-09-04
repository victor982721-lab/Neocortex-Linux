"""Declared core port for the conservative public value-review adapter."""

from __future__ import annotations
from collections.abc import Callable
from pathlib import Path
import time

from neocortex.api.status_codes import KnowledgeExitCode
from neocortex.workflow.review.value_review_contracts import (
    ValueReviewAvailability,
    ValueReviewPaths,
    ValueReviewQuery,
    ValueReviewReport,
)
from neocortex.workflow.review.value_review_tasks import (
    ValueReviewTaskRefreshResult,
    ValueReviewTaskQueue,
    ValueReviewTaskQueueStatus,
)
from neocortex.workflow.review.review_task_contracts import (
    CanonicalJsonObject,
    ReviewTaskActorKind,
    ReviewTaskEvent,
    ReviewTaskRecord,
    ReviewTaskState,
    ReviewTaskTransition,
)
from neocortex.workflow.review.review_task_repository import (
    ReviewTaskCASConflict,
    append_review_task_event as _append_review_task_event,
)
from neocortex.workflow.review.review_service import ReviewService


_DEFAULT_REVIEW_SERVICE = ReviewService()


def preview_value_review(
    paths: ValueReviewPaths,
    query: ValueReviewQuery,
) -> ValueReviewReport:
    """Compatibility delegate routed through the Review service boundary."""

    return _DEFAULT_REVIEW_SERVICE.preview_value_review(paths, query)


def read_value_review_task_queue(
    database: Path,
    paths: ValueReviewPaths,
    *,
    scope: str,
    limit: int,
    reference_time_ns: int,
    cancellation_check: Callable[[], None] | None = None,
) -> ValueReviewTaskQueue:
    """Compatibility delegate routed through the Review service boundary."""

    return _DEFAULT_REVIEW_SERVICE.read_value_review_task_queue(
        database,
        paths,
        scope=scope,
        limit=limit,
        reference_time_ns=reference_time_ns,
        cancellation_check=cancellation_check,
    )


def refresh_value_review_tasks(
    database: Path,
    paths: ValueReviewPaths,
    *,
    scope: str,
    clock_ns: Callable[[], int] = time.time_ns,
    cancellation_check: Callable[[], None] | None = None,
) -> ValueReviewTaskRefreshResult:
    """Compatibility delegate routed through the Review service boundary."""

    return _DEFAULT_REVIEW_SERVICE.refresh_value_review_tasks(
        database,
        paths,
        scope=scope,
        clock_ns=clock_ns,
        cancellation_check=cancellation_check,
    )


def read_review_task(
    database: str | Path,
    task_id: str,
    *,
    cancellation_check: Callable[[], None] | None = None,
) -> ReviewTaskRecord | None:
    """Compatibility delegate routed through the Review service boundary."""

    return _DEFAULT_REVIEW_SERVICE.read_review_task(
        database,
        task_id,
        cancellation_check=cancellation_check,
    )


def read_review_task_event_by_key(
    database: str | Path,
    event_key: str,
    *,
    cancellation_check: Callable[[], None] | None = None,
) -> ReviewTaskEvent | None:
    """Compatibility delegate routed through the Review service boundary."""

    return _DEFAULT_REVIEW_SERVICE.read_review_task_event_by_key(
        database,
        event_key,
        cancellation_check=cancellation_check,
    )


def read_review_task_history(
    database: str | Path,
    task_id: str,
    *,
    limit: int = 100,
    cancellation_check: Callable[[], None] | None = None,
) -> tuple[ReviewTaskEvent, ...]:
    """Compatibility delegate routed through the Review service boundary."""

    return _DEFAULT_REVIEW_SERVICE.read_review_task_history(
        database,
        task_id,
        limit=limit,
        cancellation_check=cancellation_check,
    )


append_review_task_event = _append_review_task_event


__all__ = (
    "CanonicalJsonObject",
    "KnowledgeExitCode",
    "ReviewTaskActorKind",
    "ReviewTaskCASConflict",
    "ReviewTaskRecord",
    "ReviewTaskState",
    "ReviewTaskTransition",
    "ValueReviewAvailability",
    "ValueReviewPaths",
    "ValueReviewQuery",
    "ValueReviewReport",
    "ValueReviewTaskQueueStatus",
    "append_review_task_event",
    "preview_value_review",
    "read_review_task",
    "read_review_task_event_by_key",
    "read_review_task_history",
    "read_value_review_task_queue",
    "refresh_value_review_tasks",
)
