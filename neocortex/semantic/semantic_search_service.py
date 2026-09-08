"""Separate-space semantic retrieval and deterministic rank-only fusion."""

from __future__ import annotations
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol

from .semantic_backends import EmbeddingBackend, TextTokenLimitExceededError, reciprocal_rank_fusion
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
    ExactSearchPage,
    ExactSearchQuery,
    FusionEvidence,
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
    ImageRetrievalCalibration,
    SemanticRanking,
    SemanticSearchResult,
)
from .semantic_sources import (
    SEMANTIC_TITLE_POLICY,
    SEMANTIC_TITLE_SECTION_KIND,
    semantic_source_database,
)
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

_QUERY_INTENT_TERM = re.compile(r"[^\W_]+", flags=re.UNICODE)
_EXPLICIT_VISUAL_TERMS = frozenset(
    {
        "abbildung",
        "bild",
        "captura",
        "diagram",
        "diagrama",
        "diagramm",
        "drawing",
        "esquema",
        "foto",
        "fotografía",
        "fotografia",
        "fotografie",
        "image",
        "imagen",
        "photograph",
        "photo",
        "picture",
        "plano",
        "schaltplan",
        "schematic",
        "screenshot",
        "visual",
        "zeichnung",
    }
)
_EXPLICIT_TEXTUAL_TERMS = frozenset(
    {
        "absatz",
        "archivo",
        "datei",
        "dice",
        "document",
        "documento",
        "dokument",
        "erwähnt",
        "erwahnt",
        "file",
        "menciona",
        "page",
        "paragraph",
        "página",
        "pagina",
        "seite",
        "says",
        "steht",
        "text",
        "texto",
    }
)
_EXPLICIT_VISUAL_SUBSTRINGS = ("图片", "图像", "照片", "相片", "截图", "图表", "示意图")
_EXPLICIT_TEXTUAL_SUBSTRINGS = ("文本", "文档", "文件", "段落", "页面", "内容")
_EXPLICIT_OCR_TERMS = frozenset(
    {"ocr", "transcribe", "transcribir", "transcription", "transcripción"}
)
_IMAGE_READING_TERMS = frozenset({"dice", "says", "leer", "read"})
_TEXT_IN_IMAGE = re.compile(
    r"\b(?:texto|text)\s+(?:(?:en|de|del|in|from|on)\s+)?"
    r"(?:(?:la|el|esta|the|this|una?)\s+)?(?:imagen|foto|image|photo|picture|captura)\b",
    re.IGNORECASE,
)


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
    query: str | None = None,
    diagnostic_item_ids: tuple[str, ...] = (),
    cancellation_check: Callable[[], None] | None = None,
) -> SemanticRanking:
    search_page: Callable[..., ExactSearchPage] = (
        search_exact_evidence_page if evidence_mode else search_exact_page
    )
    diagnostics: dict[str, object] = {}
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
        diagnostic_item_ids=diagnostic_item_ids,
        diagnostics=diagnostics,
    )
    resolved_values: list[ResolvedSearchHit] = []
    for hit_batch in batches(page.hits, SEARCH_RESOLUTION_BATCH_SIZE):
        if cancellation_check is not None:
            cancellation_check()
        if query is None:
            resolved_values.extend(resolve_search_hits(database, hit_batch))
        else:
            resolved_values.extend(resolve_search_hits(database, hit_batch, query=query))
    resolved = tuple(resolved_values)
    cutoff_reason = (
        "max_vectors_reached"
        if not page.complete
        else ("top_k" if len(page.hits) == limit and page.scanned > len(page.hits) else None)
    )
    cutoff_score = page.hits[-1].score if len(page.hits) == limit else None
    ranking_provenance = {
        **dict(provenance or {}),
        "candidate_selection": {
            "policy_signature": "semantic-candidate-funnel-v1",
            "candidate_limit": limit,
            "ranking_unit": "evidence" if evidence_mode else "item",
            "text_scope": text_scope,
            "vectors_scanned": page.scanned,
            "scan_complete": page.complete,
            "selected_candidates": len(page.hits),
            "cutoff_reason": cutoff_reason,
            "cutoff_score": cutoff_score,
            "score_interpretation": "cosine_similarity_not_probability",
        },
    }
    if diagnostic_item_ids:
        raw_targets = diagnostics.pop("target_hits", ())
        target_hits = (
            tuple(hit for hit in raw_targets if isinstance(hit, SearchHit))
            if isinstance(raw_targets, tuple)
            else ()
        )
        resolved_targets = {value.hit.ref_id: value for value in resolved}
        unresolved_targets = tuple(hit for hit in target_hits if hit.ref_id not in resolved_targets)
        if unresolved_targets:
            additional = resolve_search_hits(database, unresolved_targets, query=query)
            resolved_targets.update({value.hit.ref_id: value for value in additional})
        raw_entries = diagnostics.get("target_diagnostics", ())
        entries: list[dict[str, object]] = []
        for raw_entry in raw_entries if isinstance(raw_entries, list) else ():
            if not isinstance(raw_entry, dict):
                continue
            entry = dict(raw_entry)
            ref_id = entry.get("ref_id")
            target = resolved_targets.get(ref_id) if isinstance(ref_id, int) else None
            if target is not None:
                backend, pipeline, conflict = _retrieval_contract_provenance(target.hit.provenance)
                entry.update(
                    {
                        "source_kind": target.source_kind,
                        "source_status": target.source_status,
                        "published_revision_id": target.published_revision_id,
                        "current_revision_id": target.current_revision_id,
                        "source_revision_is_current": target.source_revision_is_current,
                        "source_processing_signature": target.source_revision.get(
                            "processing_signature"
                        ),
                        "section_kind": target.section_kind,
                        "section_id": target.section_id,
                        "start_char": target.start_char,
                        "end_char": target.end_char,
                        "snippet": target.snippet,
                        "published_chunk_fingerprint_verified": target.hit.modality
                        is EmbeddingModality.TEXT,
                        "backend": backend if isinstance(backend, str) else None,
                        "pipeline": pipeline if isinstance(pipeline, str) else None,
                        "calibration_contract_conflict": conflict,
                    }
                )
            entries.append(entry)
        ranking_provenance["target_diagnostics"] = entries
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
        provenance=ranking_provenance,
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


def _retrieval_stage_provenance(
    ranking: SemanticRanking,
    retained_hits: Sequence[SearchHit],
    *,
    stage: Literal["text_calibration", "image_calibration", "document_diversity"],
    selected_model: EmbeddingModelSpec | None = None,
    image_score_floor: float | None = None,
) -> dict[str, object]:
    """Retain bounded diagnostics for targets excluded before the final result."""
    provenance = dict(ranking.provenance)
    funnel_value = provenance.get("candidate_funnel")
    funnel = dict(funnel_value) if isinstance(funnel_value, Mapping) else {}
    funnel[stage] = {
        "input_candidates": len(ranking.hits),
        "retained_candidates": len(retained_hits),
    }
    provenance["candidate_funnel"] = funnel
    raw_targets = provenance.get("target_diagnostics")
    if not isinstance(raw_targets, list):
        return provenance
    before = {hit.ref_id for hit in ranking.hits}
    after = {hit.ref_id for hit in retained_hits}
    targets: list[dict[str, object]] = []
    for raw_target in raw_targets:
        if not isinstance(raw_target, dict):
            continue
        target = dict(raw_target)
        if stage == "text_calibration":
            model_signature = target.get("model_signature")
            source_kind = target.get("source_kind")
            floor = (
                text_retrieval_score_floor(
                    model_signature=model_signature,
                    pipeline=target.get("pipeline"),
                    backend=target.get("backend"),
                    source_kind=source_kind,
                )
                if isinstance(model_signature, str)
                and isinstance(source_kind, str)
                and selected_model is not None
                and selected_model.model_signature == TEXT_MODEL_SIGNATURE
                and target.get("calibration_contract_conflict") is False
                else None
            )
            raw_score = target.get("raw_score")
            target["source_score_floor"] = floor
            target["above_score_floor"] = (
                raw_score >= floor
                if isinstance(raw_score, (int, float)) and floor is not None
                else None
            )
            # A targeted hit may be observed by the exhaustive diagnostic scan
            # but omitted from the bounded candidate window.  Calibration is
            # applied only to candidates, so retain that distinction instead
            # of implying that the floor rejected an unexamined hit.
            if isinstance(ref_id := target.get("ref_id"), int) and ref_id not in before:
                target["threshold_evaluation"] = (
                    "not_reached_candidate_window" if floor is not None else "not_calibrated"
                )
        elif stage == "image_calibration":
            raw_score = target.get("raw_score")
            target["source_score_floor"] = image_score_floor
            target["above_score_floor"] = (
                raw_score >= image_score_floor
                if isinstance(raw_score, (int, float)) and image_score_floor is not None
                else None
            )
        ref_id = target.get("ref_id")
        if isinstance(ref_id, int) and ref_id in before:
            target["stage"] = (
                (
                    "rejected_by_text_floor"
                    if stage == "text_calibration"
                    else "rejected_by_image_calibration"
                    if stage == "image_calibration"
                    else "excluded_by_document_diversity"
                )
                if ref_id not in after
                else ("accepted_by_text_calibration" if stage == "text_calibration" else "retained")
            )
        targets.append(target)
    provenance["target_diagnostics"] = targets
    return provenance


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
                **_retrieval_stage_provenance(
                    ranking,
                    ranking.hits,
                    stage="text_calibration",
                    selected_model=selected_model,
                ),
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
            **_retrieval_stage_provenance(
                ranking,
                retained_hits,
                stage="text_calibration",
                selected_model=selected_model,
            ),
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
            **_retrieval_stage_provenance(ranking, retained_hits, stage="document_diversity"),
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


def classify_image_query_intent(
    query: str,
    *,
    image_only: bool,
) -> Literal["explicit_visual", "explicit_textual", "ambiguous"]:
    """Classify only strong modality cues; absence of a cue stays ambiguous."""

    if image_only:
        return "explicit_visual"
    terms = {term.casefold() for term in _QUERY_INTENT_TERM.findall(query)}
    if (
        terms.intersection(_EXPLICIT_OCR_TERMS)
        or _TEXT_IN_IMAGE.search(query)
        or (terms.intersection(_EXPLICIT_VISUAL_TERMS) and terms.intersection(_IMAGE_READING_TERMS))
    ):
        return "explicit_textual"
    if terms.intersection(_EXPLICIT_VISUAL_TERMS) or any(
        marker in query for marker in _EXPLICIT_VISUAL_SUBSTRINGS
    ):
        return "explicit_visual"
    if terms.intersection(_EXPLICIT_TEXTUAL_TERMS) or any(
        marker in query for marker in _EXPLICIT_TEXTUAL_SUBSTRINGS
    ):
        return "explicit_textual"
    return "ambiguous"


def _image_routing_provenance(
    *,
    intent: str,
    executed: bool,
    reason: str | None,
) -> dict[str, object]:
    return {
        "policy_signature": "semantic-image-query-routing-v1",
        "intent": intent,
        "executed": executed,
        "reason": reason,
    }


def _image_abstention_ranking(
    *,
    intent: str,
    reason: str,
    calibration: ImageRetrievalCalibration | None,
) -> SemanticRanking:
    routed_away = reason in {
        "textual_query_routed_away_from_clip",
        "ambiguous_query_requires_text_evidence",
    }
    calibration_metadata: dict[str, object] = {
        "status": (
            "not_required_for_query"
            if routed_away
            else "not_calibrated"
            if calibration is None
            else "contract_mismatch"
        ),
        "query_abstained": True,
        "abstention_reason": reason,
        "score_interpretation": "cosine_similarity_retrieval_floor_not_probability",
        "raw_hits": 0,
        "retained_hits": 0,
        "rejected_hits": 0,
    }
    if calibration is not None:
        calibration_metadata.update(
            {
                "calibration_signature": calibration.calibration_signature,
                "query_model_signature": calibration.query_model_signature,
                "indexed_model_signature": calibration.indexed_model_signature,
                "pipeline": calibration.pipeline,
                "backend": calibration.backend,
                "minimum_score": calibration.minimum_score,
                "positive_queries": calibration.positive_queries,
                "negative_queries": calibration.negative_queries,
                "sample_items": calibration.sample_items,
                "indexed_processing_signature": calibration.indexed_processing_signature,
            }
        )
    return SemanticRanking(
        name="semantic_image",
        hits=(),
        resolved=(),
        scanned=0,
        complete=True,
        provenance={
            "image_query_routing": _image_routing_provenance(
                intent=intent,
                executed=False,
                reason=reason,
            ),
            "retrieval_abstention": calibration_metadata,
        },
    )


def _image_calibration_mismatch(
    calibration: ImageRetrievalCalibration,
    *,
    query_model: EmbeddingModelSpec,
    indexed_model: EmbeddingModelSpec,
    indexed_processing_signature: str | None,
) -> str | None:
    if calibration.query_model_signature != query_model.model_signature:
        return "query_model_not_calibrated"
    if calibration.indexed_model_signature != indexed_model.model_signature:
        return "indexed_model_not_calibrated"
    if calibration.pipeline != SEMANTIC_PIPELINE_VERSION:
        return "pipeline_not_calibrated"
    if calibration.indexed_processing_signature is not None:
        if indexed_processing_signature is None:
            return "indexed_processing_signature_unavailable"
        if calibration.indexed_processing_signature != indexed_processing_signature:
            return "indexed_processing_signature_not_calibrated"
    return None


def apply_image_retrieval_calibration(
    ranking: SemanticRanking,
    *,
    calibration: ImageRetrievalCalibration,
) -> SemanticRanking:
    """Fail closed for CLIP neighbours outside one measured exact contract."""

    retained_keys: set[tuple[int, str, str, int]] = set()
    rejected_by_reason: dict[str, int] = {}
    for hit in ranking.hits:
        backend, pipeline, provenance_conflict = _retrieval_contract_provenance(hit.provenance)
        reason: str | None = None
        if provenance_conflict:
            reason = "provenance_contract_conflict"
        elif pipeline != calibration.pipeline:
            reason = "pipeline_not_calibrated"
        elif backend != calibration.backend:
            reason = "backend_not_calibrated"
        elif hit.score < calibration.minimum_score:
            reason = "below_calibrated_score_floor"
        if reason is None:
            retained_keys.add(_search_hit_key(hit))
        else:
            rejected_by_reason[reason] = rejected_by_reason.get(reason, 0) + 1

    retained_hits = tuple(hit for hit in ranking.hits if _search_hit_key(hit) in retained_keys)
    retained_resolved = tuple(
        value for value in ranking.resolved if _search_hit_key(value.hit) in retained_keys
    )
    query_abstained = bool(ranking.hits) and not retained_hits
    calibration_metadata = {
        "status": "applied",
        "calibration_signature": calibration.calibration_signature,
        "query_model_signature": calibration.query_model_signature,
        "indexed_model_signature": calibration.indexed_model_signature,
        "pipeline": calibration.pipeline,
        "backend": calibration.backend,
        "minimum_score": calibration.minimum_score,
        "positive_queries": calibration.positive_queries,
        "negative_queries": calibration.negative_queries,
        "sample_items": calibration.sample_items,
        "indexed_processing_signature": calibration.indexed_processing_signature,
        "score_interpretation": "cosine_similarity_retrieval_floor_not_probability",
        "raw_hits": len(ranking.hits),
        "retained_hits": len(retained_hits),
        "rejected_hits": len(ranking.hits) - len(retained_hits),
        "rejected_by_reason": rejected_by_reason,
        "query_abstained": query_abstained,
        "abstention_reason": (
            "all_visual_candidates_rejected_by_calibration" if query_abstained else None
        ),
    }
    return replace(
        ranking,
        hits=retained_hits,
        resolved=retained_resolved,
        provenance={
            **_retrieval_stage_provenance(
                ranking,
                retained_hits,
                stage="image_calibration",
                image_score_floor=calibration.minimum_score,
            ),
            "retrieval_abstention": calibration_metadata,
        },
    )


# endregion [01]


# region [02] Modality-isolated rankings


def default_lexical_paths(state_directory: Path) -> LexicalStatePaths:
    return LexicalStatePaths(
        pdf=state_directory / "pdf.sqlite3",
        docx=state_directory / "docx.sqlite3",
        office=state_directory / "office.sqlite3",
        audio=state_directory / "audio.sqlite3",
        video=semantic_source_database(state_directory, "video"),
        archive=state_directory / "archive.sqlite3",
        text=state_directory / "text.sqlite3",
    )


def _merge_text_query_variants(
    variants: Sequence[tuple[Mapping[str, object], SemanticRanking]],
    *,
    limit: int,
    evidence_mode: bool,
) -> SemanticRanking:
    """One best scored observation per evidence/item, never extra channel votes."""
    original = variants[0][1]
    generations: dict[str, set[int]] = {}
    for _, ranking in variants:
        for hit in ranking.hits:
            generations.setdefault(hit.indexed_model_signature, set()).add(hit.generation_id)
    if any(len(values) > 1 for values in generations.values()):
        raise RuntimeError("published semantic generation changed between query variants")
    by_key: dict[tuple[str, str], tuple[int, SearchHit, ResolvedSearchHit]] = {}
    observations: dict[tuple[str, str], list[dict[str, object]]] = {}
    metadata: list[dict[str, object]] = []
    for position, (variant, ranking) in enumerate(variants):
        metadata.append(
            {
                **variant,
                "executed": True,
                "vectors_scanned": ranking.scanned,
                "complete": ranking.complete,
                "raw_candidates": len(ranking.hits),
                "cutoff_score": ranking.cutoff_score,
            }
        )
        resolved_by_key = {_search_hit_key(value.hit): value for value in ranking.resolved}
        for rank, hit in enumerate(ranking.hits, 1):
            resolved = resolved_by_key.get(_search_hit_key(hit))
            if resolved is None:
                continue
            key = (hit.item_id, hit.entity_id if evidence_mode else "")
            observations.setdefault(key, []).append(
                {
                    "variant_id": variant["variant_id"],
                    "raw_score": hit.score,
                    "source_rank": rank,
                    "ref_id": hit.ref_id,
                    "entity_id": hit.entity_id,
                    "generation_id": hit.generation_id,
                }
            )
            previous = by_key.get(key)
            if previous is None or hit.score > previous[1].score:
                by_key[key] = (position, hit, resolved)
    ordered = sorted(by_key, key=lambda key: (-by_key[key][1].score, key))[:limit]
    hits: list[SearchHit] = []
    resolved_hits: list[ResolvedSearchHit] = []
    for key in ordered:
        position, hit, resolved = by_key[key]
        variant = variants[position][0]
        expansion = {
            "policy_signature": "semantic-query-variants-single-channel-v1",
            "winning_variant": dict(variant),
            "observations": observations[key],
            "aggregation": "best_single_observation_no_additional_channel_votes",
            "unlisted_variant_scores": "not_observed_in_that_candidate_window",
            "score_interpretation": "cosine_for_named_effective_query_not_original_query_probability",
        }
        updated_hit = replace(
            hit, provenance={**hit.provenance, "retrieval_query_variants": expansion}
        )
        support_value = resolved.section_provenance.get("query_support")
        support = dict(support_value) if isinstance(support_value, Mapping) else {}
        support["query_expansion"] = expansion
        hits.append(updated_hit)
        resolved_hits.append(
            replace(
                resolved,
                hit=updated_hit,
                section_provenance={**resolved.section_provenance, "query_support": support},
            )
        )
    provenance = dict(original.provenance)
    provenance["query_variants"] = metadata
    provenance["candidate_selection"] = {
        "policy_signature": "semantic-candidate-funnel-v1",
        "aggregation": "best_single_variant_per_evidence"
        if evidence_mode
        else "best_single_variant_per_item",
        "candidate_limit": limit,
        "selected_candidates": len(hits),
        "union_candidates": len(by_key),
        "variants_executed": len(variants),
        "vectors_scanned": sum(ranking.scanned for _, ranking in variants),
        "score_interpretation": "maximum_observed_cosine_over_named_query_variants_not_probability",
    }
    targets_by_item: dict[str, list[dict[str, object]]] = {}
    for variant, ranking in variants:
        raw_targets = ranking.provenance.get("target_diagnostics")
        for raw_target in raw_targets if isinstance(raw_targets, list) else ():
            if isinstance(raw_target, dict) and isinstance(raw_target.get("item_id"), str):
                targets_by_item.setdefault(raw_target["item_id"], []).append(
                    {**raw_target, "variant_id": variant["variant_id"]}
                )
    if targets_by_item:
        targets: list[dict[str, object]] = []
        for item_id, observed in targets_by_item.items():
            selected_hit = next((hit for hit in hits if hit.item_id == item_id), None)
            selected_variant = next(
                (variants[by_key[key][0]][0]["variant_id"] for key in ordered if key[0] == item_id),
                None,
            )
            winner = next(
                (value for value in observed if value["variant_id"] == selected_variant), None
            )
            if winner is None:

                def raw_score(value: dict[str, object]) -> float:
                    score = value.get("raw_score", -2.0)
                    return float(score) if isinstance(score, (int, float, str)) else -2.0

                winner = max(observed, key=raw_score)
            target = dict(winner)
            target["query_variant_diagnostics"] = observed
            target["raw_rank_basis"] = target["variant_id"]
            target["within_candidate_window"] = selected_hit is not None
            target["candidate_rank"] = next(
                (rank for rank, hit in enumerate(hits, 1) if hit.item_id == item_id), None
            )
            if target.get("observed_in_published_scope"):
                target["stage"] = (
                    "candidate_selected" if selected_hit is not None else "outside_candidate_window"
                )
            targets.append(target)
        provenance["target_diagnostics"] = targets
    return replace(
        original,
        hits=tuple(hits),
        resolved=tuple(resolved_hits),
        scanned=sum(ranking.scanned for _, ranking in variants),
        complete=all(ranking.complete for _, ranking in variants),
        cutoff_reason="top_k" if len(by_key) > limit else original.cutoff_reason,
        cutoff_score=hits[-1].score if len(hits) == limit else None,
        provenance=provenance,
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
    diagnostic_item_ids: tuple[str, ...] = (),
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
        diagnostic_item_ids=diagnostic_item_ids,
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
    diagnostic_item_ids: tuple[str, ...] = (),
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
    from .semantic_query_variants import text_query_expansions

    expansions = (
        text_query_expansions(query)
        if selected_model.model_signature == TEXT_MODEL_SIGNATURE
        and classify_image_query_intent(query, image_only=False) != "explicit_visual"
        else ()
    )
    prepared_backend: EmbeddingBackend | None = None

    def shared_backend(
        model: EmbeddingModelSpec,
        *,
        cache_dir: Path,
        local_files_only: bool,
        threads: int | None,
    ) -> EmbeddingBackend:
        nonlocal prepared_backend
        if prepared_backend is None:
            prepared_backend = backend_factory(
                model,
                cache_dir=cache_dir,
                local_files_only=local_files_only,
                threads=threads,
            )
        return prepared_backend

    query_backend_factory = shared_backend if expansions else backend_factory
    try:
        vector = query_vector(
            selected_model,
            query,
            cache_dir=cache,
            local_files_only=local_files_only,
            threads=threads,
            backend_factory=query_backend_factory,
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
        query=query,
        diagnostic_item_ids=diagnostic_item_ids,
        provenance={
            "channel": "source_content",
            "excluded_section_kind": SEMANTIC_TITLE_SECTION_KIND,
        },
        cancellation_check=cancellation_check,
    )
    if expansions:
        original_variant: dict[str, object] = {
            "variant_id": "original",
            "effective_query": query,
            "original_query": query,
            "interpretation": "original_user_query",
        }
        variants: list[tuple[Mapping[str, object], SemanticRanking]] = [
            (original_variant, body_ranking)
        ]
        skipped: list[dict[str, object]] = []
        remaining = max_vectors - body_ranking.scanned
        for expansion in expansions:
            if (
                not body_ranking.complete
                or body_ranking.scanned == 0
                or remaining < body_ranking.scanned
            ):
                skipped.append(
                    {
                        **expansion,
                        "executed": False,
                        "reason": "original_scope_incomplete_or_expansion_vector_budget_unavailable",
                    }
                )
                continue
            effective_query = expansion["effective_query"]
            assert isinstance(effective_query, str)
            try:
                expanded_vector = query_vector(
                    selected_model,
                    effective_query,
                    cache_dir=cache,
                    local_files_only=local_files_only,
                    threads=threads,
                    backend_factory=query_backend_factory,
                    cancellation_check=cancellation_check,
                )
            except (TextTokenLimitExceededError, SemanticModelUnavailableError) as exc:
                skipped.append(
                    {
                        **expansion,
                        "executed": False,
                        "reason": f"optional_query_expansion_unavailable:{type(exc).__name__}",
                    }
                )
                continue
            expanded = semantic_ranking(
                database,
                name=SEMANTIC_TEXT_RANKING,
                query_model=selected_model,
                target_modality=EmbeddingModality.TEXT,
                vector=expanded_vector,
                indexed_model_signatures=(selected_model.model_signature,),
                limit=limit,
                max_vectors=remaining,
                evidence_mode=evidence_mode,
                text_scope="content",
                query=effective_query,
                diagnostic_item_ids=diagnostic_item_ids,
                provenance={"channel": "source_content", "query_variant": dict(expansion)},
                cancellation_check=cancellation_check,
            )
            remaining -= expanded.scanned
            variants.append((expansion, expanded))
        body_ranking = _merge_text_query_variants(
            variants, limit=limit, evidence_mode=evidence_mode
        )
        if skipped:
            body_ranking = replace(
                body_ranking,
                provenance={**body_ranking.provenance, "query_variants_not_executed": skipped},
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
        query=query,
        diagnostic_item_ids=diagnostic_item_ids,
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
    query_intent: Literal["explicit_visual", "explicit_textual", "ambiguous"] = "ambiguous",
    allow_ambiguous_images: bool = True,
    diagnostic_item_ids: tuple[str, ...] = (),
    calibration: ImageRetrievalCalibration | None = None,
    cancellation_check: Callable[[], None] | None = None,
) -> SemanticRanking:
    query_model = clip_text_model()
    indexed_model = clip_image_model()
    # Resolve an intentional routing omission before probing an irrelevant
    # owner/model.  Missing CLIP must not make a purely textual query partial.
    if query_intent == "explicit_textual":
        return _image_abstention_ranking(
            intent=query_intent,
            reason="textual_query_routed_away_from_clip",
            calibration=calibration,
        )
    if query_intent == "ambiguous" and not allow_ambiguous_images:
        return _image_abstention_ranking(
            intent=query_intent,
            reason="ambiguous_query_requires_text_evidence",
            calibration=calibration,
        )
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
    if calibration is None:
        from .image_retrieval_calibration import load_image_retrieval_calibration

        calibration = load_image_retrieval_calibration(database)
    if calibration is None:
        return _image_abstention_ranking(
            intent=query_intent,
            reason="image_retrieval_not_calibrated",
            calibration=None,
        )
    from .image_retrieval_calibration import current_image_processing_signature

    if mismatch := _image_calibration_mismatch(
        calibration,
        query_model=query_model,
        indexed_model=indexed_model,
        indexed_processing_signature=current_image_processing_signature(database),
    ):
        return _image_abstention_ranking(
            intent=query_intent,
            reason=mismatch,
            calibration=calibration,
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
    ranking = semantic_ranking(
        database,
        name="semantic_image",
        query_model=query_model,
        target_modality=EmbeddingModality.IMAGE,
        vector=vector,
        indexed_model_signatures=(indexed_model.model_signature,),
        limit=limit,
        max_vectors=max_vectors,
        evidence_mode=evidence_mode,
        query=query,
        diagnostic_item_ids=diagnostic_item_ids,
        provenance={
            "image_query_routing": _image_routing_provenance(
                intent=query_intent,
                executed=True,
                reason=None,
            ),
        },
        cancellation_check=cancellation_check,
    )
    return apply_image_retrieval_calibration(
        ranking,
        calibration=calibration,
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
    # Apply the explanatory support tier before the public result window, not
    # after discarding lower-ranked candidates.  Every source is already bounded.
    candidate_count = sum(len(hits) for hits in raw_rankings.values())
    fused = reciprocal_rank_fusion(raw_rankings, limit=max(limit, candidate_count), weights=weights)
    resolved_by_key: dict[tuple[str, int, str, str, int], ResolvedSearchHit] = {}
    for semantic_ranking_value in rankings:
        for semantic_resolved in semantic_ranking_value.resolved:
            key = (semantic_ranking_value.name, *_search_hit_key(semantic_resolved.hit))
            prior = resolved_by_key.setdefault(key, semantic_resolved)
            if prior != semantic_resolved:
                raise ValueError("one fusion contribution resolved to incompatible witnesses")
    for lexical_ranking in lexical_rankings:
        for lexical_resolved in lexical_ranking.hits:
            key = (lexical_ranking.ranking_name, *_search_hit_key(lexical_resolved.hit))
            prior = resolved_by_key.setdefault(key, lexical_resolved)
            if prior != lexical_resolved:
                raise ValueError("one fusion contribution resolved to incompatible witnesses")

    def witness_order(value: FusionEvidence) -> tuple[bool, float, int, int, float, str]:
        assert value.witness is not None
        snippet = value.witness.snippet or ""
        support_value = value.witness.section_provenance.get(
            "snippet_query_support",
            value.witness.hit.provenance.get("query_support"),
        )
        support = support_value if isinstance(support_value, Mapping) else {}
        coverage = support.get("term_coverage")
        span = support.get("minimum_span_terms")
        return (
            not bool(snippet),
            -float(coverage) if isinstance(coverage, (int, float)) else 0.0,
            -int(support.get("phrase_match") is True),
            span if isinstance(span, int) else 1_000_000,
            -value.contribution,
            value.ranking,
        )

    output: list[FusedResolvedHit] = []
    for value in fused:
        contributions = tuple(
            replace(
                evidence,
                witness=resolved_by_key.get(
                    (
                        evidence.ranking,
                        evidence.ref_id,
                        evidence.entity_id,
                        value.item_id,
                        evidence.generation_id,
                    )
                )
                if evidence.ref_id is not None and evidence.generation_id is not None
                else None,
            )
            for evidence in value.evidence
        )
        available = tuple(evidence for evidence in contributions if evidence.witness is not None)
        if not available:
            continue
        primary = min(available, key=witness_order).witness
        assert primary is not None
        output.append(
            FusedResolvedHit(
                fused=replace(value, evidence=contributions),
                path=primary.path,
                source_kind=primary.source_kind,
                source_identity=primary.source_identity,
                snippet=primary.snippet,
                primary_evidence=primary,
            )
        )

    def scoped_counterevidence(value: FusedResolvedHit) -> bool:
        witnesses = tuple(
            evidence.witness for evidence in value.fused.evidence if evidence.witness is not None
        )
        support_values = tuple(
            witness.section_provenance.get(
                "query_support", witness.hit.provenance.get("query_support")
            )
            for witness in witnesses
        )
        return bool(witnesses) and all(
            isinstance(support, Mapping) and bool(support.get("role_counterevidence"))
            for support in support_values
        )

    output.sort(key=scoped_counterevidence)
    return tuple(output[:limit])


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


def _annotate_target_fusion(
    ranking: SemanticRanking,
    fused: Sequence[FusedResolvedHit],
) -> SemanticRanking:
    raw_targets = ranking.provenance.get("target_diagnostics")
    if not isinstance(raw_targets, list):
        return ranking
    final_ranks = {value.fused.item_id: rank for rank, value in enumerate(fused, 1)}
    targets: list[dict[str, object]] = []
    for raw_target in raw_targets:
        if not isinstance(raw_target, dict):
            continue
        target = dict(raw_target)
        item_id = target.get("item_id")
        rank = final_ranks.get(item_id) if isinstance(item_id, str) else None
        target["fused_result_rank"] = rank
        target["present_in_fused_results"] = rank is not None
        if rank is None and target.get("stage") in {"retained", "accepted_by_text_calibration"}:
            target["stage"] = "outside_final_result_window"
        targets.append(target)
    return replace(ranking, provenance={**ranking.provenance, "target_diagnostics": targets})


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
    image_only: bool,
    image_query_intent: Literal["explicit_visual", "explicit_textual", "ambiguous"] | None,
    allow_ambiguous_images: bool,
    diagnostic_item_ids: tuple[str, ...],
    image_calibration: ImageRetrievalCalibration | None,
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
                **({"diagnostic_item_ids": diagnostic_item_ids} if diagnostic_item_ids else {}),
                cancellation_check=cancellation_check,
            )
        )
    if include_images:
        query_intent = image_query_intent or classify_image_query_intent(
            context.query,
            image_only=image_only,
        )
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
                query_intent=query_intent,
                allow_ambiguous_images=allow_ambiguous_images,
                diagnostic_item_ids=diagnostic_item_ids,
                calibration=image_calibration,
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
    image_calibration: ImageRetrievalCalibration | None = None,
    image_query_intent: Literal["explicit_visual", "explicit_textual", "ambiguous"] | None = None,
    allow_ambiguous_images: bool = True,
    diagnostic_item_ids: tuple[str, ...] = (),
    cancellation_check: Callable[[], None] | None = None,
) -> SemanticSearchResult:
    """Search incompatible spaces independently, then fuse only their ranks."""

    if image_query_intent is not None and (
        not isinstance(image_query_intent, str)
        or image_query_intent not in {"explicit_visual", "explicit_textual", "ambiguous"}
    ):
        raise ValueError("image_query_intent must be an original query modality intent")
    if not isinstance(allow_ambiguous_images, bool):
        raise ValueError("allow_ambiguous_images must be a boolean")
    from .semantic_search_repository import validate_diagnostic_item_ids

    diagnostic_item_ids = validate_diagnostic_item_ids(diagnostic_item_ids)

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
        image_only=include_images and not include_text and not include_lexical,
        image_query_intent=image_query_intent,
        allow_ambiguous_images=allow_ambiguous_images,
        diagnostic_item_ids=diagnostic_item_ids,
        image_calibration=image_calibration,
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
    fused = _resolve_fused_hits(rankings, lexical_rankings, limit=context.limit)
    return SemanticSearchResult(
        context.query,
        tuple(_annotate_target_fusion(ranking, fused) for ranking in rankings),
        lexical_rankings,
        fused,
    )


# endregion [03]
