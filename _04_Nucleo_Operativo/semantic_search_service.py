"""Separate-space semantic retrieval and deterministic rank-only fusion."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol

from .semantic_backends import EmbeddingBackend, reciprocal_rank_fusion
from .semantic_config import (
    SEMANTIC_PIPELINE_VERSION,
    TEXT_MODEL_SIGNATURE,
    TEXT_RETRIEVAL_CALIBRATION_BACKEND,
    TEXT_RETRIEVAL_CALIBRATION_SIGNATURE,
    TEXT_RETRIEVAL_SCORE_FLOORS,
    clip_image_model,
    clip_text_model,
    multilingual_text_model,
    text_retrieval_score_floor,
)
from .semantic_generation_worker import batches
from .semantic_lexical import MAX_QUERY_CHARS, LexicalRanking, LexicalStatePaths
from .semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRequest,
    EmbeddingRole,
    ExactSearchQuery,
    ResolvedSearchHit,
    SearchHit,
    fingerprint_text,
)
from .semantic_ontology import expand_domain_query
from .semantic_preparation import (
    BackendFactory,
    SemanticModelUnavailableError,
    model_cache,
)
from .semantic_service_contracts import (
    MAX_LEXICAL_CANDIDATE_HITS,
    MAX_SEMANTIC_CANDIDATE_HITS,
    SEARCH_RESOLUTION_BATCH_SIZE,
    SEMANTIC_DATABASE_NAME,
    FusedResolvedHit,
    SemanticRanking,
    SemanticSearchResult,
)
from .semantic_sources import SEMANTIC_TITLE_POLICY, SEMANTIC_TITLE_SECTION_KIND
from .semantic_state import (
    has_active_embeddings,
    load_embedding_model,
    resolve_search_hits,
    search_exact_evidence_page,
    search_exact_page,
)


class LexicalSearch(Protocol):
    def __call__(
        self,
        paths: LexicalStatePaths,
        query: str,
        *,
        limit: int,
        cancellation_check: Callable[[], None] | None = None,
    ) -> tuple[LexicalRanking, ...]: ...


SEMANTIC_TEXT_RANKING = "semantic_text"
SEMANTIC_TITLE_RANKING = "semantic_title"
SEMANTIC_TITLE_FUSION_WEIGHT = 0.5


@dataclass(frozen=True, slots=True)
class _SemanticSearchContext:
    state_directory: Path
    query: str
    limit: int
    semantic_candidate_limit: int
    lexical_candidate_limit: int
    max_vectors: int
    database: Path
    database_exists: bool
    cache: Path


# region [01] Query vectors and exact rankings


def _query_vector_from_backend(
    model: EmbeddingModelSpec,
    query: str,
    embedding_backend: EmbeddingBackend,
    *,
    cancellation_check: Callable[[], None] | None = None,
) -> tuple[float, ...]:
    if cancellation_check is not None:
        cancellation_check()
    request = EmbeddingRequest(
        request_id="semantic-query",
        role=EmbeddingRole.QUERY,
        fingerprint=fingerprint_text(query),
        text=query,
    )
    vector = tuple(embedding_backend.embed((request,))[0].vector)
    if cancellation_check is not None:
        cancellation_check()
    return vector


def query_vector(
    model: EmbeddingModelSpec,
    query: str,
    *,
    cache_dir: Path,
    local_files_only: bool,
    threads: int | None,
    backend_factory: BackendFactory,
    cancellation_check: Callable[[], None] | None = None,
) -> tuple[float, ...]:
    embedding_backend = backend_factory(
        model,
        cache_dir=cache_dir,
        local_files_only=local_files_only,
        threads=threads,
    )
    return _query_vector_from_backend(
        model,
        query,
        embedding_backend,
        cancellation_check=cancellation_check,
    )


def semantic_ranking(
    database: Path,
    *,
    name: str,
    query_model: EmbeddingModelSpec,
    target_modality: EmbeddingModality,
    vector: Sequence[float],
    indexed_model_signatures: tuple[str, ...],
    limit: int,
    max_vectors: int,
    evidence_mode: bool = False,
    text_scope: Literal["all", "content", "title"] = "all",
    fusion_weight: float = 1.0,
    provenance: Mapping[str, object] | None = None,
    cancellation_check: Callable[[], None] | None = None,
) -> SemanticRanking:
    search_page = search_exact_evidence_page if evidence_mode else search_exact_page
    page = search_page(
        database,
        ExactSearchQuery(
            query_model_signature=query_model.model_signature,
            vector_space=query_model.vector_space,
            dimensions=query_model.dimensions,
            vector=vector,
            target_modality=target_modality,
            indexed_model_signatures=indexed_model_signatures,
        ),
        limit=limit,
        max_vectors=max_vectors,
        text_scope=text_scope,
        cancellation_check=cancellation_check,
    )
    resolved_values: list[ResolvedSearchHit] = []
    for hit_batch in batches(page.hits, SEARCH_RESOLUTION_BATCH_SIZE):
        if cancellation_check is not None:
            cancellation_check()
        resolved_values.extend(resolve_search_hits(database, hit_batch))
    resolved = tuple(resolved_values)
    cutoff_reason = (
        "max_vectors_reached"
        if not page.complete
        else ("top_k" if len(page.hits) == limit and page.scanned > len(page.hits) else None)
    )
    cutoff_score = page.hits[-1].score if len(page.hits) == limit else None
    return SemanticRanking(
        name=name,
        hits=page.hits,
        resolved=resolved,
        scanned=page.scanned,
        complete=page.complete,
        cutoff_reason=cutoff_reason,
        next_cursor=page.next_cursor,
        cutoff_score=cutoff_score,
        fusion_weight=fusion_weight,
        provenance=dict(provenance or {}),
    )


def _search_hit_key(hit: SearchHit) -> tuple[int, str, str, int]:
    return hit.ref_id, hit.entity_id, hit.item_id, hit.generation_id


def _retrieval_contract_provenance(
    provenance: Mapping[str, object],
) -> tuple[object, object, bool]:
    """Resolve direct or exact-payload provenance without hiding conflicts."""

    nested_value = provenance.get("payload_provenance")
    nested = nested_value if isinstance(nested_value, Mapping) else {}

    def resolve(name: str) -> tuple[object, bool]:
        direct = provenance.get(name)
        inherited = nested.get(name)
        conflict = direct is not None and inherited is not None and direct != inherited
        return (direct if direct is not None else inherited), conflict

    backend, backend_conflict = resolve("backend")
    pipeline, pipeline_conflict = resolve("pipeline")
    return backend, pipeline, backend_conflict or pipeline_conflict


def _text_retrieval_hit_decision(
    hit: SearchHit,
    resolved: ResolvedSearchHit | None,
) -> tuple[bool, str | None]:
    """Return retention plus an abstention reason; ``None`` means calibrated."""

    if resolved is None:
        return True, "source_unresolved"
    backend, pipeline, provenance_conflict = _retrieval_contract_provenance(hit.provenance)
    if provenance_conflict:
        return True, "provenance_contract_conflict"
    floor = text_retrieval_score_floor(
        model_signature=hit.indexed_model_signature,
        pipeline=pipeline,
        backend=backend,
        source_kind=resolved.source_kind,
    )
    if floor is not None:
        return hit.score >= floor, None
    if hit.indexed_model_signature != TEXT_MODEL_SIGNATURE:
        reason = "indexed_model_not_calibrated"
    elif pipeline != SEMANTIC_PIPELINE_VERSION:
        reason = "pipeline_not_calibrated"
    elif backend != TEXT_RETRIEVAL_CALIBRATION_BACKEND:
        reason = "backend_not_calibrated"
    else:
        reason = "source_kind_not_calibrated"
    return True, reason


def _text_retrieval_calibration_status(
    *,
    calibrated_hits: int,
    uncalibrated_hits: int,
) -> str:
    if uncalibrated_hits and calibrated_hits:
        return "partial"
    if uncalibrated_hits:
        return "not_applicable"
    return "applied"


def apply_text_retrieval_calibration(
    ranking: SemanticRanking,
    *,
    selected_model: EmbeddingModelSpec,
) -> SemanticRanking:
    """Filter low-evidence neighbours under the exact mixed-source contract."""

    calibration: dict[str, object] = {
        "policy_signature": TEXT_RETRIEVAL_CALIBRATION_SIGNATURE,
        "model_signature": TEXT_MODEL_SIGNATURE,
        "pipeline": SEMANTIC_PIPELINE_VERSION,
        "backend": TEXT_RETRIEVAL_CALIBRATION_BACKEND,
        "score_floor_by_source_kind": dict(TEXT_RETRIEVAL_SCORE_FLOORS),
        "score_interpretation": "cosine_similarity_retrieval_floor_not_probability",
        "raw_hits": len(ranking.hits),
    }
    if selected_model.model_signature != TEXT_MODEL_SIGNATURE:
        calibration.update(
            {
                "status": "model_not_calibrated",
                "retained_hits": len(ranking.hits),
                "rejected_hits": 0,
                "query_abstained": False,
            }
        )
        return replace(
            ranking,
            provenance={
                **ranking.provenance,
                "retrieval_abstention": calibration,
            },
        )

    resolved_by_key = {_search_hit_key(value.hit): value for value in ranking.resolved}
    retained_keys: set[tuple[int, str, str, int]] = set()
    rejected_by_source: dict[str, int] = {}
    uncalibrated_by_reason: dict[str, int] = {}
    calibrated_hits = 0
    for hit in ranking.hits:
        key = _search_hit_key(hit)
        resolved = resolved_by_key.get(key)
        retained, uncalibrated_reason = _text_retrieval_hit_decision(hit, resolved)
        if uncalibrated_reason is not None:
            retained_keys.add(key)
            uncalibrated_by_reason[uncalibrated_reason] = (
                uncalibrated_by_reason.get(uncalibrated_reason, 0) + 1
            )
            continue
        calibrated_hits += 1
        if retained:
            retained_keys.add(key)
            continue
        assert resolved is not None
        rejected_by_source[resolved.source_kind] = (
            rejected_by_source.get(resolved.source_kind, 0) + 1
        )

    retained_hits = tuple(hit for hit in ranking.hits if _search_hit_key(hit) in retained_keys)
    retained_resolved = tuple(
        value for value in ranking.resolved if _search_hit_key(value.hit) in retained_keys
    )
    rejected_hits = len(ranking.hits) - len(retained_hits)
    uncalibrated_hits = sum(uncalibrated_by_reason.values())
    status = _text_retrieval_calibration_status(
        calibrated_hits=calibrated_hits,
        uncalibrated_hits=uncalibrated_hits,
    )
    query_abstained = (
        bool(ranking.hits) and calibrated_hits == len(ranking.hits) and not retained_hits
    )
    calibration.update(
        {
            "status": status,
            "calibrated_hits": calibrated_hits,
            "uncalibrated_hits": uncalibrated_hits,
            "uncalibrated_by_reason": uncalibrated_by_reason,
            "retained_hits": len(retained_hits),
            "rejected_hits": rejected_hits,
            "rejected_by_source_kind": rejected_by_source,
            "query_abstained": query_abstained,
            "abstention_reason": (
                "all_candidates_below_calibrated_source_floor" if query_abstained else None
            ),
        }
    )
    return replace(
        ranking,
        hits=retained_hits,
        resolved=retained_resolved,
        provenance={
            **ranking.provenance,
            "retrieval_abstention": calibration,
        },
    )


def apply_document_result_diversity(
    ranking: SemanticRanking,
    *,
    max_evidence_per_item: int,
) -> SemanticRanking:
    """Cap repeated chunks without discarding the best evidence for a document."""

    if max_evidence_per_item < 1:
        raise ValueError("max_evidence_per_item must be positive")
    retained_keys: set[tuple[int, str, str, int]] = set()
    counts: dict[str, int] = {}
    for hit in ranking.hits:
        observed = counts.get(hit.item_id, 0)
        if observed >= max_evidence_per_item:
            continue
        counts[hit.item_id] = observed + 1
        retained_keys.add(_search_hit_key(hit))
    retained_hits = tuple(hit for hit in ranking.hits if _search_hit_key(hit) in retained_keys)
    retained_resolved = tuple(
        value for value in ranking.resolved if _search_hit_key(value.hit) in retained_keys
    )
    return replace(
        ranking,
        hits=retained_hits,
        resolved=retained_resolved,
        provenance={
            **ranking.provenance,
            "document_result_diversity": {
                "policy_signature": "semantic-document-diversity-v1",
                "max_evidence_per_item": max_evidence_per_item,
                "raw_hits": len(ranking.hits),
                "retained_hits": len(retained_hits),
                "distinct_items": len(counts),
            },
        },
    )


def registered_model_available(
    database: Path,
    expected: EmbeddingModelSpec,
) -> bool:
    try:
        registered = load_embedding_model(database, expected.model_signature)
    except KeyError:
        return False
    if registered != expected:
        raise RuntimeError(f"registered model differs from current contract: {expected.model_id}")
    return True


def indexed_model_available(
    database: Path,
    expected: EmbeddingModelSpec,
) -> bool:
    return registered_model_available(database, expected) and has_active_embeddings(
        database,
        expected.model_signature,
    )


def unavailable_semantic_ranking(name: str, reason: str) -> SemanticRanking:
    return SemanticRanking(
        name=name,
        hits=(),
        resolved=(),
        scanned=0,
        complete=False,
        available=False,
        unavailable_reason=reason,
    )


# endregion [01]


# region [02] Modality-isolated rankings


def default_lexical_paths(state_directory: Path) -> LexicalStatePaths:
    return LexicalStatePaths(
        pdf=state_directory / "pdf.sqlite3",
        docx=state_directory / "docx.sqlite3",
        office=state_directory / "office.sqlite3",
        audio=state_directory / "audio.sqlite3",
        archive=state_directory / "archive.sqlite3",
        text=state_directory / "text.sqlite3",
    )


def text_search_ranking(
    database: Path,
    *,
    database_exists: bool,
    selected_model: EmbeddingModelSpec,
    query: str,
    cache: Path,
    local_files_only: bool,
    threads: int | None,
    limit: int,
    max_vectors: int,
    backend_factory: BackendFactory,
    evidence_mode: bool = False,
    cancellation_check: Callable[[], None] | None = None,
) -> SemanticRanking:
    """Compatibility entry point returning only source-content evidence."""

    return text_search_rankings(
        database,
        database_exists=database_exists,
        selected_model=selected_model,
        query=query,
        cache=cache,
        local_files_only=local_files_only,
        threads=threads,
        limit=limit,
        max_vectors=max_vectors,
        backend_factory=backend_factory,
        evidence_mode=evidence_mode,
        include_title=False,
        cancellation_check=cancellation_check,
    )[0]


def text_search_rankings(
    database: Path,
    *,
    database_exists: bool,
    selected_model: EmbeddingModelSpec,
    query: str,
    cache: Path,
    local_files_only: bool,
    threads: int | None,
    limit: int,
    max_vectors: int,
    backend_factory: BackendFactory,
    evidence_mode: bool = False,
    include_title: bool = False,
    cancellation_check: Callable[[], None] | None = None,
) -> tuple[SemanticRanking, ...]:
    """Search source content and durable basename metadata as separate channels."""

    if selected_model.modality is not EmbeddingModality.TEXT:
        raise ValueError("semantic text search requires a text model")
    if not database_exists:
        return (
            unavailable_semantic_ranking(
                SEMANTIC_TEXT_RANKING,
                "semantic_index_missing",
            ),
        )
    if not indexed_model_available(database, selected_model):
        return (
            unavailable_semantic_ranking(
                SEMANTIC_TEXT_RANKING,
                "text_model_not_indexed",
            ),
        )
    try:
        vector = query_vector(
            selected_model,
            query,
            cache_dir=cache,
            local_files_only=local_files_only,
            threads=threads,
            backend_factory=backend_factory,
            cancellation_check=cancellation_check,
        )
    except SemanticModelUnavailableError as exc:
        return (unavailable_semantic_ranking(SEMANTIC_TEXT_RANKING, exc.reason),)

    body_ranking = semantic_ranking(
        database,
        name=SEMANTIC_TEXT_RANKING,
        query_model=selected_model,
        target_modality=EmbeddingModality.TEXT,
        vector=vector,
        indexed_model_signatures=(selected_model.model_signature,),
        limit=limit,
        max_vectors=max_vectors,
        evidence_mode=evidence_mode,
        text_scope="content",
        provenance={
            "channel": "source_content",
            "excluded_section_kind": SEMANTIC_TITLE_SECTION_KIND,
        },
        cancellation_check=cancellation_check,
    )
    body_ranking = apply_text_retrieval_calibration(
        body_ranking,
        selected_model=selected_model,
    )
    body_ranking = apply_document_result_diversity(
        body_ranking,
        max_evidence_per_item=2 if evidence_mode else 1,
    )
    if evidence_mode or not include_title:
        return (body_ranking,)

    title_budget = max_vectors - body_ranking.scanned
    if title_budget < 1:
        return body_ranking, replace(
            unavailable_semantic_ranking(
                SEMANTIC_TITLE_RANKING,
                "semantic_vector_budget_unavailable",
            ),
            fusion_weight=SEMANTIC_TITLE_FUSION_WEIGHT,
            provenance={
                "expected_policy_signature": SEMANTIC_TITLE_POLICY,
                "expected_basis": ("durable_source_title_or_bounded_heading_or_basename"),
                "mutable_metadata": True,
                "advisory_only": True,
            },
        )

    title_ranking = semantic_ranking(
        database,
        name=SEMANTIC_TITLE_RANKING,
        query_model=selected_model,
        target_modality=EmbeddingModality.TEXT,
        vector=vector,
        indexed_model_signatures=(selected_model.model_signature,),
        limit=limit,
        max_vectors=title_budget,
        text_scope="title",
        fusion_weight=SEMANTIC_TITLE_FUSION_WEIGHT,
        provenance={
            "expected_policy_signature": SEMANTIC_TITLE_POLICY,
            "expected_basis": ("durable_source_title_or_bounded_heading_or_basename"),
            "mutable_metadata": True,
            "advisory_only": True,
        },
        cancellation_check=cancellation_check,
    )
    observed_policies = sorted(
        {
            policy
            for resolved in title_ranking.resolved
            if isinstance(
                policy := resolved.section_provenance.get("policy_signature"),
                str,
            )
            and policy.strip()
        }
    )
    title_ranking = replace(
        title_ranking,
        provenance={
            **title_ranking.provenance,
            "observed_policy_signatures": observed_policies,
            "observed_unversioned_hits": sum(
                1
                for resolved in title_ranking.resolved
                if not isinstance(
                    policy := resolved.section_provenance.get("policy_signature"),
                    str,
                )
                or not policy.strip()
            ),
        },
    )
    title_ranking = apply_text_retrieval_calibration(
        title_ranking,
        selected_model=selected_model,
    )
    title_ranking = apply_document_result_diversity(
        title_ranking,
        max_evidence_per_item=1,
    )
    if title_ranking.scanned == 0:
        return (body_ranking,)
    return body_ranking, title_ranking


def image_search_ranking(
    database: Path,
    *,
    database_exists: bool,
    query: str,
    cache: Path,
    local_files_only: bool,
    threads: int | None,
    limit: int,
    max_vectors: int,
    backend_factory: BackendFactory,
    evidence_mode: bool = False,
    cancellation_check: Callable[[], None] | None = None,
) -> SemanticRanking:
    query_model = clip_text_model()
    indexed_model = clip_image_model()
    if not database_exists:
        return unavailable_semantic_ranking(
            "semantic_image",
            "semantic_index_missing",
        )
    if not (
        registered_model_available(database, query_model)
        and indexed_model_available(database, indexed_model)
    ):
        return unavailable_semantic_ranking(
            "semantic_image",
            "clip_models_not_indexed",
        )
    try:
        vector = query_vector(
            query_model,
            expand_domain_query(query),
            cache_dir=cache,
            local_files_only=local_files_only,
            threads=threads,
            backend_factory=backend_factory,
            cancellation_check=cancellation_check,
        )
    except SemanticModelUnavailableError as exc:
        return unavailable_semantic_ranking("semantic_image", exc.reason)
    return semantic_ranking(
        database,
        name="semantic_image",
        query_model=query_model,
        target_modality=EmbeddingModality.IMAGE,
        vector=vector,
        indexed_model_signatures=(indexed_model.model_signature,),
        limit=limit,
        max_vectors=max_vectors,
        evidence_mode=evidence_mode,
        cancellation_check=cancellation_check,
    )


# endregion [02]


# region [03] Rank fusion


def _resolve_fused_hits(
    rankings: Sequence[SemanticRanking],
    lexical_rankings: Sequence[LexicalRanking],
    *,
    limit: int,
) -> tuple[FusedResolvedHit, ...]:
    raw_rankings = {semantic_ranking.name: semantic_ranking.hits for semantic_ranking in rankings}
    raw_rankings.update(
        {
            lexical_ranking.ranking_name: lexical_ranking.search_hits
            for lexical_ranking in lexical_rankings
        }
    )
    weights = {
        semantic_ranking.name: semantic_ranking.fusion_weight for semantic_ranking in rankings
    }
    fused = reciprocal_rank_fusion(raw_rankings, limit=limit, weights=weights)
    resolved_by_item: dict[str, ResolvedSearchHit] = {}
    for semantic_ranking_value in rankings:
        for semantic_resolved in semantic_ranking_value.resolved:
            resolved_by_item.setdefault(
                semantic_resolved.hit.item_id,
                semantic_resolved,
            )
    for lexical_ranking in lexical_rankings:
        for lexical_resolved in lexical_ranking.hits:
            resolved_by_item.setdefault(
                lexical_resolved.hit.item_id,
                lexical_resolved,
            )
    return tuple(
        FusedResolvedHit(
            value,
            resolved_by_item[value.item_id].path,
            resolved_by_item[value.item_id].source_kind,
            resolved_by_item[value.item_id].source_identity,
            resolved_by_item[value.item_id].snippet,
        )
        for value in fused
        if value.item_id in resolved_by_item
    )


def _validated_search_query(query: object) -> str:
    if not isinstance(query, str):
        raise ValueError("semantic query must be a string")
    normalized = query.strip()
    if not normalized:
        raise ValueError("semantic query cannot be blank")
    if any(unicodedata.category(character) == "Cc" for character in query):
        raise ValueError("semantic query cannot contain control characters")
    if len(normalized) > MAX_QUERY_CHARS:
        raise ValueError(f"semantic query cannot exceed {MAX_QUERY_CHARS} characters")
    return normalized


def _bounded_search_integer(
    value: object,
    *,
    maximum: int,
    error_message: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(error_message)
    return value


def _semantic_candidate_limit(candidate_limit: object, *, result_limit: int) -> int:
    if candidate_limit is None:
        return min(MAX_SEMANTIC_CANDIDATE_HITS, max(result_limit * 3, result_limit))
    return _bounded_search_integer(
        candidate_limit,
        maximum=MAX_SEMANTIC_CANDIDATE_HITS,
        error_message=(
            f"semantic candidate_limit must be between 1 and {MAX_SEMANTIC_CANDIDATE_HITS}"
        ),
    )


def _cancellation_point(cancellation_check: Callable[[], None] | None) -> None:
    if cancellation_check is not None:
        cancellation_check()


def _prepare_search_context(
    state_directory: Path,
    query: object,
    *,
    limit: object,
    candidate_limit: object,
    max_vectors: object,
    include_text: bool,
    include_images: bool,
    include_lexical: bool,
    semantic_database: object,
    model_cache_override: Path | None,
    cancellation_check: Callable[[], None] | None,
) -> _SemanticSearchContext:
    normalized_query = _validated_search_query(query)
    validated_limit = _bounded_search_integer(
        limit,
        maximum=1_000,
        error_message="semantic search limit must be between 1 and 1000",
    )
    validated_candidate_limit = _semantic_candidate_limit(
        candidate_limit,
        result_limit=validated_limit,
    )
    validated_max_vectors = _bounded_search_integer(
        max_vectors,
        maximum=10_000_000,
        error_message="semantic max_vectors must be between 1 and 10000000",
    )
    if not (include_text or include_images or include_lexical):
        raise ValueError("at least one semantic or lexical ranking must be selected")
    if semantic_database is not None and not isinstance(semantic_database, Path):
        raise ValueError("semantic_database must be a Path when provided")
    _cancellation_point(cancellation_check)
    database = (
        semantic_database
        if isinstance(semantic_database, Path)
        else state_directory / SEMANTIC_DATABASE_NAME
    )
    return _SemanticSearchContext(
        state_directory,
        normalized_query,
        validated_limit,
        validated_candidate_limit,
        min(MAX_LEXICAL_CANDIDATE_HITS, max(validated_limit * 3, validated_limit)),
        validated_max_vectors,
        database,
        database.is_file(),
        model_cache(state_directory, model_cache_override),
    )


def _semantic_search_rankings(
    context: _SemanticSearchContext,
    *,
    include_text: bool,
    include_title: bool,
    include_images: bool,
    text_model: EmbeddingModelSpec | None,
    local_files_only: bool,
    threads: int | None,
    backend_factory: BackendFactory,
    evidence_mode: bool,
    cancellation_check: Callable[[], None] | None,
) -> tuple[SemanticRanking, ...]:
    rankings: list[SemanticRanking] = []
    if include_text:
        rankings.extend(
            text_search_rankings(
                context.database,
                database_exists=context.database_exists,
                selected_model=text_model or multilingual_text_model(),
                query=context.query,
                cache=context.cache,
                local_files_only=local_files_only,
                threads=threads,
                limit=context.semantic_candidate_limit,
                max_vectors=context.max_vectors,
                backend_factory=backend_factory,
                evidence_mode=evidence_mode,
                include_title=include_title,
                cancellation_check=cancellation_check,
            )
        )
    if include_images:
        rankings.append(
            image_search_ranking(
                context.database,
                database_exists=context.database_exists,
                query=context.query,
                cache=context.cache,
                local_files_only=local_files_only,
                threads=threads,
                limit=context.semantic_candidate_limit,
                max_vectors=context.max_vectors,
                backend_factory=backend_factory,
                evidence_mode=evidence_mode,
                cancellation_check=cancellation_check,
            )
        )
    return tuple(rankings)


def _lexical_search_rankings(
    context: _SemanticSearchContext,
    *,
    include_lexical: bool,
    lexical_paths: LexicalStatePaths | None,
    lexical_search: LexicalSearch,
    cancellation_check: Callable[[], None] | None,
) -> tuple[LexicalRanking, ...]:
    if not include_lexical:
        return ()
    paths = lexical_paths or default_lexical_paths(context.state_directory)
    if cancellation_check is None:
        return lexical_search(
            paths,
            context.query,
            limit=context.lexical_candidate_limit,
        )
    return lexical_search(
        paths,
        context.query,
        limit=context.lexical_candidate_limit,
        cancellation_check=cancellation_check,
    )


def search_semantic_index(
    state_directory: Path,
    query: str,
    *,
    limit: int,
    candidate_limit: int | None = None,
    max_vectors: int,
    include_text: bool,
    include_title: bool,
    include_images: bool,
    include_lexical: bool,
    lexical_paths: LexicalStatePaths | None,
    semantic_database: Path | None = None,
    text_model: EmbeddingModelSpec | None,
    model_cache_override: Path | None,
    local_files_only: bool,
    threads: int | None,
    backend_factory: BackendFactory,
    lexical_search: LexicalSearch,
    evidence_mode: bool = False,
    cancellation_check: Callable[[], None] | None = None,
) -> SemanticSearchResult:
    """Search incompatible spaces independently, then fuse only their ranks."""

    context = _prepare_search_context(
        state_directory,
        query,
        limit=limit,
        candidate_limit=candidate_limit,
        max_vectors=max_vectors,
        include_text=include_text,
        include_images=include_images,
        include_lexical=include_lexical,
        semantic_database=semantic_database,
        model_cache_override=model_cache_override,
        cancellation_check=cancellation_check,
    )
    rankings = _semantic_search_rankings(
        context,
        include_text=include_text,
        include_title=include_title,
        include_images=include_images,
        text_model=text_model,
        local_files_only=local_files_only,
        threads=threads,
        backend_factory=backend_factory,
        evidence_mode=evidence_mode,
        cancellation_check=cancellation_check,
    )
    _cancellation_point(cancellation_check)
    lexical_rankings = _lexical_search_rankings(
        context,
        include_lexical=include_lexical,
        lexical_paths=lexical_paths,
        lexical_search=lexical_search,
        cancellation_check=cancellation_check,
    )
    _cancellation_point(cancellation_check)
    return SemanticSearchResult(
        context.query,
        rankings,
        lexical_rankings,
        _resolve_fused_hits(rankings, lexical_rankings, limit=context.limit),
    )


# endregion [03]
