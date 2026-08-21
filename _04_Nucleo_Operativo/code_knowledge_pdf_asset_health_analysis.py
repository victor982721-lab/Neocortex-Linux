"""Static Code evidence for the PDF Knowledge asset-health boundary.

This module resolves only existing, versioned ownership, store, route, page
state and public read contracts.  It does not inspect live PDF rows, execute a
PDF route, or claim that the current Text-specific health reader already
proves PDF health.  Partial, protected and recovery causality therefore remain
decision-level claims that require the registered isolated experiment.
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
from .document_catalog_schema import CATALOG_SCHEMA_VERSION
from .knowledge_asset_health import KNOWLEDGE_ASSET_HEALTH_SOURCE_VERSION
from .knowledge_asset_health_contracts import (
    KNOWLEDGE_ASSET_HEALTH_CONTRACT_VERSION,
    KNOWLEDGE_ASSET_HEALTH_SCHEMA,
    KnowledgeAssetHealthQuery,
    KnowledgeAssetHealthReport,
    KnowledgeAssetIdentity,
    parse_knowledge_asset_resource_id,
)
from .knowledge_contracts import KNOWLEDGE_CONTRACT_SCHEMA_VERSION
from .logical_owner_contracts import LOGICAL_OWNER_SPECS, matching_logical_owners
from .pdf_route_models import (
    ALGORITHM_VERSION as PDF_ROUTE_VERSION,
    FAILURE_DETECTOR_VERSION as PDF_FAILURE_VERSION,
    STRUCTURAL_RECOVERY_VERSION as PDF_STRUCTURAL_RECOVERY_VERSION,
)
from .pdf_schema import PDF_SCHEMA_VERSION
from .state_topology_contracts import STATE_STORE_REGISTRY, STATE_STORE_REGISTRY_SCHEMA


CODE_KNOWLEDGE_PDF_ASSET_HEALTH_ANALYSIS_SCHEMA = (
    "neocortex.code-knowledge-pdf-asset-health-analysis/v1"
)
CODE_KNOWLEDGE_PDF_ASSET_HEALTH_POLICY = "knowledge-pdf-asset-health-contract-projection-v1"
CODE_KNOWLEDGE_PDF_ASSET_HEALTH_RESOLVER = "knowledge-pdf-asset-health-contract-resolver"
CODE_KNOWLEDGE_PDF_ASSET_HEALTH_RESOLVER_VERSION = "v1"

KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION_ID = (
    "knowledge.pdf_asset_health_preserves_page_partial_protected_and_recovery_causality"
)
KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION_VERSION = "v1"
KNOWLEDGE_PDF_ASSET_HEALTH_SUBJECT_KEY = "capability:knowledge-asset-health:pdf"

_OWNER_STORE_REQUIREMENT = "knowledge_pdf_asset_health_owner_store_contract"
_PAGE_RECOVERY_REQUIREMENT = "knowledge_pdf_asset_health_page_state_and_recovery_contract"
_PUBLIC_READ_REQUIREMENT = "knowledge_pdf_asset_health_public_read_contract"
_COUNTEREVIDENCE_REQUIREMENT = (
    "knowledge_pdf_asset_health_partial_protected_recovery_counterevidence_evaluated"
)
_EXPERIMENT_REQUIREMENT = "isolated_knowledge_pdf_asset_health_causal_experiment_result"

_LOGICAL_OWNER_ID = "pdf"
_STATE_OWNER_ID = "pdf"
_STATE_STORE_ID = "sqlite:pdf.sqlite3"
_DATABASE_NAME = "pdf.sqlite3"
_INTEGRATED_PHASES = ("extraction", "text_dedup", "derived", "catalog")
_NONTERMINAL_DOCUMENT_STATUSES = ("processing",)
_TERMINAL_DOCUMENT_STATUSES = ("done", "partial", "protected", "error")
_CATALOG_ACCEPTED_DOCUMENT_STATUSES = ("done", "partial")
_RESOURCE_ID_SCHEME = "resource:file:{volume_id}:{file_id}:{birthtime_ns}"
_IDENTITY_COMPONENTS = ("volume_id", "file_id", "birthtime_ns")
_PUBLIC_READ_MODULE = "neocortex.read_api"
_PUBLIC_READ_SYMBOL = "asset_health_payload"
_SERVICE_MODULE = "_04_Nucleo_Operativo.knowledge_asset_health"
_SERVICE_SYMBOL = "inspect_knowledge_asset_health"

_LIMITATIONS = (
    "static_contracts_do_not_observe_pdf_rows_or_a_public_pdf_health_execution",
    "versioned_phase_and_status_declarations_do_not_prove_page_level_causality",
    "process_recovery_experiments_do_not_prove_power_loss_atomicity",
    "no_semantic_ocr_or_visual_fidelity_claim_is_made",
    "knowledge_pdf_asset_health_is_advisory_read_only_and_never_authorizes_mutation",
)


KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION = AnalysisQuestionSpec(
    question_id=KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION_ID,
    version=KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION_VERSION,
    subject_kinds=("capability",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            _OWNER_STORE_REQUIREMENT,
            "question",
            "supporting",
            ("contract",),
        ),
        AnalysisEvidenceRequirementSpec(
            _PAGE_RECOVERY_REQUIREMENT,
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
        "public_health_preserves_one_snapshot_bound_pdf_inventory_page_catalog_and_search_causal_trace",
        "partial_protected_or_recovered_pdf_state_can_be_misreported_without_runtime_counterevidence",
    ),
    counterevidence_rules=(
        "processing_pdf_state_is_nonterminal_and_cannot_support_a_complete_health_claim",
        "catalog_may_consume_only_done_or_partial_pdf_state_not_processing_protected_or_error",
        "partial_pdf_state_must_preserve_page_level_incompleteness_and_diagnostic_evidence",
        "structural_recovery_must_not_cross_join_stale_pages_or_hide_protected_and_error_state",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "run_knowledge_pdf_asset_health_causal_experiment",
            "experiment",
            "Run isolated aligned, page-partial, protected, interrupted-recovery, catalog and "
            "snapshot-fence scenarios through the read-only PDF asset-health boundary.",
        ),
    ),
)


def _required_text(label: str, value: object, *, maximum: int = 1_024) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > maximum:
        raise ValueError(f"{label} is invalid")
    return value


def _live_contract_projection() -> dict[str, object]:
    owner_matches = matching_logical_owners("_04_Nucleo_Operativo.pdf_route")
    owner_specs = tuple(item for item in LOGICAL_OWNER_SPECS if item.owner_id == _LOGICAL_OWNER_ID)
    if (
        owner_matches != (_LOGICAL_OWNER_ID,)
        or len(owner_specs) != 1
        or owner_specs[0].state_owner_ids != (_STATE_OWNER_ID,)
    ):
        raise ValueError("PDF Knowledge asset-health logical ownership contract is incompatible")

    store = STATE_STORE_REGISTRY.by_owner(_STATE_OWNER_ID)
    if (
        store.state_store_id != _STATE_STORE_ID
        or store.database_name != _DATABASE_NAME
        or store.storage_engine != "sqlite"
        or store.expected_schema_version != 13
        or store.knowledge_read_kind != "documents"
        or store.knowledge_capture_mode != "configured"
        or PDF_SCHEMA_VERSION != 13
        or INVENTORY_SCHEMA_VERSION != 10
        or CATALOG_SCHEMA_VERSION != 7
        or KNOWLEDGE_CONTRACT_SCHEMA_VERSION != 1
    ):
        raise ValueError("PDF Knowledge asset-health owner/store contract is incompatible")
    if (
        PDF_ROUTE_VERSION != "pdf-route-v3"
        or PDF_FAILURE_VERSION != "pdf-failure-v3"
        or PDF_STRUCTURAL_RECOVERY_VERSION != "pdf-structural-recovery-v2"
    ):
        raise ValueError("PDF Knowledge asset-health route contract is incompatible")

    identity = KnowledgeAssetIdentity(1, 2, -1)
    query = KnowledgeAssetHealthQuery(identity.resource_id)
    if (
        query.identity != identity
        or parse_knowledge_asset_resource_id(identity.resource_id) != identity
        or identity.resource_id != "resource:file:1:2:-1"
    ):
        raise ValueError("PDF Knowledge asset-health identity contract is incompatible")

    public_read = getattr(import_module(_PUBLIC_READ_MODULE), _PUBLIC_READ_SYMBOL, None)
    service = getattr(import_module(_SERVICE_MODULE), _SERVICE_SYMBOL, None)
    if not callable(public_read) or not callable(service):
        raise ValueError("PDF Knowledge asset-health public read contract is unavailable")
    report_fields = KnowledgeAssetHealthReport.__dataclass_fields__
    if (
        KNOWLEDGE_ASSET_HEALTH_CONTRACT_VERSION != 1
        or report_fields["operation"].default != "knowledge-health"
        or report_fields["read_only"].default is not True
        or report_fields["advisory_only"].default is not True
        or report_fields["mutation_authorized"].default is not False
    ):
        raise ValueError("PDF Knowledge asset-health authority contract is incompatible")

    return {
        "policy_id": CODE_KNOWLEDGE_PDF_ASSET_HEALTH_POLICY,
        "state_store_registry_schema": STATE_STORE_REGISTRY_SCHEMA,
        "logical_owner_id": _LOGICAL_OWNER_ID,
        "state_owner_id": _STATE_OWNER_ID,
        "state_store_id": store.state_store_id,
        "database_name": store.database_name,
        "pdf_schema_version": PDF_SCHEMA_VERSION,
        "inventory_schema_version": INVENTORY_SCHEMA_VERSION,
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "knowledge_contract_version": KNOWLEDGE_CONTRACT_SCHEMA_VERSION,
        "pdf_route_version": PDF_ROUTE_VERSION,
        "pdf_failure_version": PDF_FAILURE_VERSION,
        "pdf_structural_recovery_version": PDF_STRUCTURAL_RECOVERY_VERSION,
        "integrated_phases": _INTEGRATED_PHASES,
        "nonterminal_document_statuses": _NONTERMINAL_DOCUMENT_STATUSES,
        "terminal_document_statuses": _TERMINAL_DOCUMENT_STATUSES,
        "catalog_accepted_document_statuses": _CATALOG_ACCEPTED_DOCUMENT_STATUSES,
        "health_contract_version": KNOWLEDGE_ASSET_HEALTH_CONTRACT_VERSION,
        "health_schema": KNOWLEDGE_ASSET_HEALTH_SCHEMA,
        "health_source_version": KNOWLEDGE_ASSET_HEALTH_SOURCE_VERSION,
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
class KnowledgePdfAssetHealthContractAnalysis:
    analysis_id: str
    policy_id: str
    state_store_registry_schema: str
    logical_owner_id: str
    state_owner_id: str
    state_store_id: str
    database_name: str
    pdf_schema_version: int
    inventory_schema_version: int
    catalog_schema_version: int
    knowledge_contract_version: int
    pdf_route_version: str
    pdf_failure_version: str
    pdf_structural_recovery_version: str
    integrated_phases: tuple[str, ...]
    nonterminal_document_statuses: tuple[str, ...]
    terminal_document_statuses: tuple[str, ...]
    catalog_accepted_document_statuses: tuple[str, ...]
    health_contract_version: int
    health_schema: str
    health_source_version: str
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
        _required_text("PDF Knowledge asset-health analysis id", self.analysis_id)
        expected = _live_contract_projection()
        actual = {key: value for key, value in asdict(self).items() if key != "analysis_id"}
        if actual != expected:
            raise ValueError("PDF Knowledge asset-health analysis fields are incompatible")
        expected_id = analysis_identity("code-knowledge-pdf-asset-health-analysis-v1", expected)
        if self.analysis_id != expected_id:
            raise ValueError("PDF Knowledge asset-health analysis identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": CODE_KNOWLEDGE_PDF_ASSET_HEALTH_ANALYSIS_SCHEMA,
            **asdict(self),
        }


def build_knowledge_pdf_asset_health_contract_analysis() -> KnowledgePdfAssetHealthContractAnalysis:
    """Re-resolve the exact static PDF owner, route and public-read projection."""

    projection = _live_contract_projection()
    return KnowledgePdfAssetHealthContractAnalysis(
        analysis_id=analysis_identity(
            "code-knowledge-pdf-asset-health-analysis-v1",
            projection,
        ),
        **projection,  # type: ignore[arg-type]
    )


def _contract_evidence(
    *,
    subject: AnalysisSubjectRef,
    analysis: KnowledgePdfAssetHealthContractAnalysis,
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
        producer_id=CODE_KNOWLEDGE_PDF_ASSET_HEALTH_RESOLVER,
        producer_version=CODE_KNOWLEDGE_PDF_ASSET_HEALTH_RESOLVER_VERSION,
        source_schema=CODE_KNOWLEDGE_PDF_ASSET_HEALTH_ANALYSIS_SCHEMA,
        source_record_kind=record_kind,
        source_record_id=analysis.analysis_id,
        source_projection_digest=digest,
        snapshot_id=subject.snapshot_id,
        revision_id=subject.revision_id,
        facts=facts,
        completeness="complete",
        bounded=True,
        truncated=False,
        resolver_id=CODE_KNOWLEDGE_PDF_ASSET_HEALTH_RESOLVER,
        resolver_version=CODE_KNOWLEDGE_PDF_ASSET_HEALTH_RESOLVER_VERSION,
        limitations=_LIMITATIONS,
    )


def knowledge_pdf_asset_health_questions(
    *,
    snapshot_id: str,
    snapshot_freshness: EvidenceFreshness,
    rank: int,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Publish static PDF readiness and require isolated runtime evidence."""

    _required_text("PDF Knowledge asset-health snapshot id", snapshot_id, maximum=2_048)
    if snapshot_freshness not in {"current", "publication_only", "unknown"}:
        raise ValueError("PDF Knowledge asset-health snapshot freshness is invalid")
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        raise ValueError("PDF Knowledge asset-health question rank must be positive")

    analysis = build_knowledge_pdf_asset_health_contract_analysis()
    spec = KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION
    fingerprint = analysis_question_spec_fingerprint(spec)
    subject = AnalysisSubjectRef(
        subject_kind="capability",
        subject_key=KNOWLEDGE_PDF_ASSET_HEALTH_SUBJECT_KEY,
        display_name="Knowledge PDF causal asset health",
        source_owner_id="code",
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
        revision_id=analysis.analysis_id,
    )

    owner_projection: dict[str, AnalysisScalar] = {
        "state_store_registry_schema": analysis.state_store_registry_schema,
        "logical_owner_id": analysis.logical_owner_id,
        "state_owner_id": analysis.state_owner_id,
        "state_store_id": analysis.state_store_id,
        "database_name": analysis.database_name,
        "pdf_schema_version": analysis.pdf_schema_version,
        "inventory_schema_version": analysis.inventory_schema_version,
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
    page_projection: dict[str, AnalysisScalar] = {
        "pdf_route_version": analysis.pdf_route_version,
        "pdf_failure_version": analysis.pdf_failure_version,
        "pdf_structural_recovery_version": analysis.pdf_structural_recovery_version,
        "integrated_phases": ",".join(analysis.integrated_phases),
        "nonterminal_document_statuses": ",".join(analysis.nonterminal_document_statuses),
        "terminal_document_statuses": ",".join(analysis.terminal_document_statuses),
        "catalog_accepted_document_statuses": ",".join(analysis.catalog_accepted_document_statuses),
    }
    page_evidence = _contract_evidence(
        subject=subject,
        analysis=analysis,
        record_kind=_PAGE_RECOVERY_REQUIREMENT,
        projection=page_projection,
        facts=tuple(AnalysisFact(name, value) for name, value in page_projection.items()),
    )
    read_projection: dict[str, AnalysisScalar] = {
        "health_contract_version": analysis.health_contract_version,
        "health_schema": analysis.health_schema,
        "health_source_version": analysis.health_source_version,
        "resource_id_scheme": analysis.resource_id_scheme,
        "identity_components": ",".join(analysis.identity_components),
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
            (owner_evidence, page_evidence, read_evidence),
            key=lambda item: item.evidence_id,
        )
    )
    requirements = (
        AnalysisRequirementEvaluation(
            _OWNER_STORE_REQUIREMENT,
            "satisfied",
            (owner_evidence.evidence_id,),
            "pdf_owner_store_and_inventory_catalog_knowledge_contracts_are_exactly_declared",
        ),
        AnalysisRequirementEvaluation(
            _PAGE_RECOVERY_REQUIREMENT,
            "satisfied",
            (page_evidence.evidence_id,),
            "pdf_phase_status_failure_and_structural_recovery_contracts_are_exactly_declared",
        ),
        AnalysisRequirementEvaluation(
            _PUBLIC_READ_REQUIREMENT,
            "satisfied",
            (read_evidence.evidence_id,),
            "public_health_read_is_versioned_read_only_advisory_and_non_mutating",
        ),
        AnalysisRequirementEvaluation(
            _COUNTEREVIDENCE_REQUIREMENT,
            "not_evaluated",
            (),
            "no_linked_partial_protected_recovery_or_snapshot_change_runtime_receipt",
        ),
        AnalysisRequirementEvaluation(
            _EXPERIMENT_REQUIREMENT,
            "missing",
            (),
            "no_linked_isolated_knowledge_pdf_asset_health_experiment_receipt",
        ),
    )
    evaluation = AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            "knowledge-pdf-asset-health-question-evaluation-v1",
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
    "CODE_KNOWLEDGE_PDF_ASSET_HEALTH_ANALYSIS_SCHEMA",
    "CODE_KNOWLEDGE_PDF_ASSET_HEALTH_POLICY",
    "KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION",
    "KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION_ID",
    "KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION_VERSION",
    "KNOWLEDGE_PDF_ASSET_HEALTH_SUBJECT_KEY",
    "KnowledgePdfAssetHealthContractAnalysis",
    "build_knowledge_pdf_asset_health_contract_analysis",
    "knowledge_pdf_asset_health_questions",
]
