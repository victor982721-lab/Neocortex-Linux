"""Bounded result DTOs emitted by Fast Curation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .fast_curation_policy import CurationDecision, RankedCandidate

DECISION_SCHEMA = "fast-curation-decision/v1"

@dataclass(frozen=True, slots=True)
class DocumentClassificationEvidence:
    """Catalog-safe decision DTO; no source bytes or unbounded text included."""

    document_id: str
    model_signature: str
    vector_space: str
    representation_version: str
    representation_fingerprint: str
    ontology_version: str
    prototype_version: str
    prototype_set_fingerprint: str
    policy_version: str
    calibration_version: str | None
    top_candidates: tuple[RankedCandidate, ...]
    top1_score: float | None
    top2_score: float | None
    margin: float | None
    calibrated_decision: str
    decision_reason: str
    decision_confidence: float | None = None
    confidence_kind: str = "not_calibrated"
    selected_by_family: Mapping[str, str] = field(default_factory=dict)
    destination: str | None = None
    deterministic_evidence: Mapping[str, object] = field(default_factory=dict)
    semantic_evidence: Mapping[str, object] = field(default_factory=dict)
    metadata_evidence: Mapping[str, object] = field(default_factory=dict)
    structural_evidence: Mapping[str, object] = field(default_factory=dict)
    source_path: str | None = None
    content_embedding_key: str | None = None
    decision_cache_key: str | None = None
    source_kind: str = "unknown"
    file_key: str | None = None
    input_signature: str | None = None
    ontology_id: str = ""

    def __post_init__(self) -> None:
        for name in (
            "document_id",
            "model_signature",
            "vector_space",
            "representation_version",
            "representation_fingerprint",
            "ontology_version",
            "prototype_version",
            "prototype_set_fingerprint",
            "policy_version",
            "decision_reason",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if not isinstance(self.source_kind, str) or not self.source_kind.strip():
            raise ValueError("source_kind must be non-empty")
        if self.file_key is None:
            object.__setattr__(self, "file_key", self.document_id)
        elif not isinstance(self.file_key, str) or not self.file_key.strip():
            raise ValueError("file_key must be non-empty")
        if self.input_signature is None:
            object.__setattr__(self, "input_signature", self.representation_fingerprint)
        elif not isinstance(self.input_signature, str) or not self.input_signature.strip():
            raise ValueError("input_signature must be non-empty")
        if self.calibrated_decision not in {value.value for value in CurationDecision}:
            raise ValueError("calibrated_decision is invalid")
        if len(self.top_candidates) > 15:
            raise ValueError("top candidates exceed bounded limit")
        for value in (self.top1_score, self.top2_score, self.margin, self.decision_confidence):
            if value is not None and not math.isfinite(float(value)):
                raise ValueError("decision numeric evidence must be finite")

    @property
    def decision(self) -> str:
        return self.calibrated_decision

    @property
    def organization_disposition(self) -> str:
        """Catalog spelling for the final two-state disposition."""

        return self.calibrated_decision.upper()

    @property
    def classified(self) -> bool:
        return self.calibrated_decision == CurationDecision.CLASSIFIED.value

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": DECISION_SCHEMA,
            "document_id": self.document_id,
            "model_signature": self.model_signature,
            "vector_space": self.vector_space,
            "representation_version": self.representation_version,
            "representation_fingerprint": self.representation_fingerprint,
            "ontology_version": self.ontology_version,
            "prototype_version": self.prototype_version,
            "prototype_set_fingerprint": self.prototype_set_fingerprint,
            "policy_version": self.policy_version,
            "calibration_version": self.calibration_version,
            "top_candidates": [
                {
                    "prototype_id": value.prototype_id,
                    "concept_id": value.concept_id,
                    "family": value.family,
                    "label": value.label,
                    "score": value.score,
                    "rank": value.rank,
                }
                for value in self.top_candidates
            ],
            "top1_score": self.top1_score,
            "top2_score": self.top2_score,
            "margin": self.margin,
            "calibrated_decision": self.calibrated_decision,
            "decision_reason": self.decision_reason,
            "decision_confidence": self.decision_confidence,
            "confidence_kind": self.confidence_kind,
            "selected_by_family": dict(self.selected_by_family),
            "destination": self.destination,
            "deterministic_evidence": dict(self.deterministic_evidence),
            "semantic_evidence": dict(self.semantic_evidence),
            "metadata_evidence": dict(self.metadata_evidence),
            "structural_evidence": dict(self.structural_evidence),
            "source_path": self.source_path,
            "content_embedding_key": self.content_embedding_key,
            "decision_cache_key": self.decision_cache_key,
            "source_kind": self.source_kind,
            "file_key": self.file_key,
            "input_signature": self.input_signature,
            "ontology_id": self.ontology_id,
        }


@dataclass(frozen=True, slots=True)
class FastCurationMetrics:
    documents_seen: int = 0
    classified: int = 0
    abstained: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    embeddings_produced: int = 0
    vectors_produced: int = 0
    documents_escalated: int = 0
    model_available: bool = False
    calibration_loaded: bool = False
    persisted_decisions: int = 0
    elapsed_seconds: float = 0.0
    abstention_reasons: tuple[tuple[str, int], ...] = ()

    @property
    def documents_per_second(self) -> float:
        return self.documents_seen / self.elapsed_seconds if self.elapsed_seconds else 0.0

    @property
    def embeddings_per_second(self) -> float:
        return self.embeddings_produced / self.elapsed_seconds if self.elapsed_seconds else 0.0

    @property
    def cache_hit_ratio(self) -> float:
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total else 0.0

    @property
    def vectors_per_document(self) -> float:
        return self.vectors_produced / self.documents_seen if self.documents_seen else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "documents_seen": self.documents_seen,
            "classified": self.classified,
            "abstained": self.abstained,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_ratio": self.cache_hit_ratio,
            "embeddings_produced": self.embeddings_produced,
            "embeddings_per_second": self.embeddings_per_second,
            "vectors_produced": self.vectors_produced,
            "vectors_per_document": self.vectors_per_document,
            "documents_escalated": self.documents_escalated,
            "model_available": self.model_available,
            "calibration_loaded": self.calibration_loaded,
            "persisted_decisions": self.persisted_decisions,
            "elapsed_seconds": self.elapsed_seconds,
            "documents_per_second": self.documents_per_second,
            "abstention_reasons": dict(self.abstention_reasons),
        }


@dataclass(frozen=True, slots=True)
class FastCurationRunResult:
    status: str
    root: Path
    metrics: FastCurationMetrics
    decision_samples: tuple[DocumentClassificationEvidence, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def decisions(self) -> tuple[DocumentClassificationEvidence, ...]:
        """Compatibility view, deliberately bounded to samples."""

        return self.decision_samples

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "root": str(self.root),
            "metrics": self.metrics.as_dict(),
            "decision_samples": [value.as_dict() for value in self.decision_samples],
            "errors": list(self.errors),
        }



__all__ = [
    "DECISION_SCHEMA",
    "DocumentClassificationEvidence",
    "FastCurationMetrics",
    "FastCurationRunResult",
]
