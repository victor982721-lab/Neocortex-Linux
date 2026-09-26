"""Bounded read-only facade over published local Knowledge state.

The operational owners remain the source of truth. This module selects the
canonical local state root, invokes its published readers independently, and
keeps the historical ``framework`` name as a compatibility label where needed.
It never accepts an arbitrary state path and never fuses scores from separate
snapshots.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from neocortex.api.read_contract import (
    ReadOperation,
    normalize_read_payload,
    sanitize_untrusted_text,
    validate_read_payload,
)

from neocortex.api.read_api_port import (
    KnowledgeCompleteness,
    KnowledgeExitCode,
    KnowledgeQuery,
    KnowledgeSnapshot,
    OwnerAvailability,
    RetrievalMode,
    SnapshotConsistency,
    default_state_directory,
    inspect_knowledge_asset_health,
    inspect_derivation_lineage,
    knowledge_context_exit_code,
    knowledge_search_exit_code,
    validate_knowledge_asset_resource_id,
)
from neocortex.knowledge.knowledge_read_budget import (
    KnowledgeReadBudget,
    KnowledgeReadBudgetExceeded,
)

if TYPE_CHECKING:
    from neocortex.api.read_api_port import KnowledgeSearchService


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
    from neocortex.api.read_api_port import KnowledgeSearchService, KnowledgeStatePaths

    return KnowledgeSearchService(KnowledgeStatePaths.from_directory(binding.state_directory))


def _error_entry(binding: ScopeBinding, exc: BaseException) -> dict[str, object]:
    reason = str(exc)
    if isinstance(exc, ModuleNotFoundError) and exc.name:
        reason = (
            f"Published-state inspection requires Python dependency {exc.name!r}; "
            "install the declared dependencies for this capability."
        )
    return {
        "scope": binding.scope.value,
        "state_directory": str(binding.state_directory),
        "status": "error",
        "exit_code": int(KnowledgeExitCode.FATAL),
        "error_type": type(exc).__name__,
        "reason": sanitize_untrusted_text(reason, limit=800),
    }


def _budget_exit_code(error: KnowledgeReadBudgetExceeded) -> KnowledgeExitCode:
    """Preserve explicit user cancellation instead of relabeling it partial."""

    return (
        KnowledgeExitCode.CANCELLED
        if error.reason == "cancelled"
        else KnowledgeExitCode.PARTIAL
    )


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
    # Cancellation is an explicit caller decision, not an owner failure or a
    # successful/empty federated read. Preserve it even when another scope
    # had already produced a result before the cancellation checkpoint.
    if int(KnowledgeExitCode.CANCELLED) in codes:
        return int(KnowledgeExitCode.CANCELLED)
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
        except (ModuleNotFoundError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
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
        except (ModuleNotFoundError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
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


def operational_query_payload(
    query: str,
    scope: str | ReadScope = ReadScope.ALL,
    *,
    limit: int = 20,
    cursor: str | None = None,
    request_id: str | None = None,
) -> dict[str, object]:
    """Answer an explicit operational question from published owner facts.

    This is deliberately a separate read operation from document context:
    an operational question is dispatched to the existing diagnostic/review
    owners and never asks semantic retrieval to reconstruct a corpus state.
    The returned ``operational`` records remain advisory and read-only.
    """

    from neocortex.knowledge.knowledge_operational_query import (
        KnowledgeOperationalQueryService,
        OperationalQueryRequest,
    )
    from neocortex.platform.policy import default_corpus_root

    normalized = _validate_query(query)
    selected = _scope(scope)
    bindings = scope_bindings(selected)
    bounded_limit = _validate_limit(limit)
    if cursor is not None and (
        not isinstance(cursor, str)
        or not cursor.strip()
        or len(cursor) > 8_192
        or any(ord(char) < 32 or ord(char) == 127 for char in cursor)
    ):
        raise ValueError("cursor must be a bounded non-empty string")
    normalized_cursor = cursor.strip() if cursor is not None else None

    def result_code(status: object) -> int:
        if status == "ok":
            return int(KnowledgeExitCode.SUCCESS)
        if status == "empty":
            return int(KnowledgeExitCode.NO_RESULTS)
        if status == "partial":
            return int(KnowledgeExitCode.PARTIAL)
        if status == "snapshot_changed":
            return int(KnowledgeExitCode.SNAPSHOT_CHANGED)
        # Operational owner failures are state outcomes, not empty answers.
        return int(KnowledgeExitCode.FATAL)

    try:
        source_root = default_corpus_root()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        source_root = None
        root_error = sanitize_untrusted_text(str(exc), limit=800)

    entries: list[dict[str, object]] = []
    service = KnowledgeOperationalQueryService()
    for binding in bindings:
        if source_root is None:
            entries.append({
                "scope": binding.scope.value,
                "state_directory": str(binding.state_directory),
                "status": "error",
                "exit_code": int(KnowledgeExitCode.FATAL),
                "reason": root_error,
            })
            continue
        try:
            result = service.query(OperationalQueryRequest(
                normalized,
                binding.state_directory,
                source_root,
                limit=bounded_limit,
                cursor=normalized_cursor,
                scope=selected.value,
            ))
            code = result_code(result.status)
            entries.append({
                "scope": binding.scope.value,
                "state_directory": str(binding.state_directory),
                "status": _status_for_exit_code(code),
                "exit_code": code,
                "operational": result.to_dict(),
            })
        except (ModuleNotFoundError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
            entries.append(_error_entry(binding, exc))
    code = federated_exit_code(entries)
    return _finalize_read_payload(
        {
            "schema": READ_API_SCHEMA,
            "kind": "neocortex_scoped_operational_query",
            "read_only": True,
            "scope_requested": selected.value,
            "federation_policy": FEDERATION_POLICY,
            "query": normalized,
            "limit_per_scope": bounded_limit,
            "cursor": normalized_cursor,
            "advisory_only": True,
            "mutation_authorized": False,
            "exit_code": code,
            "scopes": entries,
        },
        ReadOperation.OPERATIONAL_QUERY,
        selected,
        bindings,
        request_id=request_id,
        query=normalized,
        limit=bounded_limit,
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
    read_budget: KnowledgeReadBudget | None = None,
) -> dict[str, object]:
    """Search fixed scopes independently and preserve each ranking contract."""

    normalized = _validate_query(query)
    selected = _scope(scope)
    bindings = scope_bindings(selected)
    bounded_limit = _validate_limit(limit)
    retrieval_mode = mode if isinstance(mode, RetrievalMode) else RetrievalMode(mode)
    if read_budget is not None and not isinstance(read_budget, KnowledgeReadBudget):
        raise ValueError("read_budget must be a KnowledgeReadBudget when provided")
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
                read_budget=read_budget,
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
        except KnowledgeReadBudgetExceeded as exc:
            code = _budget_exit_code(exc)
            entries.append({
                "scope": binding.scope.value,
                "state_directory": str(binding.state_directory),
                "status": _status_for_exit_code(int(code)),
                "exit_code": int(code),
                "error": {
                    "code": exc.reason,
                    "message": sanitize_untrusted_text(str(exc)),
                },
            })
        except (ModuleNotFoundError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
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
            "read_budget": None if read_budget is None else read_budget.to_dict(),
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


def knowledge_search_projection_payload(
    query: str,
    scope: str | ReadScope = ReadScope.ALL,
    *,
    limit: int = 10,
    mode: str | RetrievalMode = RetrievalMode.EVIDENCE,
    include_history: bool = False,
    cancellation_check: CancellationCheck | None = None,
    request_id: str | None = None,
    read_budget: KnowledgeReadBudget | None = None,
) -> dict[str, object]:
    """Search and expose the additive Knowledge evidence projection.

    ``search_payload`` remains the compatibility/default read surface.  This
    opt-in sibling performs the same bounded, independent scope reads but
    replaces each typed search result with the stable evidence projection.
    Projection is deliberately imported only when this function is called so
    importing the API facade does not import Knowledge projection machinery.
    """

    from neocortex.knowledge.knowledge_evidence_projection import project_knowledge_search

    normalized = _validate_query(query)
    selected = _scope(scope)
    bindings = scope_bindings(selected)
    bounded_limit = _validate_limit(limit)
    retrieval_mode = mode if isinstance(mode, RetrievalMode) else RetrievalMode(mode)
    if read_budget is not None and not isinstance(read_budget, KnowledgeReadBudget):
        raise ValueError("read_budget must be a KnowledgeReadBudget when provided")
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
                read_budget=read_budget,
            )
            entries.append(
                {
                    "scope": binding.scope.value,
                    "state_directory": str(binding.state_directory),
                    "status": "ok" if result.complete else "partial",
                    "exit_code": int(knowledge_search_exit_code(result)),
                    "result": project_knowledge_search(
                        result,
                        scope=binding.scope.value,
                        read_budget=read_budget,
                    ),
                }
            )
        except KnowledgeReadBudgetExceeded as exc:
            code = _budget_exit_code(exc)
            entries.append({
                "scope": binding.scope.value,
                "state_directory": str(binding.state_directory),
                "status": _status_for_exit_code(int(code)),
                "exit_code": int(code),
                "error": {
                    "code": exc.reason,
                    "message": sanitize_untrusted_text(str(exc)),
                },
            })
        except (ModuleNotFoundError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
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
            "read_budget": None if read_budget is None else read_budget.to_dict(),
            "projection": "neocortex.knowledge-evidence-projection/v1",
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
    response_version: int = 1,
    response_transport: str = "json",
    read_budget: KnowledgeReadBudget | None = None,
) -> dict[str, object]:
    """Build citation-first contexts independently for each fixed scope."""

    if isinstance(response_version, bool) or response_version not in {1, 2}:
        raise ValueError("response_version must be 1 or 2")
    if read_budget is not None and not isinstance(read_budget, KnowledgeReadBudget):
        raise ValueError("read_budget must be a KnowledgeReadBudget when provided")
    if response_version == 2:
        return _context_payload_v2(
            query, scope, limit=limit, max_characters=max_characters,
            mode=mode, include_history=include_history,
            cancellation_check=cancellation_check, request_id=request_id,
            response_transport=response_transport,
            read_budget=read_budget,
        )

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
                read_budget=read_budget,
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
        except KnowledgeReadBudgetExceeded as exc:
            code = _budget_exit_code(exc)
            entries.append(
                {
                    "scope": binding.scope.value,
                    "state_directory": str(binding.state_directory),
                    "status": _status_for_exit_code(int(code)),
                    "exit_code": int(code),
                    "error": {
                        "code": exc.reason,
                        "message": sanitize_untrusted_text(str(exc)),
                    },
                }
            )
        except (ModuleNotFoundError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
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
            "read_budget": None if read_budget is None else read_budget.to_dict(),
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


def _context_payload_v2(
    query: str, scope: str | ReadScope, *, limit: int, max_characters: int,
    mode: str | RetrievalMode, include_history: bool,
    cancellation_check: CancellationCheck | None, request_id: str | None,
    response_transport: str, read_budget: KnowledgeReadBudget | None,
) -> dict[str, object]:
    from neocortex.knowledge.knowledge_context_v2 import build_context_response_v2
    from neocortex.knowledge.knowledge_context_hydration import search_context_evidence

    try:
        normalized = _validate_query(query)
        selected = _scope(scope)
        bounded_limit = _validate_limit(limit)
        bounded_characters = _validate_characters(max_characters)
        retrieval_mode = mode if isinstance(mode, RetrievalMode) else RetrievalMode(mode)
        if not isinstance(include_history, bool):
            raise ValueError("include_history must be a bool")
        resolved_request_id = _request_id(request_id)
    except (TypeError, ValueError):
        return build_context_response_v2(
            [{"scope": str(scope), "exit_code": 2, "error": {"code": "invalid_request"}}],
            query=str(query), scope=str(scope), request_id=f"read-{uuid4().hex}",
            mode=str(mode), include_history=include_history, limit=limit,
            max_characters=max_characters, transport=response_transport,
            read_budget=None if read_budget is None else read_budget.to_dict(),
        )
    minimum = build_context_response_v2(
        [], query=normalized, scope=selected.value, request_id=resolved_request_id,
        mode=retrieval_mode.value, include_history=include_history, limit=bounded_limit,
        max_characters=bounded_characters, transport=response_transport,
        read_budget=None if read_budget is None else read_budget.to_dict(),
    )
    if minimum["exit_code"] == 2:
        return minimum
    request = KnowledgeQuery(normalized, retrieval_mode=retrieval_mode,
                             include_history=include_history, limit=bounded_limit)
    entries: list[dict[str, object]] = []
    for binding in scope_bindings(selected):
        try:
            # Both presentations use the same immutable search service. V2
            # must not first discard hits through the v1 per-scope renderer.
            result, evidence_projection = search_context_evidence(
                _service(binding), request, scope=binding.scope.value,
                cancellation_check=cancellation_check,
                read_budget=read_budget,
            )
            entries.append({"scope": binding.scope.value, "result": evidence_projection,
                            "exit_code": int(knowledge_search_exit_code(result))})
        except KnowledgeReadBudgetExceeded as exc:
            code = _budget_exit_code(exc)
            entries.append({
                "scope": binding.scope.value,
                "error": {
                    "code": exc.reason,
                    "message": sanitize_untrusted_text(str(exc)),
                },
                "exit_code": int(code),
            })
        except (ModuleNotFoundError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
            entries.append({"scope": binding.scope.value, "error": {
                "code": "owner_unavailable", "message": sanitize_untrusted_text(str(exc)),
            }})
    return build_context_response_v2(
        entries, query=normalized, scope=selected.value, request_id=resolved_request_id,
        mode=retrieval_mode.value, include_history=include_history,
        limit=bounded_limit, max_characters=bounded_characters, transport=response_transport,
        read_budget=None if read_budget is None else read_budget.to_dict(),
    )


def _evidence_identifier(name: str, value: str | None, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{name} cannot be blank")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} cannot be blank")
    normalized = value.strip()
    if len(normalized) > MAX_HUMAN_QUERY_CHARS or any(
        ord(char) < 32 or ord(char) == 127 for char in normalized
    ):
        raise ValueError(f"{name} is invalid")
    return normalized


def _context_bundle_snapshot_id(bundle: dict[str, object]) -> str | None:
    snapshot = bundle.get("snapshot")
    if not isinstance(snapshot, dict):
        return None
    value = snapshot.get("snapshot_id")
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _candidate_evidence_id(citation: dict[str, object], hit: dict[str, object]) -> str | None:
    citation_value = citation.get("evidence_id")
    citation_id = citation_value if isinstance(citation_value, str) else None
    evidence = hit.get("evidence")
    hit_value = evidence.get("evidence_id") if isinstance(evidence, dict) else None
    hit_id = hit_value if isinstance(hit_value, str) else None
    if citation_id is not None and hit_id is not None and citation_id != hit_id:
        return None
    return citation_id or hit_id


@dataclass(frozen=True, slots=True)
class _EvidenceCandidate:
    scope: object
    snapshot: object
    snapshot_id: str | None
    citation: dict[str, object]
    hit: dict[str, object]
    evidence_id: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "snapshot": self.snapshot,
            "citation": self.citation,
            "hit": self.hit,
        }


def _evidence_context_records(
    scope_entries: list[object],
    *,
    context_code: int,
) -> tuple[list[dict[str, object]], list[str | None], list[_EvidenceCandidate]]:
    scopes: list[dict[str, object]] = []
    snapshot_ids: list[str | None] = []
    candidates: list[_EvidenceCandidate] = []
    for value in scope_entries:
        if not isinstance(value, dict):
            continue
        entry = dict(value)
        entry.setdefault("status", _status_for_exit_code(context_code))
        entry.setdefault("exit_code", context_code)
        scopes.append(entry)
        bundle = value.get("context")
        if not isinstance(bundle, dict):
            continue
        snapshot_id = _context_bundle_snapshot_id(bundle)
        snapshot_ids.append(snapshot_id)
        citations = bundle.get("citation_ids", [])
        selected_hits = bundle.get("selected_hits", [])
        if not isinstance(citations, list) or not isinstance(selected_hits, list):
            continue
        for citation, hit in zip(citations, selected_hits, strict=False):
            if not isinstance(citation, dict) or not isinstance(hit, dict):
                continue
            candidates.append(
                _EvidenceCandidate(
                    scope=value.get("scope"),
                    snapshot=bundle.get("snapshot"),
                    snapshot_id=snapshot_id,
                    citation=citation,
                    hit=hit,
                    evidence_id=_candidate_evidence_id(citation, hit),
                )
            )
    return scopes, snapshot_ids, candidates


def _mark_snapshot_mismatches(
    scopes: list[dict[str, object]],
    expected_snapshot_id: str,
) -> None:
    for entry in scopes:
        bundle = entry.get("context")
        if not isinstance(bundle, dict):
            continue
        if _context_bundle_snapshot_id(bundle) == expected_snapshot_id:
            continue
        entry["status"] = "snapshot_changed"
        entry["exit_code"] = int(KnowledgeExitCode.SNAPSHOT_CHANGED)
        entry["reason"] = "evidence_expected_snapshot_changed"


def _incomplete_evidence_error(code: int) -> dict[str, object] | None:
    if code in {int(KnowledgeExitCode.SUCCESS), int(KnowledgeExitCode.NO_RESULTS)}:
        return None
    status = _status_for_exit_code(code)
    return {
        "code": status,
        "message": status,
        "retryable": code
        in {int(KnowledgeExitCode.PARTIAL), int(KnowledgeExitCode.SNAPSHOT_CHANGED)},
    }


def _direct_evidence_payload(
    source_ref: Mapping[str, object] | None,
    evidence_ref: Mapping[str, object] | None, *, scope: str | ReadScope,
    max_characters: int, request_id: str | None, response_transport: str,
) -> dict[str, object]:
    from neocortex.knowledge.knowledge_context_v2 import build_context_response_v2
    from neocortex.knowledge.knowledge_evidence_lookup import EvidenceLookupError, lookup_owner_evidence

    selected_scope = str(scope)
    request = f"read-{uuid4().hex}"
    entries: list[dict[str, object]]
    try:
        request = _request_id(request_id)
        selected = _scope(scope)
        selected_scope = selected.value
        if not isinstance(source_ref, Mapping) or not isinstance(evidence_ref, Mapping):
            raise EvidenceLookupError("invalid_evidence_reference")
        binding = next((item for item in scope_bindings(selected)
                        if item.scope.value == source_ref.get("scope")), None)
        if binding is None:
            raise EvidenceLookupError("evidence_scope_mismatch")
        result = lookup_owner_evidence(binding.state_directory, source_ref, evidence_ref)
        entries = [{"scope": binding.scope.value, "result": result}]
    except EvidenceLookupError as exc:
        entries = [{"scope": selected_scope, "exit_code": 4,
                    "error": {"code": exc.code}}]
    except (TypeError, ValueError):
        entries = [{"scope": selected_scope, "exit_code": 2,
                    "error": {"code": "invalid_request"}}]
    except (OSError, RuntimeError, sqlite3.Error):
        entries = [{"scope": selected_scope, "exit_code": 4,
                    "error": {"code": "owner_evidence_unavailable"}}]
    return build_context_response_v2(
        entries, query="", scope=selected_scope, request_id=request, limit=1,
        max_characters=max_characters, transport=response_transport, operation="evidence",
    )


def evidence_payload(
    query: str = "",
    citation_id: str = "",
    scope: str | ReadScope = ReadScope.ALL,
    *,
    evidence_id: str | None = None,
    expected_snapshot_id: str | None = None,
    limit: int = 8,
    max_characters: int = 12_000,
    request_id: str | None = None,
    source_ref: Mapping[str, object] | None = None,
    evidence_ref: Mapping[str, object] | None = None,
    response_transport: str = "json",
    response_version: int = 1,
    read_budget: KnowledgeReadBudget | None = None,
) -> dict[str, object]:
    """Resolve stable evidence from one fresh context without arbitrary file reads."""

    if isinstance(response_version, bool) or response_version not in {1, 2}:
        raise ValueError("response_version must be 1 or 2")
    if read_budget is not None and not isinstance(read_budget, KnowledgeReadBudget):
        raise ValueError("read_budget must be a KnowledgeReadBudget when provided")

    if source_ref is not None or evidence_ref is not None:
        return _direct_evidence_payload(
            source_ref, evidence_ref, scope=scope, max_characters=max_characters,
            request_id=request_id, response_transport=response_transport,
        )

    if response_version == 2:
        from neocortex.knowledge.knowledge_context_v2 import (
            select_evidence_response_v2,
        )

        normalized_query = _validate_query(query)
        normalized_citation_id = _evidence_identifier(
            "citation_id", citation_id, required=True,
        )
        assert normalized_citation_id is not None
        normalized_evidence_id = _evidence_identifier("evidence_id", evidence_id)
        normalized_snapshot_id = _evidence_identifier(
            "expected_snapshot_id", expected_snapshot_id,
        )
        bounded_limit = _validate_limit(limit)
        context = context_payload(
            normalized_query,
            scope,
            limit=bounded_limit,
            max_characters=max_characters,
            request_id=request_id,
            response_version=2,
            response_transport=response_transport,
            read_budget=read_budget,
        )
        return select_evidence_response_v2(
            context,
            citation_id=normalized_citation_id,
            evidence_id=normalized_evidence_id,
            expected_snapshot_id=normalized_snapshot_id,
        )

    normalized_query = _validate_query(query)
    normalized_citation_id = _evidence_identifier("citation_id", citation_id, required=True)
    assert normalized_citation_id is not None
    normalized_evidence_id = _evidence_identifier("evidence_id", evidence_id)
    normalized_snapshot_id = _evidence_identifier(
        "expected_snapshot_id", expected_snapshot_id
    )
    bounded_limit = _validate_limit(limit)
    normalized_request_id = _request_id(request_id)
    context = context_payload(
        normalized_query,
        scope,
        limit=bounded_limit,
        max_characters=max_characters,
        request_id=normalized_request_id,
        read_budget=read_budget,
    )
    scope_entries = context.get("scopes")
    if not isinstance(scope_entries, list):
        scope_entries = []
    context_code = context.get("exit_code", int(KnowledgeExitCode.FATAL))
    if isinstance(context_code, bool) or not isinstance(context_code, int):
        context_code = int(KnowledgeExitCode.FATAL)
    evidence_scopes, snapshot_ids, candidates = _evidence_context_records(
        scope_entries,
        context_code=context_code,
    )
    if normalized_snapshot_id is not None:
        _mark_snapshot_mismatches(evidence_scopes, normalized_snapshot_id)

    snapshot_changed = (
        normalized_snapshot_id is not None
        and normalized_snapshot_id not in snapshot_ids
    )
    eligible = (
        []
        if snapshot_changed
        else [
            candidate
            for candidate in candidates
            if normalized_snapshot_id is None
            or candidate.snapshot_id == normalized_snapshot_id
        ]
    )
    ambiguous_alias = False
    if normalized_evidence_id is not None:
        selected_candidates = [
            candidate for candidate in eligible if candidate.evidence_id == normalized_evidence_id
        ]
    else:
        alias_candidates = [
            candidate
            for candidate in eligible
            if candidate.citation.get("citation_id") == normalized_citation_id
        ]
        alias_targets = {
            candidate.evidence_id or f"unknown:{index}"
            for index, candidate in enumerate(alias_candidates)
        }
        ambiguous_alias = len(alias_targets) > 1
        selected_candidates = [] if ambiguous_alias else alias_candidates

    matches = [candidate.to_dict() for candidate in selected_candidates]
    resolved_evidence_id = normalized_evidence_id
    if resolved_evidence_id is None and matches:
        resolved_evidence_id = selected_candidates[0].evidence_id

    if snapshot_changed:
        evidence_code = int(KnowledgeExitCode.SNAPSHOT_CHANGED)
        error: dict[str, object] | None = {
            "code": "snapshot_changed",
            "message": "expected evidence snapshot is not the current context snapshot",
            "retryable": True,
        }
    elif ambiguous_alias:
        evidence_code = int(KnowledgeExitCode.PARTIAL)
        error = {
            "code": "ambiguous_citation",
            "message": "citation alias identifies multiple evidence records; provide evidence_id",
            "retryable": False,
        }
    else:
        scope_code = federated_exit_code(evidence_scopes) if evidence_scopes else context_code
        evidence_code = (
            scope_code
            if matches
            and scope_code not in {
                int(KnowledgeExitCode.SUCCESS),
                int(KnowledgeExitCode.NO_RESULTS),
            }
            else (
                int(KnowledgeExitCode.SUCCESS)
                if matches
                else (
                    scope_code
                    if scope_code
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
        error = _incomplete_evidence_error(evidence_code)

    selected = _scope(scope)
    bindings = scope_bindings(selected)
    result = {
        "matches": matches,
        "scopes": evidence_scopes,
        "evidence_id": resolved_evidence_id,
        "expected_snapshot_id": normalized_snapshot_id,
    }
    return _finalize_read_payload(
        {
            "schema": READ_API_SCHEMA,
            "kind": "neocortex_evidence",
            "read_only": True,
            "scope_requested": selected.value,
            "query": normalized_query,
            "citation_id": normalized_citation_id,
            "evidence_id": resolved_evidence_id,
            "expected_snapshot_id": normalized_snapshot_id,
            "found": bool(matches),
            "matches": matches,
            "context_exit_code": context_code,
            "limit_per_scope": bounded_limit,
            "exit_code": evidence_code,
            "error": error,
            "scopes": evidence_scopes,
            "result": result,
        },
        ReadOperation.EVIDENCE,
        selected,
        bindings,
        request_id=normalized_request_id,
        query=normalized_query,
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
        except (ModuleNotFoundError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
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
    "KnowledgeReadBudget",
    "ReadScope",
    "ScopeBinding",
    "asset_health_payload",
    "context_payload",
    "evidence_payload",
    "federated_exit_code",
    "knowledge_search_projection_payload",
    "lineage_payload",
    "operational_query_payload",
    "scope_bindings",
    "search_payload",
    "status_payload",
)
