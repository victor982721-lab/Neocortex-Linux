"""Embedding-model, semantic-item and bounded text-chunk repository."""

from __future__ import annotations

import json
import sqlite3
import time
import zlib
from collections.abc import Iterable, Sequence
from pathlib import Path

from .semantic_models import (
    ContentFingerprint,
    EmbeddingModelSpec,
    SemanticItem,
    TextChunk,
    canonical_json,
    fingerprint_text,
)
from .semantic_lineage_repository import (
    _record_chunk_materialization,
    _record_chunk_refresh_publication,
)
from .semantic_repository_common import (
    MAX_STORED_CHUNK_BYTES,
    MAX_WRITE_BATCH,
    _check_batch_size,
    _chunk_batches,
    _item_batches,
    _model_from_row,
    _now,
    _same_fingerprint,
)
from .semantic_schema import SemanticStateError, semantic_database

# region [03] Vector spaces, models and source refreshes


def register_embedding_model(
    path: Path,
    model: EmbeddingModelSpec,
    *,
    allow_test_provider: bool = False,
) -> None:
    """Register an immutable model and enforce vector-space compatibility."""

    if model.provider == "test-deterministic" and not allow_test_provider:
        raise ValueError("test-deterministic models require explicit test authorization")
    now_ns = time.time_ns()
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        space = connection.execute(
            "SELECT dimensions,distance,normalization FROM vector_spaces WHERE vector_space=?",
            (model.vector_space,),
        ).fetchone()
        if space is None:
            connection.execute(
                "INSERT INTO vector_spaces(vector_space,dimensions,distance,normalization,"
                "created_ns) VALUES(?,?,?,?,?)",
                (
                    model.vector_space,
                    model.dimensions,
                    model.distance,
                    model.normalization,
                    now_ns,
                ),
            )
        elif (
            int(space["dimensions"]) != model.dimensions
            or str(space["distance"]) != model.distance
            or str(space["normalization"]) != model.normalization
        ):
            raise ValueError(
                f"model {model.model_signature!r} is incompatible with existing "
                f"vector space {model.vector_space!r}"
            )
        existing = connection.execute(
            "SELECT * FROM embedding_models WHERE model_signature=?",
            (model.model_signature,),
        ).fetchone()
        if existing is not None:
            if _model_from_row(existing) != model:
                raise ValueError(
                    f"model signature {model.model_signature!r} is already bound to "
                    "different metadata"
                )
            return
        connection.execute(
            """INSERT INTO embedding_models(
                model_signature,vector_space,modality,model_id,model_version,
                dimensions,provider,supported_roles_json,vector_dtype,
                normalization,distance,provenance_json,created_ns)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                model.model_signature,
                model.vector_space,
                model.modality.value,
                model.model_id,
                model.model_version,
                model.dimensions,
                model.provider,
                json.dumps(
                    [role.value for role in model.supported_roles],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                model.vector_dtype.value,
                model.normalization,
                model.distance,
                canonical_json(model.provenance),
                now_ns,
            ),
        )


def _upsert_item(
    connection: sqlite3.Connection,
    item: SemanticItem,
    *,
    refresh_token: str | None,
    updated_ns: int,
    invalidate_text_on_fingerprint_change: bool,
) -> None:
    prior = connection.execute(
        "SELECT content_xxh3_128,content_bytes,content_xxh3_64_guard "
        "FROM semantic_items WHERE item_id=?",
        (item.item_id,),
    ).fetchone()
    try:
        connection.execute(
            """INSERT INTO semantic_items(
                item_id,source_kind,source_identity,identity_version,path,
                content_xxh3_128,content_bytes,content_xxh3_64_guard,
                provenance_json,source_revision_json,refresh_token,active,updated_ns)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?)
            ON CONFLICT(item_id) DO UPDATE SET
                source_kind=excluded.source_kind,
                source_identity=excluded.source_identity,
                identity_version=excluded.identity_version,
                path=excluded.path,
                content_xxh3_128=excluded.content_xxh3_128,
                content_bytes=excluded.content_bytes,
                content_xxh3_64_guard=excluded.content_xxh3_64_guard,
                provenance_json=excluded.provenance_json,
                source_revision_json=excluded.source_revision_json,
                refresh_token=COALESCE(excluded.refresh_token,semantic_items.refresh_token),
                active=1,
                updated_ns=excluded.updated_ns""",
            (
                item.item_id,
                item.source_kind,
                item.source_identity,
                item.identity_version,
                item.path,
                item.fingerprint.xxh3_128,
                item.fingerprint.byte_count,
                item.fingerprint.xxh3_64_guard,
                canonical_json(item.provenance),
                canonical_json(item.source_revision),
                refresh_token,
                updated_ns,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(
            f"source identity {item.source_kind!r}/{item.source_identity!r} "
            "is already assigned to another item"
        ) from exc
    if prior is not None and not _same_fingerprint(prior, item.fingerprint):
        if invalidate_text_on_fingerprint_change:
            connection.execute(
                "UPDATE text_chunks SET active=0,updated_ns=? WHERE item_id=? AND active=1",
                (updated_ns, item.item_id),
            )
            connection.execute(
                "UPDATE semantic_evidence SET active=0,updated_ns=? WHERE item_id=? AND active=1",
                (updated_ns, item.item_id),
            )
        else:
            connection.execute(
                """UPDATE semantic_evidence SET active=0,updated_ns=?
                WHERE item_id=? AND source_entity_id=? AND active=1""",
                (updated_ns, item.item_id, item.item_id),
            )


def upsert_semantic_item(
    path: Path,
    item: SemanticItem,
    *,
    refresh_token: str | None = None,
    updated_ns: int | None = None,
) -> None:
    """Upsert one source, deactivating old chunks if its content changed."""

    if refresh_token is not None and not refresh_token.strip():
        raise ValueError("refresh_token cannot be blank")
    with semantic_database(path) as connection:
        _upsert_item(
            connection,
            item,
            refresh_token=refresh_token,
            updated_ns=_now(updated_ns),
            invalidate_text_on_fingerprint_change=True,
        )


def stage_semantic_items(
    path: Path,
    items: Iterable[SemanticItem],
    *,
    source_kind: str,
    refresh_token: str,
    updated_ns: int | None = None,
    batch_size: int = MAX_WRITE_BATCH,
    invalidate_text_on_fingerprint_change: bool = True,
) -> int:
    """Stage sources, optionally preserving independently revised text channels."""

    if not source_kind.strip() or not refresh_token.strip():
        raise ValueError("source_kind and refresh_token cannot be blank")
    _check_batch_size(batch_size)
    selected_ns = _now(updated_ns)
    count = 0
    for batch in _item_batches(items, batch_size):
        if any(item.source_kind != source_kind for item in batch):
            raise ValueError("all staged items must match source_kind")
        with semantic_database(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in batch:
                _upsert_item(
                    connection,
                    item,
                    refresh_token=refresh_token,
                    updated_ns=selected_ns,
                    invalidate_text_on_fingerprint_change=(invalidate_text_on_fingerprint_change),
                )
        count += len(batch)
    return count


def finalize_semantic_item_refresh(
    path: Path,
    *,
    source_kind: str,
    refresh_token: str,
    updated_ns: int | None = None,
) -> int:
    """Deactivate, but never delete, source rows not seen in a completed refresh."""

    if not source_kind.strip() or not refresh_token.strip():
        raise ValueError("source_kind and refresh_token cannot be blank")
    selected_ns = _now(updated_ns)
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        return _finalize_semantic_item_refresh(
            connection,
            source_kind=source_kind,
            refresh_token=refresh_token,
            updated_ns=selected_ns,
        )


def _finalize_semantic_item_refresh(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    refresh_token: str,
    updated_ns: int,
) -> int:
    """Apply one completed source refresh inside the caller's transaction."""

    connection.execute(
        """UPDATE text_chunks SET active=0,updated_ns=?
        WHERE active=1 AND item_id IN (
            SELECT item_id FROM semantic_items
            WHERE source_kind=? AND active=1
              AND COALESCE(refresh_token,'')<>?)""",
        (updated_ns, source_kind, refresh_token),
    )
    connection.execute(
        """UPDATE semantic_evidence SET active=0,updated_ns=?
        WHERE active=1 AND item_id IN (
            SELECT item_id FROM semantic_items
            WHERE source_kind=? AND active=1
              AND COALESCE(refresh_token,'')<>?)""",
        (updated_ns, source_kind, refresh_token),
    )
    cursor = connection.execute(
        "UPDATE semantic_items SET active=0,updated_ns=? WHERE source_kind=? "
        "AND active=1 AND COALESCE(refresh_token,'')<>?",
        (updated_ns, source_kind, refresh_token),
    )
    return int(cursor.rowcount)


def deactivate_semantic_item_if_fingerprint(
    path: Path,
    *,
    item_id: str,
    fingerprint: ContentFingerprint,
    updated_ns: int | None = None,
) -> bool:
    """Hide stale vectors only if the failed source identity is still current."""

    if not item_id.strip():
        raise ValueError("item_id cannot be blank")
    selected_ns = _now(updated_ns)
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT content_xxh3_128,content_bytes,content_xxh3_64_guard
            FROM semantic_items WHERE item_id=? AND active=1""",
            (item_id,),
        ).fetchone()
        if row is None or not _same_fingerprint(row, fingerprint):
            return False
        connection.execute(
            "UPDATE text_chunks SET active=0,updated_ns=? WHERE item_id=? AND active=1",
            (selected_ns, item_id),
        )
        connection.execute(
            "UPDATE semantic_evidence SET active=0,updated_ns=? WHERE item_id=? AND active=1",
            (selected_ns, item_id),
        )
        cursor = connection.execute(
            "UPDATE semantic_items SET active=0,updated_ns=? WHERE item_id=? AND active=1",
            (selected_ns, item_id),
        )
        return cursor.rowcount == 1


# endregion [03]


# region [04] Bounded text payload staging


def _encode_chunk_text(chunk: TextChunk) -> bytes:
    raw = chunk.text.encode("utf-8")
    if len(raw) > MAX_STORED_CHUNK_BYTES:
        raise ValueError(
            f"chunk {chunk.chunk_id!r} exceeds the {MAX_STORED_CHUNK_BYTES}-byte bound"
        )
    return zlib.compress(raw, level=6)


def _decode_chunk_text(payload: bytes, fingerprint: ContentFingerprint) -> str:
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(payload, MAX_STORED_CHUNK_BYTES + 1)
        if decompressor.unconsumed_tail or not decompressor.eof:
            raise SemanticStateError("chunk text exceeds its decompression bound")
    except zlib.error as exc:
        raise SemanticStateError("invalid compressed chunk text") from exc
    if len(raw) > MAX_STORED_CHUNK_BYTES:
        raise SemanticStateError("chunk text exceeds its decompression bound")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SemanticStateError("chunk text is not valid UTF-8") from exc
    if fingerprint_text(text) != fingerprint:
        raise SemanticStateError("chunk text does not match its XXH3 identity")
    return text


def stage_text_chunks(
    path: Path,
    chunks: Iterable[TextChunk],
    *,
    refresh_token: str,
    updated_ns: int | None = None,
    batch_size: int = MAX_WRITE_BATCH,
) -> int:
    """Persist bounded chunk payloads incrementally for resumable workers."""

    if not refresh_token.strip():
        raise ValueError("refresh_token cannot be blank")
    _check_batch_size(batch_size)
    selected_ns = _now(updated_ns)
    count = 0
    for batch in _chunk_batches(chunks, batch_size):
        with semantic_database(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            count += _stage_text_chunk_batch(
                connection,
                batch,
                refresh_token=refresh_token,
                updated_ns=selected_ns,
            )
    return count


def _stage_text_chunk_batch(
    connection: sqlite3.Connection,
    chunks: Sequence[TextChunk],
    *,
    refresh_token: str,
    updated_ns: int,
) -> int:
    """Stage one already-bounded chunk batch in the caller's transaction."""

    if not chunks:
        return 0
    if len(chunks) > MAX_WRITE_BATCH:
        raise ValueError(f"text chunk batch cannot exceed {MAX_WRITE_BATCH} records")
    item_ids = tuple(sorted({chunk.item_id for chunk in chunks}))
    placeholders = ",".join("?" for _ in item_ids)
    active_items = {
        str(row[0])
        for row in connection.execute(
            f"SELECT item_id FROM semantic_items WHERE active=1 AND item_id IN ({placeholders})",
            item_ids,
        )
    }
    missing = set(item_ids).difference(active_items)
    if missing:
        raise KeyError(f"unknown or inactive semantic items: {sorted(missing)}")
    connection.executemany(
        """INSERT INTO text_chunks(
            chunk_id,item_id,ordinal,section_kind,section_id,start_char,end_char,
            text_zlib,text_chars,content_xxh3_128,content_bytes,
            content_xxh3_64_guard,chunking_signature,provenance_json,
            refresh_token,active,updated_ns)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
        ON CONFLICT(chunk_id) DO UPDATE SET
            item_id=excluded.item_id,
            ordinal=excluded.ordinal,
            section_kind=excluded.section_kind,
            section_id=excluded.section_id,
            start_char=excluded.start_char,
            end_char=excluded.end_char,
            text_zlib=excluded.text_zlib,
            text_chars=excluded.text_chars,
            content_xxh3_128=excluded.content_xxh3_128,
            content_bytes=excluded.content_bytes,
            content_xxh3_64_guard=excluded.content_xxh3_64_guard,
            chunking_signature=excluded.chunking_signature,
            provenance_json=excluded.provenance_json,
            refresh_token=excluded.refresh_token,
            active=1,
            updated_ns=excluded.updated_ns
        WHERE text_chunks.active=0 OR NOT EXISTS(
            SELECT 1 FROM semantic_chunk_revisions revision
            JOIN semantic_chunk_derivations derivation
              ON derivation.chunk_revision_id=revision.chunk_revision_id
            WHERE revision.chunk_id=text_chunks.chunk_id
              AND derivation.publication_receipt_id IS NOT NULL)""",
        (
            (
                chunk.chunk_id,
                chunk.item_id,
                chunk.ordinal,
                chunk.section_kind,
                chunk.section_id,
                chunk.start_char,
                chunk.end_char,
                _encode_chunk_text(chunk),
                len(chunk.text),
                chunk.fingerprint.xxh3_128,
                chunk.fingerprint.byte_count,
                chunk.fingerprint.xxh3_64_guard,
                chunk.chunking_signature,
                canonical_json(chunk.provenance),
                refresh_token,
                updated_ns,
            )
            for chunk in chunks
        ),
    )
    for chunk in chunks:
        _record_chunk_materialization(
            connection,
            chunk_id=chunk.chunk_id,
            item_id=chunk.item_id,
            fingerprint=chunk.fingerprint,
            chunking_signature=chunk.chunking_signature,
            refresh_token=refresh_token,
            now_ns=updated_ns,
        )
    return len(chunks)


def finalize_text_chunk_refresh(
    path: Path,
    *,
    item_id: str,
    chunking_signature: str,
    refresh_token: str,
    updated_ns: int | None = None,
) -> int:
    """Publish one signature refresh without retiring other chunking profiles."""

    if not item_id.strip() or not chunking_signature.strip() or not refresh_token.strip():
        raise ValueError("item, chunking signature and refresh token cannot be blank")
    selected_ns = _now(updated_ns)
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        return _finalize_text_chunk_refresh(
            connection,
            item_id=item_id,
            chunking_signature=chunking_signature,
            refresh_token=refresh_token,
            updated_ns=selected_ns,
        )


def _finalize_text_chunk_refresh(
    connection: sqlite3.Connection,
    *,
    item_id: str,
    chunking_signature: str,
    refresh_token: str,
    updated_ns: int,
) -> int:
    """Finalize one item/profile refresh in the caller's transaction."""

    publication_receipt_id = _record_chunk_refresh_publication(
        connection,
        item_id=item_id,
        chunking_signature=chunking_signature,
        refresh_token=refresh_token,
        now_ns=updated_ns,
    )
    connection.execute(
        """UPDATE text_chunks SET refresh_token=?,active=1,updated_ns=?
        WHERE chunk_id IN (
            SELECT revision.chunk_id
            FROM semantic_chunk_revisions revision
            JOIN semantic_chunk_derivations derivation
              ON derivation.chunk_revision_id=revision.chunk_revision_id
            WHERE derivation.publication_receipt_id=?)""",
        (refresh_token, updated_ns, publication_receipt_id),
    )
    cursor = connection.execute(
        """UPDATE text_chunks SET active=0,updated_ns=?
        WHERE item_id=? AND active=1 AND chunking_signature=?
          AND chunk_id NOT IN (
            SELECT revision.chunk_id
            FROM semantic_chunk_revisions revision
            JOIN semantic_chunk_derivations derivation
              ON derivation.chunk_revision_id=revision.chunk_revision_id
            WHERE derivation.publication_receipt_id=?)""",
        (updated_ns, item_id, chunking_signature, publication_receipt_id),
    )
    connection.execute(
        """UPDATE semantic_evidence SET active=0,updated_ns=?
        WHERE item_id=? AND active=1 AND source_entity_id IN (
            SELECT chunk_id FROM text_chunks
            WHERE item_id=? AND active=0)""",
        (updated_ns, item_id, item_id),
    )
    return int(cursor.rowcount)


def publish_text_channel_revision(
    path: Path,
    *,
    item_id: str,
    channel: str,
    revision_token: str,
    updated_ns: int | None = None,
) -> int:
    """Publish one textual source revision and retire stale profile variants."""

    if not item_id.strip() or not channel.strip() or not revision_token.strip():
        raise ValueError("text channel revision identifiers cannot be blank")
    if len(channel) > 128 or len(revision_token) > 1_024:
        raise ValueError("text channel revision identifiers exceed their bounds")
    selected_ns = _now(updated_ns)
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        active_item = connection.execute(
            "SELECT 1 FROM semantic_items WHERE item_id=? AND active=1",
            (item_id,),
        ).fetchone()
        if active_item is None:
            raise KeyError(f"unknown or inactive semantic item {item_id!r}")
        prior = connection.execute(
            """SELECT revision_token FROM text_channel_revisions
            WHERE item_id=? AND channel=?""",
            (item_id, channel),
        ).fetchone()
        if prior is not None and str(prior["revision_token"]) == revision_token:
            return 0
        cursor = connection.execute(
            """UPDATE text_chunks SET active=0,updated_ns=?
            WHERE item_id=? AND section_kind=? AND active=1""",
            (selected_ns, item_id, channel),
        )
        connection.execute(
            """UPDATE semantic_evidence SET active=0,updated_ns=?
            WHERE item_id=? AND active=1 AND EXISTS(
                SELECT 1 FROM text_chunks c
                WHERE c.item_id=? AND c.section_kind=?
                  AND c.chunk_id=semantic_evidence.source_entity_id)""",
            (selected_ns, item_id, item_id, channel),
        )
        connection.execute(
            """INSERT INTO text_channel_revisions(
                item_id,channel,revision_token,updated_ns)
            VALUES(?,?,?,?)
            ON CONFLICT(item_id,channel) DO UPDATE SET
                revision_token=excluded.revision_token,
                updated_ns=excluded.updated_ns""",
            (item_id, channel, revision_token, selected_ns),
        )
        return int(cursor.rowcount)


def deactivate_text_chunks_for_item(
    path: Path,
    *,
    item_id: str,
    updated_ns: int | None = None,
) -> int:
    """Atomically deactivate every text profile and its chunk-derived evidence."""

    if not item_id.strip():
        raise ValueError("item_id cannot be blank")
    selected_ns = _now(updated_ns)
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        active_item = connection.execute(
            "SELECT 1 FROM semantic_items WHERE item_id=? AND active=1",
            (item_id,),
        ).fetchone()
        if active_item is None:
            raise KeyError(f"unknown or inactive semantic item {item_id!r}")
        cursor = connection.execute(
            "UPDATE text_chunks SET active=0,updated_ns=? WHERE item_id=? AND active=1",
            (selected_ns, item_id),
        )
        connection.execute(
            """UPDATE semantic_evidence SET active=0,updated_ns=?
            WHERE item_id=? AND active=1 AND EXISTS(
                SELECT 1 FROM text_chunks c
                WHERE c.item_id=? AND c.chunk_id=semantic_evidence.source_entity_id)""",
            (selected_ns, item_id, item_id),
        )
        return int(cursor.rowcount)


# endregion [04]
