"""Canonical flat CLI handlers for the read-only Knowledge Plane."""

from __future__ import annotations
import argparse
import json
import sqlite3
import sys
import threading
from collections.abc import Callable
from neocortex.api.status_codes import KnowledgeExitCode
from typing import TYPE_CHECKING, TextIO, TypeVar
from uuid import uuid4

from neocortex.runtime.control.console_cancellation import ConsoleCancellationBridge
from neocortex.knowledge.knowledge_contracts import (
    KnowledgeCompleteness,
    OwnerAvailability,
    SnapshotConsistency,
)

if TYPE_CHECKING:
    from neocortex.knowledge.knowledge_contracts import ContextBundle, KnowledgeSnapshot
    from neocortex.knowledge.knowledge_planner import KnowledgeQuery
    from neocortex.knowledge.knowledge_search import KnowledgeSearchResult
    from neocortex.knowledge.knowledge_service import KnowledgeSearchService


# region [01] Stable exit contract and cancellation boundary


_T = TypeVar("_T")


def _console_text(value: str, stream: object) -> str:
    """Keep corpus-derived output printable on legacy Windows consoles."""

    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return value
    try:
        value.encode(encoding)
    except UnicodeEncodeError:
        return value.encode(encoding, errors="backslashreplace").decode(encoding)
    except LookupError:  # pragma: no cover - defensive custom stream support
        return value
    return value


def _print_console_line(value: str, *, file: TextIO | None = None) -> None:
    stream = sys.stdout if file is None else file
    print(_console_text(value, stream), file=stream)


def _with_cancellation(operation: Callable[[Callable[[], None]], _T]) -> _T:
    requested = threading.Event()

    def checkpoint() -> None:
        if requested.is_set():
            raise KeyboardInterrupt

    with ConsoleCancellationBridge(requested.set):
        return operation(checkpoint)


def _service(args: argparse.Namespace) -> KnowledgeSearchService:
    from neocortex.knowledge.knowledge_service import KnowledgeSearchService
    from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths

    paths = KnowledgeStatePaths.from_directory(args.state_directory)
    return KnowledgeSearchService(paths)


def _query(args: argparse.Namespace, value: str) -> KnowledgeQuery:
    from neocortex.knowledge.knowledge_planner import KnowledgeQuery, RetrievalMode

    return KnowledgeQuery(
        value,
        retrieval_mode=RetrievalMode(args.knowledge_mode),
        include_history=args.knowledge_history,
        limit=args.knowledge_limit,
    )


def _read_budget(args: argparse.Namespace):
    max_rows = getattr(args, "knowledge_budget_rows", None)
    max_vectors = getattr(args, "knowledge_budget_vectors", None)
    max_temporary_bytes = getattr(args, "knowledge_budget_temporary_bytes", None)
    deadline_seconds = getattr(args, "knowledge_budget_seconds", None)
    if all(value is None for value in (max_rows, max_vectors, max_temporary_bytes, deadline_seconds)):
        return None
    from neocortex.knowledge.knowledge_read_budget import KnowledgeReadBudget

    return KnowledgeReadBudget(
        max_rows=max_rows,
        max_vectors=max_vectors,
        max_temporary_bytes=max_temporary_bytes,
        deadline_seconds=deadline_seconds,
    )


def _snapshot_exit_code(snapshot: KnowledgeSnapshot) -> KnowledgeExitCode:
    states = {owner.state for owner in snapshot.owners}
    if OwnerAvailability.CORRUPT in states:
        return KnowledgeExitCode.CORRUPT
    if states.intersection({OwnerAvailability.FUTURE, OwnerAvailability.INCOMPATIBLE}):
        return KnowledgeExitCode.SCHEMA_INCOMPATIBLE
    if snapshot.consistency is SnapshotConsistency.SNAPSHOT_CHANGED:
        return KnowledgeExitCode.SNAPSHOT_CHANGED
    return KnowledgeExitCode.SUCCESS


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


def _failure(operation: str, exc: BaseException) -> int:
    _print_console_line(
        f"ERROR {operation} {type(exc).__name__}: {exc}",
        file=sys.stderr,
    )
    return int(KnowledgeExitCode.FATAL)


# endregion [01]


# region [02] Stable human output


def _print_snapshot(snapshot: KnowledgeSnapshot) -> None:
    _print_console_line(
        f"KNOWLEDGE_STATUS snapshot={snapshot.snapshot_id} "
        f"consistency={snapshot.consistency.value} attempts={snapshot.attempts}"
    )
    for owner in snapshot.owners:
        publications = ",".join(f"{head.scope}:{head.generation}" for head in owner.publications)
        _print_console_line(
            f"KNOWLEDGE_OWNER owner={owner.owner} state={owner.state.value} "
            f"schema={owner.observed_schema_version or '-'} "
            f"expected={owner.expected_schema_version} "
            f"publications={publications or '-'} "
            f"warning={json.dumps(owner.warning, ensure_ascii=False)}"
        )


def _print_search(result: KnowledgeSearchResult) -> None:
    _print_console_line(
        f"KNOWLEDGE_SEARCH query={json.dumps(result.plan.normalized_query, ensure_ascii=False)} "
        f"snapshot={result.snapshot.snapshot_id} complete={int(result.complete)} "
        f"hits={len(result.hits)} rows={result.rows_scanned} "
        f"vectors={result.vectors_scanned} truncated={int(result.truncated)}"
    )
    for ranking in result.rankings:
        _print_console_line(
            f"KNOWLEDGE_RANKING name={ranking.name} channel={ranking.channel} "
            f"executed={int(ranking.executed)} available={int(ranking.available)} "
            f"complete={int(ranking.complete)} returned={ranking.returned} "
            f"reason={ranking.reason or '-'}"
        )
    for hit in result.hits:
        identifiers = dict(hit.evidence.identifiers)
        inside_zip = hit.resource.owner == "archive" or identifiers.get("inside_zip") == "1"
        locator = json.dumps(
            hit.evidence.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        _print_console_line(
            f"KNOWLEDGE_HIT rank={hit.rank} score={hit.fused_score:.12f} "
            f"resource={hit.resource.resource_id} revision={hit.revision.revision_id} "
            f"location={'archive_member' if inside_zip else 'physical'} "
            f"inside_zip={int(inside_zip)} "
            f"container={json.dumps(identifiers.get('container_path'), ensure_ascii=False)} "
            f"member={json.dumps(identifiers.get('member_path'), ensure_ascii=False)} "
            f"chain={json.dumps(identifiers.get('member_chain'), ensure_ascii=False)} "
            f"path={json.dumps(hit.resource.current_path, ensure_ascii=False)} "
            f"evidence={locator}"
        )


# endregion [02]


# region [03] Direct handlers


def run_knowledge_status(args: argparse.Namespace) -> int:
    try:
        snapshot = _with_cancellation(
            lambda checkpoint: _service(args).status(cancellation_check=checkpoint)
        )
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
        return _failure("knowledge-status", exc)
    if args.knowledge_json:
        _print_console_line(snapshot.to_json())
    else:
        _print_snapshot(snapshot)
    return int(_snapshot_exit_code(snapshot))


def _knowledge_health_exit_code(report: object) -> KnowledgeExitCode:
    completeness = getattr(getattr(report, "completeness", None), "value", None)
    reason = getattr(report, "reason_code", None)
    gaps = tuple(
        item for item in getattr(report, "gaps", ()) if isinstance(item, str)
    )
    if completeness == "complete":
        return KnowledgeExitCode.SUCCESS
    if completeness == "no_evidence":
        return KnowledgeExitCode.NO_RESULTS
    if reason == "snapshot_changed":
        return KnowledgeExitCode.SNAPSHOT_CHANGED
    if (isinstance(reason, str) and "corrupt" in reason) or any(
        "corrupt" in item for item in gaps
    ):
        return KnowledgeExitCode.CORRUPT
    if (isinstance(reason, str) and ("schema" in reason or "incompatible" in reason)) or any(
        "schema" in item or "future" in item or "incompatible" in item
        for item in gaps
    ):
        return KnowledgeExitCode.SCHEMA_INCOMPATIBLE
    return KnowledgeExitCode.PARTIAL


def run_knowledge_health(args: argparse.Namespace) -> int:
    try:
        from neocortex.knowledge.knowledge_asset_health import inspect_knowledge_asset_health
        from neocortex.knowledge.knowledge_asset_health_contracts import KnowledgeAssetHealthQuery
        from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths

        query = KnowledgeAssetHealthQuery(args.knowledge_health)
        report = inspect_knowledge_asset_health(
            KnowledgeStatePaths.from_directory(args.state_directory),
            query,
        )
    except ValueError as exc:
        _print_console_line(f"ERROR knowledge-health ValueError: {exc}", file=sys.stderr)
        return int(KnowledgeExitCode.USAGE)
    except (OSError, RuntimeError, sqlite3.Error, TypeError) as exc:
        return _failure("knowledge-health", exc)
    if args.knowledge_json:
        _print_console_line(report.to_json())
    else:
        _print_console_line(
            f"KNOWLEDGE_HEALTH resource={report.resource_id} "
            f"health={report.health.value} completeness={report.completeness.value} "
            f"reason={report.reason_code or '-'} snapshot={report.knowledge_snapshot_id or '-'}"
        )
        for gap in report.gaps:
            _print_console_line(f"KNOWLEDGE_HEALTH_GAP {gap}")
        _print_console_line("No se creó, migró ni modificó estado.")
    return int(_knowledge_health_exit_code(report))


def run_knowledge_search(args: argparse.Namespace) -> int:
    try:
        query = _query(args, args.knowledge_search)
        read_budget = _read_budget(args)
        result = _with_cancellation(
            lambda checkpoint: _service(args).search(
                query,
                cancellation_check=checkpoint,
                read_budget=read_budget,
            )
        )
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
        return _failure("knowledge-search", exc)
    if getattr(args, "knowledge_projection", False):
        from neocortex.knowledge.knowledge_evidence_projection import (
            knowledge_search_projection_payload,
        )
        from neocortex.semantic.semantic_models import canonical_json

        _print_console_line(
            canonical_json(
                knowledge_search_projection_payload(
                    result,
                    scope=getattr(args, "knowledge_scope", "personal"),
                    read_budget=read_budget,
                )
            )
        )
    elif args.knowledge_json:
        _print_console_line(result.to_json())
    else:
        _print_search(result)
    return int(knowledge_search_exit_code(result))


def _run_knowledge_context_v2(args: argparse.Namespace) -> int:
    from neocortex.knowledge.knowledge_context_v2 import (
        build_context_response_v2,
        render_context_response,
        serialize_context_response,
    )

    entries: list[dict[str, object]]
    scope = getattr(args, "knowledge_scope", "personal")
    # ``all`` is a request scope; the current published topology exposes one
    # personal binding, matching the read API's federated binding semantics.
    binding_scope = "personal" if scope == "all" else scope
    try:
        query = _query(args, args.knowledge_context)
        from neocortex.knowledge.knowledge_context_hydration import search_context_evidence

        result, evidence_projection = _with_cancellation(
            lambda checkpoint: search_context_evidence(
                _service(args),
                query,
                scope=binding_scope,
                cancellation_check=checkpoint,
                read_budget=_read_budget(args),
            )
        )
        entries = [{"scope": binding_scope, "result": evidence_projection,
                    "exit_code": int(knowledge_search_exit_code(result))}]
    except (ModuleNotFoundError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
        invalid_request = isinstance(exc, (TypeError, ValueError))
        entries = [
            {
                "scope": binding_scope,
                "error": {
                    "code": "invalid_request" if invalid_request else "owner_unavailable",
                    "message": f"{type(exc).__name__}: {exc}",
                },
                "exit_code": int(
                    KnowledgeExitCode.USAGE if invalid_request else KnowledgeExitCode.FATAL
                ),
            }
        ]
    payload = build_context_response_v2(
        entries,
        query=args.knowledge_context,
        scope=scope,
        request_id=f"read-{uuid4().hex}",
        mode=args.knowledge_mode,
        include_history=args.knowledge_history,
        limit=args.knowledge_limit,
        max_characters=args.knowledge_context_characters,
        transport="json" if args.knowledge_json else "text",
    )
    # The budget covers the complete output, including this single newline.
    print(
        serialize_context_response(payload)
        if args.knowledge_json
        else render_context_response(payload)
    )
    return int(payload["exit_code"])


def run_knowledge_context(args: argparse.Namespace) -> int:
    if getattr(args, "knowledge_response_version", 2) == 2:
        return _run_knowledge_context_v2(args)
    try:
        query = _query(args, args.knowledge_context)
        bundle = _with_cancellation(
            lambda checkpoint: _service(args).context(
                query,
                max_characters=args.knowledge_context_characters,
                max_hits=args.knowledge_limit,
                cancellation_check=checkpoint,
                read_budget=_read_budget(args),
            )
        )
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
        return _failure("knowledge-context", exc)
    if args.knowledge_json:
        _print_console_line(bundle.to_json())
    else:
        _print_console_line(
            f"KNOWLEDGE_CONTEXT completeness={bundle.completeness.value} "
            f"citations={len(bundle.citation_ids)} "
            f"characters={bundle.budget.characters_used}"
        )
        _print_console_line(bundle.rendered_context)
    return int(knowledge_context_exit_code(bundle))


# endregion [03]


__all__ = (
    "KnowledgeExitCode",
    "knowledge_context_exit_code",
    "knowledge_search_exit_code",
    "run_knowledge_context",
    "run_knowledge_health",
    "run_knowledge_search",
    "run_knowledge_status",
)
