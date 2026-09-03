"""Bounded read-only facade over published local Knowledge state.

The operational owners remain the source of truth. This module selects the
canonical local state root, invokes its published readers independently, and
keeps the historical ``framework`` name as a compatibility label where needed.
It never accepts an arbitrary state path and never fuses scores from separate
snapshots.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from neocortex.api.read_contract import (
    ReadOperation,
    normalize_read_payload,
    sanitize_untrusted_text,
    validate_read_payload,
)

from neocortex.api.read_api_port import (
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
    inspect_knowledge_asset_health,
    inspect_derivation_lineage,
    knowledge_context_exit_code,
    knowledge_search_exit_code,
    search_code,
    validate_knowledge_asset_resource_id,
)


READ_API_SCHEMA = "neocortex.read-api/v1"
FEDERATION_POLICY = "independent_scopes_no_cross_scope_score_fusion"
MAX_HUMAN_QUERY_CHARS = 4_096
MAX_HUMAN_RESULTS_PER_SCOPE = 100

CancellationCheck = Callable[[], None]


def _status_for_exit_code(code: int) -> str:
    return {
        0: "ok",
        1: "error",
        2: "usage_error",
        3: "empty",
        4: "partial",
        5: "snapshot_changed",
        6: "schema_incompatible",
        7: "corrupt",
        130: "cancelled",
    }.get(code, "error")


def _request_id(value: str | None) -> str:
    """Return a bounded request identity without trusting caller text."""

    if value is None:
        return f"read-{uuid4().hex}"
    if not isinstance(value, str) or not value.strip():
        raise ValueError("request_id must be a non-empty string")
    # A request id is metadata, not corpus content; reject controls and keep
    # the upper bound identical to the other public text inputs.
    normalized = value.strip()
    if len(normalized) > MAX_HUMAN_QUERY_CHARS or any(
        ord(char) < 32 or ord(char) == 127 for char in normalized
    ):
        raise ValueError("request_id is invalid")
    return normalized


def _read_epoch_for_bindings(
    bindings: Sequence[ScopeBinding],
    selected: ReadScope,
) -> dict[str, object]:
    """Capture publication epochs without opening any SQLite owner.

    Epoch metadata is optional for legacy state roots.  A missing or malformed
    marker is represented as unavailable evidence rather than being repaired,
    so adding this field never turns a safe read into a state mutation.
    """

    read_epoch: Callable[[str | Path], object] | None = None
    try:
        from neocortex.persistence.state_publication import read_state_epoch as _read_state_epoch

        read_epoch = _read_state_epoch
    except (ImportError, AttributeError):  # pragma: no cover - minimal runtime
        pass

    values: dict[str, object] = {}
    for binding in bindings:
        if read_epoch is None:
            values[binding.scope.value] = {
                "status": "unavailable",
                "reason": "state_publication_unavailable",
            }
            continue
        try:
            epoch = read_epoch(binding.state_directory)
            as_payload = getattr(epoch, "as_payload", None)
            if not callable(as_payload):
                raise TypeError("state publication epoch is invalid")
            values[binding.scope.value] = as_payload()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            values[binding.scope.value] = {
                "status": "unavailable",
                "reason": sanitize_untrusted_text(str(exc), limit=240),
            }
    return {
        "schema": "neocortex.read-observed-epoch/v1",
        "scope": selected.value,
        "scopes": values,
    }


def _finalize_read_payload(
    payload: dict[str, object],
    operation: ReadOperation,
    selected: ReadScope,
    bindings: Sequence[ScopeBinding],
    *,
    request_id: str | None = None,
    query: str | None = None,
    mode: str | None = None,
    include_history: bool | None = None,
    limit: int | None = None,
) -> dict[str, object]:
    """Complete and immediately validate one descriptor-backed envelope."""

    normalized = normalize_read_payload(
        payload,
        operation,
        scope=selected.value,
        request_id=_request_id(request_id),
    )
    normalized["observed_epoch"] = _read_epoch_for_bindings(bindings, selected)
    result = normalized.get("result")
    if not isinstance(result, dict):
        normalized["result"] = {"scopes": normalized.get("scopes", [])}
    else:
        result.setdefault("scopes", normalized.get("scopes", []))
    return validate_read_payload(
        normalized,
        operation,
        scope=selected.value,
        query=query,
        mode=mode,
        include_history=include_history,
        limit=limit,
        strict_echo=True,
    )


def _finalize_custom_payload(
    payload: dict[str, object],
    operation: ReadOperation,
    selected: ReadScope,
    bindings: Sequence[ScopeBinding],
    *,
    request_id: str | None = None,
) -> dict[str, object]:
    """Complete and validate lineage/asset-health with the shared registry."""

    base = dict(payload)
    base.setdefault("read_only", True)
    base.setdefault("scope_requested", selected.value)
    return _finalize_read_payload(
        base,
        operation,
        selected,
        bindings,
        request_id=request_id,
    )


class ReadScope(StrEnum):
    """Fixed state names; ``framework`` remains a read-only compatibility alias."""

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
    state_directory = default_state_directory()
    available = (ScopeBinding(ReadScope.PERSONAL, state_directory),)
    if selected is ReadScope.FRAMEWORK:
        # The former self-analysis owner was retired. Keep the named scope as
        # a read-only compatibility alias for the shared framework state.
        return (ScopeBinding(ReadScope.FRAMEWORK, state_directory),)
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
        "reason": sanitize_untrusted_text(str(exc), limit=800),
    }


def _snapshot_exit_code(snapshot: KnowledgeSnapshot) -> KnowledgeExitCode:
    states = {owner.state for owner in snapshot.owners}
    if OwnerAvailability.CORRUPT in states:
        return KnowledgeExitCode.CORRUPT
    if states.intersection({OwnerAvailability.FUTURE, OwnerAvailability.INCOMPATIBLE}):
        return KnowledgeExitCode.SCHEMA_INCOMPATIBLE
    if snapshot.consistency is SnapshotConsistency.SNAPSHOT_CHANGED:
        return KnowledgeExitCode.SNAPSHOT_CHANGED
    if not states or OwnerAvailability.AVAILABLE not in states:
        return KnowledgeExitCode.NO_RESULTS
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
    request_id: str | None = None,
) -> dict[str, object]:
    """Return independent published-state snapshots for fixed local scopes."""

    selected = _scope(scope)
    bindings = scope_bindings(selected)
    entries: list[dict[str, object]] = []
    for binding in bindings:
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
    return _finalize_read_payload(
        {
            "schema": READ_API_SCHEMA,
            "kind": "neocortex_scoped_status",
            "read_only": True,
            "scope_requested": selected.value,
            "federation_policy": FEDERATION_POLICY,
            "exit_code": federated_exit_code(entries),
            "scopes": entries,
        },
        ReadOperation.STATUS,
        selected,
        bindings,
        request_id=request_id,
    )


def _asset_health_exit_code(report: object) -> KnowledgeExitCode:
    completeness = getattr(getattr(report, "completeness", None), "value", None)
    reason = getattr(report, "reason_code", None)
    gaps = tuple(item for item in getattr(report, "gaps", ()) if isinstance(item, str))
    if completeness == "complete":
        return KnowledgeExitCode.SUCCESS
    if completeness == "no_evidence":
        return KnowledgeExitCode.NO_RESULTS
    if reason == "snapshot_changed":
        return KnowledgeExitCode.SNAPSHOT_CHANGED
    if (isinstance(reason, str) and "corrupt" in reason) or any("corrupt" in item for item in gaps):
        return KnowledgeExitCode.CORRUPT
    if (isinstance(reason, str) and ("schema" in reason or "incompatible" in reason)) or any(
        "schema" in item or "future" in item or "incompatible" in item for item in gaps
    ):
        return KnowledgeExitCode.SCHEMA_INCOMPATIBLE
    return KnowledgeExitCode.PARTIAL


def asset_health_payload(
    resource_id: str,
    scope: str | ReadScope = ReadScope.ALL,
    *,
    request_id: str | None = None,
) -> dict[str, object]:
    """Explain one stable asset independently in each fixed local scope."""

    normalized_resource_id = validate_knowledge_asset_resource_id(resource_id)
    selected = _scope(scope)
    bindings = scope_bindings(selected)
    entries: list[dict[str, object]] = []
    for binding in bindings:
        try:
            report = inspect_knowledge_asset_health(
                binding.state_directory,
                normalized_resource_id,
            )
            code = _asset_health_exit_code(report)
            entries.append(
                {
                    "scope": binding.scope.value,
                    "state_directory": str(binding.state_directory),
                    "status": report.health.value,
                    "exit_code": int(code),
                    "asset_health": report.to_dict(),
                }
            )
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
            entries.append(_error_entry(binding, exc))
    return _finalize_custom_payload(
        {
            "schema": READ_API_SCHEMA,
            "kind": "neocortex_scoped_asset_health",
            "federation_policy": FEDERATION_POLICY,
            "resource_id": normalized_resource_id,
            "exit_code": federated_exit_code(entries),
            "scopes": entries,
        },
        ReadOperation.ASSET_HEALTH,
        selected,
        bindings,
        request_id=request_id,
    )


def search_payload(
    query: str,
    scope: str | ReadScope = ReadScope.ALL,
    *,
    limit: int = 10,
    mode: str | RetrievalMode = RetrievalMode.EVIDENCE,
    include_history: bool = False,
    cancellation_check: CancellationCheck | None = None,
    request_id: str | None = None,
) -> dict[str, object]:
    """Search fixed scopes independently and preserve each ranking contract."""

    normalized = _validate_query(query)
    selected = _scope(scope)
    bindings = scope_bindings(selected)
    bounded_limit = _validate_limit(limit)
    retrieval_mode = mode if isinstance(mode, RetrievalMode) else RetrievalMode(mode)
    request = KnowledgeQuery(
        normalized,
        retrieval_mode=retrieval_mode,
        include_history=include_history,
        limit=bounded_limit,
    )
    entries: list[dict[str, object]] = []
    for binding in bindings:
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
    return _finalize_read_payload(
        {
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
        },
        ReadOperation.SEARCH,
        selected,
        bindings,
        request_id=request_id,
        query=normalized,
        mode=retrieval_mode.value,
        include_history=include_history,
        limit=bounded_limit,
    )


def context_payload(
    query: str,
    scope: str | ReadScope = ReadScope.ALL,
    *,
    limit: int = 8,
    max_characters: int = 12_000,
    mode: str | RetrievalMode = RetrievalMode.EVIDENCE,
    include_history: bool = False,
    cancellation_check: CancellationCheck | None = None,
    request_id: str | None = None,
) -> dict[str, object]:
    """Build citation-first contexts independently for each fixed scope."""

    normalized = _validate_query(query)
    selected = _scope(scope)
    bindings = scope_bindings(selected)
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
    for binding in bindings:
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
    return _finalize_read_payload(
        {
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
        },
        ReadOperation.CONTEXT,
        selected,
        bindings,
        request_id=request_id,
        query=normalized,
        mode=retrieval_mode.value,
        include_history=include_history,
        limit=bounded_limit,
    )


def evidence_payload(
    query: str,
    citation_id: str,
    scope: str | ReadScope = ReadScope.ALL,
    *,
    limit: int = 8,
    max_characters: int = 12_000,
    request_id: str | None = None,
) -> dict[str, object]:
    """Resolve one citation from a fresh stable context without arbitrary file reads."""

    if not isinstance(citation_id, str) or not citation_id.strip():
        raise ValueError("citation_id cannot be blank")
    normalized_citation_id = citation_id.strip()
    if len(normalized_citation_id) > MAX_HUMAN_QUERY_CHARS or any(
        ord(char) < 32 or ord(char) == 127 for char in normalized_citation_id
    ):
        raise ValueError("citation_id is invalid")
    context = context_payload(
        query,
        scope,
        limit=limit,
        max_characters=max_characters,
        request_id=request_id,
    )
    matches: list[dict[str, object]] = []
    scope_entries = context.get("scopes")
    if not isinstance(scope_entries, list):
        scope_entries = []
    # Evidence historically consumed a loose context double.  Normalize its
    # per-scope status here so the public evidence envelope remains valid even
    # while older producers are being migrated.
    context_code = context.get("exit_code", int(KnowledgeExitCode.FATAL))
    if isinstance(context_code, bool) or not isinstance(context_code, int):
        context_code = int(KnowledgeExitCode.FATAL)
    evidence_scopes: list[dict[str, object]] = []
    for entry in scope_entries:
        if not isinstance(entry, dict):
            continue
        normalized_entry = dict(entry)
        normalized_entry.setdefault("status", _status_for_exit_code(context_code))
        normalized_entry.setdefault("exit_code", context_code)
        evidence_scopes.append(normalized_entry)
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
            if citation.get("citation_id") != normalized_citation_id:
                continue
            matches.append(
                {
                    "scope": entry.get("scope"),
                    "snapshot": bundle.get("snapshot"),
                    "citation": citation,
                    "hit": hit,
                }
            )
    selected = _scope(scope)
    bindings = scope_bindings(selected)
    bounded_limit = _validate_limit(limit)
    evidence_code = (
        context_code
        if matches and context_code not in {0, int(KnowledgeExitCode.NO_RESULTS)}
        else (
            int(KnowledgeExitCode.SUCCESS)
            if matches
            else (
                context_code
                if context_code
                in {
                    int(KnowledgeExitCode.FATAL),
                    int(KnowledgeExitCode.PARTIAL),
                    int(KnowledgeExitCode.SNAPSHOT_CHANGED),
                    int(KnowledgeExitCode.SCHEMA_INCOMPATIBLE),
                    int(KnowledgeExitCode.CORRUPT),
                }
                else int(KnowledgeExitCode.NO_RESULTS)
            )
        )
    )
    return _finalize_read_payload(
        {
            "schema": READ_API_SCHEMA,
            "kind": "neocortex_evidence",
            "read_only": True,
            "scope_requested": selected.value,
            "query": _validate_query(query),
            "citation_id": normalized_citation_id,
            "found": bool(matches),
            "matches": matches,
            "context_exit_code": context_code,
            "limit_per_scope": bounded_limit,
            "exit_code": evidence_code,
            "scopes": evidence_scopes,
            "result": {"matches": matches, "scopes": evidence_scopes},
        },
        ReadOperation.EVIDENCE,
        selected,
        bindings,
        request_id=request_id,
        query=_validate_query(query),
        limit=bounded_limit,
    )


def code_search_payload(
    query: str,
    scope: str | ReadScope = ReadScope.PERSONAL,
    *,
    limit: int = 10,
    modes: Sequence[str] = ("hybrid",),
    request_id: str | None = None,
) -> dict[str, object]:
    """Inspect published Code state under fixed roots without touching sources."""

    normalized = _validate_query(query)
    selected = _scope(scope)
    bindings = scope_bindings(selected)
    bounded_limit = _validate_limit(limit)
    normalized_modes = tuple(dict.fromkeys(modes))
    allowed_modes = frozenset(available_search_modes())
    if not normalized_modes or any(mode not in allowed_modes for mode in normalized_modes):
        raise ValueError("code search modes contain an unsupported value")
    entries: list[dict[str, object]] = []
    for binding in bindings:
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
    return _finalize_read_payload(
        {
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
        },
        ReadOperation.INSPECT_CODE,
        selected,
        bindings,
        request_id=request_id,
        query=normalized,
        limit=bounded_limit,
    )



def lineage_payload(
    identifier: str,
    scope: str | ReadScope = ReadScope.ALL,
    *,
    request_id: str | None = None,
) -> dict[str, object]:
    """Inspect owner-local derivation lineage under fixed trusted state roots."""

    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("lineage identifier cannot be blank")
    if len(identifier) > MAX_HUMAN_QUERY_CHARS:
        raise ValueError(f"lineage identifier cannot exceed {MAX_HUMAN_QUERY_CHARS} characters")
    normalized = identifier.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError("lineage identifier is invalid")
    selected = _scope(scope)
    bindings = scope_bindings(selected)
    entries: list[dict[str, object]] = []
    for binding in bindings:
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
    return _finalize_custom_payload(
        {
            "schema": READ_API_SCHEMA,
            "kind": "neocortex_scoped_derivation_lineage",
            "federation_policy": FEDERATION_POLICY,
            "identifier": normalized,
            "exit_code": federated_exit_code(entries),
            "scopes": entries,
        },
        ReadOperation.LINEAGE,
        selected,
        bindings,
        request_id=request_id,
    )


__all__ = (
    "FEDERATION_POLICY",
    "MAX_HUMAN_QUERY_CHARS",
    "MAX_HUMAN_RESULTS_PER_SCOPE",
    "READ_API_SCHEMA",
    "ReadScope",
    "ScopeBinding",
    "asset_health_payload",
    "code_search_payload",
    "context_payload",
    "evidence_payload",
    "federated_exit_code",
    "lineage_payload",
    "scope_bindings",
    "search_payload",
    "status_payload",
)
