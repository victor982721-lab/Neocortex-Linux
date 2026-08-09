from __future__ import annotations

import sqlite3
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from _02_Deduplicacion.hashing import FULL_ALGORITHM, full_fingerprint, snapshot_path
from _04_Nucleo_Operativo import semantic_sources
from _04_Nucleo_Operativo.semantic_models import (
    SemanticItem,
    TextSection,
    fingerprint_text,
)
from _04_Nucleo_Operativo.semantic_sources import (
    MAX_SEMANTIC_TITLE_CHARS,
    SEMANTIC_TITLE_POLICY,
    SEMANTIC_TITLE_SECTION_KIND,
    SemanticSourceError,
    iter_image_source_records,
    iter_text_sections_with_metadata,
    iter_text_source_records,
    semantic_item_title_section,
)
from _04_Nucleo_Operativo.text_state import initialize_text_state, text_database


# region [01] Minimal durable image and dedup states


_TEXT_FILE_KEYS = (
    "00000000000000000000000000000001:00000000000000000000000000000002",
    "00000000000000000000000000000003:00000000000000000000000000000004",
)


def _title_item(path: str | None) -> SemanticItem:
    return SemanticItem(
        item_id="item:pdf:title-fixture",
        source_kind="pdf",
        source_identity="title-fixture",
        identity_version="fixture-v1",
        fingerprint=fingerprint_text("title-fixture"),
        path=path,
    )


def test_semantic_title_uses_only_bounded_basename_and_appends_after_content() -> None:
    item = _title_item(r"C:\sensitive\parent\  Protección   49T.v2.pdf")
    title = semantic_item_title_section(item)

    assert title is not None
    assert title.section_kind == SEMANTIC_TITLE_SECTION_KIND
    assert title.section_id == SEMANTIC_TITLE_POLICY
    assert title.text == "Protección 49T.v2"
    assert "sensitive" not in title.text
    assert title.provenance == {
        "policy_signature": SEMANTIC_TITLE_POLICY,
        "basis": "basename_without_final_extension",
        "mutable_metadata": True,
        "advisory_only": True,
    }
    content = TextSection("pdf_page", "1", "contenido")
    assert tuple(iter_text_sections_with_metadata(item, (content,))) == (
        content,
        title,
    )


def test_semantic_title_replaces_recovery_name_with_bounded_content_heading() -> None:
    item = _title_item("C:/corpus/Archivo protegido recuperado 00820 - c8930027.pdf")
    content = TextSection(
        "pdf_page",
        "1",
        "BITÁCORA DIARIA DE TRABAJOS\nCentral Hidroeléctrica La Yesca",
    )

    sections = tuple(iter_text_sections_with_metadata(item, (content,)))

    assert sections[0] == content
    assert sections[1].text == "BITÁCORA DIARIA DE TRABAJOS"
    assert sections[1].provenance["basis"] == "bounded_leading_content_heading"
    assert sections[1].provenance["generic_basename_replaced"] is True


def test_semantic_title_prefers_durable_email_subject() -> None:
    item = SemanticItem(
        item_id="item:text:email",
        source_kind="text",
        source_identity="email",
        identity_version="fixture-v1",
        fingerprint=fingerprint_text("email"),
        path="C:/corpus/mensaje.eml",
        provenance={"source_title": "Prueba funcional del alimentador norte"},
    )

    title = semantic_item_title_section(item, "Contenido visible del mensaje")

    assert title is not None
    assert title.text == "Prueba funcional del alimentador norte"
    assert title.provenance["basis"] == "durable_source_title"
    assert title.provenance["source_title_preferred"] is True


@pytest.mark.parametrize(
    "path",
    (
        None,
        " ",
        "C:/fixture/invalid\nname.pdf",
        "C:/fixture/" + ("x" * (MAX_SEMANTIC_TITLE_CHARS + 1)) + ".pdf",
        "C:/fixture/",
    ),
)
def test_semantic_title_abstains_on_missing_or_invalid_basename(
    path: str | None,
) -> None:
    assert semantic_item_title_section(_title_item(path)) is None


def _create_image_state(state_directory: Path, image_path: Path) -> tuple[str, bytes]:
    snapshot = snapshot_path(image_path)
    file_key = f"{snapshot.volume_id}:{snapshot.file_id}"
    digest = full_fingerprint(snapshot)
    with sqlite3.connect(state_directory / "image.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE images(
                file_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                last_seen_run_id INTEGER NOT NULL,
                processing_signature TEXT,
                category TEXT,
                document_candidate INTEGER NOT NULL,
                adult_classification TEXT,
                status TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO images VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                file_key,
                snapshot.path,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                101,
                "image-route-fixture-v1",
                "industrial",
                0,
                "safe",
                "done",
            ),
        )
    return file_key, digest


def _create_dedup_state(
    state_directory: Path,
    image_path: Path,
    digest: bytes,
) -> None:
    snapshot = snapshot_path(image_path)
    with sqlite3.connect(state_directory / "dedup.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE files(
                volume_id BLOB NOT NULL,
                file_id BLOB NOT NULL,
                path TEXT NOT NULL,
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
            """
        )
        connection.execute(
            "INSERT INTO files VALUES(?,?,?,?,?,?)",
            (
                snapshot.volume_id.to_bytes(16, "little"),
                snapshot.file_id.to_bytes(16, "little"),
                snapshot.path,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
            ),
        )
        connection.execute(
            "INSERT INTO fingerprints VALUES(?,?,?,?,?,?,?)",
            (
                snapshot.volume_id.to_bytes(16, "little"),
                snapshot.file_id.to_bytes(16, "little"),
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                FULL_ALGORITHM,
                digest,
            ),
        )


def _create_image_state_v5(
    state_directory: Path,
    image_path: Path,
    *,
    ocr_text: str,
    ocr_digest: str | None = None,
) -> str:
    snapshot = snapshot_path(image_path)
    file_key = f"{snapshot.volume_id}:{snapshot.file_id}"
    payload = zlib.compress(ocr_text.encode("utf-8"))
    with sqlite3.connect(state_directory / "image.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE images(
                file_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                last_seen_run_id INTEGER NOT NULL,
                processing_signature TEXT,
                category TEXT,
                document_candidate INTEGER NOT NULL,
                adult_classification TEXT,
                status TEXT NOT NULL,
                ocr_text_zlib BLOB,
                ocr_text_chars INTEGER,
                ocr_text_xxh3_128 TEXT,
                ocr_text_truncated INTEGER NOT NULL,
                unused_large_payload BLOB
            );
            """
        )
        connection.execute(
            """INSERT INTO images(
                file_key,path,size,mtime_ns,birthtime_ns,last_seen_run_id,
                processing_signature,
                category,document_candidate,adult_classification,status,
                ocr_text_zlib,ocr_text_chars,ocr_text_xxh3_128,
                ocr_text_truncated,unused_large_payload)
            VALUES(?,?,?,?,?,?,?,?,?,?,'done',?,?,?,?,?)""",
            (
                file_key,
                snapshot.path,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                505,
                "image-route-fixture-v5",
                "documento",
                1,
                "safe",
                payload,
                len(ocr_text),
                ocr_digest or fingerprint_text(ocr_text).xxh3_128,
                1,
                b"unused" * 1024,
            ),
        )
    return file_key


def _create_multi_section_text_state(
    state_directory: Path,
    source_kind: str,
) -> None:
    sections = {
        _TEXT_FILE_KEYS[0]: ("sección uno", "sección dos"),
        _TEXT_FILE_KEYS[1]: ("section three", "section four"),
    }
    revision = (123, 456, 789, 42)
    if source_kind == "pdf":
        with sqlite3.connect(state_directory / "pdf.sqlite3") as connection:
            connection.executescript(
                """
                CREATE TABLE documents(
                    file_key TEXT PRIMARY KEY,path TEXT,processing_signature TEXT,
                    status TEXT,is_partial INTEGER,size INTEGER,mtime_ns INTEGER,
                    birthtime_ns INTEGER,
                    last_seen_run_id INTEGER,normalized_text_xxh3_128 TEXT,
                    normalized_text_chars INTEGER
                );
                CREATE TABLE pages(
                    file_key TEXT,page_number INTEGER,source TEXT,
                    text_zlib BLOB,text_chars INTEGER
                );
                """
            )
            for file_key, values in sections.items():
                combined = " ".join(values)
                connection.execute(
                    "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        file_key,
                        f"C:/{file_key}.pdf",
                        "pdf-fixture-v1",
                        "done",
                        0,
                        *revision,
                        fingerprint_text(combined).xxh3_128,
                        len(combined),
                    ),
                )
                connection.executemany(
                    "INSERT INTO pages VALUES(?,?,?,?,?)",
                    (
                        (
                            file_key,
                            ordinal,
                            "native",
                            zlib.compress(text.encode("utf-8")),
                            len(text),
                        )
                        for ordinal, text in enumerate(values, start=1)
                    ),
                )
        return
    if source_kind == "docx":
        with sqlite3.connect(state_directory / "docx.sqlite3") as connection:
            connection.executescript(
                """
                CREATE TABLE documents(
                    file_key TEXT PRIMARY KEY,path TEXT,processing_signature TEXT,
                    status TEXT,size INTEGER,mtime_ns INTEGER,birthtime_ns INTEGER,
                    last_seen_run_id INTEGER,text_xxh3_128 TEXT,text_chars INTEGER,
                    text_zlib BLOB
                );
                CREATE TABLE document_parts(
                    file_key TEXT,part_name TEXT,part_kind TEXT,ordinal INTEGER,
                    text_zlib BLOB,text_chars INTEGER
                );
                """
            )
            for file_key, values in sections.items():
                combined = " ".join(values)
                connection.execute(
                    "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        file_key,
                        f"C:/{file_key}.docx",
                        "docx-fixture-v1",
                        "complete",
                        *revision,
                        fingerprint_text(combined).xxh3_128,
                        len(combined),
                        zlib.compress(combined.encode("utf-8")),
                    ),
                )
                connection.executemany(
                    "INSERT INTO document_parts VALUES(?,?,?,?,?,?)",
                    (
                        (
                            file_key,
                            f"part-{ordinal}",
                            "body",
                            ordinal,
                            zlib.compress(text.encode("utf-8")),
                            len(text),
                        )
                        for ordinal, text in enumerate(values, start=1)
                    ),
                )
        return
    if source_kind == "audio":
        with sqlite3.connect(state_directory / "audio.sqlite3") as connection:
            connection.executescript(
                """
                CREATE TABLE documents(
                    file_key TEXT PRIMARY KEY,path TEXT,processing_signature TEXT,
                    status TEXT,size INTEGER,mtime_ns INTEGER,birthtime_ns INTEGER,
                    last_seen_run_id INTEGER,text_xxh3_128 TEXT,text_chars INTEGER
                );
                CREATE TABLE segments(
                    file_key TEXT,segment_index INTEGER,start_ms INTEGER,
                    end_ms INTEGER,text TEXT
                );
                """
            )
            for file_key, values in sections.items():
                combined = " ".join(values)
                connection.execute(
                    "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        file_key,
                        f"C:/{file_key}.wav",
                        "audio-fixture-v1",
                        "complete",
                        *revision,
                        fingerprint_text(combined).xxh3_128,
                        len(combined),
                    ),
                )
                connection.executemany(
                    "INSERT INTO segments VALUES(?,?,?,?,?)",
                    (
                        (file_key, ordinal, ordinal * 1000, ordinal * 1000 + 900, text)
                        for ordinal, text in enumerate(values)
                    ),
                )
        return
    raise AssertionError(f"unsupported fixture source: {source_kind}")


def _create_office_text_state(state_directory: Path) -> str:
    file_key = "00000000000000000000000000000005:00000000000000000000000000000006"
    text = "Coordinación de protecciones"
    with sqlite3.connect(state_directory / "office.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,format TEXT,path TEXT,size INTEGER,
                mtime_ns INTEGER,birthtime_ns INTEGER,processing_signature TEXT,
                status TEXT,last_seen_run_id INTEGER,text_xxh3_128 TEXT,
                text_chars INTEGER,text_zlib BLOB
            );
            """
        )
        connection.execute(
            "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                file_key,
                "xlsx",
                "C:/estudios/protecciones.xlsx",
                321,
                654,
                987,
                "office-fixture-v1",
                "complete",
                73,
                fingerprint_text(text).xxh3_128,
                len(text),
                zlib.compress(text.encode("utf-8")),
            ),
        )
    return file_key


def _create_code_text_state(state_directory: Path) -> str:
    volume_id = "0000000000000001"
    physical_file_id = "0000000000000002"
    text = "def relay_trip() -> bool:\n    return True\n"
    with sqlite3.connect(state_directory / "code.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE files(
                volume_id TEXT,physical_file_id TEXT,current_path TEXT,
                current_version_id INTEGER,status TEXT,last_seen_run_id INTEGER
            );
            CREATE TABLE file_versions(
                version_id INTEGER,size INTEGER,mtime_ns INTEGER,
                birthtime_ns INTEGER,raw_xxh3_128 TEXT,
                first_observed_run_id INTEGER,last_observed_run_id INTEGER,
                text_xxh3_128 TEXT,text_chars INTEGER,processing_signature TEXT,
                analysis_status TEXT,language TEXT,artifact_kind TEXT,
                analyzer_id TEXT,analyzer_version TEXT,parser_kind TEXT,
                invalidated_ns INTEGER
            );
            CREATE TABLE code_chunks(
                version_id INTEGER,chunk_index INTEGER,kind TEXT,
                start_line INTEGER,end_line INTEGER,text TEXT,symbol_id INTEGER
            );
            CREATE TABLE symbols(symbol_id INTEGER,qualified_name TEXT);
            """
        )
        connection.execute(
            "INSERT INTO files VALUES(?,?,?,?,?,?)",
            (volume_id, physical_file_id, "C:/src/relay.py", 7, "current", 44),
        )
        connection.execute(
            "INSERT INTO file_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                7,
                222,
                333,
                444,
                "a" * 32,
                40,
                43,
                fingerprint_text(text).xxh3_128,
                len(text),
                "code-fixture-v1",
                "complete",
                "python",
                "source",
                "tree-sitter",
                "0.25",
                "tree-sitter",
                None,
            ),
        )
        connection.execute(
            "INSERT INTO symbols VALUES(?,?)",
            (9, "relay.relay_trip"),
        )
        connection.execute(
            "INSERT INTO code_chunks VALUES(?,?,?,?,?,?,?)",
            (7, 0, "symbol", 1, 2, text, 9),
        )
    return f"{volume_id}:{physical_file_id}"


def _create_archive_text_state(state_directory: Path) -> str:
    file_key = "archive:member-fixture"
    text = "protección diferencial dentro de un ZIP anidado"
    with sqlite3.connect(state_directory / "archive.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE containers(
                container_key TEXT PRIMARY KEY,path TEXT,status TEXT
            );
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,container_key TEXT,path TEXT,
                container_path TEXT,member_chain TEXT,member_path TEXT,
                archive_depth INTEGER,content_kind TEXT,media_type TEXT,
                size INTEGER,mtime_ns INTEGER,birthtime_ns INTEGER,
                processing_signature TEXT,status TEXT,text_xxh3_128 TEXT,
                text_chars INTEGER,text_zlib BLOB,last_seen_run_id INTEGER
            );
            """
        )
        connection.execute(
            "INSERT INTO containers VALUES(?,?,?)",
            ("container-fixture", "C:/corpus/outer.zip", "complete"),
        )
        connection.execute(
            "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                file_key,
                "container-fixture",
                "C:/corpus/outer.zip!/inner.zip!/relay.txt",
                "C:/corpus/outer.zip",
                "inner.zip!/relay.txt",
                "relay.txt",
                2,
                "txt",
                "text/plain",
                48,
                60,
                -1,
                "archive-route-fixture-v1",
                "indexed",
                fingerprint_text(text).xxh3_128,
                len(text),
                zlib.compress(text.encode("utf-8")),
                9,
            ),
        )
    return file_key


def test_archive_text_adapter_preserves_nested_virtual_provenance(tmp_path: Path) -> None:
    file_key = _create_archive_text_state(tmp_path)

    records = tuple(iter_text_source_records(tmp_path, "archive"))

    assert len(records) == 1
    record = records[0]
    assert record.item.item_id == f"item:archive:{file_key}"
    assert record.item.path == "C:/corpus/outer.zip!/inner.zip!/relay.txt"
    assert record.section.section_kind == "archive_member"
    assert record.section.provenance["inside_zip"] is True
    assert record.section.provenance["archive_depth"] == 2
    assert record.section.provenance["member_chain"] == "inner.zip!/relay.txt"


def test_generic_text_adapter_preserves_physical_provenance_and_email_title(
    tmp_path: Path,
) -> None:
    state = tmp_path / "text.sqlite3"
    initialize_text_state(state)
    text = "resultado satisfactorio de la protección del alimentador"
    file_key = "00000000000000000000000000000015:00000000000000000000000000000022"
    with text_database(state, create=False) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            content_kind,media_type,title,author,metadata_json,text_zlib,text_chars,
            text_xxh3_128,text_truncated,detail,last_seen_run_id,updated_ns)
            VALUES(?,?,?,?,?,?,'complete',?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                file_key,
                "C:/corpus/mensaje.eml",
                500,
                700,
                900,
                "text-route-v1",
                "email",
                "message/rfc822",
                "Prueba funcional del alimentador norte",
                "Operacion <operacion@example.test>",
                '{"date":"2026-08-09"}',
                zlib.compress(text.encode("utf-8")),
                len(text),
                fingerprint_text(text).xxh3_128,
                0,
                "stdlib_email_visible_text",
                12,
                1_000,
            ),
        )
        connection.commit()

    records = tuple(iter_text_source_records(tmp_path, "text"))

    assert len(records) == 1
    record = records[0]
    assert record.item.item_id == f"item:text:{file_key}"
    assert record.item.source_revision == {
        "size": 500,
        "mtime_ns": 700,
        "birthtime_ns": 900,
        "processing_signature": "text-route-v1",
        "last_seen_run_id": 12,
    }
    assert record.item.provenance["source_title"] == ("Prueba funcional del alimentador norte")
    assert record.item.provenance["source_author"] == ("Operacion <operacion@example.test>")
    assert record.section.text == text
    assert record.section.provenance["inside_zip"] is False


# endregion [01]


# region [02] Stable image identity across fingerprint acquisition paths


def test_image_fingerprint_is_stable_when_dedup_cache_appears(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "subestación eléctrica.jpg"
    image_path.write_bytes(b"not-decoded-by-source-adapter\0fixture-image")
    file_key, digest = _create_image_state(tmp_path, image_path)

    streamed = tuple(iter_image_source_records(tmp_path))
    _create_dedup_state(tmp_path, image_path, digest)
    cached = tuple(iter_image_source_records(tmp_path))

    assert len(streamed) == len(cached) == 1
    assert streamed[0].item.item_id == cached[0].item.item_id == f"item:image:{file_key}"
    assert streamed[0].item.fingerprint == cached[0].item.fingerprint
    assert streamed[0].item.source_revision["raw_content_xxh3_128"] == digest.hex()
    assert cached[0].item.source_revision["raw_content_xxh3_128"] == digest.hex()
    assert streamed[0].item.source_revision["processing_signature"] == ("image-route-fixture-v1")
    assert streamed[0].item.source_revision["last_seen_run_id"] == 101
    assert cached[0].item.source_revision["last_seen_run_id"] == 101
    assert streamed[0].item.provenance["fingerprint_acquisition"] == "streamed-source"
    assert cached[0].item.provenance["fingerprint_acquisition"] == "dedup-cache"
    assert streamed[0].ocr_section is None


# endregion [02]


# region [03] Schema compatibility and fail-closed source evidence


def test_decode_text_accepts_valid_stream_at_exact_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "áé"
    encoded = text.encode("utf-8")
    monkeypatch.setattr(semantic_sources, "MAX_SECTION_TEXT_BYTES", len(encoded))
    monkeypatch.setattr(semantic_sources, "MAX_SECTION_TEXT_CHARS", len(text))

    assert semantic_sources._decode_text(zlib.compress(encoded), len(text)) == text


@pytest.mark.parametrize("missing_trailer_bytes", (1, 4))
def test_decode_text_rejects_truncated_trailer_after_full_text(
    missing_trailer_bytes: int,
) -> None:
    text = "contenido completo"
    encoded = text.encode("utf-8")
    payload = zlib.compress(encoded)
    truncated = payload[:-missing_trailer_bytes]
    probe = zlib.decompressobj()

    assert probe.decompress(truncated) == encoded
    assert not probe.eof
    with pytest.raises(SemanticSourceError, match="incomplete or truncated"):
        semantic_sources._decode_text(truncated, len(text))


def test_decode_text_rejects_valid_stream_with_garbage_suffix() -> None:
    text = "interruptor"
    payload = zlib.compress(text.encode("utf-8")) + b"trailing-garbage"

    with pytest.raises(SemanticSourceError, match="trailing or concatenated"):
        semantic_sources._decode_text(payload, len(text))


def test_decode_text_rejects_concatenated_streams() -> None:
    first = "subestación"
    payload = zlib.compress(first.encode("utf-8")) + zlib.compress("transformador".encode("utf-8"))

    with pytest.raises(SemanticSourceError, match="trailing or concatenated"):
        semantic_sources._decode_text(payload, len(first))


def test_image_schema_v5_reads_verified_ocr_without_unused_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "placa de transformador.jpg"
    image_path.write_bytes(b"fixture-image-v5")
    text = "Transformador de potencia 115 kV"
    file_key = _create_image_state_v5(tmp_path, image_path, ocr_text=text)
    original_readonly_database = semantic_sources._readonly_database

    @contextmanager
    def guarded_database(path: Path) -> Iterator[sqlite3.Connection]:
        with original_readonly_database(path) as connection:

            def authorize(
                action: int,
                table: str | None,
                column: str | None,
                _database: str | None,
                _trigger: str | None,
            ) -> int:
                if (
                    action == sqlite3.SQLITE_READ
                    and table == "images"
                    and column == "unused_large_payload"
                ):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            yield connection

    monkeypatch.setattr(semantic_sources, "_readonly_database", guarded_database)
    records = tuple(iter_image_source_records(tmp_path))

    assert len(records) == 1
    assert records[0].item.item_id == f"item:image:{file_key}"
    assert records[0].item.source_revision["processing_signature"] == ("image-route-fixture-v5")
    assert records[0].item.source_revision["last_seen_run_id"] == 505
    assert records[0].ocr_section is not None
    assert records[0].ocr_section.text == text
    assert records[0].ocr_section.provenance["truncated"] is True


def test_image_source_rejects_snapshot_mutation(tmp_path: Path) -> None:
    image_path = tmp_path / "interruptor.jpg"
    image_path.write_bytes(b"initial-image")
    _create_image_state(tmp_path, image_path)
    image_path.write_bytes(b"changed-image-with-different-size")

    with pytest.raises(SemanticSourceError, match="changed before semantic refresh"):
        tuple(iter_image_source_records(tmp_path))


def test_image_source_rejects_unavailable_file(tmp_path: Path) -> None:
    image_path = tmp_path / "seccionador.jpg"
    image_path.write_bytes(b"temporary-image")
    _create_image_state(tmp_path, image_path)
    image_path.unlink()

    with pytest.raises(SemanticSourceError, match="source is unavailable"):
        tuple(iter_image_source_records(tmp_path))


def test_image_source_rejects_mismatched_ocr_fingerprint(tmp_path: Path) -> None:
    image_path = tmp_path / "placa.jpg"
    image_path.write_bytes(b"fixture-image-with-ocr")
    _create_image_state_v5(
        tmp_path,
        image_path,
        ocr_text="Subestación Norte",
        ocr_digest="0" * 32,
    )

    with pytest.raises(SemanticSourceError, match="OCR fingerprint mismatch"):
        tuple(iter_image_source_records(tmp_path))


# endregion [03]


# region [04] Bounded current-item reuse for multi-section text


@pytest.mark.parametrize("source_kind", ("pdf", "docx", "audio"))
def test_multisection_sources_reuse_only_each_ordered_current_item(
    tmp_path: Path,
    source_kind: str,
) -> None:
    _create_multi_section_text_state(tmp_path, source_kind)

    records = tuple(iter_text_source_records(tmp_path, source_kind))

    assert len(records) == 4
    assert records[0].item is records[1].item
    assert records[2].item is records[3].item
    assert records[0].item is not records[2].item
    assert records[0].item.source_identity == _TEXT_FILE_KEYS[0]
    assert records[2].item.source_identity == _TEXT_FILE_KEYS[1]
    expected_revision: dict[str, object] = {
        "size": 123,
        "mtime_ns": 456,
        "birthtime_ns": 789,
        "processing_signature": f"{source_kind}-fixture-v1",
        "last_seen_run_id": 42,
    }
    if source_kind == "pdf":
        expected_revision["is_partial"] = False
    assert records[0].item.source_revision == expected_revision


def test_pdf_bounded_revision_preserves_partial_flag_with_done_status(
    tmp_path: Path,
) -> None:
    _create_multi_section_text_state(tmp_path, "pdf")
    with sqlite3.connect(tmp_path / "pdf.sqlite3") as connection:
        connection.execute("UPDATE documents SET is_partial=1")

    records = tuple(iter_text_source_records(tmp_path, "pdf"))

    assert len(records) == 4
    assert all(record.item.provenance["source_status"] == "done" for record in records)
    assert all(record.item.source_revision["is_partial"] is True for record in records)


@pytest.mark.parametrize("source_kind", ("pdf", "docx"))
def test_partial_text_sources_preserve_owner_status(
    tmp_path: Path,
    source_kind: str,
) -> None:
    _create_multi_section_text_state(tmp_path, source_kind)
    with sqlite3.connect(tmp_path / f"{source_kind}.sqlite3") as connection:
        connection.execute("UPDATE documents SET status='partial'")

    records = tuple(iter_text_source_records(tmp_path, source_kind))

    assert len(records) == 4
    assert all(record.item.provenance["source_status"] == "partial" for record in records)


# endregion [04]


# region [05] Physical revisions for Office and code sources


def test_office_source_preserves_physical_revision_and_route_run(
    tmp_path: Path,
) -> None:
    file_key = _create_office_text_state(tmp_path)

    records = tuple(iter_text_source_records(tmp_path, "xlsx"))

    assert len(records) == 1
    assert records[0].item.source_identity == file_key
    assert records[0].item.source_revision == {
        "size": 321,
        "mtime_ns": 654,
        "birthtime_ns": 987,
        "processing_signature": "office-fixture-v1",
        "last_seen_run_id": 73,
    }


def test_code_source_preserves_physical_revision_version_and_runs(
    tmp_path: Path,
) -> None:
    source_identity = _create_code_text_state(tmp_path)

    records = tuple(iter_text_source_records(tmp_path, "code"))

    assert len(records) == 1
    assert records[0].item.source_identity == source_identity
    assert records[0].item.source_revision == {
        "version_id": 7,
        "size": 222,
        "mtime_ns": 333,
        "birthtime_ns": 444,
        "processing_signature": "code-fixture-v1",
        "last_seen_run_id": 44,
        "first_observed_run_id": 40,
        "last_observed_run_id": 43,
        "raw_content_xxh3_128": "a" * 32,
    }


# endregion [05]
