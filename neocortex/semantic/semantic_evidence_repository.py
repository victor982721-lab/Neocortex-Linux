"""Versioned label-prototype and advisory semantic-evidence repository."""

from __future__ import annotations
import itertools
import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from .semantic_models import (
    CalibrationStatus,
    EmbeddingModality,
    EmbeddingRole,
    EvidenceDisposition,
    LabelPrototype,
    SemanticEvidence,
    StoredLabelPrototype,
    VectorDType,
    canonical_json,
    decode_vector,
    encode_vector,
    fingerprint_text,
    normalize_vector,
)
from .semantic_repository_common import (
    MAX_EVIDENCE_ENTITIES_PER_PUBLICATION,
    MAX_EVIDENCE_ROWS_PER_PUBLICATION,
    MAX_WRITE_BATCH,
    _check_batch_size,
    _fingerprint_from_row,
    _load_model,
    _now,
    _same_fingerprint,
)
from .semantic_schema import SemanticStateError, semantic_database

# region [08] Versioned label prototypes and advisory semantic evidence


def _published_text_chunk_predicate(
    connection: sqlite3.Connection,
    alias: str,
) -> str:
    """Require owner-published chunks on schema 7 without breaking v6 readers."""

    has_derivation_owner = connection.execute(
        """SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='semantic_chunk_derivations'"""
    ).fetchone()
    if has_derivation_owner is None:
        return f"{alias}.active=1"
    return f"""{alias}.active=1 AND EXISTS(
        SELECT 1 FROM semantic_chunk_revisions revision
        WHERE revision.chunk_id={alias}.chunk_id AND (
            EXISTS(
                SELECT 1 FROM semantic_chunk_derivations derivation
                WHERE derivation.chunk_revision_id=revision.chunk_revision_id
                  AND derivation.refresh_token={alias}.refresh_token
                  AND derivation.publication_receipt_id IS NOT NULL)
            OR (
                NOT EXISTS(
                    SELECT 1 FROM semantic_chunk_derivations derivation
                    WHERE derivation.chunk_revision_id=revision.chunk_revision_id)
                AND EXISTS(
                    SELECT 1 FROM embedding_generation_members member
                    JOIN published_embedding_heads head
                      ON head.generation_id=member.generation_id
                    WHERE member.chunk_revision_id=revision.chunk_revision_id
                      AND member.entity_kind='text_chunk'))))"""


def _store_label_prototype_row(
    connection: sqlite3.Connection,
    prototype: LabelPrototype,
    vector: Sequence[float],
    *,
    updated_ns: int,
    activate: bool,
) -> None:
    if prototype.fingerprint.byte_count > 64 * 1024:
        raise ValueError("prototype text exceeds the 64 KiB safety bound")
    model = _load_model(connection, prototype.model_signature)
    if (
        model.modality is not EmbeddingModality.TEXT
        or EmbeddingRole.QUERY not in model.supported_roles
        or model.vector_space != prototype.vector_space
    ):
        raise ValueError("prototype model is not a compatible text query encoder")
    vector_blob, original_norm = encode_vector(
        vector,
        model.dimensions,
        model.vector_dtype,
    )
    existing = connection.execute(
        """SELECT ontology_id,ontology_version,concept_id,prototype_version,
            model_signature,vector_space,content_xxh3_128,content_bytes,
            content_xxh3_64_guard,vector_blob FROM label_prototypes
        WHERE prototype_id=?""",
        (prototype.prototype_id,),
    ).fetchone()
    identity = (
        prototype.ontology_id,
        prototype.ontology_version,
        prototype.concept_id,
        prototype.prototype_version,
        prototype.model_signature,
        prototype.vector_space,
    )
    if existing is not None:
        if tuple(str(existing[index]) for index in range(6)) != identity:
            raise ValueError("prototype_id is already bound to a different identity")
        if not _same_fingerprint(existing, prototype.fingerprint):
            raise ValueError("prototype content changed without a new prototype version")
        if bytes(existing["vector_blob"]) != vector_blob:
            raise ValueError("prototype vector changed under an immutable model signature")
    connection.execute(
        """INSERT INTO label_prototypes(
            prototype_id,ontology_id,ontology_version,concept_id,
            prototype_version,model_signature,vector_space,prototype_text,
            content_xxh3_128,content_bytes,content_xxh3_64_guard,
            dimensions,vector_dtype,vector_blob,original_norm,
            calibration_status,feedback_reference,provenance_json,
            active,created_ns,updated_ns)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(prototype_id) DO UPDATE SET
            calibration_status=excluded.calibration_status,
            feedback_reference=excluded.feedback_reference,
            provenance_json=excluded.provenance_json,
            active=excluded.active,
            updated_ns=excluded.updated_ns""",
        (
            prototype.prototype_id,
            prototype.ontology_id,
            prototype.ontology_version,
            prototype.concept_id,
            prototype.prototype_version,
            prototype.model_signature,
            prototype.vector_space,
            prototype.text,
            prototype.fingerprint.xxh3_128,
            prototype.fingerprint.byte_count,
            prototype.fingerprint.xxh3_64_guard,
            model.dimensions,
            model.vector_dtype.value,
            vector_blob,
            original_norm,
            prototype.calibration_status.value,
            prototype.feedback_reference,
            canonical_json(prototype.provenance),
            int(activate),
            updated_ns,
            updated_ns,
        ),
    )


def _prototype_batches(
    values: Iterable[tuple[LabelPrototype, Sequence[float]]],
    batch_size: int,
) -> Iterator[tuple[tuple[LabelPrototype, Sequence[float]], ...]]:
    iterator = iter(values)
    while batch := tuple(itertools.islice(iterator, batch_size)):
        yield batch


def stage_label_prototypes(
    path: Path,
    prototypes: Iterable[tuple[LabelPrototype, Sequence[float]]],
    *,
    updated_ns: int | None = None,
    batch_size: int = MAX_WRITE_BATCH,
    activate: bool = True,
) -> int:
    """Persist prototype vectors atomically per bounded batch.

    ``activate=False`` stages an unpublished row for a later atomic prototype
    refresh; the default preserves the single-row compatibility API.
    """

    _check_batch_size(batch_size)
    selected_ns = _now(updated_ns)
    count = 0
    for batch in _prototype_batches(prototypes, batch_size):
        with semantic_database(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for prototype, vector in batch:
                _store_label_prototype_row(
                    connection,
                    prototype,
                    vector,
                    updated_ns=selected_ns,
                    activate=activate,
                )
        count += len(batch)
    return count


def store_label_prototype(
    path: Path,
    prototype: LabelPrototype,
    vector: Sequence[float],
    *,
    updated_ns: int | None = None,
) -> None:
    """Store one text-label vector through the bounded batch implementation."""

    stage_label_prototypes(
        path,
        ((prototype, vector),),
        updated_ns=updated_ns,
        batch_size=1,
    )


def finalize_label_prototype_refresh(
    path: Path,
    *,
    ontology_id: str,
    ontology_version: str,
    prototype_version: str,
    vector_space: str,
    model_signature: str,
    active_prototype_ids: Sequence[str],
    updated_ns: int | None = None,
) -> int:
    """Atomically publish one complete, model-scoped prototype set.

    Prototype sets for other ontology versions, models, or vector spaces stay
    independent.  Evidence tied to a prototype retired by this publication is
    retained as inactive audit history rather than remaining queryable.
    """

    identifiers = (
        ontology_id,
        ontology_version,
        prototype_version,
        vector_space,
        model_signature,
    )
    if any(not value.strip() for value in identifiers):
        raise ValueError("prototype refresh identifiers cannot be blank")
    expected_ids = tuple(dict.fromkeys(active_prototype_ids))
    if not expected_ids:
        raise ValueError("a prototype refresh must publish at least one prototype")
    if len(expected_ids) != len(active_prototype_ids):
        raise ValueError("active prototype identifiers must be unique")
    if len(expected_ids) > MAX_WRITE_BATCH:
        raise ValueError("too many active prototypes for one atomic refresh")
    if any(not prototype_id.strip() for prototype_id in expected_ids):
        raise ValueError("prototype identifiers cannot be blank")

    placeholders = ",".join("?" for _ in expected_ids)
    selected_ns = _now(updated_ns)
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        model = _load_model(connection, model_signature)
        if (
            model.modality is not EmbeddingModality.TEXT
            or EmbeddingRole.QUERY not in model.supported_roles
            or model.vector_space != vector_space
        ):
            raise ValueError("prototype model is not a compatible text query encoder")
        published_ids = {
            str(row[0])
            for row in connection.execute(
                f"""SELECT prototype_id FROM label_prototypes
                WHERE ontology_id=? AND ontology_version=?
                  AND prototype_version=? AND vector_space=?
                  AND model_signature=?
                  AND prototype_id IN ({placeholders})""",
                (*identifiers, *expected_ids),
            )
        }
        if published_ids != set(expected_ids):
            raise SemanticStateError("cannot publish an incomplete or incompatible prototype set")
        connection.execute(
            f"""UPDATE label_prototypes
            SET active=1,
                updated_ns=CASE WHEN active=0 THEN ? ELSE updated_ns END
            WHERE ontology_id=? AND ontology_version=?
              AND prototype_version=? AND vector_space=?
              AND model_signature=? AND prototype_id IN ({placeholders})""",
            (selected_ns, *identifiers, *expected_ids),
        )
        cursor = connection.execute(
            f"""UPDATE label_prototypes SET active=0,updated_ns=?
            WHERE ontology_id=? AND ontology_version=? AND vector_space=?
              AND model_signature=? AND active=1
              AND (prototype_version<>? OR prototype_id NOT IN ({placeholders}))""",
            (
                selected_ns,
                ontology_id,
                ontology_version,
                vector_space,
                model_signature,
                prototype_version,
                *expected_ids,
            ),
        )
        connection.execute(
            """UPDATE semantic_evidence SET active=0,updated_ns=?
            WHERE ontology_id=? AND ontology_version=? AND vector_space=?
              AND query_model_signature=? AND active=1
              AND prototype_id IN (
                  SELECT prototype_id FROM label_prototypes
                  WHERE ontology_id=? AND ontology_version=?
                    AND vector_space=? AND model_signature=? AND active=0
              )""",
            (
                selected_ns,
                ontology_id,
                ontology_version,
                vector_space,
                model_signature,
                ontology_id,
                ontology_version,
                vector_space,
                model_signature,
            ),
        )
        return int(cursor.rowcount)


def load_label_prototypes(
    path: Path,
    *,
    ontology_id: str,
    ontology_version: str,
    prototype_version: str,
    vector_space: str,
    model_signatures: Sequence[str] = (),
    limit: int = 1_000,
) -> tuple[StoredLabelPrototype, ...]:
    """Load a bounded compatible prototype set for scoring."""

    if any(
        not value.strip()
        for value in (ontology_id, ontology_version, prototype_version, vector_space)
    ):
        raise ValueError("ontology, prototype, and vector-space identifiers cannot be blank")
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    clauses = [
        "p.ontology_id=?",
        "p.ontology_version=?",
        "p.prototype_version=?",
        "p.vector_space=?",
        "p.active=1",
        "m.active=1",
    ]
    parameters: list[object] = [
        ontology_id,
        ontology_version,
        prototype_version,
        vector_space,
    ]
    if model_signatures:
        if len(model_signatures) > MAX_WRITE_BATCH:
            raise ValueError("too many model signatures")
        clauses.append("p.model_signature IN (" + ",".join("?" for _ in model_signatures) + ")")
        parameters.extend(model_signatures)
    parameters.append(limit)
    with semantic_database(path, readonly=True) as connection:
        rows = connection.execute(
            f"""SELECT p.* FROM label_prototypes p
            JOIN embedding_models m ON m.model_signature=p.model_signature
            WHERE {" AND ".join(clauses)}
            ORDER BY p.concept_id,p.prototype_id LIMIT ?""",
            parameters,
        ).fetchall()
    output: list[StoredLabelPrototype] = []
    for row in rows:
        fingerprint = _fingerprint_from_row(row)
        text = str(row["prototype_text"])
        if fingerprint_text(text) != fingerprint:
            raise SemanticStateError("prototype text does not match its XXH3 identity")
        dimensions = int(row["dimensions"])
        vector = normalize_vector(
            decode_vector(
                bytes(row["vector_blob"]),
                dimensions,
                VectorDType(str(row["vector_dtype"])),
            ),
            dimensions,
        )[0]
        raw_provenance = json.loads(str(row["provenance_json"]))
        if not isinstance(raw_provenance, dict):
            raise SemanticStateError("prototype provenance is not a JSON object")
        output.append(
            StoredLabelPrototype(
                prototype=LabelPrototype(
                    prototype_id=str(row["prototype_id"]),
                    ontology_id=str(row["ontology_id"]),
                    ontology_version=str(row["ontology_version"]),
                    concept_id=str(row["concept_id"]),
                    prototype_version=str(row["prototype_version"]),
                    model_signature=str(row["model_signature"]),
                    vector_space=str(row["vector_space"]),
                    text=text,
                    fingerprint=fingerprint,
                    calibration_status=CalibrationStatus(str(row["calibration_status"])),
                    feedback_reference=(
                        None
                        if row["feedback_reference"] is None
                        else str(row["feedback_reference"])
                    ),
                    provenance=raw_provenance,
                ),
                vector=vector,
            )
        )
    return tuple(output)


def _evidence_batches(
    values: Iterable[SemanticEvidence],
    batch_size: int,
) -> Iterator[tuple[SemanticEvidence, ...]]:
    iterator = iter(values)
    while batch := tuple(itertools.islice(iterator, batch_size)):
        yield batch


def _validate_evidence_batch(
    connection: sqlite3.Connection,
    batch: tuple[SemanticEvidence, ...],
) -> None:
    item_ids = tuple(sorted({value.item_id for value in batch}))
    prototype_ids = tuple(sorted({value.prototype_id for value in batch}))
    model_signatures = tuple(
        sorted(
            {
                signature
                for value in batch
                for signature in (
                    value.query_model_signature,
                    value.indexed_model_signature,
                )
            }
        )
    )
    source_entity_ids = tuple(
        sorted(
            {value.source_entity_id for value in batch if value.source_entity_id != value.item_id}
        )
    )
    generation_ids = tuple(
        sorted({value.generation_id for value in batch if value.generation_id is not None})
    )
    item_placeholders = ",".join("?" for _ in item_ids)
    prototype_placeholders = ",".join("?" for _ in prototype_ids)
    model_placeholders = ",".join("?" for _ in model_signatures)
    active_items = {
        str(row[0])
        for row in connection.execute(
            f"SELECT item_id FROM semantic_items WHERE active=1 "
            f"AND item_id IN ({item_placeholders})",
            item_ids,
        )
    }
    prototypes = {
        str(row["prototype_id"]): row
        for row in connection.execute(
            f"""SELECT prototype_id,ontology_id,ontology_version,concept_id,
                model_signature,vector_space FROM label_prototypes
            WHERE active=1 AND prototype_id IN ({prototype_placeholders})""",
            prototype_ids,
        )
    }
    models = {
        str(row["model_signature"]): row
        for row in connection.execute(
            f"""SELECT model_signature,vector_space FROM embedding_models
            WHERE active=1 AND model_signature IN ({model_placeholders})""",
            model_signatures,
        )
    }
    active_chunks: dict[str, str] = {}
    if source_entity_ids:
        source_placeholders = ",".join("?" for _ in source_entity_ids)
        published_chunk = _published_text_chunk_predicate(connection, "chunk")
        active_chunks = {
            str(row["chunk_id"]): str(row["item_id"])
            for row in connection.execute(
                f"""SELECT chunk.chunk_id,chunk.item_id FROM text_chunks chunk
                WHERE {published_chunk}
                  AND chunk.chunk_id IN ({source_placeholders})""",
                source_entity_ids,
            )
        }
    generations: dict[int, str] = {}
    if generation_ids:
        generation_placeholders = ",".join("?" for _ in generation_ids)
        generations = {
            int(row["generation_id"]): str(row["model_signature"])
            for row in connection.execute(
                f"""SELECT generation_id,model_signature FROM embedding_generations
                WHERE generation_id IN ({generation_placeholders})""",
                generation_ids,
            )
        }
    for evidence in batch:
        if evidence.item_id not in active_items:
            raise KeyError(f"unknown or inactive evidence item {evidence.item_id!r}")
        if (
            evidence.source_entity_id != evidence.item_id
            and active_chunks.get(evidence.source_entity_id) != evidence.item_id
        ):
            raise ValueError("evidence source entity is not active for its item")
        prototype = prototypes.get(evidence.prototype_id)
        if prototype is None:
            raise KeyError(f"unknown or inactive prototype {evidence.prototype_id!r}")
        expected_prototype = (
            evidence.ontology_id,
            evidence.ontology_version,
            evidence.concept_id,
            evidence.query_model_signature,
            evidence.vector_space,
        )
        actual_prototype = (
            str(prototype["ontology_id"]),
            str(prototype["ontology_version"]),
            str(prototype["concept_id"]),
            str(prototype["model_signature"]),
            str(prototype["vector_space"]),
        )
        if expected_prototype != actual_prototype:
            raise ValueError("evidence conflicts with its prototype identity")
        query_model = models.get(evidence.query_model_signature)
        indexed_model = models.get(evidence.indexed_model_signature)
        if query_model is None or indexed_model is None:
            raise KeyError("evidence references an unknown embedding model")
        if (
            str(query_model["vector_space"]) != evidence.vector_space
            or str(indexed_model["vector_space"]) != evidence.vector_space
        ):
            raise ValueError("evidence attempts to mix incompatible vector spaces")
        if evidence.generation_id is not None:
            if generations.get(evidence.generation_id) != evidence.indexed_model_signature:
                raise ValueError("evidence generation does not match indexed model")


_EVIDENCE_UPSERT_SQL = """INSERT INTO semantic_evidence(
    item_id,source_entity_id,ontology_id,ontology_version,
    concept_id,prototype_id,query_model_signature,
    indexed_model_signature,vector_space,score,rank,generation_id,
    calibration_status,disposition,feedback_reference,
    provenance_json,refresh_token,active,updated_ns)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
ON CONFLICT(item_id,source_entity_id,ontology_id,ontology_version,
            concept_id,prototype_id,query_model_signature,
            indexed_model_signature) DO UPDATE SET
    vector_space=excluded.vector_space,
    score=excluded.score,
    rank=excluded.rank,
    generation_id=excluded.generation_id,
    calibration_status=excluded.calibration_status,
    disposition=excluded.disposition,
    feedback_reference=excluded.feedback_reference,
    provenance_json=excluded.provenance_json,
    refresh_token=excluded.refresh_token,
    active=1,
    updated_ns=excluded.updated_ns"""


def _upsert_evidence_batch(
    connection: sqlite3.Connection,
    batch: Sequence[SemanticEvidence],
    *,
    refresh_token: str,
    updated_ns: int,
) -> None:
    connection.executemany(
        _EVIDENCE_UPSERT_SQL,
        (
            (
                value.item_id,
                value.source_entity_id,
                value.ontology_id,
                value.ontology_version,
                value.concept_id,
                value.prototype_id,
                value.query_model_signature,
                value.indexed_model_signature,
                value.vector_space,
                value.score,
                value.rank,
                value.generation_id,
                value.calibration_status.value,
                value.disposition.value,
                value.feedback_reference,
                canonical_json(value.provenance),
                refresh_token,
                updated_ns,
            )
            for value in batch
        ),
    )


def stage_semantic_evidence(
    path: Path,
    evidence: Iterable[SemanticEvidence],
    *,
    refresh_token: str,
    updated_ns: int | None = None,
    batch_size: int = MAX_WRITE_BATCH,
) -> int:
    """Stage auditable suggestions; model-only rows remain advisory."""

    if not refresh_token.strip():
        raise ValueError("refresh_token cannot be blank")
    _check_batch_size(batch_size)
    selected_ns = _now(updated_ns)
    count = 0
    for batch in _evidence_batches(evidence, batch_size):
        with semantic_database(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            _validate_evidence_batch(connection, batch)
            _upsert_evidence_batch(
                connection,
                batch,
                refresh_token=refresh_token,
                updated_ns=selected_ns,
            )
        count += len(batch)
    return count


def _bounded_publication_entities(
    entities: Iterable[tuple[str, str]],
    *,
    limit: int = MAX_EVIDENCE_ENTITIES_PER_PUBLICATION,
) -> tuple[tuple[str, str], ...]:
    entity_iterator = iter(entities)
    selected = tuple(
        itertools.islice(
            entity_iterator,
            limit + 1,
        )
    )
    if not selected:
        raise ValueError("at least one evidence entity must be published")
    if len(selected) > limit:
        raise ValueError(f"evidence publication exceeds the entity bound of {limit}")
    if any(
        len(identity) != 2 or not identity[0].strip() or not identity[1].strip()
        for identity in selected
    ):
        raise ValueError("evidence entities require non-blank item and source IDs")
    if len(set(selected)) != len(selected):
        raise ValueError("evidence publication contains duplicate entity identities")
    return selected


def _bounded_publication_evidence(
    evidence: Iterable[SemanticEvidence],
    *,
    limit: int = MAX_EVIDENCE_ROWS_PER_PUBLICATION,
) -> tuple[SemanticEvidence, ...]:
    evidence_iterator = iter(evidence)
    selected = tuple(
        itertools.islice(
            evidence_iterator,
            limit + 1,
        )
    )
    if len(selected) > limit:
        raise ValueError(f"evidence publication exceeds the row bound of {limit}")
    return selected


def _publication_evidence_keys(
    selected_entities: tuple[tuple[str, str], ...],
    selected_evidence: tuple[SemanticEvidence, ...],
    scope: tuple[str, ...],
) -> set[tuple[str, str, str, str, str]]:
    entity_set = set(selected_entities)
    ranks_by_entity: dict[tuple[str, str], list[int]] = {
        identity: [] for identity in selected_entities
    }
    unique_keys: set[tuple[str, str, str, str, str]] = set()
    for value in selected_evidence:
        identity = (value.item_id, value.source_entity_id)
        if identity not in entity_set:
            raise ValueError("evidence references an entity outside the publication")
        actual_scope = (
            value.ontology_id,
            value.ontology_version,
            value.query_model_signature,
            value.indexed_model_signature,
            value.vector_space,
        )
        if actual_scope != scope:
            raise ValueError("evidence conflicts with the publication scope")
        key = (
            value.item_id,
            value.source_entity_id,
            value.concept_id,
            value.prototype_id,
            value.query_model_signature,
        )
        if key in unique_keys:
            raise ValueError("evidence publication contains duplicate suggestions")
        unique_keys.add(key)
        ranks_by_entity[identity].append(value.rank)
    for ranks in ranks_by_entity.values():
        if len(ranks) > 32:
            raise ValueError("at most 32 evidence rows may be published per entity")
        if sorted(ranks) != list(range(1, len(ranks) + 1)):
            raise ValueError("evidence ranks must be unique and contiguous per entity")
    return unique_keys


def publish_semantic_evidence_entities(
    path: Path,
    evidence: Iterable[SemanticEvidence],
    *,
    entities: Iterable[tuple[str, str]],
    ontology_id: str,
    ontology_version: str,
    query_model_signature: str,
    indexed_model_signature: str,
    vector_space: str,
    refresh_token: str,
    updated_ns: int | None = None,
    _max_entities: int = MAX_EVIDENCE_ENTITIES_PER_PUBLICATION,
    _max_rows: int = MAX_EVIDENCE_ROWS_PER_PUBLICATION,
) -> tuple[int, int]:
    """Atomically replace evidence for a bounded set of exact source entities.

    Empty evidence is a valid publication and records abstention by retiring the
    entity's prior suggestions in the same transaction.  The returned pair is
    ``(published_rows, stale_rows_deactivated)``.
    """

    identifiers = (
        ontology_id,
        ontology_version,
        query_model_signature,
        indexed_model_signature,
        vector_space,
        refresh_token,
    )
    if any(not value.strip() for value in identifiers):
        raise ValueError("evidence publication identifiers cannot be blank")
    selected_entities = _bounded_publication_entities(entities, limit=_max_entities)
    selected_evidence = _bounded_publication_evidence(evidence, limit=_max_rows)
    unique_keys = _publication_evidence_keys(
        selected_entities,
        selected_evidence,
        identifiers[:5],
    )

    selected_ns = _now(updated_ns)
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        query_model = _load_model(connection, query_model_signature)
        indexed_model = _load_model(connection, indexed_model_signature)
        if query_model.vector_space != vector_space or indexed_model.vector_space != vector_space:
            raise ValueError("evidence publication mixes incompatible vector spaces")
        connection.execute(
            """CREATE TEMP TABLE evidence_publication_entities(
                item_id TEXT NOT NULL,
                source_entity_id TEXT NOT NULL,
                PRIMARY KEY(item_id,source_entity_id)
            ) WITHOUT ROWID"""
        )
        connection.executemany(
            "INSERT INTO evidence_publication_entities VALUES(?,?)",
            selected_entities,
        )
        invalid_item = connection.execute(
            """SELECT p.item_id FROM evidence_publication_entities p
            LEFT JOIN semantic_items i ON i.item_id=p.item_id AND i.active=1
            WHERE i.item_id IS NULL LIMIT 1"""
        ).fetchone()
        if invalid_item is not None:
            raise KeyError(f"unknown or inactive evidence item {str(invalid_item['item_id'])!r}")
        if indexed_model.modality is EmbeddingModality.TEXT:
            published_chunk = _published_text_chunk_predicate(connection, "c")
            invalid_entity = connection.execute(
                f"""SELECT p.item_id,p.source_entity_id
                FROM evidence_publication_entities p
                LEFT JOIN text_chunks c
                  ON c.chunk_id=p.source_entity_id AND c.item_id=p.item_id
                 AND {published_chunk}
                WHERE c.chunk_id IS NULL LIMIT 1"""
            ).fetchone()
        else:
            invalid_entity = connection.execute(
                """SELECT item_id,source_entity_id
                FROM evidence_publication_entities
                WHERE source_entity_id<>item_id LIMIT 1"""
            ).fetchone()
        if invalid_entity is not None:
            raise ValueError(
                "evidence source entity is not active for its indexed model: "
                f"{str(invalid_entity['item_id'])!r}/"
                f"{str(invalid_entity['source_entity_id'])!r}"
            )
        for batch in _evidence_batches(selected_evidence, MAX_WRITE_BATCH):
            _validate_evidence_batch(connection, batch)

        prior_rows = connection.execute(
            """SELECT e.item_id,e.source_entity_id,e.concept_id,e.prototype_id,
                e.query_model_signature
            FROM semantic_evidence e
            JOIN evidence_publication_entities p
              ON p.item_id=e.item_id AND p.source_entity_id=e.source_entity_id
            WHERE e.ontology_id=? AND e.ontology_version=?
              AND e.indexed_model_signature=? AND e.vector_space=?
              AND e.active=1""",
            (
                ontology_id,
                ontology_version,
                indexed_model_signature,
                vector_space,
            ),
        ).fetchall()
        stale_count = sum(
            (
                str(row["item_id"]),
                str(row["source_entity_id"]),
                str(row["concept_id"]),
                str(row["prototype_id"]),
                str(row["query_model_signature"]),
            )
            not in unique_keys
            for row in prior_rows
        )
        connection.execute(
            """UPDATE semantic_evidence SET active=0,updated_ns=?
            WHERE ontology_id=? AND ontology_version=?
              AND indexed_model_signature=? AND vector_space=?
              AND active=1 AND EXISTS(
                  SELECT 1 FROM evidence_publication_entities p
                  WHERE p.item_id=semantic_evidence.item_id
                    AND p.source_entity_id=semantic_evidence.source_entity_id)""",
            (
                selected_ns,
                ontology_id,
                ontology_version,
                indexed_model_signature,
                vector_space,
            ),
        )
        for batch in _evidence_batches(selected_evidence, MAX_WRITE_BATCH):
            _upsert_evidence_batch(
                connection,
                batch,
                refresh_token=refresh_token,
                updated_ns=selected_ns,
            )
    return len(selected_evidence), stale_count


def record_semantic_evidence(
    path: Path,
    evidence: SemanticEvidence,
    *,
    refresh_token: str,
    updated_ns: int | None = None,
) -> None:
    """Convenience wrapper for one evidence row."""

    stage_semantic_evidence(
        path,
        (evidence,),
        refresh_token=refresh_token,
        updated_ns=updated_ns,
        batch_size=1,
    )


def finalize_semantic_evidence_refresh(
    path: Path,
    *,
    item_id: str,
    ontology_id: str,
    ontology_version: str,
    vector_space: str,
    refresh_token: str,
    updated_ns: int | None = None,
) -> int:
    """Deactivate prior suggestions outside one completed evidence refresh."""

    if any(
        not value.strip()
        for value in (
            item_id,
            ontology_id,
            ontology_version,
            vector_space,
            refresh_token,
        )
    ):
        raise ValueError("evidence refresh identifiers cannot be blank")
    with semantic_database(path) as connection:
        cursor = connection.execute(
            """UPDATE semantic_evidence SET active=0,updated_ns=?
            WHERE item_id=? AND ontology_id=? AND ontology_version=?
              AND vector_space=? AND active=1 AND refresh_token<>?""",
            (
                _now(updated_ns),
                item_id,
                ontology_id,
                ontology_version,
                vector_space,
                refresh_token,
            ),
        )
        return int(cursor.rowcount)


def finalize_semantic_evidence_model_refresh(
    path: Path,
    *,
    ontology_id: str,
    ontology_version: str,
    vector_space: str,
    indexed_model_signature: str,
    refresh_token: str,
    updated_ns: int | None = None,
) -> int:
    """Finalize one complete corpus-wide model scoring pass in one SQL update.

    Callers must invoke this only after staging every active entity for the
    stated indexed model and ontology scope.  Other models and spaces are not
    affected.
    """

    if any(
        not value.strip()
        for value in (
            ontology_id,
            ontology_version,
            vector_space,
            indexed_model_signature,
            refresh_token,
        )
    ):
        raise ValueError("evidence model refresh identifiers cannot be blank")
    with semantic_database(path) as connection:
        model = _load_model(connection, indexed_model_signature)
        if model.vector_space != vector_space:
            raise ValueError("indexed model is incompatible with evidence vector space")
        cursor = connection.execute(
            """UPDATE semantic_evidence SET active=0,updated_ns=?
            WHERE ontology_id=? AND ontology_version=? AND vector_space=?
              AND indexed_model_signature=? AND active=1 AND refresh_token<>?""",
            (
                _now(updated_ns),
                ontology_id,
                ontology_version,
                vector_space,
                indexed_model_signature,
                refresh_token,
            ),
        )
        return int(cursor.rowcount)


def list_semantic_evidence(
    path: Path,
    *,
    item_id: str,
    ontology_id: str,
    ontology_version: str,
    limit: int = 100,
) -> tuple[SemanticEvidence, ...]:
    """Return current evidence ordered by rank and score."""

    if not item_id.strip() or not ontology_id.strip() or not ontology_version.strip():
        raise ValueError("item and ontology identifiers cannot be blank")
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    with semantic_database(path, readonly=True) as connection:
        published_chunk = _published_text_chunk_predicate(connection, "c")
        rows = connection.execute(
            f"""SELECT e.* FROM semantic_evidence e
            JOIN semantic_items i ON i.item_id=e.item_id
            WHERE e.item_id=? AND e.ontology_id=? AND e.ontology_version=?
              AND e.active=1 AND i.active=1
              AND (e.source_entity_id=e.item_id OR EXISTS(
                  SELECT 1 FROM text_chunks c
                  WHERE c.chunk_id=e.source_entity_id AND {published_chunk}))
            ORDER BY e.rank,e.score DESC,e.concept_id LIMIT ?""",
            (item_id, ontology_id, ontology_version, limit),
        ).fetchall()
    output: list[SemanticEvidence] = []
    for row in rows:
        raw_provenance = json.loads(str(row["provenance_json"]))
        if not isinstance(raw_provenance, dict):
            raise SemanticStateError("semantic evidence provenance is not an object")
        output.append(
            SemanticEvidence(
                item_id=str(row["item_id"]),
                source_entity_id=str(row["source_entity_id"]),
                ontology_id=str(row["ontology_id"]),
                ontology_version=str(row["ontology_version"]),
                concept_id=str(row["concept_id"]),
                prototype_id=str(row["prototype_id"]),
                query_model_signature=str(row["query_model_signature"]),
                indexed_model_signature=str(row["indexed_model_signature"]),
                vector_space=str(row["vector_space"]),
                score=float(row["score"]),
                rank=int(row["rank"]),
                generation_id=(None if row["generation_id"] is None else int(row["generation_id"])),
                calibration_status=CalibrationStatus(str(row["calibration_status"])),
                disposition=EvidenceDisposition(str(row["disposition"])),
                feedback_reference=(
                    None if row["feedback_reference"] is None else str(row["feedback_reference"])
                ),
                provenance=raw_provenance,
            )
        )
    return tuple(output)


# endregion [08]
