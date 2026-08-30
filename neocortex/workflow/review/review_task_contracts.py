"""Lightweight, bounded contracts for durable cross-domain review tasks.

Review tasks are advisory knowledge.  They preserve typed Knowledge references and
human decisions, but never carry action authorization or a physical mutation.
"""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod,
    EvidenceRef,
    PhysicalIdentityRef,
    ResourceDisposition,
    ResourceRef,
    RevisionRef,
    RevisionState,
)


REVIEW_TASK_CONTRACT_SCHEMA_VERSION = 1
REVIEW_TASK_FRAMEWORK_SCHEMA_VERSION = 22
REVIEW_TASK_PRIORITY_ALGORITHM = "impact-x-uncertainty-x-irreversibility-v1"
REVIEW_TASK_SOURCE_FINGERPRINT_PREFIX = "review-task-source-snapshot-v1:sha256:"

MAX_REVIEW_TASK_IDENTIFIER_CHARS = 256
MAX_REVIEW_TASK_DOMAIN_CHARS = 128
MAX_REVIEW_TASK_LOGICAL_KEY_CHARS = 512
MAX_REVIEW_TASK_SELECTOR_CHARS = 512
MAX_REVIEW_TASK_NOTE_BYTES = 8 * 1024
MAX_REVIEW_TASK_JSON_BYTES = 64 * 1024
MAX_REVIEW_TASK_PUBLICATION_BYTES = 8 * 1024 * 1024
MAX_REVIEW_TASK_EVIDENCE = 64
MAX_REVIEW_TASK_SUGGESTIONS = 32
MAX_REVIEW_TASK_INPUTS_PER_PAGE = 1_000
MAX_REVIEW_TASKS_PER_PAGE = 100
MAX_REVIEW_TASK_READ_PAGE = 100


class ReviewTaskState(StrEnum):
    OPEN = "open"
    IN_REVIEW = "in_review"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"
    SUPERSEDED = "superseded"


class ReviewTaskActorKind(StrEnum):
    SYSTEM = "system"
    HUMAN = "human"


class ReviewTaskCoverage(StrEnum):
    PARTIAL = "partial"
    COMPLETE = "complete"


_TERMINAL_STATES = frozenset(
    {
        ReviewTaskState.RESOLVED,
        ReviewTaskState.DISMISSED,
        ReviewTaskState.SUPERSEDED,
    }
)
_ALLOWED_TRANSITIONS = frozenset(
    {
        (ReviewTaskState.OPEN, ReviewTaskState.IN_REVIEW),
        (ReviewTaskState.OPEN, ReviewTaskState.RESOLVED),
        (ReviewTaskState.OPEN, ReviewTaskState.DISMISSED),
        (ReviewTaskState.OPEN, ReviewTaskState.SUPERSEDED),
        (ReviewTaskState.IN_REVIEW, ReviewTaskState.RESOLVED),
        (ReviewTaskState.IN_REVIEW, ReviewTaskState.DISMISSED),
        (ReviewTaskState.IN_REVIEW, ReviewTaskState.SUPERSEDED),
        (ReviewTaskState.RESOLVED, ReviewTaskState.SUPERSEDED),
        (ReviewTaskState.DISMISSED, ReviewTaskState.SUPERSEDED),
    }
)


def _required_text(
    name: str, value: object, *, limit: int = MAX_REVIEW_TASK_IDENTIFIER_CHARS
) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-empty trimmed string")
    if len(value) > limit:
        raise ValueError(f"{name} cannot exceed {limit} characters")
    return value


def _optional_text(
    name: str,
    value: object,
    *,
    limit: int = MAX_REVIEW_TASK_IDENTIFIER_CHARS,
) -> str | None:
    if value is None:
        return None
    return _required_text(name, value, limit=limit)


def _positive_integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _factor(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a numeric factor")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise ValueError(f"{name} must be finite and between zero and one")
    return normalized


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _load_json(value: str) -> object:
    try:
        return json.loads(value, parse_constant=_reject_json_constant)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("review task JSON is invalid") from exc


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
        raise ValueError("review task value is not canonical JSON data") from exc


def _bounded_json(value: object, *, label: str, maximum_bytes: int) -> str:
    payload = _canonical_json(value)
    if len(payload.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} exceeds the {maximum_bytes}-byte limit")
    return payload


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a JSON object")
    return cast(Mapping[str, object], value)


def _sequence(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a JSON array")
    return value


@dataclass(frozen=True, slots=True)
class CanonicalJsonObject:
    """One immutable-by-value, bounded JSON object."""

    payload_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.payload_json, str):
            raise ValueError("payload_json must be a string")
        if len(self.payload_json.encode("utf-8")) > MAX_REVIEW_TASK_JSON_BYTES:
            raise ValueError(f"payload_json exceeds the {MAX_REVIEW_TASK_JSON_BYTES}-byte limit")
        value = _mapping(_load_json(self.payload_json), label="payload_json")
        if _canonical_json(value) != self.payload_json:
            raise ValueError("payload_json must use canonical JSON encoding")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CanonicalJsonObject:
        return cls(
            _bounded_json(value, label="JSON object", maximum_bytes=MAX_REVIEW_TASK_JSON_BYTES)
        )

    def to_dict(self) -> dict[str, object]:
        return dict(_mapping(_load_json(self.payload_json), label="payload_json"))


def review_task_source_snapshot_fingerprint(snapshot: CanonicalJsonObject) -> str:
    digest = hashlib.sha256(snapshot.payload_json.encode("utf-8")).hexdigest()
    return REVIEW_TASK_SOURCE_FINGERPRINT_PREFIX + digest


def _validate_source_snapshot_fingerprint(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("source_snapshot_fingerprint must be text")
    suffix = value[len(REVIEW_TASK_SOURCE_FINGERPRINT_PREFIX) :]
    if (
        len(value) != len(REVIEW_TASK_SOURCE_FINGERPRINT_PREFIX) + 64
        or not value.startswith(REVIEW_TASK_SOURCE_FINGERPRINT_PREFIX)
        or any(character not in "0123456789abcdef" for character in suffix)
    ):
        raise ValueError("source_snapshot_fingerprint is invalid")
    return value


def _physical_identity_from_payload(value: object) -> PhysicalIdentityRef | None:
    if value is None:
        return None
    payload = _mapping(value, label="physical_identity")
    return PhysicalIdentityRef(
        scheme=cast(str, payload.get("scheme")),
        value=cast(str, payload.get("value")),
        identity_version=cast(int, payload.get("identity_version")),
    )


def _resource_from_payload(value: object) -> ResourceRef | None:
    if value is None:
        return None
    payload = _mapping(value, label="resource")
    disposition_value = payload.get("disposition")
    resource = ResourceRef(
        resource_id=cast(str, payload.get("resource_id")),
        source_kind=cast(str, payload.get("source_kind")),
        owner=cast(str, payload.get("owner")),
        physical_identity=_physical_identity_from_payload(payload.get("physical_identity")),
        current_path=cast(str | None, payload.get("current_path")),
        disposition=(
            None if disposition_value is None else ResourceDisposition(cast(str, disposition_value))
        ),
        canonical_resource_id=cast(str | None, payload.get("canonical_resource_id")),
    )
    if resource.to_dict() != dict(payload):
        raise ValueError("resource payload contains unsupported or non-canonical fields")
    return resource


def _revision_from_payload(value: object) -> RevisionRef | None:
    if value is None:
        return None
    payload = _mapping(value, label="revision")
    revision = RevisionRef(
        resource_id=cast(str, payload.get("resource_id")),
        revision_id=cast(str, payload.get("revision_id")),
        producer=cast(str, payload.get("producer")),
        processing_signature=cast(str, payload.get("processing_signature")),
        generation=cast(int | None, payload.get("generation")),
        state=RevisionState(cast(str, payload.get("state"))),
        observed_at_utc=cast(str | None, payload.get("observed_at_utc")),
    )
    if revision.to_dict() != dict(payload):
        raise ValueError("revision payload contains unsupported or non-canonical fields")
    return revision


def evidence_ref_from_payload(value: object) -> EvidenceRef:
    payload = _mapping(value, label="evidence")
    raw_box = payload.get("bounding_box")
    bounding_box: tuple[float, float, float, float] | None = None
    if raw_box is not None:
        coordinates = _sequence(raw_box, label="evidence bounding_box")
        if len(coordinates) != 4:
            raise ValueError("evidence bounding_box must contain four coordinates")
        bounding_box = cast(tuple[float, float, float, float], tuple(coordinates))
    raw_identifiers = payload.get("identifiers", [])
    identifiers: list[tuple[str, str]] = []
    for item in _sequence(raw_identifiers, label="evidence identifiers"):
        identifier = _mapping(item, label="evidence identifier")
        identifiers.append(
            (cast(str, identifier.get("namespace")), cast(str, identifier.get("value")))
        )
    evidence = EvidenceRef(
        evidence_id=cast(str, payload.get("evidence_id")),
        resource_id=cast(str, payload.get("resource_id")),
        revision_id=cast(str, payload.get("revision_id")),
        method=EvidenceMethod(cast(str, payload.get("method"))),
        page=cast(int | None, payload.get("page")),
        start_line=cast(int | None, payload.get("start_line")),
        end_line=cast(int | None, payload.get("end_line")),
        sheet=cast(str | None, payload.get("sheet")),
        cell_range=cast(str | None, payload.get("cell_range")),
        start_ms=cast(int | None, payload.get("start_ms")),
        end_ms=cast(int | None, payload.get("end_ms")),
        bounding_box=bounding_box,
        coordinate_space=cast(str | None, payload.get("coordinate_space")),
        start_char=cast(int | None, payload.get("start_char")),
        end_char=cast(int | None, payload.get("end_char")),
        symbol=cast(str | None, payload.get("symbol")),
        section_kind=cast(str | None, payload.get("section_kind")),
        section_id=cast(str | None, payload.get("section_id")),
        snippet=cast(str | None, payload.get("snippet")),
        extractor=cast(str | None, payload.get("extractor")),
        extractor_version=cast(str | None, payload.get("extractor_version")),
        generation=cast(int | None, payload.get("generation")),
        identifiers=tuple(identifiers),
    )
    if evidence.to_dict() != dict(payload):
        raise ValueError("evidence payload contains unsupported or non-canonical fields")
    return evidence


@dataclass(frozen=True, slots=True)
class ReviewTaskSourceFence:
    scope: str
    task_type: str
    selector_signature: str
    source_snapshot: CanonicalJsonObject
    source_snapshot_fingerprint: str

    def __post_init__(self) -> None:
        _required_text("scope", self.scope, limit=MAX_REVIEW_TASK_DOMAIN_CHARS)
        _required_text("task_type", self.task_type, limit=MAX_REVIEW_TASK_DOMAIN_CHARS)
        _required_text(
            "selector_signature",
            self.selector_signature,
            limit=MAX_REVIEW_TASK_SELECTOR_CHARS,
        )
        if not isinstance(self.source_snapshot, CanonicalJsonObject):
            raise ValueError("source_snapshot must be a CanonicalJsonObject")
        if not self.source_snapshot.to_dict():
            raise ValueError("source_snapshot cannot be empty")
        _validate_source_snapshot_fingerprint(self.source_snapshot_fingerprint)
        expected = review_task_source_snapshot_fingerprint(self.source_snapshot)
        if self.source_snapshot_fingerprint != expected:
            raise ValueError("source_snapshot_fingerprint does not match source_snapshot")

    @classmethod
    def create(
        cls,
        *,
        scope: str,
        task_type: str,
        selector_signature: str,
        source_snapshot: Mapping[str, object],
    ) -> ReviewTaskSourceFence:
        canonical_snapshot = CanonicalJsonObject.from_mapping(source_snapshot)
        return cls(
            scope=scope,
            task_type=task_type,
            selector_signature=selector_signature,
            source_snapshot=canonical_snapshot,
            source_snapshot_fingerprint=review_task_source_snapshot_fingerprint(canonical_snapshot),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "task_type": self.task_type,
            "selector_signature": self.selector_signature,
            "source_snapshot_fingerprint": self.source_snapshot_fingerprint,
            "source_snapshot": self.source_snapshot.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ReviewTaskInput:
    input_id: str
    fingerprint_algorithm: str
    fingerprint: str
    resource: ResourceRef | None = None
    revision: RevisionRef | None = None

    def __post_init__(self) -> None:
        _required_text("input_id", self.input_id, limit=MAX_REVIEW_TASK_LOGICAL_KEY_CHARS)
        _required_text(
            "fingerprint_algorithm",
            self.fingerprint_algorithm,
            limit=MAX_REVIEW_TASK_DOMAIN_CHARS,
        )
        _required_text("fingerprint", self.fingerprint, limit=1_024)
        if self.resource is not None and not isinstance(self.resource, ResourceRef):
            raise ValueError("resource must be a ResourceRef when present")
        if self.revision is not None:
            if not isinstance(self.revision, RevisionRef):
                raise ValueError("revision must be a RevisionRef when present")
            if self.resource is None:
                raise ValueError("a revision input requires its ResourceRef")
            if self.revision.resource_id != self.resource.resource_id:
                raise ValueError("input resource and revision identities disagree")
        # `source_ref_json` is a 64 KiB owner-local column even though the
        # containing page receipt has a larger aggregate budget.  Enforce the
        # row boundary at construction so a contract-valid input can always be
        # persisted by the repository.
        self.to_json()

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": REVIEW_TASK_CONTRACT_SCHEMA_VERSION,
            "kind": "review_task_input",
            "input_id": self.input_id,
            "fingerprint_algorithm": self.fingerprint_algorithm,
            "fingerprint": self.fingerprint,
        }
        if self.resource is not None:
            payload["resource"] = self.resource.to_dict()
        if self.revision is not None:
            payload["revision"] = self.revision.to_dict()
        return payload

    def to_json(self) -> str:
        return _bounded_json(
            self.to_dict(),
            label="review task input",
            maximum_bytes=MAX_REVIEW_TASK_JSON_BYTES,
        )

    @classmethod
    def from_json(cls, payload_json: str) -> ReviewTaskInput:
        if not isinstance(payload_json, str):
            raise ValueError("review task input JSON must be text")
        if len(payload_json.encode("utf-8")) > MAX_REVIEW_TASK_JSON_BYTES:
            raise ValueError("review task input JSON is too large")
        payload = _mapping(_load_json(payload_json), label="review task input")
        item = cls(
            input_id=cast(str, payload.get("input_id")),
            fingerprint_algorithm=cast(str, payload.get("fingerprint_algorithm")),
            fingerprint=cast(str, payload.get("fingerprint")),
            resource=_resource_from_payload(payload.get("resource")),
            revision=_revision_from_payload(payload.get("revision")),
        )
        if item.to_dict() != dict(payload) or item.to_json() != payload_json:
            raise ValueError("review task input JSON is not canonical")
        return item


@dataclass(frozen=True, slots=True)
class ReviewTaskDraft:
    task_id: str
    logical_key: str
    task_version: int
    task_type: str
    scope: str
    source_kind: str
    source_input_id: str
    snapshot: CanonicalJsonObject
    evidence: tuple[EvidenceRef, ...]
    reason_code: str
    uncertainty_detail: CanonicalJsonObject
    impact: float
    uncertainty: float
    irreversibility: float
    suggestions: tuple[str, ...]
    supersedes_task_id: str | None
    created_ns: int

    def __post_init__(self) -> None:
        _required_text("task_id", self.task_id)
        _required_text("logical_key", self.logical_key, limit=MAX_REVIEW_TASK_LOGICAL_KEY_CHARS)
        for name, value in (
            ("task_type", self.task_type),
            ("scope", self.scope),
            ("source_kind", self.source_kind),
            ("reason_code", self.reason_code),
        ):
            _required_text(name, value, limit=MAX_REVIEW_TASK_DOMAIN_CHARS)
        _required_text(
            "source_input_id",
            self.source_input_id,
            limit=MAX_REVIEW_TASK_LOGICAL_KEY_CHARS,
        )
        _positive_integer("task_version", self.task_version)
        _positive_integer("created_ns", self.created_ns)
        if not isinstance(self.snapshot, CanonicalJsonObject) or not self.snapshot.to_dict():
            raise ValueError("snapshot must be a non-empty CanonicalJsonObject")
        if not isinstance(self.uncertainty_detail, CanonicalJsonObject):
            raise ValueError("uncertainty_detail must be a CanonicalJsonObject")
        if not isinstance(self.evidence, tuple):
            raise ValueError("evidence must be an immutable tuple")
        if len(self.evidence) > MAX_REVIEW_TASK_EVIDENCE:
            raise ValueError(f"evidence cannot exceed {MAX_REVIEW_TASK_EVIDENCE} items")
        if any(not isinstance(item, EvidenceRef) for item in self.evidence):
            raise ValueError("evidence must contain only EvidenceRef values")
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("evidence identifiers cannot repeat")
        _bounded_json(
            [item.to_dict() for item in self.evidence],
            label="review task evidence",
            maximum_bytes=MAX_REVIEW_TASK_JSON_BYTES,
        )
        object.__setattr__(self, "impact", _factor("impact", self.impact))
        object.__setattr__(self, "uncertainty", _factor("uncertainty", self.uncertainty))
        object.__setattr__(
            self,
            "irreversibility",
            _factor("irreversibility", self.irreversibility),
        )
        if not isinstance(self.suggestions, tuple):
            raise ValueError("suggestions must be an immutable tuple")
        if len(self.suggestions) > MAX_REVIEW_TASK_SUGGESTIONS:
            raise ValueError(f"suggestions cannot exceed {MAX_REVIEW_TASK_SUGGESTIONS} items")
        for suggestion in self.suggestions:
            _required_text("suggestion", suggestion, limit=4_096)
        if len(set(self.suggestions)) != len(self.suggestions):
            raise ValueError("suggestions cannot contain duplicates")
        _bounded_json(
            list(self.suggestions),
            label="review task suggestions",
            maximum_bytes=MAX_REVIEW_TASK_JSON_BYTES,
        )
        _optional_text("supersedes_task_id", self.supersedes_task_id)
        if self.supersedes_task_id == self.task_id:
            raise ValueError("a review task cannot supersede itself")
        if (self.supersedes_task_id is None) != (self.task_version == 1):
            raise ValueError("task_version 1 cannot supersede a task and later versions must do so")

    @property
    def priority(self) -> float:
        return self.impact * self.uncertainty * self.irreversibility

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": REVIEW_TASK_CONTRACT_SCHEMA_VERSION,
            "kind": "review_task",
            "task_id": self.task_id,
            "logical_key": self.logical_key,
            "task_version": self.task_version,
            "task_type": self.task_type,
            "scope": self.scope,
            "source_kind": self.source_kind,
            "source_input_id": self.source_input_id,
            "snapshot": self.snapshot.to_dict(),
            "evidence": [item.to_dict() for item in self.evidence],
            "reason_code": self.reason_code,
            "uncertainty_detail": self.uncertainty_detail.to_dict(),
            "impact": self.impact,
            "uncertainty": self.uncertainty,
            "irreversibility": self.irreversibility,
            "priority": self.priority,
            "priority_algorithm": REVIEW_TASK_PRIORITY_ALGORITHM,
            "suggestions": list(self.suggestions),
            "supersedes_task_id": self.supersedes_task_id,
            "created_ns": self.created_ns,
        }


@dataclass(frozen=True, slots=True)
class ReviewTaskPublication:
    batch_id: str
    batch_key: str
    fence: ReviewTaskSourceFence
    cursor_before: CanonicalJsonObject | None
    cursor_after: CanonicalJsonObject | None
    inputs: tuple[ReviewTaskInput, ...]
    tasks: tuple[ReviewTaskDraft, ...]
    coverage: ReviewTaskCoverage
    producer_signature: str
    confirmed_ns: int
    evidence_complete: bool = True
    evidence_reason: str | None = None

    def __post_init__(self) -> None:
        _required_text("batch_id", self.batch_id)
        _required_text("batch_key", self.batch_key)
        _required_text(
            "producer_signature",
            self.producer_signature,
            limit=MAX_REVIEW_TASK_SELECTOR_CHARS,
        )
        _positive_integer("confirmed_ns", self.confirmed_ns)
        if not isinstance(self.evidence_complete, bool):
            raise ValueError("evidence_complete must be a bool")
        _optional_text("evidence_reason", self.evidence_reason, limit=512)
        if self.evidence_complete != (self.evidence_reason is None):
            raise ValueError(
                "complete evidence cannot have a reason and partial evidence requires one"
            )
        if not isinstance(self.fence, ReviewTaskSourceFence):
            raise ValueError("fence must be a ReviewTaskSourceFence")
        if self.cursor_before is not None and not isinstance(
            self.cursor_before, CanonicalJsonObject
        ):
            raise ValueError("cursor_before must be a CanonicalJsonObject when present")
        if self.cursor_after is not None and not isinstance(self.cursor_after, CanonicalJsonObject):
            raise ValueError("cursor_after must be a CanonicalJsonObject when present")
        if not isinstance(self.coverage, ReviewTaskCoverage):
            raise ValueError("coverage must be a ReviewTaskCoverage")
        if self.coverage is ReviewTaskCoverage.PARTIAL:
            cursor_after = self.cursor_after
            if cursor_after is None:
                raise ValueError("partial review publication requires cursor_after")
            if (
                self.cursor_before is not None
                and self.cursor_before.payload_json == cursor_after.payload_json
            ):
                raise ValueError("partial review publication must advance its cursor")
        if not isinstance(self.inputs, tuple) or any(
            not isinstance(item, ReviewTaskInput) for item in self.inputs
        ):
            raise ValueError("inputs must be an immutable tuple of ReviewTaskInput values")
        if not isinstance(self.tasks, tuple) or any(
            not isinstance(item, ReviewTaskDraft) for item in self.tasks
        ):
            raise ValueError("tasks must be an immutable tuple of ReviewTaskDraft values")
        if len(self.inputs) > MAX_REVIEW_TASK_INPUTS_PER_PAGE:
            raise ValueError(
                f"review task publication cannot exceed {MAX_REVIEW_TASK_INPUTS_PER_PAGE} inputs"
            )
        if len(self.tasks) > MAX_REVIEW_TASKS_PER_PAGE:
            raise ValueError(
                f"review task publication cannot exceed {MAX_REVIEW_TASKS_PER_PAGE} tasks"
            )
        if len(self.tasks) > len(self.inputs):
            raise ValueError("selected tasks cannot exceed scanned inputs")
        input_ids = tuple(item.input_id for item in self.inputs)
        if len(set(input_ids)) != len(input_ids):
            raise ValueError("review task publication input identifiers cannot repeat")
        task_ids = tuple(item.task_id for item in self.tasks)
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("review task publication task identifiers cannot repeat")
        logical_keys = tuple(item.logical_key for item in self.tasks)
        if len(set(logical_keys)) != len(logical_keys):
            raise ValueError("one publication cannot contain multiple versions of a logical task")
        selected_inputs = tuple(item.source_input_id for item in self.tasks)
        if len(set(selected_inputs)) != len(selected_inputs):
            raise ValueError("one publication cannot select an input more than once")
        available_inputs = set(input_ids)
        for task in self.tasks:
            if task.scope != self.fence.scope or task.task_type != self.fence.task_type:
                raise ValueError("task scope/type must match the publication source fence")
            if task.source_input_id not in available_inputs:
                raise ValueError("every review task must bind one exact page input")
            if task.created_ns > self.confirmed_ns:
                raise ValueError("a review task cannot be created after its batch confirmation")
        self.to_json()

    @property
    def scanned_count(self) -> int:
        return len(self.inputs)

    @property
    def selected_count(self) -> int:
        return len(self.tasks)

    @property
    def page_size(self) -> int:
        return self.scanned_count

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": REVIEW_TASK_CONTRACT_SCHEMA_VERSION,
            "kind": "review_task_publication",
            "batch_id": self.batch_id,
            "batch_key": self.batch_key,
            **self.fence.to_dict(),
            "cursor_before": (None if self.cursor_before is None else self.cursor_before.to_dict()),
            "cursor_after": None if self.cursor_after is None else self.cursor_after.to_dict(),
            "inputs": [item.to_dict() for item in self.inputs],
            "tasks": [item.to_dict() for item in self.tasks],
            "page_size": self.page_size,
            "scanned_count": self.scanned_count,
            "selected_count": self.selected_count,
            "coverage": self.coverage.value,
            "evidence_complete": self.evidence_complete,
            "evidence_reason": self.evidence_reason,
            "producer_signature": self.producer_signature,
            "confirmed_ns": self.confirmed_ns,
        }

    def to_json(self) -> str:
        return _bounded_json(
            self.to_dict(),
            label="review task publication",
            maximum_bytes=MAX_REVIEW_TASK_PUBLICATION_BYTES,
        )


@dataclass(frozen=True, slots=True)
class ReviewTaskSourcePublication:
    """Append-only receipt whose latest revision atomically closes one source view."""

    publication_id: str
    publication_key: str
    fence: ReviewTaskSourceFence
    batch_id: str
    previous_publication_id: str | None
    revision: int
    confirmed_ns: int

    def __post_init__(self) -> None:
        _required_text("publication_id", self.publication_id)
        _required_text("publication_key", self.publication_key)
        if not isinstance(self.fence, ReviewTaskSourceFence):
            raise ValueError("fence must be a ReviewTaskSourceFence")
        _required_text("batch_id", self.batch_id)
        _optional_text("previous_publication_id", self.previous_publication_id)
        _positive_integer("revision", self.revision)
        _positive_integer("confirmed_ns", self.confirmed_ns)
        if (self.previous_publication_id is None) != (self.revision == 1):
            raise ValueError(
                "source publication revision 1 has no predecessor and later revisions require one"
            )
        self.to_json()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": REVIEW_TASK_CONTRACT_SCHEMA_VERSION,
            "kind": "review_task_source_publication",
            "publication_id": self.publication_id,
            "publication_key": self.publication_key,
            **self.fence.to_dict(),
            "batch_id": self.batch_id,
            "previous_publication_id": self.previous_publication_id,
            "revision": self.revision,
            "confirmed_ns": self.confirmed_ns,
        }

    def to_json(self) -> str:
        return _bounded_json(
            self.to_dict(),
            label="review task source publication",
            maximum_bytes=MAX_REVIEW_TASK_JSON_BYTES,
        )


@dataclass(frozen=True, slots=True)
class ReviewTaskEvent:
    event_id: str
    event_key: str
    task_id: str
    sequence: int
    previous_event_id: str | None
    from_state: ReviewTaskState | None
    to_state: ReviewTaskState
    actor_kind: ReviewTaskActorKind
    actor_id: str
    provenance: CanonicalJsonObject
    decision: CanonicalJsonObject | None
    note: str | None
    observed_ns: int
    recorded_ns: int

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("event_key", self.event_key),
            ("task_id", self.task_id),
        ):
            _required_text(name, value)
        _positive_integer("sequence", self.sequence)
        _optional_text("previous_event_id", self.previous_event_id)
        if self.from_state is not None and not isinstance(self.from_state, ReviewTaskState):
            raise ValueError("from_state must be a ReviewTaskState when present")
        if not isinstance(self.to_state, ReviewTaskState):
            raise ValueError("to_state must be a ReviewTaskState")
        if not isinstance(self.actor_kind, ReviewTaskActorKind):
            raise ValueError("actor_kind must be a ReviewTaskActorKind")
        _required_text("actor_id", self.actor_id)
        if not isinstance(self.provenance, CanonicalJsonObject) or not self.provenance.to_dict():
            raise ValueError("review event provenance must be non-empty")
        if self.decision is not None and not isinstance(self.decision, CanonicalJsonObject):
            raise ValueError("decision must be a CanonicalJsonObject when present")
        if self.note is not None:
            _required_text("note", self.note, limit=MAX_REVIEW_TASK_NOTE_BYTES)
            if len(self.note.encode("utf-8")) > MAX_REVIEW_TASK_NOTE_BYTES:
                raise ValueError(f"note exceeds the {MAX_REVIEW_TASK_NOTE_BYTES}-byte limit")
        _positive_integer("observed_ns", self.observed_ns)
        _positive_integer("recorded_ns", self.recorded_ns)
        if self.recorded_ns < self.observed_ns:
            raise ValueError("recorded_ns cannot precede observed_ns")
        initial = self.sequence == 1
        if initial:
            if self.previous_event_id is not None or self.from_state is not None:
                raise ValueError("initial review event cannot claim a predecessor")
            if self.to_state is not ReviewTaskState.OPEN:
                raise ValueError("initial review event must open the task")
        else:
            if self.previous_event_id is None or self.from_state is None:
                raise ValueError("non-initial review event requires its predecessor and state")
            if (self.from_state, self.to_state) not in _ALLOWED_TRANSITIONS:
                raise ValueError("review task state transition is not allowed")
        if self.to_state in {ReviewTaskState.RESOLVED, ReviewTaskState.DISMISSED}:
            if self.actor_kind is not ReviewTaskActorKind.HUMAN or self.decision is None:
                raise ValueError("resolved/dismissed review events require a human decision")
        elif self.to_state in {ReviewTaskState.OPEN, ReviewTaskState.IN_REVIEW}:
            if self.decision is not None:
                raise ValueError("open/in_review events cannot include a terminal decision")

    @property
    def terminal(self) -> bool:
        return self.to_state in _TERMINAL_STATES

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": REVIEW_TASK_CONTRACT_SCHEMA_VERSION,
            "kind": "review_task_event",
            "event_id": self.event_id,
            "event_key": self.event_key,
            "task_id": self.task_id,
            "sequence": self.sequence,
            "previous_event_id": self.previous_event_id,
            "from_state": None if self.from_state is None else self.from_state.value,
            "to_state": self.to_state.value,
            "actor_kind": self.actor_kind.value,
            "actor_id": self.actor_id,
            "provenance": self.provenance.to_dict(),
            "decision": None if self.decision is None else self.decision.to_dict(),
            "note": self.note,
            "observed_ns": self.observed_ns,
            "recorded_ns": self.recorded_ns,
        }


@dataclass(frozen=True, slots=True)
class ReviewTaskTransition:
    event_id: str
    event_key: str
    task_id: str
    expected_event_id: str
    expected_state: ReviewTaskState
    to_state: ReviewTaskState
    actor_kind: ReviewTaskActorKind
    actor_id: str
    provenance: CanonicalJsonObject
    decision: CanonicalJsonObject | None
    note: str | None
    observed_ns: int
    recorded_ns: int

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("event_key", self.event_key),
            ("task_id", self.task_id),
            ("expected_event_id", self.expected_event_id),
        ):
            _required_text(name, value)
        if self.to_state is ReviewTaskState.SUPERSEDED:
            raise ValueError("SUPERSEDED is reserved for receipt-backed repository transitions")
        if (self.expected_state, self.to_state) not in _ALLOWED_TRANSITIONS:
            raise ValueError("review task state transition is not allowed")
        # Exercise all terminal decision/actor and timestamp invariants now; the
        # repository will supply sequence and predecessor after its CAS read.
        ReviewTaskEvent(
            event_id=self.event_id,
            event_key=self.event_key,
            task_id=self.task_id,
            sequence=2,
            previous_event_id=self.expected_event_id,
            from_state=self.expected_state,
            to_state=self.to_state,
            actor_kind=self.actor_kind,
            actor_id=self.actor_id,
            provenance=self.provenance,
            decision=self.decision,
            note=self.note,
            observed_ns=self.observed_ns,
            recorded_ns=self.recorded_ns,
        )


@dataclass(frozen=True, slots=True)
class ReviewTaskRecord:
    task: ReviewTaskDraft
    source: ReviewTaskInput
    source_snapshot_fingerprint: str
    batch_id: str
    selector_signature: str
    current_event: ReviewTaskEvent

    def __post_init__(self) -> None:
        if not isinstance(self.task, ReviewTaskDraft):
            raise ValueError("task must be a ReviewTaskDraft")
        if not isinstance(self.source, ReviewTaskInput):
            raise ValueError("source must be a ReviewTaskInput")
        if not isinstance(self.current_event, ReviewTaskEvent):
            raise ValueError("current_event must be a ReviewTaskEvent")
        if self.source.input_id != self.task.source_input_id:
            raise ValueError("review task record source binding disagrees with the task")
        if self.current_event.task_id != self.task.task_id:
            raise ValueError("review task record event belongs to another task")
        _validate_source_snapshot_fingerprint(self.source_snapshot_fingerprint)
        _required_text("batch_id", self.batch_id)
        _required_text(
            "selector_signature",
            self.selector_signature,
            limit=MAX_REVIEW_TASK_SELECTOR_CHARS,
        )

    @property
    def state(self) -> ReviewTaskState:
        return self.current_event.to_state


@dataclass(frozen=True, slots=True)
class ReviewTaskVersionHead:
    """Latest durable version/event for one logical task key."""

    logical_key: str
    task_id: str
    task_version: int
    state: ReviewTaskState
    event_id: str
    source_snapshot_fingerprint: str
    source_input_fingerprint: str
    selector_signature: str
    decision: CanonicalJsonObject | None

    def __post_init__(self) -> None:
        _required_text("logical_key", self.logical_key, limit=MAX_REVIEW_TASK_LOGICAL_KEY_CHARS)
        _required_text("task_id", self.task_id)
        _positive_integer("task_version", self.task_version)
        if not isinstance(self.state, ReviewTaskState):
            raise ValueError("state must be a ReviewTaskState")
        _required_text("event_id", self.event_id)
        _validate_source_snapshot_fingerprint(self.source_snapshot_fingerprint)
        _required_text("source_input_fingerprint", self.source_input_fingerprint)
        _required_text(
            "selector_signature",
            self.selector_signature,
            limit=MAX_REVIEW_TASK_SELECTOR_CHARS,
        )
        if self.decision is not None and not isinstance(self.decision, CanonicalJsonObject):
            raise ValueError("decision must be a CanonicalJsonObject when present")


@dataclass(frozen=True, slots=True)
class ReviewTaskListCursor:
    priority: float
    created_ns: int
    task_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "priority", _factor("priority", self.priority))
        _positive_integer("created_ns", self.created_ns)
        _required_text("task_id", self.task_id)


@dataclass(frozen=True, slots=True)
class ReviewTaskRecordPage:
    items: tuple[ReviewTaskRecord, ...]
    next_cursor: ReviewTaskListCursor | None

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, ReviewTaskRecord) for item in self.items
        ):
            raise ValueError("items must be an immutable tuple of ReviewTaskRecord values")
        if self.next_cursor is not None and not isinstance(self.next_cursor, ReviewTaskListCursor):
            raise ValueError("next_cursor must be a ReviewTaskListCursor when present")

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


@dataclass(frozen=True, slots=True)
class ReviewTaskScanProgress:
    progress_id: str
    fence: ReviewTaskSourceFence
    cursor: CanonicalJsonObject | None
    last_batch_id: str
    scanned_count: int
    selected_count: int
    complete: bool
    revision: int
    created_ns: int
    updated_ns: int
    evidence_complete: bool = True
    evidence_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fence, ReviewTaskSourceFence):
            raise ValueError("fence must be a ReviewTaskSourceFence")
        if self.cursor is not None and not isinstance(self.cursor, CanonicalJsonObject):
            raise ValueError("cursor must be a CanonicalJsonObject when present")
        _required_text("progress_id", self.progress_id)
        _required_text("last_batch_id", self.last_batch_id)
        _non_negative_integer("scanned_count", self.scanned_count)
        _non_negative_integer("selected_count", self.selected_count)
        if self.selected_count > self.scanned_count:
            raise ValueError("selected_count cannot exceed scanned_count")
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be a bool")
        if not isinstance(self.evidence_complete, bool):
            raise ValueError("evidence_complete must be a bool")
        _optional_text("evidence_reason", self.evidence_reason, limit=512)
        if self.evidence_complete != (self.evidence_reason is None):
            raise ValueError(
                "complete evidence cannot have a reason and partial evidence requires one"
            )
        _positive_integer("revision", self.revision)
        _positive_integer("created_ns", self.created_ns)
        _positive_integer("updated_ns", self.updated_ns)
        if self.updated_ns < self.created_ns:
            raise ValueError("progress updated_ns cannot precede created_ns")
        if not self.complete and self.cursor is None:
            raise ValueError("partial progress requires a resumable cursor")


@dataclass(frozen=True, slots=True)
class ReviewTaskPublicationResult:
    batch_id: str
    task_ids: tuple[str, ...]
    progress: ReviewTaskScanProgress
    idempotent: bool


@dataclass(frozen=True, slots=True)
class ReviewTaskEventResult:
    event: ReviewTaskEvent
    idempotent: bool


__all__ = (
    "MAX_REVIEW_TASKS_PER_PAGE",
    "MAX_REVIEW_TASK_INPUTS_PER_PAGE",
    "MAX_REVIEW_TASK_READ_PAGE",
    "REVIEW_TASK_CONTRACT_SCHEMA_VERSION",
    "REVIEW_TASK_FRAMEWORK_SCHEMA_VERSION",
    "REVIEW_TASK_PRIORITY_ALGORITHM",
    "REVIEW_TASK_SOURCE_FINGERPRINT_PREFIX",
    "CanonicalJsonObject",
    "ReviewTaskActorKind",
    "ReviewTaskCoverage",
    "ReviewTaskDraft",
    "ReviewTaskEvent",
    "ReviewTaskEventResult",
    "ReviewTaskInput",
    "ReviewTaskListCursor",
    "ReviewTaskPublication",
    "ReviewTaskPublicationResult",
    "ReviewTaskRecord",
    "ReviewTaskRecordPage",
    "ReviewTaskScanProgress",
    "ReviewTaskSourceFence",
    "ReviewTaskSourcePublication",
    "ReviewTaskState",
    "ReviewTaskTransition",
    "ReviewTaskVersionHead",
    "review_task_source_snapshot_fingerprint",
)


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.review_task_contracts")
