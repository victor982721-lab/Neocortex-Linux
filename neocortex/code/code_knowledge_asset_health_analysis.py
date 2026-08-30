"""Static Code evidence for the public causal Knowledge asset-health contract.

This module resolves only versioned ownership, identity and read-surface
contracts.  It does not inspect a live owner database and cannot conclude that
an asset is healthy.  Runtime mismatch, absence, publication and snapshot-fence
claims require the registered isolated experiment receipt.
"""

from __future__ import annotations
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from importlib import import_module
from typing import Literal

from neocortex.deduplication.schema import SCHEMA_VERSION as INVENTORY_SCHEMA_VERSION

from .code_analysis_epistemics import (
    AnalysisEvidenceRef,
    AnalysisEvidenceRequirementSpec,
    AnalysisFact,
    AnalysisNextActionSpec,
    AnalysisQuestionEvaluation,
    AnalysisQuestionSpec,
    AnalysisRequirementEvaluation,
    AnalysisScalar,
    AnalysisSubjectRef,
    EvidenceFreshness,
    analysis_identity,
    analysis_question_spec_fingerprint,
    validate_analysis_question_evaluation,
)
from neocortex.documents.document_catalog_schema import CATALOG_SCHEMA_VERSION
from neocortex.knowledge.knowledge_asset_health import KNOWLEDGE_ASSET_HEALTH_SOURCE_VERSION
from neocortex.knowledge.knowledge_asset_health_contracts import (
    KNOWLEDGE_ASSET_HEALTH_CONTRACT_VERSION,
    KNOWLEDGE_ASSET_HEALTH_SCHEMA,
    KnowledgeAssetHealthQuery,
    KnowledgeAssetHealthReport,
    KnowledgeAssetIdentity,
    parse_knowledge_asset_resource_id,
)
from neocortex.knowledge.knowledge_contracts import KNOWLEDGE_CONTRACT_SCHEMA_VERSION
from neocortex.safety.state_topology_contracts import STATE_STORE_REGISTRY, STATE_STORE_REGISTRY_SCHEMA
from neocortex.capabilities.formats.text.text_route import TEXT_ROUTE_VERSION
from neocortex.capabilities.formats.text.text_state import TEXT_SCHEMA_VERSION


CODE_KNOWLEDGE_ASSET_HEALTH_ANALYSIS_SCHEMA = "neocortex.code-knowledge-asset-health-analysis/v1"
CODE_KNOWLEDGE_ASSET_HEALTH_POLICY = "knowledge-asset-health-contract-projection-v1"
CODE_KNOWLEDGE_ASSET_HEALTH_RESOLVER = "knowledge-asset-health-contract-resolver"
CODE_KNOWLEDGE_ASSET_HEALTH_RESOLVER_VERSION = "v1"

KNOWLEDGE_ASSET_HEALTH_QUESTION_ID = (
    "knowledge.asset_health_trace_is_snapshot_bound_and_causally_explainable"
)
KNOWLEDGE_ASSET_HEALTH_QUESTION_VERSION = "v1"
KNOWLEDGE_ASSET_HEALTH_SUBJECT_KEY = "capability:knowledge-asset-health"

_OWNER_STORE_REQUIREMENT = "knowledge_asset_health_owner_store_contract"
_CAUSAL_IDENTITY_REQUIREMENT = "knowledge_asset_health_causal_identity_contract"
_PUBLIC_READ_REQUIREMENT = "knowledge_asset_health_public_read_contract"
_COUNTEREVIDENCE_REQUIREMENT = (
    "knowledge_asset_health_stale_mismatch_and_absence_counterevidence_evaluated"
)
_EXPERIMENT_REQUIREMENT = "isolated_knowledge_asset_health_causal_experiment_result"

_RESOURCE_ID_SCHEME = "resource:file:{volume_id}:{file_id}:{birthtime_ns}"
_IDENTITY_COMPONENTS = ("volume_id", "file_id", "birthtime_ns")
_CAUSAL_STAGE_OWNERS = ("inventory", "text", "catalog", "knowledge")
_STATE_OWNER_IDS = ("inventory", "text", "catalog")
_PUBLIC_READ_MODULE = "neocortex.read_api"
_PUBLIC_READ_SYMBOL = "asset_health_payload"
_SERVICE_MODULE = "neocortex.knowledge.knowledge_asset_health"
_SERVICE_SYMBOL = "inspect_knowledge_asset_health"

_LIMITATIONS = (
    "static_contracts_do_not_observe_owner_rows_or_a_public_health_execution",
    "owner_availability_does_not_prove_a_four_stage_causal_trace",
    "resource_identity_shape_does_not_prove_search_and_health_selected_the_same_revision",
    "a_healthy_disposition_requires_complete_stable_runtime_evidence",
    "knowledge_asset_health_is_advisory_read_only_and_never_authorizes_mutation",
)


KNOWLEDGE_ASSET_HEALTH_QUESTION = AnalysisQuestionSpec(
    question_id=KNOWLEDGE_ASSET_HEALTH_QUESTION_ID,
    version=KNOWLEDGE_ASSET_HEALTH_QUESTION_VERSION,
    subject_kinds=("capability",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            _OWNER_STORE_REQUIREMENT,
            "question",
            "supporting",
            ("contract",),
        ),
        AnalysisEvidenceRequirementSpec(
            _CAUSAL_IDENTITY_REQUIREMENT,
            "question",
            "supporting",
            ("contract",),
        ),
        AnalysisEvidenceRequirementSpec(
            _PUBLIC_READ_REQUIREMENT,
            "question",
            "supporting",
            ("contract",),
        ),
        AnalysisEvidenceRequirementSpec(
            _COUNTEREVIDENCE_REQUIREMENT,
            "decision",
            "counterevidence",
            ("runtime_observation",),
        ),
        AnalysisEvidenceRequirementSpec(
            _EXPERIMENT_REQUIREMENT,
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "public_health_reads_one_stable_complete_causal_trace_for_the_exact_search_identity",
        "stale_mismatched_absent_unpublished_or_changed_owner_evidence_prevents_a_health_claim",
    ),
    counterevidence_rules=(
        "healthy_requires_one_complete_stable_inventory_text_catalog_and_search_trace",
        "mismatched_or_unpublished_identity_and_processing_evidence_cannot_be_cross_joined",
        "future_corrupt_incompatible_or_absent_owner_evidence_cannot_be_reported_healthy",
        "a_changed_knowledge_or_fact_snapshot_requires_abstention_after_one_bounded_retry",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "run_knowledge_asset_health_causal_experiment",
            "experiment",
            "Run isolated aligned, mismatch, absence, publication, identity and snapshot-fence "
            "scenarios through the read-only Knowledge asset-health service.",
        ),
    ),
)


def _required_text(label: str, value: object, *, maximum: int = 1_024) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > maximum:
        raise ValueError(f"{label} is invalid")
    return value


def _store_projection(owner: str) -> tuple[str, str, int]:
    store = STATE_STORE_REGISTRY.by_owner(owner)
    if store.storage_engine != "sqlite" or store.knowledge_capture_mode not in {
        "configured",
        "if_present",
    }:
        raise ValueError("Knowledge asset-health state-store contract is incompatible")
    return store.state_store_id, store.database_name, store.expected_schema_version


def _live_contract_projection() -> dict[str, object]:
    inventory = _store_projection("inventory")
    text = _store_projection("text")
    catalog = _store_projection("catalog")
    expected = {
        "inventory": ("sqlite:dedup.sqlite3", "dedup.sqlite3", INVENTORY_SCHEMA_VERSION),
        "text": ("sqlite:text.sqlite3", "text.sqlite3", TEXT_SCHEMA_VERSION),
        "catalog": (
            "sqlite:document_catalog.sqlite3",
            "document_catalog.sqlite3",
            CATALOG_SCHEMA_VERSION,
        ),
    }
    if {"inventory": inventory, "text": text, "catalog": catalog} != expected:
        raise ValueError("Knowledge asset-health owner/store projection is incompatible")
    if (
        INVENTORY_SCHEMA_VERSION != 10
        or TEXT_SCHEMA_VERSION != 2
        or CATALOG_SCHEMA_VERSION != 7
        or KNOWLEDGE_CONTRACT_SCHEMA_VERSION != 1
        or KNOWLEDGE_ASSET_HEALTH_CONTRACT_VERSION != 1
        or TEXT_ROUTE_VERSION != "text-route-v2"
    ):
        raise ValueError("Knowledge asset-health versioned contracts are incompatible")

    identity = KnowledgeAssetIdentity(1, 2, -1)
    query = KnowledgeAssetHealthQuery(identity.resource_id)
    if (
        query.identity != identity
        or parse_knowledge_asset_resource_id(identity.resource_id) != identity
        or identity.resource_id != "resource:file:1:2:-1"
    ):
        raise ValueError("Knowledge asset-health causal identity contract is incompatible")

    public_read = getattr(import_module(_PUBLIC_READ_MODULE), _PUBLIC_READ_SYMBOL, None)
    service = getattr(import_module(_SERVICE_MODULE), _SERVICE_SYMBOL, None)
    if not callable(public_read) or not callable(service):
        raise ValueError("Knowledge asset-health public read contract is unavailable")
    report_fields = KnowledgeAssetHealthReport.__dataclass_fields__
    if (
        report_fields["operation"].default != "knowledge-health"
        or report_fields["read_only"].default is not True
        or report_fields["advisory_only"].default is not True
        or report_fields["mutation_authorized"].default is not False
    ):
        raise ValueError("Knowledge asset-health authority contract is incompatible")

    return {
        "policy_id": CODE_KNOWLEDGE_ASSET_HEALTH_POLICY,
        "state_store_registry_schema": STATE_STORE_REGISTRY_SCHEMA,
        "causal_stage_owners": _CAUSAL_STAGE_OWNERS,
        "state_owner_ids": _STATE_OWNER_IDS,
        "state_store_ids": (inventory[0], text[0], catalog[0]),
        "inventory_schema_version": INVENTORY_SCHEMA_VERSION,
        "text_schema_version": TEXT_SCHEMA_VERSION,
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "knowledge_contract_version": KNOWLEDGE_CONTRACT_SCHEMA_VERSION,
        "health_contract_version": KNOWLEDGE_ASSET_HEALTH_CONTRACT_VERSION,
        "health_schema": KNOWLEDGE_ASSET_HEALTH_SCHEMA,
        "health_source_version": KNOWLEDGE_ASSET_HEALTH_SOURCE_VERSION,
        "text_route_version": TEXT_ROUTE_VERSION,
        "resource_id_scheme": _RESOURCE_ID_SCHEME,
        "identity_components": _IDENTITY_COMPONENTS,
        "public_read_module": _PUBLIC_READ_MODULE,
        "public_read_symbol": _PUBLIC_READ_SYMBOL,
        "service_module": _SERVICE_MODULE,
        "service_symbol": _SERVICE_SYMBOL,
        "operation": "knowledge-health",
        "read_only": True,
        "advisory_only": True,
        "authority": "advisory",
        "mutation_authority": False,
    }


@dataclass(frozen=True, slots=True)
class KnowledgeAssetHealthContractAnalysis:
    analysis_id: str
    policy_id: str
    state_store_registry_schema: str
    causal_stage_owners: tuple[str, ...]
    state_owner_ids: tuple[str, ...]
    state_store_ids: tuple[str, ...]
    inventory_schema_version: int
    text_schema_version: int
    catalog_schema_version: int
    knowledge_contract_version: int
    health_contract_version: int
    health_schema: str
    health_source_version: str
    text_route_version: str
    resource_id_scheme: str
    identity_components: tuple[str, ...]
    public_read_module: str
    public_read_symbol: str
    service_module: str
    service_symbol: str
    operation: str
    read_only: Literal[True]
    advisory_only: Literal[True]
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("Knowledge asset-health analysis id", self.analysis_id)
        expected = _live_contract_projection()
        actual = {key: value for key, value in asdict(self).items() if key != "analysis_id"}
        if actual != expected:
            raise ValueError("Knowledge asset-health contract analysis fields are incompatible")
        expected_id = analysis_identity("code-knowledge-asset-health-analysis-v1", expected)
        if self.analysis_id != expected_id:
            raise ValueError("Knowledge asset-health contract analysis identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": CODE_KNOWLEDGE_ASSET_HEALTH_ANALYSIS_SCHEMA, **asdict(self)}


def build_knowledge_asset_health_contract_analysis() -> KnowledgeAssetHealthContractAnalysis:
    """Re-resolve the exact static owner, identity and public-read projection."""

    projection = _live_contract_projection()
    return KnowledgeAssetHealthContractAnalysis(
        analysis_id=analysis_identity("code-knowledge-asset-health-analysis-v1", projection),
        **projection,  # type: ignore[arg-type]
    )


def _contract_evidence(
    *,
    subject: AnalysisSubjectRef,
    analysis: KnowledgeAssetHealthContractAnalysis,
    record_kind: str,
    projection: Mapping[str, object],
    facts: tuple[AnalysisFact, ...],
) -> AnalysisEvidenceRef:
    digest = analysis_identity(f"{record_kind}-source-v1", projection)
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            f"{record_kind}-evidence-v1",
            {
                "subject": subject.subject_key,
                "snapshot_id": subject.snapshot_id,
                "analysis_id": analysis.analysis_id,
                "projection_digest": digest,
            },
        ),
        subject_key=subject.subject_key,
        role="supporting",
        evidence_kind="contract",
        source_owner_id="code",
        producer_id=CODE_KNOWLEDGE_ASSET_HEALTH_RESOLVER,
        producer_version=CODE_KNOWLEDGE_ASSET_HEALTH_RESOLVER_VERSION,
        source_schema=CODE_KNOWLEDGE_ASSET_HEALTH_ANALYSIS_SCHEMA,
        source_record_kind=record_kind,
        source_record_id=analysis.analysis_id,
        source_projection_digest=digest,
        snapshot_id=subject.snapshot_id,
        revision_id=subject.revision_id,
        facts=facts,
        completeness="complete",
        bounded=True,
        truncated=False,
        resolver_id=CODE_KNOWLEDGE_ASSET_HEALTH_RESOLVER,
        resolver_version=CODE_KNOWLEDGE_ASSET_HEALTH_RESOLVER_VERSION,
        limitations=_LIMITATIONS,
    )


def knowledge_asset_health_questions(
    *,
    snapshot_id: str,
    snapshot_freshness: EvidenceFreshness,
    rank: int,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Publish static readiness and require isolated runtime decision evidence."""

    _required_text("Knowledge asset-health snapshot id", snapshot_id, maximum=2_048)
    if snapshot_freshness not in {"current", "publication_only", "unknown"}:
        raise ValueError("Knowledge asset-health snapshot freshness is invalid")
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        raise ValueError("Knowledge asset-health question rank must be positive")

    analysis = build_knowledge_asset_health_contract_analysis()
    spec = KNOWLEDGE_ASSET_HEALTH_QUESTION
    fingerprint = analysis_question_spec_fingerprint(spec)
    subject = AnalysisSubjectRef(
        subject_kind="capability",
        subject_key=KNOWLEDGE_ASSET_HEALTH_SUBJECT_KEY,
        display_name="Knowledge causal asset health",
        source_owner_id="code",
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
        revision_id=analysis.analysis_id,
    )

    owner_projection: dict[str, AnalysisScalar] = {
        "state_store_registry_schema": analysis.state_store_registry_schema,
        "causal_stage_owners": ",".join(analysis.causal_stage_owners),
        "state_owner_ids": ",".join(analysis.state_owner_ids),
        "state_store_ids": ",".join(analysis.state_store_ids),
        "inventory_schema_version": analysis.inventory_schema_version,
        "text_schema_version": analysis.text_schema_version,
        "catalog_schema_version": analysis.catalog_schema_version,
        "knowledge_contract_version": analysis.knowledge_contract_version,
    }
    owner_evidence = _contract_evidence(
        subject=subject,
        analysis=analysis,
        record_kind=_OWNER_STORE_REQUIREMENT,
        projection=owner_projection,
        facts=tuple(AnalysisFact(name, value) for name, value in owner_projection.items()),
    )
    identity_projection: dict[str, AnalysisScalar] = {
        "resource_id_scheme": analysis.resource_id_scheme,
        "identity_components": ",".join(analysis.identity_components),
        "text_route_version": analysis.text_route_version,
        "health_contract_version": analysis.health_contract_version,
    }
    identity_evidence = _contract_evidence(
        subject=subject,
        analysis=analysis,
        record_kind=_CAUSAL_IDENTITY_REQUIREMENT,
        projection=identity_projection,
        facts=tuple(AnalysisFact(name, value) for name, value in identity_projection.items()),
    )
    read_projection: dict[str, AnalysisScalar] = {
        "health_schema": analysis.health_schema,
        "health_source_version": analysis.health_source_version,
        "public_read": f"{analysis.public_read_module}.{analysis.public_read_symbol}",
        "service": f"{analysis.service_module}.{analysis.service_symbol}",
        "operation": analysis.operation,
        "read_only": analysis.read_only,
        "advisory_only": analysis.advisory_only,
        "mutation_authority": analysis.mutation_authority,
    }
    read_evidence = _contract_evidence(
        subject=subject,
        analysis=analysis,
        record_kind=_PUBLIC_READ_REQUIREMENT,
        projection=read_projection,
        facts=tuple(AnalysisFact(name, value) for name, value in read_projection.items()),
    )
    evidence = tuple(
        sorted(
            (owner_evidence, identity_evidence, read_evidence),
            key=lambda item: item.evidence_id,
        )
    )
    requirements = (
        AnalysisRequirementEvaluation(
            _OWNER_STORE_REQUIREMENT,
            "satisfied",
            (owner_evidence.evidence_id,),
            "inventory_text_catalog_and_knowledge_owner_contracts_are_exactly_declared",
        ),
        AnalysisRequirementEvaluation(
            _CAUSAL_IDENTITY_REQUIREMENT,
            "satisfied",
            (identity_evidence.evidence_id,),
            "search_and_health_share_the_canonical_physical_resource_identity_contract",
        ),
        AnalysisRequirementEvaluation(
            _PUBLIC_READ_REQUIREMENT,
            "satisfied",
            (read_evidence.evidence_id,),
            "public_read_service_is_versioned_read_only_advisory_and_non_mutating",
        ),
        AnalysisRequirementEvaluation(
            _COUNTEREVIDENCE_REQUIREMENT,
            "not_evaluated",
            (),
            "no_linked_stale_mismatch_absence_or_snapshot_change_runtime_receipt",
        ),
        AnalysisRequirementEvaluation(
            _EXPERIMENT_REQUIREMENT,
            "missing",
            (),
            "no_linked_isolated_knowledge_asset_health_experiment_receipt",
        ),
    )
    evaluation = AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            "knowledge-asset-health-question-evaluation-v1",
            {
                "snapshot_id": snapshot_id,
                "analysis_id": analysis.analysis_id,
                "spec": fingerprint,
                "evidence_ids": tuple(item.evidence_id for item in evidence),
            },
        ),
        question_id=spec.question_id,
        question_version=spec.version,
        question_spec_fingerprint=fingerprint,
        rank=rank,
        subject=subject,
        evidence=evidence,
        requirements=requirements,
        observation_status="confirmed",
        inference_status="abstained",
        inferences=(),
        hypotheses=spec.hypotheses,
        question_readiness="ready",
        decision_readiness="experiment_required",
        decision=None,
        decision_reason="decision_evidence_incomplete",
        counterevidence_status="not_evaluated",
        next_action_ids=tuple(item.action_id for item in spec.next_actions),
        limitations=_LIMITATIONS,
    )
    validate_analysis_question_evaluation(spec, evaluation)
    return (spec,), (evaluation,)


__all__ = [
    "CODE_KNOWLEDGE_ASSET_HEALTH_ANALYSIS_SCHEMA",
    "CODE_KNOWLEDGE_ASSET_HEALTH_POLICY",
    "KNOWLEDGE_ASSET_HEALTH_QUESTION",
    "KNOWLEDGE_ASSET_HEALTH_QUESTION_ID",
    "KNOWLEDGE_ASSET_HEALTH_QUESTION_VERSION",
    "KNOWLEDGE_ASSET_HEALTH_SUBJECT_KEY",
    "KnowledgeAssetHealthContractAnalysis",
    "build_knowledge_asset_health_contract_analysis",
    "knowledge_asset_health_questions",
]
