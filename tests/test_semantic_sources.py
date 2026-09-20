from __future__ import annotations

import json
import sqlite3
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

import neocortex.capabilities.formats.text.text_state as text_state_module
from neocortex.deduplication.fingerprinting import FULL_ALGORITHM, full_fingerprint, snapshot_path
from neocortex.semantic import semantic_sources
from neocortex.semantic.derivation_contracts import MaterializationRef
from neocortex.foundation.file_identity import file_key_from_snapshot
from neocortex.semantic.semantic_models import (
    SemanticItem,
    TextSection,
    fingerprint_text,
)
from neocortex.semantic.semantic_sources import (
    MAX_SEMANTIC_TITLE_CHARS,
    SEMANTIC_TITLE_POLICY,
    SEMANTIC_TITLE_SECTION_KIND,
    SemanticSourceError,
    iter_image_source_records,
    iter_text_sections_with_metadata,
    iter_text_source_records,
    semantic_item_title_section,
    semantic_source_heads,
)
from neocortex.capabilities.formats.text.text_route import TextRoute, TextRouteConfig
from neocortex.capabilities.formats.text.text_state import initialize_text_state, text_database


TEST_CAPABILITIES = ("base", 'inference')
pytestmark = pytest.mark.capability("base", 'inference')


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


def test_image_source_head_uses_dedup_content_and_ignores_observation_clock(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "head.png"
    image_path.write_bytes(b"stable-image-content")
    _create_image_state_v5(tmp_path, image_path, ocr_text="texto retenido")
    _create_dedup_state(tmp_path, image_path, full_fingerprint(snapshot_path(image_path)))

    first = semantic_source_heads(tmp_path, ("image",))[0]
    with sqlite3.connect(tmp_path / "image.sqlite3") as connection:
        connection.execute("UPDATE images SET last_seen_run_id=606")
    observation_only = semantic_source_heads(tmp_path, ("image",))[0]
    with sqlite3.connect(tmp_path / "image.sqlite3") as connection:
        connection.execute("UPDATE images SET category='captura'")
    changed = semantic_source_heads(tmp_path, ("image",))[0]

    assert first.complete
    assert first.row_count == 1
    assert observation_only.digest == first.digest
    assert changed.digest != first.digest


def test_image_source_head_abstains_without_a_dedup_full_fingerprint(tmp_path: Path) -> None:
    image_path = tmp_path / "head-without-dedup.png"
    image_path.write_bytes(b"image-without-dedup")
    _create_image_state_v5(tmp_path, image_path, ocr_text="texto")

    head = semantic_source_heads(tmp_path, ("image",))[0]

    assert head.complete is False
    assert head.reason == "dedup_full_fingerprint_missing"


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


def test_generic_text_adapter_preserves_physical_provenance_and_email_title(
    tmp_path: Path,
) -> None:
    state = tmp_path / "text.sqlite3"
    text = "resultado satisfactorio de la protección del alimentador"
    source = tmp_path / "mensaje.eml"
    source.write_text(
        "From: Operacion <operacion@example.test>\n"
        "To: Pruebas <pruebas@example.test>\n"
        "Subject: Prueba funcional del alimentador norte\n"
        "Date: Sun, 9 Aug 2026 12:00:00 -0600\n"
        "Content-Type: text/plain; charset=utf-8\n\n"
        f"{text}\n",
        encoding="utf-8",
    )
    snapshot = snapshot_path(source)

    class _TextFrameworkState:
        def selected_route_candidate_counts(
            self,
            _run_id: int,
            mime: str,
            max_file_bytes: int | None,
            _route_name: str,
            _selection: object,
        ) -> tuple[int, int]:
            if mime != "message/rfc822":
                return (0, 0)
            return (1, int(max_file_bytes is None or snapshot.size <= max_file_bytes))

        def iter_selected_route_candidates(
            self,
            _run_id: int,
            mime: str,
            _route_name: str,
            _selection: object,
        ):
            if mime == "message/rfc822":
                yield snapshot

    summary = TextRoute(
        TextRouteConfig(state_path=state),
        _TextFrameworkState(),
        12,
    ).run()
    assert summary.extracted == 1
    file_key = file_key_from_snapshot(snapshot)
    with text_database(state, readonly=True) as connection:
        owner_facts = connection.execute(
            """SELECT d.size,d.mtime_ns,d.birthtime_ns,d.processing_signature,
            d.last_seen_run_id,d.revision_id,r.resource_id,r.producer,r.generation,
            r.revision_state,r.observed_at_utc,r.fingerprint_algorithm,
            r.fingerprint,m.materialization_json,m.fingerprint_algorithm
              AS representation_algorithm,m.fingerprint AS representation_fingerprint
            FROM documents d JOIN text_input_revisions r
              ON r.revision_id=d.revision_id
            JOIN text_materialization_heads h
              ON h.resource_id=r.resource_id
             AND h.materialization_kind='text_representation'
            JOIN text_materializations m
              ON m.owner=h.materialization_owner
             AND m.materialization_id=h.materialization_id
            WHERE d.file_key=?""",
            (file_key,),
        ).fetchone()
    assert owner_facts is not None
    representation = MaterializationRef.from_dict(
        json.loads(str(owner_facts["materialization_json"]))
    )
    assert representation.revision is not None

    records = tuple(iter_text_source_records(tmp_path, "text"))

    assert len(records) == 1
    record = records[0]
    assert record.item.item_id == f"item:text:{file_key}"
    assert record.item.source_revision == {
        "size": int(owner_facts["size"]),
        "mtime_ns": int(owner_facts["mtime_ns"]),
        "birthtime_ns": int(owner_facts["birthtime_ns"]),
        "processing_signature": str(owner_facts["processing_signature"]),
        "last_seen_run_id": int(owner_facts["last_seen_run_id"]),
        "revision_id": str(owner_facts["revision_id"]),
        "owner_revision": {
            "owner": "text",
            "revision": representation.revision.to_dict(),
            "fingerprint_algorithm": str(owner_facts["fingerprint_algorithm"]),
            "fingerprint": str(owner_facts["fingerprint"]),
        },
        "consumed_materialization": {
            "materialization": representation.to_dict(),
            "fingerprint_algorithm": str(owner_facts["representation_algorithm"]),
            "fingerprint": str(owner_facts["representation_fingerprint"]),
        },
    }
    assert record.item.provenance["source_title"] == ("Prueba funcional del alimentador norte")
    assert record.item.provenance["source_author"] == ("Operacion <operacion@example.test>")
    assert record.section.text == f"{text}\n"


@pytest.mark.parametrize(
    ("fault", "message"),
    (
        ("corrupt_receipt", "owner-local validation"),
        ("missing_receipt", "owner-local validation"),
        ("downgraded_revision", "downgraded to legacy"),
    ),
)
def test_generic_text_adapter_rejects_unvalidated_owner_publication(
    tmp_path: Path,
    fault: str,
    message: str,
) -> None:
    state = tmp_path / "text.sqlite3"
    source = tmp_path / "owner-publication.txt"
    source.write_text("publicación Text con causalidad durable", encoding="utf-8")
    snapshot = snapshot_path(source)

    class _TextFrameworkState:
        def selected_route_candidate_counts(
            self,
            _run_id: int,
            mime: str,
            max_file_bytes: int | None,
            _route_name: str,
            _selection: object,
        ) -> tuple[int, int]:
            if mime != "text/plain":
                return (0, 0)
            return (1, int(max_file_bytes is None or snapshot.size <= max_file_bytes))

        def iter_selected_route_candidates(
            self,
            _run_id: int,
            mime: str,
            _route_name: str,
            _selection: object,
        ):
            if mime == "text/plain":
                yield snapshot

    summary = TextRoute(
        TextRouteConfig(state_path=state),
        _TextFrameworkState(),
        1,
    ).run()
    assert summary.extracted == 1
    with sqlite3.connect(state) as connection:
        if fault == "corrupt_receipt":
            connection.execute("DROP TRIGGER text_work_receipts_no_update")
            connection.execute("UPDATE text_work_receipts SET receipt_json='{}'")
        elif fault == "missing_receipt":
            connection.execute("DROP TRIGGER text_work_receipts_no_delete")
            connection.execute("DELETE FROM text_work_receipts")
        else:
            connection.execute("DROP TRIGGER text_documents_revision_no_downgrade")
            connection.execute("UPDATE documents SET revision_id=NULL")

    with pytest.raises(SemanticSourceError, match=message):
        tuple(iter_text_source_records(tmp_path, "text"))


def test_generic_text_adapter_keeps_migrated_v1_explicitly_unattributed(
    tmp_path: Path,
) -> None:
    state = tmp_path / "text.sqlite3"
    text = "evidencia legacy sin receipt inventado"
    encoded = text.encode("utf-8")
    with sqlite3.connect(state) as connection:
        text_state_module._create_text_v1_schema(connection)
        connection.execute("INSERT INTO metadata VALUES('schema_version','1')")
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            content_kind,media_type,text_zlib,text_chars,text_xxh3_128,
            last_seen_run_id,updated_ns)
            VALUES('legacy','/legacy.txt',?,?,?,'legacy-psig','complete',
            'txt','text/plain',?,?,?,1,1)""",
            (
                len(encoded),
                10,
                -1,
                zlib.compress(encoded),
                len(text),
                fingerprint_text(text).xxh3_128,
            ),
        )
        connection.execute(
            """INSERT INTO document_fts(file_key,path,content_kind,title,author,body)
            VALUES('legacy','/legacy.txt','txt','','',?)""",
            (text,),
        )
    initialize_text_state(state)

    records = tuple(iter_text_source_records(tmp_path, "text"))

    assert len(records) == 1
    assert records[0].section.text == text
    assert "owner_revision" not in records[0].item.source_revision
    assert "consumed_materialization" not in records[0].item.source_revision


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
    assert "adult_classification" not in streamed[0].item.provenance
    assert "adult_classification" not in cached[0].item.provenance
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


# region [05] Physical revisions for Office


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




# endregion [05]
