"""Semantic hydration observes the existing operation between bounded work units."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from neocortex.knowledge.knowledge_read_budget import (
    KnowledgeReadBudget,
    KnowledgeReadBudgetExceeded,
)
from neocortex.runtime.control.read_operation import read_operation
from neocortex.semantic import semantic_search_repository as repository
from neocortex.semantic.semantic_state import resolve_search_hits, search_exact_evidence_page
from tests.test_retrieval_target_diagnostics import _published_fixture
from tests.test_semantic_generation_search_failures import _query


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


@pytest.mark.parametrize("reason", ("cancelled", "deadline_exceeded"))
@pytest.mark.parametrize(
    ("boundary", "hit_limit"),
    (
        ("_decode_chunk_text", 7),
        ("query_centered_snippet", 7),
        ("query_term_support", 7),
        ("_resolved_text_search_hit", 1),
    ),
)
def test_expired_hydration_stops_at_the_work_boundary_without_returning_a_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    boundary: str,
    hit_limit: int,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _published_fixture(database)
    hits = search_exact_evidence_page(
        database, _query(model), limit=hit_limit, max_vectors=7,
    ).hits
    assert len(hits) == hit_limit
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    expired = False
    calls = 0
    original = getattr(repository, boundary)

    def finish_one_unit(*args, **kwargs):
        nonlocal expired, calls
        value = original(*args, **kwargs)
        calls += 1
        expired = True
        return value

    monkeypatch.setattr(repository, boundary, finish_one_unit)
    budget = KnowledgeReadBudget(
        max_rows=hit_limit,
        cancellation_check=(lambda: expired) if reason == "cancelled" else None,
        deadline_ns=50 if reason == "deadline_exceeded" else None,
        monotonic_clock=lambda: 100 if expired else 1,
    )
    returned = False
    with pytest.raises(KnowledgeReadBudgetExceeded) as error:
        with read_operation(budget, None):
            resolve_search_hits(database, hits, query="presión")
            returned = True

    assert error.value.reason == reason
    assert not returned, "an expired hydration must not return a completed tuple"
    assert calls == 1, "later units must not run after cancellation/deadline"
    assert budget.rows_used == hit_limit
    assert budget.vectors_used == 0
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("query", (None, "presión"))
def test_hydration_with_a_live_allowance_preserves_hits_and_row_accounting(
    tmp_path: Path, query: str | None,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _published_fixture(database)
    hits = search_exact_evidence_page(database, _query(model), limit=7, max_vectors=7).hits
    reference = resolve_search_hits(database, hits, query=query)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    budget = KnowledgeReadBudget(max_rows=len(hits), cancellation_check=lambda: False)

    with read_operation(budget, None):
        actual = resolve_search_hits(database, hits, query=query)

    assert actual == reference
    assert tuple(value.hit for value in actual) == hits
    assert budget.rows_used == len(hits)
    assert budget.vectors_used == 0
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
