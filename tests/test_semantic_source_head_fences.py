"""TOCTOU fences for compact Semantic source-head projections."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.capabilities.formats.image.state import initialize_image_state
from neocortex.semantic import semantic_sources
from neocortex.semantic.semantic_sources import semantic_source_heads


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _create_pdf_owner(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                processing_signature TEXT NOT NULL,
                status TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                last_seen_run_id INTEGER,
                is_partial INTEGER NOT NULL,
                normalized_text_xxh3_128 TEXT,
                normalized_text_chars INTEGER NOT NULL
            );
            CREATE TABLE pages(
                file_key TEXT NOT NULL,
                page_number INTEGER NOT NULL,
                source TEXT NOT NULL,
                text_zlib BLOB NOT NULL,
                text_chars INTEGER NOT NULL,
                PRIMARY KEY(file_key,page_number)
            );
            INSERT INTO documents VALUES(
                'file:1','/corpus/a.pdf','pdf-v1','done',20,10,5,1,0,'a',1
            );
            INSERT INTO pages VALUES('file:1',1,'text',X'61',1);
            """
        )


def _create_image_and_dedup_owners(root: Path) -> None:
    image_database = root / "image.sqlite3"
    initialize_image_state(image_database)
    with sqlite3.connect(image_database) as connection:
        connection.execute(
            """INSERT INTO images(
            file_key,path,mime,size,mtime_ns,birthtime_ns,last_seen_run_id,
            processing_signature,status,category,document_candidate,
            ocr_text_zlib,ocr_text_chars,ocr_text_xxh3_128,ocr_text_truncated,updated_ns
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "image-key",
                "/corpus/a.jpg",
                "image/jpeg",
                10,
                20,
                0,
                1,
                "image-v1",
                "done",
                "photo",
                0,
                None,
                0,
                None,
                0,
                1,
            ),
        )

    with sqlite3.connect(root / "dedup.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE files(
                path TEXT PRIMARY KEY,
                volume_id BLOB NOT NULL,
                file_id BLOB NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL
            );
            CREATE TABLE fingerprints(
                volume_id BLOB NOT NULL,
                file_id BLOB NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                algorithm TEXT NOT NULL,
                digest BLOB NOT NULL
            );
            INSERT INTO files VALUES('/corpus/a.jpg',X'01',X'02',10,20,0);
            INSERT INTO fingerprints VALUES(
                X'01',X'02',10,20,0,'xxh3_128_full_v1',X'00000000000000000000000000000001'
            );
            """
        )


def test_source_head_rejects_owner_drift_between_pre_fence_and_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "pdf.sqlite3"
    _create_pdf_owner(database)
    real_capture = semantic_sources.capture_sqlite_read_fence
    calls = 0

    def capture(path: Path):
        nonlocal calls
        calls += 1
        fence = real_capture(path)
        if calls == 1:
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE documents SET normalized_text_xxh3_128='changed'")
        return fence

    monkeypatch.setattr(semantic_sources, "capture_sqlite_read_fence", capture)

    head = semantic_source_heads(tmp_path, ("pdf",))[0]

    assert head.complete is False
    assert head.coverage == "blocked"
    assert head.reason == "SemanticSourceError"
    assert calls == 1


def test_image_head_fences_dedup_owner_after_attached_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_image_and_dedup_owners(tmp_path)
    real_capture = semantic_sources.capture_sqlite_read_fence
    dedup_calls = 0

    def capture(path: Path):
        nonlocal dedup_calls
        if path.name == "dedup.sqlite3":
            dedup_calls += 1
            if dedup_calls == 2:
                with sqlite3.connect(path) as connection:
                    connection.execute(
                        "UPDATE fingerprints SET digest=?",
                        (b"changed-digest-1",),
                    )
        return real_capture(path)

    monkeypatch.setattr(semantic_sources, "capture_sqlite_read_fence", capture)

    head = semantic_source_heads(tmp_path, ("image",))[0]

    assert head.complete is False
    assert head.coverage == "blocked"
    assert head.reason == "SemanticSourceError"
    assert dedup_calls == 2
