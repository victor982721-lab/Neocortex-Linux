"""Durable, non-mutating curation review lifecycle.

This module bridges the published curation-plan page with the existing
Framework ``ReviewTask`` owner.  It deliberately does not create an owner,
authorize a filesystem effect, or touch ``file_actions``.  A plan page is
published as advisory review work and human decisions are appended through the
existing CAS event contract.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from neocortex.curation.preview import CurationItem, CurationPlanPage, build_curation_plan_page
from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod,
    EvidenceRef,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from neocortex.runtime.control.locking import FrameworkRunLock
from neocortex.workflow.review.review_task_contracts import (
    CanonicalJsonObject,
    ReviewTaskActorKind,
    ReviewTaskCoverage,
    ReviewTaskDraft,
    ReviewTaskEvent,
    ReviewTaskInput,
    ReviewTaskPublication,
    ReviewTaskPublicationResult,
    ReviewTaskSourceFence,
    ReviewTaskState,
    ReviewTaskTransition,
    ReviewTaskVersionHead,
)
from neocortex.workflow.review.review_task_repository import (
    ReviewTaskCASConflict,
    append_review_task_event,
    lookup_review_task_version_heads,
    publish_review_task_page,
    read_review_task,
    read_review_task_event_by_key,
    read_review_task_publication,
    read_review_task_progress,
)


CURATION_REVIEW_SCHEMA_VERSION = 1
CURATION_REVIEW_TASK_TYPE = "curation-review"
CURATION_REVIEW_SCOPE = "personal"
CURATION_REVIEW_SELECTOR_SIGNATURE = "curation-plan-v1"
CURATION_REVIEW_PRODUCER_SIGNATURE = "neocortex.curation-review/v1"
CURATION_REVIEW_CONTRACT = "neocortex.curation-review/v1"
CURATION_PLAN_CONTRACT = "neocortex.curation-plan/v1"
MAX_CURATION_REVIEW_PAGE = 100
MAX_CURATION_REVIEW_ITEM_SNAPSHOT_BYTES = 64 * 1024

ClockNs = Callable[[], int]


class CurationLifecycleError(RuntimeError):
    """A curation review request cannot satisfy its durable contract."""


class CurationLifecycleSnapshotChanged(CurationLifecycleError):
    """The plan or ReviewTask head changed at an authority boundary."""


class CurationLifecycleUnavailable(CurationLifecycleError):
    """The required published owner is absent or incompatible."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise CurationLifecycleError("curation review value is not canonical JSON") from exc


def _sha256_json(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_plan_digest(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError("plan_digest must be sha256:<64 lowercase hex characters>")
    return value


def _validate_item_id(value: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > 4_096:
        raise ValueError("item_id must be a non-empty trimmed string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("item_id contains a control character")
    return value


def _validate_actor(value: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > 256:
        raise ValueError("actor must be a non-empty trimmed string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("actor contains a control character")
    return value


def _validate_note(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value.strip() != value or len(value.encode()) > 8 * 1024:
        raise ValueError("note must be a bounded trimmed string")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ValueError("note contains a control character")
    return value


def _validate_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_CURATION_REVIEW_PAGE:
        raise ValueError(
            f"curation review limit must be between 1 and {MAX_CURATION_REVIEW_PAGE}"
        )
    return value


def _validate_cursor(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value.strip() != value or len(value.encode()) > 2_048:
        raise ValueError("curation review cursor is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("curation review cursor contains a control character")
    return value


def _clock(clock_ns: ClockNs) -> int:
    value = clock_ns()
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CurationLifecycleError("curation review clock returned an invalid timestamp")
    return value


def _cursor_object(cursor: str | None) -> CanonicalJsonObject | None:
    return None if cursor is None else CanonicalJsonObject.from_mapping({"curation_cursor": cursor})


def _item_snapshot(page: CurationPlanPage, item: CurationItem) -> dict[str, object]:
    payload = {
        "contract": "neocortex.curation-item/v1",
        "plan_digest": page.plan_digest,
        "snapshot_id": page.snapshot_id,
        "item": item.to_dict(),
        "advisory_only": True,
        "mutation_authorized": False,
    }
    encoded = _canonical_json(payload).encode("utf-8")
    if len(encoded) > MAX_CURATION_REVIEW_ITEM_SNAPSHOT_BYTES:
        raise CurationLifecycleError("curation item evidence exceeds the ReviewTask row limit")
    return payload


def _logical_key(item_id: str) -> str:
    digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()
    return f"curation-review-logical-v1:{digest}"


def _resource_and_revision(page: CurationPlanPage, item: CurationItem) -> tuple[ResourceRef, RevisionRef]:
    item_digest = hashlib.sha256(item.item_id.encode("utf-8")).hexdigest()
    resource_id = f"curation-resource-v1:{item_digest}"
    revision_id = f"curation-revision-v1:{page.plan_digest[7:]}:{item_digest[:16]}"
    resource = ResourceRef(
        resource_id=resource_id,
        source_kind=item.kind,
        owner="curation",
        current_path=item.source_path,
    )
    revision = RevisionRef(
        resource_id=resource_id,
        revision_id=revision_id,
        producer=CURATION_REVIEW_PRODUCER_SIGNATURE,
        processing_signature=page.plan_digest,
        generation=page.scan_id,
        state=RevisionState.CURRENT,
    )
    return resource, revision


def _review_input(page: CurationPlanPage, item: CurationItem) -> ReviewTaskInput:
    resource, revision = _resource_and_revision(page, item)
    fingerprint = _sha256_json(item.to_dict())
    return ReviewTaskInput(
        input_id=f"curation-review-input-v1:{hashlib.sha256(item.item_id.encode('utf-8')).hexdigest()}",
        fingerprint_algorithm="sha256-canonical-json-v1",
        fingerprint=fingerprint,
        resource=resource,
        revision=revision,
    )


def _review_evidence(page: CurationPlanPage, item: CurationItem, revision: RevisionRef) -> tuple[EvidenceRef, ...]:
    return (
        EvidenceRef(
            evidence_id=f"curation-review-evidence-v1:{hashlib.sha256((page.plan_digest + item.item_id).encode('utf-8')).hexdigest()}",
            resource_id=revision.resource_id,
            revision_id=revision.revision_id,
            method=EvidenceMethod.STRUCTURAL,
            section_kind="curation_item",
            section_id=item.item_id,
            snippet=item.reason,
            extractor="neocortex.curation.preview",
            extractor_version=str(page.schema_version),
            generation=page.scan_id,
            identifiers=(
                ("curation.plan_digest", page.plan_digest),
                ("curation.item_id", item.item_id),
            ),
        ),
    )


def _task_id(
    logical_key: str,
    source: ReviewTaskInput,
    page: CurationPlanPage,
    version: int,
) -> str:
    return "curation-review-task-v1:" + hashlib.sha256(
        _canonical_json(
            {
                "logical_key": logical_key,
                "plan_digest": page.plan_digest,
                "source_fingerprint": source.fingerprint,
                "task_version": version,
            }
        ).encode("utf-8")
    ).hexdigest()


def _draft(
    page: CurationPlanPage,
    item: CurationItem,
    source: ReviewTaskInput,
    previous: ReviewTaskVersionHead | None,
    now_ns: int,
) -> ReviewTaskDraft | None:
    logical_key = _logical_key(item.item_id)
    if previous is not None and (
        previous.source_snapshot_fingerprint == _fence(page).source_snapshot_fingerprint
        and previous.source_input_fingerprint == source.fingerprint
    ):
        # Same immutable item/source already has a durable head.  Replays are
        # handled by the deterministic batch key; this guard also prevents a
        # different page size from creating a second version of one item.
        return None
    version = 1 if previous is None else previous.task_version + 1
    resource = source.resource
    revision = source.revision
    if resource is None or revision is None:  # pragma: no cover - construction invariant
        raise CurationLifecycleError("curation review input lacks logical resource revision")
    snapshot = CanonicalJsonObject.from_mapping(_item_snapshot(page, item))
    detail = CanonicalJsonObject.from_mapping(
        {
            "epistemic": "advisory",
            "item_status": item.status,
            "action": item.action,
            "reason": item.reason,
            "plan_digest": page.plan_digest,
            "verification_mode": item.evidence.get("verification_mode"),
        }
    )
    return ReviewTaskDraft(
        task_id=_task_id(logical_key, source, page, version),
        logical_key=logical_key,
        task_version=version,
        task_type=CURATION_REVIEW_TASK_TYPE,
        scope=CURATION_REVIEW_SCOPE,
        source_kind=item.kind,
        source_input_id=source.input_id,
        snapshot=snapshot,
        evidence=_review_evidence(page, item, revision),
        reason_code="curation_item_review",
        uncertainty_detail=detail,
        impact=0.8 if item.kind == "duplicate_group" else 0.6,
        uncertainty=0.8,
        irreversibility=0.2,
        suggestions=("inspect_source_evidence", "resolve_or_dismiss"),
        supersedes_task_id=None if previous is None else previous.task_id,
        created_ns=now_ns,
    )


def _fence(page: CurationPlanPage) -> ReviewTaskSourceFence:
    return ReviewTaskSourceFence.create(
        scope=CURATION_REVIEW_SCOPE,
        task_type=CURATION_REVIEW_TASK_TYPE,
        selector_signature=CURATION_REVIEW_SELECTOR_SIGNATURE,
        source_snapshot={
            "contract": CURATION_PLAN_CONTRACT,
            "plan_digest": page.plan_digest,
            "snapshot_id": page.snapshot_id,
            "coverage": page.coverage,
            "root": page.root,
            "scan_id": page.scan_id,
            "items_total": page.items_total,
        },
    )


def _assert_page(page: CurationPlanPage, plan_digest: str) -> None:
    if page.coverage != "complete":
        raise CurationLifecycleUnavailable("curation plan coverage is not complete")
    if page.plan_digest != plan_digest:
        raise CurationLifecycleSnapshotChanged("curation plan digest changed")
    if not page.snapshot_id or not page.snapshot_id.startswith("sha256:"):
        raise CurationLifecycleError("curation plan snapshot id is invalid")


def _same_page(left: CurationPlanPage, right: CurationPlanPage) -> bool:
    return _canonical_json(left.to_dict()) == _canonical_json(right.to_dict())


@dataclass(frozen=True, slots=True)
class CurationReviewItem:
    item: CurationItem
    task_id: str | None
    task_version: int | None
    state: ReviewTaskState | None
    current_event_id: str | None
    decision: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "item": self.item.to_dict(),
            "item_id": self.item.item_id,
            "task_id": self.task_id,
            "task_version": self.task_version,
            "state": None if self.state is None else self.state.value,
            "current_event_id": self.current_event_id,
            "decision": self.decision,
        }


@dataclass(frozen=True, slots=True)
class CurationReviewResult:
    status: str
    reason: str | None
    plan_digest: str
    snapshot_id: str | None
    cursor: str | None
    next_cursor: str | None
    items_total: int
    items: tuple[CurationReviewItem, ...]
    publication: ReviewTaskPublicationResult | None

    @property
    def idempotent(self) -> bool | None:
        return None if self.publication is None else self.publication.idempotent

    def to_dict(self) -> dict[str, object]:
        publication = self.publication
        return {
            "schema_version": CURATION_REVIEW_SCHEMA_VERSION,
            "kind": "neocortex_curation_review",
            "contract": CURATION_REVIEW_CONTRACT,
            "status": self.status,
            "reason": self.reason,
            "plan_digest": self.plan_digest,
            "snapshot_id": self.snapshot_id,
            "cursor": self.cursor,
            "next_cursor": self.next_cursor,
            "items_total": self.items_total,
            "items": [item.to_dict() for item in self.items],
            "publication": (
                None
                if publication is None
                else {
                    "batch_id": publication.batch_id,
                    "task_ids": list(publication.task_ids),
                    "idempotent": publication.idempotent,
                    "progress": {
                        "complete": publication.progress.complete,
                        "revision": publication.progress.revision,
                        "scanned_count": publication.progress.scanned_count,
                        "selected_count": publication.progress.selected_count,
                    },
                }
            ),
            "effects": {
                "state": "review_task_publication",
                "corpus": "none",
                "external": "none",
            },
            "trust": {
                "content_class": "untrusted_corpus_evidence",
                "instruction_authority": False,
                "actions_authorized": False,
            },
        }


@dataclass(frozen=True, slots=True)
class CurationDecisionResult:
    """One durable decision event and whether the call replayed it."""

    event: ReviewTaskEvent
    idempotent: bool


def _heads(
    database: Path,
    page: CurationPlanPage,
    *,
    items: tuple[CurationItem, ...],
) -> dict[str, ReviewTaskVersionHead]:
    if not items:
        return {}
    keys = tuple(_logical_key(item.item_id) for item in items)
    result = lookup_review_task_version_heads(
        database,
        keys,
        scope=CURATION_REVIEW_SCOPE,
        task_type=CURATION_REVIEW_TASK_TYPE,
    )
    return {item.logical_key: item for item in result}


def review_curation_page(
    state_directory: Path,
    database: Path,
    *,
    plan_digest: str,
    limit: int = 50,
    cursor: str | None = None,
    clock_ns: ClockNs = time.time_ns,
) -> CurationReviewResult:
    """Publish one plan page as advisory ReviewTasks, never as an effect."""

    bounded_digest = _validate_plan_digest(plan_digest)
    bounded_limit = _validate_limit(limit)
    bounded_cursor = _validate_cursor(cursor)
    state_directory = Path(state_directory)
    database = Path(database)
    if not database.is_file():
        raise CurationLifecycleUnavailable("Framework review owner is absent")

    before = build_curation_plan_page(state_directory, bounded_limit, bounded_cursor)
    _assert_page(before, bounded_digest)
    with FrameworkRunLock(database.parent / "framework.lock"):
        after = build_curation_plan_page(state_directory, bounded_limit, bounded_cursor)
        _assert_page(after, bounded_digest)
        if not _same_page(before, after):
            raise CurationLifecycleSnapshotChanged("curation plan changed before publication")
        fence = _fence(after)
        progress = read_review_task_progress(database, fence)
        page_items = tuple(after.items)
        head_by_key = _heads(database, after, items=page_items)
        inputs = tuple(_review_input(after, item) for item in page_items)
        batch_payload = {
            "plan_digest": after.plan_digest,
            "cursor_before": bounded_cursor,
            "input_fingerprints": [source.fingerprint for source in inputs],
        }
        batch_digest = hashlib.sha256(_canonical_json(batch_payload).encode("utf-8")).hexdigest()
        batch_id = f"curation-review-batch-v1:{batch_digest}"
        batch_key = f"curation-review-page-v1:{batch_digest}"
        existing = read_review_task_publication(database, batch_id)
        if existing is not None:
            if (
                existing.batch_key != batch_key
                or existing.fence != fence
                or existing.cursor_before != _cursor_object(after.cursor)
                or existing.cursor_after != _cursor_object(after.next_cursor)
                or existing.inputs != inputs
                or existing.coverage
                != (
                    ReviewTaskCoverage.COMPLETE
                    if after.next_cursor is None
                    else ReviewTaskCoverage.PARTIAL
                )
                or existing.producer_signature != CURATION_REVIEW_PRODUCER_SIGNATURE
                or not existing.evidence_complete
                or existing.evidence_reason is not None
            ):
                raise CurationLifecycleError("curation review batch idempotency key changed payload")
            if progress is None:
                raise CurationLifecycleError("curation review batch has no durable progress")
            final_heads = _heads(database, after, items=page_items)
            result = ReviewTaskPublicationResult(
                batch_id=existing.batch_id,
                task_ids=tuple(task.task_id for task in existing.tasks),
                progress=progress,
                idempotent=True,
            )
            linked = tuple(
                CurationReviewItem(
                    item=item,
                    task_id=(head.task_id if (head := final_heads.get(_logical_key(item.item_id))) else None),
                    task_version=(head.task_version if head else None),
                    state=(head.state if head else None),
                    current_event_id=(head.event_id if head else None),
                    decision=(None if head is None or head.decision is None else head.decision.to_dict()),
                )
                for item in page_items
            )
            return CurationReviewResult(
                status="complete",
                reason=None,
                plan_digest=after.plan_digest,
                snapshot_id=after.snapshot_id,
                cursor=after.cursor,
                next_cursor=after.next_cursor,
                items_total=after.items_total,
                items=linked,
                publication=result,
            )
        now_ns = _clock(clock_ns)
        tasks: list[ReviewTaskDraft] = []
        for item, source in zip(page_items, inputs, strict=True):
            task = _draft(after, item, source, head_by_key.get(_logical_key(item.item_id)), now_ns)
            if task is not None:
                tasks.append(task)
        publication = ReviewTaskPublication(
            batch_id=batch_id,
            batch_key=batch_key,
            fence=fence,
            cursor_before=_cursor_object(after.cursor),
            cursor_after=_cursor_object(after.next_cursor),
            inputs=inputs,
            tasks=tuple(tasks),
            coverage=(
                ReviewTaskCoverage.COMPLETE
                if after.next_cursor is None
                else ReviewTaskCoverage.PARTIAL
            ),
            producer_signature=CURATION_REVIEW_PRODUCER_SIGNATURE,
            confirmed_ns=now_ns,
        )
        try:
            result = publish_review_task_page(
                database,
                publication,
                expected_progress_revision=(None if progress is None else progress.revision),
            )
        except ReviewTaskCASConflict as exc:
            raise CurationLifecycleSnapshotChanged(
                "ReviewTask progress changed before publication"
            ) from exc
        final_heads = _heads(database, after, items=page_items)
    linked = tuple(
        CurationReviewItem(
            item=item,
            task_id=(head.task_id if (head := final_heads.get(_logical_key(item.item_id))) else None),
            task_version=(head.task_version if head else None),
            state=(head.state if head else None),
            current_event_id=(head.event_id if head else None),
            decision=(None if head is None or head.decision is None else head.decision.to_dict()),
        )
        for item in page_items
    )
    return CurationReviewResult(
        status="complete",
        reason=None,
        plan_digest=after.plan_digest,
        snapshot_id=after.snapshot_id,
        cursor=after.cursor,
        next_cursor=after.next_cursor,
        items_total=after.items_total,
        items=linked,
        publication=result,
    )


def _review_snapshot(record: Any, plan_digest: str) -> Mapping[str, object]:
    try:
        snapshot = record.task.snapshot.to_dict()
    except (AttributeError, TypeError, ValueError) as exc:
        raise CurationLifecycleError("curation ReviewTask snapshot is invalid") from exc
    if (
        snapshot.get("contract") != "neocortex.curation-item/v1"
        or snapshot.get("plan_digest") != plan_digest
        or snapshot.get("advisory_only") is not True
        or snapshot.get("mutation_authorized") is not False
    ):
        raise CurationLifecycleSnapshotChanged("ReviewTask is not bound to the requested plan")
    return snapshot


def _event_replay(
    event: ReviewTaskEvent,
    *,
    task_id: str,
    expected_event_id: str,
    decision: str,
    decision_scope: str,
    actor: str,
    note: str | None,
    selector_signature: str,
    source_input_fingerprint: str,
    source_snapshot_fingerprint: str,
) -> bool:
    payload = None if event.decision is None else event.decision.to_dict()
    return (
        event.task_id == task_id
        and event.previous_event_id == expected_event_id
        and event.to_state is ReviewTaskState(decision)
        and event.actor_kind is ReviewTaskActorKind.HUMAN
        and event.actor_id == actor
        and event.note == note
        and payload == {
            "decision": decision,
            "schema": "neocortex.review-task-decision/v1",
            "scope": decision_scope,
            "selector_signature": selector_signature,
            "source_input_fingerprint": source_input_fingerprint,
            "source_snapshot_fingerprint": source_snapshot_fingerprint,
        }
    )


def _decide_curation_item_result(
    state_directory: Path,
    database: Path,
    *,
    plan_digest: str,
    item_id: str,
    expected_event_id: str,
    decision: str,
    decision_scope: str,
    actor: str,
    note: str | None = None,
    clock_ns: ClockNs = time.time_ns,
) -> CurationDecisionResult:
    """Append a human review decision bound to one unchanged curation plan.

    The returned event is a review fact only.  In particular, ``resolved`` and
    ``dismissed`` never authorize ``file_actions`` or a filesystem mutation.
    """

    bounded_digest = _validate_plan_digest(plan_digest)
    bounded_item = _validate_item_id(item_id)
    bounded_actor = _validate_actor(actor)
    bounded_note = _validate_note(note)
    if decision not in {"resolved", "dismissed"}:
        raise ValueError("decision must be resolved or dismissed")
    if decision_scope not in {"until-source-change", "until-policy-change", "permanent"}:
        raise ValueError("decision_scope is invalid")
    if not isinstance(expected_event_id, str) or not expected_event_id:
        raise ValueError("expected_event_id must be a non-empty string")
    if not Path(database).is_file():
        raise CurationLifecycleUnavailable("Framework review owner is absent")

    # Re-read a bounded page from the live plan to prove that the requested
    # digest is still current before resolving the logical task head.
    page = build_curation_plan_page(Path(state_directory), 1, None)
    _assert_page(page, bounded_digest)
    logical_key = _logical_key(bounded_item)
    heads = lookup_review_task_version_heads(
        Path(database),
        (logical_key,),
        scope=CURATION_REVIEW_SCOPE,
        task_type=CURATION_REVIEW_TASK_TYPE,
    )
    if not heads:
        raise CurationLifecycleError("curation item has no published review task")
    head = heads[0]
    record = read_review_task(Path(database), head.task_id)
    if record is None:
        raise CurationLifecycleError("curation review task disappeared")
    _review_snapshot(record, bounded_digest)
    if record.task.task_type != CURATION_REVIEW_TASK_TYPE or record.task.logical_key != logical_key:
        raise CurationLifecycleError("curation ReviewTask identity is contradictory")
    decision_payload = CanonicalJsonObject.from_mapping(
        {
            "decision": decision,
            "schema": "neocortex.review-task-decision/v1",
            "scope": decision_scope,
            "selector_signature": record.selector_signature,
            "source_input_fingerprint": record.source.fingerprint,
            "source_snapshot_fingerprint": record.source_snapshot_fingerprint,
        }
    )
    event_identity = {
        "actor": bounded_actor,
        "decision": decision,
        "decision_scope": decision_scope,
        "expected_event_id": expected_event_id,
        "item_id": bounded_item,
        "note": bounded_note,
        "plan_digest": bounded_digest,
        "task_id": record.task.task_id,
    }
    event_digest = hashlib.sha256(_canonical_json(event_identity).encode("utf-8")).hexdigest()
    event_key = f"curation-review-event-key-v1:{event_digest}"
    existing = read_review_task_event_by_key(Path(database), event_key)
    if existing is not None:
        if not _event_replay(
            existing,
            task_id=record.task.task_id,
            expected_event_id=expected_event_id,
            decision=decision,
            decision_scope=decision_scope,
            actor=bounded_actor,
            note=bounded_note,
            selector_signature=record.selector_signature,
            source_input_fingerprint=record.source.fingerprint,
            source_snapshot_fingerprint=record.source_snapshot_fingerprint,
        ):
            raise CurationLifecycleError("curation decision idempotency key changed payload")
        return CurationDecisionResult(existing, True)
    if record.current_event.event_id != expected_event_id:
        raise CurationLifecycleSnapshotChanged("ReviewTask event head changed")
    now_ns = _clock(clock_ns)
    transition = ReviewTaskTransition(
        event_id=f"curation-review-event-v1:{event_digest}",
        event_key=event_key,
        task_id=record.task.task_id,
        expected_event_id=expected_event_id,
        expected_state=record.state,
        to_state=ReviewTaskState(decision),
        actor_kind=ReviewTaskActorKind.HUMAN,
        actor_id=bounded_actor,
        provenance=CanonicalJsonObject.from_mapping(
            {
                "contract": CURATION_REVIEW_CONTRACT,
                "item_id": bounded_item,
                "plan_digest": bounded_digest,
                "surface": "curate-decide",
            }
        ),
        decision=decision_payload,
        note=bounded_note,
        observed_ns=now_ns,
        recorded_ns=now_ns,
    )
    try:
        result = append_review_task_event(Path(database), transition)
    except ReviewTaskCASConflict as exc:
        raise CurationLifecycleSnapshotChanged("ReviewTask event head changed") from exc
    return CurationDecisionResult(result.event, result.idempotent)


def decide_curation_item(
    state_directory: Path,
    database: Path,
    *,
    plan_digest: str,
    item_id: str,
    expected_event_id: str,
    decision: str,
    decision_scope: str,
    actor: str,
    note: str | None = None,
    clock_ns: ClockNs = time.time_ns,
) -> ReviewTaskEvent:
    """Compatibility facade returning only the durable decision event."""

    return _decide_curation_item_result(
        state_directory,
        database,
        plan_digest=plan_digest,
        item_id=item_id,
        expected_event_id=expected_event_id,
        decision=decision,
        decision_scope=decision_scope,
        actor=actor,
        note=note,
        clock_ns=clock_ns,
    ).event


__all__ = (
    "CURATION_PLAN_CONTRACT",
    "CURATION_REVIEW_CONTRACT",
    "CURATION_REVIEW_PRODUCER_SIGNATURE",
    "CURATION_REVIEW_SCHEMA_VERSION",
    "CURATION_REVIEW_SCOPE",
    "CURATION_REVIEW_SELECTOR_SIGNATURE",
    "CURATION_REVIEW_TASK_TYPE",
    "CurationDecisionResult",
    "CurationLifecycleError",
    "CurationLifecycleSnapshotChanged",
    "CurationLifecycleUnavailable",
    "CurationReviewItem",
    "CurationReviewResult",
    "decide_curation_item",
    "review_curation_page",
)
