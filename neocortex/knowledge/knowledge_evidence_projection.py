"""Stable, read-only projection of Knowledge evidence.

The search contracts intentionally keep retrieval, identity and owner state
separate.  This module is the small public adapter that joins those already
validated contracts for consumers that need one evidence record.  It performs
no lookup, opens no state and never upgrades ranking scores into authority.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .knowledge_read_budget import KnowledgeReadBudget
from neocortex.semantic.semantic_models import canonical_json

if TYPE_CHECKING:
    from .knowledge_contracts import KnowledgeHit
    from .knowledge_search_contracts import KnowledgeSearchResult


KNOWLEDGE_EVIDENCE_PROJECTION_SCHEMA = "neocortex.knowledge-evidence-projection/v1"
EVIDENCE_PROJECTION_SCHEMA = KNOWLEDGE_EVIDENCE_PROJECTION_SCHEMA
KNOWLEDGE_EVIDENCE_PROJECTION_VERSION = 1
EVIDENCE_PROJECTION_VERSION = KNOWLEDGE_EVIDENCE_PROJECTION_VERSION

_LOCATOR_FIELDS = (
    "page",
    "start_line",
    "end_line",
    "sheet",
    "cell_range",
    "start_ms",
    "end_ms",
    "bounding_box",
    "coordinate_space",
    "start_char",
    "end_char",
    "symbol",
    "section_kind",
    "section_id",
)
_MAX_SIGNALS = 64
_KNOWLEDGE_PROJECTION_SCOPES = ("personal", "framework", "all")

KnowledgeProjectionScope = Literal["personal", "framework", "all"]


def _contract_types() -> tuple[type[Any], type[Any]]:
    """Load canonical contracts only when this optional facade is used."""

    from .knowledge_contracts import KnowledgeHit
    from .knowledge_search_contracts import KnowledgeSearchResult

    return KnowledgeHit, KnowledgeSearchResult


def _locator(evidence: Any) -> dict[str, object]:
    result: dict[str, object] = {}
    for name in _LOCATOR_FIELDS:
        value = getattr(evidence, name)
        if value is not None:
            result[name] = list(value) if name == "bounding_box" else value
    return result


def _signal(signal: Any) -> dict[str, object]:
    result: dict[str, object] = {
        "source": signal.source,
        "score_kind": signal.score_kind,
        "raw_score": signal.raw_score,
        "source_rank": signal.source_rank,
    }
    for name in ("model_signature", "query_model_signature", "generation", "contribution"):
        value = getattr(signal, name)
        if value is not None:
            result[name] = value
    return result


def _validate_hit(hit: Any) -> None:
    KnowledgeHit, _ = _contract_types()
    if not isinstance(hit, KnowledgeHit):
        raise TypeError("hit must be a validated KnowledgeHit")


def validate_knowledge_projection_scope(scope: object | None) -> str | None:
    """Validate the fixed read scopes without importing the API facade.

    ``None`` keeps the historical hit-level projection meaning "scope not
    supplied".  Callers that project a federated search should pass the
    selected scope explicitly; no arbitrary owner or path is accepted here.
    """

    if scope is None:
        return None
    if not isinstance(scope, str) or scope not in _KNOWLEDGE_PROJECTION_SCOPES:
        raise ValueError("scope must be personal, framework or all")
    return scope


def _read_budget_payload(value: object | None) -> dict[str, object] | None:
    """Copy a caller-owned bounded-read budget without exposing callbacks."""

    if value is None:
        return None
    if isinstance(value, KnowledgeReadBudget):
        return copy.deepcopy(value.to_dict())
    if isinstance(value, Mapping):
        return KnowledgeReadBudget.from_mapping(value).to_dict()
    raise TypeError("read_budget must be a KnowledgeReadBudget or mapping")


def project_knowledge_hit(hit: KnowledgeHit, *, scope: str | None = None) -> dict[str, object]:
    """Project one validated hit without deriving authority from retrieval."""

    _validate_hit(hit)
    scope = validate_knowledge_projection_scope(scope)
    evidence = hit.evidence
    resource = hit.resource
    revision = hit.revision
    locator = _locator(evidence)
    has_locator = bool(locator)
    snippet = evidence.snippet
    modality = "text" if snippet is not None and has_locator else "reference_only"
    identity: dict[str, object] = {
        "resource_id": resource.resource_id,
        "revision_id": revision.revision_id,
        "evidence_id": evidence.evidence_id,
        "owner": resource.owner,
        "source_kind": resource.source_kind,
    }
    if resource.physical_identity is not None:
        identity["physical_identity"] = resource.physical_identity.to_dict()

    signals = [_signal(signal) for signal in hit.signals[:_MAX_SIGNALS]]
    omitted_signals = max(0, len(hit.signals) - len(signals))
    reasons = [] if has_locator else ["locator_unavailable"]
    if snippet is None:
        reasons.append("value_unavailable")
    projection: dict[str, object] = {
        "schema": KNOWLEDGE_EVIDENCE_PROJECTION_SCHEMA,
        "schema_version": KNOWLEDGE_EVIDENCE_PROJECTION_VERSION,
        "kind": "knowledge_evidence_projection",
        "version": KNOWLEDGE_EVIDENCE_PROJECTION_VERSION,
        "identity": identity,
        "equality": {
            "key": [resource.resource_id, revision.revision_id, evidence.evidence_id],
            "basis": "resource_revision_evidence",
            "status": "not_assessed",
            "bytewise": "not_assessed",
            "authority": "none",
        },
        "provenance": {
            "scope": scope,
            "owner": resource.owner,
            "source_kind": resource.source_kind,
            "producer": revision.producer,
            "processing_signature": revision.processing_signature,
            "revision_id": revision.revision_id,
            "generation": revision.generation,
            "observed_at_utc": revision.observed_at_utc,
            "evidence_method": evidence.method.value,
            "extractor": evidence.extractor,
            "extractor_version": evidence.extractor_version,
            "evidence_generation": evidence.generation,
            "identifiers": [
                {"namespace": namespace, "value": value}
                for namespace, value in evidence.identifiers
            ],
        },
        "value": {
            "kind": modality,
            "text": snippet,
            "status": "present" if snippet is not None else "unavailable",
            "interpretation": "documented_value_not_assessed",
        },
        "similarity": {
            "authority": "advisory_only",
            "interpretation": "retrieval_score_not_probability_or_permission",
            "fused_score": hit.fused_score,
            "signals": signals,
            "omitted_signals": omitted_signals,
        },
        "disposition": {
            "resource": resource.disposition.value if resource.disposition is not None else None,
            "revision": revision.state.value,
            "evidence": "reference_only" if modality == "reference_only" else "not_assessed",
            "canonical_resource_id": resource.canonical_resource_id,
            "authority": "advisory_only",
            "reasons": list(hit.reasons),
        },
        "evidence": {
            "evidence_id": evidence.evidence_id,
            "method": evidence.method.value,
            "modality": modality,
            "replayable": has_locator,
            "snippet": snippet,
        },
        "coverage": {
            "status": "complete" if not reasons else "partial",
            "reasons": reasons,
            "has_value": snippet is not None,
            "has_locator": has_locator,
            "answer_sufficiency": "not_assessed",
        },
        "locator": locator or None,
    }
    if hit.confidence is not None:
        projection["confidence"] = hit.confidence
    if hit.warnings:
        projection["warnings"] = list(hit.warnings)
    return projection


def project_knowledge_search(
    result: KnowledgeSearchResult,
    *,
    scope: str | None = None,
    read_budget: object | None = None,
) -> dict[str, object]:
    """Project a search result with additive bounded-read metadata.

    The underlying result remains the source of truth for ranking, telemetry,
    owner blocking and coverage.  This adapter only copies those validated
    values; it does not execute a search, combine scopes or infer sufficiency.
    """

    _, KnowledgeSearchResult = _contract_types()
    if not isinstance(result, KnowledgeSearchResult):
        raise TypeError("result must be a validated KnowledgeSearchResult")
    scope = validate_knowledge_projection_scope(scope)
    items = [project_knowledge_hit(hit, scope=scope) for hit in result.hits]
    partial = (
        not result.complete
        or result.truncated
        or bool(result.warnings)
        or bool(result.blocking_owners)
    )
    reasons: list[str] = []
    if not result.complete:
        reasons.append("search_incomplete")
    if result.truncated:
        reasons.append("candidate_scan_truncated")
    if result.warnings:
        reasons.extend(result.warnings)
    if result.blocking_owners:
        reasons.append("blocking_owners")
    coverage = {
        "status": "partial" if partial else ("complete" if items else "no_evidence"),
        "reasons": sorted(set(reasons)),
        "complete": result.complete,
        "truncated": result.truncated,
        "omitted_candidates": result.omitted_candidates,
        "rows_scanned": result.rows_scanned,
        "vectors_scanned": result.vectors_scanned,
        "answer_sufficiency": "not_assessed",
    }
    return {
        "schema": KNOWLEDGE_EVIDENCE_PROJECTION_SCHEMA,
        "schema_version": KNOWLEDGE_EVIDENCE_PROJECTION_VERSION,
        "kind": "knowledge_evidence_search_projection",
        "version": KNOWLEDGE_EVIDENCE_PROJECTION_VERSION,
        "query": result.plan.normalized_query,
        "scope": scope,
        "items": items,
        "rankings": [copy.deepcopy(ranking.to_dict()) for ranking in result.rankings],
        "telemetry": (
            None if result.telemetry is None else copy.deepcopy(result.telemetry.to_dict())
        ),
        "blocking_owners": list(result.blocking_owners),
        "elapsed_milliseconds": result.elapsed_milliseconds,
        "result_window_full": result.result_window_full,
        "window_omitted_candidates": result.window_omitted_candidates,
        "read_budget": _read_budget_payload(read_budget),
        "warnings": list(result.warnings),
        "coverage": coverage,
        "snapshot": result.snapshot.to_dict(),
    }


def evidence_projection_payload(hit: KnowledgeHit, *, scope: str | None = None) -> dict[str, object]:
    """Stable payload alias used by API/SDK callers."""

    return project_knowledge_hit(hit, scope=scope)


def knowledge_evidence_projection_payload(
    hit: KnowledgeHit, *, scope: str | None = None
) -> dict[str, object]:
    return project_knowledge_hit(hit, scope=scope)


def evidence_search_projection_payload(
    result: KnowledgeSearchResult,
    *,
    scope: str | None = None,
    read_budget: object | None = None,
) -> dict[str, object]:
    return project_knowledge_search(result, scope=scope, read_budget=read_budget)


def knowledge_search_projection_payload(
    result: KnowledgeSearchResult,
    *,
    scope: str | None = None,
    read_budget: object | None = None,
) -> dict[str, object]:
    """Named search alias for API/SDK adapters."""

    return project_knowledge_search(result, scope=scope, read_budget=read_budget)


def knowledge_evidence_search_projection_payload(
    result: KnowledgeSearchResult,
    *,
    scope: str | None = None,
    read_budget: object | None = None,
) -> dict[str, object]:
    """Compatibility alias spelling the evidence/search relationship."""

    return project_knowledge_search(result, scope=scope, read_budget=read_budget)


@dataclass(frozen=True, slots=True)
class KnowledgeEvidenceProjection:
    """Immutable wrapper for callers that prefer a contract object."""

    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        required = {
            "schema",
            "schema_version",
            "kind",
            "version",
            "identity",
            "equality",
            "provenance",
            "value",
            "similarity",
            "disposition",
            "evidence",
            "coverage",
            "locator",
        }
        if not isinstance(self.payload, Mapping) or not required.issubset(self.payload):
            raise ValueError("projection payload is incomplete")
        if self.payload.get("schema") != KNOWLEDGE_EVIDENCE_PROJECTION_SCHEMA:
            raise ValueError("projection schema is incompatible")
        object.__setattr__(self, "payload", copy.deepcopy(dict(self.payload)))

    @classmethod
    def from_hit(cls, hit: KnowledgeHit, *, scope: str | None = None) -> "KnowledgeEvidenceProjection":
        return cls(project_knowledge_hit(hit, scope=scope))

    def to_dict(self) -> dict[str, object]:
        return copy.deepcopy(dict(self.payload))

    def to_json(self) -> str:
        return canonical_json(self.to_dict())


EvidenceProjection = KnowledgeEvidenceProjection


@dataclass(frozen=True, slots=True)
class KnowledgeSearchProjection:
    """Immutable wrapper around one projected Knowledge search."""

    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        required = {
            "schema",
            "schema_version",
            "kind",
            "version",
            "query",
            "scope",
            "items",
            "rankings",
            "telemetry",
            "blocking_owners",
            "elapsed_milliseconds",
            "result_window_full",
            "window_omitted_candidates",
            "read_budget",
            "warnings",
            "coverage",
            "snapshot",
        }
        if not isinstance(self.payload, Mapping) or not required.issubset(self.payload):
            raise ValueError("search projection payload is incomplete")
        if self.payload.get("schema") != KNOWLEDGE_EVIDENCE_PROJECTION_SCHEMA:
            raise ValueError("projection schema is incompatible")
        validate_knowledge_projection_scope(self.payload.get("scope"))
        object.__setattr__(self, "payload", copy.deepcopy(dict(self.payload)))

    @classmethod
    def from_result(
        cls,
        result: KnowledgeSearchResult,
        *,
        scope: str | None = None,
        read_budget: object | None = None,
    ) -> "KnowledgeSearchProjection":
        return cls(
            project_knowledge_search(result, scope=scope, read_budget=read_budget)
        )

    def to_dict(self) -> dict[str, object]:
        return copy.deepcopy(dict(self.payload))

    def to_json(self) -> str:
        return canonical_json(self.to_dict())


KnowledgeEvidenceSearchProjection = KnowledgeSearchProjection
SearchEvidenceProjection = KnowledgeSearchProjection


__all__ = (
    "EVIDENCE_PROJECTION_SCHEMA",
    "EVIDENCE_PROJECTION_VERSION",
    "KNOWLEDGE_EVIDENCE_PROJECTION_SCHEMA",
    "KNOWLEDGE_EVIDENCE_PROJECTION_VERSION",
    "EvidenceProjection",
    "KnowledgeEvidenceProjection",
    "KnowledgeEvidenceSearchProjection",
    "KnowledgeProjectionScope",
    "KnowledgeSearchProjection",
    "SearchEvidenceProjection",
    "evidence_projection_payload",
    "evidence_search_projection_payload",
    "knowledge_evidence_projection_payload",
    "knowledge_evidence_search_projection_payload",
    "knowledge_search_projection_payload",
    "project_knowledge_hit",
    "project_knowledge_search",
    "validate_knowledge_projection_scope",
)
