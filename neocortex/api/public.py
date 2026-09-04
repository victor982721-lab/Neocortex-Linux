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
    from neocortex.api.curation_verification_api import (
        CURATION_SCAN_API_SCHEMA as CURATION_SCAN_API_SCHEMA,
        CURATION_VERIFY_API_SCHEMA as CURATION_VERIFY_API_SCHEMA,
        CurationScanOutput as CurationScanOutput,
        CurationVerifyOutput as CurationVerifyOutput,
        curation_scan_payload as curation_scan_payload,
        curation_verify_payload as curation_verify_payload,
    )
    from neocortex.curation.preview import CurationPlanPage as CurationPlanPage
    from neocortex.curation.preview import CurationSourceHead as CurationSourceHead
    from neocortex.runtime.config.application_config import ApplicationConfig as ApplicationConfig
    from neocortex.capabilities.formats.audio.models import AudioRouteConfig as AudioRouteConfig
    from neocortex.capabilities.formats.audio.models import AudioRouteSummary as AudioRouteSummary
    from neocortex.capabilities.formats.audio.route import AudioRoute as AudioRoute
    from neocortex.code.code_contracts import CodeRelationEndpoint as CodeRelationEndpoint
    from neocortex.code.code_contracts import CodeRouteConfig as CodeRouteConfig
    from neocortex.code.code_contracts import CodeRouteSummary as CodeRouteSummary
    from neocortex.code.code_contracts import CodeSearchHit as CodeSearchHit
    from neocortex.code.code_contracts import CodeSearchQuery as CodeSearchQuery
    from neocortex.code.code_contracts import CodeSearchRelation as CodeSearchRelation
    from neocortex.code.ingestion.code_projects import list_projects as list_projects
    from neocortex.code.ingestion.code_projects import reconstruct_project as reconstruct_project
    from neocortex.code.code_route import CodeRoute as CodeRoute
    from neocortex.code.search.code_search import search_code as search_code
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
    from neocortex.capabilities.formats.image.route import ImageRouteConfig as ImageRouteConfig
    from neocortex.capabilities.formats.image.route import ImageRouteSummary as ImageRouteSummary
    from neocortex.knowledge.knowledge_contracts import ContextBundle as ContextBundle
    from neocortex.knowledge.knowledge_contracts import ContextContradictionRef as ContextContradictionRef
    from neocortex.knowledge.knowledge_contracts import ContextEntityRef as ContextEntityRef
    from neocortex.knowledge.knowledge_contracts import ContextGraphBudget as ContextGraphBudget
    from neocortex.knowledge.knowledge_contracts import ContextPlanRef as ContextPlanRef
    from neocortex.knowledge.knowledge_contracts import ContextPlanStepRef as ContextPlanStepRef
    from neocortex.knowledge.knowledge_contracts import ContextRelationRef as ContextRelationRef
    from neocortex.knowledge.knowledge_contracts import EvidenceRef as EvidenceRef
    from neocortex.knowledge.knowledge_contracts import KnowledgeHit as KnowledgeHit
    from neocortex.knowledge.knowledge_contracts import KnowledgePhaseTiming as KnowledgePhaseTiming
    from neocortex.knowledge.knowledge_contracts import KnowledgeQueryTelemetry as KnowledgeQueryTelemetry
    from neocortex.knowledge.knowledge_contracts import KnowledgeSnapshot as KnowledgeSnapshot
    from neocortex.knowledge.knowledge_contracts import KnowledgeTelemetryClock as KnowledgeTelemetryClock
    from neocortex.knowledge.knowledge_contracts import (
        KnowledgeTelemetryOperation as KnowledgeTelemetryOperation,
    )
    from neocortex.knowledge.knowledge_contracts import KnowledgeTimingPhase as KnowledgeTimingPhase
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
    from neocortex.capabilities.formats.pdf.pdf_derived import search_pdf_state as search_pdf_state
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
    "CURATION_DECISION_API_SCHEMA",
    "CURATION_PLAN_API_SCHEMA",
    "CURATION_REVIEW_API_SCHEMA",
    "CURATION_SCAN_API_SCHEMA",
    "CURATION_VERIFY_API_SCHEMA",
    "CurationPlanOutput",
    "CurationPlanPage",
    "CurationScanOutput",
    "CurationSourceHead",
    "CurationVerifyOutput",
    "CodeRelationEndpoint",
    "CodeRoute",
    "CodeRouteConfig",
    "CodeRouteSummary",
    "CodeSearchHit",
    "CodeSearchQuery",
    "CodeSearchRelation",
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
    "StageDescriptor",
    "detect_content_type",
    "verify_pdf_state",
    "list_projects",
    "reconstruct_project",
    "search_code",
    "curation_plan_payload",
    "curation_review_payload",
    "curation_decide_payload",
    "curation_authorize_payload",
    "curation_scan_payload",
    "curation_verify_payload",
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
    "KnowledgePlan",
    "KnowledgeQuery",
    "KnowledgeQueryTelemetry",
    "KnowledgeSearchResult",
    "KnowledgeSearchService",
    "KnowledgeStateRootError",
    "KnowledgeSnapshot",
    "KnowledgeStatePaths",
    "KnowledgeTelemetryClock",
    "KnowledgeTelemetryOperation",
    "KnowledgeTimingPhase",
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
    "CodeRelationEndpoint": ("neocortex.code.code_contracts", "CodeRelationEndpoint"),
    "CodeRoute": ("neocortex.code.code_route", "CodeRoute"),
    "CodeRouteConfig": ("neocortex.code.code_contracts", "CodeRouteConfig"),
    "CodeRouteSummary": ("neocortex.code.code_contracts", "CodeRouteSummary"),
    "CodeSearchHit": ("neocortex.code.code_contracts", "CodeSearchHit"),
    "CodeSearchQuery": ("neocortex.code.code_contracts", "CodeSearchQuery"),
    "CodeSearchRelation": ("neocortex.code.code_contracts", "CodeSearchRelation"),
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
    "ImageRouteConfig": ("neocortex.capabilities.formats.image.route", "ImageRouteConfig"),
    "ImageRouteSummary": ("neocortex.capabilities.formats.image.route", "ImageRouteSummary"),
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
    "search_pdf_state": ("neocortex.capabilities.formats.pdf.pdf_derived", "search_pdf_state"),
    "search_docx_state": ("neocortex.capabilities.formats.docx.route", "search_docx_state"),
    "doctor_pdf_runtime": ("neocortex.capabilities.formats.pdf.pdf_admin", "doctor_pdf_runtime"),
    "InitialRunResult": ("neocortex.runtime.models", "InitialRunResult"),
    "OfficeRoute": ("neocortex.capabilities.formats.office.route", "OfficeRoute"),
    "OfficeRouteConfig": ("neocortex.capabilities.formats.office.route", "OfficeRouteConfig"),
    "OfficeRouteSummary": ("neocortex.capabilities.formats.office.route", "OfficeRouteSummary"),
    "RouteOnlyRunResult": ("neocortex.runtime.models", "RouteOnlyRunResult"),
    "StageDescriptor": ("neocortex.semantic.derivation_contracts", "StageDescriptor"),
    "detect_content_type": ("neocortex.platform.content_types", "detect_content_type"),
    "verify_pdf_state": ("neocortex.capabilities.formats.pdf.pdf_admin", "verify_pdf_state"),
    "list_projects": ("neocortex.code.ingestion.code_projects", "list_projects"),
    "reconstruct_project": ("neocortex.code.ingestion.code_projects", "reconstruct_project"),
    "search_code": ("neocortex.code.search.code_search", "search_code"),
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
    "KnowledgeHit": ("neocortex.knowledge.knowledge_contracts", "KnowledgeHit"),
    "KnowledgePhaseTiming": ("neocortex.knowledge.knowledge_contracts", "KnowledgePhaseTiming"),
    "KnowledgePlan": ("neocortex.knowledge.knowledge_planner", "KnowledgePlan"),
    "KnowledgeQuery": ("neocortex.knowledge.knowledge_planner", "KnowledgeQuery"),
    "KnowledgeQueryTelemetry": (
        "neocortex.knowledge.knowledge_contracts",
        "KnowledgeQueryTelemetry",
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
