"""Typed, advisory-only contracts for conservative file value review.

The contracts deliberately separate evidence from recommendation.  A
``ValueReviewItem`` is a review queue entry; it is never authorization to move,
archive, rename, or delete a file.
"""

from __future__ import annotations
import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any


VALUE_REVIEW_CONTRACT_VERSION = 1
MIN_VALUE_REVIEW_LIMIT = 1
MAX_VALUE_REVIEW_LIMIT = 1_000


class ValueReviewState(str, Enum):
    """Conservative recommendation states exposed by ``value-preview``."""

    KEEP = "keep"
    REVIEW_LOW_VALUE = "review_low_value"
    ARCHIVE_CANDIDATE = "archive_candidate"
    EXACT_DUPLICATE_CANDIDATE = "exact_duplicate_candidate"
    UNKNOWN = "unknown"


class ValueReviewAvailability(str, Enum):
    READY = "ready"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class ValueEvidenceStrength(str, Enum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"


class ValueOwnerHealth(str, Enum):
    HEALTHY = "healthy"
    PARTIAL = "partial"
    INCOMPATIBLE = "incompatible"
    CORRUPT = "corrupt"
    ENCRYPTED = "encrypted"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ValueDimensionName(str, Enum):
    EXACT_DUPLICATION = "exact_duplication"
    EXTRACTION_QUALITY = "extraction_quality"
    UNIQUENESS = "uniqueness"
    AGE = "age"
    SIZE = "size"
    PATH_TYPE = "path_type"
    COVERAGE = "coverage"
    CITATIONS = "citations"
    USAGE = "usage"


class ValueDimensionAssessment(str, Enum):
    PROTECT = "protect"
    SUPPORTS_EXACT_DUPLICATE_REVIEW = "supports_exact_duplicate_review"
    SUPPORTS_LOW_VALUE_REVIEW = "supports_low_value_review"
    SUPPORTS_ARCHIVE_REVIEW = "supports_archive_review"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ValueEvidenceFact:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class ValueEvidence:
    evidence_id: str
    owner: str
    kind: str
    strength: ValueEvidenceStrength
    publication_id: str | None = None
    record_id: str | None = None
    facts: tuple[ValueEvidenceFact, ...] = ()


@dataclass(frozen=True, slots=True)
class ValueProvenance:
    owner: str
    schema_version: int
    publication_id: str
    read_mode: str = "sqlite_query_only"


@dataclass(frozen=True, slots=True)
class ValueDimension:
    name: ValueDimensionName
    assessment: ValueDimensionAssessment
    value: str | int | float | None
    evidence_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ValueReviewPaths:
    """Existing SQLite owners used by the read-only preview.

    Paths may be absent.  Readers always use ``mode=ro`` and must never create
    any of them.
    """

    inventory: Path
    catalog: Path
    pdf: Path | None = None
    docx: Path | None = None
    office: Path | None = None
    text: Path | None = None
    audio: Path | None = None

    @classmethod
    def from_directory(cls, state_directory: Path) -> ValueReviewPaths:
        root = Path(state_directory).absolute()
        return cls(
            inventory=root / "dedup.sqlite3",
            catalog=root / "document_catalog.sqlite3",
            pdf=root / "pdf.sqlite3",
            docx=root / "docx.sqlite3",
            office=root / "office.sqlite3",
            text=root / "text.sqlite3",
            audio=root / "audio.sqlite3",
        )


@dataclass(frozen=True, slots=True)
class ValueReviewQuery:
    """Bounded scope and prefilters for a deterministic review preview."""

    limit: int = 100
    scope: str | None = None
    extensions: tuple[str, ...] = ()
    source_kinds: tuple[str, ...] = ()
    states: tuple[ValueReviewState, ...] = ()
    minimum_size_bytes: int | None = None
    maximum_size_bytes: int | None = None
    reference_time_ns: int | None = None
    protected_roots: tuple[str, ...] = ()

    def validate(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise ValueError("limit must be an integer between 1 and 1000")
        if not MIN_VALUE_REVIEW_LIMIT <= self.limit <= MAX_VALUE_REVIEW_LIMIT:
            raise ValueError("limit must be between 1 and 1000")
        for label, value in (
            ("minimum_size_bytes", self.minimum_size_bytes),
            ("maximum_size_bytes", self.maximum_size_bytes),
            ("reference_time_ns", self.reference_time_ns),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{label} must be a non-negative integer")
        if (
            self.minimum_size_bytes is not None
            and self.maximum_size_bytes is not None
            and self.minimum_size_bytes > self.maximum_size_bytes
        ):
            raise ValueError("minimum_size_bytes cannot exceed maximum_size_bytes")
        _validate_filter_values("extensions", self.extensions, maximum=64)
        _validate_filter_values("source_kinds", self.source_kinds, maximum=32)
        if any(not isinstance(value, ValueReviewState) for value in self.states):
            raise ValueError("states values must be ValueReviewState members")
        if len(set(self.states)) != len(self.states):
            raise ValueError("states must not contain duplicates")
        _validate_optional_scope("scope", self.scope)
        if len(self.protected_roots) > 64:
            raise ValueError("protected_roots accepts at most 64 values")
        for root in self.protected_roots:
            _validate_optional_scope("protected_roots", root)


def _validate_filter_values(label: str, values: tuple[str, ...], *, maximum: int) -> None:
    if len(values) > maximum:
        raise ValueError(f"{label} accepts at most {maximum} values")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ValueError(f"{label} values must be non-empty strings")
        key = value.strip().casefold()
        if key in normalized:
            raise ValueError(f"{label} must not contain duplicates")
        normalized.add(key)


def _validate_optional_scope(label: str, value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > 32_768:
        raise ValueError(f"{label} must be a non-empty path")
    candidate = value.strip()
    windows_absolute = len(candidate) >= 3 and candidate[1] == ":" and candidate[2] in "\\/"
    if not (candidate.startswith(("/", "\\\\")) or windows_absolute):
        raise ValueError(f"{label} must be absolute")


@dataclass(frozen=True, slots=True)
class ValueFileObservation:
    """Normalized facts accepted from SQLite or a future federated reader."""

    resource_id: str
    path: str
    size_bytes: int
    mtime_ns: int
    birthtime_ns: int
    owner_health: ValueOwnerHealth = ValueOwnerHealth.UNKNOWN
    source_kind: str | None = None
    source_status: str | None = None
    catalog_status: str | None = None
    catalog_uncertainty: str | None = None
    primary_project: str | None = None
    text_fingerprint: str | None = None
    text_duplicate_count: int | None = None
    exact_duplicate_role: str | None = None
    exact_duplicate_hash: str | None = None
    exact_duplicate_group_id: str | None = None
    exact_duplicate_keeper_id: str | None = None
    coverage_ratio: float | None = None
    citation_count: int | None = None
    usage_count: int | None = None
    coverage_evidence_ids: tuple[str, ...] = ()
    citation_evidence_ids: tuple[str, ...] = ()
    usage_evidence_ids: tuple[str, ...] = ()
    evidence: tuple[ValueEvidence, ...] = ()
    provenance: tuple[ValueProvenance, ...] = ()
    uncertainties: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ValueReviewItem:
    resource_id: str
    path: str
    state: ValueReviewState
    review_priority: int
    size_bytes: int
    source_kind: str | None
    dimensions: tuple[ValueDimension, ...]
    provenance: tuple[ValueProvenance, ...]
    evidence: tuple[ValueEvidence, ...]
    reasons: tuple[str, ...]
    uncertainties: tuple[str, ...]
    advisory_only: bool = True
    mutation_authorized: bool = False


@dataclass(frozen=True, slots=True)
class ValueReviewReport:
    availability: ValueReviewAvailability
    complete: bool
    reason: str | None
    candidate_count: int
    matched_count: int
    returned_count: int
    truncated: bool
    items: tuple[ValueReviewItem, ...]
    provenance: tuple[ValueProvenance, ...] = ()
    uncertainties: tuple[str, ...] = ()
    contract_version: int = VALUE_REVIEW_CONTRACT_VERSION
    operation: str = "value-preview"
    advisory_only: bool = True
    mutation_authorized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


__all__ = [
    "MAX_VALUE_REVIEW_LIMIT",
    "MIN_VALUE_REVIEW_LIMIT",
    "VALUE_REVIEW_CONTRACT_VERSION",
    "ValueDimension",
    "ValueDimensionAssessment",
    "ValueDimensionName",
    "ValueEvidence",
    "ValueEvidenceFact",
    "ValueEvidenceStrength",
    "ValueFileObservation",
    "ValueOwnerHealth",
    "ValueProvenance",
    "ValueReviewAvailability",
    "ValueReviewItem",
    "ValueReviewPaths",
    "ValueReviewQuery",
    "ValueReviewReport",
    "ValueReviewState",
]
