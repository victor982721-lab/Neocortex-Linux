"""Read-only structural ports for the private Knowledge contract helpers.

The public dataclasses remain owned by ``knowledge_contracts``.  Helper
modules depend only on these structural views during static analysis, so
the facade may delegate to them without creating reverse import edges.
"""

from __future__ import annotations
from collections.abc import Callable, Mapping
from typing import Any, Protocol


class KnowledgeTelemetryClock(Protocol):
    """Read-only structural view of ``KnowledgeTelemetryClock``."""

    @property
    def read_ns(self) -> Callable[[], int]: ...

    @property
    def signature(self) -> str: ...

    @property
    def identified(self) -> bool: ...


class KnowledgePhaseTiming(Protocol):
    """Read-only structural view of ``KnowledgePhaseTiming``."""

    @property
    def phase(self) -> Any: ...

    @property
    def duration_ns(self) -> int: ...

    @property
    def service_attempt(self) -> int: ...

    @property
    def owner(self) -> str | None: ...

    @property
    def ranking_names(self) -> tuple[str, ...]: ...

    @property
    def snapshot_id(self) -> str | None: ...

    @property
    def executed(self) -> bool: ...

    def to_dict(self) -> dict[str, object]: ...


class KnowledgeQueryTelemetry(Protocol):
    """Read-only structural view of ``KnowledgeQueryTelemetry``."""

    @property
    def operation(self) -> Any: ...

    @property
    def total_duration_ns(self) -> int: ...

    @property
    def phases(self) -> tuple[KnowledgePhaseTiming, ...]: ...

    @property
    def clock_signature(self) -> str: ...

    def to_dict(self) -> dict[str, object]: ...

    def to_json(self) -> str: ...


class PhysicalIdentityRef(Protocol):
    """Read-only structural view of ``PhysicalIdentityRef``."""

    @property
    def scheme(self) -> str: ...

    @property
    def value(self) -> str: ...

    @property
    def identity_version(self) -> int: ...

    def to_dict(self) -> dict[str, object]: ...


class ResourceRef(Protocol):
    """Read-only structural view of ``ResourceRef``."""

    @property
    def resource_id(self) -> str: ...

    @property
    def source_kind(self) -> str: ...

    @property
    def owner(self) -> str: ...

    @property
    def physical_identity(self) -> PhysicalIdentityRef | None: ...

    @property
    def current_path(self) -> str | None: ...

    @property
    def disposition(self) -> Any | None: ...

    @property
    def canonical_resource_id(self) -> str | None: ...

    def to_dict(self) -> dict[str, object]: ...


class RevisionRef(Protocol):
    """Read-only structural view of ``RevisionRef``."""

    @property
    def resource_id(self) -> str: ...

    @property
    def revision_id(self) -> str: ...

    @property
    def producer(self) -> str: ...

    @property
    def processing_signature(self) -> str: ...

    @property
    def generation(self) -> int | None: ...

    @property
    def state(self) -> Any: ...

    @property
    def observed_at_utc(self) -> str | None: ...

    def to_dict(self) -> dict[str, object]: ...


class EvidenceRef(Protocol):
    """Read-only structural view of ``EvidenceRef``."""

    @property
    def evidence_id(self) -> str: ...

    @property
    def resource_id(self) -> str: ...

    @property
    def revision_id(self) -> str: ...

    @property
    def method(self) -> Any: ...

    @property
    def page(self) -> int | None: ...

    @property
    def start_line(self) -> int | None: ...

    @property
    def end_line(self) -> int | None: ...

    @property
    def sheet(self) -> str | None: ...

    @property
    def cell_range(self) -> str | None: ...

    @property
    def start_ms(self) -> int | None: ...

    @property
    def end_ms(self) -> int | None: ...

    @property
    def bounding_box(self) -> tuple[float, float, float, float] | None: ...

    @property
    def coordinate_space(self) -> str | None: ...

    @property
    def start_char(self) -> int | None: ...

    @property
    def end_char(self) -> int | None: ...

    @property
    def symbol(self) -> str | None: ...

    @property
    def section_kind(self) -> str | None: ...

    @property
    def section_id(self) -> str | None: ...

    @property
    def snippet(self) -> str | None: ...

    @property
    def extractor(self) -> str | None: ...

    @property
    def extractor_version(self) -> str | None: ...

    @property
    def generation(self) -> int | None: ...

    @property
    def identifiers(self) -> tuple[tuple[str, str], ...]: ...

    def to_dict(self) -> dict[str, object]: ...


class RankingSignal(Protocol):
    """Read-only structural view of ``RankingSignal``."""

    @property
    def evidence(self) -> EvidenceRef | None: ...

    @property
    def query_support(self) -> Mapping[str, object]: ...

    @property
    def source(self) -> str: ...

    @property
    def score_kind(self) -> str: ...

    @property
    def raw_score(self) -> float: ...

    @property
    def source_rank(self) -> int: ...

    @property
    def model_signature(self) -> str | None: ...

    @property
    def generation(self) -> int | None: ...

    @property
    def contribution(self) -> float | None: ...

    @property
    def query_model_signature(self) -> str | None: ...

    def to_dict(self) -> dict[str, object]: ...


class KnowledgeHit(Protocol):
    """Read-only structural view of ``KnowledgeHit``."""

    @property
    def rank(self) -> int: ...

    @property
    def resource(self) -> ResourceRef: ...

    @property
    def revision(self) -> RevisionRef: ...

    @property
    def evidence(self) -> EvidenceRef: ...

    @property
    def signals(self) -> tuple[RankingSignal, ...]: ...

    @property
    def fused_score(self) -> float: ...

    @property
    def reasons(self) -> tuple[str, ...]: ...

    @property
    def confidence(self) -> float | None: ...

    @property
    def warnings(self) -> tuple[str, ...]: ...

    def to_dict(self) -> dict[str, object]: ...

    def to_json(self) -> str: ...


class PublicationHead(Protocol):
    """Read-only structural view of ``PublicationHead``."""

    @property
    def scope(self) -> str: ...

    @property
    def publication_id(self) -> str: ...

    @property
    def generation(self) -> int: ...

    @property
    def model_signature(self) -> str | None: ...

    def to_dict(self) -> dict[str, object]: ...


class LogicalWatermark(Protocol):
    """Read-only structural view of ``LogicalWatermark``."""

    @property
    def name(self) -> str: ...

    @property
    def value(self) -> str: ...

    def to_dict(self) -> dict[str, object]: ...


class ActiveModel(Protocol):
    """Read-only structural view of ``ActiveModel``."""

    @property
    def signature(self) -> str: ...

    @property
    def vector_space(self) -> str: ...

    @property
    def modality(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    @property
    def generation(self) -> int: ...

    def to_dict(self) -> dict[str, object]: ...


class OwnerSnapshot(Protocol):
    """Read-only structural view of ``OwnerSnapshot``."""

    @property
    def owner(self) -> str: ...

    @property
    def state(self) -> Any: ...

    @property
    def expected_schema_version(self) -> int: ...

    @property
    def observed_schema_version(self) -> int | None: ...

    @property
    def publications(self) -> tuple[PublicationHead, ...]: ...

    @property
    def watermarks(self) -> tuple[LogicalWatermark, ...]: ...

    @property
    def data_version_before(self) -> int | None: ...

    @property
    def data_version_after(self) -> int | None: ...

    @property
    def warning(self) -> str | None: ...

    @property
    def error_code(self) -> str | None: ...

    @property
    def identity_changed(self) -> bool: ...

    @property
    def changed(self) -> bool: ...

    def identity_dict(self) -> dict[str, object]: ...

    def to_dict(self) -> dict[str, object]: ...


class KnowledgeSnapshot(Protocol):
    """Read-only structural view of ``KnowledgeSnapshot``."""

    @property
    def source_version(self) -> str: ...

    @property
    def captured_at_utc(self) -> str: ...

    @property
    def captured_monotonic_ns(self) -> int: ...

    @property
    def owners(self) -> tuple[OwnerSnapshot, ...]: ...

    @property
    def active_models(self) -> tuple[ActiveModel, ...]: ...

    @property
    def snapshot_id(self) -> str: ...

    @property
    def consistency(self) -> Any: ...

    @property
    def attempts(self) -> int: ...

    @property
    def warnings(self) -> tuple[str, ...]: ...

    @property
    def changed_owners(self) -> tuple[str, ...]: ...

    def to_dict(self) -> dict[str, object]: ...

    def to_json(self) -> str: ...


class ContextPlanStepRef(Protocol):
    """Read-only structural view of ``ContextPlanStepRef``."""

    @property
    def channel(self) -> str: ...

    @property
    def ranking_name(self) -> str: ...

    @property
    def reason(self) -> str: ...

    @property
    def candidate_limit(self) -> int: ...

    @property
    def required(self) -> bool: ...

    def to_dict(self) -> dict[str, object]: ...

    def to_json(self) -> str: ...


class ContextPlanRef(Protocol):
    """Read-only structural view of ``ContextPlanRef``."""

    @property
    def plan_id(self) -> str: ...

    @property
    def normalized_query(self) -> str: ...

    @property
    def retrieval_mode(self) -> str: ...

    @property
    def intents(self) -> tuple[str, ...]: ...

    @property
    def exact_terms(self) -> tuple[str, ...]: ...

    @property
    def source_kinds(self) -> tuple[str, ...]: ...

    @property
    def formats(self) -> tuple[str, ...]: ...

    @property
    def project(self) -> str | None: ...

    @property
    def date_from(self) -> str | None: ...

    @property
    def date_to(self) -> str | None: ...

    @property
    def include_history(self) -> bool: ...

    @property
    def limit(self) -> int: ...

    @property
    def max_per_resource(self) -> int: ...

    @property
    def min_section_distance(self) -> int: ...

    @property
    def max_vectors(self) -> int: ...

    @property
    def steps(self) -> tuple[ContextPlanStepRef, ...]: ...

    @property
    def notices(self) -> tuple[str, ...]: ...

    def to_dict(self) -> dict[str, object]: ...

    def to_json(self) -> str: ...


class ContextGraphBudget(Protocol):
    """Read-only structural view of ``ContextGraphBudget``."""

    @property
    def identifiers_considered(self) -> int: ...

    @property
    def entities_included(self) -> int: ...

    @property
    def relations_included(self) -> int: ...

    @property
    def omitted_identifiers(self) -> int: ...

    @property
    def omitted_entities(self) -> int: ...

    @property
    def omitted_relations(self) -> int: ...

    @property
    def identifier_limit_per_evidence(self) -> int: ...

    @property
    def measurement_scope(self) -> str: ...

    @property
    def omitted_total(self) -> int: ...

    def to_dict(self) -> dict[str, object]: ...


class ContextBudget(Protocol):
    """Read-only structural view of ``ContextBudget``."""

    @property
    def character_limit(self) -> int: ...

    @property
    def characters_used(self) -> int: ...

    @property
    def estimated_tokens(self) -> int: ...

    @property
    def estimator_signature(self) -> str: ...

    @property
    def omitted_candidates(self) -> int: ...

    @property
    def truncated_evidence_ids(self) -> tuple[str, ...]: ...

    @property
    def measurement_scope(self) -> str: ...

    def to_dict(self) -> dict[str, object]: ...


class ContextEntityRef(Protocol):
    """Read-only structural view of ``ContextEntityRef``."""

    @property
    def entity_id(self) -> str: ...

    @property
    def entity_kind(self) -> str: ...

    @property
    def label(self) -> str: ...

    @property
    def evidence_ids(self) -> tuple[str, ...]: ...

    @property
    def resource_ids(self) -> tuple[str, ...]: ...

    def to_dict(self) -> dict[str, object]: ...

    def to_json(self) -> str: ...


class ContextContradictionRef(Protocol):
    """Read-only structural view of ``ContextContradictionRef``."""

    @property
    def contradiction_id(self) -> str: ...

    @property
    def contradiction_kind(self) -> str: ...

    @property
    def topic(self) -> str: ...

    @property
    def values(self) -> tuple[str, ...]: ...

    @property
    def citation_ids(self) -> tuple[str, ...]: ...

    @property
    def summary(self) -> str: ...

    def to_dict(self) -> dict[str, object]: ...

    def to_json(self) -> str: ...

    @staticmethod
    def _stable_id(
        contradiction_kind: str,
        topic: str,
        values: tuple[str, ...],
    ) -> str: ...


class ContextRelationRef(Protocol):
    """Read-only structural view of ``ContextRelationRef``."""

    @property
    def relation_id(self) -> str: ...

    @property
    def source_entity_id(self) -> str: ...

    @property
    def target_entity_id(self) -> str: ...

    @property
    def relation_kind(self) -> str: ...

    @property
    def method(self) -> Any: ...

    @property
    def provenance(self) -> tuple[str, ...]: ...

    @property
    def evidence_ids(self) -> tuple[str, ...]: ...

    @property
    def confidence(self) -> float | None: ...

    def to_dict(self) -> dict[str, object]: ...

    def to_json(self) -> str: ...


class ContextBundle(Protocol):
    """Read-only structural view of ``ContextBundle``."""

    @property
    def normalized_query(self) -> str: ...

    @property
    def intents(self) -> tuple[str, ...]: ...

    @property
    def plan_id(self) -> str: ...

    @property
    def plan(self) -> ContextPlanRef: ...

    @property
    def snapshot(self) -> KnowledgeSnapshot: ...

    @property
    def selected_hits(self) -> tuple[KnowledgeHit, ...]: ...

    @property
    def citation_ids(self) -> tuple[tuple[str, str], ...]: ...

    @property
    def graph_budget(self) -> ContextGraphBudget: ...

    @property
    def budget(self) -> ContextBudget: ...

    @property
    def rendered_context(self) -> str: ...

    @property
    def completeness(self) -> Any: ...

    @property
    def entities(self) -> tuple[ContextEntityRef, ...]: ...

    @property
    def relations(self) -> tuple[ContextRelationRef, ...]: ...

    @property
    def contradictions(self) -> tuple[ContextContradictionRef, ...]: ...

    @property
    def missing_information(self) -> tuple[str, ...]: ...

    @property
    def warnings(self) -> tuple[str, ...]: ...

    @property
    def telemetry(self) -> KnowledgeQueryTelemetry | None: ...

    @property
    def blocking_owners(self) -> tuple[str, ...]: ...

    def to_dict(self) -> dict[str, object]: ...

    def to_json(self) -> str: ...


__all__ = [
    "ActiveModel",
    "ContextBudget",
    "ContextBundle",
    "ContextContradictionRef",
    "ContextEntityRef",
    "ContextGraphBudget",
    "ContextPlanRef",
    "ContextPlanStepRef",
    "ContextRelationRef",
    "EvidenceRef",
    "KnowledgeHit",
    "KnowledgePhaseTiming",
    "KnowledgeQueryTelemetry",
    "KnowledgeSnapshot",
    "KnowledgeTelemetryClock",
    "LogicalWatermark",
    "OwnerSnapshot",
    "PhysicalIdentityRef",
    "PublicationHead",
    "RankingSignal",
    "ResourceRef",
    "RevisionRef",
]
