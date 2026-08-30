"""Immutable, versioned contracts for the read-only Knowledge Plane.

The contracts deliberately serialize only documented fields.  They never
expose arbitrary internal objects, and optional locator precision is omitted
when an owner cannot prove it.
"""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from . import knowledge_contract_context as _contract_context
from . import knowledge_contract_payloads as _contract_payloads
from . import knowledge_contract_references as _contract_references
from . import knowledge_contract_snapshot as _contract_snapshot
from . import knowledge_contract_telemetry as _contract_telemetry
from .knowledge_contract_validation import (
    optional_text as _contract_optional_text_impl,
    required_text as _contract_required_text_impl,
)
from neocortex.semantic.semantic_models import canonical_json, fingerprint_text

# region [01] Versions, bounds and stable vocabulary


KNOWLEDGE_CONTRACT_SCHEMA_VERSION = 1
KNOWLEDGE_TELEMETRY_SCHEMA_VERSION = 1
KNOWLEDGE_TELEMETRY_CLOCK_SIGNATURE = "python-perf-counter-ns-v1"
KNOWLEDGE_TELEMETRY_UNIDENTIFIED_CLOCK_SIGNATURE = "python-callable-unidentified-ns-v1"
MAX_KNOWLEDGE_TELEMETRY_PHASES = 512
MAX_KNOWLEDGE_TELEMETRY_RANKINGS_PER_PHASE = 64
MAX_KNOWLEDGE_TELEMETRY_NAME_CHARS = 256
MAX_KNOWLEDGE_TELEMETRY_RANKING_CHARS_PER_PHASE = 4_096
MAX_KNOWLEDGE_TELEMETRY_SNAPSHOT_ID_CHARS = 512
MAX_KNOWLEDGE_TELEMETRY_CLOCK_SIGNATURE_CHARS = 128
MAX_KNOWLEDGE_SNIPPET_CHARS = 4_096
MAX_EVIDENCE_IDENTIFIERS = 64
MAX_EVIDENCE_IDENTIFIER_COMPONENT_CHARS = 512
MAX_EVIDENCE_SYMBOL_CHARS = 1_024
MAX_CONTEXT_PLAN_STEPS = 32
MAX_CONTEXT_PLAN_VALUES = 4_096
MAX_CONTEXT_PLAN_VALUE_CHARS = 4_096
MAX_CONTEXT_PLAN_TOTAL_VALUE_CHARS = 4_096
MAX_CONTEXT_RELATION_PROVENANCE_ITEMS = 16
MAX_CONTEXT_RELATION_PROVENANCE_CHARS = 4_096


class ResourceDisposition(StrEnum):
    CANONICAL = "canonical"
    DUPLICATE = "duplicate"
    SUPERSEDED = "superseded"
    DERIVED = "derived"


class RevisionState(StrEnum):
    CURRENT = "current"
    HISTORICAL = "historical"
    SUPERSEDED = "superseded"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"


class EvidenceMethod(StrEnum):
    STRUCTURAL = "structural"
    EXTRACTED = "extracted"
    INFERRED = "inferred"
    HUMAN_CONFIRMED = "human_confirmed"
    AMBIGUOUS = "ambiguous"


class OwnerAvailability(StrEnum):
    AVAILABLE = "available"
    ABSENT = "absent"
    FUTURE = "future"
    CORRUPT = "corrupt"
    INCOMPATIBLE = "incompatible"


class SnapshotConsistency(StrEnum):
    STABLE = "stable"
    SNAPSHOT_CHANGED = "snapshot_changed"


class KnowledgeCompleteness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NO_EVIDENCE = "no_evidence"
    UNSUPPORTED = "unsupported"


class KnowledgeTimingPhase(StrEnum):
    PLANNER = "planner"
    SNAPSHOT_BEFORE = "snapshot_before"
    OWNER_RANKING = "owner_ranking"
    FUSION = "fusion"
    BROKER = "broker"
    SNAPSHOT_AFTER = "snapshot_after"
    CONTEXT_COMPILE = "context_compile"


class KnowledgeTelemetryOperation(StrEnum):
    SEARCH = "search"
    CONTEXT = "context"


def _required_text(name: str, value: str) -> str:
    return _contract_required_text_impl(name, value)


def _optional_text(name: str, value: str | None) -> str | None:
    return _contract_optional_text_impl(name, value)


def _base_payload(kind: str) -> dict[str, object]:
    return _contract_payloads.base_payload(
        kind,
        schema_version=KNOWLEDGE_CONTRACT_SCHEMA_VERSION,
    )


def _canonical_output(payload: Mapping[str, object]) -> str:
    return _contract_payloads.canonical_output(
        payload,
        canonical_json_fn=canonical_json,
    )


@dataclass(frozen=True, slots=True)
class KnowledgeTelemetryClock:
    """One callable and the explicit timing domain its readings belong to."""

    read_ns: Callable[[], int] = field(
        default=time.perf_counter_ns,
        compare=False,
        repr=False,
    )
    signature: str = KNOWLEDGE_TELEMETRY_CLOCK_SIGNATURE

    def __post_init__(self) -> None:
        _contract_telemetry.validate_telemetry_clock(
            self,
            required_text_fn=_required_text,
            default_signature=KNOWLEDGE_TELEMETRY_CLOCK_SIGNATURE,
            max_signature_chars=MAX_KNOWLEDGE_TELEMETRY_CLOCK_SIGNATURE_CHARS,
            perf_counter_ns=time.perf_counter_ns,
        )

    @classmethod
    def from_legacy(
        cls,
        clock_ns: Callable[[], int] | None,
    ) -> KnowledgeTelemetryClock:
        """Preserve callable injection without claiming an unidentified domain."""

        return _contract_telemetry.telemetry_clock_from_legacy(
            cls,
            clock_ns,
            perf_counter_ns=time.perf_counter_ns,
            unidentified_signature=KNOWLEDGE_TELEMETRY_UNIDENTIFIED_CLOCK_SIGNATURE,
        )

    @property
    def identified(self) -> bool:
        return _contract_telemetry.telemetry_clock_identified(
            self.signature,
            unidentified_signature=KNOWLEDGE_TELEMETRY_UNIDENTIFIED_CLOCK_SIGNATURE,
        )

    def compatible_with(
        self,
        signature: str,
        *,
        trust_unidentified: bool = False,
    ) -> bool:
        return _contract_telemetry.telemetry_clock_compatible(
            self.signature,
            signature,
            identified=self.identified,
            trust_unidentified=trust_unidentified,
        )

    def now_ns(self) -> int:
        return _contract_telemetry.telemetry_clock_now_ns(self.read_ns)


@dataclass(frozen=True, slots=True)
class KnowledgePhaseTiming:
    """One bounded monotonic duration from a successful Knowledge operation."""

    phase: KnowledgeTimingPhase
    duration_ns: int
    service_attempt: int = 0
    owner: str | None = None
    ranking_names: tuple[str, ...] = ()
    snapshot_id: str | None = None
    executed: bool = True

    def __post_init__(self) -> None:
        _contract_telemetry.validate_phase_timing(
            self,
            timing_phase_type=KnowledgeTimingPhase,
            required_text_fn=_required_text,
            optional_text_fn=_optional_text,
            max_name_chars=MAX_KNOWLEDGE_TELEMETRY_NAME_CHARS,
            max_snapshot_id_chars=MAX_KNOWLEDGE_TELEMETRY_SNAPSHOT_ID_CHARS,
            max_rankings_per_phase=MAX_KNOWLEDGE_TELEMETRY_RANKINGS_PER_PHASE,
            max_ranking_chars_per_phase=(
                MAX_KNOWLEDGE_TELEMETRY_RANKING_CHARS_PER_PHASE
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.knowledge_phase_timing_payload(self)


@dataclass(frozen=True, slots=True)
class KnowledgeQueryTelemetry:
    """Versioned in-memory timing envelope; never participates in identities."""

    operation: KnowledgeTelemetryOperation
    total_duration_ns: int
    phases: tuple[KnowledgePhaseTiming, ...]
    clock_signature: str = KNOWLEDGE_TELEMETRY_CLOCK_SIGNATURE

    def __post_init__(self) -> None:
        _contract_telemetry.validate_query_telemetry(
            self,
            telemetry_operation_type=KnowledgeTelemetryOperation,
            phase_timing_type=KnowledgePhaseTiming,
            timing_phase_type=KnowledgeTimingPhase,
            required_text_fn=_required_text,
            max_clock_signature_chars=MAX_KNOWLEDGE_TELEMETRY_CLOCK_SIGNATURE_CHARS,
            max_phases=MAX_KNOWLEDGE_TELEMETRY_PHASES,
        )

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.knowledge_query_telemetry_payload(
            self, telemetry_schema_version=KNOWLEDGE_TELEMETRY_SCHEMA_VERSION
        )

    def to_json(self) -> str:
        return _canonical_output(self.to_dict())


# endregion [01]


# region [02] Resource, revision and heterogeneous evidence


@dataclass(frozen=True, slots=True)
class PhysicalIdentityRef:
    scheme: str
    value: str
    identity_version: int

    def __post_init__(self) -> None:
        _contract_references.validate_physical_identity_ref(
            self, required_text_fn=_required_text
        )

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.physical_identity_ref_payload(self)


@dataclass(frozen=True, slots=True)
class ResourceRef:
    resource_id: str
    source_kind: str
    owner: str
    physical_identity: PhysicalIdentityRef | None = None
    current_path: str | None = None
    disposition: ResourceDisposition | None = None
    canonical_resource_id: str | None = None

    def __post_init__(self) -> None:
        _contract_references.validate_resource_ref(
            self,
            required_text_fn=_required_text,
            optional_text_fn=_optional_text,
            resource_disposition_type=ResourceDisposition,
        )

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.resource_ref_payload(
            self, base_payload_fn=_base_payload
        )


@dataclass(frozen=True, slots=True)
class RevisionRef:
    resource_id: str
    revision_id: str
    producer: str
    processing_signature: str
    generation: int | None
    state: RevisionState
    observed_at_utc: str | None = None

    def __post_init__(self) -> None:
        _contract_references.validate_revision_ref(
            self,
            required_text_fn=_required_text,
            optional_text_fn=_optional_text,
        )

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.revision_ref_payload(
            self, base_payload_fn=_base_payload
        )


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: str
    resource_id: str
    revision_id: str
    method: EvidenceMethod
    page: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    sheet: str | None = None
    cell_range: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    bounding_box: tuple[float, float, float, float] | None = None
    coordinate_space: str | None = None
    start_char: int | None = None
    end_char: int | None = None
    symbol: str | None = None
    section_kind: str | None = None
    section_id: str | None = None
    snippet: str | None = None
    extractor: str | None = None
    extractor_version: str | None = None
    generation: int | None = None
    identifiers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _contract_references.validate_evidence_ref(
            self,
            required_text_fn=_required_text,
            optional_text_fn=_optional_text,
            max_snippet_chars=MAX_KNOWLEDGE_SNIPPET_CHARS,
            max_symbol_chars=MAX_EVIDENCE_SYMBOL_CHARS,
            max_identifiers=MAX_EVIDENCE_IDENTIFIERS,
            max_identifier_component_chars=(MAX_EVIDENCE_IDENTIFIER_COMPONENT_CHARS),
        )

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.evidence_ref_payload(
            self, base_payload_fn=_base_payload
        )


# endregion [02]


# region [03] Ranking and hit contract


@dataclass(frozen=True, slots=True)
class RankingSignal:
    source: str
    score_kind: str
    raw_score: float
    source_rank: int
    model_signature: str | None = None
    generation: int | None = None
    contribution: float | None = None
    query_model_signature: str | None = None

    def __post_init__(self) -> None:
        _contract_references.validate_ranking_signal(
            self,
            required_text_fn=_required_text,
            optional_text_fn=_optional_text,
        )

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.ranking_signal_payload(self)


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    rank: int
    resource: ResourceRef
    revision: RevisionRef
    evidence: EvidenceRef
    signals: tuple[RankingSignal, ...]
    fused_score: float
    reasons: tuple[str, ...]
    confidence: float | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _contract_references.validate_knowledge_hit(
            self, required_text_fn=_required_text
        )

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.knowledge_hit_payload(
            self, base_payload_fn=_base_payload
        )

    def to_json(self) -> str:
        return _canonical_output(self.to_dict())


# endregion [03]


# region [04] Logical cross-owner snapshot


@dataclass(frozen=True, slots=True)
class PublicationHead:
    scope: str
    publication_id: str
    generation: int
    model_signature: str | None = None

    def __post_init__(self) -> None:
        _contract_snapshot.validate_publication_head(
            self,
            required_text_fn=_required_text,
            optional_text_fn=_optional_text,
        )

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.publication_head_payload(self)


@dataclass(frozen=True, slots=True)
class LogicalWatermark:
    name: str
    value: str

    def __post_init__(self) -> None:
        _contract_snapshot.validate_logical_watermark(
            self, required_text_fn=_required_text
        )

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.logical_watermark_payload(self)


@dataclass(frozen=True, slots=True)
class ActiveModel:
    signature: str
    vector_space: str
    modality: str
    dimensions: int
    generation: int

    def __post_init__(self) -> None:
        _contract_snapshot.validate_active_model(self, required_text_fn=_required_text)

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.active_model_payload(self)


@dataclass(frozen=True, slots=True)
class OwnerSnapshot:
    owner: str
    state: OwnerAvailability
    expected_schema_version: int
    observed_schema_version: int | None = None
    publications: tuple[PublicationHead, ...] = ()
    watermarks: tuple[LogicalWatermark, ...] = ()
    data_version_before: int | None = None
    data_version_after: int | None = None
    warning: str | None = None
    error_code: str | None = None
    identity_changed: bool = False

    def __post_init__(self) -> None:
        _contract_snapshot.validate_owner_snapshot(
            self,
            required_text_fn=_required_text,
            optional_text_fn=_optional_text,
            owner_availability_type=OwnerAvailability,
        )

    @property
    def changed(self) -> bool:
        return _contract_snapshot.owner_snapshot_changed(self)

    def identity_dict(self) -> dict[str, object]:
        return _contract_payloads.owner_snapshot_identity_payload(self)

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.owner_snapshot_payload(self)


@dataclass(frozen=True, slots=True)
class KnowledgeSnapshot:
    source_version: str
    captured_at_utc: str
    captured_monotonic_ns: int
    owners: tuple[OwnerSnapshot, ...]
    active_models: tuple[ActiveModel, ...]
    snapshot_id: str
    consistency: SnapshotConsistency = SnapshotConsistency.STABLE
    attempts: int = 1
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _contract_snapshot.validate_knowledge_snapshot(
            self,
            required_text_fn=_required_text,
            snapshot_consistency_type=SnapshotConsistency,
            owner_availability_type=OwnerAvailability,
        )

    @classmethod
    def create(
        cls,
        *,
        source_version: str,
        captured_at_utc: str,
        captured_monotonic_ns: int,
        owners: tuple[OwnerSnapshot, ...],
        active_models: tuple[ActiveModel, ...] = (),
        consistency: SnapshotConsistency = SnapshotConsistency.STABLE,
        attempts: int = 1,
        warnings: tuple[str, ...] = (),
    ) -> KnowledgeSnapshot:
        return _contract_snapshot.create_knowledge_snapshot(
            cls,
            source_version=source_version,
            captured_at_utc=captured_at_utc,
            captured_monotonic_ns=captured_monotonic_ns,
            owners=owners,
            active_models=active_models,
            consistency=consistency,
            attempts=attempts,
            warnings=warnings,
            schema_version=KNOWLEDGE_CONTRACT_SCHEMA_VERSION,
            canonical_json_fn=canonical_json,
            fingerprint_text_fn=fingerprint_text,
        )

    @property
    def changed_owners(self) -> tuple[str, ...]:
        return _contract_snapshot.knowledge_snapshot_changed_owners(self)

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.knowledge_snapshot_payload(
            self, base_payload_fn=_base_payload
        )

    def to_json(self) -> str:
        return _canonical_output(self.to_dict())


# endregion [04]


# region [05] Context bundle envelope


def _validate_context_plan_values(name: str, values: tuple[str, ...]) -> None:
    _contract_context.validate_context_plan_values(
        name,
        values,
        required_text_fn=_required_text,
        max_values=MAX_CONTEXT_PLAN_VALUES,
        max_value_chars=MAX_CONTEXT_PLAN_VALUE_CHARS,
        max_total_value_chars=MAX_CONTEXT_PLAN_TOTAL_VALUE_CHARS,
    )


@dataclass(frozen=True, slots=True)
class ContextPlanStepRef:
    channel: str
    ranking_name: str
    reason: str
    candidate_limit: int
    required: bool

    def __post_init__(self) -> None:
        _contract_context.validate_context_plan_step_ref(
            self,
            required_text_fn=_required_text,
            max_value_chars=MAX_CONTEXT_PLAN_VALUE_CHARS,
        )

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.context_plan_step_ref_payload(
            self, base_payload_fn=_base_payload
        )

    def to_json(self) -> str:
        return _canonical_output(self.to_dict())


@dataclass(frozen=True, slots=True)
class ContextPlanRef:
    plan_id: str
    normalized_query: str
    retrieval_mode: str
    intents: tuple[str, ...]
    exact_terms: tuple[str, ...]
    source_kinds: tuple[str, ...]
    formats: tuple[str, ...]
    project: str | None
    date_from: str | None
    date_to: str | None
    include_history: bool
    limit: int
    max_per_resource: int
    min_section_distance: int
    max_vectors: int
    steps: tuple[ContextPlanStepRef, ...]
    notices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _contract_context.validate_context_plan_ref(
            self,
            required_text_fn=_required_text,
            optional_text_fn=_optional_text,
            validate_values_fn=_validate_context_plan_values,
            max_value_chars=MAX_CONTEXT_PLAN_VALUE_CHARS,
            max_steps=MAX_CONTEXT_PLAN_STEPS,
            plan_step_type=ContextPlanStepRef,
        )

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.context_plan_ref_payload(
            self, base_payload_fn=_base_payload
        )

    def to_json(self) -> str:
        return _canonical_output(self.to_dict())


@dataclass(frozen=True, slots=True)
class ContextGraphBudget:
    identifiers_considered: int
    entities_included: int
    relations_included: int
    omitted_identifiers: int = 0
    omitted_entities: int = 0
    omitted_relations: int = 0
    identifier_limit_per_evidence: int = MAX_EVIDENCE_IDENTIFIERS
    measurement_scope: str = "selected_evidence_graph"

    def __post_init__(self) -> None:
        _contract_context.validate_context_graph_budget(
            self,
            max_evidence_identifiers=MAX_EVIDENCE_IDENTIFIERS,
        )

    @property
    def omitted_total(self) -> int:
        return _contract_context.context_graph_omitted_total(self)

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.context_graph_budget_payload(self)


@dataclass(frozen=True, slots=True)
class ContextBudget:
    character_limit: int
    characters_used: int
    estimated_tokens: int
    estimator_signature: str
    omitted_candidates: int = 0
    truncated_evidence_ids: tuple[str, ...] = ()
    measurement_scope: str = "rendered_context"

    def __post_init__(self) -> None:
        _contract_context.validate_context_budget(self, required_text_fn=_required_text)

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.context_budget_payload(self)


def _validate_context_references(
    name: str,
    references: tuple[str, ...],
) -> None:
    _contract_context.validate_context_references(
        name,
        references,
        required_text_fn=_required_text,
    )


@dataclass(frozen=True, slots=True)
class ContextEntityRef:
    entity_id: str
    entity_kind: str
    label: str
    evidence_ids: tuple[str, ...]
    resource_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _contract_context.validate_context_entity_ref(
            self,
            required_text_fn=_required_text,
            validate_references_fn=_validate_context_references,
        )

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.context_entity_ref_payload(
            self, base_payload_fn=_base_payload
        )

    def to_json(self) -> str:
        return _canonical_output(self.to_dict())


@dataclass(frozen=True, slots=True)
class ContextContradictionRef:
    contradiction_id: str
    contradiction_kind: str
    topic: str
    values: tuple[str, ...]
    citation_ids: tuple[str, ...]

    @staticmethod
    def _stable_id(
        contradiction_kind: str,
        topic: str,
        values: tuple[str, ...],
    ) -> str:
        return _contract_context.context_contradiction_stable_id(
            contradiction_kind,
            topic,
            values,
            canonical_json_fn=canonical_json,
            fingerprint_text_fn=fingerprint_text,
        )

    @classmethod
    def create(
        cls,
        *,
        contradiction_kind: str,
        topic: str,
        values: tuple[str, ...],
        citation_ids: tuple[str, ...],
    ) -> "ContextContradictionRef":
        return _contract_context.create_context_contradiction(
            cls,
            contradiction_kind=contradiction_kind,
            topic=topic,
            values=values,
            citation_ids=citation_ids,
        )

    def __post_init__(self) -> None:
        _contract_context.validate_context_contradiction(
            self,
            required_text_fn=_required_text,
            validate_references_fn=_validate_context_references,
        )

    @property
    def summary(self) -> str:
        return _contract_context.context_contradiction_summary(self)

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.context_contradiction_ref_payload(
            self, base_payload_fn=_base_payload
        )

    def to_json(self) -> str:
        return _canonical_output(self.to_dict())


@dataclass(frozen=True, slots=True)
class ContextRelationRef:
    relation_id: str
    source_entity_id: str
    target_entity_id: str
    relation_kind: str
    method: EvidenceMethod
    provenance: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    confidence: float | None = None

    def __post_init__(self) -> None:
        _contract_context.validate_context_relation_ref(
            self,
            required_text_fn=_required_text,
            validate_references_fn=_validate_context_references,
            evidence_method_type=EvidenceMethod,
            max_provenance_items=MAX_CONTEXT_RELATION_PROVENANCE_ITEMS,
            max_provenance_chars=MAX_CONTEXT_RELATION_PROVENANCE_CHARS,
        )

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.context_relation_ref_payload(
            self, base_payload_fn=_base_payload
        )

    def to_json(self) -> str:
        return _canonical_output(self.to_dict())


@dataclass(frozen=True, slots=True)
class ContextBundle:
    normalized_query: str
    intents: tuple[str, ...]
    plan_id: str
    plan: ContextPlanRef
    snapshot: KnowledgeSnapshot
    selected_hits: tuple[KnowledgeHit, ...]
    citation_ids: tuple[tuple[str, str], ...]
    graph_budget: ContextGraphBudget
    budget: ContextBudget
    rendered_context: str
    completeness: KnowledgeCompleteness
    entities: tuple[ContextEntityRef, ...] = ()
    relations: tuple[ContextRelationRef, ...] = ()
    contradictions: tuple[ContextContradictionRef, ...] = ()
    missing_information: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    telemetry: KnowledgeQueryTelemetry | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    blocking_owners: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _contract_context.validate_context_bundle(
            self,
            required_text_fn=_required_text,
            knowledge_completeness_type=KnowledgeCompleteness,
            telemetry_type=KnowledgeQueryTelemetry,
            telemetry_operation_type=KnowledgeTelemetryOperation,
        )

    def to_dict(self) -> dict[str, object]:
        return _contract_payloads.context_bundle_payload(
            self, base_payload_fn=_base_payload
        )

    def to_json(self) -> str:
        return _canonical_output(self.to_dict())


# endregion [05]


__all__ = (  # noqa: RUF022
    "KNOWLEDGE_CONTRACT_SCHEMA_VERSION",
    "KNOWLEDGE_TELEMETRY_CLOCK_SIGNATURE",
    "KNOWLEDGE_TELEMETRY_UNIDENTIFIED_CLOCK_SIGNATURE",
    "KNOWLEDGE_TELEMETRY_SCHEMA_VERSION",
    "MAX_EVIDENCE_IDENTIFIER_COMPONENT_CHARS",
    "MAX_EVIDENCE_IDENTIFIERS",
    "MAX_EVIDENCE_SYMBOL_CHARS",
    "ActiveModel",
    "ContextBudget",
    "ContextBundle",
    "ContextContradictionRef",
    "ContextEntityRef",
    "ContextGraphBudget",
    "ContextPlanRef",
    "ContextPlanStepRef",
    "ContextRelationRef",
    "EvidenceMethod",
    "EvidenceRef",
    "KnowledgeCompleteness",
    "KnowledgeHit",
    "KnowledgePhaseTiming",
    "KnowledgeQueryTelemetry",
    "KnowledgeSnapshot",
    "KnowledgeTelemetryClock",
    "KnowledgeTelemetryOperation",
    "KnowledgeTimingPhase",
    "LogicalWatermark",
    "OwnerAvailability",
    "OwnerSnapshot",
    "PhysicalIdentityRef",
    "PublicationHead",
    "RankingSignal",
    "ResourceDisposition",
    "ResourceRef",
    "RevisionRef",
    "RevisionState",
    "SnapshotConsistency",
)


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.knowledge_contracts")
