"""Declared core port for NeoCortex's bounded read-only public facade.

Only the fixed-scope public adapter should consume this module.  Keeping the
cross-package contract here prevents ``neocortex.read_api`` from depending on
the internal layout of the Knowledge, Code and path owners.
"""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

from pathlib import Path
from typing import TYPE_CHECKING

from neocortex.runtime.config.app_paths import default_state_directory, self_analysis_data_directory
from neocortex.api.cli.cli_knowledge import (
    KnowledgeExitCode,
    knowledge_context_exit_code,
    knowledge_search_exit_code,
)
from neocortex.code.code_contracts import CodeSearchQuery
from neocortex.code.code_search import available_search_modes, search_code
from neocortex.knowledge.knowledge_contracts import (
    KnowledgeCompleteness,
    KnowledgeSnapshot,
    OwnerAvailability,
    SnapshotConsistency,
)
from neocortex.knowledge.knowledge_planner import KnowledgeQuery, RetrievalMode
from neocortex.knowledge.knowledge_service import KnowledgeSearchService
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths

if TYPE_CHECKING:
    from neocortex.code.code_question_resolver import CodeQuestionResolution
    from neocortex.knowledge.knowledge_asset_health_contracts import KnowledgeAssetHealthReport


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

    return inspect(
        KnowledgeStatePaths.from_directory(state_directory),
        KnowledgeAssetHealthQuery(resource_id),
    )


def resolve_code_question(
    state_directory: Path,
    question_id: str,
    *,
    limit: int = 10,
) -> CodeQuestionResolution:
    """Load the focal Code reader only for an exact bounded question."""

    from neocortex.code.code_question_resolver import resolve_code_question as resolve

    return resolve(state_directory, question_id, limit=limit)


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
    "resolve_code_question",
    "search_code",
    "self_analysis_data_directory",
    "validate_knowledge_asset_resource_id",
)


_preserve_legacy_module(globals(), '_04_Nucleo_Operativo.read_api_port')
