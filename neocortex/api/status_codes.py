"""Shared status codes for canonical read-only API surfaces."""

from __future__ import annotations

from enum import IntEnum
from typing import TYPE_CHECKING

from neocortex.knowledge.knowledge_contracts import (
    KnowledgeCompleteness, OwnerAvailability, SnapshotConsistency,
)

if TYPE_CHECKING:
    from neocortex.knowledge.knowledge_contracts import ContextBundle, KnowledgeSnapshot
    from neocortex.knowledge.knowledge_search import KnowledgeSearchResult


class KnowledgeExitCode(IntEnum):
    """Stable process codes shared by Knowledge and review adapters."""

    SUCCESS = 0
    FATAL = 1
    USAGE = 2
    NO_RESULTS = 3
    PARTIAL = 4
    SNAPSHOT_CHANGED = 5
    SCHEMA_INCOMPATIBLE = 6
    CORRUPT = 7
    CANCELLED = 130


def _blocking_snapshot_exit_code(
    snapshot: KnowledgeSnapshot,
    blocking_owners: tuple[str, ...],
) -> KnowledgeExitCode:
    required = set(blocking_owners)
    states = {owner.state for owner in snapshot.owners if owner.owner in required}
    if OwnerAvailability.CORRUPT in states:
        return KnowledgeExitCode.CORRUPT
    if states.intersection({OwnerAvailability.FUTURE, OwnerAvailability.INCOMPATIBLE}):
        return KnowledgeExitCode.SCHEMA_INCOMPATIBLE
    if snapshot.consistency is SnapshotConsistency.SNAPSHOT_CHANGED:
        return KnowledgeExitCode.SNAPSHOT_CHANGED
    return KnowledgeExitCode.SUCCESS

def knowledge_search_exit_code(result: KnowledgeSearchResult) -> KnowledgeExitCode:
    snapshot_code = _blocking_snapshot_exit_code(
        result.snapshot,
        result.blocking_owners,
    )
    if snapshot_code is not KnowledgeExitCode.SUCCESS:
        return snapshot_code
    if not result.complete:
        return KnowledgeExitCode.PARTIAL
    if not result.hits:
        return KnowledgeExitCode.NO_RESULTS
    return KnowledgeExitCode.SUCCESS

def knowledge_context_exit_code(bundle: ContextBundle) -> KnowledgeExitCode:
    snapshot_code = _blocking_snapshot_exit_code(
        bundle.snapshot,
        bundle.blocking_owners,
    )
    if snapshot_code is not KnowledgeExitCode.SUCCESS:
        return snapshot_code
    if bundle.completeness in {
        KnowledgeCompleteness.PARTIAL,
        KnowledgeCompleteness.UNSUPPORTED,
    }:
        return KnowledgeExitCode.PARTIAL
    if bundle.completeness is KnowledgeCompleteness.NO_EVIDENCE:
        return KnowledgeExitCode.NO_RESULTS
    return KnowledgeExitCode.SUCCESS


__all__ = ["KnowledgeExitCode", "knowledge_context_exit_code", "knowledge_search_exit_code"]
