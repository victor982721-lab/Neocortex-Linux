"""Bounded exact-vector search and semantic-hit resolution repository."""

from __future__ import annotations
from neocortex.runtime.control.read_operation import read_rows, read_checkpoint, remaining_read_limit
import heapq
import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict

if TYPE_CHECKING:
    from .semantic_exact_index import ExactIndexHandle

from .semantic_item_repository import _decode_chunk_text
from .semantic_lexical import (
    PreparedLiteralQuery,
    analyze_literal_text,
    prepare_literal_query,
    query_centered_snippet,
    query_term_support,
)
from .semantic_models import (
    ActiveEmbeddingPage,
    ActiveEmbeddingRecord,
    EmbeddingModality,
    ExactSearchPage,
    ExactSearchQuery,
    ResolvedSearchHit,
    SearchHit,
    SemanticEntityKind,
    VectorDType,
    cosine_similarity,
    decode_vector,
    normalize_vector,
)
from .semantic_repository_common import (
    MAX_WRITE_BATCH,
    _fingerprint_from_row,
    _load_model,
)
from .semantic_search_order import ExactSearchHeapKey, ExactSearchOrder, exact_search_order
from .semantic_vector_search import (
    SemanticVectorSearch,
    VectorSearchBudget,
    VectorSearchContractError,
    VectorSearchPage,
    VectorSearchRequest,
    VectorSearchUnavailable,
    validate_vector_page,
)
from .semantic_schema import SemanticStateError, semantic_database
from .semantic_resources import native_vector_operation
from .semantic_sources import SEMANTIC_TITLE_POLICY, SEMANTIC_TITLE_SECTION_KIND

TextEmbeddingScope = Literal["all", "content", "title"]

_VECTORIZED_SEARCH_MIN_ROWS = 8
MAX_DIAGNOSTIC_ITEMS = 20
_MAX_DIAGNOSTIC_RANK_ENTRIES = 100_000
_MAX_DIAGNOSTIC_RANK_BYTES = 64 * 1024 * 1024


def validate_diagnostic_item_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > MAX_DIAGNOSTIC_ITEMS or any(
        not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 512
        or any(unicodedata.category(character) == "Cc" for character in item_id)
        for item_id in value
    ):
        raise ValueError("diagnostic_item_ids must be a tuple of at most 20 bounded item IDs")
    return tuple(dict.fromkeys(value))


@dataclass
class _TargetedSearchDiagnostics:
    """Bounded score-only rank accounting during the original exact scan.

    No model call or vector reread is performed.  Exhausting the optional rank
    accounting budget disables global-rank claims, never truncates retrieval.
    """

    item_ids: tuple[str, ...]
    evidence_mode: bool
    best: dict[tuple[str, str], ExactSearchOrder] = field(default_factory=dict)
    targets: dict[str, SearchHit] = field(default_factory=dict)
    rank_budget_exhausted: bool = False
    rank_bytes: int = 0

    def observe(self, hit: SearchHit) -> None:
        if hit.item_id in self.item_ids:
            prior_target = self.targets.get(hit.item_id)
            better = prior_target is None or self._order(hit) < self._order(prior_target)
            if better:
                self.targets[hit.item_id] = hit
        if self.rank_budget_exhausted:
            return
        key = (hit.item_id, hit.entity_id if self.evidence_mode else "")
        entry = self._order(hit)
        prior = self.best.get(key)
        if prior is None:
            self.rank_bytes += 256 + 4 * (len(hit.item_id) + len(hit.entity_id) + len(hit.indexed_model_signature))
            if len(self.best) >= _MAX_DIAGNOSTIC_RANK_ENTRIES or self.rank_bytes > _MAX_DIAGNOSTIC_RANK_BYTES:
                self.best.clear()
                self.rank_budget_exhausted = True
                return
        if prior is None or entry < prior:
            self.best[key] = entry

    @staticmethod
    def _order(hit: SearchHit) -> ExactSearchOrder:
        return exact_search_order(hit.score, hit.item_id, hit.entity_id, hit.indexed_model_signature, hit.ref_id)

    def export(self, page: ExactSearchPage) -> dict[str, object]:
        entries: list[dict[str, object]] = []
        target_hits: list[SearchHit] = []
        for item_id in self.item_ids:
            hit = next((selected for selected in page.hits if selected.item_id == item_id), None)
            hit = hit or self.targets.get(item_id)
            if hit is not None:
                target_hits.append(hit)
            candidate_rank = next(
                (rank for rank, selected in enumerate(page.hits, 1) if selected.item_id == item_id), None,
            )
            observed_rank: int | None = None
            if hit is not None and not self.rank_budget_exhausted:
                target_order = self._order(hit)
                observed_rank = 1 + sum(
                    entry < target_order for entry in self.best.values()
                )
            elif candidate_rank is not None:
                observed_rank = candidate_rank
            entry: dict[str, object] = {
                "item_id": item_id,
                "observed_in_published_scope": hit is not None,
                "within_candidate_window": candidate_rank is not None,
                "candidate_rank": candidate_rank,
                "observed_rank": observed_rank,
                "raw_rank": observed_rank if page.complete else None,
                "rank_is_global": page.complete and observed_rank is not None,
                "rank_granularity": "evidence" if self.evidence_mode else "item",
                "rank_budget_exhausted": self.rank_budget_exhausted,
                "stage": (
                    "candidate_selected" if candidate_rank is not None
                    else "outside_candidate_window" if hit is not None
                    else "not_in_published_search_scope" if page.complete
                    else "unobserved_in_incomplete_scan"
                ),
            }
            if hit is not None:
                entry.update({
                    "raw_score": hit.score, "ref_id": hit.ref_id, "entity_id": hit.entity_id,
                    "generation_id": hit.generation_id, "model_signature": hit.indexed_model_signature,
                })
            entries.append(entry)
        return {"target_diagnostics": entries, "target_hits": tuple(target_hits)}

# region [06] Bounded exact cosine fallback


def _validate_text_scope(
    modality: EmbeddingModality,
    text_scope: TextEmbeddingScope,
) -> None:
    if text_scope not in {"all", "content", "title"}:
        raise ValueError("text_scope must be all, content or title")
    if modality is not EmbeddingModality.TEXT and text_scope != "all":
        raise ValueError("text_scope is only valid for text embeddings")


def _search_models(
    connection: sqlite3.Connection,
    query: ExactSearchQuery,
) -> tuple[str, ...]:
    query_model = _load_model(connection, query.query_model_signature)
    if query_model.vector_space != query.vector_space or query_model.dimensions != query.dimensions:
        raise ValueError("query vector is incompatible with its registered model")
    rows = connection.execute(
        """SELECT model_signature FROM embedding_models
        WHERE vector_space=? AND modality=? AND dimensions=? AND active=1
        ORDER BY model_signature""",
        (query.vector_space, query.target_modality.value, query.dimensions),
    ).fetchall()
    available = tuple(str(row[0]) for row in rows)
    if query.indexed_model_signatures:
        missing = set(query.indexed_model_signatures).difference(available)
        if missing:
            raise ValueError(f"indexed models are absent or incompatible: {sorted(missing)}")
        return query.indexed_model_signatures
    if not available:
        raise ValueError("no compatible indexed models are registered")
    return available


def _search_sql(
    modality: EmbeddingModality,
    pair_count: int,
    *,
    text_scope: TextEmbeddingScope = "all",
) -> str:
    _validate_text_scope(modality, text_scope)
    selected = ",".join("(?,?)" for _ in range(pair_count))
    if pair_count == 1:
        # One published pair is already ordered by the existing search index.
        # Keep members outermost: sorting the wide vector/provenance projection
        # only to restore member_id order would spill every candidate to disk.
        selection = ""
        # Do not require INDEXED BY: read-only callers may still inspect an
        # owner with missing/changed index metadata without repairing it.
        members = "FROM embedding_generation_members e"
        pair_clause = "e.model_signature=? AND e.generation_id=? AND "
        join = "CROSS JOIN"
    else:
        selection = f"WITH selected(model_signature,generation_id) AS (VALUES {selected})"
        members = """FROM selected s
        JOIN embedding_generation_members e
          ON e.model_signature=s.model_signature
         AND e.generation_id=s.generation_id"""
        pair_clause = ""
        join = "JOIN"
    if modality is EmbeddingModality.TEXT:
        scope_clause = {
            "all": "",
            "content": (
                " AND c.section_kind NOT IN "
                f"('{SEMANTIC_TITLE_SECTION_KIND}','video_metadata_title')"
            ),
            "title": (
                " AND c.section_kind IN "
                f"('{SEMANTIC_TITLE_SECTION_KIND}','video_metadata_title')"
            ),
        }[text_scope]
        return f"""{selection}
        SELECT e.member_id AS ref_id,e.entity_id,i.item_id,
            e.model_signature,m.vector_space,m.modality,e.generation_id,
            e.provenance_json,p.vector_blob,p.dimensions,p.vector_dtype
        {members}
        {join} embedding_models m ON m.model_signature=e.model_signature
        {join} vector_payloads p ON p.payload_id=e.payload_id
        {join} semantic_chunk_revisions c
          ON c.chunk_revision_id=e.chunk_revision_id
        {join} semantic_item_revisions i
          ON i.item_revision_id=e.item_revision_id
        {join} embedding_generations g ON g.generation_id=e.generation_id
        WHERE {pair_clause}e.member_id>? AND e.entity_kind='text_chunk' AND g.status='ready'
          AND e.content_xxh3_128=c.content_xxh3_128
          AND e.content_bytes=c.content_bytes
          AND e.content_xxh3_64_guard=c.content_xxh3_64_guard
          {scope_clause}
        ORDER BY e.member_id LIMIT ?"""
    return f"""{selection}
    SELECT e.member_id AS ref_id,e.entity_id,i.item_id,
        e.model_signature,m.vector_space,m.modality,e.generation_id,
        e.provenance_json,p.vector_blob,p.dimensions,p.vector_dtype
    {members}
    {join} embedding_models m ON m.model_signature=e.model_signature
    {join} vector_payloads p ON p.payload_id=e.payload_id
    {join} semantic_item_revisions i ON i.item_revision_id=e.item_revision_id
    {join} embedding_generations g ON g.generation_id=e.generation_id
    WHERE {pair_clause}e.member_id>? AND e.entity_kind='image_item' AND g.status='ready'
      AND e.content_xxh3_128=i.content_xxh3_128
      AND e.content_bytes=i.content_bytes
      AND e.content_xxh3_64_guard=i.content_xxh3_64_guard
    ORDER BY e.member_id LIMIT ?"""


def _published_model_generations(
    connection: sqlite3.Connection,
    model_signatures: Sequence[str],
) -> tuple[tuple[str, int], ...]:
    if not model_signatures:
        return ()
    placeholders = ",".join("?" for _ in model_signatures)
    rows = connection.execute(
        f"""SELECT h.model_signature,h.generation_id,g.model_signature,g.status
        FROM published_embedding_heads h
        JOIN embedding_generations g ON g.generation_id=h.generation_id
        WHERE h.model_signature IN ({placeholders})""",
        tuple(model_signatures),
    ).fetchall()
    by_model: dict[str, int] = {}
    for row in rows:
        head_model = str(row[0])
        if str(row[2]) != head_model or str(row[3]) != "ready":
            raise SemanticStateError(f"published embedding head for {head_model!r} is invalid")
        by_model[head_model] = int(row[1])
    return tuple(
        (signature, by_model[signature]) for signature in model_signatures if signature in by_model
    )


def has_active_embeddings(path: Path, model_signature: str) -> bool:
    """Return whether one registered model has at least one searchable vector."""

    if not model_signature.strip():
        raise ValueError("model_signature cannot be blank")
    with semantic_database(path, readonly=True) as connection:
        model = _load_model(connection, model_signature)
        pairs = _published_model_generations(connection, (model_signature,))
        if not pairs:
            return False
        entity_kind = (
            SemanticEntityKind.TEXT_CHUNK.value
            if model.modality is EmbeddingModality.TEXT
            else SemanticEntityKind.IMAGE_ITEM.value
        )
        row = connection.execute(
            """SELECT 1 FROM embedding_generation_members
            WHERE generation_id=? AND model_signature=? AND entity_kind=? LIMIT 1""",
            (pairs[0][1], model_signature, entity_kind),
        ).fetchone()
    return row is not None


def _exact_search_hit(
    row: sqlite3.Row,
    query: ExactSearchQuery,
    query_vector: tuple[float, ...],
) -> SearchHit:
    dimensions = int(row["dimensions"])
    if dimensions != query.dimensions:
        raise SemanticStateError("persisted vector dimension violates its space")
    stored = decode_vector(
        bytes(row["vector_blob"]),
        dimensions,
        VectorDType(str(row["vector_dtype"])),
    )
    raw_provenance = json.loads(str(row["provenance_json"]))
    if not isinstance(raw_provenance, dict):
        raise SemanticStateError("embedding provenance is not a JSON object")
    return SearchHit(
        ref_id=int(row["ref_id"]),
        entity_id=str(row["entity_id"]),
        item_id=str(row["item_id"]),
        indexed_model_signature=str(row["model_signature"]),
        vector_space=str(row["vector_space"]),
        modality=EmbeddingModality(str(row["modality"])),
        score=cosine_similarity(query_vector, stored, dimensions),
        generation_id=int(row["generation_id"]),
        provenance=raw_provenance,
        query_model_signature=query.query_model_signature,
    )


def _search_hit_from_score(
    row: sqlite3.Row,
    query: ExactSearchQuery,
    score: float,
) -> SearchHit:
    """Materialize one hit after a batch scorer validated its vector payload."""

    raw_provenance = json.loads(str(row["provenance_json"]))
    if not isinstance(raw_provenance, dict):
        raise SemanticStateError("embedding provenance is not a JSON object")
    return SearchHit(
        ref_id=int(row["ref_id"]),
        entity_id=str(row["entity_id"]),
        item_id=str(row["item_id"]),
        indexed_model_signature=str(row["model_signature"]),
        vector_space=str(row["vector_space"]),
        modality=EmbeddingModality(str(row["modality"])),
        score=max(-1.0, min(1.0, float(score))),
        generation_id=int(row["generation_id"]),
        provenance=raw_provenance,
        query_model_signature=query.query_model_signature,
    )


@native_vector_operation(lambda rows, query, _vector, _numpy: len(rows) * query.dimensions * 16)
def _numpy_exact_search_hits(
    rows: Sequence[sqlite3.Row],
    query: ExactSearchQuery,
    query_vector: tuple[float, ...],
    numpy: Any,
) -> tuple[SearchHit, ...]:
    """Score a SQLite page exactly with bounded NumPy/BLAS batches.

    This remains an exhaustive cosine scan over every selected published
    vector.  NumPy replaces per-value Python unpacking and arithmetic; SQLite
    remains the source of truth and all identity/provenance checks stay on the
    existing path.
    """

    query_values = numpy.asarray(query_vector, dtype=numpy.float64)
    query_norm = float(numpy.linalg.norm(query_values))
    if not numpy.isfinite(query_norm) or query_norm <= 0.0:
        raise ValueError("embedding vectors must have a finite non-zero L2 norm")

    scores: list[float | None] = [None] * len(rows)
    positions_by_dtype: dict[VectorDType, list[int]] = {}
    for position, row in enumerate(rows):
        dimensions = int(row["dimensions"])
        if dimensions != query.dimensions:
            raise SemanticStateError("persisted vector dimension violates its space")
        dtype = VectorDType(str(row["vector_dtype"]))
        payload = bytes(row["vector_blob"])
        width = 2 if dtype is VectorDType.FLOAT16 else 4
        expected_bytes = dimensions * width
        if len(payload) != expected_bytes:
            raise ValueError(
                f"invalid vector payload length: expected {expected_bytes}, got {len(payload)}"
            )
        positions_by_dtype.setdefault(dtype, []).append(position)

    for dtype, positions in positions_by_dtype.items():
        numpy_dtype = numpy.dtype("<f2" if dtype is VectorDType.FLOAT16 else "<f4")
        matrix = numpy.empty((len(positions), query.dimensions), dtype=numpy.float64)
        for matrix_row, source_position in enumerate(positions):
            matrix[matrix_row] = numpy.frombuffer(
                bytes(rows[source_position]["vector_blob"]),
                dtype=numpy_dtype,
                count=query.dimensions,
            )
        if not bool(numpy.all(numpy.isfinite(matrix))):
            raise ValueError("embedding vectors must contain only finite values")
        norms = numpy.linalg.norm(matrix, axis=1)
        if not bool(numpy.all(numpy.isfinite(norms))) or bool(numpy.any(norms <= 0.0)):
            raise ValueError("embedding vectors must have a finite non-zero L2 norm")
        batch_scores = (matrix @ query_values) / (norms * query_norm)
        if not bool(numpy.all(numpy.isfinite(batch_scores))):
            raise ValueError("cosine similarity must be finite")
        for source_position, score in zip(positions, batch_scores, strict=True):
            scores[source_position] = float(score)

    if any(score is None for score in scores):
        raise SemanticStateError("vectorized exact search omitted a selected row")
    return tuple(
        _search_hit_from_score(row, query, score)
        for row, score in zip(rows, scores, strict=True)
        if score is not None
    )


def _exact_search_hits(
    rows: Sequence[sqlite3.Row],
    query: ExactSearchQuery,
    query_vector: tuple[float, ...],
) -> tuple[SearchHit, ...]:
    """Score one bounded page, retaining the scalar path as a safe fallback."""

    if len(rows) < _VECTORIZED_SEARCH_MIN_ROWS:
        return tuple(_exact_search_hit(row, query, query_vector) for row in rows)
    try:
        import numpy
    except ImportError:  # Base/source-only installs may omit the Semantic extra.
        return tuple(_exact_search_hit(row, query, query_vector) for row in rows)
    from neocortex.runtime.control.native_library_resources import NativeLibraryControlUnavailable
    try:
        return _numpy_exact_search_hits(rows, query, query_vector, numpy)
    except NativeLibraryControlUnavailable:
        return tuple(_exact_search_hit(row, query, query_vector) for row in rows)


def _retain_exact_search_hit(
    hit: SearchHit,
    *,
    limit: int,
    best_by_item: dict[str, tuple[ExactSearchHeapKey, int, SearchHit]],
    heap: list[tuple[ExactSearchHeapKey, int, SearchHit]],
) -> None:
    entry = (ExactSearchHeapKey(_TargetedSearchDiagnostics._order(hit)), hit.ref_id, hit)
    prior = best_by_item.get(hit.item_id)
    if prior is not None:
        if entry[0] > prior[0]:
            best_by_item[hit.item_id] = entry
            heapq.heappush(heap, entry)
    elif len(best_by_item) < limit:
        best_by_item[hit.item_id] = entry
        heapq.heappush(heap, entry)
    else:
        while heap and best_by_item.get(heap[0][2].item_id) != heap[0]:
            heapq.heappop(heap)
        if not heap:
            raise SemanticStateError("exact-search item heap became empty")
        if entry[0] > heap[0][0]:
            removed = heapq.heappop(heap)
            del best_by_item[removed[2].item_id]
            best_by_item[hit.item_id] = entry
            heapq.heappush(heap, entry)
    if len(heap) > max(limit * 2, limit + 64):
        heap[:] = best_by_item.values()
        heapq.heapify(heap)


def _retain_exact_evidence_hit(
    hit: SearchHit,
    *,
    limit: int,
    best_by_evidence: dict[tuple[str, str], tuple[ExactSearchHeapKey, int, SearchHit]],
    heap: list[tuple[ExactSearchHeapKey, int, SearchHit]],
) -> None:
    """Retain one best vector per concrete entity, not per resource item."""

    key = (hit.item_id, hit.entity_id)
    entry = (ExactSearchHeapKey(_TargetedSearchDiagnostics._order(hit)), hit.ref_id, hit)
    prior = best_by_evidence.get(key)
    if prior is not None:
        if entry[0] > prior[0]:
            best_by_evidence[key] = entry
            heapq.heappush(heap, entry)
    elif len(best_by_evidence) < limit:
        best_by_evidence[key] = entry
        heapq.heappush(heap, entry)
    else:
        while heap:
            heap_key = (heap[0][2].item_id, heap[0][2].entity_id)
            if best_by_evidence.get(heap_key) == heap[0]:
                break
            heapq.heappop(heap)
        if not heap:
            raise SemanticStateError("exact-search evidence heap became empty")
        if entry[0] > heap[0][0]:
            removed = heapq.heappop(heap)
            del best_by_evidence[(removed[2].item_id, removed[2].entity_id)]
            best_by_evidence[key] = entry
            heapq.heappush(heap, entry)
    if len(heap) > max(limit * 2, limit + 64):
        heap[:] = best_by_evidence.values()
        heapq.heapify(heap)


def _scan_exact_page(
    path: Path,
    query: ExactSearchQuery,
    *,
    limit: int = 20,
    max_vectors: int = 50_000,
    after_ref_id: int = 0,
    batch_size: int = 512,
    evidence_mode: bool,
    text_scope: TextEmbeddingScope = "all",
    cancellation_check: Callable[[], None] | None = None,
    diagnostic_item_ids: tuple[str, ...] = (),
    diagnostics: dict[str, object] | None = None,
) -> ExactSearchPage:
    """Shared bounded scan for discovery and concrete-evidence retrieval."""

    _validate_text_scope(query.target_modality, text_scope)
    selected_diagnostic_ids = validate_diagnostic_item_ids(diagnostic_item_ids)
    target_diagnostics = (
        _TargetedSearchDiagnostics(selected_diagnostic_ids, evidence_mode)
        if selected_diagnostic_ids else None
    )
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    if not 1 <= max_vectors <= 10_000_000:
        raise ValueError("max_vectors must be between 1 and 10000000")
    if after_ref_id < 0:
        raise ValueError("after_ref_id cannot be negative")
    if not 1 <= batch_size <= 10_000:
        raise ValueError("batch_size must be between 1 and 10000")
    if cancellation_check is not None:
        cancellation_check()
    query_vector, _ = normalize_vector(query.vector, query.dimensions)
    heap: list[tuple[ExactSearchHeapKey, int, SearchHit]] = []
    best_by_item: dict[str, tuple[ExactSearchHeapKey, int, SearchHit]] = {}
    best_by_evidence: dict[tuple[str, str], tuple[ExactSearchHeapKey, int, SearchHit]] = {}
    max_vectors = remaining_read_limit(max_vectors, vectors=True)
    scanned = 0
    last_ref_id = after_ref_id
    has_more = False
    with semantic_database(path, readonly=True) as connection:
        model_signatures = _search_models(connection, query)
        pairs = _published_model_generations(connection, model_signatures)
        if not pairs:
            empty_page = ExactSearchPage((), 0, None, True)
            if target_diagnostics is not None and diagnostics is not None:
                diagnostics.update(target_diagnostics.export(empty_page))
            return empty_page
        sql = _search_sql(
            query.target_modality,
            len(pairs),
            text_scope=text_scope,
        )
        pair_values = tuple(value for pair in pairs for value in pair)
        cursor = connection.execute(
            sql,
            (*pair_values, after_ref_id, max_vectors + 1),
        )
        while rows := cursor.fetchmany(batch_size):
            if cancellation_check is not None:
                cancellation_check()
            remaining = max_vectors - scanned
            if remaining <= 0:
                has_more = True
                break
            selected_rows = rows[:remaining]
            read_checkpoint(vectors=len(selected_rows))
            page_hits = _exact_search_hits(selected_rows, query, query_vector)
            for hit in page_hits:
                if cancellation_check is not None and scanned % 128 == 0:
                    cancellation_check()
                if target_diagnostics is not None:
                    target_diagnostics.observe(hit)
                if evidence_mode:
                    _retain_exact_evidence_hit(
                        hit,
                        limit=limit,
                        best_by_evidence=best_by_evidence,
                        heap=heap,
                    )
                else:
                    _retain_exact_search_hit(
                        hit,
                        limit=limit,
                        best_by_item=best_by_item,
                        heap=heap,
                    )
                scanned += 1
                last_ref_id = hit.ref_id
            if len(rows) > remaining:
                has_more = True
                break
    selected = best_by_evidence.values() if evidence_mode else best_by_item.values()
    hits = tuple(
        entry[2]
        for entry in sorted(
            selected,
            key=lambda value: value[0].order,
        )
    )
    page = ExactSearchPage(
        hits=hits,
        scanned=scanned,
        next_cursor=last_ref_id if has_more else None,
        complete=not has_more,
    )
    if target_diagnostics is not None and diagnostics is not None:
        diagnostics.update(target_diagnostics.export(page))
    return page


class NativeExactVectorSearch:
    """The existing SQLite exact scan, retained as the ranking oracle."""

    def __init__(self) -> None:
        self._closed = False

    def search_page(
        self, request: VectorSearchRequest, budget: VectorSearchBudget,
        cancelled: Callable[[], None] | None = None,
    ) -> VectorSearchPage:
        if self._closed:
            raise VectorSearchContractError("native vector backend is closed")
        diagnostics: dict[str, object] = {}
        page = _scan_exact_page(
            request.owner_path, request.query,
            limit=budget.limit, max_vectors=budget.max_vectors,
            after_ref_id=request.after_ref_id, batch_size=budget.batch_size,
            evidence_mode=request.evidence_mode, text_scope=request.text_scope,
            diagnostic_item_ids=request.diagnostic_item_ids, diagnostics=diagnostics,
            cancellation_check=cancelled,
        )
        return VectorSearchPage(
            page, "native_exact", request.snapshot_id,
            "complete" if page.complete else "partial", diagnostics=diagnostics,
        )

    def close(self) -> None:
        self._closed = True


def _search_exact_page(
    path: Path,
    query: ExactSearchQuery,
    *,
    limit: int = 20,
    max_vectors: int = 50_000,
    after_ref_id: int = 0,
    batch_size: int = 512,
    evidence_mode: bool,
    text_scope: TextEmbeddingScope = "all",
    cancellation_check: Callable[[], None] | None = None,
    diagnostic_item_ids: tuple[str, ...] = (),
    diagnostics: dict[str, object] | None = None,
    exact_index: ExactIndexHandle | None = None,
    vector_backend: SemanticVectorSearch | None = None,
    backend_diagnostics: dict[str, object] | None = None,
) -> ExactSearchPage:
    """Dispatch the complete request while the owner retains its source fence.

    Only an explicit pre-scan decline permits the native exact fallback. A
    failure, changed artifact, wrong snapshot, or malformed response propagates
    without a retry. Hydration remains in ``resolve_search_hits``.
    """
    from neocortex.persistence.sqlite_immutable import capture_sqlite_read_fence

    if exact_index is not None and vector_backend is not None:
        raise ValueError("select exact_index or vector_backend, not both")
    _validate_text_scope(query.target_modality, text_scope)
    selected_diagnostic_ids = validate_diagnostic_item_ids(diagnostic_item_ids)
    budget = VectorSearchBudget(limit, remaining_read_limit(max_vectors, vectors=True), batch_size)
    if cancellation_check is not None:
        cancellation_check()
    owner_path = Path(path).absolute()
    fence = capture_sqlite_read_fence(owner_path)
    # Filesystem identity captures the whole owner/head snapshot without a
    # database open or copying it for a warm persisted query.
    snapshot_id = hashlib.sha256(repr((str(owner_path), fence)).encode("utf-8")).hexdigest()
    request = VectorSearchRequest(
        query, owner_path, snapshot_id, after_ref_id, evidence_mode,
        text_scope, selected_diagnostic_ids,
    )
    backend = vector_backend
    owns_backend = backend is None
    if backend is None and exact_index is not None:
        from .semantic_exact_index import PersistedExactVectorSearch

        backend = PersistedExactVectorSearch(exact_index, owns_handle=False)
    if backend is None:
        backend = NativeExactVectorSearch()
    fallback_reason: str | None = None
    try:
        result = backend.search_page(request, budget, cancellation_check)
        if isinstance(result, VectorSearchUnavailable):
            if result.phase != "prescan" or not result.reason or not result.backend_id:
                raise VectorSearchContractError("backend decline is not a valid pre-scan outcome")
            if capture_sqlite_read_fence(owner_path) != fence:
                raise SemanticStateError("semantic owner changed before exact fallback; no implicit retry")
            fallback_reason = result.reason
            native = NativeExactVectorSearch()
            try:
                result = native.search_page(request, budget, cancellation_check)
            finally:
                native.close()
        validate_vector_page(result, request, budget)
        if fallback_reason is None and not isinstance(backend, NativeExactVectorSearch):
            # External backends receive the admitted upper bound before
            # execution. Settle the actual count before hydration/next page.
            read_checkpoint(vectors=result.page.scanned)
        if capture_sqlite_read_fence(owner_path) != fence:
            raise SemanticStateError("semantic owner changed during vector query; no implicit retry")
        if cancellation_check is not None:
            cancellation_check()
        if diagnostics is not None:
            diagnostics.update(result.diagnostics)
        if backend_diagnostics is not None:
            backend_diagnostics.update(
                backend_id=result.backend_id, snapshot_id=result.snapshot_id,
                coverage=result.coverage, fallback_reason=fallback_reason or result.fallback_reason,
                scanned=result.page.scanned, next_cursor=result.page.next_cursor,
                complete=result.page.complete,
            )
        return result.page
    finally:
        if owns_backend:
            backend.close()


class _VectorBackendOptions(TypedDict, total=False):
    exact_index: ExactIndexHandle
    vector_backend: SemanticVectorSearch
    backend_diagnostics: dict[str, object]


def _vector_backend_options(
    exact_index: ExactIndexHandle | None,
    vector_backend: SemanticVectorSearch | None,
    backend_diagnostics: dict[str, object] | None,
) -> _VectorBackendOptions:
    options: _VectorBackendOptions = {}
    if exact_index is not None:
        options["exact_index"] = exact_index
    if vector_backend is not None:
        options["vector_backend"] = vector_backend
    if backend_diagnostics is not None:
        options["backend_diagnostics"] = backend_diagnostics
    return options


def search_exact_page(
    path: Path,
    query: ExactSearchQuery,
    *,
    limit: int = 20,
    max_vectors: int = 50_000,
    after_ref_id: int = 0,
    batch_size: int = 512,
    text_scope: TextEmbeddingScope = "all",
    cancellation_check: Callable[[], None] | None = None,
    diagnostic_item_ids: tuple[str, ...] = (),
    diagnostics: dict[str, object] | None = None,
    exact_index: ExactIndexHandle | None = None,
    vector_backend: SemanticVectorSearch | None = None,
    backend_diagnostics: dict[str, object] | None = None,
) -> ExactSearchPage:
    """Scan discovery hits, retaining the best entity per resource item."""

    return _search_exact_page(
        path,
        query,
        limit=limit,
        max_vectors=max_vectors,
        after_ref_id=after_ref_id,
        batch_size=batch_size,
        evidence_mode=False,
        text_scope=text_scope,
        cancellation_check=cancellation_check,
        diagnostic_item_ids=diagnostic_item_ids,
        diagnostics=diagnostics,
        **_vector_backend_options(exact_index, vector_backend, backend_diagnostics),
    )


def search_exact_evidence_page(
    path: Path,
    query: ExactSearchQuery,
    *,
    limit: int = 20,
    max_vectors: int = 50_000,
    after_ref_id: int = 0,
    batch_size: int = 512,
    text_scope: TextEmbeddingScope = "all",
    cancellation_check: Callable[[], None] | None = None,
    diagnostic_item_ids: tuple[str, ...] = (),
    diagnostics: dict[str, object] | None = None,
    exact_index: ExactIndexHandle | None = None,
    vector_backend: SemanticVectorSearch | None = None,
    backend_diagnostics: dict[str, object] | None = None,
) -> ExactSearchPage:
    """Scan concrete evidence while retaining several entities per resource."""

    return _search_exact_page(
        path,
        query,
        limit=limit,
        max_vectors=max_vectors,
        after_ref_id=after_ref_id,
        batch_size=batch_size,
        evidence_mode=True,
        text_scope=text_scope,
        cancellation_check=cancellation_check,
        diagnostic_item_ids=diagnostic_item_ids,
        diagnostics=diagnostics,
        **_vector_backend_options(exact_index, vector_backend, backend_diagnostics),
    )


def load_active_embedding_page(
    path: Path,
    model_signature: str,
    *,
    after_ref_id: int = 0,
    limit: int = 512,
    text_scope: TextEmbeddingScope = "all",
    _generation_id: int | None = None,
) -> ActiveEmbeddingPage:
    """Read current vectors once for bounded prototype/evidence scoring."""

    if after_ref_id < 0:
        raise ValueError("after_ref_id cannot be negative")
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    with semantic_database(path, readonly=True) as connection:
        model = _load_model(connection, model_signature)
        _validate_text_scope(model.modality, text_scope)
        if _generation_id is None:
            pairs = _published_model_generations(connection, (model_signature,))
        else:
            generation = connection.execute(
                """SELECT model_signature,status FROM embedding_generations
                WHERE generation_id=?""",
                (_generation_id,),
            ).fetchone()
            if (
                generation is None
                or str(generation[0]) != model_signature
                or str(generation[1]) != "ready"
            ):
                raise SemanticStateError("pinned embedding generation is unavailable")
            pairs = ((model_signature, _generation_id),)
        if not pairs:
            return ActiveEmbeddingPage((), None, True)
        sql = _search_sql(model.modality, 1, text_scope=text_scope)
        rows = connection.execute(
            sql,
            (model_signature, pairs[0][1], after_ref_id, limit + 1),
        ).fetchall()
    has_more = len(rows) > limit
    selected = rows[:limit]
    records: list[ActiveEmbeddingRecord] = []
    for row in selected:
        dimensions = int(row["dimensions"])
        if dimensions != model.dimensions:
            raise SemanticStateError("persisted vector dimension violates its model")
        raw_provenance = json.loads(str(row["provenance_json"]))
        if not isinstance(raw_provenance, dict):
            raise SemanticStateError("embedding provenance is not a JSON object")
        records.append(
            ActiveEmbeddingRecord(
                ref_id=int(row["ref_id"]),
                entity_id=str(row["entity_id"]),
                item_id=str(row["item_id"]),
                model_signature=model.model_signature,
                vector_space=model.vector_space,
                modality=model.modality,
                vector=normalize_vector(
                    decode_vector(
                        bytes(row["vector_blob"]),
                        dimensions,
                        VectorDType(str(row["vector_dtype"])),
                    ),
                    dimensions,
                )[0],
                generation_id=int(row["generation_id"]),
                provenance=raw_provenance,
            )
        )
    next_cursor = records[-1].ref_id if has_more and records else None
    return ActiveEmbeddingPage(
        records=tuple(records),
        next_cursor=next_cursor,
        complete=not has_more,
    )


def iter_active_embedding_pages(
    path: Path,
    model_signature: str,
    *,
    after_ref_id: int = 0,
    page_size: int = 512,
    text_scope: TextEmbeddingScope = "all",
) -> Iterator[ActiveEmbeddingPage]:
    """Iterate bounded pages without holding a corpus-wide SQLite snapshot."""

    with semantic_database(path, readonly=True) as connection:
        model = _load_model(connection, model_signature)
        _validate_text_scope(model.modality, text_scope)
        pairs = _published_model_generations(connection, (model_signature,))
    if not pairs:
        yield ActiveEmbeddingPage((), None, True)
        return
    generation_id = pairs[0][1]
    cursor = after_ref_id
    while True:
        page = load_active_embedding_page(
            path,
            model_signature,
            after_ref_id=cursor,
            limit=page_size,
            text_scope=text_scope,
            _generation_id=generation_id,
        )
        yield page
        if page.complete:
            break
        if page.next_cursor is None or page.next_cursor <= cursor:
            raise SemanticStateError("active embedding cursor did not advance")
        cursor = page.next_cursor


# endregion [06]


# region [07] Bounded hit resolution


@dataclass(frozen=True, slots=True)
class _ResolvedSearchSource:
    row: sqlite3.Row
    source_revision: dict[str, object]
    source_status: str | None
    published_revision_id: int
    current_revision_id: int | None


def _validate_hit_resolution_request(
    hits: Sequence[SearchHit],
    snippet_chars: int,
) -> None:
    if len(hits) > MAX_WRITE_BATCH:
        raise ValueError(f"at most {MAX_WRITE_BATCH} hits can be resolved per call")
    if not 0 <= snippet_chars <= 4_096:
        raise ValueError("snippet_chars must be between 0 and 4096")


def _load_search_hit_snapshots(
    path: Path,
    member_ids: tuple[int, ...],
) -> dict[int, sqlite3.Row]:
    placeholders = ",".join("?" for _ in member_ids)
    with semantic_database(path, readonly=True) as connection:
        rows = read_rows(connection.execute(
            f"""SELECT member.member_id,member.generation_id,
                member.model_signature,member.entity_kind,member.entity_id,
                member.item_id,model.vector_space,model.modality,
                revision.item_revision_id AS published_revision_id,
                CASE WHEN current_item.active=1
                      AND current_item.source_kind=revision.source_kind
                      AND current_item.source_identity=revision.source_identity
                      AND current_item.identity_version=revision.identity_version
                     THEN current_item.path END AS path,
                revision.source_kind,revision.source_identity,
                revision.provenance_json AS item_provenance_json,
                current_item.provenance_json AS current_item_provenance_json,
                revision.source_revision_json,
                CASE WHEN current_item.active=1
                      AND revision.source_kind=current_item.source_kind
                      AND revision.source_identity=current_item.source_identity
                      AND revision.identity_version=current_item.identity_version
                      AND revision.content_xxh3_128=current_item.content_xxh3_128
                      AND revision.content_bytes=current_item.content_bytes
                      AND revision.content_xxh3_64_guard=
                          current_item.content_xxh3_64_guard
                      AND revision.source_revision_json=
                          current_item.source_revision_json
                     THEN revision.item_revision_id
                     WHEN current_item.active=1 THEN (
                    SELECT candidate.item_revision_id
                    FROM semantic_item_revisions candidate
                    WHERE candidate.item_id=current_item.item_id
                      AND candidate.source_kind=current_item.source_kind
                      AND candidate.source_identity=current_item.source_identity
                      AND candidate.identity_version=current_item.identity_version
                      AND candidate.content_xxh3_128=current_item.content_xxh3_128
                      AND candidate.content_bytes=current_item.content_bytes
                      AND candidate.content_xxh3_64_guard=
                          current_item.content_xxh3_64_guard
                      AND candidate.source_revision_json=
                          current_item.source_revision_json
                    ORDER BY candidate.item_revision_id DESC LIMIT 1
                ) END AS current_revision_id,
                c.section_kind,c.section_id,c.start_char,c.end_char,c.text_zlib,
                c.provenance_json AS section_provenance_json,
                c.content_xxh3_128,c.content_bytes,c.content_xxh3_64_guard
            FROM embedding_generation_members member
            JOIN embedding_models model
              ON model.model_signature=member.model_signature
            JOIN semantic_item_revisions revision
              ON revision.item_revision_id=member.item_revision_id
             AND revision.item_id=member.item_id
            JOIN semantic_items current_item
              ON current_item.item_id=member.item_id
            LEFT JOIN semantic_chunk_revisions c
              ON c.chunk_revision_id=member.chunk_revision_id
            WHERE member.member_id IN ({placeholders})""",
            member_ids,
        ))
    return {int(row["member_id"]): row for row in rows}


def _expected_hit_entity_kind(hit: SearchHit) -> str:
    if hit.modality is EmbeddingModality.TEXT:
        return SemanticEntityKind.TEXT_CHUNK.value
    return SemanticEntityKind.IMAGE_ITEM.value


def _require_consistent_hit_snapshot(
    hit: SearchHit,
    source: sqlite3.Row | None,
) -> sqlite3.Row:
    if (
        source is None
        or int(source["generation_id"]) != hit.generation_id
        or str(source["model_signature"]) != hit.indexed_model_signature
        or str(source["vector_space"]) != hit.vector_space
        or str(source["modality"]) != hit.modality.value
        or str(source["entity_kind"]) != _expected_hit_entity_kind(hit)
        or str(source["entity_id"]) != hit.entity_id
        or str(source["item_id"]) != hit.item_id
    ):
        raise SemanticStateError(
            f"published hit snapshot is unavailable or inconsistent: {hit.ref_id}"
        )
    return source


def _json_object(raw: object, *, error: str) -> dict[str, object]:
    value = json.loads(str(raw))
    if not isinstance(value, dict):
        raise SemanticStateError(error)
    return value


def _search_source_revision_ids(source: sqlite3.Row) -> tuple[int, int | None]:
    published_revision_id = int(source["published_revision_id"])
    current_revision_id = (
        None if source["current_revision_id"] is None else int(source["current_revision_id"])
    )
    return published_revision_id, current_revision_id


def _search_source_status(
    source: sqlite3.Row,
    *,
    published_provenance: dict[str, object],
    published_revision_id: int,
    current_revision_id: int | None,
) -> str | None:
    provenance = published_provenance
    if current_revision_id == published_revision_id:
        provenance = _json_object(
            source["current_item_provenance_json"],
            error="semantic current-item provenance is not a JSON object",
        )
    return next(
        (
            value.strip()
            for name in ("source_status", "analysis_status")
            if isinstance((value := provenance.get(name)), str) and value.strip()
        ),
        None,
    )


def _resolved_search_source(
    hit: SearchHit,
    source: sqlite3.Row | None,
) -> _ResolvedSearchSource:
    selected = _require_consistent_hit_snapshot(hit, source)
    source_revision = _json_object(
        selected["source_revision_json"],
        error="semantic source revision is not a JSON object",
    )
    published_provenance = _json_object(
        selected["item_provenance_json"],
        error="semantic item provenance is not a JSON object",
    )
    published_revision_id, current_revision_id = _search_source_revision_ids(selected)
    return _ResolvedSearchSource(
        row=selected,
        source_revision=source_revision,
        source_status=_search_source_status(
            selected,
            published_provenance=published_provenance,
            published_revision_id=published_revision_id,
            current_revision_id=current_revision_id,
        ),
        published_revision_id=published_revision_id,
        current_revision_id=current_revision_id,
    )


def _resolved_text_search_hit(
    hit: SearchHit,
    source: _ResolvedSearchSource,
    *,
    snippet_chars: int,
    query: str | None = None,
    prepared_query: PreparedLiteralQuery | None = None,
) -> ResolvedSearchHit:
    row = source.row
    section_provenance = _json_object(
        row["section_provenance_json"],
        error="semantic section provenance is not a JSON object",
    )
    section_kind = str(row["section_kind"])
    legacy_locator = section_provenance.get("locator")
    if (
        str(row["source_kind"]) == "video"
        and section_kind == "video_metadata_title"
        and str(row["section_id"]) == "title"
        and isinstance(legacy_locator, dict)
        and legacy_locator.get("kind") == "video_title"
    ):
        # Older Video generations used the owner-native section name.  Keep
        # those bytes searchable as title metadata without re-OCR or reset,
        # while exposing the canonical advisory title contract to consumers.
        section_provenance = {
            **section_provenance,
            "policy_signature": SEMANTIC_TITLE_POLICY,
            "basis": "durable_source_title",
            "mutable_metadata": True,
            "advisory_only": True,
            "legacy_section_kind": "video_metadata_title",
        }
        section_kind = SEMANTIC_TITLE_SECTION_KIND
    fingerprint = _fingerprint_from_row(row)
    read_checkpoint()
    text = _decode_chunk_text(bytes(row["text_zlib"]), fingerprint)
    read_checkpoint()
    prepared = prepared_query or prepare_literal_query(query or "")
    analysis = analyze_literal_text(
        text, prepared, snippet_chars=snippet_chars, include_support=query is not None,
    )
    snippet, excerpt = query_centered_snippet(
        text, query, max_chars=snippet_chars, prepared=prepared, analysis=analysis,
    )
    read_checkpoint()
    if query is not None:
        section_provenance = {**section_provenance, "retrieval_excerpt": excerpt}
        section_provenance["query_support"] = query_term_support(
            query, text, basis="scored_chunk", prepared=prepared, analysis=analysis,
        )
        read_checkpoint()
        section_provenance["snippet_query_support"] = query_term_support(
            query, snippet or "", basis="scored_chunk_window",
            prepared=prepared,
            analysis=analysis if snippet is text else None,
        )
        read_checkpoint()
    return ResolvedSearchHit(
        hit=hit,
        path=None if row["path"] is None else str(row["path"]),
        source_kind=str(row["source_kind"]),
        source_identity=str(row["source_identity"]),
        section_kind=section_kind,
        section_id=str(row["section_id"]),
        start_char=int(row["start_char"]),
        end_char=int(row["end_char"]),
        snippet=snippet,
        source_status=source.source_status,
        source_revision=source.source_revision,
        section_provenance=section_provenance,
        published_revision_id=source.published_revision_id,
        current_revision_id=source.current_revision_id,
    )


def _resolved_image_search_hit(
    hit: SearchHit,
    source: _ResolvedSearchSource,
) -> ResolvedSearchHit:
    row = source.row
    return ResolvedSearchHit(
        hit=hit,
        path=None if row["path"] is None else str(row["path"]),
        source_kind=str(row["source_kind"]),
        source_identity=str(row["source_identity"]),
        section_kind=None,
        section_id=None,
        start_char=None,
        end_char=None,
        snippet=None,
        source_status=source.source_status,
        source_revision=source.source_revision,
        published_revision_id=source.published_revision_id,
        current_revision_id=source.current_revision_id,
    )


def _resolved_search_hit(
    hit: SearchHit,
    source: sqlite3.Row | None,
    *,
    snippet_chars: int,
    query: str | None = None,
    prepared_query: PreparedLiteralQuery | None = None,
) -> ResolvedSearchHit:
    # Row admission precedes detached hydration. Keep cancellation/deadline
    # live across that work too, including construction of the final result.
    read_checkpoint()
    resolved_source = _resolved_search_source(hit, source)
    if hit.modality is EmbeddingModality.TEXT:
        resolved = _resolved_text_search_hit(
            hit,
            resolved_source,
            snippet_chars=snippet_chars,
            query=query,
            prepared_query=prepared_query,
        )
    else:
        resolved = _resolved_image_search_hit(hit, resolved_source)
    read_checkpoint()
    return resolved


def resolve_search_hits(
    path: Path,
    hits: Sequence[SearchHit],
    *,
    snippet_chars: int = 240,
    query: str | None = None,
) -> tuple[ResolvedSearchHit, ...]:
    """Resolve immutable evidence with an identity-safe locator and DB currency.

    ``active`` is deliberately not filtered: the published head remains the
    visibility contract until a successor is atomically published.
    """

    _validate_hit_resolution_request(hits, snippet_chars)
    if not hits:
        return ()
    member_ids = tuple(dict.fromkeys(hit.ref_id for hit in hits))
    snapshots = _load_search_hit_snapshots(path, member_ids)
    prepared_query = prepare_literal_query(query or "")
    return tuple(
        _resolved_search_hit(
            hit,
            snapshots.get(hit.ref_id),
            snippet_chars=snippet_chars,
            query=query,
            prepared_query=prepared_query,
        )
        for hit in hits
    )


# endregion [07]
