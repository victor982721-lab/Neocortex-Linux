"""Literal retrieval witnesses, negation scope and verbatim scored excerpts."""

from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path

import pytest

from neocortex.semantic import semantic_lexical, semantic_search_repository
from neocortex.semantic.semantic_lexical import (
    LexicalAvailability,
    query_centered_snippet,
    query_term_support,
    search_lexical_source,
)
from neocortex.semantic.semantic_models import (
    EmbeddingModality,
    SearchHit,
    fingerprint_text,
)


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _docx_state(path: Path, texts: tuple[str, ...]) -> None:
    """Create a self-contained FTS owner under pytest's temporary directory."""
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY, path TEXT NOT NULL,
                status TEXT NOT NULL, size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL, birthtime_ns INTEGER NOT NULL,
                processing_signature TEXT NOT NULL, last_seen_run_id INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE document_fts USING fts5(
                file_key UNINDEXED, path UNINDEXED, title, author, body,
                tokenize='unicode61 remove_diacritics 2'
            );
            """
        )
        for index, text in enumerate(texts):
            identity = f"fixture-{index}"
            source_path = str(path.parent / f"fixture-{index}.docx")
            connection.execute(
                "INSERT INTO documents VALUES(?,?,'complete',?,1,-1,'fixture-v1',1)",
                (identity, source_path, len(text.encode("utf-8"))),
            )
            connection.execute(
                "INSERT INTO document_fts VALUES(?,?,'','',?)",
                (identity, source_path, text),
            )


@pytest.mark.parametrize(
    "negation",
    ("sin", "no", "nunca", "ningún", "not", "without", "never", "nicht", "ohne", "kein"),
)
def test_protected_negation_is_mandatory_in_every_fts_plan(negation: str) -> None:
    plan = semantic_lexical._compile_natural_fts_query_plan(
        f"el equipo {negation} energizado"
    )

    assert all(strategy != "content_terms_any_two" for strategy, _ in plan.fallbacks)
    assert plan.fallbacks, "stopword-only elision remains available"
    assert all(
        f'"{negation}"' in query
        for query in (plan.primary_query, *(query for _, query in plan.fallbacks))
    )


@pytest.mark.parametrize(
    ("query", "positive", "negative"),
    (
        ("¿Qué equipo no energizado?", "equipo energizado", "equipo no energizado"),
        ("equipo sin presión", "equipo con presión", "equipo sin presión"),
        ("find equipment without pressure", "equipment pressure", "equipment without pressure"),
        ("equipment not energized", "equipment energized", "equipment not energized"),
        ("zeige Ausrüstung ohne Druck", "Ausrüstung Druck", "Ausrüstung ohne Druck"),
    ),
)
def test_fts_cannot_recover_negated_query_by_dropping_its_negation(
    tmp_path: Path, query: str, positive: str, negative: str,
) -> None:
    missing = tmp_path / "missing-negation.sqlite3"
    _docx_state(missing, (positive,))
    absent = search_lexical_source("docx", missing, query)
    assert absent.availability is LexicalAvailability.AVAILABLE
    assert absent.hits == ()

    complete = tmp_path / "complete-negation.sqlite3"
    _docx_state(complete, (positive, negative))
    found = search_lexical_source("docx", complete, query)
    assert len(found.hits) == 1
    assert found.hits[0].source_identity == "fixture-1"
    support = found.hits[0].hit.provenance["query_support"]
    assert isinstance(support, dict)
    assert support["support"] == "full_terms"
    assert support["missing_negation_terms"] == []
    assert support["interpretation"] == "literal_overlap_not_entailment"
    assert support["query_strategy"] != "content_terms_any_two"


def test_any_two_fallback_reports_partial_literal_support_without_changing_bm25(
    tmp_path: Path,
) -> None:
    state = tmp_path / "partial-support.sqlite3"
    _docx_state(state, ("La protección diferencial permaneció operativa", "reactancia nominal"))
    query = "protección diferencial reactancia"

    result = search_lexical_source("docx", state, query)
    assert result.availability is LexicalAvailability.AVAILABLE
    assert len(result.hits) == 1
    resolved = result.hits[0]
    provenance = resolved.hit.provenance
    support = provenance["query_support"]
    assert isinstance(support, dict)
    assert "query_support" not in resolved.section_provenance
    assert support["basis"] == "fts_snippet"
    assert support["support"] == "partial_terms"
    assert support["matched_terms"] == ["proteccion", "diferencial"]
    assert support["missing_terms"] == ["reactancia"]
    assert support["term_coverage"] == pytest.approx(2 / 3)
    assert support["interpretation"] == "literal_overlap_not_entailment"
    assert support["query_strategy"] == "content_terms_any_two"
    assert support["query_fallback_used"] is True
    assert provenance["query_strategy"] == "content_terms_any_two"
    assert provenance["query_fallback_used"] is True
    with sqlite3.connect(state) as connection:
        raw = connection.execute(
            "SELECT bm25(document_fts) FROM document_fts WHERE document_fts MATCH ?",
            (provenance["applied_query"],),
        ).fetchone()[0]
    assert provenance["raw_bm25"] == raw
    assert resolved.hit.score == -raw
    assert search_lexical_source("docx", state, query).hits == result.hits


@pytest.mark.parametrize("basis", ("fts_snippet", "scored_chunk"))
@pytest.mark.parametrize(
    ("text", "matched", "missing", "phrase", "span"),
    (
        ("[PRESION] [INTERNA]", ["presion", "interna"], [], True, 2),
        ("Presión nominal mayor, medición interna estable", ["presion", "interna"], [], False, 5),
        ("Solo presión fue medida", ["presion"], ["interna"], False, 1),
        ("depresión interior", [], ["presion", "interna"], False, None),
    ),
)
def test_query_support_reports_phrase_proximity_and_missing_terms_not_entailment(
    basis: str, text: str, matched: list[str], missing: list[str],
    phrase: bool, span: int | None,
) -> None:
    support = query_term_support("presión interna", text, basis=basis)

    assert support["basis"] == basis
    assert support["matched_terms"] == matched
    assert support["missing_terms"] == missing
    assert support["term_coverage"] == len(matched) / 2
    assert support["phrase_match"] is phrase
    assert support["minimum_span_terms"] == span
    assert support["support"] == (
        "no_terms" if not matched else "partial_terms" if missing else "full_terms"
    )
    assert support["interpretation"] == "literal_overlap_not_entailment"
    assert "probability" not in support
    assert "score" not in support


def test_query_support_deduplicates_accented_terms_and_preserves_missing_negation() -> None:
    support = query_term_support(
        "sin presión PRESION interna", "presión interna", basis="scored_chunk"
    )

    assert support["matched_terms"] == ["presion", "interna"]
    assert support["missing_terms"] == ["sin"]
    assert support["negation_terms"] == ["sin"]
    assert support["missing_negation_terms"] == ["sin"]
    assert support["term_coverage"] == pytest.approx(2 / 3)
    assert support["phrase_match"] is False
    assert support["support"] == "partial_terms"


def test_full_literal_negation_overlap_does_not_claim_its_scope_is_satisfied() -> None:
    support = query_term_support(
        "sin presión interna",
        "La presión interna era estable, sin desviaciones",
        basis="scored_chunk",
    )

    assert support["support"] == "full_terms"
    assert support["negation_terms"] == ["sin"]
    assert support["missing_negation_terms"] == []
    assert support["phrase_match"] is False
    assert support["interpretation"] == "literal_overlap_not_entailment"


@pytest.mark.parametrize("query", ("", "de la y el"))
def test_query_support_never_reports_full_support_for_no_content_terms(query: str) -> None:
    support = query_term_support(query, "presión interna", basis="scored_chunk")

    assert support["support"] == "no_content_terms"
    assert support["matched_terms"] == support["missing_terms"] == []
    assert support["term_coverage"] == 0.0
    assert support["phrase_match"] is False
    assert support["minimum_span_terms"] is None


def test_proximity_uses_closest_complete_occurrence_not_first_match() -> None:
    support = query_term_support(
        "presión interna",
        "presión nominal todavía estable durante inspección; presión interna registrada",
        basis="scored_chunk",
    )

    assert support["minimum_span_terms"] == 2
    assert support["phrase_match"] is True


def _assert_verbatim_excerpt(text: str, snippet: str | None, excerpt: dict[str, object]) -> None:
    start, end = excerpt["start_in_chunk"], excerpt["end_in_chunk"]
    assert isinstance(start, int) and isinstance(end, int)
    assert 0 <= start <= end <= len(text)
    assert (snippet or "") == text[start:end]
    assert excerpt["chunk_chars"] == len(text)
    assert excerpt["basis"] == "normalized_scored_chunk"
    assert excerpt["truncated"] is (start > 0 or end < len(text))


def test_query_centered_snippet_selects_late_accented_evidence_as_exact_slice() -> None:
    text = "Información preliminar sin relación. " * 18
    text += "La PÉRDIDA de PRESIÓN INTERNA se documentó en sitio. "
    text += "Notas posteriores independientes. " * 8

    snippet, excerpt = query_centered_snippet(text, "pérdida presión interna", max_chars=76)

    assert snippet is not None and len(snippet) <= 76
    assert "PÉRDIDA de PRESIÓN INTERNA" in snippet
    assert excerpt["start_in_chunk"] != 0
    assert excerpt["query_terms_found"] is True
    _assert_verbatim_excerpt(text, snippet, excerpt)


@pytest.mark.parametrize(
    ("text", "query", "max_chars", "expected", "found"),
    (
        ("presión interna", "presión", 0, None, True),
        ("presión interna", "torque", 0, None, False),
        ("presión interna", "torque", 7, "presión", False),
        ("presión interna", None, 7, "presión", False),
        ("presión interna", "presión", 30, "presión interna", True),
        ("", "presión", 30, "", False),
        ("", "presión", 0, None, False),
    ),
)
def test_excerpt_metadata_describes_zero_empty_and_unmatched_windows_truthfully(
    text: str, query: str | None, max_chars: int, expected: str | None, found: bool,
) -> None:
    snippet, excerpt = query_centered_snippet(text, query, max_chars=max_chars)

    assert snippet == expected
    assert len(snippet or "") <= max_chars
    assert excerpt["query_terms_found"] is found
    _assert_verbatim_excerpt(text, snippet, excerpt)


def test_query_centered_snippet_rejects_negative_length() -> None:
    with pytest.raises(ValueError, match="negative"):
        query_centered_snippet("presión interna", "presión", max_chars=-1)


def _published_text_snapshot(hit: SearchHit, text: str) -> sqlite3.Row:
    fingerprint = fingerprint_text(text)
    values = {
        "generation_id": hit.generation_id,
        "model_signature": hit.indexed_model_signature,
        "vector_space": hit.vector_space,
        "modality": hit.modality.value,
        "entity_kind": "text_chunk",
        "entity_id": hit.entity_id,
        "item_id": hit.item_id,
        "path": "/tmp/retrieval-fixture.docx",
        "source_kind": "docx",
        "source_identity": "retrieval-fixture",
        "source_revision_json": '{"mtime_ns": 1}',
        "item_provenance_json": '{"source_status": "complete"}',
        "current_item_provenance_json": '{"source_status": "complete"}',
        "published_revision_id": 4,
        "current_revision_id": 4,
        "section_kind": "document",
        "section_id": "fulltext",
        "start_char": 400,
        "end_char": 400 + len(text),
        "section_provenance_json": json.dumps({"fixture_witness": "preserved"}),
        "content_xxh3_128": fingerprint.xxh3_128,
        "content_bytes": fingerprint.byte_count,
        "content_xxh3_64_guard": fingerprint.xxh3_64_guard,
        "text_zlib": zlib.compress(text.encode("utf-8")),
    }
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT " + ", ".join(f":{key} AS {key}" for key in values), values
        ).fetchone()


@pytest.mark.parametrize(
    ("query", "full_missing", "snippet_missing"),
    (
        ("sin presión interna", ["sin"], ["sin"]),
        ("reactancia presión interna", [], ["reactancia"]),
    ),
)
def test_resolved_scored_chunk_keeps_original_score_and_provenance_while_exposing_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str,
    full_missing: list[str], snippet_missing: list[str],
) -> None:
    text = "Reactancia registrada. " + "Registro preliminar de actividades generales. " * 20
    text += "La presión interna se mantuvo estable durante la inspección."
    hit = SearchHit(
        ref_id=8, entity_id="chunk:fixture", item_id="item:fixture",
        indexed_model_signature="existing-fixture-model-v1", vector_space="fixture-space",
        modality=EmbeddingModality.TEXT, score=0.731, generation_id=3,
        provenance={"backend": "fixture", "score_transform": "none"},
    )
    snapshot = _published_text_snapshot(hit, text)

    def load_fixture_snapshots(path: Path, member_ids: tuple[int, ...]) -> dict[int, sqlite3.Row]:
        assert path == tmp_path / "unopened.sqlite3"
        assert member_ids == (hit.ref_id,)
        return {hit.ref_id: snapshot}

    monkeypatch.setattr(
        semantic_search_repository, "_load_search_hit_snapshots", load_fixture_snapshots
    )
    baseline, = semantic_search_repository.resolve_search_hits(
        tmp_path / "unopened.sqlite3", (hit,), snippet_chars=72
    )
    resolved, = semantic_search_repository.resolve_search_hits(
        tmp_path / "unopened.sqlite3", (hit,), snippet_chars=72, query=query
    )

    assert baseline.hit is resolved.hit is hit
    assert resolved.hit.score == 0.731
    assert resolved.hit.provenance == {"backend": "fixture", "score_transform": "none"}
    assert baseline.snippet == text[:72]
    assert resolved.snippet != baseline.snippet
    assert resolved.snippet is not None and "presión interna" in resolved.snippet
    assert resolved.start_char == 400 and resolved.end_char == 400 + len(text)
    assert resolved.section_provenance["fixture_witness"] == "preserved"
    support = resolved.section_provenance["query_support"]
    assert isinstance(support, dict)
    assert support["basis"] == "scored_chunk"
    assert support["support"] == ("partial_terms" if full_missing else "full_terms")
    assert support["missing_terms"] == full_missing
    assert support["missing_negation_terms"] == full_missing
    assert support["interpretation"] == "literal_overlap_not_entailment"
    snippet_support = resolved.section_provenance["snippet_query_support"]
    assert isinstance(snippet_support, dict)
    assert snippet_support["basis"] == "scored_chunk_window"
    assert snippet_support["support"] == "partial_terms"
    assert snippet_support["missing_terms"] == snippet_missing
    assert snippet_support["interpretation"] == "literal_overlap_not_entailment"
    excerpt = resolved.section_provenance["retrieval_excerpt"]
    assert isinstance(excerpt, dict)
    _assert_verbatim_excerpt(text, resolved.snippet, excerpt)
    assert not (tmp_path / "unopened.sqlite3").exists()
