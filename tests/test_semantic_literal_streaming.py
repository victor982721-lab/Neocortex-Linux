"""Literal evidence keeps exact semantics with bounded, interruptible work."""

from __future__ import annotations

import hashlib
from pathlib import Path
import random
import re
import sqlite3
import unicodedata

import pytest

from neocortex.knowledge.knowledge_read_budget import (
    KnowledgeReadBudget,
    KnowledgeReadBudgetExceeded,
)
from neocortex.runtime.control.read_operation import read_operation
from neocortex.semantic import semantic_lexical as lexical
from neocortex.semantic import semantic_search_repository as repository
from neocortex.semantic import semantic_state
from neocortex.semantic.semantic_models import SemanticItem, TextChunk, fingerprint_text
from tests.test_semantic_generation_search_failures import _initialize, _model, _query
from tests.test_retrieval_target_diagnostics import _published_fixture


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _fold(term: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", term.casefold())
        if not unicodedata.combining(char)
    )


def _oracle(text: str, query: str, terms: tuple[str, ...], max_chars: int):
    """Enumerate all small candidate spans, independently of the streaming state."""
    tokens = list(re.finditer(r"[^\W_]+", text))
    folded = [_fold(token.group()) for token in tokens]
    matches = [index for index, term in enumerate(folded) if term in terms]
    observed = {folded[index] for index in matches}
    complete_spans = [
        right - left + 1 for left in matches for right in matches if right >= left
        and {folded[index] for index in range(left, right + 1)} >= observed
    ]
    phrase = [_fold(term) for term in re.findall(r"[^\W_]+", query[:4096])][:64]
    literal = {
        "matched_terms": [term for term in terms if term in observed],
        "missing_terms": [term for term in terms if term not in observed],
        "minimum_span_terms": min(complete_spans, default=None),
        "phrase_match": bool(phrase) and any(
            folded[index:index + len(phrase)] == phrase for index in range(len(folded))
        ),
        "term_coverage": len(observed) / len(terms) if terms else 0.0,
    }
    start = 0
    if max_chars and len(text) > max_chars and matches:
        candidates = [
            (-len(set(folded[left:right + 1]) & set(terms)),
             tokens[right].end() - tokens[left].start(), tokens[left].start(), tokens[right].end())
            for left in matches for right in matches if right >= left
            and tokens[right].end() - tokens[left].start() <= max_chars
        ]
        if candidates:
            _, _, first, last = min(candidates)
        else:
            first, last = tokens[matches[0]].span()
        start = min(max(0, first - max(0, max_chars - (last - first)) // 2), len(text) - max_chars)
    end = min(len(text), start + max_chars)
    return literal, (text[start:end] if max_chars else None), {
        "policy_signature": "query-centered-scored-chunk-v1",
        "basis": "normalized_scored_chunk",
        "start_in_chunk": start,
        "end_in_chunk": end,
        "chunk_chars": len(text),
        "truncated": start > 0 or end < len(text),
        "query_terms_found": bool(matches),
    }


@pytest.mark.parametrize("block_chars", (1, 2, 7, 4096))
def test_literal_windows_and_support_match_exhaustive_small_spans(
    monkeypatch: pytest.MonkeyPatch, block_chars: int,
) -> None:
    monkeypatch.setattr(lexical, "_LITERAL_SCAN_BLOCK_CHARS", block_chars)
    rng = random.Random(1968)
    queries = (
        ("alpha beta gamma", ("alpha", "beta", "gamma")),
        ("alpha alpha beta", ("alpha", "beta")),
        ("alpha missing beta", ("alpha", "missing", "beta")),
        ("de la", ()), ("", ()),
        ("Σ ß ᾈ \uff14 ½", ("\u03c3", "ss", "\u03b1\u03b9", "4", "1\u20442")),
    )
    for _ in range(40):
        text = " ".join(rng.choices(
            ("alpha", "ÁLPHA", "beta", "gamma", "delta", "de", "la", "Σ", "ß", "ᾈ", "\uff14", "½"),
            k=rng.randrange(1, 16),
        ))
        for query, terms in queries:
            max_chars = rng.choice((0, 1, 3, 7, 12, len(text), len(text) + 1))
            literal, snippet, excerpt = _oracle(text, query, terms, max_chars)
            support = lexical.query_term_support(query, text, basis="scored_chunk")
            assert {key: support[key] for key in literal} == literal
            assert lexical.query_centered_snippet(text, query, max_chars=max_chars) == (snippet, excerpt)
            prepared = lexical.prepare_literal_query(query)
            analysis = lexical.analyze_literal_text(text, prepared, snippet_chars=max_chars)
            assert lexical.query_term_support(
                query, text, basis="scored_chunk", prepared=prepared, analysis=analysis,
            ) == support
            assert lexical.query_centered_snippet(
                text, query, max_chars=max_chars, prepared=prepared, analysis=analysis,
            ) == (snippet, excerpt)


@pytest.mark.parametrize("offset", (4095, 4096, 4097))
def test_original_token_boundaries_survive_unicode_and_oversized_words(offset: int) -> None:
    text = "." * offset + "ＡſΣᾈ１２３ ßeta ﬁnal e\u0301 vapor" + "z" * 100_000 + " presión"
    query = "as\u03c3\u03b1\u03b9123 sseta final e presión"
    terms = ("as\u03c3\u03b1\u03b9123", "sseta", "final", "e", "presion")
    expected, snippet, excerpt = _oracle(text, query, terms, 27)
    actual = lexical.query_term_support(query, text, basis="scored_chunk")
    assert {key: actual[key] for key in expected} == expected
    assert lexical.query_centered_snippet(text, query, max_chars=27) == (snippet, excerpt)


def test_a_matching_word_larger_than_the_window_keeps_the_original_fallback() -> None:
    query = "\uff21" * 4096
    text = "." * 4095 + query
    snippet, excerpt = lexical.query_centered_snippet(text, query, max_chars=13)
    assert snippet == text[4095:4108]
    assert excerpt["start_in_chunk"] == 4095 and excerpt["query_terms_found"] is True
    support = lexical.query_term_support(query, text, basis="scored_chunk")
    assert support["phrase_match"] is True and support["minimum_span_terms"] == 1


@pytest.mark.parametrize("query", (None, "", "de la y el"))
@pytest.mark.parametrize("max_chars", (0, 240))
def test_a_snippet_without_content_terms_never_scans_the_text(
    monkeypatch: pytest.MonkeyPatch, query: str | None, max_chars: int,
) -> None:
    def forbidden_scan(*_args, **_kwargs):
        raise AssertionError("an empty literal query does not require a token traversal")

    monkeypatch.setattr(lexical, "_literal_tokens", forbidden_scan)
    text = "vapor " * 100_000
    snippet, extent = lexical.query_centered_snippet(text, query, max_chars=max_chars)
    assert snippet == (text[:max_chars] if max_chars else None)
    assert extent["query_terms_found"] is False


class _ObservedPattern:
    def __init__(self, original, on_block):
        self.original = original
        self.on_block = on_block

    def findall(self, *args):
        return self.original.findall(*args)

    def finditer(self, text, start=0, end=None):
        stop = len(text) if end is None else end
        yield from self.original.finditer(text, start, stop)
        self.on_block(text, start, stop)


@pytest.mark.parametrize("reason", ("cancelled", "deadline_exceeded"))
@pytest.mark.parametrize("helper", ("support", "snippet"))
@pytest.mark.parametrize("text", ("vapor " * 100_000 + "presión", "x" * 600_007), ids=("many_tokens", "single_token"))
def test_budget_interrupts_inside_the_first_text_block_including_one_huge_token(
    monkeypatch: pytest.MonkeyPatch, reason: str, helper: str, text: str,
) -> None:
    traversed = []

    def after_block(value, start, end):
        if value is text:
            traversed.append((start, end))

    monkeypatch.setattr(lexical, "_NATURAL_TERM", _ObservedPattern(lexical._NATURAL_TERM, after_block))
    budget = KnowledgeReadBudget(
        cancellation_check=(lambda: bool(traversed)) if reason == "cancelled" else None,
        deadline_ns=50 if reason == "deadline_exceeded" else None,
        monotonic_clock=lambda: 100 if traversed else 1,
    )
    with pytest.raises(KnowledgeReadBudgetExceeded) as caught:
        with read_operation(budget, None):
            if helper == "support":
                lexical.query_term_support("vapor presión", text, basis="scored_chunk")
            else:
                lexical.query_centered_snippet(text, "vapor presión", max_chars=240)
            pytest.fail("an interrupted helper must not return completed evidence")
    assert caught.value.reason == reason
    assert traversed == [(0, 4096)]
    assert budget.rows_used == budget.vectors_used == budget.temporary_bytes_used == 0


def test_hydration_prepares_one_query_and_reuses_literal_work_for_whole_chunk_snippets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "semantic.sqlite3"
    model = _published_fixture(path)
    hits = semantic_state.search_exact_evidence_page(path, _query(model), limit=7, max_vectors=7).hits
    expected = semantic_state.resolve_search_hits(path, hits, query="presión interna")
    prepared = []
    scanned = []
    original_prepare = repository.prepare_literal_query
    original_tokens = lexical._literal_tokens

    def prepare(query):
        value = original_prepare(query)
        prepared.append(value)
        return value

    def tokens(text, max_folded_chars, **kwargs):
        scanned.append(text)
        yield from original_tokens(text, max_folded_chars, **kwargs)

    monkeypatch.setattr(repository, "prepare_literal_query", prepare)
    monkeypatch.setattr(lexical, "_literal_tokens", tokens)
    budget = KnowledgeReadBudget(max_rows=7, cancellation_check=lambda: False)
    before = hashlib.sha256(path.read_bytes()).digest()
    with read_operation(budget, None):
        actual = semantic_state.resolve_search_hits(path, hits, query="presión interna")
    assert actual == expected
    assert len(prepared) == 1 and len(scanned) == len(hits) == 7
    assert budget.rows_used == 7 and budget.vectors_used == 0
    assert hashlib.sha256(path.read_bytes()).digest() == before


def _publish_large_chunk(path: Path):
    model = _model()
    _initialize(path, model)
    text = "vapor " * 100_000 + "presión"
    item = SemanticItem("item:literal", "text", "fixture:literal", "literal-source-v1", fingerprint_text(text))
    semantic_state.upsert_semantic_item(path, item, refresh_token="items", updated_ns=10)
    chunk = TextChunk("chunk:literal", item.item_id, 0, "text", "fulltext", 0, len(text),
                      text, fingerprint_text(text), "literal-chunking-v1")
    semantic_state.stage_text_chunks(path, (chunk,), refresh_token="chunks", updated_ns=11)
    semantic_state.finalize_text_chunk_refresh(path, item_id=item.item_id, chunking_signature="literal-chunking-v1",
                                              refresh_token="chunks", updated_ns=12)
    generation = semantic_state.start_embedding_generation(path, model_signature=model.model_signature,
                                                           processing_signature="literal-processing-v1", started_ns=20)
    semantic_state.enqueue_text_chunk_jobs(path, generation, (chunk.chunk_id,), now_ns=21)
    lease, = semantic_state.claim_embedding_jobs(path, generation, worker_id="fixture", limit=1,
                                                lease_seconds=60, now_ns=22)
    semantic_state.complete_embedding_job(path, lease.job_id, worker_id="fixture", vector=(1.0, 0.0, 0.0, 0.0),
                                          provenance={"backend": "fixture"}, now_ns=23)
    semantic_state.finalize_embedding_generation(path, generation, completed_ns=24)
    return model, text


@pytest.mark.parametrize("reason", ("cancelled", "deadline_exceeded"))
def test_published_search_aborts_during_literal_work_and_retry_preserves_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    path = tmp_path / "semantic.sqlite3"
    model, text = _publish_large_chunk(path)
    hits = semantic_state.search_exact_evidence_page(path, _query(model), limit=1, max_vectors=1).hits
    assert len(hits) == 1
    expected = semantic_state.resolve_search_hits(path, hits, query="vapor presión")
    traversed = []

    def after_block(value, start, end):
        if len(value) == len(text):
            traversed.append((start, end))

    original = lexical._NATURAL_TERM
    monkeypatch.setattr(lexical, "_NATURAL_TERM", _ObservedPattern(original, after_block))
    budget = KnowledgeReadBudget(
        max_rows=1,
        cancellation_check=(lambda: bool(traversed)) if reason == "cancelled" else None,
        deadline_ns=50 if reason == "deadline_exceeded" else None,
        monotonic_clock=lambda: 100 if traversed else 1,
    )
    before = hashlib.sha256(path.read_bytes()).digest()
    with pytest.raises(KnowledgeReadBudgetExceeded) as caught:
        with read_operation(budget, None):
            semantic_state.resolve_search_hits(path, hits, query="vapor presión")
            pytest.fail("an expired search must not return evidence")
    assert caught.value.reason == reason and traversed == [(0, 4096)]
    assert budget.rows_used == 1 and budget.vectors_used == 0
    assert hashlib.sha256(path.read_bytes()).digest() == before
    monkeypatch.setattr(lexical, "_NATURAL_TERM", original)
    retry_budget = KnowledgeReadBudget(max_rows=1, cancellation_check=lambda: False)
    with read_operation(retry_budget, None):
        actual = semantic_state.resolve_search_hits(path, hits, query="vapor presión")
    assert actual == expected and actual[0].hit == hits[0]
    assert retry_budget.rows_used == 1 and budget.rows_used == 1


def test_reuse_cannot_attach_another_text_query_or_window_to_evidence() -> None:
    text = "alpha beta"
    prepared = lexical.prepare_literal_query("alpha")
    analysis = lexical.analyze_literal_text(text, prepared, snippet_chars=5)
    with pytest.raises(ValueError, match="text and query"):
        lexical.query_term_support("alpha", "other", basis="scored_chunk", prepared=prepared, analysis=analysis)
    with pytest.raises(ValueError, match="prepared literal query"):
        lexical.query_term_support("beta", text, basis="scored_chunk", prepared=prepared, analysis=analysis)
    with pytest.raises(ValueError, match="window"):
        lexical.query_centered_snippet(text, "alpha", max_chars=7, prepared=prepared, analysis=analysis)


@pytest.mark.parametrize("multiple", (False, True))
@pytest.mark.parametrize("mode", ("standalone_callback", "ambient_deadline"))
def test_lexical_last_hit_preserves_standalone_callback_and_ambient_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multiple: bool, mode: str,
) -> None:
    path = tmp_path / "text.sqlite3"
    text = "noise " * 110_000 + "vapor presión"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE documents(file_key TEXT PRIMARY KEY, path TEXT, status TEXT,
              size INTEGER, mtime_ns INTEGER, birthtime_ns INTEGER,
              processing_signature TEXT, last_seen_run_id INTEGER, revision_id TEXT);
            CREATE VIRTUAL TABLE document_fts USING fts5(file_key UNINDEXED,
              path UNINDEXED, title, author, keywords, body,
              tokenize='unicode61 remove_diacritics 2');
        """)
        connection.execute(
            "INSERT INTO documents VALUES('literal','/synthetic/literal.txt','complete',?,1,1,'literal-v1',1,'revision')",
            (len(text.encode()),),
        )
        connection.execute("INSERT INTO document_fts VALUES('literal','/synthetic/literal.txt','','','',?)", (text,))

    def search(callback=None):
        if multiple:
            return lexical.search_lexical_sources(
                lexical.LexicalStatePaths(text=path), "vapor presión", cancellation_check=callback,
            )
        return lexical.search_lexical_source("text", path, "vapor presión", cancellation_check=callback)

    expected = search()
    traversed = []

    def after_block(value, start, end):
        if len(value) == len(text):
            traversed.append((start, end))

    original = lexical._NATURAL_TERM
    monkeypatch.setattr(lexical, "_NATURAL_TERM", _ObservedPattern(original, after_block))
    cancellation = RuntimeError("original standalone cancellation")

    def callback():
        if traversed:
            raise cancellation

    budget = KnowledgeReadBudget(max_rows=1, deadline_ns=50, monotonic_clock=lambda: 100 if traversed else 1)
    before = hashlib.sha256(path.read_bytes()).digest()
    returned = False
    with pytest.raises(RuntimeError) as caught:
        if mode == "ambient_deadline":
            with read_operation(budget, None):
                search()
                returned = True
        else:
            search(callback)
            returned = True
    if mode == "ambient_deadline":
        assert isinstance(caught.value, KnowledgeReadBudgetExceeded)
        assert caught.value.reason == "deadline_exceeded" and budget.rows_used == 1
    else:
        assert caught.value is cancellation
    assert not returned and traversed == [(0, 4096)]
    assert hashlib.sha256(path.read_bytes()).digest() == before
    monkeypatch.setattr(lexical, "_NATURAL_TERM", original)
    assert search() == expected
