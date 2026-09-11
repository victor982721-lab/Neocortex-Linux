"""Stable public contracts and operational bounds for semantic services."""

from __future__ import annotations
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal as _Literal
from typing import Mapping

from .semantic_lexical import LexicalAvailability, LexicalRanking
from .semantic_models import (
    FusedHit,
    GenerationSummary,
    ResolvedSearchHit,
    SearchHit,
)
from .semantic_contract_validation import (
    validate_semantic_cost_calibration as _validate_semantic_cost_calibration,
    validate_semantic_plan as _validate_semantic_plan,
    validate_semantic_source_plan as _validate_semantic_source_plan,
    validate_semantic_workload_plan as _validate_semantic_workload_plan,
)

_validation_surface_dependencies = (json, math)


# region [01] Operational identity and bounded work

SEMANTIC_DATABASE_NAME = "semantic.sqlite3"
SEMANTIC_ONTOLOGY_ID = "neocortex-industrial"
SEMANTIC_PROTOTYPE_VERSION = "bilingual-domain-prototype-v1"
STAGING_BATCH_SIZE = 128
JOB_BATCH_SIZE = 128
DEFAULT_SEARCH_MAX_VECTORS = 500_000
MAX_SEMANTIC_CANDIDATE_HITS = 1_000
SEARCH_RESOLUTION_BATCH_SIZE = 500
MAX_LEXICAL_CANDIDATE_HITS = 1_000
WORKER_LEASE_SECONDS = 900.0
LEASE_HEARTBEAT_INTERVAL_SECONDS = WORKER_LEASE_SECONDS / 3.0
LEASE_HEARTBEAT_JOIN_TIMEOUT_SECONDS = 65.0
EVIDENCE_PAGE_SIZE = 256
MIN_ADVISORY_EVIDENCE_SCORE = 0.0
IMAGE_OCR_TEXT_CHANNEL = "image_ocr"
SEMANTIC_PLAN_TEXT_SOURCE_KINDS = frozenset(
    {"pdf", "docx", "xlsx", "pptx", "odt", "audio", "archive", "text", "code", "video"}
)

# endregion [01]


# region [02] Public immutable result contracts


@dataclass(frozen=True, slots=True)
class GenerationWorkResult:
    summary: GenerationSummary
    queued: int
    reused: int
    embedded: int
    failed: int
    stop_reason: str | None = field(default=None, kw_only=True)


@dataclass(frozen=True, slots=True)
class SemanticIndexResult:
    semantic_database: Path
    sources: tuple[str, ...]
    items_staged: int
    chunks_staged: int
    generations: tuple[GenerationWorkResult, ...]
    new_jobs_staged: int = 0
    execution_mode: _Literal[
        "enumerated", "exact_replay", "content_compatible_replay"
    ] = "enumerated"
    sources_reused: int = 0
    sources_enumerated: int = 0
    truncated: bool = False
    truncation_reason: str | None = None

    @property
    def errors(self) -> int:
        return sum(result.summary.errors for result in self.generations)

    @property
    def incomplete(self) -> int:
        return sum(result.summary.unfinished for result in self.generations)

    @property
    def stale(self) -> int:
        return sum(result.summary.stale for result in self.generations)

    @property
    def complete(self) -> bool:
        """Only a fully published, non-partial invocation is delivered."""

        return (
            bool(self.generations)
            and not self.truncated
            and all(
                result.summary.status == "ready"
                and result.summary.unfinished == 0
                and result.summary.errors == 0
                and result.summary.stale == 0
                for result in self.generations
            )
        )


@dataclass(frozen=True, slots=True)
class SemanticCostCalibration:
    """Measured model-only throughput for one exact planning workload."""

    calibration_signature: str
    execution_signature: str
    processing_signature: str
    workload: str
    model_signature: str
    role: str
    contents_per_second: float
    sample_contents: int
    sample_input_bytes: int

    def __post_init__(self) -> None:
        _validate_semantic_cost_calibration(self)


@dataclass(frozen=True, slots=True)
class SemanticSourcePlan:
    """Read-only counts derived from one validated route-owned source."""

    source_kind: str
    database: Path
    schema_version: int
    resources: int
    sections: int
    chunks: int
    embedding_entities: int
    source_bytes: int
    section_text_bytes: int
    input_bytes: int
    snapshot_xxh3_128: str

    def __post_init__(self) -> None:
        _validate_semantic_source_plan(
            self,
            text_source_kinds=SEMANTIC_PLAN_TEXT_SOURCE_KINDS,
        )


@dataclass(frozen=True, slots=True)
class SemanticWorkloadPlan:
    """Cost and reuse projection for one model/role processing signature."""

    name: str
    modality: str
    role: str
    model_signature: str
    vector_space: str
    model_id: str
    model_version: str
    dimensions: int
    provider: str
    supported_roles: tuple[str, ...]
    vector_dtype: str
    normalization: str
    distance: str
    model_provenance_json: str
    processing_signature: str
    embedding_entities: int
    unique_contents: int
    preexisting_reusable_contents: int
    planned_reusable_contents: int
    new_unique_contents: int
    input_bytes: int
    unique_input_bytes: int
    new_vector_blob_bytes_lower_bound: int
    model_request_contents_lower_bound: int
    model_request_contents_upper_bound: int
    estimated_model_seconds_lower_bound: float | None
    estimated_model_seconds_upper_bound: float | None
    cost_calibration_signature: str | None
    cost_execution_signature: str | None
    cost_calibration_contents_per_second: float | None
    cost_calibration_sample_contents: int | None
    cost_calibration_sample_input_bytes: int | None
    cost_unavailable_reason: str | None

    def __post_init__(self) -> None:
        _validate_semantic_workload_plan(self)

    @property
    def estimated_model_seconds(self) -> float | None:
        if self.estimated_model_seconds_lower_bound != (self.estimated_model_seconds_upper_bound):
            return None
        return self.estimated_model_seconds_lower_bound

    @property
    def cost_complete(self) -> bool:
        return self.estimated_model_seconds is not None

    @property
    def cost_calibrated(self) -> bool:
        return self.cost_calibration_signature is not None


@dataclass(frozen=True, slots=True)
class SemanticPlan:
    """Validated read-only projection over independently fenced DB snapshots."""

    scope: str
    selected_sources: tuple[str, ...]
    semantic_database: Path
    semantic_schema_version: int | None
    source_plans: tuple[SemanticSourcePlan, ...]
    workloads: tuple[SemanticWorkloadPlan, ...]
    text_chunking_signature: str | None
    content_set_xxh3_128: str
    semantic_snapshot_xxh3_128: str
    plan_signature: str
    resources: int
    sections: int
    chunks: int
    embedding_entities: int
    source_bytes: int
    section_text_bytes: int
    input_bytes: int
    unique_contents: int
    unique_input_bytes: int
    reusable_unique_contents: int
    new_unique_contents: int
    new_vector_blob_bytes_lower_bound: int
    model_request_contents_lower_bound: int
    model_request_contents_upper_bound: int
    estimated_model_seconds_lower_bound: float | None
    estimated_model_seconds_upper_bound: float | None
    scratch_storage_bytes: int
    max_scratch_bytes: int
    originals_verified: bool | None
    execution_ready: bool | None
    dry_run: bool = True
    jobs_created: int = 0
    state_mutated: bool = False
    estimate_kind: str = "model_only_request_range_from_exact_content_projection"
    vector_bytes_kind: str = "lower_bound_vector_blob_only"
    snapshot_scope: str = (
        "read_transaction_per_database_with_data_version_fence_not_cross_database_atomic"
    )
    sqlite_read_snapshot_may_touch_shm: bool = True

    def __post_init__(self) -> None:
        _validate_semantic_plan(
            self,
            text_source_kinds=SEMANTIC_PLAN_TEXT_SOURCE_KINDS,
            image_ocr_text_channel=IMAGE_OCR_TEXT_CHANNEL,
            source_plan_type=SemanticSourcePlan,
            workload_plan_type=SemanticWorkloadPlan,
        )

    @property
    def estimated_model_seconds(self) -> float | None:
        if self.estimated_model_seconds_lower_bound != (self.estimated_model_seconds_upper_bound):
            return None
        return self.estimated_model_seconds_lower_bound

    @property
    def cost_complete(self) -> bool:
        return self.estimated_model_seconds is not None

    @property
    def cost_calibrated(self) -> bool:
        required = tuple(
            workload
            for workload in self.workloads
            if workload.model_request_contents_upper_bound > 0
        )
        return bool(required) and all(workload.cost_calibrated for workload in required)

    @property
    def complete(self) -> bool:
        """Operational completeness; planning success alone is insufficient."""

        return self.execution_ready is True and self.originals_verified is not False


@dataclass(frozen=True, slots=True)
class SemanticRanking:
    name: str
    hits: tuple[SearchHit, ...]
    resolved: tuple[ResolvedSearchHit, ...]
    scanned: int
    complete: bool
    available: bool = True
    unavailable_reason: str | None = None
    cutoff_reason: str | None = None
    next_cursor: int | None = None
    cutoff_score: float | None = None
    fusion_weight: float = 1.0
    provenance: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ImageRetrievalCalibration:
    """Measured CLIP retrieval floor bound to one exact runtime contract.

    NeoCortex deliberately has no built-in visual score threshold.  A caller
    may supply this contract only after evaluating both positive and negative
    queries on a representative fixture; otherwise image retrieval abstains.
    """

    calibration_signature: str
    query_model_signature: str
    indexed_model_signature: str
    pipeline: str
    backend: str
    minimum_score: float
    positive_queries: int
    negative_queries: int
    sample_items: int
    # Durable calibrations carry the exact indexed-generation contract.  The
    # optional default preserves the Python API used by older callers and
    # fixture tests; persisted v1 calibrations always populate it.
    indexed_processing_signature: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "calibration_signature",
            "query_model_signature",
            "indexed_model_signature",
            "pipeline",
            "backend",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"image retrieval {name} cannot be blank")
        if self.indexed_processing_signature is not None and (
            not isinstance(self.indexed_processing_signature, str)
            or not self.indexed_processing_signature.strip()
        ):
            raise ValueError("image retrieval indexed_processing_signature cannot be blank")
        if (
            isinstance(self.minimum_score, bool)
            or not isinstance(self.minimum_score, (int, float))
            or not math.isfinite(float(self.minimum_score))
            or not -1.0 <= float(self.minimum_score) <= 1.0
        ):
            raise ValueError("image retrieval minimum_score must be finite between -1 and 1")
        for name in ("positive_queries", "negative_queries", "sample_items"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"image retrieval {name} must be positive")


@dataclass(frozen=True, slots=True)
class FusedResolvedHit:
    fused: FusedHit
    path: str | None
    source_kind: str
    source_identity: str
    snippet: str | None
    primary_evidence: ResolvedSearchHit | None = None

@dataclass(frozen=True, slots=True)
class SemanticSearchResult:
    query: str
    rankings: tuple[SemanticRanking, ...]
    lexical_rankings: tuple[LexicalRanking, ...]
    fused: tuple[FusedResolvedHit, ...]

    @property
    def complete(self) -> bool:
        semantic_complete = all(ranking.available and ranking.complete for ranking in self.rankings)
        lexical_complete = all(
            ranking.availability is LexicalAvailability.AVAILABLE
            for ranking in self.lexical_rankings
        )
        return semantic_complete and lexical_complete


@dataclass(frozen=True, slots=True)
class ModelPreparation:
    model_signature: str
    model_id: str
    dimensions: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class SemanticStatus:
    exists: bool
    schema_version: int | None = None
    counts: Mapping[str, int] = field(default_factory=dict)
    generations: tuple[GenerationSummary, ...] = ()
    # Receipt timings are captured on the same fenced read view as counts and
    # generation summaries, so a status response cannot mix generations from
    # one snapshot with timing rows from a later owner read.
    generation_timings: Mapping[int, Mapping[str, int]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SemanticEvidencePassResult:
    indexed_model_signature: str
    query_model_signature: str
    vector_space: str
    prototypes: int
    entities_scored: int
    evidence_staged: int
    stale_evidence_deactivated: int
    entities_abstained: int = 0


@dataclass(frozen=True, slots=True)
class SemanticClassificationResult:
    semantic_database: Path
    ontology_id: str
    ontology_version: str
    passes: tuple[SemanticEvidencePassResult, ...]
    skipped: Mapping[str, str] = field(default_factory=dict)


# endregion [02]
