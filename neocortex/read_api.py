"""Bounded read-only facade over personal and framework Knowledge state.

The operational owners remain the source of truth.  This module only selects
the two canonical state roots, invokes their published readers independently,
and labels every result with its scope.  It never accepts an arbitrary state
path and never fuses scores from separate snapshots.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from _04_Nucleo_Operativo.read_api_port import (
    CodeSearchQuery,
    KnowledgeCompleteness,
    KnowledgeExitCode,
    KnowledgeQuery,
    KnowledgeSearchService,
    KnowledgeSnapshot,
    KnowledgeStatePaths,
    OwnerAvailability,
    RetrievalMode,
    SnapshotConsistency,
    available_search_modes,
    default_state_directory,
    inspect_derivation_lineage,
    knowledge_context_exit_code,
    knowledge_search_exit_code,
    search_code,
    self_analysis_data_directory,
)


READ_API_SCHEMA = "neocortex.read-api/v1"
FEDERATION_POLICY = "independent_scopes_no_cross_scope_score_fusion"
MAX_HUMAN_QUERY_CHARS = 4_096
MAX_HUMAN_RESULTS_PER_SCOPE = 100

CancellationCheck = Callable[[], None]


class ReadScope(StrEnum):
    """Fixed, non-user-selectable state namespaces exposed by NeoCortex."""

    PERSONAL = "personal"
    FRAMEWORK = "framework"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class ScopeBinding:
    scope: ReadScope
    state_directory: Path


def _scope(value: str | ReadScope) -> ReadScope:
    try:
        return value if isinstance(value, ReadScope) else ReadScope(value)
    except ValueError as exc:
        raise ValueError("scope must be personal, framework or all") from exc


def scope_bindings(value: str | ReadScope) -> tuple[ScopeBinding, ...]:
    """Resolve one public scope without accepting an arbitrary filesystem path."""

    selected = _scope(value)
    available = (
        ScopeBinding(ReadScope.PERSONAL, default_state_directory()),
        ScopeBinding(ReadScope.FRAMEWORK, self_analysis_data_directory()),
    )
    if selected is ReadScope.ALL:
        return available
    return tuple(binding for binding in available if binding.scope is selected)


def _validate_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query cannot be blank")
    if len(query) > MAX_HUMAN_QUERY_CHARS:
        raise ValueError(f"query cannot exceed {MAX_HUMAN_QUERY_CHARS} characters")
    return query.strip()


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= MAX_HUMAN_RESULTS_PER_SCOPE:
        raise ValueError(f"limit must be between 1 and {MAX_HUMAN_RESULTS_PER_SCOPE} per scope")
    return limit


def _validate_characters(max_characters: int) -> int:
    if isinstance(max_characters, bool) or not isinstance(max_characters, int):
        raise ValueError("max_characters must be an integer")
    if not 1 <= max_characters <= 1_000_000:
        raise ValueError("max_characters must be between 1 and 1000000 per scope")
    return max_characters


def _service(binding: ScopeBinding) -> KnowledgeSearchService:
    return KnowledgeSearchService(KnowledgeStatePaths.from_directory(binding.state_directory))


def _error_entry(binding: ScopeBinding, exc: BaseException) -> dict[str, object]:
    return {
        "scope": binding.scope.value,
        "state_directory": str(binding.state_directory),
        "status": "error",
        "exit_code": int(KnowledgeExitCode.FATAL),
        "error_type": type(exc).__name__,
        "reason": str(exc),
    }


def _snapshot_exit_code(snapshot: KnowledgeSnapshot) -> KnowledgeExitCode:
    states = {owner.state for owner in snapshot.owners}
    if OwnerAvailability.CORRUPT in states:
        return KnowledgeExitCode.CORRUPT
    if states.intersection({OwnerAvailability.FUTURE, OwnerAvailability.INCOMPATIBLE}):
        return KnowledgeExitCode.SCHEMA_INCOMPATIBLE
    if snapshot.consistency is SnapshotConsistency.SNAPSHOT_CHANGED:
        return KnowledgeExitCode.SNAPSHOT_CHANGED
    return KnowledgeExitCode.SUCCESS


def _snapshot_status(snapshot: KnowledgeSnapshot) -> str:
    states = {owner.state for owner in snapshot.owners}
    if (
        OwnerAvailability.CORRUPT in states
        or states.intersection({OwnerAvailability.FUTURE, OwnerAvailability.INCOMPATIBLE})
        or snapshot.consistency is SnapshotConsistency.SNAPSHOT_CHANGED
    ):
        return "attention_required"
    if OwnerAvailability.AVAILABLE not in states:
        return "empty"
    return "ready"


def federated_exit_code(entries: Sequence[dict[str, object]]) -> int:
    """Combine independent scope exit codes without combining their rankings."""

    codes = [
        value
        if isinstance(value := entry.get("exit_code"), int) and not isinstance(value, bool)
        else int(KnowledgeExitCode.FATAL)
        for entry in entries
    ]
    if not codes:
        return int(KnowledgeExitCode.FATAL)
    if all(code == int(KnowledgeExitCode.SUCCESS) for code in codes):
        return int(KnowledgeExitCode.SUCCESS)
    if all(code == int(KnowledgeExitCode.NO_RESULTS) for code in codes):
        return int(KnowledgeExitCode.NO_RESULTS)
    if any(code == int(KnowledgeExitCode.SUCCESS) for code in codes) or (
        any(code == int(KnowledgeExitCode.NO_RESULTS) for code in codes) and len(set(codes)) > 1
    ):
        return int(KnowledgeExitCode.PARTIAL)
    priority = (
        KnowledgeExitCode.CORRUPT,
        KnowledgeExitCode.SCHEMA_INCOMPATIBLE,
        KnowledgeExitCode.SNAPSHOT_CHANGED,
        KnowledgeExitCode.FATAL,
        KnowledgeExitCode.PARTIAL,
        KnowledgeExitCode.NO_RESULTS,
    )
    for code in priority:
        if int(code) in codes:
            return int(code)
    return int(KnowledgeExitCode.FATAL)


def status_payload(
    scope: str | ReadScope = ReadScope.ALL,
    *,
    cancellation_check: CancellationCheck | None = None,
) -> dict[str, object]:
    """Return independent published-state snapshots for fixed local scopes."""

    selected = _scope(scope)
    entries: list[dict[str, object]] = []
    for binding in scope_bindings(selected):
        try:
            snapshot = _service(binding).status(cancellation_check=cancellation_check)
            code = _snapshot_exit_code(snapshot)
            entries.append(
                {
                    "scope": binding.scope.value,
                    "state_directory": str(binding.state_directory),
                    "status": _snapshot_status(snapshot),
                    "exit_code": int(code),
                    "snapshot": snapshot.to_dict(),
                }
            )
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
            entries.append(_error_entry(binding, exc))
    return {
        "schema": READ_API_SCHEMA,
        "kind": "neocortex_scoped_status",
        "read_only": True,
        "scope_requested": selected.value,
        "federation_policy": FEDERATION_POLICY,
        "exit_code": federated_exit_code(entries),
        "scopes": entries,
    }


def search_payload(
    query: str,
    scope: str | ReadScope = ReadScope.ALL,
    *,
    limit: int = 10,
    mode: str | RetrievalMode = RetrievalMode.EVIDENCE,
    include_history: bool = False,
    cancellation_check: CancellationCheck | None = None,
) -> dict[str, object]:
    """Search fixed scopes independently and preserve each ranking contract."""

    normalized = _validate_query(query)
    selected = _scope(scope)
    bounded_limit = _validate_limit(limit)
    retrieval_mode = mode if isinstance(mode, RetrievalMode) else RetrievalMode(mode)
    request = KnowledgeQuery(
        normalized,
        retrieval_mode=retrieval_mode,
        include_history=include_history,
        limit=bounded_limit,
    )
    entries: list[dict[str, object]] = []
    for binding in scope_bindings(selected):
        try:
            result = _service(binding).search(
                request,
                cancellation_check=cancellation_check,
            )
            entries.append(
                {
                    "scope": binding.scope.value,
                    "state_directory": str(binding.state_directory),
                    "status": "ok" if result.complete else "partial",
                    "exit_code": int(knowledge_search_exit_code(result)),
                    "result": result.to_dict(),
                }
            )
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
            entries.append(_error_entry(binding, exc))
    return {
        "schema": READ_API_SCHEMA,
        "kind": "neocortex_scoped_search",
        "read_only": True,
        "scope_requested": selected.value,
        "federation_policy": FEDERATION_POLICY,
        "query": normalized,
        "mode": retrieval_mode.value,
        "include_history": include_history,
        "limit_per_scope": bounded_limit,
        "exit_code": federated_exit_code(entries),
        "scopes": entries,
    }


def context_payload(
    query: str,
    scope: str | ReadScope = ReadScope.ALL,
    *,
    limit: int = 8,
    max_characters: int = 12_000,
    mode: str | RetrievalMode = RetrievalMode.EVIDENCE,
    include_history: bool = False,
    cancellation_check: CancellationCheck | None = None,
) -> dict[str, object]:
    """Build citation-first contexts independently for each fixed scope."""

    normalized = _validate_query(query)
    selected = _scope(scope)
    bounded_limit = _validate_limit(limit)
    bounded_characters = _validate_characters(max_characters)
    retrieval_mode = mode if isinstance(mode, RetrievalMode) else RetrievalMode(mode)
    request = KnowledgeQuery(
        normalized,
        retrieval_mode=retrieval_mode,
        include_history=include_history,
        limit=bounded_limit,
    )
    entries: list[dict[str, object]] = []
    for binding in scope_bindings(selected):
        try:
            bundle = _service(binding).context(
                request,
                max_characters=bounded_characters,
                max_hits=bounded_limit,
                cancellation_check=cancellation_check,
            )
            entries.append(
                {
                    "scope": binding.scope.value,
                    "state_directory": str(binding.state_directory),
                    "status": (
                        "ok"
                        if bundle.completeness is KnowledgeCompleteness.COMPLETE
                        else bundle.completeness.value
                    ),
                    "exit_code": int(knowledge_context_exit_code(bundle)),
                    "context": bundle.to_dict(),
                }
            )
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
            entries.append(_error_entry(binding, exc))
    return {
        "schema": READ_API_SCHEMA,
        "kind": "neocortex_scoped_context",
        "read_only": True,
        "scope_requested": selected.value,
        "federation_policy": FEDERATION_POLICY,
        "query": normalized,
        "mode": retrieval_mode.value,
        "include_history": include_history,
        "limit_per_scope": bounded_limit,
        "max_characters_per_scope": bounded_characters,
        "exit_code": federated_exit_code(entries),
        "scopes": entries,
    }


def evidence_payload(
    query: str,
    citation_id: str,
    scope: str | ReadScope = ReadScope.ALL,
    *,
    limit: int = 8,
    max_characters: int = 12_000,
) -> dict[str, object]:
    """Resolve one citation from a fresh stable context without arbitrary file reads."""

    if not isinstance(citation_id, str) or not citation_id.strip():
        raise ValueError("citation_id cannot be blank")
    context = context_payload(
        query,
        scope,
        limit=limit,
        max_characters=max_characters,
    )
    matches: list[dict[str, object]] = []
    scope_entries = context.get("scopes")
    if not isinstance(scope_entries, list):
        scope_entries = []
    for entry in scope_entries:
        if not isinstance(entry, dict):
            continue
        bundle = entry.get("context")
        if not isinstance(bundle, dict):
            continue
        citations = bundle.get("citation_ids", [])
        selected_hits = bundle.get("selected_hits", [])
        if not isinstance(citations, list) or not isinstance(selected_hits, list):
            continue
        for citation, hit in zip(citations, selected_hits, strict=False):
            if not isinstance(citation, dict) or not isinstance(hit, dict):
                continue
            if citation.get("citation_id") != citation_id:
                continue
            matches.append(
                {
                    "scope": entry.get("scope"),
                    "snapshot": bundle.get("snapshot"),
                    "citation": citation,
                    "hit": hit,
                }
            )
    return {
        "schema": READ_API_SCHEMA,
        "kind": "neocortex_evidence",
        "read_only": True,
        "scope_requested": _scope(scope).value,
        "query": _validate_query(query),
        "citation_id": citation_id,
        "found": bool(matches),
        "matches": matches,
        "context_exit_code": context["exit_code"],
        "exit_code": (
            int(KnowledgeExitCode.SUCCESS) if matches else int(KnowledgeExitCode.NO_RESULTS)
        ),
    }


def code_search_payload(
    query: str,
    scope: str | ReadScope = ReadScope.FRAMEWORK,
    *,
    limit: int = 10,
    modes: Sequence[str] = ("hybrid",),
) -> dict[str, object]:
    """Inspect published Code state under fixed roots without touching sources."""

    normalized = _validate_query(query)
    selected = _scope(scope)
    bounded_limit = _validate_limit(limit)
    normalized_modes = tuple(dict.fromkeys(modes))
    allowed_modes = frozenset(available_search_modes())
    if not normalized_modes or any(mode not in allowed_modes for mode in normalized_modes):
        raise ValueError("code search modes contain an unsupported value")
    entries: list[dict[str, object]] = []
    for binding in scope_bindings(selected):
        try:
            hits = search_code(
                binding.state_directory / "code.sqlite3",
                CodeSearchQuery(
                    text=normalized,
                    modes=normalized_modes,
                    limit=bounded_limit,
                ),
            )
            entries.append(
                {
                    "scope": binding.scope.value,
                    "state_directory": str(binding.state_directory),
                    "status": "ok" if hits else "no_results",
                    "exit_code": int(
                        KnowledgeExitCode.SUCCESS if hits else KnowledgeExitCode.NO_RESULTS
                    ),
                    "hits": [asdict(hit) for hit in hits],
                }
            )
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
            entries.append(_error_entry(binding, exc))
    return {
        "schema": READ_API_SCHEMA,
        "kind": "neocortex_scoped_code_search",
        "read_only": True,
        "scope_requested": selected.value,
        "federation_policy": FEDERATION_POLICY,
        "query": normalized,
        "modes": list(normalized_modes),
        "limit_per_scope": bounded_limit,
        "exit_code": federated_exit_code(entries),
        "scopes": entries,
    }


def lineage_payload(
    identifier: str,
    scope: str | ReadScope = ReadScope.ALL,
) -> dict[str, object]:
    """Inspect owner-local derivation lineage under fixed trusted state roots."""

    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("lineage identifier cannot be blank")
    if len(identifier) > MAX_HUMAN_QUERY_CHARS:
        raise ValueError(f"lineage identifier cannot exceed {MAX_HUMAN_QUERY_CHARS} characters")
    normalized = identifier.strip()
    selected = _scope(scope)
    entries: list[dict[str, object]] = []
    for binding in scope_bindings(selected):
        try:
            result = inspect_derivation_lineage(binding.state_directory, normalized)
            entries.append(
                {
                    "scope": binding.scope.value,
                    "state_directory": str(binding.state_directory),
                    "status": result["status"],
                    "exit_code": result["exit_code"],
                    "lineage": result,
                }
            )
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
            entries.append(_error_entry(binding, exc))
    return {
        "schema": READ_API_SCHEMA,
        "kind": "neocortex_scoped_derivation_lineage",
        "read_only": True,
        "scope_requested": selected.value,
        "federation_policy": FEDERATION_POLICY,
        "identifier": normalized,
        "exit_code": federated_exit_code(entries),
        "scopes": entries,
    }


__all__ = (
    "FEDERATION_POLICY",
    "MAX_HUMAN_QUERY_CHARS",
    "MAX_HUMAN_RESULTS_PER_SCOPE",
    "READ_API_SCHEMA",
    "ReadScope",
    "ScopeBinding",
    "code_search_payload",
    "context_payload",
    "evidence_payload",
    "federated_exit_code",
    "lineage_payload",
    "scope_bindings",
    "search_payload",
    "status_payload",
)
