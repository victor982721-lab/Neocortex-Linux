"""Canonical lazy public facade for the modular NeoCortex framework."""


# region [01] Type-checking API declarations
# Static consumers see the same concrete symbols while runtime imports remain
# deferred until a public attribute is requested through the package facade.

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from neocortex.api.curation_api import (
        CURATION_PLAN_API_SCHEMA as CURATION_PLAN_API_SCHEMA,
        CurationPlanOutput as CurationPlanOutput,
        curation_plan_payload as curation_plan_payload,
    )
    from neocortex.api.curation_lifecycle_api import (
        CURATION_DECISION_API_SCHEMA as CURATION_DECISION_API_SCHEMA,
        CURATION_REVIEW_API_SCHEMA as CURATION_REVIEW_API_SCHEMA,
        curation_decide_payload as curation_decide_payload,
        curation_review_payload as curation_review_payload,
    )
    from neocortex.api.curation_authorization_api import (
        CURATION_AUTHORIZATION_API_SCHEMA as CURATION_AUTHORIZATION_API_SCHEMA,
        curation_authorize_payload as curation_authorize_payload,
    )
    from neocortex.api.curation_application_api import (
        CURATION_APPLY_API_SCHEMA as CURATION_APPLY_API_SCHEMA,
        CURATION_APPLY_SCHEMA as CURATION_APPLY_SCHEMA,
        CURATION_RECONCILE_API_SCHEMA as CURATION_RECONCILE_API_SCHEMA,
        CurationApplyOutput as CurationApplyOutput,
        CurationReconcileOutput as CurationReconcileOutput,
        curation_apply_payload as curation_apply_payload,
        curation_reconcile_payload as curation_reconcile_payload,
    )
    from neocortex.api.curation_recovery_api import (
        CURATION_RECOVERY_STATUS_API_SCHEMA as CURATION_RECOVERY_STATUS_API_SCHEMA,
        CURATION_RESTORE_API_SCHEMA as CURATION_RESTORE_API_SCHEMA,
        CurationRecoveryStatusOutput as CurationRecoveryStatusOutput,
        curation_recovery_status_payload as curation_recovery_status_payload,
        curation_restore_payload as curation_restore_payload,
        curation_restore_preview_payload as curation_restore_preview_payload,
    )
    from neocortex.api.curation_verification_api import (
        CURATION_SCAN_API_SCHEMA as CURATION_SCAN_API_SCHEMA,
        CURATION_VERIFY_API_SCHEMA as CURATION_VERIFY_API_SCHEMA,
        CurationScanOutput as CurationScanOutput,
        CurationVerifyOutput as CurationVerifyOutput,
        curation_scan_payload as curation_scan_payload,
        curation_verify_payload as curation_verify_payload,
    )
    from neocortex.api.curation_checkpoint_api import (
        CURATION_CHECKPOINT_CREATE_API_SCHEMA as CURATION_CHECKPOINT_CREATE_API_SCHEMA,
        CURATION_CHECKPOINT_RESUME_API_SCHEMA as CURATION_CHECKPOINT_RESUME_API_SCHEMA,
        CURATION_CHECKPOINT_STATUS_API_SCHEMA as CURATION_CHECKPOINT_STATUS_API_SCHEMA,
        curation_checkpoint_create_payload as curation_checkpoint_create_payload,
        curation_checkpoint_resume_payload as curation_checkpoint_resume_payload,
        curation_checkpoint_status_payload as curation_checkpoint_status_payload,
    )
    from neocortex.api.read_api import (
        asset_health_payload as asset_health_payload,
        context_payload as context_payload,
        evidence_payload as evidence_payload,
        lineage_payload as lineage_payload,
        knowledge_search_projection_payload as knowledge_search_projection_payload,
        operational_query_payload as operational_query_payload,
        search_payload as search_payload,
        status_payload as status_payload,
    )
    from neocortex.api.content_diagnostics_api import (
        CONTENT_DIAGNOSTICS_SCHEMA as CONTENT_DIAGNOSTICS_SCHEMA,
        CONTENT_DIAGNOSTICS_V2_SCHEMA as CONTENT_DIAGNOSTICS_V2_SCHEMA,
        content_diagnostics_payload as content_diagnostics_payload,
        content_diagnostics_v2_payload as content_diagnostics_v2_payload,
    )
    from neocortex.curation.preview import CurationPlanPage as CurationPlanPage
    from neocortex.curation.preview import CurationSourceHead as CurationSourceHead
    from neocortex.runtime.config.application_config import ApplicationConfig as ApplicationConfig
    from neocortex.capabilities.formats.audio.models import AudioRouteConfig as AudioRouteConfig
    from neocortex.capabilities.formats.audio.models import AudioRouteSummary as AudioRouteSummary
    from neocortex.capabilities.formats.audio.route import AudioRoute as AudioRoute
    from neocortex.platform.content_types import DetectedType as DetectedType
    from neocortex.platform.content_types import detect_content_type as detect_content_type
    from neocortex.capabilities.formats.docx.route import DocxRoute as DocxRoute
    from neocortex.capabilities.formats.docx.route import DocxRouteConfig as DocxRouteConfig
    from neocortex.capabilities.formats.docx.route import DocxRouteSummary as DocxRouteSummary
    from neocortex.capabilities.formats.docx.route import search_docx_state as search_docx_state
    from neocortex.semantic.derivation_contracts import CapabilityFailure as CapabilityFailure
    from neocortex.semantic.derivation_contracts import (
        DERIVATION_CONTRACT_SCHEMA_VERSION as DERIVATION_CONTRACT_SCHEMA_VERSION,
    )
    from neocortex.semantic.derivation_contracts import DerivationRef as DerivationRef
    from neocortex.semantic.derivation_contracts import InputBinding as InputBinding
    from neocortex.semantic.derivation_contracts import MaterializationRef as MaterializationRef
    from neocortex.semantic.derivation_contracts import OutputBinding as OutputBinding
    from neocortex.semantic.derivation_contracts import (
        ReproducibilityClass as ReproducibilityClass,
    )
    from neocortex.semantic.derivation_contracts import StageDescriptor as StageDescriptor
    from neocortex.semantic.derivation_contracts import WorkExecutionMode as WorkExecutionMode
    from neocortex.semantic.derivation_contracts import WorkOutcome as WorkOutcome
    from neocortex.semantic.derivation_contracts import WorkReceipt as WorkReceipt
    from neocortex.runtime.control.global_resources import GlobalResourceCoordinator as GlobalResourceCoordinator
    from neocortex.runtime.control.global_resources import GlobalResourceLimits as GlobalResourceLimits
    from neocortex.runtime.control.global_resources import GlobalResourceSummary as GlobalResourceSummary
    from neocortex.capabilities.formats.image.route import ImageRoute as ImageRoute
    from neocortex.capabilities.formats.image.contracts import ImageRouteConfig as ImageRouteConfig
    from neocortex.capabilities.formats.image.contracts import ImageRouteSummary as ImageRouteSummary
    from neocortex.knowledge.knowledge_contracts import ContextBundle as ContextBundle
    from neocortex.knowledge.knowledge_contracts import ContextContradictionRef as ContextContradictionRef
    from neocortex.knowledge.knowledge_contracts import ContextEntityRef as ContextEntityRef
    from neocortex.knowledge.knowledge_contracts import ContextGraphBudget as ContextGraphBudget
    from neocortex.knowledge.knowledge_contracts import ContextPlanRef as ContextPlanRef
    from neocortex.knowledge.knowledge_contracts import ContextPlanStepRef as ContextPlanStepRef
    from neocortex.knowledge.knowledge_contracts import ContextRelationRef as ContextRelationRef
    from neocortex.knowledge.knowledge_contracts import EvidenceRef as EvidenceRef
    from neocortex.knowledge.knowledge_evidence_projection import (
        EVIDENCE_PROJECTION_SCHEMA as EVIDENCE_PROJECTION_SCHEMA,
        EVIDENCE_PROJECTION_VERSION as EVIDENCE_PROJECTION_VERSION,
        EvidenceProjection as EvidenceProjection,
        KNOWLEDGE_EVIDENCE_PROJECTION_SCHEMA as KNOWLEDGE_EVIDENCE_PROJECTION_SCHEMA,
        KNOWLEDGE_EVIDENCE_PROJECTION_VERSION as KNOWLEDGE_EVIDENCE_PROJECTION_VERSION,
        KnowledgeEvidenceProjection as KnowledgeEvidenceProjection,
        KnowledgeSearchProjection as KnowledgeSearchProjection,
        evidence_projection_payload as evidence_projection_payload,
        evidence_search_projection_payload as evidence_search_projection_payload,
        knowledge_evidence_projection_payload as knowledge_evidence_projection_payload,
        project_knowledge_hit as project_knowledge_hit,
        project_knowledge_search as project_knowledge_search,
    )
    from neocortex.knowledge.knowledge_contracts import KnowledgeHit as KnowledgeHit
    from neocortex.knowledge.knowledge_contracts import KnowledgePhaseTiming as KnowledgePhaseTiming
    from neocortex.knowledge.knowledge_contracts import KnowledgeQueryTelemetry as KnowledgeQueryTelemetry
    from neocortex.knowledge.knowledge_contracts import KnowledgeSnapshot as KnowledgeSnapshot
    from neocortex.knowledge.knowledge_contracts import KnowledgeTelemetryClock as KnowledgeTelemetryClock
    from neocortex.knowledge.knowledge_contracts import (
        KnowledgeTelemetryOperation as KnowledgeTelemetryOperation,
    )
    from neocortex.knowledge.knowledge_contracts import KnowledgeTimingPhase as KnowledgeTimingPhase
    from neocortex.knowledge.knowledge_read_budget import (
        KNOWLEDGE_READ_BUDGET_SCHEMA as KNOWLEDGE_READ_BUDGET_SCHEMA,
        KnowledgeReadBudget as KnowledgeReadBudget,
        KnowledgeReadBudgetExceeded as KnowledgeReadBudgetExceeded,
    )
    from neocortex.knowledge.knowledge_contracts import ResourceRef as ResourceRef
    from neocortex.knowledge.knowledge_contracts import RevisionRef as RevisionRef
    from neocortex.knowledge.knowledge_planner import KnowledgePlan as KnowledgePlan
    from neocortex.knowledge.knowledge_planner import KnowledgeQuery as KnowledgeQuery
    from neocortex.knowledge.knowledge_planner import RetrievalMode as RetrievalMode
    from neocortex.knowledge.knowledge_planner import plan_knowledge_query as plan_knowledge_query
    from neocortex.knowledge.knowledge_search import KnowledgeSearchResult as KnowledgeSearchResult
    from neocortex.knowledge.knowledge_service import KnowledgeSearchService as KnowledgeSearchService
    from neocortex.knowledge.knowledge_snapshot import KnowledgeStateRootError as KnowledgeStateRootError
    from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths as KnowledgeStatePaths
    from neocortex.runtime.models import ActionSummary as ActionSummary
    from neocortex.runtime.models import FrameworkConfig as FrameworkConfig
    from neocortex.runtime.models import InitialRunResult as InitialRunResult
    from neocortex.runtime.models import RouteOnlyRunResult as RouteOnlyRunResult
    from neocortex.runtime.orchestration.run_manifest import (
        RunBudget as RunBudget,
        RunManifest as RunManifest,
    )
    from neocortex.runtime.orchestration.run_status import RunStatus as RunStatus
    from neocortex.api.lifecycle_read_api import (
        LIFECYCLE_ENVELOPE_SCHEMA as LIFECYCLE_ENVELOPE_SCHEMA,
        LIFECYCLE_STATUS_KIND as LIFECYCLE_STATUS_KIND,
        LIFECYCLE_STATUS_OPERATION as LIFECYCLE_STATUS_OPERATION,
        LifecycleStatusContractError as LifecycleStatusContractError,
        RUN_CHECKPOINT_SCHEMA as RUN_CHECKPOINT_SCHEMA,
        lifecycle_status_payload as lifecycle_status_payload,
    )
    from neocortex.api.run_lifecycle import (
        read_run_status as read_run_status,
        read_run_status_json as read_run_status_json,
    )
    from neocortex.capabilities.formats.office.route import OfficeRoute as OfficeRoute
    from neocortex.capabilities.formats.office.route import OfficeRouteConfig as OfficeRouteConfig
    from neocortex.capabilities.formats.office.route import OfficeRouteSummary as OfficeRouteSummary
    from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator as FrameworkOrchestrator
    from neocortex.capabilities.formats.pdf.pdf_admin import PdfDoctorReport as PdfDoctorReport
    from neocortex.capabilities.formats.pdf.pdf_admin import PdfVerifyReport as PdfVerifyReport
    from neocortex.capabilities.formats.pdf.pdf_admin import doctor_pdf_runtime as doctor_pdf_runtime
    from neocortex.capabilities.formats.pdf.pdf_admin import verify_pdf_state as verify_pdf_state
    from neocortex.capabilities.formats.pdf.pdf_derived import PdfDerivedIndexer as PdfDerivedIndexer
    from neocortex.capabilities.formats.pdf.pdf_derived import PdfDerivedSummary as PdfDerivedSummary
    from neocortex.capabilities.formats.pdf.pdf_derived_queries import search_pdf_state as search_pdf_state
    from neocortex.capabilities.formats.pdf.pdf_route import PdfRoute as PdfRoute
    from neocortex.capabilities.formats.pdf.pdf_route import PdfRouteConfig as PdfRouteConfig
    from neocortex.capabilities.formats.pdf.pdf_route import PdfRouteSummary as PdfRouteSummary
    from neocortex.runtime.orchestration.route_registry import RouteAdapter as RouteAdapter
    from neocortex.runtime.orchestration.route_registry import RouteExecutionContext as RouteExecutionContext

# endregion [01]


# region [02] Stable public export manifest

__all__ = [  # noqa: RUF022
    "ActionSummary",
    "ApplicationConfig",
    "AudioRoute",
    "AudioRouteConfig",
    "AudioRouteSummary",
    "CapabilityFailure",
    "CURATION_AUTHORIZATION_API_SCHEMA",
    "CURATION_APPLY_API_SCHEMA",
    "CURATION_APPLY_SCHEMA",
    "CURATION_RECONCILE_API_SCHEMA",
    "CURATION_RECOVERY_STATUS_API_SCHEMA",
    "CURATION_RESTORE_API_SCHEMA",
    "CURATION_DECISION_API_SCHEMA",
    "CURATION_PLAN_API_SCHEMA",
    "CURATION_REVIEW_API_SCHEMA",
    "CURATION_SCAN_API_SCHEMA",
    "CURATION_VERIFY_API_SCHEMA",
    "CURATION_CHECKPOINT_CREATE_API_SCHEMA",
    "CURATION_CHECKPOINT_RESUME_API_SCHEMA",
    "CURATION_CHECKPOINT_STATUS_API_SCHEMA",
    "CONTENT_DIAGNOSTICS_SCHEMA",
    "CONTENT_DIAGNOSTICS_V2_SCHEMA",
    "CurationPlanOutput",
    "CurationApplyOutput",
    "CurationReconcileOutput",
    "CurationRecoveryStatusOutput",
    "CurationPlanPage",
    "CurationScanOutput",
    "CurationSourceHead",
    "CurationVerifyOutput",
    "DERIVATION_CONTRACT_SCHEMA_VERSION",
    "DetectedType",
    "DerivationRef",
    "DocxRoute",
    "DocxRouteConfig",
    "DocxRouteSummary",
    "FrameworkConfig",
    "FrameworkOrchestrator",
    "GlobalResourceCoordinator",
    "GlobalResourceLimits",
    "GlobalResourceSummary",
    "ImageRoute",
    "ImageRouteConfig",
    "ImageRouteSummary",
    "InputBinding",
    "MaterializationRef",
    "OutputBinding",
    "PdfDoctorReport",
    "PdfRoute",
    "PdfRouteConfig",
    "PdfRouteSummary",
    "PdfVerifyReport",
    "RouteAdapter",
    "RouteExecutionContext",
    "ReproducibilityClass",
    "PdfDerivedIndexer",
    "PdfDerivedSummary",
    "search_pdf_state",
    "search_docx_state",
    "doctor_pdf_runtime",
    "InitialRunResult",
    "OfficeRoute",
    "OfficeRouteConfig",
    "OfficeRouteSummary",
    "RouteOnlyRunResult",
    "RunBudget",
    "RunManifest",
    "RunStatus",
    "LIFECYCLE_ENVELOPE_SCHEMA",
    "LIFECYCLE_STATUS_KIND",
    "LIFECYCLE_STATUS_OPERATION",
    "LifecycleStatusContractError",
    "RUN_CHECKPOINT_SCHEMA",
    "lifecycle_status_payload",
    "read_run_status",
    "read_run_status_json",
    "StageDescriptor",
    "detect_content_type",
    "verify_pdf_state",
    "curation_plan_payload",
    "curation_review_payload",
    "curation_decide_payload",
    "curation_authorize_payload",
    "curation_apply_payload",
    "curation_reconcile_payload",
    "curation_recovery_status_payload",
    "curation_restore_payload",
    "curation_restore_preview_payload",
    "curation_scan_payload",
    "curation_verify_payload",
    "curation_checkpoint_create_payload",
    "curation_checkpoint_resume_payload",
    "curation_checkpoint_status_payload",
    "status_payload",
    "search_payload",
    "context_payload",
    "content_diagnostics_payload",
    "content_diagnostics_v2_payload",
    "evidence_payload",
    "operational_query_payload",
    "asset_health_payload",
    "lineage_payload",
    "knowledge_search_projection_payload",
    "ContextBundle",
    "ContextContradictionRef",
    "ContextEntityRef",
    "ContextGraphBudget",
    "ContextPlanRef",
    "ContextPlanStepRef",
    "ContextRelationRef",
    "EvidenceRef",
    "EVIDENCE_PROJECTION_SCHEMA",
    "EVIDENCE_PROJECTION_VERSION",
    "EvidenceProjection",
    "KnowledgeHit",
    "KNOWLEDGE_EVIDENCE_PROJECTION_SCHEMA",
    "KNOWLEDGE_EVIDENCE_PROJECTION_VERSION",
    "KnowledgeEvidenceProjection",
    "KnowledgeSearchProjection",
    "KnowledgePhaseTiming",
    "KnowledgePlan",
    "KnowledgeQuery",
    "KnowledgeQueryTelemetry",
    "KNOWLEDGE_READ_BUDGET_SCHEMA",
    "KnowledgeReadBudget",
    "KnowledgeReadBudgetExceeded",
    "KnowledgeSearchResult",
    "KnowledgeSearchService",
    "KnowledgeStateRootError",
    "KnowledgeSnapshot",
    "KnowledgeStatePaths",
    "KnowledgeTelemetryClock",
    "KnowledgeTelemetryOperation",
    "KnowledgeTimingPhase",
    "evidence_projection_payload",
    "evidence_search_projection_payload",
    "knowledge_evidence_projection_payload",
    "project_knowledge_hit",
    "project_knowledge_search",
    "ResourceRef",
    "RetrievalMode",
    "RevisionRef",
    "WorkExecutionMode",
    "WorkOutcome",
    "WorkReceipt",
    "plan_knowledge_query",
]

_EXPORTS: Final[dict[str, tuple[str, str]]] = {
    "CURATION_AUTHORIZATION_API_SCHEMA": (
        "neocortex.api.curation_authorization_api",
        "CURATION_AUTHORIZATION_API_SCHEMA",
    ),
    "CURATION_APPLY_SCHEMA": (
        "neocortex.api.curation_application_api",
        "CURATION_APPLY_SCHEMA",
    ),
    "CURATION_APPLY_API_SCHEMA": (
        "neocortex.api.curation_application_api",
        "CURATION_APPLY_API_SCHEMA",
    ),
    "CurationApplyOutput": (
        "neocortex.api.curation_application_api",
        "CurationApplyOutput",
    ),
    "CurationReconcileOutput": (
        "neocortex.api.curation_application_api",
        "CurationReconcileOutput",
    ),
    "CURATION_RECOVERY_STATUS_API_SCHEMA": (
        "neocortex.api.curation_recovery_api",
        "CURATION_RECOVERY_STATUS_API_SCHEMA",
    ),
    "CURATION_RESTORE_API_SCHEMA": (
        "neocortex.api.curation_recovery_api",
        "CURATION_RESTORE_API_SCHEMA",
    ),
    "CurationRecoveryStatusOutput": (
        "neocortex.api.curation_recovery_api",
        "CurationRecoveryStatusOutput",
    ),
    "CURATION_RECONCILE_API_SCHEMA": (
        "neocortex.api.curation_application_api",
        "CURATION_RECONCILE_API_SCHEMA",
    ),
    "CURATION_DECISION_API_SCHEMA": (
        "neocortex.api.curation_lifecycle_api",
        "CURATION_DECISION_API_SCHEMA",
    ),
    "CURATION_PLAN_API_SCHEMA": ("neocortex.api.curation_api", "CURATION_PLAN_API_SCHEMA"),
    "CURATION_REVIEW_API_SCHEMA": (
        "neocortex.api.curation_lifecycle_api",
        "CURATION_REVIEW_API_SCHEMA",
    ),
    "CurationPlanOutput": ("neocortex.api.curation_api", "CurationPlanOutput"),
    "CurationPlanPage": ("neocortex.curation.preview", "CurationPlanPage"),
    "CurationScanOutput": (
        "neocortex.api.curation_verification_api",
        "CurationScanOutput",
    ),
    "CurationSourceHead": ("neocortex.curation.preview", "CurationSourceHead"),
    "CurationVerifyOutput": (
        "neocortex.api.curation_verification_api",
        "CurationVerifyOutput",
    ),
    "ActionSummary": ("neocortex.runtime.models", "ActionSummary"),
    "ApplicationConfig": ("neocortex.runtime.config.application_config", "ApplicationConfig"),
    "AudioRoute": ("neocortex.capabilities.formats.audio.route", "AudioRoute"),
    "AudioRouteConfig": ("neocortex.capabilities.formats.audio.models", "AudioRouteConfig"),
    "AudioRouteSummary": ("neocortex.capabilities.formats.audio.models", "AudioRouteSummary"),
    "CapabilityFailure": ("neocortex.semantic.derivation_contracts", "CapabilityFailure"),
    "DERIVATION_CONTRACT_SCHEMA_VERSION": (
        "neocortex.semantic.derivation_contracts",
        "DERIVATION_CONTRACT_SCHEMA_VERSION",
    ),
    "DetectedType": ("neocortex.platform.content_types", "DetectedType"),
    "DerivationRef": ("neocortex.semantic.derivation_contracts", "DerivationRef"),
    "DocxRoute": ("neocortex.capabilities.formats.docx.route", "DocxRoute"),
    "DocxRouteConfig": ("neocortex.capabilities.formats.docx.route", "DocxRouteConfig"),
    "DocxRouteSummary": ("neocortex.capabilities.formats.docx.route", "DocxRouteSummary"),
    "FrameworkConfig": ("neocortex.runtime.models", "FrameworkConfig"),
    "FrameworkOrchestrator": ("neocortex.runtime.orchestration.orchestrator", "FrameworkOrchestrator"),
    "GlobalResourceCoordinator": (
        "neocortex.runtime.control.global_resources",
        "GlobalResourceCoordinator",
    ),
    "GlobalResourceLimits": ("neocortex.runtime.control.global_resources", "GlobalResourceLimits"),
    "GlobalResourceSummary": ("neocortex.runtime.control.global_resources", "GlobalResourceSummary"),
    "ImageRoute": ("neocortex.capabilities.formats.image.route", "ImageRoute"),
    "ImageRouteConfig": ("neocortex.capabilities.formats.image.contracts", "ImageRouteConfig"),
    "ImageRouteSummary": ("neocortex.capabilities.formats.image.contracts", "ImageRouteSummary"),
    "InputBinding": ("neocortex.semantic.derivation_contracts", "InputBinding"),
    "MaterializationRef": ("neocortex.semantic.derivation_contracts", "MaterializationRef"),
    "OutputBinding": ("neocortex.semantic.derivation_contracts", "OutputBinding"),
    "PdfDoctorReport": ("neocortex.capabilities.formats.pdf.pdf_admin", "PdfDoctorReport"),
    "PdfRoute": ("neocortex.capabilities.formats.pdf.pdf_route", "PdfRoute"),
    "PdfRouteConfig": ("neocortex.capabilities.formats.pdf.pdf_route_models", "PdfRouteConfig"),
    "PdfRouteSummary": ("neocortex.capabilities.formats.pdf.pdf_route_models", "PdfRouteSummary"),
    "PdfVerifyReport": ("neocortex.capabilities.formats.pdf.pdf_admin", "PdfVerifyReport"),
    "RouteAdapter": ("neocortex.runtime.orchestration.route_registry", "RouteAdapter"),
    "RouteExecutionContext": ("neocortex.runtime.orchestration.route_registry", "RouteExecutionContext"),
    "ReproducibilityClass": (
        "neocortex.semantic.derivation_contracts",
        "ReproducibilityClass",
    ),
    "PdfDerivedIndexer": ("neocortex.capabilities.formats.pdf.pdf_derived", "PdfDerivedIndexer"),
    "PdfDerivedSummary": ("neocortex.capabilities.formats.pdf.pdf_derived", "PdfDerivedSummary"),
    "search_pdf_state": ("neocortex.capabilities.formats.pdf.pdf_derived_queries", "search_pdf_state"),
    "search_docx_state": ("neocortex.capabilities.formats.docx.route", "search_docx_state"),
    "doctor_pdf_runtime": ("neocortex.capabilities.formats.pdf.pdf_admin", "doctor_pdf_runtime"),
    "InitialRunResult": ("neocortex.runtime.models", "InitialRunResult"),
    "OfficeRoute": ("neocortex.capabilities.formats.office.route", "OfficeRoute"),
    "OfficeRouteConfig": ("neocortex.capabilities.formats.office.route", "OfficeRouteConfig"),
    "OfficeRouteSummary": ("neocortex.capabilities.formats.office.route", "OfficeRouteSummary"),
    "RouteOnlyRunResult": ("neocortex.runtime.models", "RouteOnlyRunResult"),
    "RunBudget": ("neocortex.runtime.orchestration.run_manifest", "RunBudget"),
    "RunManifest": ("neocortex.runtime.orchestration.run_manifest", "RunManifest"),
    "RunStatus": ("neocortex.runtime.orchestration.run_status", "RunStatus"),
    "LIFECYCLE_ENVELOPE_SCHEMA": (
        "neocortex.api.lifecycle_read_api",
        "LIFECYCLE_ENVELOPE_SCHEMA",
    ),
    "LIFECYCLE_STATUS_KIND": (
        "neocortex.api.lifecycle_read_api",
        "LIFECYCLE_STATUS_KIND",
    ),
    "LIFECYCLE_STATUS_OPERATION": (
        "neocortex.api.lifecycle_read_api",
        "LIFECYCLE_STATUS_OPERATION",
    ),
    "LifecycleStatusContractError": (
        "neocortex.api.lifecycle_read_api",
        "LifecycleStatusContractError",
    ),
    "RUN_CHECKPOINT_SCHEMA": (
        "neocortex.api.lifecycle_read_api",
        "RUN_CHECKPOINT_SCHEMA",
    ),
    "lifecycle_status_payload": (
        "neocortex.api.lifecycle_read_api",
        "lifecycle_status_payload",
    ),
    "read_run_status": ("neocortex.api.run_lifecycle", "read_run_status"),
    "read_run_status_json": ("neocortex.api.run_lifecycle", "read_run_status_json"),
    "StageDescriptor": ("neocortex.semantic.derivation_contracts", "StageDescriptor"),
    "detect_content_type": ("neocortex.platform.content_types", "detect_content_type"),
    "verify_pdf_state": ("neocortex.capabilities.formats.pdf.pdf_admin", "verify_pdf_state"),
    "CONTENT_DIAGNOSTICS_SCHEMA": (
        "neocortex.api.content_diagnostics_api",
        "CONTENT_DIAGNOSTICS_SCHEMA",
    ),
    "CONTENT_DIAGNOSTICS_V2_SCHEMA": (
        "neocortex.api.content_diagnostics_api",
        "CONTENT_DIAGNOSTICS_V2_SCHEMA",
    ),
    "content_diagnostics_payload": (
        "neocortex.api.content_diagnostics_api",
        "content_diagnostics_payload",
    ),
    "content_diagnostics_v2_payload": (
        "neocortex.api.content_diagnostics_api",
        "content_diagnostics_v2_payload",
    ),
    "curation_plan_payload": ("neocortex.api.curation_api", "curation_plan_payload"),
    "curation_review_payload": (
        "neocortex.api.curation_lifecycle_api",
        "curation_review_payload",
    ),
    "curation_decide_payload": (
        "neocortex.api.curation_lifecycle_api",
        "curation_decide_payload",
    ),
    "curation_authorize_payload": (
        "neocortex.api.curation_authorization_api",
        "curation_authorize_payload",
    ),
    "curation_apply_payload": (
        "neocortex.api.curation_application_api",
        "curation_apply_payload",
    ),
    "curation_reconcile_payload": (
        "neocortex.api.curation_application_api",
        "curation_reconcile_payload",
    ),
    "curation_recovery_status_payload": (
        "neocortex.api.curation_recovery_api",
        "curation_recovery_status_payload",
    ),
    "curation_restore_payload": (
        "neocortex.api.curation_recovery_api",
        "curation_restore_payload",
    ),
    "curation_restore_preview_payload": (
        "neocortex.api.curation_recovery_api",
        "curation_restore_preview_payload",
    ),
    "CURATION_SCAN_API_SCHEMA": (
        "neocortex.api.curation_verification_api",
        "CURATION_SCAN_API_SCHEMA",
    ),
    "CURATION_VERIFY_API_SCHEMA": (
        "neocortex.api.curation_verification_api",
        "CURATION_VERIFY_API_SCHEMA",
    ),
    "curation_scan_payload": (
        "neocortex.api.curation_verification_api",
        "curation_scan_payload",
    ),
    "curation_verify_payload": (
        "neocortex.api.curation_verification_api",
        "curation_verify_payload",
    ),
    "CURATION_CHECKPOINT_CREATE_API_SCHEMA": (
        "neocortex.api.curation_checkpoint_api",
        "CURATION_CHECKPOINT_CREATE_API_SCHEMA",
    ),
    "CURATION_CHECKPOINT_RESUME_API_SCHEMA": (
        "neocortex.api.curation_checkpoint_api",
        "CURATION_CHECKPOINT_RESUME_API_SCHEMA",
    ),
    "CURATION_CHECKPOINT_STATUS_API_SCHEMA": (
        "neocortex.api.curation_checkpoint_api",
        "CURATION_CHECKPOINT_STATUS_API_SCHEMA",
    ),
    "curation_checkpoint_create_payload": (
        "neocortex.api.curation_checkpoint_api",
        "curation_checkpoint_create_payload",
    ),
    "curation_checkpoint_resume_payload": (
        "neocortex.api.curation_checkpoint_api",
        "curation_checkpoint_resume_payload",
    ),
    "curation_checkpoint_status_payload": (
        "neocortex.api.curation_checkpoint_api",
        "curation_checkpoint_status_payload",
    ),
    "status_payload": ("neocortex.api.read_api", "status_payload"),
    "search_payload": ("neocortex.api.read_api", "search_payload"),
    "context_payload": ("neocortex.api.read_api", "context_payload"),
    "evidence_payload": ("neocortex.api.read_api", "evidence_payload"),
    "operational_query_payload": (
        "neocortex.api.read_api",
        "operational_query_payload",
    ),
    "asset_health_payload": ("neocortex.api.read_api", "asset_health_payload"),
    "lineage_payload": ("neocortex.api.read_api", "lineage_payload"),
    "knowledge_search_projection_payload": (
        "neocortex.api.read_api",
        "knowledge_search_projection_payload",
    ),
    "ContextBundle": ("neocortex.knowledge.knowledge_contracts", "ContextBundle"),
    "ContextContradictionRef": (
        "neocortex.knowledge.knowledge_contracts",
        "ContextContradictionRef",
    ),
    "ContextEntityRef": ("neocortex.knowledge.knowledge_contracts", "ContextEntityRef"),
    "ContextGraphBudget": ("neocortex.knowledge.knowledge_contracts", "ContextGraphBudget"),
    "ContextPlanRef": ("neocortex.knowledge.knowledge_contracts", "ContextPlanRef"),
    "ContextPlanStepRef": ("neocortex.knowledge.knowledge_contracts", "ContextPlanStepRef"),
    "ContextRelationRef": ("neocortex.knowledge.knowledge_contracts", "ContextRelationRef"),
    "EvidenceRef": ("neocortex.knowledge.knowledge_contracts", "EvidenceRef"),
    "EVIDENCE_PROJECTION_SCHEMA": (
        "neocortex.knowledge.knowledge_evidence_projection",
        "EVIDENCE_PROJECTION_SCHEMA",
    ),
    "EVIDENCE_PROJECTION_VERSION": (
        "neocortex.knowledge.knowledge_evidence_projection",
        "EVIDENCE_PROJECTION_VERSION",
    ),
    "EvidenceProjection": (
        "neocortex.knowledge.knowledge_evidence_projection",
        "EvidenceProjection",
    ),
    "KnowledgeHit": ("neocortex.knowledge.knowledge_contracts", "KnowledgeHit"),
    "KNOWLEDGE_EVIDENCE_PROJECTION_SCHEMA": (
        "neocortex.knowledge.knowledge_evidence_projection",
        "KNOWLEDGE_EVIDENCE_PROJECTION_SCHEMA",
    ),
    "KNOWLEDGE_EVIDENCE_PROJECTION_VERSION": (
        "neocortex.knowledge.knowledge_evidence_projection",
        "KNOWLEDGE_EVIDENCE_PROJECTION_VERSION",
    ),
    "KnowledgeEvidenceProjection": (
        "neocortex.knowledge.knowledge_evidence_projection",
        "KnowledgeEvidenceProjection",
    ),
    "KnowledgeSearchProjection": (
        "neocortex.knowledge.knowledge_evidence_projection",
        "KnowledgeSearchProjection",
    ),
    "KnowledgePhaseTiming": ("neocortex.knowledge.knowledge_contracts", "KnowledgePhaseTiming"),
    "KnowledgePlan": ("neocortex.knowledge.knowledge_planner", "KnowledgePlan"),
    "KnowledgeQuery": ("neocortex.knowledge.knowledge_planner", "KnowledgeQuery"),
    "KnowledgeQueryTelemetry": (
        "neocortex.knowledge.knowledge_contracts",
        "KnowledgeQueryTelemetry",
    ),
    "KNOWLEDGE_READ_BUDGET_SCHEMA": (
        "neocortex.knowledge.knowledge_read_budget",
        "KNOWLEDGE_READ_BUDGET_SCHEMA",
    ),
    "KnowledgeReadBudget": (
        "neocortex.knowledge.knowledge_read_budget",
        "KnowledgeReadBudget",
    ),
    "KnowledgeReadBudgetExceeded": (
        "neocortex.knowledge.knowledge_read_budget",
        "KnowledgeReadBudgetExceeded",
    ),
    "KnowledgeSearchResult": ("neocortex.knowledge.knowledge_search", "KnowledgeSearchResult"),
    "KnowledgeSearchService": ("neocortex.knowledge.knowledge_service", "KnowledgeSearchService"),
    "KnowledgeStateRootError": ("neocortex.knowledge.knowledge_snapshot", "KnowledgeStateRootError"),
    "KnowledgeSnapshot": ("neocortex.knowledge.knowledge_contracts", "KnowledgeSnapshot"),
    "KnowledgeStatePaths": ("neocortex.knowledge.knowledge_snapshot", "KnowledgeStatePaths"),
    "KnowledgeTelemetryClock": (
        "neocortex.knowledge.knowledge_contracts",
        "KnowledgeTelemetryClock",
    ),
    "KnowledgeTelemetryOperation": (
        "neocortex.knowledge.knowledge_contracts",
        "KnowledgeTelemetryOperation",
    ),
    "KnowledgeTimingPhase": ("neocortex.knowledge.knowledge_contracts", "KnowledgeTimingPhase"),
    "evidence_projection_payload": (
        "neocortex.knowledge.knowledge_evidence_projection",
        "evidence_projection_payload",
    ),
    "evidence_search_projection_payload": (
        "neocortex.knowledge.knowledge_evidence_projection",
        "evidence_search_projection_payload",
    ),
    "knowledge_evidence_projection_payload": (
        "neocortex.knowledge.knowledge_evidence_projection",
        "knowledge_evidence_projection_payload",
    ),
    "project_knowledge_hit": (
        "neocortex.knowledge.knowledge_evidence_projection",
        "project_knowledge_hit",
    ),
    "project_knowledge_search": (
        "neocortex.knowledge.knowledge_evidence_projection",
        "project_knowledge_search",
    ),
    "ResourceRef": ("neocortex.knowledge.knowledge_contracts", "ResourceRef"),
    "RetrievalMode": ("neocortex.knowledge.knowledge_planner", "RetrievalMode"),
    "RevisionRef": ("neocortex.knowledge.knowledge_contracts", "RevisionRef"),
    "WorkExecutionMode": ("neocortex.semantic.derivation_contracts", "WorkExecutionMode"),
    "WorkOutcome": ("neocortex.semantic.derivation_contracts", "WorkOutcome"),
    "WorkReceipt": ("neocortex.semantic.derivation_contracts", "WorkReceipt"),
    "plan_knowledge_query": ("neocortex.knowledge.knowledge_planner", "plan_knowledge_query"),
}

# endregion [02]


# region [03] PEP 562 lazy resolution


def __getattr__(name: str) -> Any:
    """Resolve and cache one declared public symbol on first access."""

    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose the stable public surface to interactive introspection."""

    return sorted(set(globals()) | set(__all__))


# endregion [03]
