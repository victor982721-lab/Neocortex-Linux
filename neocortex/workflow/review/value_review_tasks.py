"""Durable, bounded ReviewTask projection for conservative Value findings.

The Inventory/Catalog owners remain the source of truth.  An explicit refresh
reads one immutable keyset page, then publishes its receipt and review tasks in
one Framework-owner transaction.  Normal queue reads never create or migrate
state and no task authorizes a physical mutation.
"""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import hashlib
import json
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum, StrEnum
from pathlib import Path
from typing import cast

from neocortex.sqlite_schema_contract import (
    SQLiteSchemaContractError,
    read_application_schema_version,
)

from neocortex.persistence.framework_schema import SCHEMA_VERSION as FRAMEWORK_SCHEMA_VERSION
from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod,
    EvidenceRef,
    PhysicalIdentityRef,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from neocortex.workflow.review.review_task_contracts import (
    CanonicalJsonObject,
    ReviewTaskCoverage,
    ReviewTaskDraft,
    ReviewTaskInput,
    ReviewTaskPublication,
    ReviewTaskPublicationResult,
    ReviewTaskRecord,
    ReviewTaskScanProgress,
    ReviewTaskSourceFence,
    ReviewTaskState,
    ReviewTaskVersionHead,
)
from neocortex.persistence.sqlite_paths import readonly_sqlite_uri
from neocortex.workflow.review.value_review import rank_value_observations
from neocortex.workflow.review.value_review_contracts import (
    VALUE_REVIEW_CONTRACT_VERSION,
    ValueFileObservation,
    ValueReviewAvailability,
    ValueReviewItem,
    ValueReviewPaths,
    ValueReviewQuery,
    ValueReviewState,
)
from neocortex.workflow.review.value_review_repository import (
    ValueObservationLoad,
    ValueReviewPageCursor,
    load_value_review_observation_page,
    read_value_review_source_snapshot,
)


VALUE_REVIEW_TASK_TYPE = "value-review"
VALUE_REVIEW_TASK_PRODUCER_SIGNATURE = "neocortex.value-review.tasks/v1"
VALUE_REVIEW_TASK_SELECTOR_VERSION = 1
VALUE_REVIEW_TASK_PAGE_SIZE = 100
_DAY_NS = 86_400_000_000_000
_ACTIONABLE_STATES = frozenset(
    {
        ValueReviewState.EXACT_DUPLICATE_CANDIDATE,
        ValueReviewState.REVIEW_LOW_VALUE,
        ValueReviewState.ARCHIVE_CANDIDATE,
    }
)

CancellationCheck = Callable[[], None]
ClockNs = Callable[[], int]


class ValueReviewTaskQueueStatus(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    STALE = "stale"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"


def _reference_day_ns(value: int) -> int:
    return (value // _DAY_NS) * _DAY_NS


def _fence_reference_day_ns(fence: ReviewTaskSourceFence) -> int:
    value = fence.source_snapshot.to_dict().get("reference_day_ns")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueReviewTaskStateError("ReviewTask fence lacks a valid evaluation epoch")
    return value


def _owner_snapshot_without_epoch(fence: ReviewTaskSourceFence) -> CanonicalJsonObject:
    payload = fence.source_snapshot.to_dict()
    payload.pop("reference_day_ns", None)
    return CanonicalJsonObject.from_mapping(payload)


@dataclass(frozen=True, slots=True)
class ValueReviewTaskQueue:
    status: ValueReviewTaskQueueStatus
    reason: str | None
    fence: ReviewTaskSourceFence | None
    progress: ReviewTaskScanProgress | None
    records: tuple[ReviewTaskRecord, ...]
    has_more: bool

    @property
    def complete(self) -> bool:
        return (
            self.status is ValueReviewTaskQueueStatus.READY
            and self.progress is not None
            and self.progress.complete
            and not self.has_more
        )

    def report_dict(self) -> dict[str, object]:
        items = [_value_task_item(record) for record in self.records]
        selected_count = 0 if self.progress is None else self.progress.selected_count
        scanned_count = 0 if self.progress is None else self.progress.scanned_count
        effective_evidence_complete = (
            self.progress is not None
            and self.progress.evidence_complete
            and self.status is ValueReviewTaskQueueStatus.READY
        )
        effective_evidence_reason = (
            None
            if effective_evidence_complete
            else (None if self.progress is None else self.progress.evidence_reason or self.reason)
        )
        current_count = len(items) if not self.has_more else max(len(items) + 1, selected_count)
        availability = {
            ValueReviewTaskQueueStatus.READY: ValueReviewAvailability.READY.value,
            ValueReviewTaskQueueStatus.PARTIAL: ValueReviewAvailability.PARTIAL.value,
            ValueReviewTaskQueueStatus.STALE: ValueReviewAvailability.PARTIAL.value,
            ValueReviewTaskQueueStatus.ABSENT: ValueReviewAvailability.UNAVAILABLE.value,
            ValueReviewTaskQueueStatus.UNAVAILABLE: ValueReviewAvailability.UNAVAILABLE.value,
        }[self.status]
        return {
            "advisory_only": True,
            "availability": availability,
            "candidate_count": scanned_count,
            "complete": self.complete,
            "contract_version": VALUE_REVIEW_CONTRACT_VERSION,
            "items": items,
            "matched_count": current_count,
            "mutation_authorized": False,
            "operation": "value-preview",
            "provenance": [],
            "queue": {
                "coverage": (
                    None
                    if self.progress is None
                    else "complete"
                    if self.progress.complete
                    else "partial"
                ),
                "has_more": self.has_more,
                "current_count_exact": not self.has_more,
                "progress_revision": (None if self.progress is None else self.progress.revision),
                "scan_complete": (False if self.progress is None else self.progress.complete),
                "evidence_complete": effective_evidence_complete,
                "evidence_reason": effective_evidence_reason,
                "selected_in_scan": selected_count,
                "source_snapshot_fingerprint": (
                    None if self.fence is None else self.fence.source_snapshot_fingerprint
                ),
                "status": self.status.value,
            },
            "reason": self.reason,
            "returned_count": len(items),
            "truncated": self.has_more,
            "uncertainties": ([] if self.reason is None else [self.reason]),
        }


@dataclass(frozen=True, slots=True)
class ValueReviewTaskRefreshResult:
    status: str
    reason: str | None
    fence: ReviewTaskSourceFence | None
    page_report: dict[str, object] | None
    publication: ReviewTaskPublicationResult | None
    wrote_state: bool

    def to_dict(self) -> dict[str, object]:
        progress = None if self.publication is None else self.publication.progress
        return {
            "kind": "neocortex_value_review_task_refresh",
            "schema_version": 1,
            "status": self.status,
            "reason": self.reason,
            "wrote_state": self.wrote_state,
            "source_snapshot_fingerprint": (
                None if self.fence is None else self.fence.source_snapshot_fingerprint
            ),
            "batch_id": None if self.publication is None else self.publication.batch_id,
            "task_ids": ([] if self.publication is None else list(self.publication.task_ids)),
            "idempotent": (None if self.publication is None else self.publication.idempotent),
            "progress": (
                None
                if progress is None
                else {
                    "complete": progress.complete,
                    "evidence_complete": progress.evidence_complete,
                    "evidence_reason": progress.evidence_reason,
                    "revision": progress.revision,
                    "scanned_count": progress.scanned_count,
                    "selected_count": progress.selected_count,
                }
            ),
            "page_report": self.page_report,
        }


class ValueReviewTaskStateError(RuntimeError):
    """The ReviewTask owner or a source fence cannot be trusted."""


def build_value_review_task_fence(
    paths: ValueReviewPaths,
    *,
    scope: str,
    reference_time_ns: int,
) -> tuple[ReviewTaskSourceFence | None, str | None]:
    if (
        isinstance(reference_time_ns, bool)
        or not isinstance(reference_time_ns, int)
        or reference_time_ns < 0
    ):
        raise ValueError("reference_time_ns must be a non-negative integer")
    source = read_value_review_source_snapshot(paths)
    if source.availability is ValueReviewAvailability.UNAVAILABLE:
        return None, source.reason or "value_review_source_unavailable"
    source_payload = source.to_dict()
    source_payload["reference_day_ns"] = _reference_day_ns(reference_time_ns)
    selector_payload = {
        "policy": "conservative-value-review-v1",
        "scope": scope,
        "selector_version": VALUE_REVIEW_TASK_SELECTOR_VERSION,
    }
    selector_signature = "value-review-selector-v1:sha256:" + _sha256_json(selector_payload)
    return (
        ReviewTaskSourceFence.create(
            scope=scope,
            task_type=VALUE_REVIEW_TASK_TYPE,
            selector_signature=selector_signature,
            source_snapshot=source_payload,
        ),
        source.reason,
    )


def read_value_review_task_queue(
    database: Path,
    paths: ValueReviewPaths,
    *,
    scope: str,
    limit: int,
    reference_time_ns: int,
    cancellation_check: CancellationCheck | None = None,
) -> ValueReviewTaskQueue:
    """Read only the current source-fenced queue; never create or migrate it."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    _checkpoint(cancellation_check)
    version = _read_framework_version(database)
    if version is None or version < FRAMEWORK_SCHEMA_VERSION:
        return ValueReviewTaskQueue(
            ValueReviewTaskQueueStatus.ABSENT,
            "review_task_queue_absent",
            None,
            None,
            (),
            False,
        )
    if version > FRAMEWORK_SCHEMA_VERSION:
        return ValueReviewTaskQueue(
            ValueReviewTaskQueueStatus.UNAVAILABLE,
            "framework_state_future",
            None,
            None,
            (),
            False,
        )
    requested_fence, source_reason = build_value_review_task_fence(
        paths,
        scope=scope,
        reference_time_ns=reference_time_ns,
    )
    if requested_fence is None:
        return ValueReviewTaskQueue(
            ValueReviewTaskQueueStatus.UNAVAILABLE,
            source_reason,
            None,
            None,
            (),
            False,
        )
    from neocortex.workflow.review.review_task_repository import (
        has_review_task_scan_history,
        list_current_review_tasks,
        read_latest_complete_review_task_progress,
        read_review_task_progress,
    )

    progress = read_review_task_progress(
        database,
        requested_fence,
        cancellation_check=cancellation_check,
    )
    fence = requested_fence
    if progress is None or not progress.complete:
        from neocortex.workflow.review.review_task_repository import find_review_task_scan_progress

        incomplete, complete = find_review_task_scan_progress(
            database,
            scope=scope,
            task_type=VALUE_REVIEW_TASK_TYPE,
            selector_signature=requested_fence.selector_signature,
            owner_source_snapshot=_owner_snapshot_without_epoch(requested_fence),
            cancellation_check=cancellation_check,
        )
        # A committed complete epoch remains the public queue head while the
        # next evaluation epoch is still building.  Refresh resumes the
        # incomplete epoch; read-only consumers never observe its partial page
        # as if it replaced the last complete truth.
        selected = (
            complete
            or read_latest_complete_review_task_progress(
                database,
                scope=scope,
                task_type=VALUE_REVIEW_TASK_TYPE,
                selector_signature=requested_fence.selector_signature,
                cancellation_check=cancellation_check,
            )
            or incomplete
        )
        if selected is not None:
            progress = selected
            fence = selected.fence
    if progress is None:
        historical_scan = has_review_task_scan_history(
            database,
            scope=scope,
            task_type=VALUE_REVIEW_TASK_TYPE,
            selector_signature=fence.selector_signature,
            cancellation_check=cancellation_check,
        )
        if historical_scan:
            return ValueReviewTaskQueue(
                ValueReviewTaskQueueStatus.STALE,
                "review_task_source_stale",
                fence,
                None,
                (),
                False,
            )
        stale = list_current_review_tasks(
            database,
            limit=1,
            scope=scope,
            task_type=VALUE_REVIEW_TASK_TYPE,
            states=tuple(ReviewTaskState),
            cancellation_check=cancellation_check,
        )
        return ValueReviewTaskQueue(
            (
                ValueReviewTaskQueueStatus.STALE
                if stale.items
                else ValueReviewTaskQueueStatus.ABSENT
            ),
            ("review_task_source_stale" if stale.items else "review_task_queue_absent"),
            fence,
            None,
            (),
            False,
        )
    policy_time_stale = progress.complete and (
        _fence_reference_day_ns(progress.fence) != _reference_day_ns(reference_time_ns)
    )
    source_changed = progress.complete and (
        _owner_snapshot_without_epoch(progress.fence)
        != _owner_snapshot_without_epoch(requested_fence)
    )
    page = list_current_review_tasks(
        database,
        limit=limit,
        scope=scope,
        task_type=VALUE_REVIEW_TASK_TYPE,
        states=(ReviewTaskState.OPEN, ReviewTaskState.IN_REVIEW),
        source_snapshot_fingerprint=fence.source_snapshot_fingerprint,
        source_snapshot_as_published=policy_time_stale or source_changed,
        cancellation_check=cancellation_check,
    )
    for record in page.items:
        if record.source_snapshot_fingerprint != fence.source_snapshot_fingerprint:
            raise ValueReviewTaskStateError("review task escaped its source fence")
        _validated_value_snapshot(record)
    evidence_complete = progress.evidence_complete and source_reason is None
    status = (
        ValueReviewTaskQueueStatus.STALE
        if policy_time_stale or source_changed
        else (
            ValueReviewTaskQueueStatus.READY
            if progress.complete and evidence_complete
            else ValueReviewTaskQueueStatus.PARTIAL
        )
    )
    queue_reason = (
        None
        if status is ValueReviewTaskQueueStatus.READY
        else (
            "review_task_source_changed"
            if source_changed
            else "review_task_policy_time_stale"
            if policy_time_stale
            else progress.evidence_reason or source_reason or "review_task_scan_partial"
        )
    )
    return ValueReviewTaskQueue(
        status,
        queue_reason,
        fence,
        progress,
        page.items,
        page.has_more,
    )


def refresh_value_review_tasks(
    database: Path,
    paths: ValueReviewPaths,
    *,
    scope: str,
    clock_ns: ClockNs = time.time_ns,
    cancellation_check: CancellationCheck | None = None,
) -> ValueReviewTaskRefreshResult:
    """Publish one bounded Value page under the exclusive Framework lock."""

    now_ns = clock_ns()
    if isinstance(now_ns, bool) or not isinstance(now_ns, int) or now_ns <= 0:
        raise RuntimeError("value review refresh clock returned an invalid timestamp")
    reference_time_ns = _reference_day_ns(now_ns)
    _checkpoint(cancellation_check)
    fence, source_reason = build_value_review_task_fence(
        paths,
        scope=scope,
        reference_time_ns=reference_time_ns,
    )
    if fence is None:
        return ValueReviewTaskRefreshResult("unavailable", source_reason, None, None, None, False)

    from neocortex.workflow.review.review_task_repository import (
        ReviewTaskCASConflict,
        lookup_review_task_version_heads,
        publish_review_task_page,
        read_review_task_progress,
    )

    version = _read_framework_version(database)
    if version is not None and version > FRAMEWORK_SCHEMA_VERSION:
        return ValueReviewTaskRefreshResult(
            "unavailable", "framework_state_future", fence, None, None, False
        )
    progress = (
        read_review_task_progress(
            database,
            fence,
            cancellation_check=cancellation_check,
        )
        if version == FRAMEWORK_SCHEMA_VERSION
        else None
    )
    if progress is None and version == FRAMEWORK_SCHEMA_VERSION:
        from neocortex.workflow.review.review_task_repository import find_review_task_scan_progress

        candidate, _complete = find_review_task_scan_progress(
            database,
            scope=scope,
            task_type=VALUE_REVIEW_TASK_TYPE,
            selector_signature=fence.selector_signature,
            owner_source_snapshot=_owner_snapshot_without_epoch(fence),
            cancellation_check=cancellation_check,
        )
        if candidate is not None:
            fence = candidate.fence
            progress = candidate
            reference_time_ns = _fence_reference_day_ns(candidate.fence)
            source_reason = candidate.evidence_reason or source_reason
    if progress is not None and progress.complete:
        return ValueReviewTaskRefreshResult(
            "complete" if progress.evidence_complete and source_reason is None else "partial",
            progress.evidence_reason or source_reason,
            fence,
            None,
            None,
            False,
        )
    cursor_before = _page_cursor(progress)
    query = ValueReviewQuery(
        limit=VALUE_REVIEW_TASK_PAGE_SIZE,
        reference_time_ns=reference_time_ns,
    )
    loaded = load_value_review_observation_page(
        paths,
        query,
        page_size=VALUE_REVIEW_TASK_PAGE_SIZE,
        after=cursor_before,
    )
    if loaded.availability is ValueReviewAvailability.UNAVAILABLE:
        return ValueReviewTaskRefreshResult("unavailable", loaded.reason, fence, None, None, False)
    report = rank_value_observations(
        loaded.observations,
        query,
        availability=loaded.availability,
        complete=loaded.complete,
        reason=loaded.reason,
        provenance=loaded.provenance,
        uncertainties=loaded.uncertainties,
    )
    report_dict = report.to_dict()
    actionable = tuple(item for item in report.items if item.state in _ACTIONABLE_STATES)
    logical_keys = tuple(_logical_key(scope, item) for item in actionable)

    _checkpoint(cancellation_check)
    from neocortex.persistence.framework_state_writer import FrameworkState
    from neocortex.runtime.control.locking import FrameworkRunLock

    database = Path(database)
    with FrameworkRunLock(database.parent / "framework.lock"):
        _checkpoint(cancellation_check)
        source_after, _ = build_value_review_task_fence(
            paths,
            scope=scope,
            reference_time_ns=reference_time_ns,
        )
        if source_after is None or source_after.source_snapshot_fingerprint != (
            fence.source_snapshot_fingerprint
        ):
            return ValueReviewTaskRefreshResult(
                "snapshot_changed",
                "value_review_source_changed_before_publication",
                fence,
                report_dict,
                None,
                False,
            )
        with FrameworkState(database):
            pass
        heads = lookup_review_task_version_heads(
            database,
            logical_keys,
            scope=scope,
            task_type=VALUE_REVIEW_TASK_TYPE,
            cancellation_check=cancellation_check,
        )
        head_by_key = {head.logical_key: head for head in heads}
        publication = _publication(
            fence=fence,
            loaded=loaded,
            report_items=actionable,
            head_by_key=head_by_key,
            scope=scope,
            now_ns=now_ns,
            source_reason=source_reason,
        )
        try:
            result = publish_review_task_page(
                database,
                publication,
                expected_progress_revision=(None if progress is None else progress.revision),
                cancellation_check=cancellation_check,
            )
        except ReviewTaskCASConflict:
            return ValueReviewTaskRefreshResult(
                "snapshot_changed",
                "review_task_progress_changed_before_publication",
                fence,
                report_dict,
                None,
                False,
            )
    return ValueReviewTaskRefreshResult(
        (
            "complete"
            if result.progress.complete
            and result.progress.evidence_complete
            and source_reason is None
            else "partial"
        ),
        result.progress.evidence_reason or source_reason,
        fence,
        report_dict,
        result,
        not result.idempotent,
    )


def _publication(
    *,
    fence: ReviewTaskSourceFence,
    loaded: ValueObservationLoad,
    report_items: tuple[ValueReviewItem, ...],
    head_by_key: Mapping[str, ReviewTaskVersionHead],
    scope: str,
    now_ns: int,
    source_reason: str | None,
) -> ReviewTaskPublication:
    observations = tuple(sorted(loaded.observations, key=lambda item: item.resource_id))
    inputs = tuple(_review_input(item) for item in observations)
    input_by_resource = {
        item.resource.resource_id: item for item in inputs if item.resource is not None
    }
    tasks: list[ReviewTaskDraft] = []
    for item in report_items:
        logical_key = _logical_key(scope, item)
        previous = head_by_key.get(logical_key)
        source = input_by_resource[item.resource_id]
        if (
            previous is not None
            and previous.state
            in {
                ReviewTaskState.RESOLVED,
                ReviewTaskState.DISMISSED,
            }
            and not _terminal_scope_expired(previous, source=source, fence=fence)
        ):
            continue
        version = 1 if previous is None else int(previous.task_version) + 1
        supersedes = None if previous is None else str(previous.task_id)
        identity_payload = {
            "logical_key": logical_key,
            "source_fingerprint": source.fingerprint,
            "source_snapshot_fingerprint": fence.source_snapshot_fingerprint,
            "task_version": version,
        }
        task_id = "review-task:value:" + _sha256_json(identity_payload)
        tasks.append(
            ReviewTaskDraft(
                task_id=task_id,
                logical_key=logical_key,
                task_version=version,
                task_type=VALUE_REVIEW_TASK_TYPE,
                scope=scope,
                source_kind=item.source_kind or "file",
                source_input_id=source.input_id,
                snapshot=CanonicalJsonObject.from_mapping(_value_item_dict(item)),
                evidence=_review_evidence(item, source),
                reason_code=item.state.value,
                uncertainty_detail=CanonicalJsonObject.from_mapping(
                    {
                        "not_a_probability": True,
                        "uncertainties": list(item.uncertainties),
                    }
                ),
                impact=item.review_priority / 100.0,
                uncertainty=1.0,
                irreversibility=1.0,
                suggestions=("inspect_evidence", "resolve_or_dismiss"),
                supersedes_task_id=supersedes,
                created_ns=now_ns,
            )
        )
    cursor_before = (
        None
        if loaded.cursor_before is None
        else CanonicalJsonObject.from_mapping(loaded.cursor_before.to_dict())
    )
    cursor_after = (
        None
        if loaded.cursor_after is None
        else CanonicalJsonObject.from_mapping(loaded.cursor_after.to_dict())
    )
    batch_payload = {
        "cursor_before": None if cursor_before is None else cursor_before.to_dict(),
        "input_fingerprints": [item.fingerprint for item in inputs],
        "source_snapshot_fingerprint": fence.source_snapshot_fingerprint,
    }
    batch_digest = _sha256_json(batch_payload)
    return ReviewTaskPublication(
        batch_id=f"review-task-batch:{batch_digest}",
        batch_key=f"review-task-page:{batch_digest}",
        fence=fence,
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        inputs=inputs,
        tasks=tuple(tasks),
        coverage=(
            ReviewTaskCoverage.COMPLETE
            if loaded.cursor_after is None
            else ReviewTaskCoverage.PARTIAL
        ),
        producer_signature=VALUE_REVIEW_TASK_PRODUCER_SIGNATURE,
        confirmed_ns=now_ns,
        evidence_complete=(
            loaded.availability is ValueReviewAvailability.READY and source_reason is None
        ),
        evidence_reason=(
            None
            if loaded.availability is ValueReviewAvailability.READY and source_reason is None
            else loaded.reason or source_reason or "value_review_evidence_partial"
        ),
    )


def _terminal_scope_expired(
    previous: ReviewTaskVersionHead,
    *,
    source: ReviewTaskInput,
    fence: ReviewTaskSourceFence,
) -> bool:
    decision = previous.decision
    if decision is None:
        return False
    payload = decision.to_dict()
    expected = {
        "decision",
        "schema",
        "scope",
        "selector_signature",
        "source_input_fingerprint",
        "source_snapshot_fingerprint",
    }
    if set(payload) != expected:
        # Legacy terminal decisions remain canonical; no intent is invented.
        return False
    if (
        payload.get("schema") != "neocortex.review-task-decision/v1"
        or payload.get("decision") != previous.state.value
        or payload.get("source_input_fingerprint") != previous.source_input_fingerprint
        or payload.get("source_snapshot_fingerprint") != previous.source_snapshot_fingerprint
        or payload.get("selector_signature") != previous.selector_signature
    ):
        raise ValueReviewTaskStateError("terminal ReviewTask decision is contradictory")
    decision_scope = payload.get("scope")
    if decision_scope == "permanent":
        return False
    if decision_scope == "until-source-change":
        return source.fingerprint != previous.source_input_fingerprint
    if decision_scope == "until-policy-change":
        return fence.selector_signature != previous.selector_signature
    raise ValueReviewTaskStateError("terminal ReviewTask decision scope is invalid")


def _review_input(observation: ValueFileObservation) -> ReviewTaskInput:
    payload = _json_data(asdict(observation))
    fingerprint = _sha256_json(payload)
    resource = ResourceRef(
        resource_id=observation.resource_id,
        source_kind=observation.source_kind or "file",
        owner="inventory",
        physical_identity=PhysicalIdentityRef(
            scheme="neocortex-file-resource-id-v1",
            value=observation.resource_id,
            identity_version=1,
        ),
        current_path=observation.path,
    )
    revision = RevisionRef(
        resource_id=observation.resource_id,
        revision_id=f"revision:value-review:{fingerprint}",
        producer="neocortex.value-review.source",
        processing_signature="value-review-observation-v1",
        generation=None,
        state=RevisionState.CURRENT,
    )
    return ReviewTaskInput(
        input_id=f"value-review-input:{fingerprint}",
        fingerprint_algorithm="sha256-canonical-json-v1",
        fingerprint=fingerprint,
        resource=resource,
        revision=revision,
    )


def _review_evidence(
    item: ValueReviewItem,
    source: ReviewTaskInput,
) -> tuple[EvidenceRef, ...]:
    if source.revision is None:
        raise AssertionError("Value Review input must include a revision")
    result: list[EvidenceRef] = []
    for evidence in item.evidence:
        identifiers = [
            ("neocortex.value.owner", evidence.owner),
            ("neocortex.value.kind", evidence.kind),
            ("neocortex.value.strength", evidence.strength.value),
        ]
        for label, value in (
            ("publication_id", evidence.publication_id),
            ("record_id", evidence.record_id),
        ):
            if value is not None:
                identifiers.append((f"neocortex.value.{label}", _bounded_identifier(value)))
        for fact in evidence.facts:
            identifiers.append(
                (
                    f"neocortex.value.fact.{_bounded_identifier(fact.name)}",
                    _bounded_identifier(fact.value),
                )
            )
        result.append(
            EvidenceRef(
                evidence_id=evidence.evidence_id,
                resource_id=item.resource_id,
                revision_id=source.revision.revision_id,
                method=(
                    EvidenceMethod.EXTRACTED
                    if evidence.kind
                    in {
                        "extraction_health",
                        "published_catalog_record",
                        "published_text_fingerprint_count",
                    }
                    else EvidenceMethod.STRUCTURAL
                ),
                extractor="neocortex.value-review",
                extractor_version="1",
                identifiers=tuple(identifiers[:64]),
            )
        )
    return tuple(result)


def _logical_key(scope: str, item: ValueReviewItem) -> str:
    digest = _sha256_json(
        {
            "reason_code": item.state.value,
            "resource_id": item.resource_id,
            "scope": scope,
            "source_kind": item.source_kind or "file",
            "task_type": VALUE_REVIEW_TASK_TYPE,
        }
    )
    return f"review-task-logical:value:{digest}"


def _value_item_dict(item: ValueReviewItem) -> dict[str, object]:
    payload = _json_data(asdict(item))
    if not isinstance(payload, dict):  # pragma: no cover - dataclass invariant
        raise AssertionError("ValueReviewItem did not serialize to an object")
    return cast(dict[str, object], payload)


def _validated_value_snapshot(record: ReviewTaskRecord) -> dict[str, object]:
    payload = record.task.snapshot.to_dict()
    required = {
        "advisory_only",
        "dimensions",
        "evidence",
        "mutation_authorized",
        "path",
        "provenance",
        "reasons",
        "resource_id",
        "review_priority",
        "size_bytes",
        "source_kind",
        "state",
        "uncertainties",
    }
    if set(payload) != required:
        raise ValueReviewTaskStateError("review task Value snapshot has an invalid shape")
    resource = record.source.resource
    if (
        resource is None
        or payload.get("resource_id") != resource.resource_id
        or payload.get("path") != resource.current_path
        or payload.get("state") != record.task.reason_code
        or record.task.reason_code not in {value.value for value in _ACTIONABLE_STATES}
        or payload.get("advisory_only") is not True
        or payload.get("mutation_authorized") is not False
    ):
        raise ValueReviewTaskStateError("review task Value snapshot contradicts its source")
    return payload


def _value_task_item(record: ReviewTaskRecord) -> dict[str, object]:
    payload = dict(_validated_value_snapshot(record))
    payload["review_task"] = {
        "batch_id": record.batch_id,
        "current_event_id": record.current_event.event_id,
        "priority": record.task.priority,
        "source_snapshot_fingerprint": record.source_snapshot_fingerprint,
        "state": record.state.value,
        "task_id": record.task.task_id,
        "task_version": record.task.task_version,
    }
    return payload


def _read_framework_version(database: Path) -> int | None:
    database = Path(database)
    try:
        metadata = os.lstat(database)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueReviewTaskStateError(f"framework state is inaccessible: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueReviewTaskStateError("framework state is not a regular file")
    try:
        connection = sqlite3.connect(readonly_sqlite_uri(database), uri=True, timeout=60.0)
        try:
            connection.execute("PRAGMA query_only=ON")
            return read_application_schema_version(connection, label="framework")
        finally:
            connection.close()
    except (sqlite3.DatabaseError, SQLiteSchemaContractError) as exc:
        detail = str(exc).casefold()
        reason = (
            "framework_state_busy"
            if "locked" in detail or "busy" in detail
            else (
                "framework_state_inaccessible"
                if "unable to open" in detail
                else "framework_state_corrupt"
            )
        )
        raise ValueReviewTaskStateError(reason) from exc


def _page_cursor(progress: ReviewTaskScanProgress | None) -> ValueReviewPageCursor | None:
    if progress is None or progress.cursor is None:
        return None
    return ValueReviewPageCursor.from_mapping(progress.cursor.to_dict())


def _bounded_identifier(value: str) -> str:
    if len(value) <= 512:
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_data(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _json_data(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_data(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_data(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"unsupported Value Review JSON value: {type(value).__name__}")


def _sha256_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _checkpoint(callback: CancellationCheck | None) -> None:
    if callback is not None:
        callback()


__all__ = (
    "VALUE_REVIEW_TASK_PAGE_SIZE",
    "VALUE_REVIEW_TASK_PRODUCER_SIGNATURE",
    "VALUE_REVIEW_TASK_TYPE",
    "ValueReviewTaskQueue",
    "ValueReviewTaskQueueStatus",
    "ValueReviewTaskRefreshResult",
    "ValueReviewTaskStateError",
    "build_value_review_task_fence",
    "read_value_review_task_queue",
    "refresh_value_review_tasks",
)


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.value_review_tasks")
