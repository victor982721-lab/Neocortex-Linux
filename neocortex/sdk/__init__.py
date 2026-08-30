"""Stable, read-only Python facade for the canonical Knowledge Plane.

Symbols are resolved lazily and cached here without wrapping or subclassing
them, so legacy and canonical imports retain object identity.
The supported operations are the existing ``KnowledgeSearchService`` methods
``status()``, ``search()`` and ``context()``; this module deliberately adds no
future Knowledge endpoints.
"""


# region [01] Static public contract

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
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
    )

__all__ = (
    "DERIVATION_CONTRACT_SCHEMA_VERSION",
    "CapabilityFailure",
    "ContextBundle",
    "ContextContradictionRef",
    "ContextEntityRef",
    "ContextGraphBudget",
    "ContextPlanRef",
    "ContextPlanStepRef",
    "ContextRelationRef",
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
    "plan_knowledge_query",
)

_PUBLIC_NAMES: Final = frozenset(__all__)
_PUBLIC_FACADE: Final = "neocortex.api.public"

# endregion [01]


# region [02] Identity-preserving lazy resolution


def __getattr__(name: str) -> Any:
    """Resolve one SDK symbol through the canonical public facade."""

    if name not in _PUBLIC_NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    public = import_module(_PUBLIC_FACADE)
    value = getattr(public, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose the stable facade without resolving any implementation module."""

    return sorted(set(globals()) | _PUBLIC_NAMES)


# endregion [02]
