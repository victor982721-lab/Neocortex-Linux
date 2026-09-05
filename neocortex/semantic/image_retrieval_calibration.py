"""Durable, local calibration for CLIP image retrieval.

Calibration is deliberately a small owner-local metadata record rather than a
second index.  It is derived from a caller-supplied labelled fixture, bound to
the currently published image generation, and consumed only when every part of
that contract still matches.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .semantic_backends import EmbeddingBackend
from .semantic_config import SEMANTIC_PIPELINE_VERSION, clip_image_model, clip_text_model
from .semantic_models import EmbeddingModality
from .semantic_schema import semantic_database
from .semantic_ontology import expand_domain_query
from .semantic_service_contracts import ImageRetrievalCalibration

CALIBRATION_SCHEMA = "neocortex-image-retrieval-calibration/v1"
CALIBRATION_METADATA_KEY = "image_retrieval_calibration.v1"
MAX_CALIBRATION_DATASET_BYTES = 256 * 1024
MIN_CALIBRATION_SAMPLE_ITEMS = 20
MAX_CALIBRATION_SAMPLE_ITEMS = 50
MIN_CALIBRATION_QUERIES_PER_POLARITY = 3


class ImageCalibrationError(RuntimeError):
    """A calibration set cannot establish a safe retrieval floor."""


@dataclass(frozen=True, slots=True)
class _CalibrationQuery:
    query: str
    expected_item_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImageCalibrationEvidence:
    """Compact evidence retained by the CLI without persisting raw queries."""

    dataset_digest: str
    sample_item_ids: tuple[str, ...]
    positive_floor: float
    negative_ceiling: float
    generated_ns: int
    generation_id: int


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _dataset_digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _query_values(value: object, *, name: str, positive: bool) -> tuple[_CalibrationQuery, ...]:
    if not isinstance(value, list):
        raise ImageCalibrationError(f"calibration {name} must be a JSON array")
    if len(value) < MIN_CALIBRATION_QUERIES_PER_POLARITY:
        raise ImageCalibrationError(
            f"calibration {name} requires at least "
            f"{MIN_CALIBRATION_QUERIES_PER_POLARITY} queries"
        )
    if len(value) > 1_000:
        raise ImageCalibrationError(f"calibration {name} exceeds the query bound")
    result: list[_CalibrationQuery] = []
    for entry in value:
        if isinstance(entry, str):
            query = entry
            expected: tuple[str, ...] = ()
        elif isinstance(entry, Mapping):
            query = entry.get("query")
            raw_expected = entry.get("expected_item_ids", ())
            if not isinstance(raw_expected, list) or any(
                not isinstance(item_id, str) or not item_id.strip() for item_id in raw_expected
            ):
                raise ImageCalibrationError(
                    f"calibration {name} expected_item_ids must be non-empty strings"
                )
            expected = tuple(dict.fromkeys(item_id.strip() for item_id in raw_expected))
        else:
            raise ImageCalibrationError(f"calibration {name} entries must be strings or objects")
        if not isinstance(query, str) or not query.strip() or len(query) > 4_096:
            raise ImageCalibrationError(f"calibration {name} contains an invalid query")
        if positive and not expected:
            raise ImageCalibrationError(
                "positive calibration queries must declare expected_item_ids"
            )
        result.append(_CalibrationQuery(query.strip(), expected))
    return tuple(result)


def load_calibration_dataset(
    path: Path,
) -> tuple[
    Mapping[str, object],
    tuple[_CalibrationQuery, ...],
    tuple[_CalibrationQuery, ...],
]:
    """Read and validate a bounded labelled calibration description."""

    if (
        not path.is_file()
        or path.is_symlink()
        or path.stat().st_size > MAX_CALIBRATION_DATASET_BYTES
    ):
        raise ImageCalibrationError("calibration dataset is not a bounded regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ImageCalibrationError(f"cannot read calibration dataset: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != CALIBRATION_SCHEMA:
        raise ImageCalibrationError(f"calibration dataset schema must be {CALIBRATION_SCHEMA}")
    positive = _query_values(
        payload.get("positive_queries"),
        name="positive_queries",
        positive=True,
    )
    negative = _query_values(
        payload.get("negative_queries"),
        name="negative_queries",
        positive=False,
    )
    return payload, positive, negative


def _published_image_contract(database: Path) -> tuple[int, str] | None:
    """Return the current published image generation and exact processing contract."""

    if not database.is_file():
        return None
    model_signature = clip_image_model().model_signature
    try:
        with semantic_database(database, readonly=True) as connection:
            row = connection.execute(
                """SELECT g.generation_id,g.processing_signature
                FROM published_embedding_heads h
                JOIN embedding_generations g ON g.generation_id=h.generation_id
                WHERE h.model_signature=? AND g.model_signature=? AND g.status='ready'""",
                (model_signature, model_signature),
            ).fetchone()
    except (OSError, RuntimeError, sqlite3.DatabaseError, TypeError, ValueError):
        return None
    if row is None:
        return None
    processing_signature = str(row[1])
    if not processing_signature.strip():
        return None
    return int(row[0]), processing_signature


def current_image_processing_signature(database: Path) -> str | None:
    """Expose the published image generation contract for retrieval guards."""

    contract = _published_image_contract(database)
    return None if contract is None else contract[1]


def _active_image_item_ids(database: Path) -> tuple[str, ...]:
    if not database.is_file():
        return ()
    model_signature = clip_image_model().model_signature
    try:
        with semantic_database(database, readonly=True) as connection:
            rows = connection.execute(
                """SELECT m.item_id
                FROM embedding_generation_members m
                JOIN published_embedding_heads h ON h.generation_id=m.generation_id
                JOIN semantic_items i ON i.item_id=m.item_id
                WHERE h.model_signature=? AND m.model_signature=?
                  AND m.entity_kind='image_item' AND i.active=1
                ORDER BY m.item_id""",
                (model_signature, model_signature),
            ).fetchall()
    except (OSError, RuntimeError, sqlite3.DatabaseError, TypeError, ValueError):
        return ()
    return tuple(dict.fromkeys(str(row[0]) for row in rows))


def _sample_item_ids(
    payload: Mapping[str, object],
    active_item_ids: Sequence[str],
) -> tuple[str, ...]:
    raw = payload.get("sample_item_ids")
    if raw is None:
        selected = tuple(active_item_ids[:MAX_CALIBRATION_SAMPLE_ITEMS])
    elif isinstance(raw, list) and all(
        isinstance(item_id, str) and item_id.strip() for item_id in raw
    ):
        selected = tuple(dict.fromkeys(item_id.strip() for item_id in raw))
    else:
        raise ImageCalibrationError("calibration sample_item_ids must be non-empty strings")
    if not MIN_CALIBRATION_SAMPLE_ITEMS <= len(selected) <= MAX_CALIBRATION_SAMPLE_ITEMS:
        raise ImageCalibrationError(
            "calibration sample must contain between "
            f"{MIN_CALIBRATION_SAMPLE_ITEMS} and {MAX_CALIBRATION_SAMPLE_ITEMS} image items"
        )
    active = set(active_item_ids)
    missing = tuple(item_id for item_id in selected if item_id not in active)
    if missing:
        raise ImageCalibrationError(
            "calibration sample references unpublished image items: " + ", ".join(missing[:3])
        )
    return selected


def _validate_score(value: float, *, label: str) -> float:
    if not math.isfinite(value) or not -1.0 <= value <= 1.0:
        raise ImageCalibrationError(f"{label} score is outside cosine bounds")
    return value


def measure_image_retrieval_calibration(
    database: Path,
    dataset_path: Path,
    *,
    cache: Path,
    local_files_only: bool,
    threads: int | None,
    backend_factory: Callable[..., EmbeddingBackend],
    max_vectors: int = 500_000,
    cancellation_check: Callable[[], None] | None = None,
) -> tuple[ImageRetrievalCalibration, ImageCalibrationEvidence]:
    """Measure a floor from labelled local queries against the current image head."""

    payload, positive_queries, negative_queries = load_calibration_dataset(dataset_path)
    contract = _published_image_contract(database)
    if contract is None:
        raise ImageCalibrationError("no ready published image generation is available")
    generation_id, processing_signature = contract
    active_item_ids = _active_image_item_ids(database)
    sample_item_ids = _sample_item_ids(payload, active_item_ids)
    sample_set = set(sample_item_ids)

    # Lazy import avoids a module cycle: the search service consumes the
    # persisted calibration and this measurement reuses its exact query/rank
    # implementation.
    from .semantic_search_service import query_vector, semantic_ranking

    query_model = clip_text_model()
    indexed_model = clip_image_model()

    def scores_for(query: str) -> tuple[object, ...]:
        if cancellation_check is not None:
            cancellation_check()
        vector = query_vector(
            query_model,
            expand_domain_query(query),
            cache_dir=cache,
            local_files_only=local_files_only,
            threads=threads,
            backend_factory=backend_factory,
            cancellation_check=cancellation_check,
        )
        ranking = semantic_ranking(
            database,
            name="semantic_image_calibration",
            query_model=query_model,
            target_modality=EmbeddingModality.IMAGE,
            vector=vector,
            indexed_model_signatures=(indexed_model.model_signature,),
            limit=min(1_000, max_vectors),
            max_vectors=max_vectors,
            cancellation_check=cancellation_check,
        )
        return tuple(hit for hit in ranking.hits if hit.item_id in sample_set)

    positive_scores: list[float] = []
    for entry in positive_queries:
        hits = scores_for(entry.query)
        expected = set(entry.expected_item_ids)
        values = [float(hit.score) for hit in hits if hit.item_id in expected]
        if not values:
            raise ImageCalibrationError(
                f"positive query has no scored expected image: {entry.query!r}"
            )
        positive_scores.append(_validate_score(max(values), label="positive"))

    negative_scores: list[float] = []
    for entry in negative_queries:
        hits = scores_for(entry.query)
        values = [float(hit.score) for hit in hits]
        negative_scores.append(
            _validate_score(max(values, default=-1.0), label="negative")
        )
    positive_floor = min(positive_scores)
    negative_ceiling = max(negative_scores)
    if positive_floor <= negative_ceiling:
        raise ImageCalibrationError(
            "calibration queries do not separate positive and negative image scores"
        )
    minimum_score = (positive_floor + negative_ceiling) / 2.0
    digest = _dataset_digest(payload)
    calibration = ImageRetrievalCalibration(
        calibration_signature=f"image-retrieval-calibration-v1:sha256={digest}",
        query_model_signature=query_model.model_signature,
        indexed_model_signature=indexed_model.model_signature,
        pipeline=SEMANTIC_PIPELINE_VERSION,
        backend="fastembed",
        minimum_score=minimum_score,
        positive_queries=len(positive_queries),
        negative_queries=len(negative_queries),
        sample_items=len(sample_item_ids),
        indexed_processing_signature=processing_signature,
    )
    evidence = ImageCalibrationEvidence(
        dataset_digest=digest,
        sample_item_ids=sample_item_ids,
        positive_floor=positive_floor,
        negative_ceiling=negative_ceiling,
        generated_ns=time.time_ns(),
        generation_id=generation_id,
    )
    return calibration, evidence


def persist_image_retrieval_calibration(
    database: Path,
    calibration: ImageRetrievalCalibration,
    evidence: ImageCalibrationEvidence,
) -> None:
    """Atomically replace the owner-local calibration metadata record."""

    if calibration.indexed_processing_signature is None:
        raise ImageCalibrationError("durable image calibration needs a processing signature")
    if calibration.sample_items != len(evidence.sample_item_ids):
        raise ImageCalibrationError("calibration sample_items does not match its evidence")
    if not (
        math.isfinite(evidence.positive_floor)
        and math.isfinite(evidence.negative_ceiling)
        and evidence.positive_floor > evidence.negative_ceiling
    ):
        raise ImageCalibrationError("calibration score evidence is not separable")
    expected_floor = (evidence.positive_floor + evidence.negative_ceiling) / 2.0
    if not math.isclose(calibration.minimum_score, expected_floor, rel_tol=0.0, abs_tol=1e-12):
        raise ImageCalibrationError("calibration minimum_score does not match its evidence")
    payload = {
        "schema": CALIBRATION_SCHEMA,
        "calibration_signature": calibration.calibration_signature,
        "query_model_signature": calibration.query_model_signature,
        "indexed_model_signature": calibration.indexed_model_signature,
        "indexed_processing_signature": calibration.indexed_processing_signature,
        "pipeline": calibration.pipeline,
        "backend": calibration.backend,
        "minimum_score": calibration.minimum_score,
        "positive_queries": calibration.positive_queries,
        "negative_queries": calibration.negative_queries,
        "sample_items": calibration.sample_items,
        "dataset_digest": evidence.dataset_digest,
        "sample_item_ids": list(evidence.sample_item_ids),
        "positive_floor": evidence.positive_floor,
        "negative_ceiling": evidence.negative_ceiling,
        "generated_ns": evidence.generated_ns,
        "generation_id": evidence.generation_id,
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    with semantic_database(database) as connection:
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (CALIBRATION_METADATA_KEY, encoded),
        )


def load_image_retrieval_calibration(database: Path) -> ImageRetrievalCalibration | None:
    """Load only a complete, structurally valid durable calibration record."""

    if not database.is_file():
        return None
    try:
        with semantic_database(database, readonly=True) as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key=?",
                (CALIBRATION_METADATA_KEY,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict) or payload.get("schema") != CALIBRATION_SCHEMA:
            return None
        return ImageRetrievalCalibration(
            calibration_signature=str(payload["calibration_signature"]),
            query_model_signature=str(payload["query_model_signature"]),
            indexed_model_signature=str(payload["indexed_model_signature"]),
            pipeline=str(payload["pipeline"]),
            backend=str(payload["backend"]),
            minimum_score=float(payload["minimum_score"]),
            positive_queries=int(payload["positive_queries"]),
            negative_queries=int(payload["negative_queries"]),
            sample_items=int(payload["sample_items"]),
            indexed_processing_signature=str(payload["indexed_processing_signature"]),
        )
    except (
        OSError,
        RuntimeError,
        sqlite3.DatabaseError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None


__all__ = [
    "CALIBRATION_METADATA_KEY",
    "CALIBRATION_SCHEMA",
    "ImageCalibrationError",
    "ImageCalibrationEvidence",
    "current_image_processing_signature",
    "load_calibration_dataset",
    "load_image_retrieval_calibration",
    "measure_image_retrieval_calibration",
    "persist_image_retrieval_calibration",
]
