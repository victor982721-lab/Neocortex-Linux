"""Idempotent schema migrations for derived PDF indexes."""

from __future__ import annotations

from . import preserve_legacy_module as _preserve_legacy_module

import sqlite3


# region [01] Canonical derived DDL


_DERIVED_SCHEMA_DDL = (
    """CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(
        file_key UNINDEXED,
        path UNINDEXED,
        page_number UNINDEXED,
        text,
        tokenize='unicode61 remove_diacritics 2'
    )""",
    """CREATE TABLE IF NOT EXISTS page_fts_state(
        file_key TEXT NOT NULL,
        page_number INTEGER NOT NULL,
        text_xxh3_128 TEXT NOT NULL,
        PRIMARY KEY(file_key,page_number)
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS text_signatures(
        file_key TEXT PRIMARY KEY,
        algorithm_version INTEGER NOT NULL,
        simhash64 TEXT NOT NULL,
        token_count INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS similarity_buckets(
        run_id INTEGER NOT NULL,
        signature_kind TEXT NOT NULL,
        band INTEGER NOT NULL,
        bucket INTEGER NOT NULL,
        file_key TEXT NOT NULL,
        PRIMARY KEY(run_id,signature_kind,band,bucket,file_key)
    ) WITHOUT ROWID""",
    """CREATE INDEX IF NOT EXISTS similarity_bucket_lookup_idx
        ON similarity_buckets(run_id,signature_kind,band,bucket)""",
    """CREATE TABLE IF NOT EXISTS similarity_relations(
        run_id INTEGER NOT NULL,
        file_key_a TEXT NOT NULL,
        file_key_b TEXT NOT NULL,
        kind TEXT NOT NULL,
        score REAL NOT NULL,
        algorithm_version INTEGER NOT NULL,
        evidence_json TEXT NOT NULL,
        PRIMARY KEY(run_id,file_key_a,file_key_b,kind)
    ) WITHOUT ROWID""",
    """CREATE INDEX IF NOT EXISTS similarity_relations_score_idx
        ON similarity_relations(run_id,kind,score DESC)""",
    """CREATE TABLE IF NOT EXISTS similarity_state(
        signature_kind TEXT PRIMARY KEY,
        active_xxh3_128 TEXT NOT NULL,
        threshold REAL NOT NULL,
        algorithm_version INTEGER NOT NULL,
        relation_run_id INTEGER NOT NULL,
        relation_count INTEGER NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS page_layouts(
        file_key TEXT NOT NULL,
        page_number INTEGER NOT NULL,
        algorithm_version INTEGER NOT NULL,
        source_kind TEXT NOT NULL,
        geometry_simhash64 TEXT NOT NULL,
        visual_simhash64 TEXT NOT NULL,
        header_simhash64 TEXT NOT NULL,
        footer_simhash64 TEXT NOT NULL,
        layout_simhash64 TEXT NOT NULL,
        layout_zlib BLOB NOT NULL,
        updated_ns INTEGER NOT NULL,
        PRIMARY KEY(file_key,page_number),
        FOREIGN KEY(file_key) REFERENCES documents(file_key) ON DELETE CASCADE
    ) WITHOUT ROWID""",
    """CREATE INDEX IF NOT EXISTS page_layout_signature_idx
        ON page_layouts(algorithm_version,layout_simhash64)""",
    """CREATE TABLE IF NOT EXISTS document_layouts(
        file_key TEXT PRIMARY KEY,
        algorithm_version INTEGER NOT NULL,
        mapped_pages INTEGER NOT NULL,
        layout_simhash64 TEXT NOT NULL,
        geometry_simhash64 TEXT NOT NULL,
        visual_simhash64 TEXT NOT NULL,
        header_simhash64 TEXT NOT NULL,
        footer_simhash64 TEXT NOT NULL,
        page_sequence_xxh3_128 TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        updated_ns INTEGER NOT NULL,
        FOREIGN KEY(file_key) REFERENCES documents(file_key) ON DELETE CASCADE
    ) WITHOUT ROWID""",
    """CREATE INDEX IF NOT EXISTS document_layout_signature_idx
        ON document_layouts(algorithm_version,layout_simhash64)""",
    """CREATE TABLE IF NOT EXISTS layout_groups(
        relation_run_id INTEGER NOT NULL,
        group_key TEXT NOT NULL,
        representative_file_key TEXT NOT NULL,
        member_count INTEGER NOT NULL,
        minimum_edge_score REAL NOT NULL,
        algorithm_version INTEGER NOT NULL,
        evidence_json TEXT NOT NULL,
        PRIMARY KEY(relation_run_id,group_key)
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS layout_group_members(
        relation_run_id INTEGER NOT NULL,
        group_key TEXT NOT NULL,
        file_key TEXT NOT NULL,
        PRIMARY KEY(relation_run_id,group_key,file_key)
    ) WITHOUT ROWID""",
    """CREATE INDEX IF NOT EXISTS layout_group_member_lookup_idx
        ON layout_group_members(file_key,relation_run_id)""",
)


# endregion [01]


# region [02] Schema migration


def initialize_derived_schema(connection: sqlite3.Connection) -> None:
    page_columns = {row[1] for row in connection.execute("PRAGMA table_info(pages)")}
    if "profile_json" not in page_columns:
        connection.execute("ALTER TABLE pages ADD COLUMN profile_json TEXT")
    document_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(documents)")
    }
    additions = (
        ("profile_version", "INTEGER"),
        ("template_simhash64", "TEXT"),
    )
    for name, declaration in additions:
        if name not in document_columns:
            connection.execute(f"ALTER TABLE documents ADD COLUMN {name} {declaration}")
    for statement in _DERIVED_SCHEMA_DDL:
        connection.execute(statement)


# endregion [02]

_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.pdf_derived_schema")
