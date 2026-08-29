"""Code-owner retrieval support for the Knowledge Search facade.

Facade seams and mutable runtime collaborators are injected on every call.  The
module deliberately has no dependency on :mod:`knowledge_search`, so either
side can be imported first without creating a cycle.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .code_contracts import CodeSearchHit, CodeSearchQuery, CodeSearchRelation
from neocortex.foundation.file_identity import FileIdentity
from .knowledge_contracts import (
    EvidenceMethod,
    EvidenceRef,
    KnowledgeSnapshot,
    RankingSignal,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from .knowledge_planner import KnowledgePlan
from .knowledge_search_contracts import KnowledgeCandidate, RankingExecution
from .knowledge_snapshot import KnowledgeStatePaths
from .semantic_models import ContentFingerprint
from .sqlite_cancellation import SQLiteCancellationBridge


_CleanupPreservingPrimary = Callable[..., None]
_CanonicalJson = Callable[[Mapping[str, object]], str]
_FingerprintText = Callable[[str], ContentFingerprint]
_DirectResourceRef = Callable[..., tuple[ResourceRef, tuple[str, ...]]]
_CodeVersionMetadata = Callable[..., dict[int, sqlite3.Row]]
_CodeResourceRevision = Callable[
    ...,
    tuple[ResourceRef, RevisionRef, tuple[str, ...]],
]
_BoundedRelationValue = Callable[[str, str, set[str]], str]
_CodeRelationCandidate = Callable[..., tuple[KnowledgeCandidate | None, bool]]
_SearchCode = Callable[..., tuple[CodeSearchHit, ...]]
_ReraiseCapturedCancellation = Callable[
    [SQLiteCancellationBridge, BaseException],
    None,
]


@dataclass(frozen=True, slots=True)
class _RankingDependencies:
    owner_available: Callable[[KnowledgeSnapshot, str], bool]
    planned_candidate_limit: Callable[[KnowledgePlan, str], int]
    max_candidates: int
    max_relation_candidates: int
    query_cues: frozenset[str]
    cancellation_bridge_type: type[SQLiteCancellationBridge]
    search_code_fn: _SearchCode
    code_search_query_type: type[CodeSearchQuery]
    code_version_metadata_fn: _CodeVersionMetadata
    code_resource_revision_fn: _CodeResourceRevision
    code_relation_candidate_fn: _CodeRelationCandidate
    sqlite_error_type: type[sqlite3.Error]
    reraise_captured_cancellation: _ReraiseCapturedCancellation
    file_identity_error_type: type[Exception]
    evidence_method_type: type[EvidenceMethod]
    evidence_ref_type: type[EvidenceRef]
    fingerprint_text_fn: _FingerprintText
    knowledge_candidate_type: type[KnowledgeCandidate]
    ranking_signal_type: type[RankingSignal]
    ranking_execution_type: type[RankingExecution]


@dataclass(frozen=True, slots=True)
class _MaterializedCodeRanking:
    target_limit: int
    materialized_hit_count: int
    ranked_hits: tuple[tuple[int, CodeSearchHit], ...]
    relation_entries: tuple[tuple[int, CodeSearchHit, CodeSearchRelation], ...]
    relation_candidate_window_reached: bool
    relation_limit_reached: bool
    metadata: Mapping[int, sqlite3.Row]


def code_version_metadata(
    path: Path,
    version_ids: Sequence[int],
    *,
    cancellation_check: Callable[[], None] | None = None,
    connect_code_state_fn: Callable[..., Any],
    cancellation_bridge_type: type[SQLiteCancellationBridge],
    sqlite_cancellation_scope_fn: Callable[..., Any],
    cleanup_preserving_primary_fn: _CleanupPreservingPrimary,
    sqlite_batch_size: int,
) -> dict[int, sqlite3.Row]:
    """Resolve current physical identity and producer metadata in batches."""

    result: dict[int, sqlite3.Row] = {}
    cancellation = cancellation_bridge_type(cancellation_check)
    connection = connect_code_state_fn(path, readonly=True)
    primary_error: BaseException | None = None
    try:
        with sqlite_cancellation_scope_fn(connection, cancellation):
            for offset in range(0, len(version_ids), sqlite_batch_size):
                cancellation.checkpoint()
                batch = tuple(dict.fromkeys(version_ids[offset : offset + sqlite_batch_size]))
                if not batch:
                    continue
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"""SELECT v.version_id,f.volume_id,f.physical_file_id,v.size,
                    v.mtime_ns,v.birthtime_ns,v.raw_xxh3_128,v.processing_signature,v.analyzer_id,
                    v.analyzer_version,v.analysis_status,v.first_observed_run_id,
                    v.last_observed_run_id
                    FROM file_versions v JOIN files f ON f.file_id=v.file_id
                    WHERE v.version_id IN ({placeholders})
                    AND f.current_version_id=v.version_id AND f.status='current'
                    AND v.invalidated_ns IS NULL""",
                    batch,
                ).fetchall()
                result.update((int(row["version_id"]), row) for row in rows)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if primary_error is None:
            connection.close()
        else:
            cleanup_preserving_primary_fn(
                connection.close,
                primary_error,
                label="code metadata connection close cleanup",
            )
    return result


def code_resource_revision(
    row: sqlite3.Row,
    *,
    path: str,
    file_identity_type: type[FileIdentity],
    direct_resource_ref_fn: _DirectResourceRef,
    canonical_json_fn: _CanonicalJson,
    fingerprint_text_fn: _FingerprintText,
    revision_ref_type: type[RevisionRef],
    revision_state_type: type[RevisionState],
) -> tuple[ResourceRef, RevisionRef, tuple[str, ...]]:
    """Build neutral identity and revision contracts for a current code row."""

    source_identity = f"{row['volume_id']}:{row['physical_file_id']}"
    identity = file_identity_type(
        int(str(row["volume_id"]), 16),
        int(str(row["physical_file_id"]), 16),
    )
    resource, identity_warnings = direct_resource_ref_fn(
        source_kind="code",
        owner="code",
        source_identity=source_identity,
        identity=identity,
        birthtime_ns=row["birthtime_ns"],
        path=path,
    )
    source_revision = {
        "version_id": int(row["version_id"]),
        "size": int(row["size"]),
        "mtime_ns": int(row["mtime_ns"]),
        "birthtime_ns": int(row["birthtime_ns"]),
        "raw_content_xxh3_128": row["raw_xxh3_128"],
    }
    revision_fingerprint = fingerprint_text_fn(
        canonical_json_fn(
            {
                "source_kind": "code",
                "source_identity": source_identity,
                "source_revision": source_revision,
            }
        )
    )
    revision = revision_ref_type(
        resource.resource_id,
        f"revision:code:{revision_fingerprint.xxh3_128}",
        f"{row['analyzer_id']}:{row['analyzer_version']}",
        str(row["processing_signature"]),
        None,
        (
            revision_state_type.CURRENT
            if row["analysis_status"] in {"complete", "text_only"}
            else revision_state_type.PARTIAL
        ),
    )
    return resource, revision, identity_warnings


def bounded_code_relation_value(
    namespace: str,
    value: str,
    warnings: set[str],
    *,
    max_identifier_chars: int,
    fingerprint_text_fn: _FingerprintText,
) -> str:
    """Keep evidence identifiers within their public character contract."""

    if len(value) <= max_identifier_chars:
        return value
    fingerprint = fingerprint_text_fn(value)
    warnings.add(f"{namespace}_fingerprinted_due_to_contract_limit")
    return f"xxh3-v1:{fingerprint.xxh3_128}:{fingerprint.byte_count}:{fingerprint.xxh3_64_guard}"


def _append_relation_identifiers(
    identifiers: list[tuple[str, str]],
    warnings: set[str],
    values: Sequence[tuple[str, object | None]],
    *,
    bounded_value_fn: _BoundedRelationValue,
) -> None:
    for namespace, value in values:
        if value is None:
            continue
        identifiers.append(
            (
                namespace,
                bounded_value_fn(namespace, str(value), warnings),
            )
        )


def _relation_source_contract(
    metadata: Mapping[int, sqlite3.Row],
    relation: CodeSearchRelation,
    *,
    code_resource_revision_fn: _CodeResourceRevision,
    file_identity_error_type: type[Exception],
) -> tuple[sqlite3.Row, ResourceRef, RevisionRef, tuple[str, ...]] | None:
    source_row = metadata.get(relation.source.version_id)
    if source_row is None:
        return None
    try:
        source_resource, source_revision, warnings = code_resource_revision_fn(
            source_row,
            path=relation.source.path,
        )
    except (file_identity_error_type, ValueError):
        return None
    return source_row, source_resource, source_revision, warnings


def _relation_target_contract(
    metadata: Mapping[int, sqlite3.Row],
    relation: CodeSearchRelation,
    *,
    code_resource_revision_fn: _CodeResourceRevision,
    file_identity_error_type: type[Exception],
) -> tuple[ResourceRef | None, tuple[str, ...]]:
    if relation.target is None:
        return None, ()
    target_row = metadata.get(relation.target.version_id)
    if target_row is None:
        return None, ("code_relation_target_changed_after_owner_read",)
    try:
        target_resource, _, identity_warnings = code_resource_revision_fn(
            target_row,
            path=relation.target.path,
        )
    except (file_identity_error_type, ValueError):
        return None, ("code_relation_target_changed_after_owner_read",)
    return target_resource, identity_warnings


def _relation_resolution(
    relation: CodeSearchRelation,
    target_resource: ResourceRef | None,
    source_warnings: tuple[str, ...],
    target_warnings: tuple[str, ...],
) -> tuple[bool, set[str]]:
    warnings = {*source_warnings, *target_warnings}
    resolved = relation.resolved and target_resource is not None
    if not resolved:
        warnings.add("code_relation_unresolved")
    if not relation.confirmed:
        warnings.add("code_relation_unconfirmed")
    return resolved, warnings


def _relation_identifiers(
    relation: CodeSearchRelation,
    source_resource: ResourceRef,
    target_resource: ResourceRef | None,
    *,
    resolved: bool,
    warnings: set[str],
    bounded_relation_value_fn: _BoundedRelationValue,
) -> tuple[tuple[str, str], ...]:
    identifiers: list[tuple[str, str]] = []
    _append_relation_identifiers(
        identifiers,
        warnings,
        (
            ("code_relation_id", f"{relation.source_table}:{relation.source_row_id}"),
            ("code_relation_family", relation.family),
            ("code_relation_kind", relation.kind),
            ("code_relation_name", relation.name),
            ("code_relation_source_resource", source_resource.resource_id),
            ("code_relation_source_version_id", relation.source.version_id),
            ("code_relation_source_symbol", relation.source.symbol),
        ),
        bounded_value_fn=bounded_relation_value_fn,
    )
    if resolved:
        assert target_resource is not None
        assert relation.target is not None
        _append_relation_identifiers(
            identifiers,
            warnings,
            (
                ("code_relation_target_resource", target_resource.resource_id),
                ("code_relation_target_version_id", relation.target.version_id),
                ("code_relation_target_symbol", relation.target.symbol),
            ),
            bounded_value_fn=bounded_relation_value_fn,
        )
    _append_relation_identifiers(
        identifiers,
        warnings,
        (
            ("code_relation_target_hint", relation.target_hint),
            ("code_relation_resolved", str(resolved).lower()),
            ("code_relation_confirmed", str(relation.confirmed).lower()),
            ("code_relation_confidence", format(relation.confidence, ".17g")),
            ("code_relation_provenance", relation.provenance),
            ("code_relation_scope", relation.scope),
            ("code_relation_version_spec", relation.version_spec),
        ),
        bounded_value_fn=bounded_relation_value_fn,
    )
    return tuple(identifiers)


def _relation_identity_payload(
    relation: CodeSearchRelation,
    source_resource: ResourceRef,
    source_revision: RevisionRef,
    target_resource: ResourceRef | None,
) -> dict[str, object]:
    return {
        "family": relation.family,
        "kind": relation.kind,
        "name": relation.name,
        "source_resource_id": source_resource.resource_id,
        "source_revision_id": source_revision.revision_id,
        "source_symbol": relation.source.symbol,
        "target_resource_id": (None if target_resource is None else target_resource.resource_id),
        "target_symbol": (None if relation.target is None else relation.target.symbol),
        "target_hint": relation.target_hint,
        "confirmed": relation.confirmed,
        "confidence": relation.confidence,
        "provenance": relation.provenance,
        "scope": relation.scope,
        "version_spec": relation.version_spec,
    }


def _relation_evidence(
    relation: CodeSearchRelation,
    source_resource: ResourceRef,
    source_revision: RevisionRef,
    target_resource: ResourceRef | None,
    identifiers: tuple[tuple[str, str], ...],
    *,
    resolved: bool,
    canonical_json_fn: _CanonicalJson,
    fingerprint_text_fn: _FingerprintText,
    evidence_method_type: type[EvidenceMethod],
    evidence_ref_type: type[EvidenceRef],
) -> EvidenceRef:
    identity_payload = _relation_identity_payload(
        relation,
        source_resource,
        source_revision,
        target_resource,
    )
    fingerprint = fingerprint_text_fn(canonical_json_fn(identity_payload))
    return evidence_ref_type(
        evidence_id=(
            "evidence:code-relation:v1:"
            f"{fingerprint.xxh3_128}:"
            f"{fingerprint.byte_count}:"
            f"{fingerprint.xxh3_64_guard}"
        ),
        resource_id=source_resource.resource_id,
        revision_id=source_revision.revision_id,
        method=(
            evidence_method_type.AMBIGUOUS
            if not resolved
            else (
                evidence_method_type.STRUCTURAL
                if relation.confirmed
                else evidence_method_type.INFERRED
            )
        ),
        symbol=relation.source.symbol,
        section_kind="code_relation",
        section_id=f"{relation.source_table}:{relation.source_row_id}",
        extractor="code-relations",
        extractor_version="1",
        identifiers=identifiers,
    )


def code_relation_candidate(
    metadata: Mapping[int, sqlite3.Row],
    *,
    source_rank: int,
    hit: CodeSearchHit,
    relation: CodeSearchRelation,
    code_resource_revision_fn: _CodeResourceRevision,
    bounded_relation_value_fn: _BoundedRelationValue,
    file_identity_error_type: type[Exception],
    canonical_json_fn: _CanonicalJson,
    fingerprint_text_fn: _FingerprintText,
    evidence_method_type: type[EvidenceMethod],
    evidence_ref_type: type[EvidenceRef],
    knowledge_candidate_type: type[KnowledgeCandidate],
    ranking_signal_type: type[RankingSignal],
    revision_state_type: type[RevisionState],
) -> tuple[KnowledgeCandidate | None, bool]:
    """Materialize one owner relation without inventing a target endpoint."""

    source_contract = _relation_source_contract(
        metadata,
        relation,
        code_resource_revision_fn=code_resource_revision_fn,
        file_identity_error_type=file_identity_error_type,
    )
    if source_contract is None:
        return None, True
    source_row, source_resource, source_revision, source_warnings = source_contract
    target_resource, target_warnings = _relation_target_contract(
        metadata,
        relation,
        code_resource_revision_fn=code_resource_revision_fn,
        file_identity_error_type=file_identity_error_type,
    )
    resolved, warnings = _relation_resolution(
        relation,
        target_resource,
        source_warnings,
        target_warnings,
    )
    identifiers = _relation_identifiers(
        relation,
        source_resource,
        target_resource,
        resolved=resolved,
        warnings=warnings,
        bounded_relation_value_fn=bounded_relation_value_fn,
    )
    evidence = _relation_evidence(
        relation,
        source_resource,
        source_revision,
        target_resource,
        identifiers,
        resolved=resolved,
        canonical_json_fn=canonical_json_fn,
        fingerprint_text_fn=fingerprint_text_fn,
        evidence_method_type=evidence_method_type,
        evidence_ref_type=evidence_ref_type,
    )
    candidate = knowledge_candidate_type(
        source_resource,
        source_revision,
        evidence,
        ranking_signal_type(
            "code_structural",
            "code_rrf",
            hit.score,
            source_rank,
            model_signature=str(source_row["processing_signature"]),
        ),
        (
            "current structured code relation resolved both endpoints"
            if resolved
            else "structured code relation matched but its target is unresolved"
        ),
        confidence=relation.confidence,
        warnings=tuple(sorted(warnings)),
    )
    return candidate, (
        not resolved
        or not relation.confirmed
        or source_revision.state is revision_state_type.PARTIAL
    )


def _ranking_report(
    dependencies: _RankingDependencies,
    *,
    executed: bool,
    available: bool,
    complete: bool,
    returned: int = 0,
    rows_scanned: int = 0,
    reason: str | None = None,
    result_window_full: bool = False,
) -> RankingExecution:
    return dependencies.ranking_execution_type(
        "code_structural",
        "structural_code",
        executed,
        available,
        complete,
        returned,
        rows_scanned=rows_scanned,
        reason=reason,
        result_window_full=result_window_full,
    )


def _early_ranking_report(
    dependencies: _RankingDependencies,
    plan: KnowledgePlan,
    snapshot: KnowledgeSnapshot,
) -> RankingExecution | None:
    if not dependencies.owner_available(snapshot, "code"):
        return _ranking_report(
            dependencies,
            executed=False,
            available=False,
            complete=True,
            reason="code_owner_unavailable",
        )
    if plan.source_kinds and "code" not in plan.source_kinds:
        return _ranking_report(
            dependencies,
            executed=True,
            available=True,
            complete=True,
            reason="source_filter_excludes_code",
        )
    return None


def _code_query_text(plan: KnowledgePlan, query_cues: frozenset[str]) -> str:
    code_terms = tuple(
        token
        for token in plan.normalized_query.split()
        if token.casefold().strip(".,:;!?¿¡") not in query_cues
    )
    return " ".join(code_terms) or plan.normalized_query


def _collect_relation_entries(
    ranked_hits: tuple[tuple[int, CodeSearchHit], ...],
    *,
    processing_limit: int,
    max_relation_candidates: int,
) -> tuple[
    tuple[tuple[int, CodeSearchHit, CodeSearchRelation], ...],
    bool,
    bool,
]:
    entries: list[tuple[int, CodeSearchHit, CodeSearchRelation]] = []
    for source_rank, hit in ranked_hits:
        for relation in hit.relations:
            if len(entries) >= processing_limit:
                relation_limit_reached = processing_limit >= max_relation_candidates
                return (
                    tuple(entries),
                    not relation_limit_reached,
                    relation_limit_reached,
                )
            entries.append((source_rank, hit, relation))
    return tuple(entries), False, False


def _metadata_version_ids(
    ranked_hits: tuple[tuple[int, CodeSearchHit], ...],
    relation_entries: tuple[tuple[int, CodeSearchHit, CodeSearchRelation], ...],
) -> tuple[int, ...]:
    version_ids = [hit.version_id for _, hit in ranked_hits]
    for _, _, relation in relation_entries:
        version_ids.append(relation.source.version_id)
        if relation.target is not None:
            version_ids.append(relation.target.version_id)
    return tuple(version_ids)


def _read_code_owner(
    paths: KnowledgeStatePaths,
    plan: KnowledgePlan,
    cancellation: SQLiteCancellationBridge,
    dependencies: _RankingDependencies,
) -> _MaterializedCodeRanking:
    target_limit = dependencies.planned_candidate_limit(plan, "structural_code")
    requested_limit = min(dependencies.max_candidates, target_limit + 1)
    cancellation_callback = cancellation.checkpoint if cancellation.enabled else None
    materialized_hits = dependencies.search_code_fn(
        paths.code,
        dependencies.code_search_query_type(
            text=_code_query_text(plan, dependencies.query_cues),
            modes=(
                "literal",
                "fts",
                "symbol",
                "definition",
                "reference",
                "import",
                "dependency",
                "call",
                "signature",
                "diagnostic",
            ),
            project=None,
            limit=requested_limit,
        ),
        cancellation_check=cancellation_callback,
    )
    ranked_hits = tuple(
        (source_rank, hit)
        for source_rank, hit in enumerate(materialized_hits, 1)
        if plan.project is None
        or (hit.project is not None and hit.project.casefold() == plan.project.casefold())
    )[:target_limit]
    processing_limit = min(
        dependencies.max_relation_candidates,
        max(0, requested_limit - len(materialized_hits)),
    )
    relation_entries, candidate_window_reached, relation_limit_reached = _collect_relation_entries(
        ranked_hits,
        processing_limit=processing_limit,
        max_relation_candidates=dependencies.max_relation_candidates,
    )
    metadata = dependencies.code_version_metadata_fn(
        paths.code,
        _metadata_version_ids(ranked_hits, relation_entries),
        cancellation_check=cancellation_callback,
    )
    return _MaterializedCodeRanking(
        target_limit,
        len(materialized_hits),
        ranked_hits,
        relation_entries,
        candidate_window_reached,
        relation_limit_reached,
        metadata,
    )


def _direct_candidates(
    materialized: _MaterializedCodeRanking,
    dependencies: _RankingDependencies,
) -> tuple[list[KnowledgeCandidate], int]:
    candidates: list[KnowledgeCandidate] = []
    invalid_rows = 0
    for source_rank, hit in materialized.ranked_hits:
        row = materialized.metadata.get(hit.version_id)
        if row is None:
            invalid_rows += 1
            continue
        try:
            resource, revision, identity_warnings = dependencies.code_resource_revision_fn(
                row, path=hit.path
            )
        except (dependencies.file_identity_error_type, ValueError):
            invalid_rows += 1
            continue
        symbol_component = hit.symbol or "file"
        evidence_id = (
            f"evidence:code:{hit.version_id}:{hit.start_line}:"
            f"{hit.end_line}:"
            f"{dependencies.fingerprint_text_fn(symbol_component).xxh3_128}"
        )
        identifiers = [("code_version_id", str(hit.version_id))]
        if hit.symbol is not None:
            identifiers.append(("symbol", hit.symbol))
        evidence = dependencies.evidence_ref_type(
            evidence_id,
            resource.resource_id,
            revision.revision_id,
            dependencies.evidence_method_type.STRUCTURAL,
            start_line=hit.start_line,
            end_line=hit.end_line,
            symbol=hit.symbol,
            section_kind="code_search_hit",
            section_id="|".join(hit.match_types),
            snippet=hit.snippet,
            extractor=str(row["analyzer_id"]),
            extractor_version=str(row["analyzer_version"]),
            generation=None,
            identifiers=tuple(identifiers),
        )
        candidates.append(
            dependencies.knowledge_candidate_type(
                resource,
                revision,
                evidence,
                dependencies.ranking_signal_type(
                    "code_structural",
                    "code_rrf",
                    hit.score,
                    source_rank,
                    model_signature=str(row["processing_signature"]),
                    generation=None,
                ),
                "current structured code evidence matched the query",
                warnings=tuple(
                    sorted(
                        {
                            *identity_warnings,
                            "owner_api_does_not_expose_prefusion_rows_scanned",
                        }
                    )
                ),
            )
        )
    return candidates, invalid_rows


def _relation_candidates(
    materialized: _MaterializedCodeRanking,
    dependencies: _RankingDependencies,
) -> tuple[list[KnowledgeCandidate], int, int]:
    candidates: list[KnowledgeCandidate] = []
    invalid_rows = 0
    incomplete_relations = 0
    for source_rank, hit, relation in materialized.relation_entries:
        candidate, incomplete = dependencies.code_relation_candidate_fn(
            materialized.metadata,
            source_rank=source_rank,
            hit=hit,
            relation=relation,
        )
        if candidate is None:
            invalid_rows += 1
            continue
        candidates.append(candidate)
        if incomplete:
            incomplete_relations += 1
    return candidates, invalid_rows, incomplete_relations


def _ranking_reason(
    *,
    invalid_rows: int,
    relation_limit_reached: bool,
    incomplete_relations: int,
) -> str | None:
    if invalid_rows:
        return "code_identity_invalid_or_stale"
    if relation_limit_reached:
        return "code_relation_limit_reached"
    if incomplete_relations:
        return "code_relation_unresolved_or_unconfirmed"
    return None


def _finish_code_ranking(
    materialized: _MaterializedCodeRanking,
    dependencies: _RankingDependencies,
    direct_candidates: list[KnowledgeCandidate],
    relation_candidates: list[KnowledgeCandidate],
    *,
    invalid_rows: int,
    incomplete_relations: int,
) -> tuple[tuple[KnowledgeCandidate, ...], RankingExecution]:
    candidates = [*direct_candidates, *relation_candidates]
    candidate_window_reached = (
        materialized.materialized_hit_count > materialized.target_limit
        or (
            materialized.target_limit == dependencies.max_candidates
            and materialized.materialized_hit_count >= materialized.target_limit
        )
        or materialized.relation_candidate_window_reached
        or len(candidates) > materialized.target_limit
    )
    visible_candidates = tuple(candidates[: materialized.target_limit])
    # A candidate window filled to the requested top-k is successful bounded
    # retrieval.  It is separate from hard relation-budget exhaustion.
    reason = _ranking_reason(
        invalid_rows=invalid_rows,
        relation_limit_reached=materialized.relation_limit_reached,
        incomplete_relations=incomplete_relations,
    )
    report = _ranking_report(
        dependencies,
        executed=True,
        available=True,
        complete=reason is None,
        returned=len(visible_candidates),
        rows_scanned=(materialized.materialized_hit_count + len(materialized.relation_entries)),
        reason=reason,
        result_window_full=candidate_window_reached,
    )
    return visible_candidates, report


def code_ranking(
    paths: KnowledgeStatePaths,
    plan: KnowledgePlan,
    snapshot: KnowledgeSnapshot,
    *,
    cancellation_check: Callable[[], None] | None = None,
    owner_available_fn: Callable[[KnowledgeSnapshot, str], bool],
    planned_candidate_limit_fn: Callable[[KnowledgePlan, str], int],
    max_candidates: int,
    max_relation_candidates: int,
    code_query_cues: frozenset[str],
    cancellation_bridge_type: type[SQLiteCancellationBridge],
    search_code_fn: _SearchCode,
    code_search_query_type: type[CodeSearchQuery],
    code_version_metadata_fn: _CodeVersionMetadata,
    code_resource_revision_fn: _CodeResourceRevision,
    code_relation_candidate_fn: _CodeRelationCandidate,
    sqlite_error_type: type[sqlite3.Error],
    reraise_captured_cancellation_fn: _ReraiseCapturedCancellation,
    file_identity_error_type: type[Exception],
    evidence_method_type: type[EvidenceMethod],
    evidence_ref_type: type[EvidenceRef],
    fingerprint_text_fn: _FingerprintText,
    knowledge_candidate_type: type[KnowledgeCandidate],
    ranking_signal_type: type[RankingSignal],
    ranking_execution_type: type[RankingExecution],
) -> tuple[tuple[KnowledgeCandidate, ...], RankingExecution]:
    """Execute the planned structural code channel with bounded materialization."""

    dependencies = _RankingDependencies(
        owner_available_fn,
        planned_candidate_limit_fn,
        max_candidates,
        max_relation_candidates,
        code_query_cues,
        cancellation_bridge_type,
        search_code_fn,
        code_search_query_type,
        code_version_metadata_fn,
        code_resource_revision_fn,
        code_relation_candidate_fn,
        sqlite_error_type,
        reraise_captured_cancellation_fn,
        file_identity_error_type,
        evidence_method_type,
        evidence_ref_type,
        fingerprint_text_fn,
        knowledge_candidate_type,
        ranking_signal_type,
        ranking_execution_type,
    )
    early_report = _early_ranking_report(dependencies, plan, snapshot)
    if early_report is not None:
        return (), early_report
    cancellation = cancellation_bridge_type(cancellation_check)
    try:
        materialized = _read_code_owner(paths, plan, cancellation, dependencies)
    except (RuntimeError, sqlite_error_type) as exc:
        reraise_captured_cancellation_fn(cancellation, exc)
        return (), _ranking_report(
            dependencies,
            executed=True,
            available=True,
            complete=False,
            reason=f"owner_read_failed:{type(exc).__name__}",
        )
    direct, direct_invalid = _direct_candidates(materialized, dependencies)
    relations, relation_invalid, incomplete_relations = _relation_candidates(
        materialized,
        dependencies,
    )
    return _finish_code_ranking(
        materialized,
        dependencies,
        direct,
        relations,
        invalid_rows=direct_invalid + relation_invalid,
        incomplete_relations=incomplete_relations,
    )


__all__ = [
    "bounded_code_relation_value",
    "code_ranking",
    "code_relation_candidate",
    "code_resource_revision",
    "code_version_metadata",
]
