"""Stable composition boundary for the workflow Review surfaces.

The Review implementation grew around several owner-specific modules.  This
small service keeps callers from having to know whether a read comes from the
candidate tables, the value-review projection, or the append-only
``ReviewTask`` repository.  It deliberately contains no state, opens no
database on import, and performs no corpus mutation.  The existing functions
remain the compatibility API; this class is an additive seam for new callers
and for the eventual extraction of readers, staging, and publication.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
import time
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .value_review_contracts import (
    ValueFileObservation,
    ValueProvenance,
    ValueReviewAvailability,
    ValueReviewPaths,
    ValueReviewQuery,
    ValueReviewReport,
)

if TYPE_CHECKING:
    from .review import (
        ReviewCandidateRecord,
        ReviewDecisionRecord,
        ReviewDecisionStatus,
        ReviewRecommendation,
        ReviewStatus,
    )
    from .review_task_contracts import (
        ReviewTaskEvent,
        ReviewTaskRecord,
    )
    from .review_task_query import ReviewTaskReadQuery, ReviewTaskReadResult
    from .value_review_tasks import (
        ClockNs,
        ValueReviewTaskQueue,
        ValueReviewTaskRefreshResult,
    )


CancellationCheck = Callable[[], None]


@runtime_checkable
class ReviewReader(Protocol):
    """Read-only portion of the Review service contract.

    Implementations must read published state only, use bounded queries, and
    preserve the owner/source fence exposed by the underlying contracts.  The
    protocol is intentionally small enough for a fixture or a future remote
    reader to implement without importing the repositories.
    """

    def list_review_candidates(
        self,
        database: str | Path,
        *,
        limit: int,
        route_name: str | None = None,
        recommendation: ReviewRecommendation | None = None,
        status: ReviewStatus = "open",
    ) -> list[ReviewCandidateRecord]: ...

    def list_review_decisions(
        self,
        database: str | Path,
        *,
        limit: int,
        route_name: str | None = None,
        reason_code: str | None = None,
        status: ReviewDecisionStatus | None = None,
        volume_id: int | None = None,
        file_id: int | None = None,
        candidate_generation: int | None = None,
    ) -> list[ReviewDecisionRecord]: ...

    def preview_value_review(
        self,
        paths: ValueReviewPaths,
        query: ValueReviewQuery,
    ) -> ValueReviewReport: ...

    def read_review_task(
        self,
        database: str | Path,
        task_id: str,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> ReviewTaskRecord | None: ...

    def query_current_review_tasks(
        self,
        database: str | Path,
        query: ReviewTaskReadQuery,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> ReviewTaskReadResult: ...


class ReviewService:
    """Thin, late-bound facade over the existing Review implementations.

    Imports of repository modules are intentionally local to each method.  A
    cold import therefore does not inspect SQLite, load optional providers, or
    make the package depend on a particular owner implementation.  Delegates
    retain their original signatures and exceptions, so legacy adapters do not
    need to change while callers migrate to this boundary.
    """

    def list_review_candidates(
        self,
        database: str | Path,
        *,
        limit: int,
        route_name: str | None = None,
        recommendation: ReviewRecommendation | None = None,
        status: ReviewStatus = "open",
    ) -> list[ReviewCandidateRecord]:
        from .review import list_review_candidates

        return list_review_candidates(
            database,
            limit=limit,
            route_name=route_name,
            recommendation=recommendation,
            status=status,
        )

    def get_review_candidate(
        self,
        database: str | Path,
        *,
        route_name: str,
        volume_id: int,
        file_id: int,
        reason_code: str,
    ) -> ReviewCandidateRecord | None:
        from .review import get_review_candidate

        return get_review_candidate(
            database,
            route_name=route_name,
            volume_id=volume_id,
            file_id=file_id,
            reason_code=reason_code,
        )

    def list_review_decisions(
        self,
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
        from .review import list_review_decisions

        return list_review_decisions(
            database,
            limit=limit,
            route_name=route_name,
            reason_code=reason_code,
            status=status,
            volume_id=volume_id,
            file_id=file_id,
            candidate_generation=candidate_generation,
        )

    def get_review_decision_by_key(
        self,
        database: str | Path,
        idempotency_key: str,
    ) -> ReviewDecisionRecord | None:
        from .review import get_review_decision_by_key

        return get_review_decision_by_key(database, idempotency_key)

    def preview_value_review(
        self,
        paths: ValueReviewPaths,
        query: ValueReviewQuery,
    ) -> ValueReviewReport:
        from .value_review import preview_value_review

        return preview_value_review(paths, query)

    def rank_value_observations(
        self,
        observations: Iterable[ValueFileObservation],
        query: ValueReviewQuery,
        *,
        availability: ValueReviewAvailability = ValueReviewAvailability.READY,
        complete: bool = True,
        reason: str | None = None,
        provenance: tuple[ValueProvenance, ...] = (),
        uncertainties: tuple[str, ...] = (),
    ) -> ValueReviewReport:
        from .value_review import rank_value_observations

        return rank_value_observations(
            observations,
            query,
            availability=availability,
            complete=complete,
            reason=reason,
            provenance=provenance,
            uncertainties=uncertainties,
        )

    def read_value_review_task_queue(
        self,
        database: Path,
        paths: ValueReviewPaths,
        *,
        scope: str,
        limit: int,
        reference_time_ns: int,
        cancellation_check: CancellationCheck | None = None,
    ) -> ValueReviewTaskQueue:
        from .value_review_tasks import read_value_review_task_queue

        return read_value_review_task_queue(
            database,
            paths,
            scope=scope,
            limit=limit,
            reference_time_ns=reference_time_ns,
            cancellation_check=cancellation_check,
        )

    def refresh_value_review_tasks(
        self,
        database: Path,
        paths: ValueReviewPaths,
        *,
        scope: str,
        clock_ns: ClockNs = time.time_ns,
        cancellation_check: CancellationCheck | None = None,
    ) -> ValueReviewTaskRefreshResult:
        """Advance one bounded advisory queue page through the legacy writer.

        This is the only mutating method on the facade, and it mutates Review
        state only: it does not touch the corpus or authorize organization
        actions.  Keeping it explicit makes the read/write boundary visible to
        future staging and CAS extraction work.
        """

        from .value_review_tasks import refresh_value_review_tasks

        return refresh_value_review_tasks(
            database,
            paths,
            scope=scope,
            clock_ns=clock_ns,
            cancellation_check=cancellation_check,
        )

    def read_review_task(
        self,
        database: str | Path,
        task_id: str,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> ReviewTaskRecord | None:
        from .review_task_repository import read_review_task

        return read_review_task(
            database,
            task_id,
            cancellation_check=cancellation_check,
        )

    def read_review_task_history(
        self,
        database: str | Path,
        task_id: str,
        *,
        limit: int = 100,
        cancellation_check: CancellationCheck | None = None,
    ) -> tuple[ReviewTaskEvent, ...]:
        from .review_task_repository import read_review_task_history

        return read_review_task_history(
            database,
            task_id,
            limit=limit,
            cancellation_check=cancellation_check,
        )

    def query_current_review_tasks(
        self,
        database: str | Path,
        query: ReviewTaskReadQuery,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> ReviewTaskReadResult:
        from .review_task_query import query_current_review_tasks

        return query_current_review_tasks(
            database, query, cancellation_check=cancellation_check,
        )

    def read_review_task_event_by_key(
        self,
        database: str | Path,
        event_key: str,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> ReviewTaskEvent | None:
        from .review_task_repository import read_review_task_event_by_key

        return read_review_task_event_by_key(
            database,
            event_key,
            cancellation_check=cancellation_check,
        )


__all__ = ("ReviewReader", "ReviewService")
