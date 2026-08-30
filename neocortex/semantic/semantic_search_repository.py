"""Bounded exact-vector search and semantic-hit resolution repository."""

from __future__ import annotations
import heapq
import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .semantic_item_repository import _decode_chunk_text
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
from .semantic_schema import SemanticStateError, semantic_database
from .semantic_sources import SEMANTIC_TITLE_SECTION_KIND

TextEmbeddingScope = Literal["all", "content", "title"]

_VECTORIZED_SEARCH_MIN_ROWS = 8

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
    if modality is EmbeddingModality.TEXT:
        scope_clause = {
            "all": "",
            "content": (f" AND c.section_kind<>'{SEMANTIC_TITLE_SECTION_KIND}'"),
            "title": f" AND c.section_kind='{SEMANTIC_TITLE_SECTION_KIND}'",
        }[text_scope]
        return f"""WITH selected(model_signature,generation_id) AS
            (VALUES {selected})
        SELECT e.member_id AS ref_id,e.entity_id,i.item_id,
            e.model_signature,m.vector_space,m.modality,e.generation_id,
            e.provenance_json,p.vector_blob,p.dimensions,p.vector_dtype
        FROM selected s
        JOIN embedding_generation_members e
          ON e.model_signature=s.model_signature
         AND e.generation_id=s.generation_id
        JOIN embedding_models m ON m.model_signature=e.model_signature
        JOIN vector_payloads p ON p.payload_id=e.payload_id
        JOIN semantic_chunk_revisions c
          ON c.chunk_revision_id=e.chunk_revision_id
        JOIN semantic_item_revisions i
          ON i.item_revision_id=e.item_revision_id
        JOIN embedding_generations g ON g.generation_id=e.generation_id
        WHERE e.member_id>? AND e.entity_kind='text_chunk' AND g.status='ready'
          AND e.content_xxh3_128=c.content_xxh3_128
          AND e.content_bytes=c.content_bytes
          AND e.content_xxh3_64_guard=c.content_xxh3_64_guard
          {scope_clause}
        ORDER BY e.member_id LIMIT ?"""
    return f"""WITH selected(model_signature,generation_id) AS
        (VALUES {selected})
    SELECT e.member_id AS ref_id,e.entity_id,i.item_id,
        e.model_signature,m.vector_space,m.modality,e.generation_id,
        e.provenance_json,p.vector_blob,p.dimensions,p.vector_dtype
    FROM selected s
    JOIN embedding_generation_members e
      ON e.model_signature=s.model_signature
     AND e.generation_id=s.generation_id
    JOIN embedding_models m ON m.model_signature=e.model_signature
    JOIN vector_payloads p ON p.payload_id=e.payload_id
    JOIN semantic_item_revisions i ON i.item_revision_id=e.item_revision_id
    JOIN embedding_generations g ON g.generation_id=e.generation_id
    WHERE e.member_id>? AND e.entity_kind='image_item' AND g.status='ready'
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
    return _numpy_exact_search_hits(rows, query, query_vector, numpy)


def _retain_exact_search_hit(
    hit: SearchHit,
    *,
    limit: int,
    best_by_item: dict[str, tuple[float, int, SearchHit]],
    heap: list[tuple[float, int, SearchHit]],
) -> None:
    entry = (hit.score, hit.ref_id, hit)
    prior = best_by_item.get(hit.item_id)
    if prior is not None:
        if entry[:2] > prior[:2]:
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
        if entry[:2] > heap[0][:2]:
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
    best_by_evidence: dict[tuple[str, str], tuple[float, int, SearchHit]],
    heap: list[tuple[float, int, SearchHit]],
) -> None:
    """Retain one best vector per concrete entity, not per resource item."""

    key = (hit.item_id, hit.entity_id)
    entry = (hit.score, hit.ref_id, hit)
    prior = best_by_evidence.get(key)
    if prior is not None:
        if entry[:2] > prior[:2]:
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
        if entry[:2] > heap[0][:2]:
            removed = heapq.heappop(heap)
            del best_by_evidence[(removed[2].item_id, removed[2].entity_id)]
            best_by_evidence[key] = entry
            heapq.heappush(heap, entry)
    if len(heap) > max(limit * 2, limit + 64):
        heap[:] = best_by_evidence.values()
        heapq.heapify(heap)


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
) -> ExactSearchPage:
    """Shared bounded scan for discovery and concrete-evidence retrieval."""

    _validate_text_scope(query.target_modality, text_scope)
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
    heap: list[tuple[float, int, SearchHit]] = []
    best_by_item: dict[str, tuple[float, int, SearchHit]] = {}
    best_by_evidence: dict[tuple[str, str], tuple[float, int, SearchHit]] = {}
    scanned = 0
    last_ref_id = after_ref_id
    has_more = False
    with semantic_database(path, readonly=True) as connection:
        model_signatures = _search_models(connection, query)
        pairs = _published_model_generations(connection, model_signatures)
        if not pairs:
            return ExactSearchPage((), 0, None, True)
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
            page_hits = _exact_search_hits(selected_rows, query, query_vector)
            for hit in page_hits:
                if cancellation_check is not None and scanned % 128 == 0:
                    cancellation_check()
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
            key=lambda value: (
                -value[0],
                value[2].item_id,
                value[2].entity_id,
                value[2].indexed_model_signature,
            ),
        )
    )
    return ExactSearchPage(
        hits=hits,
        scanned=scanned,
        next_cursor=last_ref_id if has_more else None,
        complete=not has_more,
    )


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
        rows = connection.execute(
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
        ).fetchall()
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
) -> ResolvedSearchHit:
    row = source.row
    section_provenance = _json_object(
        row["section_provenance_json"],
        error="semantic section provenance is not a JSON object",
    )
    fingerprint = _fingerprint_from_row(row)
    text = _decode_chunk_text(bytes(row["text_zlib"]), fingerprint)
    snippet = text[:snippet_chars] if snippet_chars else None
    return ResolvedSearchHit(
        hit=hit,
        path=None if row["path"] is None else str(row["path"]),
        source_kind=str(row["source_kind"]),
        source_identity=str(row["source_identity"]),
        section_kind=str(row["section_kind"]),
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
) -> ResolvedSearchHit:
    resolved_source = _resolved_search_source(hit, source)
    if hit.modality is EmbeddingModality.TEXT:
        return _resolved_text_search_hit(
            hit,
            resolved_source,
            snippet_chars=snippet_chars,
        )
    return _resolved_image_search_hit(hit, resolved_source)


def resolve_search_hits(
    path: Path,
    hits: Sequence[SearchHit],
    *,
    snippet_chars: int = 240,
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
    return tuple(
        _resolved_search_hit(
            hit,
            snapshots.get(hit.ref_id),
            snippet_chars=snippet_chars,
        )
        for hit in hits
    )


# endregion [07]
