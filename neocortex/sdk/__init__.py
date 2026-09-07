"""Stable Python facade for Knowledge and curation lifecycle reads/decisions.

Symbols are resolved lazily and cached here without wrapping or subclassing
them, so callers receive the canonical contract objects directly.
Knowledge retains its existing ``status()``, ``search()`` and ``context()``
service.  Curation exposes a fixed-root, paginated plan read, a human-gated
review journey and a digest-bound authorization grant; grants are durable but
do not apply filesystem effects.
"""


# region [01] Static public contract

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from neocortex.api.curation_api import CURATION_PLAN_API_SCHEMA as CURATION_PLAN_API_SCHEMA
    from neocortex.api.curation_api import CurationPlanOutput as CurationPlanOutput
    from neocortex.api.curation_api import curation_plan_payload as curation_plan_payload
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
    from neocortex.api.public import (
        CapabilityFailure as CapabilityFailure,
        ContextBundle as ContextBundle,
        ContextContradictionRef as ContextContradictionRef,
        ContextEntityRef as ContextEntityRef,
        ContextGraphBudget as ContextGraphBudget,
        ContextPlanRef as ContextPlanRef,
        ContextPlanStepRef as ContextPlanStepRef,
        ContextRelationRef as ContextRelationRef,
        DERIVATION_CONTRACT_SCHEMA_VERSION as DERIVATION_CONTRACT_SCHEMA_VERSION,
        DerivationRef as DerivationRef,
        EvidenceRef as EvidenceRef,
        InputBinding as InputBinding,
        KnowledgeHit as KnowledgeHit,
        KnowledgePhaseTiming as KnowledgePhaseTiming,
        KnowledgePlan as KnowledgePlan,
        KnowledgeQuery as KnowledgeQuery,
        KnowledgeQueryTelemetry as KnowledgeQueryTelemetry,
        KnowledgeSearchResult as KnowledgeSearchResult,
        KnowledgeSearchService as KnowledgeSearchService,
        KnowledgeSnapshot as KnowledgeSnapshot,
        KnowledgeStatePaths as KnowledgeStatePaths,
        KnowledgeTelemetryClock as KnowledgeTelemetryClock,
        KnowledgeStateRootError as KnowledgeStateRootError,
        KnowledgeTelemetryOperation as KnowledgeTelemetryOperation,
        KnowledgeTimingPhase as KnowledgeTimingPhase,
        MaterializationRef as MaterializationRef,
        OutputBinding as OutputBinding,
        ReproducibilityClass as ReproducibilityClass,
        ResourceRef as ResourceRef,
        RetrievalMode as RetrievalMode,
        RevisionRef as RevisionRef,
        StageDescriptor as StageDescriptor,
        WorkExecutionMode as WorkExecutionMode,
        WorkOutcome as WorkOutcome,
        WorkReceipt as WorkReceipt,
        plan_knowledge_query as plan_knowledge_query,
        RunManifest as RunManifest,
        RunStatus as RunStatus,
        read_run_status as read_run_status,
        read_run_status_json as read_run_status_json,
        asset_health_payload as asset_health_payload,
        code_search_payload as code_search_payload,
        context_payload as context_payload,
        evidence_payload as evidence_payload,
        lineage_payload as lineage_payload,
        operational_query_payload as operational_query_payload,
        search_payload as search_payload,
        status_payload as status_payload,
    )
    from neocortex.curation.preview import CurationPlanPage as CurationPlanPage
    from neocortex.curation.preview import CurationSourceHead as CurationSourceHead

__all__ = (  # noqa: RUF022
    "CURATION_APPLY_API_SCHEMA",
    "CURATION_APPLY_SCHEMA",
    "CURATION_AUTHORIZATION_API_SCHEMA",
    "CURATION_DECISION_API_SCHEMA",
    "CURATION_PLAN_API_SCHEMA",
    "CURATION_RECONCILE_API_SCHEMA",
    "CURATION_RECOVERY_STATUS_API_SCHEMA",
    "CURATION_RESTORE_API_SCHEMA",
    "CURATION_REVIEW_API_SCHEMA",
    "CURATION_SCAN_API_SCHEMA",
    "CURATION_VERIFY_API_SCHEMA",
    "CURATION_CHECKPOINT_CREATE_API_SCHEMA",
    "CURATION_CHECKPOINT_RESUME_API_SCHEMA",
    "CURATION_CHECKPOINT_STATUS_API_SCHEMA",
    "DERIVATION_CONTRACT_SCHEMA_VERSION",
    "CapabilityFailure",
    "ContextBundle",
    "ContextContradictionRef",
    "ContextEntityRef",
    "ContextGraphBudget",
    "ContextPlanRef",
    "ContextPlanStepRef",
    "ContextRelationRef",
    "CurationApplyOutput",
    "CurationPlanOutput",
    "CurationPlanPage",
    "CurationReconcileOutput",
    "CurationRecoveryStatusOutput",
    "CurationScanOutput",
    "CurationSourceHead",
    "CurationVerifyOutput",
    "DerivationRef",
    "EvidenceRef",
    "InputBinding",
    "KnowledgeHit",
    "KnowledgePhaseTiming",
    "KnowledgePlan",
    "KnowledgeQuery",
    "KnowledgeQueryTelemetry",
    "KnowledgeSearchResult",
    "KnowledgeSearchService",
    "KnowledgeSnapshot",
    "KnowledgeStatePaths",
    "KnowledgeStateRootError",
    "KnowledgeTelemetryClock",
    "KnowledgeTelemetryOperation",
    "KnowledgeTimingPhase",
    "MaterializationRef",
    "OutputBinding",
    "ReproducibilityClass",
    "ResourceRef",
    "RetrievalMode",
    "RevisionRef",
    "StageDescriptor",
    "WorkExecutionMode",
    "WorkOutcome",
    "WorkReceipt",
    "curation_apply_payload",
    "curation_authorize_payload",
    "curation_decide_payload",
    "curation_plan_payload",
    "curation_reconcile_payload",
    "curation_recovery_status_payload",
    "curation_restore_payload",
    "curation_restore_preview_payload",
    "curation_review_payload",
    "curation_scan_payload",
    "curation_verify_payload",
    "curation_checkpoint_create_payload",
    "curation_checkpoint_resume_payload",
    "curation_checkpoint_status_payload",
    "status_payload",
    "search_payload",
    "context_payload",
    "evidence_payload",
    "operational_query_payload",
    "asset_health_payload",
    "code_search_payload",
    "lineage_payload",
    "plan_knowledge_query",
    "RunManifest",
    "RunStatus",
    "read_run_status",
    "read_run_status_json",
)

_PUBLIC_NAMES: Final = frozenset(__all__)
_PUBLIC_FACADE: Final = "neocortex.api.public"
_CURATION_EXPORTS: Final[dict[str, tuple[str, str]]] = {
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
}

_READ_EXPORTS: Final[dict[str, tuple[str, str]]] = {
    "status_payload": ("neocortex.api.read_api", "status_payload"),
    "search_payload": ("neocortex.api.read_api", "search_payload"),
    "context_payload": ("neocortex.api.read_api", "context_payload"),
    "evidence_payload": ("neocortex.api.read_api", "evidence_payload"),
    "operational_query_payload": (
        "neocortex.api.read_api",
        "operational_query_payload",
    ),
    "asset_health_payload": ("neocortex.api.read_api", "asset_health_payload"),
    "code_search_payload": ("neocortex.api.read_api", "code_search_payload"),
    "lineage_payload": ("neocortex.api.read_api", "lineage_payload"),
}

# endregion [01]


# region [02] Identity-preserving lazy resolution


def __getattr__(name: str) -> Any:
    """Resolve one SDK symbol through the canonical public facade."""

    if name not in _PUBLIC_NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    target = _CURATION_EXPORTS.get(name) or _READ_EXPORTS.get(name)
    if target is None:
        target = (_PUBLIC_FACADE, name)
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose the stable facade without resolving any implementation module."""

    return sorted(set(globals()) | _PUBLIC_NAMES)


# endregion [02]
