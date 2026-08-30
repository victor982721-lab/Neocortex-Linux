"""Shared limits, validation and row conversion for semantic repositories."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import itertools
import json
import sqlite3
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

from .semantic_models import (
    ContentFingerprint,
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRole,
    SemanticItem,
    TextChunk,
    VectorDType,
)
from .semantic_schema import SemanticStateError, semantic_database

MAX_WRITE_BATCH = 500
MAX_EVIDENCE_ENTITIES_PER_PUBLICATION = MAX_WRITE_BATCH
MAX_EVIDENCE_ROWS_PER_PUBLICATION = MAX_WRITE_BATCH * 32
MAX_STORED_CHUNK_BYTES = 4 * 1024 * 1024
MAX_ERROR_CHARS = 8_000


class StaleEmbeddingJobError(SemanticStateError):
    """Raised when source content changed after a job was queued."""


# region [02] Shared validation and row conversion


def _now(value: int | None) -> int:
    selected = time.time_ns() if value is None else value
    if selected < 0:
        raise ValueError("timestamps cannot be negative")
    return selected


def _check_batch_size(batch_size: int) -> None:
    if not 1 <= batch_size <= MAX_WRITE_BATCH:
        raise ValueError(f"batch_size must be between 1 and {MAX_WRITE_BATCH}")


def _batches(values: Iterable[str], batch_size: int) -> Iterator[tuple[str, ...]]:
    iterator = iter(values)
    while batch := tuple(itertools.islice(iterator, batch_size)):
        yield batch


def _item_batches(
    values: Iterable[SemanticItem],
    batch_size: int,
) -> Iterator[tuple[SemanticItem, ...]]:
    iterator = iter(values)
    while batch := tuple(itertools.islice(iterator, batch_size)):
        yield batch


def _chunk_batches(
    values: Iterable[TextChunk],
    batch_size: int,
) -> Iterator[tuple[TextChunk, ...]]:
    iterator = iter(values)
    while batch := tuple(itertools.islice(iterator, batch_size)):
        yield batch


def _fingerprint_from_row(row: sqlite3.Row) -> ContentFingerprint:
    return ContentFingerprint(
        xxh3_128=str(row["content_xxh3_128"]),
        byte_count=int(row["content_bytes"]),
        xxh3_64_guard=str(row["content_xxh3_64_guard"]),
    )


def _model_from_row(row: sqlite3.Row) -> EmbeddingModelSpec:
    raw_roles = json.loads(str(row["supported_roles_json"]))
    raw_provenance = json.loads(str(row["provenance_json"]))
    if not isinstance(raw_roles, list) or not isinstance(raw_provenance, dict):
        raise SemanticStateError("invalid persisted model JSON")
    return EmbeddingModelSpec(
        model_signature=str(row["model_signature"]),
        vector_space=str(row["vector_space"]),
        modality=EmbeddingModality(str(row["modality"])),
        model_id=str(row["model_id"]),
        model_version=str(row["model_version"]),
        dimensions=int(row["dimensions"]),
        provider=str(row["provider"]),
        supported_roles=tuple(EmbeddingRole(str(value)) for value in raw_roles),
        vector_dtype=VectorDType(str(row["vector_dtype"])),
        normalization=str(row["normalization"]),
        distance=str(row["distance"]),
        provenance=raw_provenance,
    )


def _load_model(
    connection: sqlite3.Connection, model_signature: str
) -> EmbeddingModelSpec:
    row = connection.execute(
        "SELECT * FROM embedding_models WHERE model_signature=? AND active=1",
        (model_signature,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown active embedding model {model_signature!r}")
    return _model_from_row(row)


def load_embedding_model(path: Path, model_signature: str) -> EmbeddingModelSpec:
    """Load the exact versioned model contract needed by a worker."""

    with semantic_database(path, readonly=True) as connection:
        return _load_model(connection, model_signature)


def load_semantic_item(
    path: Path,
    item_id: str,
    *,
    include_inactive: bool = False,
) -> SemanticItem:
    """Load current identity/revision metadata for a final source check."""

    if not item_id.strip():
        raise ValueError("item_id cannot be blank")
    with semantic_database(path, readonly=True) as connection:
        row = connection.execute(
            "SELECT * FROM semantic_items WHERE item_id=? AND (?=1 OR active=1)",
            (item_id, int(include_inactive)),
        ).fetchone()
    if row is None:
        raise KeyError(f"unknown or inactive semantic item {item_id!r}")
    provenance = json.loads(str(row["provenance_json"]))
    source_revision = json.loads(str(row["source_revision_json"]))
    if not isinstance(provenance, dict) or not isinstance(source_revision, dict):
        raise SemanticStateError("semantic item metadata is not a JSON object")
    return SemanticItem(
        item_id=str(row["item_id"]),
        source_kind=str(row["source_kind"]),
        source_identity=str(row["source_identity"]),
        identity_version=str(row["identity_version"]),
        fingerprint=_fingerprint_from_row(row),
        path=None if row["path"] is None else str(row["path"]),
        provenance=provenance,
        source_revision=source_revision,
    )


def _same_fingerprint(row: sqlite3.Row, fingerprint: ContentFingerprint) -> bool:
    return (
        str(row["content_xxh3_128"]) == fingerprint.xxh3_128
        and int(row["content_bytes"]) == fingerprint.byte_count
        and str(row["content_xxh3_64_guard"]) == fingerprint.xxh3_64_guard
    )


# endregion [02]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.semantic_repository_common")
