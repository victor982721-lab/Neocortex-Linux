"""Focused contracts for the multimodal catalog adapters.

The fixtures are intentionally small owner-shaped SQLite files.  They exercise
the catalog boundary without opening or modifying the user's durable state.
"""

from __future__ import annotations

import sqlite3
import zlib
from pathlib import Path

from neocortex.documents.document_catalog import (
    SourceDocument,
    document_catalog_database,
    initialize_document_catalog,
    update_document_catalog,
    update_document_catalog_source,
)


def _file_identity(path: Path) -> tuple[str, str]:
    metadata = path.stat()
    return str(metadata.st_dev), str(metadata.st_ino)


def _compressed(value: str) -> bytes:
    return zlib.compress(value.encode("utf-8"))


def _make_video_owner(path: Path, source: Path, *, status: str = "complete") -> None:
    volume_id, file_id = _file_identity(source)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY, path TEXT, size INTEGER,
                mtime_ns INTEGER, birthtime_ns INTEGER, status TEXT,
                processing_signature TEXT, title TEXT, duration_seconds REAL,
                format_name TEXT, video_streams INTEGER, audio_streams INTEGER,
                subtitle_streams INTEGER, chapters INTEGER, frame_count INTEGER,
                ocr_frame_count INTEGER, ocr_text_chars INTEGER,
                probe_json TEXT, audio_status TEXT
            );
            CREATE TABLE frame_fts(
                file_key TEXT, path TEXT, title TEXT, timestamp_ms INTEGER,
                body TEXT
            );
            """
        )
        key = f"{volume_id}:{file_id}"
        stat = source.stat()
        connection.execute(
            """INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                str(source),
                stat.st_size,
                stat.st_mtime_ns,
                -1,
                status,
                "video-fixture-v1",
                source.stem,
                8.0,
                "matroska",
                1,
                0,
                0,
                0,
                2,
                1,
                18,
                "{}",
                "none",
            ),
        )
        connection.execute(
            "INSERT INTO frame_fts VALUES(?,?,?,?,?)",
            (key, str(source), source.stem, 1000, "Transformador U2"),
        )


def _make_image_owner(path: Path, source: Path, *, truncated: bool = False) -> None:
    volume_id, file_id = _file_identity(source)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE images(
                file_key TEXT PRIMARY KEY, path TEXT, size INTEGER,
                mtime_ns INTEGER, birthtime_ns INTEGER, status TEXT,
                processing_signature TEXT, mime TEXT, category TEXT,
                confidence REAL, ocr_text_xxh3_128 TEXT,
                ocr_text_truncated INTEGER, decode_quality TEXT,
                decode_provenance TEXT, ocr_text_zlib BLOB
            )"""
        )
        stat = source.stat()
        connection.execute(
            "INSERT INTO images VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"{volume_id}:{file_id}",
                str(source),
                stat.st_size,
                stat.st_mtime_ns,
                -1,
                "done",
                "image-fixture-v1",
                "image/png",
                "technical",
                0.91,
                "ocr-hash",
                int(truncated),
                "good",
                "fixture",
                _compressed("placa de identificación del transformador"),
            ),
        )


def _make_archive_owner(path: Path, source: Path, *, container_status: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE containers(
                container_key TEXT PRIMARY KEY, path TEXT, status TEXT
            );
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY, container_key TEXT, path TEXT,
                container_path TEXT, member_chain TEXT, member_path TEXT,
                archive_depth INTEGER, content_kind TEXT, media_type TEXT,
                size INTEGER, mtime_ns INTEGER, birthtime_ns INTEGER,
                processing_signature TEXT, status TEXT, text_zlib BLOB,
                text_chars INTEGER, text_xxh3_128 TEXT
            );
            """
        )
        container_key = "container-fixture"
        connection.execute(
            "INSERT INTO containers VALUES(?,?,?)",
            (container_key, str(source), container_status),
        )
        connection.execute(
            """INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "archive:member-fixture",
                container_key,
                f"{source}!/report.txt",
                str(source),
                "report.txt",
                "report.txt",
                0,
                "text",
                "text/plain",
                18,
                source.stat().st_mtime_ns,
                -1,
                "archive-fixture-v1",
                "indexed",
                _compressed("Reporte técnico de aceite"),
                27,
                "archive-text-hash",
            ),
        )


def _make_code_owner(path: Path, source: Path, *, truncated: bool = False) -> None:
    volume_id, physical_file_id = _file_identity(source)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE files(
                file_id INTEGER PRIMARY KEY, volume_id TEXT,
                physical_file_id TEXT, current_path TEXT, current_version_id INTEGER,
                status TEXT
            );
            CREATE TABLE file_versions(
                version_id INTEGER PRIMARY KEY, file_id INTEGER, size INTEGER,
                mtime_ns INTEGER, birthtime_ns INTEGER, analysis_status TEXT,
                processing_signature TEXT, language TEXT, artifact_kind TEXT,
                text_xxh3_128 TEXT, text_truncated INTEGER, text_zlib BLOB,
                provenance_json TEXT
            );
            CREATE TABLE code_chunks(
                chunk_id INTEGER PRIMARY KEY, version_id INTEGER,
                chunk_index INTEGER, text TEXT
            );
            """
        )
        stat = source.stat()
        connection.execute(
            "INSERT INTO files VALUES(?,?,?,?,?,?)",
            (1, volume_id, physical_file_id, str(source), 1, "current"),
        )
        connection.execute(
            """INSERT INTO file_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                1,
                1,
                stat.st_size,
                stat.st_mtime_ns,
                -1,
                "complete",
                "code-fixture-v1",
                "python",
                "source",
                "code-text-hash",
                int(truncated),
                _compressed("def transformer_status(): return 'U2'"),
                "{}",
            ),
        )


def test_multimodal_catalog_adapters_preserve_owner_and_coverage(tmp_path: Path) -> None:
    catalog = tmp_path / "document_catalog.sqlite3"
    initialize_document_catalog(catalog)

    video_file = tmp_path / "clip.mkv"
    image_file = tmp_path / "plate.png"
    archive_file = tmp_path / "bundle.zip"
    code_file = tmp_path / "module.py"
    for path in (video_file, image_file, archive_file, code_file):
        path.write_bytes(b"fixture")

    video_db = tmp_path / "video.sqlite3"
    image_db = tmp_path / "image.sqlite3"
    archive_db = tmp_path / "archive.sqlite3"
    code_db = tmp_path / "code.sqlite3"
    _make_video_owner(video_db, video_file, status="partial")
    _make_image_owner(image_db, image_file, truncated=True)
    _make_archive_owner(archive_db, archive_file, container_status="partial")
    _make_code_owner(code_db, code_file, truncated=True)

    summaries = (
        update_document_catalog_source(
            catalog,
            video_db,
            "video",
            verify_source_paths=True,
        ),
        update_document_catalog_source(
            catalog,
            image_db,
            "image",
            verify_source_paths=True,
        ),
        update_document_catalog_source(
            catalog,
            archive_db,
            "archive",
            verify_source_paths=True,
        ),
        update_document_catalog_source(
            catalog,
            code_db,
            "code",
            verify_source_paths=True,
        ),
    )

    assert [summary.candidates for summary in summaries] == [1, 1, 1, 1]
    assert all(summary.classified == 1 for summary in summaries)
    with document_catalog_database(catalog, readonly=True) as connection:
        rows = connection.execute(
            """SELECT source_kind,file_key,path,source_status,catalog_status
            FROM documents WHERE active=1 ORDER BY source_kind"""
        ).fetchall()

    assert [str(row[0]) for row in rows] == ["archive", "code", "image", "video"]
    assert all(str(row[4]) == "review" for row in rows)
    assert str(next(row[2] for row in rows if row[0] == "archive")).endswith(
        "!/report.txt"
    )
    assert str(next(row[1] for row in rows if row[0] == "code")).startswith("code:")


def test_complete_multimodal_assets_can_be_classified(tmp_path: Path) -> None:
    catalog = tmp_path / "document_catalog.sqlite3"
    initialize_document_catalog(catalog)
    source = tmp_path / "complete.mp4"
    source.write_bytes(b"complete fixture")
    database = tmp_path / "video.sqlite3"
    _make_video_owner(database, source)

    summary = update_document_catalog_source(
        catalog,
        database,
        "video",
        verify_source_paths=True,
    )

    assert summary.candidates == summary.classified == 1
    with document_catalog_database(catalog, readonly=True) as connection:
        status = connection.execute(
            "SELECT source_status,catalog_status FROM documents WHERE source_kind='video'"
        ).fetchone()
    assert tuple(status) == ("complete", "classified")


def test_catalog_update_discovers_present_optional_owners(tmp_path: Path) -> None:
    source = tmp_path / "discovered.mp4"
    source.write_bytes(b"discovery fixture")
    _make_video_owner(tmp_path / "video.sqlite3", source)

    summaries = update_document_catalog(tmp_path)

    assert any(summary.source_kind == "video" for summary in summaries)


def test_source_document_defaults_remain_compatible() -> None:
    document = SourceDocument(
        source_kind="text",
        file_key="1:2",
        path="/tmp/file.txt",
        volume_id="1",
        file_id="2",
        size=0,
        mtime_ns=0,
        birthtime_ns=-1,
        source_status="complete",
        processing_signature="fixture",
        text_fingerprint=None,
        title="file",
        author="",
        metadata="",
    )
    assert document.coverage == "complete"
    assert not document.virtual
