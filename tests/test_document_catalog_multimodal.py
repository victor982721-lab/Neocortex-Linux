"""Focused contracts for the multimodal catalog adapters.

The fixtures are intentionally small owner-shaped SQLite files.  They exercise
the catalog boundary without opening or modifying the user's durable state.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
import zlib

from neocortex.documents.document_catalog import (
    SourceDocument,
    document_catalog_database,
    initialize_document_catalog,
    update_document_catalog,
    update_document_catalog_source,
)
from neocortex.runtime.orchestration.route_registry import (
    RouteExecutionContext,
    _update_document_catalog_after_route,
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


def _make_archive_owner(
    path: Path,
    source: Path,
    *,
    container_status: str,
    member_status: str = "indexed",
    text: str | None = "Reporte técnico de aceite",
) -> None:
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
                member_status,
                None if text is None else _compressed(text),
                0 if text is None else len(text),
                None if text is None else "archive-text-hash",
            ),
        )


def _make_pdf_owner(path: Path, source: Path, *, status: str) -> None:
    volume_id, file_id = _file_identity(source)
    stat = source.stat()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY, path TEXT, size INTEGER,
                mtime_ns INTEGER, birthtime_ns INTEGER, status TEXT,
                processing_signature TEXT, normalized_text_xxh3_128 TEXT,
                metadata_json TEXT, page_count INTEGER
            );
            CREATE TABLE pages(
                file_key TEXT, page_number INTEGER, source TEXT,
                text_zlib BLOB, text_chars INTEGER
            );
            """
        )
        key = f"{volume_id}:{file_id}"
        connection.execute(
            "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                str(source),
                stat.st_size,
                stat.st_mtime_ns,
                -1,
                status,
                "pdf-fixture-v1",
                None,
                '{"title":"Protected technical source"}',
                1,
            ),
        )


def _make_audio_owner(path: Path, sources: tuple[tuple[Path, str], ...]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE documents(
                file_key TEXT PRIMARY KEY, path TEXT, size INTEGER,
                mtime_ns INTEGER, birthtime_ns INTEGER, status TEXT,
                processing_signature TEXT, text_xxh3_128 TEXT, title TEXT,
                language TEXT, duration_seconds REAL, speech_duration_seconds REAL,
                model_name TEXT, backend_version TEXT, media_metadata_json TEXT,
                text_zlib BLOB
            )"""
        )
        for source, status in sources:
            volume_id, file_id = _file_identity(source)
            stat = source.stat()
            connection.execute(
                "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"{volume_id}:{file_id}",
                    str(source),
                    stat.st_size,
                    stat.st_mtime_ns,
                    -1,
                    status,
                    "audio-fixture-v1",
                    None,
                    source.stem,
                    "es",
                    3.0,
                    0.0,
                    "fixture",
                    "fixture",
                    "{}",
                    None,
                ),
            )


def _make_code_owner(
    path: Path,
    source: Path,
    *,
    truncated: bool = False,
    hex_identity: bool = False,
) -> None:
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
        if hex_identity:
            connection.execute(
                "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO metadata VALUES('schema_version','7')")
            stored_volume_id = f"{int(volume_id):x}"
            stored_physical_file_id = f"{int(physical_file_id):x}"
        else:
            stored_volume_id = volume_id
            stored_physical_file_id = physical_file_id
        connection.execute(
            "INSERT INTO files VALUES(?,?,?,?,?,?)",
            (
                1,
                stored_volume_id,
                stored_physical_file_id,
                str(source),
                1,
                "current",
            ),
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
    assert [summary.review_required for summary in summaries] == [1, 1, 1, 1]
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


def test_partial_and_protected_facts_remain_queryable_without_fabricated_text(
    tmp_path: Path,
) -> None:
    catalog = tmp_path / "document_catalog.sqlite3"
    initialize_document_catalog(catalog)

    protected_pdf = tmp_path / "protected.pdf"
    metadata_only_archive = tmp_path / "metadata-only.zip"
    no_speech_audio = tmp_path / "no-speech.mp3"
    no_audio_media = tmp_path / "no-audio.webm"
    for source in (
        protected_pdf,
        metadata_only_archive,
        no_speech_audio,
        no_audio_media,
    ):
        source.write_bytes(b"fixture")

    _make_pdf_owner(tmp_path / "pdf.sqlite3", protected_pdf, status="protected")
    _make_archive_owner(
        tmp_path / "archive.sqlite3",
        metadata_only_archive,
        container_status="complete",
        member_status="metadata_only",
        text=None,
    )
    _make_audio_owner(
        tmp_path / "audio.sqlite3",
        ((no_speech_audio, "no_speech"), (no_audio_media, "no_audio")),
    )

    summaries = (
        update_document_catalog_source(
            catalog,
            tmp_path / "pdf.sqlite3",
            "pdf",
            verify_source_paths=True,
        ),
        update_document_catalog_source(
            catalog,
            tmp_path / "archive.sqlite3",
            "archive",
            verify_source_paths=True,
        ),
        update_document_catalog_source(
            catalog,
            tmp_path / "audio.sqlite3",
            "audio",
            verify_source_paths=True,
        ),
    )

    assert [summary.candidates for summary in summaries] == [1, 1, 2]
    assert all(summary.classified == summary.candidates for summary in summaries)
    with document_catalog_database(catalog, readonly=True) as connection:
        rows = connection.execute(
            """SELECT source_kind,source_status,catalog_status,text_fingerprint
            FROM documents WHERE active=1 ORDER BY source_kind,source_status"""
        ).fetchall()

    assert [(str(row[0]), str(row[1])) for row in rows] == [
        ("archive", "metadata_only"),
        ("audio", "no_audio"),
        ("audio", "no_speech"),
        ("pdf", "protected"),
    ]
    assert all(str(row[2]) == "review" for row in rows)
    assert all(row[3] is None for row in rows)


def test_independent_multimodal_catalog_producers_overlap_and_replay(
    tmp_path: Path,
    monkeypatch,
) -> None:
    catalog = tmp_path / "document_catalog.sqlite3"
    initialize_document_catalog(catalog)
    source_by_kind = {
        "archive": tmp_path / "bundle.zip",
        "image": tmp_path / "plate.png",
        "video": tmp_path / "clip.mkv",
        "code": tmp_path / "module.py",
    }
    for source in source_by_kind.values():
        source.write_bytes(b"fixture")

    _make_archive_owner(
        tmp_path / "archive.sqlite3",
        source_by_kind["archive"],
        container_status="complete",
    )
    # Integrated producers now preserve the selected corpus scope. This
    # fixture therefore needs the physical container evidence that a current
    # Archive owner publishes; legacy unanchored rows stay covered elsewhere.
    from neocortex.foundation.file_identity import FileIdentity
    from neocortex.platform.policy import stat_birthtime_ns
    anchor = source_by_kind["archive"].stat()
    anchor_key = FileIdentity(anchor.st_dev, anchor.st_ino).packed_key
    with sqlite3.connect(tmp_path / "archive.sqlite3") as connection:
        for field in ("size", "mtime_ns", "birthtime_ns"):
            connection.execute(f"ALTER TABLE containers ADD COLUMN {field} INTEGER")
        connection.execute(
            "UPDATE containers SET container_key=?,size=?,mtime_ns=?,birthtime_ns=?",
            (anchor_key, anchor.st_size, anchor.st_mtime_ns, stat_birthtime_ns(anchor)),
        )
        connection.execute("UPDATE documents SET container_key=?", (anchor_key,))
    _make_image_owner(tmp_path / "image.sqlite3", source_by_kind["image"])
    _make_video_owner(tmp_path / "video.sqlite3", source_by_kind["video"])
    _make_code_owner(
        tmp_path / "code.sqlite3",
        source_by_kind["code"],
        hex_identity=True,
    )

    config = SimpleNamespace(
        document_catalog_enabled=True,
        document_catalog_database=catalog,
        document_taxonomy_path=None,
        document_classification_max_chars=1024,
        resume_run_id=None,
        archive_database=tmp_path / "archive.sqlite3",
        image_database=tmp_path / "image.sqlite3",
        video_database=tmp_path / "video.sqlite3",
        code_database=tmp_path / "code.sqlite3",
    )

    class LifecycleProbe:
        def begin_route_phase(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def complete_route_phase(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def fail_route_phase(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def record_event(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

    contexts = {
        kind: RouteExecutionContext(
            config=config,  # type: ignore[arg-type]
            root=tmp_path,
            framework_state=LifecycleProbe(),  # type: ignore[arg-type]
            run_id=index + 1,
            scan_id=100 + index,
            progress=None,
            resource_coordinator=None,
            cancellation=None,  # type: ignore[arg-type]
        )
        for index, kind in enumerate(source_by_kind)
    }

    import neocortex.documents.document_catalog as catalog_module

    original_update = catalog_module.update_document_catalog_source
    active = 0
    max_active = 0
    activity_lock = threading.Lock()
    started_together = threading.Barrier(4)

    def recording_update(*args: object, **kwargs: object):
        nonlocal active, max_active
        with activity_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            # Route orchestration permits source computation to overlap.
            # The catalog owns short SQL transactions and publication CAS.
            started_together.wait(timeout=5)
            return original_update(*args, **kwargs)
        finally:
            with activity_lock:
                active -= 1

    monkeypatch.setattr(catalog_module, "update_document_catalog_source", recording_update)

    def run_kind(kind: str):
        return _update_document_catalog_after_route(contexts[kind], kind)  # type: ignore[arg-type]

    with ThreadPoolExecutor(max_workers=4) as executor:
        first = tuple(executor.map(run_kind, source_by_kind))

    assert max_active == 4
    assert [summary.candidates for summaries in first for summary in summaries] == [1] * 4
    with document_catalog_database(catalog, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents WHERE active=1").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM catalog_publications").fetchone()[0] == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM catalog_generations WHERE status='published'"
        ).fetchone()[0] == 4

    active = 0
    max_active = 0
    with ThreadPoolExecutor(max_workers=4) as executor:
        replay = tuple(executor.map(run_kind, source_by_kind))

    assert max_active == 4
    assert [summary.cache_hits for summaries in replay for summary in summaries] == [1] * 4
    assert [summary.classified for summaries in replay for summary in summaries] == [0] * 4


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
