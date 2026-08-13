"""Typed contracts shared by Neocortex semantic indexing components.

The semantic layer deliberately distinguishes an input modality from a vector
space.  Two encoders may be comparable (for example CLIP text and vision) only
when they declare the same explicit ``vector_space`` and dimensionality.
"""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import xxhash


# region [01] Stable enums and JSON policy


class EmbeddingModality(StrEnum):
    """Physical kind of input accepted by an embedding encoder."""

    TEXT = "text"
    IMAGE = "image"


class EmbeddingRole(StrEnum):
    """Semantic role requested from an encoder."""

    QUERY = "query"
    PASSAGE = "passage"
    IMAGE = "image"


class VectorDType(StrEnum):
    """Compact on-disk representation used by the exact fallback index."""

    FLOAT16 = "float16"
    FLOAT32 = "float32"


class SemanticEntityKind(StrEnum):
    """Entities that can own a durable embedding reference."""

    TEXT_CHUNK = "text_chunk"
    IMAGE_ITEM = "image_item"


class CalibrationStatus(StrEnum):
    """Whether semantic scores have been calibrated against human feedback."""

    UNCALIBRATED = "uncalibrated"
    CALIBRATED = "calibrated"


class EvidenceDisposition(StrEnum):
    """Decision state; model-only output is always advisory."""

    ADVISORY = "advisory"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


def canonical_json(value: object | None) -> str:
    """Serialize JSON-compatible provenance without hiding invalid values."""

    return json.dumps(
        {} if value is None else value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _require_non_blank_string(
    name: str,
    value: object,
    *,
    blank_message: str | None = None,
) -> str:
    """Return one validated string without leaking ``AttributeError``."""

    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(blank_message or f"{name} cannot be blank")
    return value


def _optional_string_is_blank(name: str, value: object | None) -> bool:
    """Validate an optional string and report whether it has visible content."""

    if value is None:
        return True
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string when present")
    return not value.strip()


def _require_dimensions(value: object) -> int:
    """Reject bool and non-integers before using a bounded vector dimension."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 65_536
    ):
        raise ValueError("dimensions must be an integer between 1 and 65536")
    return value


# endregion [01]


# region [02] Non-cryptographic content identity


_FINGERPRINT_GUARD_SEED = 0x4E454F43


@dataclass(frozen=True, slots=True)
class ContentFingerprint:
    """Bounded collision guard for content reuse, based only on XXH3.

    The byte count and an independently seeded XXH3-64 guard accompany the
    primary XXH3-128 digest.  This is not a security primitive; it is an
    explicit, fast identity for cache invalidation and embedding reuse.
    """

    xxh3_128: str
    byte_count: int
    xxh3_64_guard: str

    def __post_init__(self) -> None:
        if len(self.xxh3_128) != 32 or any(
            character not in "0123456789abcdef" for character in self.xxh3_128
        ):
            raise ValueError("xxh3_128 must be 32 lowercase hexadecimal characters")
        if len(self.xxh3_64_guard) != 16 or any(
            character not in "0123456789abcdef" for character in self.xxh3_64_guard
        ):
            raise ValueError(
                "xxh3_64_guard must be 16 lowercase hexadecimal characters"
            )
        if self.byte_count < 0:
            raise ValueError("byte_count cannot be negative")


def fingerprint_bytes(payload: bytes | bytearray | memoryview) -> ContentFingerprint:
    """Return the version-neutral XXH3 identity of an in-memory payload."""

    view = memoryview(payload)
    return ContentFingerprint(
        xxh3_128=xxhash.xxh3_128_hexdigest(view),
        byte_count=view.nbytes,
        xxh3_64_guard=xxhash.xxh3_64_hexdigest(
            view,
            seed=_FINGERPRINT_GUARD_SEED,
        ),
    )


def fingerprint_text(text: str) -> ContentFingerprint:
    """Fingerprint UTF-8 text exactly as it will be submitted to a backend."""

    return fingerprint_bytes(text.encode("utf-8"))


def fingerprint_chunks(chunks: Iterable[bytes]) -> ContentFingerprint:
    """Fingerprint a byte stream incrementally, without joining it in memory."""

    primary = xxhash.xxh3_128()
    guard = xxhash.xxh3_64(seed=_FINGERPRINT_GUARD_SEED)
    byte_count = 0
    for chunk in chunks:
        primary.update(chunk)
        guard.update(chunk)
        byte_count += len(chunk)
    return ContentFingerprint(
        xxh3_128=primary.hexdigest(),
        byte_count=byte_count,
        xxh3_64_guard=guard.hexdigest(),
    )


# endregion [02]


# region [03] Model, source and request contracts


@dataclass(frozen=True, slots=True)
class EmbeddingModelSpec:
    """Versioned encoder contract stored alongside every vector payload."""

    model_signature: str
    vector_space: str
    modality: EmbeddingModality
    model_id: str
    model_version: str
    dimensions: int
    provider: str
    supported_roles: tuple[EmbeddingRole, ...]
    vector_dtype: VectorDType = VectorDType.FLOAT16
    normalization: str = "l2"
    distance: str = "cosine"
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("model_signature", self.model_signature),
            ("vector_space", self.vector_space),
            ("model_id", self.model_id),
            ("model_version", self.model_version),
            ("provider", self.provider),
        ):
            _require_non_blank_string(name, value)
        _require_dimensions(self.dimensions)
        if self.normalization != "l2" or self.distance != "cosine":
            raise ValueError("the semantic fallback currently requires l2/cosine")
        if not self.supported_roles:
            raise ValueError("supported_roles cannot be empty")
        if len(set(self.supported_roles)) != len(self.supported_roles):
            raise ValueError("supported_roles cannot contain duplicates")
        valid_roles = (
            {EmbeddingRole.QUERY, EmbeddingRole.PASSAGE}
            if self.modality is EmbeddingModality.TEXT
            else {EmbeddingRole.IMAGE}
        )
        if any(role not in valid_roles for role in self.supported_roles):
            raise ValueError("supported_roles are incompatible with modality")
        canonical_json(self.provenance)


@dataclass(frozen=True, slots=True)
class SemanticItem:
    """Stable source identity independent from a mutable filesystem path."""

    item_id: str
    source_kind: str
    source_identity: str
    identity_version: str
    fingerprint: ContentFingerprint
    path: str | None = None
    provenance: Mapping[str, object] = field(default_factory=dict)
    source_revision: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("item_id", self.item_id),
            ("source_kind", self.source_kind),
            ("source_identity", self.source_identity),
            ("identity_version", self.identity_version),
        ):
            _require_non_blank_string(name, value)
        canonical_json(self.source_revision)
        canonical_json(self.provenance)


@dataclass(frozen=True, slots=True)
class TextChunk:
    """Bounded natural unit submitted to a passage encoder."""

    chunk_id: str
    item_id: str
    ordinal: int
    section_kind: str
    section_id: str
    start_char: int
    end_char: int
    text: str
    fingerprint: ContentFingerprint
    chunking_signature: str
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("chunk_id", self.chunk_id),
            ("item_id", self.item_id),
            ("section_kind", self.section_kind),
            ("section_id", self.section_id),
            ("chunking_signature", self.chunking_signature),
        ):
            _require_non_blank_string(name, value)
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise ValueError("ordinal must be an integer")
        if self.ordinal < 0:
            raise ValueError("ordinal cannot be negative")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (self.start_char, self.end_char)
        ):
            raise ValueError("chunk offsets must be integers")
        if self.start_char < 0 or self.end_char <= self.start_char:
            raise ValueError("chunk offsets must describe a non-empty range")
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        if not self.text:
            raise ValueError("text cannot be empty")
        if self.end_char - self.start_char < len(self.text):
            raise ValueError(
                "chunk locator span cannot be shorter than normalized text"
            )
        if fingerprint_text(self.text) != self.fingerprint:
            raise ValueError("fingerprint does not match chunk text")
        canonical_json(self.provenance)


@dataclass(frozen=True, slots=True)
class TextSection:
    """Natural source boundary such as a PDF page or audio segment."""

    section_kind: str
    section_id: str
    text: str
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("section_kind", self.section_kind),
            ("section_id", self.section_id),
        ):
            _require_non_blank_string(
                name,
                value,
                blank_message="section_kind and section_id cannot be blank",
            )
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        canonical_json(self.provenance)


@dataclass(frozen=True, slots=True)
class EmbeddingRequest:
    """One bounded backend request with exactly one compatible payload."""

    request_id: str
    role: EmbeddingRole
    fingerprint: ContentFingerprint
    text: str | None = None
    image_path: Path | None = None
    image_bytes: bytes | None = None
    source_revision: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_blank_string("request_id", self.request_id)
        payload_count = sum(
            value is not None
            for value in (self.text, self.image_path, self.image_bytes)
        )
        if payload_count != 1:
            raise ValueError("exactly one request payload must be supplied")
        if self.role in {EmbeddingRole.QUERY, EmbeddingRole.PASSAGE}:
            if self.text is None:
                raise ValueError("text roles require a text payload")
            if not isinstance(self.text, str):
                raise ValueError("text must be a string")
            if fingerprint_text(self.text) != self.fingerprint:
                raise ValueError("fingerprint does not match request text")
        elif self.text is not None:
            raise ValueError("image roles cannot carry text")
        if self.image_bytes is not None:
            if fingerprint_bytes(self.image_bytes) != self.fingerprint:
                raise ValueError("fingerprint does not match request image bytes")
        canonical_json(self.source_revision)


@dataclass(frozen=True, slots=True)
class BackendEmbedding:
    """Validated backend output before compact persistence."""

    request_id: str
    vector: tuple[float, ...]
    provenance: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LabelPrototype:
    """Versioned ontology label encoded in an explicit compatible space."""

    prototype_id: str
    ontology_id: str
    ontology_version: str
    concept_id: str
    prototype_version: str
    model_signature: str
    vector_space: str
    text: str
    fingerprint: ContentFingerprint
    calibration_status: CalibrationStatus = CalibrationStatus.UNCALIBRATED
    feedback_reference: str | None = None
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("prototype_id", self.prototype_id),
            ("ontology_id", self.ontology_id),
            ("ontology_version", self.ontology_version),
            ("concept_id", self.concept_id),
            ("prototype_version", self.prototype_version),
            ("model_signature", self.model_signature),
            ("vector_space", self.vector_space),
            ("text", self.text),
        ):
            _require_non_blank_string(name, value)
        if fingerprint_text(self.text) != self.fingerprint:
            raise ValueError("fingerprint does not match prototype text")
        feedback_missing = _optional_string_is_blank(
            "feedback_reference",
            self.feedback_reference,
        )
        if self.calibration_status is CalibrationStatus.CALIBRATED and feedback_missing:
            raise ValueError("calibrated prototypes require a feedback reference")
        canonical_json(self.provenance)


@dataclass(frozen=True, slots=True)
class StoredLabelPrototype:
    prototype: LabelPrototype
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class SemanticEvidence:
    """Auditable item-to-concept suggestion; never an automatic file action."""

    item_id: str
    source_entity_id: str
    ontology_id: str
    ontology_version: str
    concept_id: str
    prototype_id: str
    query_model_signature: str
    indexed_model_signature: str
    vector_space: str
    score: float
    rank: int
    generation_id: int | None = None
    calibration_status: CalibrationStatus = CalibrationStatus.UNCALIBRATED
    disposition: EvidenceDisposition = EvidenceDisposition.ADVISORY
    feedback_reference: str | None = None
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("item_id", self.item_id),
            ("source_entity_id", self.source_entity_id),
            ("ontology_id", self.ontology_id),
            ("ontology_version", self.ontology_version),
            ("concept_id", self.concept_id),
            ("prototype_id", self.prototype_id),
            ("query_model_signature", self.query_model_signature),
            ("indexed_model_signature", self.indexed_model_signature),
            ("vector_space", self.vector_space),
        ):
            _require_non_blank_string(name, value)
        if not math.isfinite(self.score) or not -1.0 <= self.score <= 1.0:
            raise ValueError("evidence score must be finite and between -1 and 1")
        if self.rank < 1:
            raise ValueError("evidence rank must be positive")
        if (
            self.calibration_status is CalibrationStatus.UNCALIBRATED
            and self.disposition is not EvidenceDisposition.ADVISORY
        ):
            raise ValueError("uncalibrated model evidence must remain advisory")
        feedback_required = (
            self.calibration_status is CalibrationStatus.CALIBRATED
            or self.disposition is not EvidenceDisposition.ADVISORY
        )
        feedback_missing = _optional_string_is_blank(
            "feedback_reference",
            self.feedback_reference,
        )
        if feedback_required and feedback_missing:
            raise ValueError(
                "calibrated, confirmed or rejected evidence requires feedback"
            )
        canonical_json(self.provenance)


# endregion [03]


# region [04] Durable job and search result contracts


@dataclass(frozen=True, slots=True)
class EmbeddingJobLease:
    job_id: int
    generation_id: int
    model_signature: str
    vector_space: str
    modality: EmbeddingModality
    role: EmbeddingRole
    entity_kind: SemanticEntityKind
    entity_id: str
    item_id: str
    fingerprint: ContentFingerprint
    attempt: int
    lease_until_ns: int
    text: str | None = None
    image_path: Path | None = None
    source_revision: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.text is None) == (self.image_path is None):
            raise ValueError("a job lease must expose exactly one source payload")
        if self.modality is EmbeddingModality.TEXT and self.text is None:
            raise ValueError("text leases require text")
        if self.modality is EmbeddingModality.IMAGE and self.image_path is None:
            raise ValueError("image leases require an image path")
        canonical_json(self.source_revision)


@dataclass(frozen=True, slots=True)
class GenerationSummary:
    generation_id: int
    model_signature: str
    processing_signature: str
    status: str
    pending: int
    leased: int
    done: int
    errors: int
    stale: int
    cursor: Mapping[str, object]

    @property
    def unfinished(self) -> int:
        return self.pending + self.leased


@dataclass(frozen=True, slots=True)
class ExactSearchQuery:
    """Query vector and explicit compatibility boundary for exact search."""

    query_model_signature: str
    vector_space: str
    dimensions: int
    vector: Sequence[float]
    target_modality: EmbeddingModality
    indexed_model_signatures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("query_model_signature", self.query_model_signature),
            ("vector_space", self.vector_space),
        ):
            _require_non_blank_string(
                name,
                value,
                blank_message=(
                    "query model signature and vector space cannot be blank"
                ),
            )
        validate_vector(self.vector, self.dimensions)


@dataclass(frozen=True, slots=True)
class SearchHit:
    ref_id: int
    entity_id: str
    item_id: str
    indexed_model_signature: str
    vector_space: str
    modality: EmbeddingModality
    score: float
    generation_id: int
    provenance: Mapping[str, object] = field(default_factory=dict)
    query_model_signature: str | None = None

    def __post_init__(self) -> None:
        if self.query_model_signature is not None:
            _require_non_blank_string(
                "query_model_signature",
                self.query_model_signature,
                blank_message=("query_model_signature cannot be blank when present"),
            )


@dataclass(frozen=True, slots=True)
class ResolvedSearchHit:
    """A search hit joined back to bounded human-readable source evidence."""

    hit: SearchHit
    path: str | None
    source_kind: str
    source_identity: str
    section_kind: str | None
    section_id: str | None
    start_char: int | None
    end_char: int | None
    snippet: str | None
    source_revision: Mapping[str, object] = field(default_factory=dict)
    section_provenance: Mapping[str, object] = field(default_factory=dict)
    source_status: str | None = None
    published_revision_id: int | None = None
    current_revision_id: int | None = None

    def __post_init__(self) -> None:
        if self.source_status is not None:
            _require_non_blank_string(
                "source_status",
                self.source_status,
                blank_message="source_status cannot be blank when present",
            )
        for name, value in (
            ("published_revision_id", self.published_revision_id),
            ("current_revision_id", self.current_revision_id),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise ValueError(f"{name} must be a positive integer when present")
        if self.current_revision_id is not None and self.published_revision_id is None:
            raise ValueError("current_revision_id requires a published_revision_id")
        canonical_json(self.source_revision)
        canonical_json(self.section_provenance)

    @property
    def source_revision_is_current(self) -> bool | None:
        """Return DB-local revision currency when immutable IDs are available."""

        if self.published_revision_id is None:
            return None
        return self.current_revision_id == self.published_revision_id


@dataclass(frozen=True, slots=True)
class ExactSearchPage:
    hits: tuple[SearchHit, ...]
    scanned: int
    next_cursor: int | None
    complete: bool


@dataclass(frozen=True, slots=True)
class ActiveEmbeddingRecord:
    """One current source-matched vector for single-pass prototype scoring."""

    ref_id: int
    entity_id: str
    item_id: str
    model_signature: str
    vector_space: str
    modality: EmbeddingModality
    vector: tuple[float, ...]
    generation_id: int
    provenance: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActiveEmbeddingPage:
    records: tuple[ActiveEmbeddingRecord, ...]
    next_cursor: int | None
    complete: bool


@dataclass(frozen=True, slots=True)
class FusionEvidence:
    ranking: str
    rank: int
    raw_score: float
    contribution: float
    entity_id: str
    indexed_model_signature: str
    query_model_signature: str | None = None

    def __post_init__(self) -> None:
        if self.query_model_signature is not None:
            _require_non_blank_string(
                "query_model_signature",
                self.query_model_signature,
                blank_message=("query_model_signature cannot be blank when present"),
            )


@dataclass(frozen=True, slots=True)
class FusedHit:
    item_id: str
    score: float
    evidence: tuple[FusionEvidence, ...]


# endregion [04]


# region [05] Vector validation and compact codec


def validate_vector(
    values: Sequence[float],
    dimensions: int,
) -> tuple[float, ...]:
    """Return finite floats with an exact dimension and non-zero L2 norm."""

    validated_dimensions = _require_dimensions(dimensions)
    if len(values) != validated_dimensions:
        raise ValueError(
            f"expected {validated_dimensions} vector values, received {len(values)}"
        )
    vector = tuple(float(value) for value in values)
    if any(not math.isfinite(value) for value in vector):
        raise ValueError("embedding vectors must contain only finite values")
    norm_squared = math.fsum(value * value for value in vector)
    if norm_squared <= 0.0 or not math.isfinite(norm_squared):
        raise ValueError("embedding vectors must have a finite non-zero L2 norm")
    return vector


def normalize_vector(
    values: Sequence[float],
    dimensions: int,
) -> tuple[tuple[float, ...], float]:
    """L2-normalize a vector and return its original norm."""

    vector = validate_vector(values, dimensions)
    norm = math.sqrt(math.fsum(value * value for value in vector))
    return tuple(value / norm for value in vector), norm


def encode_vector(
    values: Sequence[float],
    dimensions: int,
    dtype: VectorDType,
) -> tuple[bytes, float]:
    """Normalize and encode a vector using explicit little-endian storage."""

    validated_dimensions = _require_dimensions(dimensions)
    normalized, original_norm = normalize_vector(values, validated_dimensions)
    format_character = "e" if dtype is VectorDType.FLOAT16 else "f"
    try:
        payload = struct.pack(
            f"<{validated_dimensions}{format_character}",
            *normalized,
        )
    except (OverflowError, struct.error) as exc:
        raise ValueError("vector cannot be represented by the selected dtype") from exc
    return payload, original_norm


def decode_vector(
    payload: bytes,
    dimensions: int,
    dtype: VectorDType,
) -> tuple[float, ...]:
    """Decode a compact vector after checking its exact byte length."""

    validated_dimensions = _require_dimensions(dimensions)
    width = 2 if dtype is VectorDType.FLOAT16 else 4
    expected_bytes = validated_dimensions * width
    if len(payload) != expected_bytes:
        raise ValueError(
            f"invalid vector payload length: expected {expected_bytes}, got {len(payload)}"
        )
    format_character = "e" if dtype is VectorDType.FLOAT16 else "f"
    return tuple(struct.unpack(f"<{validated_dimensions}{format_character}", payload))


def cosine_similarity(
    left: Sequence[float],
    right: Sequence[float],
    dimensions: int,
) -> float:
    """Calculate cosine similarity without assuming quantized unit length."""

    left_vector = validate_vector(left, dimensions)
    right_vector = validate_vector(right, dimensions)
    numerator = math.fsum(
        left_value * right_value
        for left_value, right_value in zip(left_vector, right_vector, strict=True)
    )
    left_norm = math.sqrt(math.fsum(value * value for value in left_vector))
    right_norm = math.sqrt(math.fsum(value * value for value in right_vector))
    score = numerator / (left_norm * right_norm)
    return max(-1.0, min(1.0, score))


# endregion [05]
