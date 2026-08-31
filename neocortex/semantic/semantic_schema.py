"""SQLite connection, schema contract, and migrations for semantic state."""

from __future__ import annotations
import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from neocortex.persistence.sqlite_paths import readonly_sqlite_uri
from neocortex.persistence.sqlite_schema_contract import (
    SQLiteSchemaContract,
    SQLiteSchemaContractError,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)


SEMANTIC_SCHEMA_VERSION = 7
_RETIRED_IMAGE_KEYS = frozenset(
    {
        "adult_classification",
        "adult_content",
        "adult_candidate",
        "adult_analyzed",
        "adult_confidence",
        "adult_provenance",
        "adult_evidence_json",
    }
)


class SemanticStateError(RuntimeError):
    """Base class for durable semantic-state failures."""


# region [01] Connection lifecycle


def _configure_common_connection(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")
    connection.execute("PRAGMA foreign_keys=ON")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise SemanticStateError("semantic state could not enable foreign keys")


def _configure_read_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA query_only=ON")
    if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SemanticStateError("semantic state could not enforce query-only mode")


def _configure_write_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA cache_size=-32768")
    connection.execute("PRAGMA wal_autocheckpoint=2048")
    connection.execute("PRAGMA journal_size_limit=268435456")


@contextmanager
def semantic_database(path: Path, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """Open the semantic database with bounded WAL/cache settings."""

    if readonly:
        connection = sqlite3.connect(readonly_sqlite_uri(path), uri=True, timeout=60.0)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=60.0)
    try:
        _configure_common_connection(connection)
        if readonly:
            _configure_read_connection(connection)
        else:
            _configure_write_connection(connection)
        yield connection
        if not readonly:
            connection.commit()
    except BaseException:
        if not readonly:
            connection.rollback()
        raise
    finally:
        connection.close()


# endregion [01]


# region [02] Canonical DDL and sequential migrations


_MIGRATION_1 = (
    """CREATE TABLE metadata(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE schema_migrations(
        version INTEGER PRIMARY KEY,
        description TEXT NOT NULL,
        applied_ns INTEGER NOT NULL
    )""",
    """CREATE TABLE vector_spaces(
        vector_space TEXT PRIMARY KEY,
        dimensions INTEGER NOT NULL CHECK(dimensions>0),
        distance TEXT NOT NULL CHECK(distance='cosine'),
        normalization TEXT NOT NULL CHECK(normalization='l2'),
        created_ns INTEGER NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE embedding_models(
        model_signature TEXT PRIMARY KEY,
        vector_space TEXT NOT NULL,
        modality TEXT NOT NULL CHECK(modality IN ('text','image')),
        model_id TEXT NOT NULL,
        model_version TEXT NOT NULL,
        dimensions INTEGER NOT NULL CHECK(dimensions>0),
        provider TEXT NOT NULL,
        supported_roles_json TEXT NOT NULL,
        vector_dtype TEXT NOT NULL CHECK(vector_dtype IN ('float16','float32')),
        normalization TEXT NOT NULL CHECK(normalization='l2'),
        distance TEXT NOT NULL CHECK(distance='cosine'),
        provenance_json TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        created_ns INTEGER NOT NULL,
        FOREIGN KEY(vector_space) REFERENCES vector_spaces(vector_space)
    ) WITHOUT ROWID""",
    """CREATE INDEX embedding_models_space_idx
        ON embedding_models(vector_space,modality,active)""",
    """CREATE TABLE semantic_items(
        item_id TEXT PRIMARY KEY,
        source_kind TEXT NOT NULL,
        source_identity TEXT NOT NULL,
        identity_version TEXT NOT NULL,
        path TEXT,
        content_xxh3_128 TEXT NOT NULL,
        content_bytes INTEGER NOT NULL CHECK(content_bytes>=0),
        content_xxh3_64_guard TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        refresh_token TEXT,
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        updated_ns INTEGER NOT NULL,
        UNIQUE(source_kind,source_identity)
    ) WITHOUT ROWID""",
    """CREATE INDEX semantic_items_refresh_idx
        ON semantic_items(source_kind,refresh_token,active)""",
    """CREATE INDEX semantic_items_path_idx ON semantic_items(path)""",
    """CREATE TABLE text_chunks(
        chunk_id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal>=0),
        section_kind TEXT NOT NULL,
        section_id TEXT NOT NULL,
        start_char INTEGER NOT NULL CHECK(start_char>=0),
        end_char INTEGER NOT NULL CHECK(end_char>start_char),
        text_zlib BLOB NOT NULL,
        text_chars INTEGER NOT NULL CHECK(text_chars>0),
        content_xxh3_128 TEXT NOT NULL,
        content_bytes INTEGER NOT NULL CHECK(content_bytes>0),
        content_xxh3_64_guard TEXT NOT NULL,
        chunking_signature TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        refresh_token TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        updated_ns INTEGER NOT NULL,
        FOREIGN KEY(item_id) REFERENCES semantic_items(item_id)
    ) WITHOUT ROWID""",
    """CREATE INDEX text_chunks_item_active_idx
        ON text_chunks(item_id,active,chunking_signature,ordinal)""",
    """CREATE INDEX text_chunks_refresh_idx
        ON text_chunks(item_id,chunking_signature,refresh_token)""",
    """CREATE TABLE embedding_generations(
        generation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        model_signature TEXT NOT NULL,
        processing_signature TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN
            ('building','ready','ready_partial','failed')),
        provenance_json TEXT NOT NULL,
        cursor_json TEXT NOT NULL,
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        pending_count INTEGER NOT NULL DEFAULT 0,
        leased_count INTEGER NOT NULL DEFAULT 0,
        done_count INTEGER NOT NULL DEFAULT 0,
        error_count INTEGER NOT NULL DEFAULT 0,
        stale_count INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(model_signature) REFERENCES embedding_models(model_signature)
    )""",
    """CREATE INDEX embedding_generations_resume_idx
        ON embedding_generations(model_signature,processing_signature,status,generation_id)""",
    """CREATE TABLE vector_payloads(
        payload_id INTEGER PRIMARY KEY AUTOINCREMENT,
        model_signature TEXT NOT NULL,
        content_xxh3_128 TEXT NOT NULL,
        content_bytes INTEGER NOT NULL CHECK(content_bytes>=0),
        content_xxh3_64_guard TEXT NOT NULL,
        dimensions INTEGER NOT NULL CHECK(dimensions>0),
        vector_dtype TEXT NOT NULL CHECK(vector_dtype IN ('float16','float32')),
        vector_blob BLOB NOT NULL,
        original_norm REAL NOT NULL CHECK(original_norm>0),
        provenance_json TEXT NOT NULL,
        created_ns INTEGER NOT NULL,
        UNIQUE(model_signature,content_xxh3_128,content_bytes,content_xxh3_64_guard),
        FOREIGN KEY(model_signature) REFERENCES embedding_models(model_signature)
    )""",
    """CREATE TABLE text_embeddings(
        ref_id INTEGER PRIMARY KEY AUTOINCREMENT,
        chunk_id TEXT NOT NULL,
        model_signature TEXT NOT NULL,
        payload_id INTEGER NOT NULL,
        generation_id INTEGER NOT NULL,
        content_xxh3_128 TEXT NOT NULL,
        content_bytes INTEGER NOT NULL,
        content_xxh3_64_guard TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        updated_ns INTEGER NOT NULL,
        UNIQUE(chunk_id,model_signature),
        FOREIGN KEY(chunk_id) REFERENCES text_chunks(chunk_id),
        FOREIGN KEY(model_signature) REFERENCES embedding_models(model_signature),
        FOREIGN KEY(payload_id) REFERENCES vector_payloads(payload_id),
        FOREIGN KEY(generation_id) REFERENCES embedding_generations(generation_id)
    )""",
    """CREATE INDEX text_embeddings_search_idx
        ON text_embeddings(model_signature,ref_id)""",
    """CREATE TABLE image_embeddings(
        ref_id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id TEXT NOT NULL,
        model_signature TEXT NOT NULL,
        payload_id INTEGER NOT NULL,
        generation_id INTEGER NOT NULL,
        content_xxh3_128 TEXT NOT NULL,
        content_bytes INTEGER NOT NULL,
        content_xxh3_64_guard TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        updated_ns INTEGER NOT NULL,
        UNIQUE(item_id,model_signature),
        FOREIGN KEY(item_id) REFERENCES semantic_items(item_id),
        FOREIGN KEY(model_signature) REFERENCES embedding_models(model_signature),
        FOREIGN KEY(payload_id) REFERENCES vector_payloads(payload_id),
        FOREIGN KEY(generation_id) REFERENCES embedding_generations(generation_id)
    )""",
    """CREATE INDEX image_embeddings_search_idx
        ON image_embeddings(model_signature,ref_id)""",
    """CREATE TABLE embedding_jobs(
        job_id INTEGER PRIMARY KEY AUTOINCREMENT,
        generation_id INTEGER NOT NULL,
        model_signature TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('passage','image')),
        entity_kind TEXT NOT NULL CHECK(entity_kind IN ('text_chunk','image_item')),
        entity_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        content_xxh3_128 TEXT NOT NULL,
        content_bytes INTEGER NOT NULL,
        content_xxh3_64_guard TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN
            ('pending','leased','done','error','stale')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
        max_attempts INTEGER NOT NULL CHECK(max_attempts>0),
        available_ns INTEGER NOT NULL,
        lease_owner TEXT,
        lease_until_ns INTEGER,
        error_type TEXT,
        error_message TEXT,
        created_ns INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL,
        UNIQUE(generation_id,entity_kind,entity_id),
        FOREIGN KEY(generation_id) REFERENCES embedding_generations(generation_id),
        FOREIGN KEY(model_signature) REFERENCES embedding_models(model_signature),
        FOREIGN KEY(item_id) REFERENCES semantic_items(item_id)
    )""",
    """CREATE INDEX embedding_jobs_claim_idx
        ON embedding_jobs(generation_id,status,available_ns,lease_until_ns,job_id)""",
    """CREATE INDEX embedding_jobs_cache_idx
        ON embedding_jobs(model_signature,content_xxh3_128,content_bytes,
                          content_xxh3_64_guard,status)""",
)

_MIGRATION_2 = (
    """CREATE TABLE label_prototypes(
        prototype_id TEXT PRIMARY KEY,
        ontology_id TEXT NOT NULL,
        ontology_version TEXT NOT NULL,
        concept_id TEXT NOT NULL,
        prototype_version TEXT NOT NULL,
        model_signature TEXT NOT NULL,
        vector_space TEXT NOT NULL,
        prototype_text TEXT NOT NULL,
        content_xxh3_128 TEXT NOT NULL,
        content_bytes INTEGER NOT NULL CHECK(content_bytes>0),
        content_xxh3_64_guard TEXT NOT NULL,
        dimensions INTEGER NOT NULL CHECK(dimensions>0),
        vector_dtype TEXT NOT NULL CHECK(vector_dtype IN ('float16','float32')),
        vector_blob BLOB NOT NULL,
        original_norm REAL NOT NULL CHECK(original_norm>0),
        calibration_status TEXT NOT NULL CHECK(calibration_status IN
            ('uncalibrated','calibrated')),
        feedback_reference TEXT,
        provenance_json TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        created_ns INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL,
        UNIQUE(ontology_id,ontology_version,concept_id,prototype_version,
               model_signature),
        FOREIGN KEY(model_signature) REFERENCES embedding_models(model_signature),
        FOREIGN KEY(vector_space) REFERENCES vector_spaces(vector_space)
    ) WITHOUT ROWID""",
    """CREATE INDEX label_prototypes_lookup_idx
        ON label_prototypes(ontology_id,ontology_version,vector_space,active,
                            concept_id)""",
    """CREATE TABLE semantic_evidence(
        evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id TEXT NOT NULL,
        source_entity_id TEXT NOT NULL,
        ontology_id TEXT NOT NULL,
        ontology_version TEXT NOT NULL,
        concept_id TEXT NOT NULL,
        prototype_id TEXT NOT NULL,
        query_model_signature TEXT NOT NULL,
        indexed_model_signature TEXT NOT NULL,
        vector_space TEXT NOT NULL,
        score REAL NOT NULL CHECK(score>=-1.0 AND score<=1.0),
        rank INTEGER NOT NULL CHECK(rank>0),
        generation_id INTEGER,
        calibration_status TEXT NOT NULL CHECK(calibration_status IN
            ('uncalibrated','calibrated')),
        disposition TEXT NOT NULL CHECK(disposition IN
            ('advisory','confirmed','rejected')),
        feedback_reference TEXT,
        provenance_json TEXT NOT NULL,
        refresh_token TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        updated_ns INTEGER NOT NULL,
        UNIQUE(item_id,source_entity_id,ontology_id,ontology_version,concept_id,
               prototype_id,query_model_signature,indexed_model_signature),
        FOREIGN KEY(item_id) REFERENCES semantic_items(item_id),
        FOREIGN KEY(prototype_id) REFERENCES label_prototypes(prototype_id),
        FOREIGN KEY(query_model_signature)
            REFERENCES embedding_models(model_signature),
        FOREIGN KEY(indexed_model_signature)
            REFERENCES embedding_models(model_signature),
        FOREIGN KEY(vector_space) REFERENCES vector_spaces(vector_space),
        FOREIGN KEY(generation_id)
            REFERENCES embedding_generations(generation_id)
    )""",
    """CREATE INDEX semantic_evidence_item_idx
        ON semantic_evidence(item_id,ontology_id,ontology_version,active,
                             rank,score DESC)""",
    """CREATE INDEX semantic_evidence_refresh_idx
        ON semantic_evidence(item_id,ontology_id,ontology_version,vector_space,
                             refresh_token,active)""",
)

_MIGRATION_3 = (
    """ALTER TABLE semantic_items
        ADD COLUMN source_revision_json TEXT NOT NULL DEFAULT '{}'""",
)

_MIGRATION_4 = (
    """CREATE INDEX semantic_evidence_model_refresh_idx
        ON semantic_evidence(ontology_id,ontology_version,vector_space,
                             indexed_model_signature,active,refresh_token)""",
)

_MIGRATION_5 = (
    """CREATE TABLE text_channel_revisions(
        item_id TEXT NOT NULL,
        channel TEXT NOT NULL,
        revision_token TEXT NOT NULL,
        updated_ns INTEGER NOT NULL,
        PRIMARY KEY(item_id,channel),
        FOREIGN KEY(item_id) REFERENCES semantic_items(item_id)
    ) WITHOUT ROWID""",
)

_MIGRATION_6 = (
    """ALTER TABLE embedding_generations
        ADD COLUMN base_generation_id INTEGER
        REFERENCES embedding_generations(generation_id)""",
    """ALTER TABLE embedding_generations
        ADD COLUMN base_clone_complete INTEGER NOT NULL DEFAULT 0
        CHECK(base_clone_complete IN (0,1))""",
    """CREATE INDEX embedding_generations_base_idx
        ON embedding_generations(model_signature,base_generation_id,status,
                                 generation_id)""",
    """CREATE TABLE semantic_item_revisions(
        item_revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        source_identity TEXT NOT NULL,
        identity_version TEXT NOT NULL,
        path TEXT,
        content_xxh3_128 TEXT NOT NULL,
        content_bytes INTEGER NOT NULL CHECK(content_bytes>=0),
        content_xxh3_64_guard TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        source_revision_json TEXT NOT NULL,
        captured_ns INTEGER NOT NULL,
        FOREIGN KEY(item_id) REFERENCES semantic_items(item_id)
    )""",
    """CREATE INDEX semantic_item_revisions_item_idx
        ON semantic_item_revisions(item_id,item_revision_id)""",
    """CREATE TABLE semantic_chunk_revisions(
        chunk_revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
        chunk_id TEXT NOT NULL UNIQUE,
        item_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal>=0),
        section_kind TEXT NOT NULL,
        section_id TEXT NOT NULL,
        start_char INTEGER NOT NULL CHECK(start_char>=0),
        end_char INTEGER NOT NULL CHECK(end_char>start_char),
        text_zlib BLOB NOT NULL,
        text_chars INTEGER NOT NULL CHECK(text_chars>0),
        content_xxh3_128 TEXT NOT NULL,
        content_bytes INTEGER NOT NULL CHECK(content_bytes>0),
        content_xxh3_64_guard TEXT NOT NULL,
        chunking_signature TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        captured_ns INTEGER NOT NULL,
        FOREIGN KEY(item_id) REFERENCES semantic_items(item_id)
    )""",
    """CREATE INDEX semantic_chunk_revisions_item_idx
        ON semantic_chunk_revisions(item_id,chunk_revision_id)""",
    """CREATE TABLE embedding_generation_members(
        member_id INTEGER PRIMARY KEY AUTOINCREMENT,
        generation_id INTEGER NOT NULL,
        model_signature TEXT NOT NULL,
        entity_kind TEXT NOT NULL CHECK(entity_kind IN
            ('text_chunk','image_item')),
        entity_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        item_revision_id INTEGER NOT NULL,
        chunk_revision_id INTEGER,
        payload_id INTEGER NOT NULL,
        content_xxh3_128 TEXT NOT NULL,
        content_bytes INTEGER NOT NULL CHECK(content_bytes>=0),
        content_xxh3_64_guard TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        updated_ns INTEGER NOT NULL,
        base_member_id INTEGER,
        UNIQUE(generation_id,entity_kind,entity_id),
        CHECK((entity_kind='text_chunk' AND chunk_revision_id IS NOT NULL) OR
              (entity_kind='image_item' AND chunk_revision_id IS NULL)),
        FOREIGN KEY(generation_id)
            REFERENCES embedding_generations(generation_id),
        FOREIGN KEY(model_signature)
            REFERENCES embedding_models(model_signature),
        FOREIGN KEY(item_revision_id)
            REFERENCES semantic_item_revisions(item_revision_id),
        FOREIGN KEY(chunk_revision_id)
            REFERENCES semantic_chunk_revisions(chunk_revision_id),
        FOREIGN KEY(payload_id) REFERENCES vector_payloads(payload_id)
    )""",
    """CREATE INDEX embedding_generation_members_search_idx
        ON embedding_generation_members(
            generation_id,model_signature,entity_kind,member_id)""",
    """CREATE INDEX embedding_generation_members_clone_idx
        ON embedding_generation_members(generation_id,base_member_id)""",
    """CREATE TABLE published_embedding_heads(
        model_signature TEXT PRIMARY KEY,
        generation_id INTEGER NOT NULL UNIQUE,
        published_ns INTEGER NOT NULL,
        FOREIGN KEY(model_signature)
            REFERENCES embedding_models(model_signature),
        FOREIGN KEY(generation_id)
            REFERENCES embedding_generations(generation_id)
    ) WITHOUT ROWID""",
)

_MIGRATION_7 = (
    """ALTER TABLE vector_payloads
        ADD COLUMN legacy_before_receipts INTEGER NOT NULL DEFAULT 0
        CHECK(legacy_before_receipts IN (0,1))""",
    """UPDATE vector_payloads SET legacy_before_receipts=1""",
    """CREATE TRIGGER vector_payloads_no_update
        BEFORE UPDATE ON vector_payloads BEGIN
            SELECT RAISE(ABORT,'semantic vector payloads are append-only');
        END""",
    """CREATE TRIGGER vector_payloads_no_delete
        BEFORE DELETE ON vector_payloads BEGIN
            SELECT RAISE(ABORT,'semantic vector payloads are append-only');
        END""",
    """ALTER TABLE embedding_jobs
        ADD COLUMN attempt_started_ns INTEGER
        CHECK(attempt_started_ns IS NULL OR attempt_started_ns>=0)""",
    """ALTER TABLE embedding_jobs
        ADD COLUMN attempt_sequence INTEGER NOT NULL DEFAULT 0
        CHECK(attempt_sequence>=0)""",
    """ALTER TABLE embedding_jobs
        ADD COLUMN input_item_revision_id INTEGER
        REFERENCES semantic_item_revisions(item_revision_id)""",
    """ALTER TABLE embedding_jobs
        ADD COLUMN input_chunk_revision_id INTEGER
        REFERENCES semantic_chunk_revisions(chunk_revision_id)""",
    """CREATE INDEX semantic_item_revisions_source_revision_idx
        ON semantic_item_revisions(
            json_extract(source_revision_json,'$.revision_id'),item_revision_id)
        WHERE json_type(source_revision_json,'$.revision_id')='text'""",
    """CREATE TRIGGER semantic_item_revisions_no_update
        BEFORE UPDATE ON semantic_item_revisions BEGIN
            SELECT RAISE(ABORT,'semantic item revisions are append-only');
        END""",
    """CREATE TRIGGER semantic_item_revisions_no_delete
        BEFORE DELETE ON semantic_item_revisions BEGIN
            SELECT RAISE(ABORT,'semantic item revisions are append-only');
        END""",
    """CREATE TRIGGER semantic_chunk_revisions_no_update
        BEFORE UPDATE ON semantic_chunk_revisions BEGIN
            SELECT RAISE(ABORT,'semantic chunk revisions are append-only');
        END""",
    """CREATE TRIGGER semantic_chunk_revisions_no_delete
        BEFORE DELETE ON semantic_chunk_revisions BEGIN
            SELECT RAISE(ABORT,'semantic chunk revisions are append-only');
        END""",
    """CREATE TABLE semantic_work_receipts(
        receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
        receipt_key TEXT NOT NULL UNIQUE,
        contract_version TEXT NOT NULL
            CHECK(contract_version='neocortex.work-receipt/v1'),
        stage_id TEXT NOT NULL,
        stage_version TEXT NOT NULL,
        processing_signature TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN
            ('succeeded','failed','cancelled','abandoned')),
        execution_mode TEXT NOT NULL CHECK(execution_mode IN
            ('executed','cache_hit','replay','attempted','unknown')),
        reproducibility_class TEXT NOT NULL CHECK(reproducibility_class IN
            ('exact','environment_bound','seeded','equivalent','best_effort',
             'non_replayable')),
        entity_kind TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        item_revision_id INTEGER,
        chunk_revision_id INTEGER,
        generation_id INTEGER,
        model_signature TEXT,
        payload_id INTEGER,
        job_id INTEGER,
        attempt INTEGER CHECK(attempt IS NULL OR attempt>=1),
        started_ns INTEGER CHECK(started_ns IS NULL OR started_ns>=0),
        finished_ns INTEGER CHECK(finished_ns IS NULL OR finished_ns>=0),
        duration_ns INTEGER CHECK(duration_ns IS NULL OR duration_ns>=0),
        receipt_json TEXT NOT NULL,
        committed_ns INTEGER NOT NULL CHECK(committed_ns>=0),
        CHECK(finished_ns IS NULL OR started_ns IS NULL OR
              finished_ns>=started_ns),
        FOREIGN KEY(item_revision_id)
            REFERENCES semantic_item_revisions(item_revision_id),
        FOREIGN KEY(chunk_revision_id)
            REFERENCES semantic_chunk_revisions(chunk_revision_id),
        FOREIGN KEY(generation_id)
            REFERENCES embedding_generations(generation_id),
        FOREIGN KEY(model_signature)
            REFERENCES embedding_models(model_signature),
        FOREIGN KEY(payload_id) REFERENCES vector_payloads(payload_id)
    )""",
    """CREATE INDEX semantic_work_receipts_entity_idx
        ON semantic_work_receipts(entity_kind,entity_id,receipt_id)""",
    """CREATE INDEX semantic_work_receipts_generation_idx
        ON semantic_work_receipts(generation_id,model_signature,receipt_id)""",
    """CREATE INDEX semantic_work_receipts_embedding_lookup_idx
        ON semantic_work_receipts(
            stage_id,status,entity_id,generation_id,payload_id,receipt_id)""",
    """CREATE TRIGGER semantic_work_receipts_no_update
        BEFORE UPDATE ON semantic_work_receipts BEGIN
            SELECT RAISE(ABORT,'semantic work receipts are append-only');
        END""",
    """CREATE TRIGGER semantic_work_receipts_no_delete
        BEFORE DELETE ON semantic_work_receipts BEGIN
            SELECT RAISE(ABORT,'semantic work receipts are append-only');
        END""",
    """CREATE TABLE semantic_chunk_derivations(
        derivation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        chunk_revision_id INTEGER NOT NULL,
        item_revision_id INTEGER NOT NULL,
        refresh_token TEXT NOT NULL,
        materialization_receipt_id INTEGER NOT NULL UNIQUE,
        publication_receipt_id INTEGER,
        created_ns INTEGER NOT NULL CHECK(created_ns>=0),
        UNIQUE(chunk_revision_id,item_revision_id,refresh_token),
        FOREIGN KEY(chunk_revision_id)
            REFERENCES semantic_chunk_revisions(chunk_revision_id),
        FOREIGN KEY(item_revision_id)
            REFERENCES semantic_item_revisions(item_revision_id),
        FOREIGN KEY(materialization_receipt_id)
            REFERENCES semantic_work_receipts(receipt_id),
        FOREIGN KEY(publication_receipt_id)
            REFERENCES semantic_work_receipts(receipt_id)
    )""",
    """CREATE INDEX semantic_chunk_derivations_chunk_idx
        ON semantic_chunk_derivations(
            chunk_revision_id,item_revision_id,derivation_id)""",
    """CREATE INDEX semantic_chunk_derivations_refresh_idx
        ON semantic_chunk_derivations(refresh_token,publication_receipt_id,
                                      derivation_id)""",
    """CREATE TRIGGER semantic_chunk_derivations_publication_once
        BEFORE UPDATE ON semantic_chunk_derivations
        WHEN NOT (
            OLD.publication_receipt_id IS NULL AND
            NEW.publication_receipt_id IS NOT NULL AND
            NEW.derivation_id=OLD.derivation_id AND
            NEW.chunk_revision_id=OLD.chunk_revision_id AND
            NEW.item_revision_id=OLD.item_revision_id AND
            NEW.refresh_token=OLD.refresh_token AND
            NEW.materialization_receipt_id=OLD.materialization_receipt_id AND
            NEW.created_ns=OLD.created_ns)
        BEGIN
            SELECT RAISE(ABORT,'semantic chunk derivations are immutable after publication');
        END""",
    """CREATE TRIGGER semantic_chunk_derivations_no_delete
        BEFORE DELETE ON semantic_chunk_derivations BEGIN
            SELECT RAISE(ABORT,'semantic chunk derivations are append-only');
        END""",
    """CREATE TABLE semantic_derivation_outbox(
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        receipt_id INTEGER NOT NULL UNIQUE,
        event_kind TEXT NOT NULL,
        aggregate_kind TEXT NOT NULL,
        aggregate_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        committed_ns INTEGER NOT NULL CHECK(committed_ns>=0),
        FOREIGN KEY(receipt_id) REFERENCES semantic_work_receipts(receipt_id)
    )""",
    """CREATE INDEX semantic_derivation_outbox_scan_idx
        ON semantic_derivation_outbox(event_id,event_kind)""",
    """CREATE TRIGGER semantic_derivation_outbox_no_update
        BEFORE UPDATE ON semantic_derivation_outbox BEGIN
            SELECT RAISE(ABORT,'semantic derivation outbox is append-only');
        END""",
    """CREATE TRIGGER semantic_derivation_outbox_no_delete
        BEFORE DELETE ON semantic_derivation_outbox BEGIN
            SELECT RAISE(ABORT,'semantic derivation outbox is append-only');
        END""",
)


def _execute_migration(
    connection: sqlite3.Connection,
    statements: tuple[str, ...],
    *,
    version: int,
    description: str,
    applied_ns: int,
) -> None:
    for statement in statements:
        connection.execute(statement)
    connection.execute(
        "INSERT INTO schema_migrations(version,description,applied_ns) VALUES(?,?,?)",
        (version, description, applied_ns),
    )


def _migrate_to_v1(connection: sqlite3.Connection, applied_ns: int) -> None:
    _execute_migration(
        connection,
        _MIGRATION_1,
        version=1,
        description="initial semantic vector state",
        applied_ns=applied_ns,
    )


def _migrate_to_v2(connection: sqlite3.Connection, applied_ns: int) -> None:
    _execute_migration(
        connection,
        _MIGRATION_2,
        version=2,
        description="versioned label prototypes and advisory semantic evidence",
        applied_ns=applied_ns,
    )


def _migrate_to_v3(connection: sqlite3.Connection, applied_ns: int) -> None:
    _execute_migration(
        connection,
        _MIGRATION_3,
        version=3,
        description="explicit source revision metadata for mutation checks",
        applied_ns=applied_ns,
    )


def _migrate_to_v4(connection: sqlite3.Connection, applied_ns: int) -> None:
    _execute_migration(
        connection,
        _MIGRATION_4,
        version=4,
        description="bounded model-scoped semantic evidence refresh",
        applied_ns=applied_ns,
    )


def _migrate_to_v5(connection: sqlite3.Connection, applied_ns: int) -> None:
    _execute_migration(
        connection,
        _MIGRATION_5,
        version=5,
        description="independent text-channel source revisions",
        applied_ns=applied_ns,
    )


def _migrate_to_v6(connection: sqlite3.Connection, applied_ns: int) -> None:
    """Add published semantic snapshots and import the exact v5 visible view."""

    for statement in _MIGRATION_6:
        connection.execute(statement)

    # Revisions are immutable inputs to a published member.  The v5 import
    # snapshots only rows that its readers could actually observe; every legacy
    # table and row remains intact for diagnosis and rollback via backup.
    connection.execute(
        """INSERT INTO semantic_item_revisions(
            item_id,source_kind,source_identity,identity_version,path,
            content_xxh3_128,content_bytes,content_xxh3_64_guard,
            provenance_json,source_revision_json,captured_ns)
        SELECT i.item_id,i.source_kind,i.source_identity,i.identity_version,i.path,
            i.content_xxh3_128,i.content_bytes,i.content_xxh3_64_guard,
            i.provenance_json,i.source_revision_json,?
        FROM semantic_items i
        WHERE i.active=1 AND (
            EXISTS(
                SELECT 1 FROM text_embeddings e
                JOIN text_chunks c ON c.chunk_id=e.chunk_id
                WHERE c.item_id=i.item_id AND c.active=1
                  AND e.content_xxh3_128=c.content_xxh3_128
                  AND e.content_bytes=c.content_bytes
                  AND e.content_xxh3_64_guard=c.content_xxh3_64_guard)
            OR EXISTS(
                SELECT 1 FROM image_embeddings e
                WHERE e.item_id=i.item_id
                  AND e.content_xxh3_128=i.content_xxh3_128
                  AND e.content_bytes=i.content_bytes
                  AND e.content_xxh3_64_guard=i.content_xxh3_64_guard))""",
        (applied_ns,),
    )
    connection.execute(
        """INSERT INTO semantic_chunk_revisions(
            chunk_id,item_id,ordinal,section_kind,section_id,start_char,end_char,
            text_zlib,text_chars,content_xxh3_128,content_bytes,
            content_xxh3_64_guard,chunking_signature,provenance_json,captured_ns)
        SELECT c.chunk_id,c.item_id,c.ordinal,c.section_kind,c.section_id,
            c.start_char,c.end_char,c.text_zlib,c.text_chars,c.content_xxh3_128,
            c.content_bytes,c.content_xxh3_64_guard,c.chunking_signature,
            c.provenance_json,?
        FROM text_chunks c JOIN semantic_items i ON i.item_id=c.item_id
        WHERE c.active=1 AND i.active=1 AND EXISTS(
            SELECT 1 FROM text_embeddings e
            WHERE e.chunk_id=c.chunk_id
              AND e.content_xxh3_128=c.content_xxh3_128
              AND e.content_bytes=c.content_bytes
              AND e.content_xxh3_64_guard=c.content_xxh3_64_guard)""",
        (applied_ns,),
    )

    models = tuple(
        str(row[0])
        for row in connection.execute(
            """SELECT model_signature FROM text_embeddings e
            JOIN text_chunks c ON c.chunk_id=e.chunk_id
            JOIN semantic_items i ON i.item_id=c.item_id
            WHERE c.active=1 AND i.active=1
              AND e.content_xxh3_128=c.content_xxh3_128
              AND e.content_bytes=c.content_bytes
              AND e.content_xxh3_64_guard=c.content_xxh3_64_guard
            UNION
            SELECT model_signature FROM image_embeddings e
            JOIN semantic_items i ON i.item_id=e.item_id
            WHERE i.active=1
              AND e.content_xxh3_128=i.content_xxh3_128
              AND e.content_bytes=i.content_bytes
              AND e.content_xxh3_64_guard=i.content_xxh3_64_guard
            ORDER BY model_signature"""
        )
    )
    for model_signature in models:
        text_count = int(
            connection.execute(
                """SELECT COUNT(*) FROM text_embeddings e
                JOIN text_chunks c ON c.chunk_id=e.chunk_id
                JOIN semantic_items i ON i.item_id=c.item_id
                WHERE e.model_signature=? AND c.active=1 AND i.active=1
                  AND e.content_xxh3_128=c.content_xxh3_128
                  AND e.content_bytes=c.content_bytes
                  AND e.content_xxh3_64_guard=c.content_xxh3_64_guard""",
                (model_signature,),
            ).fetchone()[0]
        )
        image_count = int(
            connection.execute(
                """SELECT COUNT(*) FROM image_embeddings e
                JOIN semantic_items i ON i.item_id=e.item_id
                WHERE e.model_signature=? AND i.active=1
                  AND e.content_xxh3_128=i.content_xxh3_128
                  AND e.content_bytes=i.content_bytes
                  AND e.content_xxh3_64_guard=i.content_xxh3_64_guard""",
                (model_signature,),
            ).fetchone()[0]
        )
        expected_count = text_count + image_count
        cursor = connection.execute(
            """INSERT INTO embedding_generations(
                model_signature,processing_signature,status,provenance_json,
                cursor_json,started_ns,completed_ns,pending_count,leased_count,
                done_count,error_count,stale_count,base_generation_id,
                base_clone_complete)
            VALUES(?,?,'ready',?,?,?, ?,0,0,?,0,0,NULL,1)""",
            (
                model_signature,
                "legacy-v5-visible-snapshot-v1",
                '{"migration":"semantic-v5-to-v6"}',
                '{"imported":"v5-visible-view"}',
                applied_ns,
                applied_ns,
                expected_count,
            ),
        )
        if cursor.lastrowid is None:
            raise SemanticStateError("v5 semantic import produced no generation id")
        generation_id = int(cursor.lastrowid)
        connection.execute(
            """INSERT INTO embedding_generation_members(
                generation_id,model_signature,entity_kind,entity_id,item_id,
                item_revision_id,chunk_revision_id,payload_id,content_xxh3_128,
                content_bytes,content_xxh3_64_guard,provenance_json,updated_ns)
            SELECT ?,e.model_signature,'text_chunk',e.chunk_id,c.item_id,
                ir.item_revision_id,cr.chunk_revision_id,e.payload_id,
                e.content_xxh3_128,e.content_bytes,e.content_xxh3_64_guard,
                e.provenance_json,e.updated_ns
            FROM text_embeddings e
            JOIN text_chunks c ON c.chunk_id=e.chunk_id
            JOIN semantic_items i ON i.item_id=c.item_id
            JOIN semantic_item_revisions ir ON ir.item_id=i.item_id
            JOIN semantic_chunk_revisions cr ON cr.chunk_id=c.chunk_id
            WHERE e.model_signature=? AND c.active=1 AND i.active=1
              AND e.content_xxh3_128=c.content_xxh3_128
              AND e.content_bytes=c.content_bytes
              AND e.content_xxh3_64_guard=c.content_xxh3_64_guard""",
            (generation_id, model_signature),
        )
        connection.execute(
            """INSERT INTO embedding_generation_members(
                generation_id,model_signature,entity_kind,entity_id,item_id,
                item_revision_id,chunk_revision_id,payload_id,content_xxh3_128,
                content_bytes,content_xxh3_64_guard,provenance_json,updated_ns)
            SELECT ?,e.model_signature,'image_item',e.item_id,e.item_id,
                ir.item_revision_id,NULL,e.payload_id,e.content_xxh3_128,
                e.content_bytes,e.content_xxh3_64_guard,e.provenance_json,e.updated_ns
            FROM image_embeddings e
            JOIN semantic_items i ON i.item_id=e.item_id
            JOIN semantic_item_revisions ir ON ir.item_id=i.item_id
            WHERE e.model_signature=? AND i.active=1
              AND e.content_xxh3_128=i.content_xxh3_128
              AND e.content_bytes=i.content_bytes
              AND e.content_xxh3_64_guard=i.content_xxh3_64_guard""",
            (generation_id, model_signature),
        )
        actual_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM embedding_generation_members WHERE generation_id=?",
                (generation_id,),
            ).fetchone()[0]
        )
        if actual_count != expected_count:
            raise SemanticStateError("v5 semantic visible-row count changed during migration")
        connection.execute(
            """INSERT INTO published_embedding_heads(
                model_signature,generation_id,published_ns) VALUES(?,?,?)""",
            (model_signature, generation_id, applied_ns),
        )

    foreign_key_error = connection.execute("PRAGMA foreign_key_check").fetchone()
    if foreign_key_error is not None:
        raise SemanticStateError(
            f"semantic v6 migration created a foreign-key violation: {tuple(foreign_key_error)!r}"
        )
    integrity = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check"))
    if integrity != ("ok",):
        raise SemanticStateError(f"semantic v6 migration failed integrity_check: {integrity!r}")
    connection.execute(
        "INSERT INTO schema_migrations(version,description,applied_ns) VALUES(6,?,?)",
        ("atomic published embedding snapshots", applied_ns),
    )


def _migrate_to_v7(connection: sqlite3.Connection, applied_ns: int) -> None:
    """Add owner-local derivation receipts without inventing legacy facts."""

    _execute_migration(
        connection,
        _MIGRATION_7,
        version=7,
        description="owner-local semantic derivation receipts and outbox",
        applied_ns=applied_ns,
    )


_MIGRATIONS_BY_TARGET: dict[int, Callable[[sqlite3.Connection, int], None]] = {
    1: _migrate_to_v1,
    2: _migrate_to_v2,
    3: _migrate_to_v3,
    4: _migrate_to_v4,
    5: _migrate_to_v5,
    6: _migrate_to_v6,
    7: _migrate_to_v7,
}

_TABLE_NAMES_BY_VERSION = {
    1: (
        "metadata",
        "schema_migrations",
        "vector_spaces",
        "embedding_models",
        "semantic_items",
        "text_chunks",
        "embedding_generations",
        "vector_payloads",
        "text_embeddings",
        "image_embeddings",
        "embedding_jobs",
    ),
    2: ("label_prototypes", "semantic_evidence"),
    3: (),
    4: (),
    5: ("text_channel_revisions",),
    6: (
        "semantic_item_revisions",
        "semantic_chunk_revisions",
        "embedding_generation_members",
        "published_embedding_heads",
    ),
    7: (
        "semantic_work_receipts",
        "semantic_chunk_derivations",
        "semantic_derivation_outbox",
    ),
}

_NAMED_INDEXES_BY_VERSION = {
    1: {
        "embedding_models_space_idx": "embedding_models",
        "semantic_items_refresh_idx": "semantic_items",
        "semantic_items_path_idx": "semantic_items",
        "text_chunks_item_active_idx": "text_chunks",
        "text_chunks_refresh_idx": "text_chunks",
        "embedding_generations_resume_idx": "embedding_generations",
        "text_embeddings_search_idx": "text_embeddings",
        "image_embeddings_search_idx": "image_embeddings",
        "embedding_jobs_claim_idx": "embedding_jobs",
        "embedding_jobs_cache_idx": "embedding_jobs",
    },
    2: {
        "label_prototypes_lookup_idx": "label_prototypes",
        "semantic_evidence_item_idx": "semantic_evidence",
        "semantic_evidence_refresh_idx": "semantic_evidence",
    },
    3: {},
    4: {"semantic_evidence_model_refresh_idx": "semantic_evidence"},
    5: {},
    6: {
        "embedding_generations_base_idx": "embedding_generations",
        "semantic_item_revisions_item_idx": "semantic_item_revisions",
        "semantic_chunk_revisions_item_idx": "semantic_chunk_revisions",
        "embedding_generation_members_search_idx": "embedding_generation_members",
        "embedding_generation_members_clone_idx": "embedding_generation_members",
    },
    7: {
        "semantic_item_revisions_source_revision_idx": "semantic_item_revisions",
        "semantic_work_receipts_entity_idx": "semantic_work_receipts",
        "semantic_work_receipts_generation_idx": "semantic_work_receipts",
        "semantic_work_receipts_embedding_lookup_idx": "semantic_work_receipts",
        "semantic_chunk_derivations_chunk_idx": "semantic_chunk_derivations",
        "semantic_chunk_derivations_refresh_idx": "semantic_chunk_derivations",
        "semantic_derivation_outbox_scan_idx": "semantic_derivation_outbox",
    },
}


# endregion [02]


# region [03] Versioned schema contract


@dataclass(frozen=True, slots=True)
class _ColumnContract:
    declared_type: str
    not_null: bool
    default_sql: str | None
    primary_key_position: int


@dataclass(frozen=True, slots=True)
class _TableContract:
    columns: dict[str, _ColumnContract]
    without_rowid: bool
    strict: bool


@dataclass(frozen=True, slots=True)
class _IndexColumnContract:
    name: str
    descending: bool
    collation: str


@dataclass(frozen=True, slots=True)
class _SchemaContract:
    tables: dict[str, _TableContract]
    indexes: dict[
        str,
        tuple[str, tuple[_IndexColumnContract, ...], bool, bool],
    ]
    unique_keys: frozenset[tuple[str, tuple[_IndexColumnContract, ...]]]


def _quoted_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_options(connection: sqlite3.Connection) -> dict[str, tuple[bool, bool]]:
    return {
        str(row[1]): (bool(row[4]), bool(row[5]))
        for row in connection.execute("PRAGMA table_list")
        if str(row[2]) == "table"
    }


def _index_columns(
    connection: sqlite3.Connection,
    index: str,
) -> tuple[_IndexColumnContract, ...]:
    rows = connection.execute(f"PRAGMA index_xinfo({_quoted_identifier(index)})").fetchall()
    return tuple(
        _IndexColumnContract(
            name=(str(row[2]) if row[2] is not None else f"<expression:{int(row[1])}>"),
            descending=bool(row[3]),
            collation=str(row[4] or "BINARY"),
        )
        for row in rows
        if bool(row[5])
    )


def _tables_through(version: int) -> tuple[str, ...]:
    return tuple(
        table for target in range(1, version + 1) for table in _TABLE_NAMES_BY_VERSION[target]
    )


def _named_indexes_through(version: int) -> dict[str, str]:
    return {
        index: table
        for target in range(1, version + 1)
        for index, table in _NAMED_INDEXES_BY_VERSION[target].items()
    }


def _build_exact_current_schema(connection: sqlite3.Connection) -> None:
    for target in range(1, SEMANTIC_SCHEMA_VERSION + 1):
        _MIGRATIONS_BY_TARGET[target](connection, target)


@lru_cache(maxsize=1)
def _exact_current_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_exact_current_schema)


@lru_cache(maxsize=SEMANTIC_SCHEMA_VERSION)
def _canonical_contract(version: int) -> _SchemaContract:
    connection = sqlite3.connect(":memory:")
    try:
        for target in range(1, version + 1):
            _MIGRATIONS_BY_TARGET[target](connection, target)

        table_options = _table_options(connection)
        tables: dict[str, _TableContract] = {}
        unique_keys: set[tuple[str, tuple[_IndexColumnContract, ...]]] = set()
        for table in _tables_through(version):
            columns = {
                str(row[1]): _ColumnContract(
                    declared_type=str(row[2]).upper(),
                    not_null=bool(row[3]),
                    default_sql=None if row[4] is None else str(row[4]),
                    primary_key_position=int(row[5]),
                )
                for row in connection.execute(f"PRAGMA table_xinfo({_quoted_identifier(table)})")
            }
            without_rowid, strict = table_options[table]
            tables[table] = _TableContract(columns, without_rowid, strict)
            for row in connection.execute(f"PRAGMA index_list({_quoted_identifier(table)})"):
                if bool(row[2]) and str(row[3]) == "u":
                    unique_keys.add((table, _index_columns(connection, str(row[1]))))

        indexes: dict[
            str,
            tuple[str, tuple[_IndexColumnContract, ...], bool, bool],
        ] = {}
        for index, table in _named_indexes_through(version).items():
            row = next(
                (
                    item
                    for item in connection.execute(
                        f"PRAGMA index_list({_quoted_identifier(table)})"
                    )
                    if str(item[1]) == index
                ),
                None,
            )
            if row is None:  # pragma: no cover - canonical DDL invariant
                raise AssertionError(f"canonical index was not created: {index}")
            indexes[index] = (
                table,
                _index_columns(connection, index),
                bool(row[2]),
                bool(row[4]),
            )
        return _SchemaContract(tables, indexes, frozenset(unique_keys))
    finally:
        connection.close()


def _validate_schema(connection: sqlite3.Connection, version: int) -> None:
    expected = _canonical_contract(version)
    actual_objects = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    table_options = _table_options(connection)
    errors: list[str] = []

    for table, table_contract in expected.tables.items():
        object_type = actual_objects.get(table)
        if object_type != "table":
            errors.append(
                f"required table {table!r} is missing"
                if object_type is None
                else f"required table {table!r} is a {object_type}"
            )
            continue
        actual_columns = {
            str(row[1]): _ColumnContract(
                declared_type=str(row[2]).upper(),
                not_null=bool(row[3]),
                default_sql=None if row[4] is None else str(row[4]),
                primary_key_position=int(row[5]),
            )
            for row in connection.execute(f"PRAGMA table_xinfo({_quoted_identifier(table)})")
        }
        for column, column_contract in table_contract.columns.items():
            actual = actual_columns.get(column)
            if actual is None:
                errors.append(f"table {table!r} is missing column {column!r}")
            elif actual != column_contract:
                errors.append(f"table {table!r} column {column!r} has an invalid declaration")
        options = table_options.get(table)
        if options is not None and options != (
            table_contract.without_rowid,
            table_contract.strict,
        ):
            errors.append(f"table {table!r} has incompatible table options")

    for index, (table, columns, unique, partial) in expected.indexes.items():
        rows = {
            str(row[1]): row
            for row in connection.execute(f"PRAGMA index_list({_quoted_identifier(table)})")
        }
        actual = rows.get(index)
        if actual is None:
            errors.append(f"required index {index!r} is missing from table {table!r}")
            continue
        if bool(actual[2]) != unique or bool(actual[4]) != partial:
            errors.append(f"index {index!r} has incompatible options")
        if _index_columns(connection, index) != columns:
            errors.append(f"index {index!r} has incompatible columns")

    for table, columns in expected.unique_keys:
        actual_unique_keys = {
            _index_columns(connection, str(row[1]))
            for row in connection.execute(f"PRAGMA index_list({_quoted_identifier(table)})")
            if bool(row[2])
        }
        if columns not in actual_unique_keys:
            errors.append(f"table {table!r} is missing required unique key {columns!r}")

    if errors:
        raise SemanticStateError("semantic schema contract validation failed: " + "; ".join(errors))


def _application_objects(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }


def _read_metadata_version(
    connection: sqlite3.Connection,
    *,
    required: bool,
) -> int | None:
    metadata_type = connection.execute(
        "SELECT type FROM sqlite_master WHERE name='metadata'"
    ).fetchone()
    if metadata_type is None or str(metadata_type[0]) != "table":
        raise SemanticStateError("semantic database has no valid metadata table")
    try:
        rows = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version' LIMIT 2"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise SemanticStateError("semantic schema metadata is malformed") from exc
    if not rows:
        if required:
            raise SemanticStateError("semantic metadata lacks schema_version")
        return None
    if len(rows) != 1:
        raise SemanticStateError("semantic metadata has no unique schema_version")
    raw_version = str(rows[0][0])
    try:
        version = int(raw_version)
    except ValueError as exc:
        raise SemanticStateError(
            f"semantic schema version is not an integer: {raw_version!r}"
        ) from exc
    if raw_version != str(version):
        raise SemanticStateError(f"semantic schema version is not canonical: {raw_version!r}")
    return version


def _read_schema_version(connection: sqlite3.Connection) -> int | None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    objects = _application_objects(connection)
    if not objects:
        if version != 0:
            raise SemanticStateError("semantic database declares a version but contains no schema")
        return None
    if version < 1 or version > SEMANTIC_SCHEMA_VERSION:
        raise SemanticStateError(
            f"semantic schema {version} is unsupported; expected 1..{SEMANTIC_SCHEMA_VERSION}"
        )
    metadata_version = _read_metadata_version(
        connection,
        required=version == SEMANTIC_SCHEMA_VERSION,
    )
    if metadata_version is not None and metadata_version != version:
        raise SemanticStateError(
            "semantic metadata schema_version does not match PRAGMA user_version"
        )
    return version


def _validate_migration_history(
    connection: sqlite3.Connection,
    version: int,
) -> None:
    try:
        rows = connection.execute(
            "SELECT version,description,applied_ns FROM schema_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise SemanticStateError("semantic migration history is malformed") from exc
    versions = tuple(int(row[0]) for row in rows)
    expected = tuple(range(1, version + 1))
    if versions != expected:
        raise SemanticStateError(
            f"semantic migration history is {versions!r}; expected {expected!r}"
        )
    if any(not str(row[1]).strip() or int(row[2]) < 0 for row in rows):
        raise SemanticStateError("semantic migration history contains invalid records")


def _validate_version_contract(
    connection: sqlite3.Connection,
    version: int,
) -> None:
    _validate_schema(connection, version)
    if version == SEMANTIC_SCHEMA_VERSION:
        try:
            validate_sqlite_schema_contract(
                connection,
                _exact_current_contract(),
                label="semantic",
                exact=True,
            )
        except SQLiteSchemaContractError as exc:
            raise SemanticStateError(str(exc)) from exc
    _validate_migration_history(connection, version)


# endregion [03]


# region [04] Atomic initialization


def _store_schema_version(connection: sqlite3.Connection, version: int) -> None:
    connection.execute(f"PRAGMA user_version={version}")
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES('schema_version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(version),),
    )


def _migrate_from(connection: sqlite3.Connection, version: int | None) -> None:
    current = 0 if version is None else version
    while current < SEMANTIC_SCHEMA_VERSION:
        target = current + 1
        migration = _MIGRATIONS_BY_TARGET.get(target)
        if migration is None:  # pragma: no cover - module invariant
            raise SemanticStateError(f"semantic migration to v{target} is missing")
        migration(connection, time.time_ns())
        _store_schema_version(connection, target)
        current = target


def _inspect_existing_schema(path: Path) -> int | None:
    """Validate an existing semantic schema without opening a write transaction."""

    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        with semantic_database(path, readonly=True) as connection:
            version = _read_schema_version(connection)
            if version is not None:
                _validate_version_contract(connection, version)
            return version
    except sqlite3.DatabaseError as exc:
        raise SemanticStateError("semantic schema inspection failed") from exc


def initialize_semantic_state(path: Path) -> None:
    """Create or migrate semantic state atomically after contract validation."""

    initial_version = _inspect_existing_schema(path)
    if initial_version == SEMANTIC_SCHEMA_VERSION:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=60.0)
    try:
        _configure_common_connection(connection)
        _configure_write_connection(connection)
        connection.execute("BEGIN IMMEDIATE")
        locked_version = _read_schema_version(connection)
        if locked_version != initial_version:
            raise SemanticStateError("semantic schema changed during initialization")
        if locked_version is not None:
            _validate_version_contract(connection, locked_version)
        _migrate_from(connection, locked_version)
        _validate_version_contract(connection, SEMANTIC_SCHEMA_VERSION)
        connection.commit()
    except sqlite3.DatabaseError as exc:
        connection.rollback()
        source = "new" if initial_version is None else str(initial_version)
        raise SemanticStateError(
            f"semantic schema initialization from version {source} failed"
        ) from exc
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _scrub_retired_image_value(value: object) -> tuple[object, bool]:
    if isinstance(value, dict):
        changed = False
        cleaned: dict[str, object] = {}
        for key, child in value.items():
            if key in _RETIRED_IMAGE_KEYS or key.startswith("adult_"):
                changed = True
                continue
            scrubbed, child_changed = _scrub_retired_image_value(child)
            cleaned[str(key)] = scrubbed
            changed = changed or child_changed
        return cleaned, changed
    if isinstance(value, list):
        cleaned_list: list[object] = []
        changed = False
        for child in value:
            scrubbed, child_changed = _scrub_retired_image_value(child)
            cleaned_list.append(scrubbed)
            changed = changed or child_changed
        return cleaned_list, changed
    return value, False


def _scrub_json_payload(raw: object, *, label: str) -> tuple[str, bool]:
    try:
        decoded = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SemanticStateError(f"{label} is malformed") from exc
    scrubbed, changed = _scrub_retired_image_value(decoded)
    if not changed:
        return str(raw), False
    return (
        json.dumps(scrubbed, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        True,
    )


def _image_scrub_projection_queries() -> tuple[tuple[str, str], ...]:
    """Return mutable image projection scans and their human-readable labels."""

    return (
        (
            "semantic_items",
            "SELECT item_id,provenance_json,source_revision_json "
            "FROM semantic_items WHERE source_kind='image' ORDER BY item_id",
        ),
        (
            "image_embeddings",
            "SELECT e.ref_id,e.provenance_json FROM image_embeddings e "
            "JOIN semantic_items i ON i.item_id=e.item_id "
            "WHERE i.source_kind='image' ORDER BY e.ref_id",
        ),
        (
            "semantic_evidence",
            "SELECT e.evidence_id,e.provenance_json FROM semantic_evidence e "
            "JOIN semantic_items i ON i.item_id=e.item_id "
            "WHERE i.source_kind='image' ORDER BY e.evidence_id",
        ),
        (
            "embedding_generation_members",
            "SELECT m.member_id,m.provenance_json FROM embedding_generation_members m "
            "JOIN semantic_items i ON i.item_id=m.item_id "
            "WHERE m.entity_kind='image_item' AND i.source_kind='image' "
            "ORDER BY m.member_id",
        ),
        (
            "embedding_generations",
            "SELECT g.generation_id,g.provenance_json FROM embedding_generations g "
            "JOIN embedding_models m ON m.model_signature=g.model_signature "
            "WHERE m.modality='image' ORDER BY g.generation_id",
        ),
    )


def _image_scrub_has_work(connection: sqlite3.Connection) -> bool:
    for label, query in _image_scrub_projection_queries():
        for row in connection.execute(query):
            identifier = str(row[0])
            if label == "semantic_items":
                _, provenance_changed = _scrub_json_payload(
                    row[1], label=f"{label} provenance for {identifier}"
                )
                _, revision_changed = _scrub_json_payload(
                    row[2], label=f"{label} source revision for {identifier}"
                )
                if provenance_changed or revision_changed:
                    return True
            else:
                _, changed = _scrub_json_payload(
                    row[1], label=f"{label} provenance for {identifier}"
                )
                if changed:
                    return True
    return False


def scrub_retired_image_provenance(path: Path) -> tuple[int, int]:
    """Scrub mutable image projections after the NudeNet removal.

    The operation is deliberately explicit and idempotent.  Immutable revision,
    payload, receipt, and outbox tables are not rewritten; they remain
    historical evidence and can be handled only by a separately authorized
    migration.  Current image items are deactivated so a later image refresh
    republishes them from the NudeNet-free source adapter.  The return value is
    ``(changed_items, changed_projection_rows)``.
    """

    selected = Path(path)
    if not selected.is_file():
        return 0, 0
    if _inspect_existing_schema(selected) != SEMANTIC_SCHEMA_VERSION:
        raise SemanticStateError(
            f"semantic image scrub requires schema {SEMANTIC_SCHEMA_VERSION}"
        )
    with semantic_database(selected, readonly=True) as connection:
        if not _image_scrub_has_work(connection):
            return 0, 0

    changed_items = 0
    changed_projections = 0
    now = time.time_ns()
    with semantic_database(selected) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for label, query in _image_scrub_projection_queries():
            for row in connection.execute(query):
                identifier = str(row[0])
                if label == "semantic_items":
                    provenance, provenance_changed = _scrub_json_payload(
                        row[1], label=f"{label} provenance for {identifier}"
                    )
                    source_revision, revision_changed = _scrub_json_payload(
                        row[2], label=f"{label} source revision for {identifier}"
                    )
                    if not (provenance_changed or revision_changed):
                        continue
                    connection.execute(
                        """UPDATE semantic_items SET provenance_json=?,
                        source_revision_json=?,active=0,refresh_token=NULL,
                        updated_ns=? WHERE item_id=?""",
                        (provenance, source_revision, now, identifier),
                    )
                    changed_items += 1
                    continue
                provenance, changed = _scrub_json_payload(
                    row[1], label=f"{label} provenance for {identifier}"
                )
                if not changed:
                    continue
                if label == "image_embeddings":
                    connection.execute(
                        "UPDATE image_embeddings SET provenance_json=?,updated_ns=? "
                        "WHERE ref_id=?",
                        (provenance, now, int(row[0])),
                    )
                elif label == "semantic_evidence":
                    connection.execute(
                        "UPDATE semantic_evidence SET provenance_json=?,active=0,updated_ns=? "
                        "WHERE evidence_id=?",
                        (provenance, now, int(row[0])),
                    )
                elif label == "embedding_generation_members":
                    connection.execute(
                        "UPDATE embedding_generation_members SET provenance_json=?,updated_ns=? "
                        "WHERE member_id=?",
                        (provenance, now, int(row[0])),
                    )
                elif label == "embedding_generations":
                    connection.execute(
                        "UPDATE embedding_generations SET provenance_json=? WHERE generation_id=?",
                        (provenance, int(row[0])),
                    )
                else:  # pragma: no cover - query table list is module-owned
                    raise SemanticStateError(f"unknown image scrub projection: {label}")
                changed_projections += 1
        connection.execute(
            """INSERT INTO metadata(key,value) VALUES('retired_image_adult_scrub_ns',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (str(now),),
        )
    return changed_items, changed_projections


# endregion [04]
