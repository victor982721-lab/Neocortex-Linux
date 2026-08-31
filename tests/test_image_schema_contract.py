# region [00] Contexto del módulo
# Módulo: tests/test_image_schema_contract.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.capabilities.formats.image.state import SCHEMA_VERSION, initialize_image_state
from neocortex.persistence.sqlite_backup import backup_sqlite_online
from neocortex.persistence.sqlite_schema_contract import SQLiteSchemaContractError
# endregion [01]

# region [02] Implementación


_LEGACY_IMAGE_DDL = """CREATE TABLE images(
    file_key TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    birthtime_ns INTEGER NOT NULL,
    last_seen_run_id INTEGER NOT NULL,
    processing_signature TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    category TEXT,
    confidence REAL,
    runner_up TEXT,
    runner_up_score REAL,
    features_json TEXT,
    attributes_json TEXT,
    evidence_json TEXT,
    error_type TEXT,
    error_message TEXT,
    updated_ns INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID"""

_LEGACY_ADDITIONS = {
    2: (
        ("semantic_json", "TEXT"),
        ("confidence_kind", "TEXT"),
        ("winner_score", "REAL"),
        ("score_margin", "REAL"),
        ("document_candidate", "INTEGER NOT NULL DEFAULT 0"),
        ("document_candidate_score", "REAL"),
        ("document_candidate_uncertainty", "TEXT"),
        ("error_phase", "TEXT"),
        ("error_retryable", "INTEGER"),
        ("error_disposition", "TEXT"),
        ("error_provenance", "TEXT"),
    ),
    3: (
        ("adult_candidate", "INTEGER NOT NULL DEFAULT 0"),
        ("adult_analyzed", "INTEGER NOT NULL DEFAULT 0"),
        ("adult_classification", "TEXT"),
        ("adult_confidence", "REAL"),
        ("adult_provenance", "TEXT"),
    ),
    4: (
        ("adult_evidence_json", "TEXT"),
        ("decode_quality", "TEXT"),
        ("decode_provenance", "TEXT"),
    ),
}

_CURRENT_IMAGE_COLUMNS = {
    "file_key",
    "path",
    "mime",
    "size",
    "mtime_ns",
    "birthtime_ns",
    "last_seen_run_id",
    "processing_signature",
    "status",
    "category",
    "confidence",
    "confidence_kind",
    "winner_score",
    "runner_up",
    "runner_up_score",
    "score_margin",
    "document_candidate",
    "document_candidate_score",
    "document_candidate_uncertainty",
    "ocr_text_zlib",
    "ocr_text_chars",
    "ocr_text_xxh3_128",
    "ocr_text_truncated",
    "decode_quality",
    "decode_provenance",
    "features_json",
    "attributes_json",
    "semantic_json",
    "evidence_json",
    "error_type",
    "error_message",
    "error_phase",
    "error_retryable",
    "error_disposition",
    "error_provenance",
    "updated_ns",
}

_CURRENT_IMAGE_INDEXES = {
    "images_category_idx",
    "images_document_review_idx",
    "images_error_review_idx",
    "images_ocr_text_idx",
    "images_run_path_idx",
}


def _create_legacy_image_state(path: Path, version: int) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE metadata(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID"""
        )
        connection.execute(_LEGACY_IMAGE_DDL)
        for target_version, additions in _LEGACY_ADDITIONS.items():
            if target_version > version:
                continue
            for column, declaration in additions:
                connection.execute(
                    f'ALTER TABLE images ADD COLUMN "{column}" {declaration}'
                )
        connection.execute(
            "CREATE INDEX images_run_path_idx ON images(last_seen_run_id,path)"
        )
        connection.execute(
            "CREATE INDEX images_category_idx "
            "ON images(category,confidence DESC) WHERE status='done'"
        )
        if version >= 2:
            connection.execute(
                "CREATE INDEX images_document_review_idx "
                "ON images(document_candidate,document_candidate_score DESC) "
                "WHERE status='done' AND document_candidate=1"
            )
            connection.execute(
                "CREATE INDEX images_error_review_idx "
                "ON images(error_disposition,error_retryable) WHERE status='error'"
            )
        if version >= 3:
            connection.execute(
                "CREATE INDEX images_adult_review_idx "
                "ON images(adult_classification,adult_confidence DESC) "
                "WHERE status='done' AND adult_candidate=1"
            )
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES(?,?)",
            (("schema_version", str(version)), ("legacy_marker", "preserve-me")),
        )
        connection.execute(
            """INSERT INTO images(
                file_key,path,mime,size,mtime_ns,birthtime_ns,last_seen_run_id,
                processing_signature,status,category,confidence,features_json,
                attributes_json,evidence_json,updated_ns
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "00000000000000000000000000000001:00000000000000000000000000000002",
                "C:/legacy/image.png",
                "image/png",
                123,
                456,
                789,
                11,
                "legacy-image-signature",
                "done",
                "industrial",
                0.75,
                "{}",
                "{}",
                "{}",
                999,
            ),
        )
        if version >= 3:
            connection.execute(
                """UPDATE images SET adult_candidate=1,adult_analyzed=1,
                adult_classification='explicit',adult_confidence=0.99,
                adult_provenance='legacy-nudenet'"""
            )
        if version >= 4:
            connection.execute(
                "UPDATE images SET adult_evidence_json='{""legacy"":true}'"
            )


def _schema_names(path: Path, object_type: str) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%'",
                (object_type,),
            )
        }


@pytest.mark.parametrize("legacy_version", range(5))
def test_migrations_preserve_product_state_and_remove_nudenet_columns(
    tmp_path: Path,
    legacy_version: int,
) -> None:
    database = tmp_path / f"image-v{legacy_version}.sqlite3"
    _create_legacy_image_state(database, legacy_version)

    initialize_image_state(database)

    with sqlite3.connect(database) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(images)")
        }
        row = connection.execute(
            """SELECT path,processing_signature,status,category,confidence,
                ocr_text_zlib,ocr_text_truncated
            FROM images"""
        ).fetchone()
    assert metadata == {
        "legacy_marker": "preserve-me",
        "schema_version": str(SCHEMA_VERSION),
    }
    assert columns == _CURRENT_IMAGE_COLUMNS
    assert _schema_names(database, "table") == {"images", "metadata"}
    assert _schema_names(database, "index") == _CURRENT_IMAGE_INDEXES
    assert row == (
        "C:/legacy/image.png",
        "legacy-image-signature",
        "done",
        "industrial",
        0.75,
        None,
        0,
    )


def test_current_schema_validation_is_read_only_and_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "image.sqlite3"
    initialize_image_state(database)
    before = database.read_bytes()

    initialize_image_state(database)

    assert database.read_bytes() == before


def test_nudenet_migration_preserves_a_verified_backup_before_removing_data(
    tmp_path: Path,
) -> None:
    database = tmp_path / "image.sqlite3"
    backup = tmp_path / "backups" / "image-before-v6.sqlite3"
    _create_legacy_image_state(database, 4)
    backup.parent.mkdir()

    result = backup_sqlite_online(database, backup)
    assert result.integrity.healthy
    initialize_image_state(database)

    with sqlite3.connect(backup) as connection:
        backup_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(images)")}
        legacy_classification = connection.execute(
            "SELECT adult_classification FROM images"
        ).fetchone()[0]
    with sqlite3.connect(database) as connection:
        current_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(images)")}

    assert "adult_classification" in backup_columns
    assert legacy_classification == "explicit"
    assert "adult_classification" not in current_columns


@pytest.mark.parametrize(
    "corrupt",
    (
        "DROP INDEX images_ocr_text_idx",
        "ALTER TABLE images DROP COLUMN decode_provenance",
        "DROP TABLE images",
    ),
)
def test_incomplete_current_schema_is_rejected_without_mutation(
    tmp_path: Path,
    corrupt: str,
) -> None:
    database = tmp_path / "image.sqlite3"
    initialize_image_state(database)
    with sqlite3.connect(database) as connection:
        connection.execute(corrupt)
    before = database.read_bytes()

    with pytest.raises(SQLiteSchemaContractError, match="schema contract is invalid"):
        initialize_image_state(database)

    assert database.read_bytes() == before


@pytest.mark.parametrize(
    ("declared_version", "exception", "message"),
    (
        (str(SCHEMA_VERSION + 1), RuntimeError, "newer than supported"),
        ("05", SQLiteSchemaContractError, "not canonical"),
        ("-1", SQLiteSchemaContractError, "not canonical"),
        ("5 ", SQLiteSchemaContractError, "not canonical"),
    ),
)
def test_invalid_versions_are_rejected_without_mutation(
    tmp_path: Path,
    declared_version: str,
    exception: type[Exception],
    message: str,
) -> None:
    database = tmp_path / "image.sqlite3"
    initialize_image_state(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (declared_version,),
        )
    before = database.read_bytes()

    with pytest.raises(exception, match=message):
        initialize_image_state(database)

    assert database.read_bytes() == before


def test_failed_legacy_upgrade_rolls_back_schema_and_metadata(tmp_path: Path) -> None:
    database = tmp_path / "malformed-image.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE images(file_key TEXT PRIMARY KEY) WITHOUT ROWID;
            INSERT INTO metadata VALUES('schema_version','0');
            INSERT INTO metadata VALUES('legacy_marker','preserve-me');
            INSERT INTO images VALUES('legacy-key');
            """
        )
    schema_before = _schema_names(database, "index")

    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        initialize_image_state(database)

    with sqlite3.connect(database) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        columns = tuple(
            str(row[1]) for row in connection.execute("PRAGMA table_info(images)")
        )
        rows = connection.execute("SELECT file_key FROM images").fetchall()
    assert metadata == {
        "legacy_marker": "preserve-me",
        "schema_version": "0",
    }
    assert columns == ("file_key",)
    assert rows == [("legacy-key",)]
    assert _schema_names(database, "index") == schema_before
# endregion [02]
