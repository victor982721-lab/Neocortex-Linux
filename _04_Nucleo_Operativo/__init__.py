"""Lazy public facade for the modular NeoCortex operational framework."""


# region [01] Type-checking API declarations
# Static consumers see the same concrete symbols while runtime imports remain
# deferred until a public attribute is requested through the package facade.

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .application_config import ApplicationConfig as ApplicationConfig
    from .audio_models import AudioRouteConfig as AudioRouteConfig
    from .audio_models import AudioRouteSummary as AudioRouteSummary
    from .audio_route import AudioRoute as AudioRoute
    from .code_contracts import CodeRelationEndpoint as CodeRelationEndpoint
    from .code_contracts import CodeRouteConfig as CodeRouteConfig
    from .code_contracts import CodeRouteSummary as CodeRouteSummary
    from .code_contracts import CodeSearchHit as CodeSearchHit
    from .code_contracts import CodeSearchQuery as CodeSearchQuery
    from .code_contracts import CodeSearchRelation as CodeSearchRelation
    from .code_projects import list_projects as list_projects
    from .code_projects import reconstruct_project as reconstruct_project
    from .code_route import CodeRoute as CodeRoute
    from .code_search import search_code as search_code
    from .platform.shared.content_types import DetectedType as DetectedType
    from .platform.shared.content_types import detect_content_type as detect_content_type
    from .capabilities.formats.docx.route import DocxRoute as DocxRoute
    from .capabilities.formats.docx.route import DocxRouteConfig as DocxRouteConfig
    from .capabilities.formats.docx.route import DocxRouteSummary as DocxRouteSummary
    from .capabilities.formats.docx.route import search_docx_state as search_docx_state
    from .derivation_contracts import CapabilityFailure as CapabilityFailure
    from .derivation_contracts import (
        DERIVATION_CONTRACT_SCHEMA_VERSION as DERIVATION_CONTRACT_SCHEMA_VERSION,
    )
    from .derivation_contracts import DerivationRef as DerivationRef
    from .derivation_contracts import InputBinding as InputBinding
    from .derivation_contracts import MaterializationRef as MaterializationRef
    from .derivation_contracts import OutputBinding as OutputBinding
    from .derivation_contracts import (
        ReproducibilityClass as ReproducibilityClass,
    )
    from .derivation_contracts import StageDescriptor as StageDescriptor
    from .derivation_contracts import WorkExecutionMode as WorkExecutionMode
    from .derivation_contracts import WorkOutcome as WorkOutcome
    from .derivation_contracts import WorkReceipt as WorkReceipt
    from .global_resources import GlobalResourceCoordinator as GlobalResourceCoordinator
    from .global_resources import GlobalResourceLimits as GlobalResourceLimits
    from .global_resources import GlobalResourceSummary as GlobalResourceSummary
    from .capabilities.formats.image.route import ImageRoute as ImageRoute
    from .capabilities.formats.image.route import ImageRouteConfig as ImageRouteConfig
    from .capabilities.formats.image.route import ImageRouteSummary as ImageRouteSummary
    from .knowledge_contracts import ContextBundle as ContextBundle
    from .knowledge_contracts import ContextContradictionRef as ContextContradictionRef
    from .knowledge_contracts import ContextEntityRef as ContextEntityRef
    from .knowledge_contracts import ContextGraphBudget as ContextGraphBudget
    from .knowledge_contracts import ContextPlanRef as ContextPlanRef
    from .knowledge_contracts import ContextPlanStepRef as ContextPlanStepRef
    from .knowledge_contracts import ContextRelationRef as ContextRelationRef
    from .knowledge_contracts import EvidenceRef as EvidenceRef
    from .knowledge_contracts import KnowledgeHit as KnowledgeHit
    from .knowledge_contracts import KnowledgePhaseTiming as KnowledgePhaseTiming
    from .knowledge_contracts import KnowledgeQueryTelemetry as KnowledgeQueryTelemetry
    from .knowledge_contracts import KnowledgeSnapshot as KnowledgeSnapshot
    from .knowledge_contracts import KnowledgeTelemetryClock as KnowledgeTelemetryClock
    from .knowledge_contracts import (
        KnowledgeTelemetryOperation as KnowledgeTelemetryOperation,
    )
    from .knowledge_contracts import KnowledgeTimingPhase as KnowledgeTimingPhase
    from .knowledge_contracts import ResourceRef as ResourceRef
    from .knowledge_contracts import RevisionRef as RevisionRef
    from .knowledge_planner import KnowledgePlan as KnowledgePlan
    from .knowledge_planner import KnowledgeQuery as KnowledgeQuery
    from .knowledge_planner import RetrievalMode as RetrievalMode
    from .knowledge_planner import plan_knowledge_query as plan_knowledge_query
    from .knowledge_search import KnowledgeSearchResult as KnowledgeSearchResult
    from .knowledge_service import KnowledgeSearchService as KnowledgeSearchService
    from .knowledge_snapshot import KnowledgeStateRootError as KnowledgeStateRootError
    from .knowledge_snapshot import KnowledgeStatePaths as KnowledgeStatePaths
    from .models import ActionSummary as ActionSummary
    from .models import FrameworkConfig as FrameworkConfig
    from .models import InitialRunResult as InitialRunResult
    from .models import RouteOnlyRunResult as RouteOnlyRunResult
    from .models import SelfAnalysisRunResult as SelfAnalysisRunResult
    from .office_route import OfficeRoute as OfficeRoute
    from .office_route import OfficeRouteConfig as OfficeRouteConfig
    from .office_route import OfficeRouteSummary as OfficeRouteSummary
    from .orchestrator import FrameworkOrchestrator as FrameworkOrchestrator
    from .pdf_admin import PdfDoctorReport as PdfDoctorReport
    from .pdf_admin import PdfVerifyReport as PdfVerifyReport
    from .pdf_admin import doctor_pdf_runtime as doctor_pdf_runtime
    from .pdf_admin import verify_pdf_state as verify_pdf_state
    from .pdf_derived import PdfDerivedIndexer as PdfDerivedIndexer
    from .pdf_derived import PdfDerivedSummary as PdfDerivedSummary
    from .pdf_derived import search_pdf_state as search_pdf_state
    from .pdf_route import PdfRoute as PdfRoute
    from .pdf_route import PdfRouteConfig as PdfRouteConfig
    from .pdf_route import PdfRouteSummary as PdfRouteSummary
    from .route_registry import RouteAdapter as RouteAdapter
    from .route_registry import RouteExecutionContext as RouteExecutionContext

# endregion [01]


# region [02] Stable public export manifest

__all__ = [  # noqa: RUF022
    "ActionSummary",
    "ApplicationConfig",
    "AudioRoute",
    "AudioRouteConfig",
    "AudioRouteSummary",
    "CapabilityFailure",
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
    "SelfAnalysisRunResult",
    "StageDescriptor",
    "detect_content_type",
    "verify_pdf_state",
    "list_projects",
    "reconstruct_project",
    "search_code",
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
    "ActionSummary": (".models", "ActionSummary"),
    "ApplicationConfig": (".application_config", "ApplicationConfig"),
    "AudioRoute": (".audio_route", "AudioRoute"),
    "AudioRouteConfig": (".audio_models", "AudioRouteConfig"),
    "AudioRouteSummary": (".audio_models", "AudioRouteSummary"),
    "CapabilityFailure": (".derivation_contracts", "CapabilityFailure"),
    "CodeRelationEndpoint": (".code_contracts", "CodeRelationEndpoint"),
    "CodeRoute": (".code_route", "CodeRoute"),
    "CodeRouteConfig": (".code_contracts", "CodeRouteConfig"),
    "CodeRouteSummary": (".code_contracts", "CodeRouteSummary"),
    "CodeSearchHit": (".code_contracts", "CodeSearchHit"),
    "CodeSearchQuery": (".code_contracts", "CodeSearchQuery"),
    "CodeSearchRelation": (".code_contracts", "CodeSearchRelation"),
    "DERIVATION_CONTRACT_SCHEMA_VERSION": (
        ".derivation_contracts",
        "DERIVATION_CONTRACT_SCHEMA_VERSION",
    ),
    "DetectedType": (".platform.shared.content_types", "DetectedType"),
    "DerivationRef": (".derivation_contracts", "DerivationRef"),
    "DocxRoute": (".capabilities.formats.docx.route", "DocxRoute"),
    "DocxRouteConfig": (".capabilities.formats.docx.route", "DocxRouteConfig"),
    "DocxRouteSummary": (".capabilities.formats.docx.route", "DocxRouteSummary"),
    "FrameworkConfig": (".models", "FrameworkConfig"),
    "FrameworkOrchestrator": (".orchestrator", "FrameworkOrchestrator"),
    "GlobalResourceCoordinator": (
        ".global_resources",
        "GlobalResourceCoordinator",
    ),
    "GlobalResourceLimits": (".global_resources", "GlobalResourceLimits"),
    "GlobalResourceSummary": (".global_resources", "GlobalResourceSummary"),
    "ImageRoute": (".capabilities.formats.image.route", "ImageRoute"),
    "ImageRouteConfig": (".capabilities.formats.image.route", "ImageRouteConfig"),
    "ImageRouteSummary": (".capabilities.formats.image.route", "ImageRouteSummary"),
    "InputBinding": (".derivation_contracts", "InputBinding"),
    "MaterializationRef": (".derivation_contracts", "MaterializationRef"),
    "OutputBinding": (".derivation_contracts", "OutputBinding"),
    "PdfDoctorReport": (".pdf_admin", "PdfDoctorReport"),
    "PdfRoute": (".pdf_route", "PdfRoute"),
    "PdfRouteConfig": (".pdf_route", "PdfRouteConfig"),
    "PdfRouteSummary": (".pdf_route", "PdfRouteSummary"),
    "PdfVerifyReport": (".pdf_admin", "PdfVerifyReport"),
    "RouteAdapter": (".route_registry", "RouteAdapter"),
    "RouteExecutionContext": (".route_registry", "RouteExecutionContext"),
    "ReproducibilityClass": (
        ".derivation_contracts",
        "ReproducibilityClass",
    ),
    "PdfDerivedIndexer": (".pdf_derived", "PdfDerivedIndexer"),
    "PdfDerivedSummary": (".pdf_derived", "PdfDerivedSummary"),
    "search_pdf_state": (".pdf_derived", "search_pdf_state"),
    "search_docx_state": (".capabilities.formats.docx.route", "search_docx_state"),
    "doctor_pdf_runtime": (".pdf_admin", "doctor_pdf_runtime"),
    "InitialRunResult": (".models", "InitialRunResult"),
    "OfficeRoute": (".office_route", "OfficeRoute"),
    "OfficeRouteConfig": (".office_route", "OfficeRouteConfig"),
    "OfficeRouteSummary": (".office_route", "OfficeRouteSummary"),
    "RouteOnlyRunResult": (".models", "RouteOnlyRunResult"),
    "SelfAnalysisRunResult": (".models", "SelfAnalysisRunResult"),
    "StageDescriptor": (".derivation_contracts", "StageDescriptor"),
    "detect_content_type": (".platform.shared.content_types", "detect_content_type"),
    "verify_pdf_state": (".pdf_admin", "verify_pdf_state"),
    "list_projects": (".code_projects", "list_projects"),
    "reconstruct_project": (".code_projects", "reconstruct_project"),
    "search_code": (".code_search", "search_code"),
    "ContextBundle": (".knowledge_contracts", "ContextBundle"),
    "ContextContradictionRef": (
        ".knowledge_contracts",
        "ContextContradictionRef",
    ),
    "ContextEntityRef": (".knowledge_contracts", "ContextEntityRef"),
    "ContextGraphBudget": (".knowledge_contracts", "ContextGraphBudget"),
    "ContextPlanRef": (".knowledge_contracts", "ContextPlanRef"),
    "ContextPlanStepRef": (".knowledge_contracts", "ContextPlanStepRef"),
    "ContextRelationRef": (".knowledge_contracts", "ContextRelationRef"),
    "EvidenceRef": (".knowledge_contracts", "EvidenceRef"),
    "KnowledgeHit": (".knowledge_contracts", "KnowledgeHit"),
    "KnowledgePhaseTiming": (".knowledge_contracts", "KnowledgePhaseTiming"),
    "KnowledgePlan": (".knowledge_planner", "KnowledgePlan"),
    "KnowledgeQuery": (".knowledge_planner", "KnowledgeQuery"),
    "KnowledgeQueryTelemetry": (
        ".knowledge_contracts",
        "KnowledgeQueryTelemetry",
    ),
    "KnowledgeSearchResult": (".knowledge_search", "KnowledgeSearchResult"),
    "KnowledgeSearchService": (".knowledge_service", "KnowledgeSearchService"),
    "KnowledgeStateRootError": (".knowledge_snapshot", "KnowledgeStateRootError"),
    "KnowledgeSnapshot": (".knowledge_contracts", "KnowledgeSnapshot"),
    "KnowledgeStatePaths": (".knowledge_snapshot", "KnowledgeStatePaths"),
    "KnowledgeTelemetryClock": (
        ".knowledge_contracts",
        "KnowledgeTelemetryClock",
    ),
    "KnowledgeTelemetryOperation": (
        ".knowledge_contracts",
        "KnowledgeTelemetryOperation",
    ),
    "KnowledgeTimingPhase": (".knowledge_contracts", "KnowledgeTimingPhase"),
    "ResourceRef": (".knowledge_contracts", "ResourceRef"),
    "RetrievalMode": (".knowledge_planner", "RetrievalMode"),
    "RevisionRef": (".knowledge_contracts", "RevisionRef"),
    "WorkExecutionMode": (".derivation_contracts", "WorkExecutionMode"),
    "WorkOutcome": (".derivation_contracts", "WorkOutcome"),
    "WorkReceipt": (".derivation_contracts", "WorkReceipt"),
    "plan_knowledge_query": (".knowledge_planner", "plan_knowledge_query"),
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
