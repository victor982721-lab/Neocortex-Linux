"""Bounded catalog retrieval for Knowledge Search.
# region [00] Contexto del módulo
# Módulo: neocortex/knowledge_search_catalog.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


The facade injects owner services and public contract types on every call. This
module deliberately has no dependency on ``knowledge_search`` or
``document_catalog`` so import order and runtime monkeypatching remain stable.
"""

# region [01] Dependencias del módulo
from __future__ import annotations
from neocortex.knowledge.knowledge_read_operation import read_rows, read_query_limit
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Protocol

from neocortex.documents.document_resource_binding import (
    ResourceBindingError,
    parse_resource_binding,
)
from neocortex.foundation.file_identity import FileIdentity, FileIdentityEncoding
from .knowledge_contracts import (
    EvidenceMethod,
    EvidenceRef,
    KnowledgeSnapshot,
    PhysicalIdentityRef,
    RankingSignal,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from .knowledge_planner import KnowledgePlan
from .knowledge_search_contracts import KnowledgeCandidate, RankingExecution
from .knowledge_snapshot import KnowledgeStatePaths
from neocortex.semantic.semantic_models import ContentFingerprint
from neocortex.persistence.sqlite_cancellation import SQLiteCancellationBridge
# endregion [01]

# region [02] Implementación


class _CatalogCursor(Protocol):
    def fetchall(self) -> Sequence[Mapping[str, object]]: ...


class _CatalogConnection(Protocol):
    def execute(
        self,
        statement: str,
        parameters: tuple[object, ...] = (),
    ) -> _CatalogCursor: ...


class _CatalogDatabaseManager(Protocol):
    def __enter__(self) -> _CatalogConnection: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


_JsonLoads = Callable[[str], object]
_OwnerAvailable = Callable[[KnowledgeSnapshot, str], bool]
_PlannedCandidateLimit = Callable[[KnowledgePlan, str], int]
_DocumentCatalogDatabase = Callable[..., _CatalogDatabaseManager]
_SQLiteCancellationScope = Callable[
    ...,
    AbstractContextManager[SQLiteCancellationBridge],
]
_ReraiseCapturedCancellation = Callable[
    [SQLiteCancellationBridge, BaseException],
    None,
]
_CleanupPreservingPrimary = Callable[..., None]
_DecimalIdentityValue = Callable[[object], int]
_DirectResourceRef = Callable[..., tuple[ResourceRef, tuple[str, ...]]]
_CanonicalJson = Callable[[Mapping[str, object] | None], str]
_FingerprintText = Callable[[str], ContentFingerprint]
_CatalogIdentifiers = Callable[[object], tuple[tuple[str, str], ...]]


def escape_like(value: str) -> str:
    """Escape SQL LIKE metacharacters while preserving literal text."""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def catalog_identifiers(
    value: object,
    *,
    json_loads_fn: _JsonLoads,
) -> tuple[tuple[str, str], ...]:
    """Decode at most 64 ordered, unique standard identifiers."""

    try:
        decoded = json_loads_fn(str(value))
    except (TypeError, ValueError):
        return ()
    if not isinstance(decoded, list):
        return ()
    identifiers: list[tuple[str, str]] = []
    for item in decoded[:64]:
        if isinstance(item, str) and item.strip():
            identifiers.append(("standard_identifier", item.strip()))
        elif isinstance(item, dict):
            identifier = item.get("identifier")
            if isinstance(identifier, str) and identifier.strip():
                identifiers.append(("standard_identifier", identifier.strip()))
    return tuple(dict.fromkeys(identifiers))


def _ranking_report(
    ranking_execution_type: type[RankingExecution],
    *,
    executed: bool,
    available: bool,
    complete: bool,
    returned: int = 0,
    rows_scanned: int = 0,
    reason: str | None = None,
    result_window_full: bool = False,
) -> RankingExecution:
    return ranking_execution_type(
        "catalog_metadata",
        "catalog",
        executed,
        available,
        complete,
        returned,
        rows_scanned=rows_scanned,
        reason=reason,
        result_window_full=result_window_full,
    )


def _snapshot_heads(
    snapshot: KnowledgeSnapshot,
) -> tuple[tuple[str, int], ...]:
    owner = next(item for item in snapshot.owners if item.owner == "catalog")
    return tuple((head.scope, head.generation) for head in owner.publications)


def _row_value(row: Mapping[str, object], name: str) -> object | None:
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def _available_snapshot_heads(
    rows: Sequence[Mapping[str, object]],
    expected_heads: tuple[tuple[str, int], ...],
) -> tuple[tuple[tuple[str, int], ...], str | None]:
    observed: dict[tuple[str, int], Mapping[str, object]] = {}
    for row in rows:
        try:
            key = (str(row["source_kind"]), int(str(row["generation_id"])))
        except (KeyError, TypeError, ValueError):
            continue
        observed[key] = row

    available: list[tuple[str, int]] = []
    for source_kind, generation in expected_heads:
        observed_row = observed.get((source_kind, generation))
        if observed_row is None:
            continue
        status = str(_row_value(observed_row, "status") or "").casefold()
        actual_kind = str(_row_value(observed_row, "actual_kind") or "")
        if status in {"published", "superseded"} and (
            actual_kind.casefold() == source_kind.casefold()
        ):
            available.append((source_kind, generation))

    if not available:
        return (), "catalog_snapshot_heads_unavailable"
    if len(available) != len(expected_heads):
        return tuple(available), "catalog_snapshot_heads_partially_unavailable"
    return tuple(available), None


def _valid_identifier_metadata(value: object, json_loads_fn: _JsonLoads) -> bool:
    try:
        decoded = json_loads_fn(str(value))
    except (TypeError, ValueError):
        return False
    return isinstance(decoded, list) and len(decoded) <= 64


def _read_catalog_rows(
    paths: KnowledgeStatePaths,
    plan: KnowledgePlan,
    heads: tuple[tuple[str, int], ...],
    cancellation: SQLiteCancellationBridge,
    *,
    lexical_owner_formats: Mapping[str, frozenset[str]],
    escape_like_fn: Callable[[str], str],
    target_limit: int,
    max_candidates: int,
    document_catalog_database_fn: _DocumentCatalogDatabase,
    sqlite_cancellation_scope_fn: _SQLiteCancellationScope,
    cleanup_preserving_primary_fn: _CleanupPreservingPrimary,
) -> tuple[Sequence[Mapping[str, object]], str | None]:
    expected_values = ",".join("(?,?)" for _ in heads)
    head_parameters = tuple(value for head in heads for value in head)
    manager = document_catalog_database_fn(paths.catalog, readonly=True)
    connection = manager.__enter__()
    primary_error: BaseException | None = None
    try:
        with sqlite_cancellation_scope_fn(connection, cancellation):
            preflight_rows = connection.execute(
                f"""WITH expected(source_kind,generation_id) AS (
                VALUES {expected_values})
                SELECT e.source_kind,e.generation_id,g.status,
                g.source_kind AS actual_kind
                FROM expected e LEFT JOIN catalog_generations g
                ON g.generation_id=e.generation_id
                ORDER BY e.source_kind,e.generation_id""",
                head_parameters,
            ).fetchall()
            available_heads, head_reason = _available_snapshot_heads(
                preflight_rows,
                heads,
            )
            if not available_heads:
                return (), head_reason

            values = ",".join("(?,?)" for _ in available_heads)
            parameters: list[object] = [value for head in available_heads for value in head]
            clauses = ["d.active=1", "d.catalog_status<>'error'"]
            if plan.source_kinds:
                expanded_source_kinds: set[str] = set()
                for value in plan.source_kinds:
                    if value in {"office", "audio"}:
                        expanded_source_kinds.update(lexical_owner_formats[value])
                    else:
                        expanded_source_kinds.add(value)
                placeholders = ",".join("?" for _ in expanded_source_kinds)
                clauses.append(f"d.source_kind COLLATE NOCASE IN ({placeholders})")
                parameters.extend(sorted(expanded_source_kinds))
            if plan.formats:
                format_clauses: list[str] = []
                for value in plan.formats:
                    extension = value.casefold().lstrip(".")
                    format_clauses.append("lower(d.path) LIKE ? ESCAPE '\\'")
                    parameters.append(f"%.{escape_like_fn(extension)}")
                clauses.append(f"({' OR '.join(format_clauses)})")
            if plan.project is not None:
                clauses.append(
                    "(d.primary_project=? COLLATE NOCASE "
                    "OR EXISTS(SELECT 1 FROM json_each("
                    "CASE WHEN json_valid(d.projects_json) THEN d.projects_json "
                    "ELSE '[]' END) project WHERE "
                    "CASE WHEN project.type='text' THEN CAST(project.value AS TEXT) "
                    "ELSE COALESCE(json_extract(project.value,'$.label'),"
                    "json_extract(project.value,'$.project')) END=? COLLATE NOCASE))"
                )
                parameters.extend((plan.project, plan.project))
            requested_limit = read_query_limit(min(max_candidates, target_limit + 1))
            rows = read_rows(connection.execute(
                f"""WITH expected(source_kind,generation_id) AS (
                VALUES {values})
                SELECT d.source_kind,d.file_key,d.path,d.volume_id,d.file_id,
                d.birthtime_ns,d.size,d.mtime_ns,d.processing_signature,
                d.classifier_signature,d.primary_kind,d.primary_subtype,
                d.primary_project,d.confidence,d.uncertainty,
                d.standard_references_json,d.source_status,d.catalog_status,
                d.updated_ns,d.last_seen_catalog_run_id,d.resource_binding_json,
                g.generation_id
                FROM expected e JOIN catalog_generations g
                ON g.generation_id=e.generation_id
                JOIN catalog_generation_documents d
                ON d.generation_id=e.generation_id
                AND d.source_kind=e.source_kind
                WHERE {" AND ".join(clauses)}
                ORDER BY d.confidence DESC,d.source_kind,d.path COLLATE NOCASE
                LIMIT ?""",
                (*parameters, requested_limit),
            ))
            return rows, head_reason
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if primary_error is None:
            manager.__exit__(None, None, None)
        else:
            cleanup_preserving_primary_fn(
                lambda: manager.__exit__(
                    type(primary_error),
                    primary_error,
                    primary_error.__traceback__,
                ),
                primary_error,
                label="catalog connection close cleanup",
            )


def _materialize_candidate(
    row: Mapping[str, object],
    source_rank: int,
    *,
    decimal_identity_value_fn: _DecimalIdentityValue,
    file_identity_type: type[FileIdentity],
    file_identity_encoding: FileIdentityEncoding,
    direct_resource_ref_fn: _DirectResourceRef,
    canonical_json_fn: _CanonicalJson,
    fingerprint_text_fn: _FingerprintText,
    revision_ref_type: type[RevisionRef],
    revision_state_type: type[RevisionState],
    catalog_identifiers_fn: _CatalogIdentifiers,
    evidence_ref_type: type[EvidenceRef],
    evidence_method_type: type[EvidenceMethod],
    knowledge_candidate_type: type[KnowledgeCandidate],
    ranking_signal_type: type[RankingSignal],
) -> tuple[KnowledgeCandidate, bool]:
    source_kind = str(row["source_kind"])
    file_key = str(row["file_key"])
    path = str(row["path"])
    identity_warnings: tuple[str, ...] = ()
    raw_binding = _row_value(row, "resource_binding_json")
    binding: Mapping[str, object] | None = None
    if raw_binding is not None:
        try:
            parsed = parse_resource_binding(raw_binding)
        except ResourceBindingError as exc:
            raise ValueError("catalog resource binding is invalid") from exc
        if parsed.get("source_kind") != source_kind or parsed.get("file_key") != file_key:
            raise ValueError("catalog resource binding does not match its row")
        binding = parsed

    if source_kind == "archive":
        # Archive members are logical references, not filesystem identities.
        # Never reinterpret the owner key or denormalized volume/file columns
        # as decimal or hex merely because they happen to contain numeric text.
        if binding is None:
            resource = ResourceRef(
                f"resource:{source_kind}:{file_key}",
                source_kind,
                source_kind,
                current_path=path,
            )
            identity_warnings = ("physical_identity_unresolved",)
        else:
            ref = binding.get("resource_ref")
            if not isinstance(ref, Mapping):
                raise ValueError("catalog resource binding resource_ref is invalid")
            physical_value = ref.get("physical_identity")
            physical = None
            if isinstance(physical_value, Mapping):
                try:
                    physical = PhysicalIdentityRef(
                        str(physical_value["scheme"]),
                        str(physical_value["value"]),
                        int(physical_value["identity_version"]),
                    )
                except (KeyError, TypeError, ValueError, OverflowError) as exc:
                    raise ValueError("catalog resource binding physical identity is invalid") from exc
            resource = ResourceRef(
                str(ref["resource_id"]),
                str(ref["source_kind"]),
                str(ref["owner"]),
                physical,
                str(ref.get("current_path") or path),
                None,
                (None if ref.get("canonical_resource_id") is None else str(ref["canonical_resource_id"])),
            )
            identity_warnings = () if physical is not None else ("physical_identity_unresolved",)
    else:
        identity = file_identity_type(
            decimal_identity_value_fn(row["volume_id"]),
            decimal_identity_value_fn(row["file_id"]),
        )
        if file_identity_type.decode(file_key, encoding=file_identity_encoding) != identity:
            raise ValueError("catalog file_key disagrees with neutral identity fields")
        resource, identity_warnings = direct_resource_ref_fn(
            source_kind=source_kind,
            owner="catalog",
            source_identity=file_key,
            identity=identity,
            birthtime_ns=row["birthtime_ns"],
            path=path,
        )
    revision_payload = {
        "source_kind": source_kind,
        "file_key": file_key,
        "processing_signature": str(row["processing_signature"]),
        "size": int(str(row["size"])),
        "mtime_ns": int(str(row["mtime_ns"])),
    }
    if binding is not None:
        revision_payload["resource_binding"] = dict(binding)
    revision_fingerprint = fingerprint_text_fn(canonical_json_fn(revision_payload))
    revision_id = f"revision:catalog:{revision_fingerprint.xxh3_128}"
    generation = int(str(row["generation_id"]))
    source_status = str(row["source_status"]).casefold()
    catalog_status = str(row["catalog_status"]).casefold()
    uncertainty = str(row["uncertainty"]).casefold()
    partial = (
        source_status not in {"complete", "done"}
        or catalog_status != "classified"
        or uncertainty == "alta"
    )
    revision = revision_ref_type(
        resource.resource_id,
        revision_id,
        "document-catalog-v6",
        str(row["processing_signature"]),
        generation,
        revision_state_type.PARTIAL if partial else revision_state_type.CURRENT,
    )
    identifiers = catalog_identifiers_fn(row["standard_references_json"])
    if binding is not None and source_kind == "archive":
        member = binding.get("archive_member")
        if isinstance(member, Mapping):
            archive_identifiers = tuple(
                (name, str(member[name]))
                for name in ("container_key", "container_path", "member_chain")
                if member.get(name) is not None and str(member[name])
            )
            identifiers = tuple(dict.fromkeys((*identifiers, *archive_identifiers)))
    snippet_parts = [f"kind={row['primary_kind']}"]
    if row["primary_subtype"] is not None:
        snippet_parts.append(f"subtype={row['primary_subtype']}")
    if row["primary_project"] is not None:
        snippet_parts.append(f"project={row['primary_project']}")
    if identifiers:
        snippet_parts.append("identifiers=" + ", ".join(value for _, value in identifiers))
    snippet_parts.append(f"uncertainty={row['uncertainty']}")
    evidence = evidence_ref_type(
        f"evidence:catalog:{generation}:{source_kind}:{file_key}",
        resource.resource_id,
        revision_id,
        evidence_method_type.INFERRED,
        section_kind="catalog_classification",
        section_id=str(row["primary_kind"]),
        snippet="; ".join(snippet_parts)[:4_096],
        extractor="document-catalog",
        extractor_version="6",
        generation=generation,
        identifiers=identifiers,
    )
    confidence = float(str(row["confidence"]))
    warnings = set(identity_warnings)
    if source_status not in {"complete", "done"}:
        warnings.add(f"catalog_source_status:{source_status}")
    if catalog_status != "classified":
        warnings.add(f"catalog_status:{catalog_status}")
    if uncertainty == "alta":
        warnings.add("catalog_uncertainty:alta")
    candidate = knowledge_candidate_type(
        resource,
        revision,
        evidence,
        ranking_signal_type(
            "catalog_metadata",
            "catalog_confidence",
            confidence,
            source_rank,
            model_signature=str(row["classifier_signature"]),
            generation=generation,
        ),
        "published catalog metadata satisfied explicit filters",
        confidence=confidence,
        warnings=tuple(sorted(warnings)),
    )
    return candidate, partial


def catalog_ranking(
    paths: KnowledgeStatePaths,
    plan: KnowledgePlan,
    snapshot: KnowledgeSnapshot,
    *,
    cancellation_check: Callable[[], None] | None = None,
    owner_available_fn: _OwnerAvailable,
    ranking_execution_type: type[RankingExecution],
    lexical_owner_formats: Mapping[str, frozenset[str]],
    escape_like_fn: Callable[[str], str],
    planned_candidate_limit_fn: _PlannedCandidateLimit,
    max_candidates: int,
    cancellation_bridge_type: type[SQLiteCancellationBridge],
    document_catalog_database_fn: _DocumentCatalogDatabase,
    sqlite_cancellation_scope_fn: _SQLiteCancellationScope,
    sqlite_error_type: type[Exception],
    reraise_captured_cancellation_fn: _ReraiseCapturedCancellation,
    cleanup_preserving_primary_fn: _CleanupPreservingPrimary,
    decimal_identity_value_fn: _DecimalIdentityValue,
    file_identity_type: type[FileIdentity],
    file_identity_encoding: FileIdentityEncoding,
    file_identity_error_type: type[Exception],
    value_error_type: type[Exception],
    direct_resource_ref_fn: _DirectResourceRef,
    canonical_json_fn: _CanonicalJson,
    fingerprint_text_fn: _FingerprintText,
    revision_ref_type: type[RevisionRef],
    revision_state_type: type[RevisionState],
    catalog_identifiers_fn: _CatalogIdentifiers,
    json_loads_fn: _JsonLoads,
    evidence_ref_type: type[EvidenceRef],
    evidence_method_type: type[EvidenceMethod],
    knowledge_candidate_type: type[KnowledgeCandidate],
    ranking_signal_type: type[RankingSignal],
) -> tuple[tuple[KnowledgeCandidate, ...], RankingExecution]:
    """Execute the catalog channel against the exact snapshot generations."""

    if not owner_available_fn(snapshot, "catalog"):
        return (), _ranking_report(
            ranking_execution_type,
            executed=False,
            available=False,
            complete=True,
            reason="catalog_owner_unavailable",
        )
    heads = _snapshot_heads(snapshot)
    if not heads:
        return (), _ranking_report(
            ranking_execution_type,
            executed=False,
            available=False,
            complete=False,
            reason="catalog_has_no_publication_heads",
        )

    target_limit = planned_candidate_limit_fn(plan, "catalog")
    cancellation = cancellation_bridge_type(cancellation_check)
    try:
        materialized_rows, head_reason = _read_catalog_rows(
            paths,
            plan,
            heads,
            cancellation,
            lexical_owner_formats=lexical_owner_formats,
            escape_like_fn=escape_like_fn,
            target_limit=target_limit,
            max_candidates=max_candidates,
            document_catalog_database_fn=document_catalog_database_fn,
            sqlite_cancellation_scope_fn=sqlite_cancellation_scope_fn,
            cleanup_preserving_primary_fn=cleanup_preserving_primary_fn,
        )
    except (RuntimeError, sqlite_error_type) as exc:
        reraise_captured_cancellation_fn(cancellation, exc)
        return (), _ranking_report(
            ranking_execution_type,
            executed=True,
            available=True,
            complete=False,
            reason=f"owner_read_failed:{type(exc).__name__}",
        )

    candidate_window_reached = len(materialized_rows) > target_limit or (
        target_limit == max_candidates and len(materialized_rows) >= target_limit
    )
    rows = materialized_rows[:target_limit]
    candidates: list[KnowledgeCandidate] = []
    invalid_rows = 0
    invalid_identifier_rows = 0
    partial_rows = 0
    materialization_errors = (
        file_identity_error_type,
        value_error_type,
        TypeError,
        OverflowError,
        KeyError,
    )
    for source_rank, row in enumerate(rows, 1):
        identifier_valid = _valid_identifier_metadata(
            _row_value(row, "standard_references_json"),
            json_loads_fn,
        )
        try:
            candidate, partial = _materialize_candidate(
                row,
                source_rank,
                decimal_identity_value_fn=decimal_identity_value_fn,
                file_identity_type=file_identity_type,
                file_identity_encoding=file_identity_encoding,
                direct_resource_ref_fn=direct_resource_ref_fn,
                canonical_json_fn=canonical_json_fn,
                fingerprint_text_fn=fingerprint_text_fn,
                revision_ref_type=revision_ref_type,
                revision_state_type=revision_state_type,
                catalog_identifiers_fn=catalog_identifiers_fn,
                evidence_ref_type=evidence_ref_type,
                evidence_method_type=evidence_method_type,
                knowledge_candidate_type=knowledge_candidate_type,
                ranking_signal_type=ranking_signal_type,
            )
        except materialization_errors:
            invalid_rows += 1
            continue
        candidates.append(candidate)
        invalid_identifier_rows += not identifier_valid
        partial_rows += partial

    if head_reason is not None:
        reason = head_reason
    elif invalid_rows:
        reason = "catalog_identity_or_provenance_invalid"
    elif invalid_identifier_rows:
        reason = "catalog_identifier_json_invalid"
    elif partial_rows:
        reason = "catalog_partial_or_review"
    elif plan.date_from or plan.date_to:
        reason = "catalog_content_date_filter_unsupported"
    else:
        reason = None
    return tuple(candidates), _ranking_report(
        ranking_execution_type,
        executed=True,
        available=True,
        complete=reason is None,
        returned=len(candidates),
        rows_scanned=len(materialized_rows),
        reason=reason,
        result_window_full=candidate_window_reached,
    )


__all__ = ["catalog_identifiers", "catalog_ranking", "escape_like"]
# endregion [02]
