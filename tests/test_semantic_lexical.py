from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from _04_Nucleo_Operativo import semantic_lexical
from _04_Nucleo_Operativo.semantic_lexical import (
    MAX_QUERY_CHARS,
    MAX_QUERY_TERM_CHARS,
    MAX_QUERY_TERMS,
    LexicalAvailability,
    LexicalStatePaths,
    compile_natural_fts_query,
    search_lexical_source,
    search_lexical_sources,
)
from _04_Nucleo_Operativo.semantic_models import EmbeddingModality


# region [01] Minimal route-compatible FTS fixtures


def _create_pdf_state(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                status TEXT NOT NULL,
                is_partial INTEGER NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                processing_signature TEXT NOT NULL,
                last_seen_run_id INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE page_fts USING fts5(
                file_key UNINDEXED,
                path UNINDEXED,
                page_number UNINDEXED,
                text,
                tokenize='unicode61 remove_diacritics 2'
            );
            INSERT INTO documents VALUES(
                'pdf-key','C:/docs/proteccion.pdf','done',0,100,20,10,'pdf-v11',7
            );
            INSERT INTO page_fts VALUES(
                'pdf-key','C:/docs/proteccion.pdf',7,
                'Protección de interruptor y relevador de subestación'
            );
            """
        )


def _create_docx_state(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                status TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                processing_signature TEXT NOT NULL,
                last_seen_run_id INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE document_fts USING fts5(
                file_key UNINDEXED,
                path UNINDEXED,
                title,
                author,
                body,
                tokenize='unicode61 remove_diacritics 2'
            );
            INSERT INTO documents VALUES(
                'docx-key','C:/docs/proteccion.docx','complete',200,30,11,
                'docx-v5',8
            );
            INSERT INTO document_fts VALUES(
                'docx-key','C:/docs/proteccion.docx','Estudio','Victor',
                'Protección de interruptor OR relay DROP TABLE documents breaker'
            );
            """
        )


def _create_office_state(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                status TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                processing_signature TEXT NOT NULL,
                last_seen_run_id INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE document_fts USING fts5(
                file_key UNINDEXED,
                format UNINDEXED,
                path UNINDEXED,
                title,
                author,
                body,
                tokenize='unicode61 remove_diacritics 2'
            );
            INSERT INTO documents VALUES(
                'office-key','C:/docs/proteccion.xlsx','complete',300,40,12,
                'office-v1',9
            );
            INSERT INTO document_fts VALUES(
                'office-key','xlsx','C:/docs/proteccion.xlsx','Matriz','Victor',
                'Protección de interruptor de potencia'
            );
            """
        )


def _create_audio_state(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                status TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                processing_signature TEXT NOT NULL,
                last_seen_run_id INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE transcript_fts USING fts5(
                file_key UNINDEXED,
                path UNINDEXED,
                title,
                body,
                tokenize='unicode61 remove_diacritics 2'
            );
            INSERT INTO documents VALUES(
                'audio-key','C:/audio/maniobra.m4a','complete',400,50,13,
                'audio-v1',10
            );
            INSERT INTO transcript_fts VALUES(
                'audio-key','C:/audio/maniobra.m4a','Maniobra',
                'Protección de interruptor durante mantenimiento'
            );
            """
        )


def _create_archive_state(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                container_path TEXT NOT NULL,
                member_chain TEXT NOT NULL,
                member_path TEXT NOT NULL,
                archive_depth INTEGER NOT NULL,
                content_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                text_chars INTEGER NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                processing_signature TEXT NOT NULL,
                last_seen_run_id INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE document_fts USING fts5(
                file_key UNINDEXED,path UNINDEXED,container_path UNINDEXED,
                container_name,member_chain,content_kind,body,
                tokenize='unicode61 remove_diacritics 2'
            );
            INSERT INTO documents VALUES(
                'archive:key','C:/docs/contenedor.zip!/interno.zip!/proteccion.txt',
                'C:/docs/contenedor.zip','interno.zip!/proteccion.txt',
                'proteccion.txt',2,'text','indexed',45,45,60,-1,'archive-v1',11
            );
            INSERT INTO document_fts VALUES(
                'archive:key','C:/docs/contenedor.zip!/interno.zip!/proteccion.txt',
                'C:/docs/contenedor.zip','contenedor.zip',
                'interno.zip!/proteccion.txt','text',
                'Protección diferencial dentro de un ZIP anidado'
            );
            """
        )


def _create_text_state(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                status TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                processing_signature TEXT NOT NULL,
                last_seen_run_id INTEGER NOT NULL,
                revision_id TEXT
            );
            CREATE VIRTUAL TABLE document_fts USING fts5(
                file_key UNINDEXED,path UNINDEXED,content_kind,title,author,body,
                tokenize='unicode61 remove_diacritics 2'
            );
            INSERT INTO documents VALUES(
                '21:34','C:/docs/bitacora.eml','complete',500,70,14,
                'text-route-v1',12,'revision:text:owner-native-fixture'
            );
            INSERT INTO document_fts VALUES(
                '21:34','C:/docs/bitacora.eml','email','Alimentador norte','Victor',
                'Protección diferencial del alimentador dentro del correo'
            );
            """
        )


# endregion [01]


# region [02] Natural query safety


def test_compile_natural_query_quotes_fts_operators_and_punctuation() -> None:
    assert (
        compile_natural_fts_query('IEC-61850: "protección" OR (breaker*) IEC')
        == '"IEC" AND "61850" AND "protección" AND "OR" AND "breaker"'
    )


def test_punctuation_rich_query_cannot_inject_fts_or_sql(tmp_path: Path) -> None:
    state = tmp_path / "docx.sqlite3"
    _create_docx_state(state)

    result = search_lexical_source(
        "docx",
        state,
        'breaker: (OR) "DROP TABLE documents" --',
    )

    assert len(result.hits) == 1
    with sqlite3.connect(state) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone() == (1,)


def test_strict_all_term_match_remains_primary_and_observable(tmp_path: Path) -> None:
    state = tmp_path / "pdf.sqlite3"
    _create_pdf_state(state)

    result = search_lexical_source(
        "pdf",
        state,
        "protección interruptor relevador",
    )

    assert len(result.hits) == 1
    provenance = result.hits[0].hit.provenance
    assert provenance["query_strategy"] == "strict_all_terms"
    assert provenance["query_fallback_used"] is False
    assert provenance["applied_query"] == result.normalized_query


def test_stopword_elision_recovers_natural_query_without_weakening_primary(
    tmp_path: Path,
) -> None:
    state = tmp_path / "pdf.sqlite3"
    _create_pdf_state(state)

    result = search_lexical_source(
        "pdf",
        state,
        "la protección de interruptor",
    )

    assert result.normalized_query == '"la" AND "protección" AND "de" AND "interruptor"'
    assert len(result.hits) == 1
    provenance = result.hits[0].hit.provenance
    assert provenance["query_strategy"] == "content_terms_all"
    assert provenance["query_fallback_used"] is True
    assert provenance["applied_query"] == '"protección" AND "interruptor"'


def test_soft_fallback_requires_two_content_terms_and_remains_bounded(
    tmp_path: Path,
) -> None:
    state = tmp_path / "pdf.sqlite3"
    _create_pdf_state(state)

    recovered = search_lexical_source(
        "pdf",
        state,
        "protección interruptor transformador",
    )
    unrelated = search_lexical_source(
        "pdf",
        state,
        "protección transformador capacitor",
    )

    assert len(recovered.hits) == 1
    assert recovered.hits[0].hit.provenance["query_strategy"] == ("content_terms_any_two")
    assert unrelated.hits == ()


def test_stopword_only_query_is_not_broadened(tmp_path: Path) -> None:
    state = tmp_path / "pdf.sqlite3"
    _create_pdf_state(state)

    result = search_lexical_source("pdf", state, "de la y el")

    assert result.hits == ()


def test_question_scaffolding_cannot_outrank_the_requested_subject(
    tmp_path: Path,
) -> None:
    state = tmp_path / "pdf.sqlite3"
    with sqlite3.connect(state) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,path TEXT NOT NULL,status TEXT NOT NULL,
                is_partial INTEGER NOT NULL,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,processing_signature TEXT NOT NULL,
                last_seen_run_id INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE page_fts USING fts5(
                file_key UNINDEXED,path UNINDEXED,page_number UNINDEXED,text,
                tokenize='unicode61 remove_diacritics 2'
            );
            INSERT INTO documents VALUES
                ('relevant','C:/docs/transformador.pdf','done',0,100,20,10,'pdf-v11',7),
                ('noise','C:/docs/historial.pdf','done',0,100,20,10,'pdf-v11',7);
            INSERT INTO page_fts VALUES
                ('relevant','C:/docs/transformador.pdf',2,
                 'Pruebas de transformadores de potencia y tratamiento de aceite'),
                ('noise','C:/docs/historial.pdf',1,
                 'Qué evidencia hay disponible en el historial general');
            """
        )

    result = search_lexical_source(
        "pdf",
        state,
        "¿Qué evidencia hay sobre transformadores de potencia?",
    )

    assert [hit.path for hit in result.hits] == ["C:/docs/transformador.pdf"]
    provenance = result.hits[0].hit.provenance
    assert result.normalized_query == (
        '"Qué" AND "evidencia" AND "hay" AND "sobre" AND "transformadores" AND "de" AND "potencia"'
    )
    assert provenance["query_strategy"] == "question_content_terms_all"
    assert provenance["query_fallback_used"] is False
    assert provenance["query_rewrite_used"] is True
    assert result.hits[0].hit.provenance["applied_query"] == ('"transformadores" AND "potencia"')


@pytest.mark.parametrize(
    ("query", "document_text", "applied_query"),
    [
        (
            "What evidence is there about power transformers?",
            "Power transformers require dielectric testing",
            '"power" AND "transformers"',
        ),
        (
            "Welche Evidenz gibt es über Leistungstransformatoren?",
            "Leistungstransformatoren benötigen eine Isolationsprüfung",
            '"Leistungstransformatoren"',
        ),
    ],
)
def test_question_rewrite_preserves_english_and_german_subjects(
    tmp_path: Path,
    query: str,
    document_text: str,
    applied_query: str,
) -> None:
    state = tmp_path / "pdf.sqlite3"
    with sqlite3.connect(state) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,path TEXT NOT NULL,status TEXT NOT NULL,
                is_partial INTEGER NOT NULL,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,processing_signature TEXT NOT NULL,
                last_seen_run_id INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE page_fts USING fts5(
                file_key UNINDEXED,path UNINDEXED,page_number UNINDEXED,text,
                tokenize='unicode61 remove_diacritics 2'
            );
            INSERT INTO documents VALUES
                ('relevant','C:/docs/relevant.pdf','done',0,100,20,10,'pdf-v11',7);
            """
        )
        connection.execute(
            "INSERT INTO page_fts VALUES('relevant','C:/docs/relevant.pdf',1,?)",
            (document_text,),
        )

    result = search_lexical_source("pdf", state, query)

    assert [hit.path for hit in result.hits] == ["C:/docs/relevant.pdf"]
    provenance = result.hits[0].hit.provenance
    assert provenance["query_strategy"] == "question_content_terms_all"
    assert provenance["applied_query"] == applied_query


@pytest.mark.parametrize(
    ("source_kind", "factory", "table", "column"),
    [
        ("pdf", _create_pdf_state, "page_fts", "text"),
        ("docx", _create_docx_state, "document_fts", "body"),
        ("office", _create_office_state, "document_fts", "body"),
        ("audio", _create_audio_state, "transcript_fts", "body"),
        ("archive", _create_archive_state, "document_fts", "body"),
        ("text", _create_text_state, "document_fts", "body"),
    ],
)
def test_bounded_cjk_substring_fallback_covers_every_lexical_owner(
    tmp_path: Path,
    source_kind: str,
    factory: Callable[[Path], None],
    table: str,
    column: str,
) -> None:
    state = tmp_path / f"{source_kind}.sqlite3"
    factory(state)
    with sqlite3.connect(state) as connection:
        connection.execute(
            f"UPDATE {table} SET {column}=?",
            ("年度变压器油处理试验记录",),
        )
    before = hashlib.sha256(state.read_bytes()).hexdigest()

    result = search_lexical_source(source_kind, state, "油处理")

    assert hashlib.sha256(state.read_bytes()).hexdigest() == before
    assert len(result.hits) == 1
    provenance = result.hits[0].hit.provenance
    assert provenance["backend"] == "sqlite_bounded_cjk_substring"
    assert provenance["query_strategy"] == "cjk_substring_all_terms"
    assert provenance["substring_terms"] == ("油处理",)
    assert provenance["scanned_rows"] == 1
    assert provenance["scan_row_limit"] == 50_000
    assert result.hits[0].hit.vector_space == (f"lexical:substring-cjk:{source_kind}:v1")


def test_cjk_question_scaffolding_preserves_exact_technical_subject(
    tmp_path: Path,
) -> None:
    state = tmp_path / "pdf.sqlite3"
    _create_pdf_state(state)
    with sqlite3.connect(state) as connection:
        connection.execute(
            "UPDATE page_fts SET text=?",
            ("年度变压器油处理试验记录与绝缘油分析",),
        )

    result = search_lexical_source(
        "pdf",
        state,
        "有哪些关于变压器油处理的证据\N{FULLWIDTH QUESTION MARK}",
    )

    assert len(result.hits) == 1
    provenance = result.hits[0].hit.provenance
    assert provenance["applied_query"] == '"变压器油处理"'
    assert provenance["substring_terms"] == ("变压器油处理",)
    assert provenance["query_rewrite_used"] is True
    assert provenance["query_fallback_used"] is True


def test_mixed_cjk_fallback_keeps_latin_and_numeric_terms_mandatory(
    tmp_path: Path,
) -> None:
    state = tmp_path / "pdf.sqlite3"
    _create_pdf_state(state)
    with sqlite3.connect(state) as connection:
        connection.execute(
            "UPDATE page_fts SET text=?",
            ("IEC 60076 年度变压器油处理例行试验和绝缘测试",),
        )
        connection.execute(
            "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?)",
            ("noise", "C:/docs/noise.pdf", "done", 0, 50, 2, 1, "pdf-v11", 7),
        )
        connection.execute(
            "INSERT INTO page_fts VALUES(?,?,?,?)",
            ("noise", "C:/docs/noise.pdf", 1, "年度变压器油处理维护记录"),
        )

    result = search_lexical_source("pdf", state, "IEC 60076 油处理")

    assert [hit.path for hit in result.hits] == ["C:/docs/proteccion.pdf"]
    assert result.hits[0].hit.provenance["substring_terms"] == (
        "油处理",
        "IEC",
        "60076",
    )


def test_single_han_character_never_triggers_broad_substring_scan(
    tmp_path: Path,
) -> None:
    state = tmp_path / "pdf.sqlite3"
    _create_pdf_state(state)
    with sqlite3.connect(state) as connection:
        connection.execute(
            "UPDATE page_fts SET text=?",
            ("年度变压器油处理试验记录",),
        )

    result = search_lexical_source("pdf", state, "油")

    assert result.availability is LexicalAvailability.AVAILABLE
    assert result.hits == ()


def test_cjk_substring_scan_fails_closed_above_the_row_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "pdf.sqlite3"
    _create_pdf_state(state)
    with sqlite3.connect(state) as connection:
        connection.execute(
            "UPDATE page_fts SET text=?",
            ("年度变压器油处理试验记录",),
        )
        connection.execute(
            "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?)",
            ("second", "C:/docs/second.pdf", "done", 0, 50, 2, 1, "pdf-v11", 7),
        )
        connection.execute(
            "INSERT INTO page_fts VALUES(?,?,?,?)",
            ("second", "C:/docs/second.pdf", 1, "其他变压器油处理记录"),
        )
    monkeypatch.setattr(semantic_lexical, "MAX_CJK_SUBSTRING_SCAN_ROWS", 1)

    result = search_lexical_source("pdf", state, "油处理")

    assert result.availability is LexicalAvailability.READ_FAILED
    assert result.unavailable_reason == "cjk_substring_scan_limit_exceeded"
    assert result.hits == ()


def test_cjk_golden_set_has_top1_precision_and_negative_abstention(
    tmp_path: Path,
) -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "cjk_lexical_samples.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    documents = fixture["documents"]
    queries = fixture["queries"]
    assert len(documents) == 18
    assert len(queries) == 24
    assert sum(query["expected_id"] is not None for query in queries) == 19
    assert sum(query["expected_id"] is None for query in queries) == 5

    state = tmp_path / "cjk-golden.sqlite3"
    with sqlite3.connect(state) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,path TEXT NOT NULL,status TEXT NOT NULL,
                is_partial INTEGER NOT NULL,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,processing_signature TEXT NOT NULL,
                last_seen_run_id INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE page_fts USING fts5(
                file_key UNINDEXED,path UNINDEXED,page_number UNINDEXED,text,
                tokenize='unicode61 remove_diacritics 2'
            );
            """
        )
        for ordinal, document in enumerate(documents, start=1):
            text = str(document["text"])
            connection.execute(
                "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    document["id"],
                    document["path"],
                    "done",
                    0,
                    len(text.encode("utf-8")),
                    ordinal,
                    -1,
                    "pdf-v12-cjk-fixture",
                    1,
                ),
            )
            connection.execute(
                "INSERT INTO page_fts VALUES(?,?,?,?)",
                (document["id"], document["path"], 1, text),
            )
    before = hashlib.sha256(state.read_bytes()).hexdigest()
    cjk_fallbacks = 0

    for query in queries:
        result = search_lexical_source("pdf", state, str(query["query"]), limit=3)
        expected_id = query["expected_id"]
        if expected_id is None:
            assert result.hits == (), query
            continue
        assert result.hits, query
        assert result.hits[0].source_identity == expected_id, query
        if result.hits[0].hit.provenance["backend"] == ("sqlite_bounded_cjk_substring"):
            cjk_fallbacks += 1

    assert cjk_fallbacks >= 2
    assert hashlib.sha256(state.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    "query,match",
    [
        ("", "non-empty"),
        ("*** -- ()", "letters or numbers"),
        ("x" * (MAX_QUERY_CHARS + 1), "characters"),
        (" ".join("x" for _ in range(MAX_QUERY_TERMS + 1)), "terms"),
        ("x" * (MAX_QUERY_TERM_CHARS + 1), "terms cannot exceed"),
    ],
)
def test_natural_query_limits_are_explicit(query: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        compile_natural_fts_query(query)


# endregion [02]


# region [03] Independent resolved rankings


def test_searches_all_fts_sources_as_separate_resolved_rankings(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "pdf.sqlite3"
    docx = tmp_path / "docx.sqlite3"
    office = tmp_path / "office.sqlite3"
    audio = tmp_path / "audio.sqlite3"
    _create_pdf_state(pdf)
    _create_docx_state(docx)
    _create_office_state(office)
    _create_audio_state(audio)

    results = search_lexical_sources(
        LexicalStatePaths(pdf=pdf, docx=docx, office=office, audio=audio),
        "protección, interruptor!!!",
        limit=10,
    )

    assert tuple(result.ranking_name for result in results) == (
        "fts_pdf",
        "fts_docx",
        "fts_office",
        "fts_audio",
    )
    assert all(result.availability is LexicalAvailability.AVAILABLE for result in results)
    assert all(len(result.hits) == 1 for result in results)
    assert all(result.normalized_query == '"protección" AND "interruptor"' for result in results)

    by_ranking = {result.ranking_name: result.hits[0] for result in results}
    assert by_ranking["fts_pdf"].hit.item_id == "item:pdf:pdf-key"
    assert by_ranking["fts_pdf"].section_kind == "page"
    assert by_ranking["fts_pdf"].section_id == "7"
    assert by_ranking["fts_pdf"].source_status == "done"
    assert by_ranking["fts_pdf"].source_revision["is_partial"] is False
    assert by_ranking["fts_docx"].hit.item_id == "item:docx:docx-key"
    assert by_ranking["fts_docx"].source_status == "complete"
    assert by_ranking["fts_office"].hit.item_id == "item:xlsx:office-key"
    assert by_ranking["fts_office"].source_kind == "xlsx"
    assert by_ranking["fts_office"].source_status == "complete"
    assert by_ranking["fts_audio"].hit.item_id == "item:audio:audio-key"
    assert by_ranking["fts_audio"].source_status == "complete"

    for result in results:
        resolved = result.hits[0]
        assert result.search_hits == (resolved.hit,)
        assert resolved.hit.modality is EmbeddingModality.TEXT
        assert resolved.path is not None
        assert resolved.snippet is not None
        assert resolved.hit.provenance["backend"] == "sqlite_fts5"
        assert resolved.hit.provenance["rank_position"] == 1
        assert isinstance(resolved.hit.provenance["raw_bm25"], float)


def test_archive_source_is_additive_and_preserves_nested_member_provenance(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.sqlite3"
    _create_archive_state(archive)

    legacy = search_lexical_sources(LexicalStatePaths(), "protección")
    results = search_lexical_sources(
        LexicalStatePaths(archive=archive),
        "protección",
    )

    assert len(legacy) == 4
    assert tuple(result.ranking_name for result in results) == (
        "fts_pdf",
        "fts_docx",
        "fts_office",
        "fts_audio",
        "fts_archive",
    )
    hit = results[-1].hits[0]
    assert hit.source_kind == "archive"
    assert hit.path == "C:/docs/contenedor.zip!/interno.zip!/proteccion.txt"
    assert hit.section_kind == "archive_member"
    assert hit.section_provenance == {
        "inside_zip": True,
        "container_path": "C:/docs/contenedor.zip",
        "member_chain": "interno.zip!/proteccion.txt",
        "member_path": "proteccion.txt",
        "archive_depth": 2,
        "content_kind": "text",
    }


def test_generic_text_source_is_additive_and_preserves_physical_evidence(
    tmp_path: Path,
) -> None:
    text = tmp_path / "text.sqlite3"
    _create_text_state(text)

    results = search_lexical_sources(
        LexicalStatePaths(text=text),
        "protección alimentador",
    )

    assert tuple(result.ranking_name for result in results) == (
        "fts_pdf",
        "fts_docx",
        "fts_office",
        "fts_audio",
        "fts_text",
    )
    hit = results[-1].hits[0]
    assert hit.source_kind == "text"
    assert hit.path == "C:/docs/bitacora.eml"
    assert hit.source_identity == "21:34"
    assert hit.section_kind == "document"
    assert hit.source_revision == {
        "size": 500,
        "mtime_ns": 70,
        "birthtime_ns": 14,
        "processing_signature": "text-route-v1",
        "last_seen_run_id": 12,
        "revision_id": "revision:text:owner-native-fixture",
    }


def test_docx_materializes_ranking_before_generating_snippets(
    tmp_path: Path,
) -> None:
    state = tmp_path / "docx-large.sqlite3"
    with sqlite3.connect(state) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                status TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                processing_signature TEXT NOT NULL,
                last_seen_run_id INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE document_fts USING fts5(
                file_key UNINDEXED,
                path UNINDEXED,
                title,
                author,
                body,
                tokenize='unicode61 remove_diacritics 2'
            );
            """
        )
        for index in range(24):
            file_key = f"docx-{index:02d}"
            path = f"C:/docs/protection-{index:02d}.docx"
            body = f"protection differential transformer fixture {index} " + (
                f"technical filler {index} " * 2_000
            )
            connection.execute(
                "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)",
                (file_key, path, "complete", len(body), index, index, "docx-v5", 1),
            )
            connection.execute(
                "INSERT INTO document_fts VALUES(?,?,?,?,?)",
                (file_key, path, "Protection study", "Victor", body),
            )

        query = compile_natural_fts_query("protection differential transformer")
        plan_rows = connection.execute(
            "EXPLAIN QUERY PLAN " + semantic_lexical._SPECS["docx"].sql,
            (query, 7),
        ).fetchall()
        legacy_rows = connection.execute(
            """SELECT f.rowid AS fts_rowid,f.path,
            snippet(document_fts,4,'[',']',' ... ',24) AS snippet,
            bm25(document_fts) AS raw_bm25
            FROM document_fts AS f JOIN documents AS d ON d.file_key=f.file_key
            WHERE document_fts MATCH ? AND d.status IN ('complete','partial')
            ORDER BY raw_bm25,f.path COLLATE NOCASE LIMIT ?""",
            (query, 7),
        ).fetchall()

    result = search_lexical_source(
        "docx",
        state,
        "protection differential transformer",
        limit=7,
    )

    assert any("MATERIALIZE ranked" in str(row[3]) for row in plan_rows)
    assert tuple(
        (
            hit.hit.ref_id,
            hit.path,
            hit.snippet,
            hit.hit.provenance["raw_bm25"],
        )
        for hit in result.hits
    ) == tuple(legacy_rows)


@pytest.mark.parametrize("source_kind", ["pdf", "docx"])
def test_partial_text_owner_rows_remain_searchable_and_explicit(
    tmp_path: Path,
    source_kind: str,
) -> None:
    state = tmp_path / f"{source_kind}.sqlite3"
    if source_kind == "pdf":
        _create_pdf_state(state)
    else:
        _create_docx_state(state)
    with sqlite3.connect(state) as connection:
        connection.execute("UPDATE documents SET status='partial'")

    result = search_lexical_source(source_kind, state, "protección")

    assert result.availability is LexicalAvailability.AVAILABLE
    assert len(result.hits) == 1
    assert result.hits[0].source_status == "partial"
    assert result.hits[0].source_revision["processing_signature"] == (
        "pdf-v11" if source_kind == "pdf" else "docx-v5"
    )


def test_bounded_pdf_revision_preserves_partial_flag_with_done_status(
    tmp_path: Path,
) -> None:
    state = tmp_path / "pdf.sqlite3"
    _create_pdf_state(state)
    with sqlite3.connect(state) as connection:
        connection.execute("UPDATE documents SET is_partial=1")

    result = search_lexical_source("pdf", state, "protección")

    assert len(result.hits) == 1
    assert result.hits[0].source_status == "done"
    assert result.hits[0].source_revision["is_partial"] is True


def test_missing_and_unconfigured_sources_are_reported(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite3"

    results = search_lexical_sources(
        LexicalStatePaths(pdf=missing),
        "interruptor",
    )

    assert len(results) == 4
    assert results[0].availability is LexicalAvailability.DATABASE_MISSING
    assert results[0].unavailable_reason == "state_database_missing"
    assert results[0].hits == ()
    assert all(result.availability is LexicalAvailability.NOT_CONFIGURED for result in results[1:])


@pytest.mark.parametrize("limit", [0, 1_001])
def test_search_limit_is_bounded(limit: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 1000"):
        search_lexical_source("pdf", None, "interruptor", limit=limit)


def test_unknown_source_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported lexical source"):
        search_lexical_source("image", None, "interruptor")


def test_corrupt_sqlite_is_not_reported_as_unavailable(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"this is not a SQLite database")

    with pytest.raises(sqlite3.DatabaseError):
        search_lexical_source("pdf", corrupt, "interruptor")


# endregion [03]
