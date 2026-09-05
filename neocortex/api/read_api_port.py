"""Declared core port for NeoCortex's bounded read-only public facade.

Only the fixed-scope public adapter should consume this module.  Keeping the
cross-package contract here prevents ``neocortex.api.read_api`` from depending on
the internal layout of the Knowledge, Code and path owners.
"""

from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING, Any

from neocortex.runtime.config.app_paths import default_state_directory
from neocortex.api.status_codes import KnowledgeExitCode
from neocortex.api.cli.cli_knowledge import (
    knowledge_context_exit_code,
    knowledge_search_exit_code,
)
from neocortex.code.code_contracts import CodeSearchQuery
from neocortex.code.search.code_search import available_search_modes, search_code
from neocortex.knowledge.knowledge_contracts import (
    KnowledgeCompleteness,
    KnowledgeSnapshot,
    OwnerAvailability,
    SnapshotConsistency,
)
from neocortex.knowledge.knowledge_planner import KnowledgeQuery, RetrievalMode

if TYPE_CHECKING:
    from neocortex.knowledge.knowledge_asset_health_contracts import KnowledgeAssetHealthReport
    from neocortex.knowledge.knowledge_service import KnowledgeSearchService
    from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths


def __getattr__(name: str) -> Any:
    """Preserve the port exports without loading owner schemas during help."""

    if name == "KnowledgeSearchService":
        from neocortex.knowledge.knowledge_service import KnowledgeSearchService

        return KnowledgeSearchService
    if name == "KnowledgeStatePaths":
        from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths

        return KnowledgeStatePaths
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def inspect_derivation_lineage(
    state_directory: Path,
    identifier: str,
) -> dict[str, object]:
    """Load the lineage reader only when that explicit surface is invoked."""

    from neocortex.semantic.derivation_lineage_service import inspect_derivation_lineage as inspect

    return inspect(state_directory, identifier)


def validate_knowledge_asset_resource_id(resource_id: str) -> str:
    """Validate one stable asset identifier through the owner contract."""

    from neocortex.knowledge.knowledge_asset_health_contracts import KnowledgeAssetHealthQuery

    return KnowledgeAssetHealthQuery(resource_id).resource_id


def inspect_knowledge_asset_health(
    state_directory: Path,
    resource_id: str,
) -> KnowledgeAssetHealthReport:
    """Load the Health owner only for an explicit fixed-scope inspection."""

    from neocortex.knowledge.knowledge_asset_health import inspect_knowledge_asset_health as inspect
    from neocortex.knowledge.knowledge_asset_health_contracts import KnowledgeAssetHealthQuery
    from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths

    return inspect(
        KnowledgeStatePaths.from_directory(state_directory),
        KnowledgeAssetHealthQuery(resource_id),
    )



__all__ = (
    "CodeSearchQuery",
    "KnowledgeCompleteness",
    "KnowledgeExitCode",
    "KnowledgeQuery",
    "KnowledgeSearchService",
    "KnowledgeSnapshot",
    "KnowledgeStatePaths",
    "OwnerAvailability",
    "RetrievalMode",
    "SnapshotConsistency",
    "available_search_modes",
    "default_state_directory",
    "inspect_derivation_lineage",
    "inspect_knowledge_asset_health",
    "knowledge_context_exit_code",
    "knowledge_search_exit_code",
    "search_code",
    "validate_knowledge_asset_resource_id",
)
