"""Declared core port for NeoCortex's bounded read-only public facade.

Only the fixed-scope public adapter should consume this module.  Keeping the
cross-package contract here prevents ``neocortex.read_api`` from depending on
the internal layout of the Knowledge, Code and path owners.
"""

from __future__ import annotations

from .app_paths import default_state_directory, self_analysis_data_directory
from .cli_knowledge import (
    KnowledgeExitCode,
    knowledge_context_exit_code,
    knowledge_search_exit_code,
)
from .code_contracts import CodeSearchQuery
from .code_search import available_search_modes, search_code
from .knowledge_contracts import (
    KnowledgeCompleteness,
    KnowledgeSnapshot,
    OwnerAvailability,
    SnapshotConsistency,
)
from .knowledge_planner import KnowledgeQuery, RetrievalMode
from .knowledge_service import KnowledgeSearchService
from .knowledge_snapshot import KnowledgeStatePaths


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
    "knowledge_context_exit_code",
    "knowledge_search_exit_code",
    "search_code",
    "self_analysis_data_directory",
)
