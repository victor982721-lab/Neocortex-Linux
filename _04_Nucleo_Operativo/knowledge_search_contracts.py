"""Public Knowledge Search contracts with legacy facade identities."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/knowledge_search_contracts.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import math
from dataclasses import dataclass, field
from types import FunctionType
from typing import TYPE_CHECKING

from .knowledge_contracts import (
    KnowledgeQueryTelemetry,
    KnowledgeTelemetryOperation,
    RankingSignal,
    ResourceRef,
    RevisionRef,
)
from .semantic_models import canonical_json
# endregion [01]

# region [02] Implementación

if TYPE_CHECKING:
    from .knowledge_contracts import (
        EvidenceRef,
        KnowledgeHit,
        KnowledgeSnapshot,
    )
    from .knowledge_planner import KnowledgePlan


_LEGACY_MODULE = "_04_Nucleo_Operativo.knowledge_search"


@dataclass(frozen=True, slots=True)
class KnowledgeCandidate:
    resource: ResourceRef
    revision: RevisionRef
    evidence: EvidenceRef
    signal: RankingSignal
    reason: str
    confidence: float | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("candidate retrieval reason cannot be blank")
        if self.revision.resource_id != self.resource.resource_id:
            raise ValueError("candidate revision does not belong to its resource")
        if (
            self.evidence.resource_id != self.resource.resource_id
            or self.evidence.revision_id != self.revision.revision_id
        ):
            raise ValueError("candidate evidence does not belong to its revision")
        if self.confidence is not None and (
            not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0
        ):
            raise ValueError("candidate confidence must be between 0 and 1")

    @property
    def evidence_key(self) -> tuple[str, str, str]:
        return (
            self.resource.resource_id,
            self.revision.revision_id,
            self.evidence.evidence_id,
        )


@dataclass(frozen=True, slots=True)
class ResourceDiscoverySignal:
    """Advisory resource-level signal that cannot stand in for evidence."""

    resource: ResourceRef
    revision: RevisionRef
    signal: RankingSignal
    reason: str
    fusion_weight: float
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("resource discovery reason cannot be blank")
        if self.revision.resource_id != self.resource.resource_id:
            raise ValueError("resource discovery revision does not belong to resource")
        if not math.isfinite(self.fusion_weight) or not 0.0 < self.fusion_weight <= 1.0:
            raise ValueError("resource discovery fusion_weight must be in (0, 1]")

    @property
    def resource_revision_key(self) -> tuple[str, str]:
        return self.resource.resource_id, self.revision.revision_id


@dataclass(frozen=True, slots=True)
class RankingExecution:
    name: str
    channel: str
    executed: bool
    available: bool
    complete: bool
    returned: int
    rows_scanned: int = 0
    vectors_scanned: int = 0
    reason: str | None = None
    owner: str | None = None
    elapsed_ns: int | None = None
    result_window_full: bool = False
    next_cursor: int | None = None
    cutoff_score: float | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.channel.strip():
            raise ValueError("ranking execution name and channel cannot be blank")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.returned, self.rows_scanned, self.vectors_scanned)
        ):
            raise ValueError("ranking execution counters cannot be negative")
        if self.owner is not None and not self.owner.strip():
            raise ValueError("ranking execution owner cannot be blank")
        if self.elapsed_ns is not None and (
            isinstance(self.elapsed_ns, bool)
            or not isinstance(self.elapsed_ns, int)
            or self.elapsed_ns < 0
        ):
            raise ValueError("ranking execution elapsed_ns cannot be negative")
        if not isinstance(self.result_window_full, bool):
            raise ValueError("ranking result_window_full must be a boolean")
        if self.next_cursor is not None and (
            isinstance(self.next_cursor, bool)
            or not isinstance(self.next_cursor, int)
            or self.next_cursor < 0
        ):
            raise ValueError("ranking next_cursor cannot be negative")
        if self.cutoff_score is not None and (
            isinstance(self.cutoff_score, bool)
            or not isinstance(self.cutoff_score, (int, float))
            or not math.isfinite(self.cutoff_score)
        ):
            raise ValueError("ranking cutoff_score must be finite")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "channel": self.channel,
            "executed": self.executed,
            "available": self.available,
            "complete": self.complete,
            "returned": self.returned,
            "rows_scanned": self.rows_scanned,
            "row_count_semantics": "materialized_lower_bound",
            "vectors_scanned": self.vectors_scanned,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.owner is not None:
            payload["owner"] = self.owner
        if self.elapsed_ns is not None:
            payload["elapsed_ns"] = self.elapsed_ns
        if self.result_window_full:
            payload["result_window_full"] = True
        if self.next_cursor is not None:
            payload["next_cursor"] = self.next_cursor
        if self.cutoff_score is not None:
            payload["cutoff_score"] = self.cutoff_score
        return payload


@dataclass(frozen=True, slots=True)
class KnowledgeSearchResult:
    plan: KnowledgePlan
    snapshot: KnowledgeSnapshot
    hits: tuple[KnowledgeHit, ...]
    rankings: tuple[RankingExecution, ...]
    complete: bool
    truncated: bool
    omitted_candidates: int
    rows_scanned: int
    vectors_scanned: int
    elapsed_milliseconds: int
    warnings: tuple[str, ...] = ()
    telemetry: KnowledgeQueryTelemetry | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    blocking_owners: tuple[str, ...] = ()
    result_window_full: bool = False
    window_omitted_candidates: int = 0

    def __post_init__(self) -> None:
        if self.telemetry is not None and (
            not isinstance(self.telemetry, KnowledgeQueryTelemetry)
            or self.telemetry.operation is not KnowledgeTelemetryOperation.SEARCH
        ):
            raise ValueError("KnowledgeSearchResult telemetry must describe search")
        if not isinstance(self.blocking_owners, tuple):
            raise ValueError("search blocking owners must be a tuple")
        if any(not isinstance(owner, str) or not owner.strip() for owner in self.blocking_owners):
            raise ValueError("search blocking owners cannot be blank")
        if self.blocking_owners != tuple(sorted(set(self.blocking_owners))):
            raise ValueError("search blocking owners must be unique and ordered")
        if not isinstance(self.result_window_full, bool):
            raise ValueError("search result_window_full must be a boolean")
        if (
            isinstance(self.window_omitted_candidates, bool)
            or not isinstance(self.window_omitted_candidates, int)
            or self.window_omitted_candidates < 0
        ):
            raise ValueError("search window omission count cannot be negative")
        if self.window_omitted_candidates > self.omitted_candidates:
            raise ValueError("search window omissions cannot exceed total omitted candidates")
        if self.window_omitted_candidates and not self.result_window_full:
            raise ValueError("search window omissions require a full result window")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "kind": "knowledge_search_result",
            "query": self.plan.normalized_query,
            "plan": self.plan.to_dict(),
            "snapshot": self.snapshot.to_dict(),
            "hits": [hit.to_dict() for hit in self.hits],
            "rankings": [ranking.to_dict() for ranking in self.rankings],
            "complete": self.complete,
            "truncated": self.truncated,
            "omitted_candidates": self.omitted_candidates,
            "rows_scanned": self.rows_scanned,
            "row_count_semantics": "sum_of_materialized_lower_bounds",
            "vectors_scanned": self.vectors_scanned,
            "elapsed_milliseconds": self.elapsed_milliseconds,
        }
        if self.result_window_full:
            payload["result_window_full"] = True
            payload["window_omitted_candidates"] = self.window_omitted_candidates
        if self.warnings:
            payload["warnings"] = list(self.warnings)
        if self.telemetry is not None:
            payload["telemetry"] = self.telemetry.to_dict()
        if self.blocking_owners:
            payload["blocking_owners"] = list(self.blocking_owners)
        return payload

    def to_json(self) -> str:
        return canonical_json(self.to_dict())


def _restore_function_module(value: object) -> None:
    if isinstance(value, FunctionType) and value.__module__ == __name__:
        value.__module__ = _LEGACY_MODULE


def _restore_legacy_module(contract: type[object]) -> None:
    contract.__module__ = _LEGACY_MODULE
    for member in vars(contract).values():
        if isinstance(member, property):
            for accessor in (member.fget, member.fset, member.fdel):
                _restore_function_module(accessor)
            continue
        if isinstance(member, (classmethod, staticmethod)):
            _restore_function_module(member.__func__)
            continue
        _restore_function_module(member)


for _contract in (
    KnowledgeCandidate,
    ResourceDiscoverySignal,
    RankingExecution,
    KnowledgeSearchResult,
):
    _restore_legacy_module(_contract)

del _contract


__all__ = (
    "KnowledgeCandidate",
    "KnowledgeSearchResult",
    "RankingExecution",
    "ResourceDiscoverySignal",
)
# endregion [02]
