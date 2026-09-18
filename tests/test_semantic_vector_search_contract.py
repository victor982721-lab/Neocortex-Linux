"""Conformance of real vector backends and hostile boundary responses."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
from typing import Any

import pytest

from neocortex.semantic import semantic_search_repository as repository
from neocortex.semantic.semantic_exact_index import (
    ExactIndexUnavailable, PersistedExactVectorSearch, prepare_exact_index,
)
from neocortex.semantic.semantic_vector_search import (
    VectorSearchContractError, VectorSearchPage, VectorSearchUnavailable,
)
from tests.semantic_exact_index_fixtures import published_text_fixture
from tests.test_semantic_exact_index_equivalence import _page_oracle

TEST_CAPABILITIES = ("inference",)
pytestmark = pytest.mark.capability("inference")


@pytest.mark.parametrize("backend_name", ("native", "persisted"))
@pytest.mark.parametrize("evidence_mode", (False, True))
@pytest.mark.parametrize("case", ("full", "partial", "cursor", "scope", "diagnostics"))
def test_real_backends_preserve_exact_page_and_all_request_constraints(
    tmp_path: Path, backend_name: str, evidence_mode: bool, case: str,
) -> None:
    fixture = published_text_fixture(tmp_path, rows=24, with_titles=True)
    query = replace(fixture.query, indexed_model_signatures=(fixture.model.model_signature,))
    # Build for 'all'; selecting content deliberately exercises the pre-scan
    # fallback, while the ordinary, partial and cursor requests use the index.
    handle = prepare_exact_index(
        fixture.database, tmp_path / "index", model_signature=fixture.model.model_signature,
        text_scope="all",
    )
    backend = repository.NativeExactVectorSearch() if backend_name == "native" else PersistedExactVectorSearch(handle)
    search = repository.search_exact_evidence_page if evidence_mode else repository.search_exact_page
    arguments = {
        "limit": 4, "max_vectors": 8 if case == "partial" else 24,
        "after_ref_id": 8 if case == "cursor" else 0, "batch_size": 8,
        "text_scope": "content" if case == "scope" else "all",
        "diagnostic_item_ids": (fixture.item_ids[0],) if case == "diagnostics" else (),
    }
    expected_diagnostics: dict[str, object] = {}
    expected = search(fixture.database, query, **arguments, diagnostics=expected_diagnostics)
    actual_diagnostics: dict[str, object] = {}
    boundary: dict[str, object] = {}
    try:
        actual = search(
            fixture.database, query, **arguments, vector_backend=backend,
            diagnostics=actual_diagnostics, backend_diagnostics=boundary,
        )
        assert _page_oracle(actual) == _page_oracle(expected)
        assert actual_diagnostics == expected_diagnostics
        assert boundary["coverage"] == ("partial" if case == "partial" else "complete")
        assert boundary["complete"] == actual.complete
        assert len(boundary["snapshot_id"]) == 64
        fallback = backend_name == "persisted" and case in {"scope", "diagnostics"}
        assert bool(boundary["fallback_reason"]) is fallback
        assert boundary["backend_id"] == ("persisted_exact" if backend_name == "persisted" and not fallback else "native_exact")
    finally:
        backend.close()
        handle.close()


def test_replacement_backend_receives_entire_query_and_budget(tmp_path: Path) -> None:
    fixture = published_text_fixture(tmp_path, rows=24)
    query = replace(fixture.query, indexed_model_signatures=(fixture.model.model_signature,) * 2)
    received = []
    class Recording:
        def search_page(self, request: Any, budget: Any, cancelled: Any = None) -> Any:
            received.append((request, budget, cancelled))
            return repository.NativeExactVectorSearch().search_page(request, budget, cancelled)
        def close(self) -> None:
            pass
    def callback() -> None:
        pass
    repository.search_exact_evidence_page(
        fixture.database, query, limit=3, max_vectors=8, batch_size=8,
        after_ref_id=8, text_scope="content", diagnostic_item_ids=(fixture.item_ids[0],),
        cancellation_check=callback, vector_backend=Recording(),
    )
    assert len(received) == 1
    request, budget, cancelled = received[0]
    assert request.query is query
    assert request.query.vector is query.vector
    assert request.query.indexed_model_signatures == (fixture.model.model_signature,) * 2
    assert request.owner_path == fixture.database
    assert request.after_ref_id == 8 and request.evidence_mode and request.text_scope == "content"
    assert request.metric == "cosine" and request.diagnostic_item_ids == (fixture.item_ids[0],)
    assert (budget.limit, budget.max_vectors, budget.batch_size) == (3, 8, 8)
    assert cancelled is callback


@pytest.mark.parametrize("corruption", ("snapshot", "order", "duplicate", "nan", "coverage", "cursor", "scan_budget", "model", "shape"))
def test_malformed_backend_response_abstains_without_native_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str,
) -> None:
    fixture = published_text_fixture(tmp_path, rows=24)
    page = repository.search_exact_page(fixture.database, fixture.query, limit=3)
    calls = []
    class Broken:
        def search_page(self, request: Any, budget: Any, cancelled: Any = None) -> Any:
            calls.append(request)
            altered = page
            if corruption == "order":
                altered = replace(page, hits=tuple(reversed(page.hits)))
            elif corruption == "duplicate":
                altered = replace(page, hits=(page.hits[0], page.hits[0]))
            elif corruption == "nan":
                altered = replace(page, hits=(replace(page.hits[0], score=float("nan")),))
            elif corruption == "cursor":
                altered = replace(page, complete=False, next_cursor=0)
            elif corruption == "scan_budget":
                altered = replace(page, scanned=budget.max_vectors + 1)
            elif corruption == "model":
                altered = replace(page, hits=(replace(page.hits[0], query_model_signature="other"),))
            elif corruption == "shape":
                return {"hits": [], "complete": True}
            return VectorSearchPage(
                altered, "broken", "old-snapshot" if corruption == "snapshot" else request.snapshot_id,
                "partial" if corruption == "coverage" else "complete",
            )
        def close(self) -> None:
            pass
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("malformed vector page retried against the native scan")
    monkeypatch.setattr(repository, "_scan_exact_page", forbidden)
    with pytest.raises(VectorSearchContractError):
        repository.search_exact_page(fixture.database, fixture.query, vector_backend=Broken())
    assert len(calls) == 1


def test_only_explicit_prescan_decline_falls_back_with_original_budget(tmp_path: Path) -> None:
    fixture = published_text_fixture(tmp_path, rows=24)
    class Unavailable:
        def search_page(self, request: Any, budget: Any, cancelled: Any = None) -> Any:
            return VectorSearchUnavailable("unavailable", "unsupported_filter")
        def close(self) -> None:
            pass
    metadata: dict[str, object] = {}
    page = repository.search_exact_page(
        fixture.database, fixture.query, vector_backend=Unavailable(), max_vectors=8,
        limit=3, batch_size=8, backend_diagnostics=metadata,
    )
    assert page.scanned == 8 and not page.complete and page.next_cursor is not None
    assert metadata["fallback_reason"] == "unsupported_filter" and metadata["coverage"] == "partial"


@pytest.mark.parametrize("failure", ("scan_exception", "decline_after_drift"))
def test_drift_or_query_exception_never_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    fixture = published_text_fixture(tmp_path, rows=24)
    class Changed:
        def search_page(self, request: Any, budget: Any, cancelled: Any = None) -> Any:
            if failure == "scan_exception":
                raise ExactIndexUnavailable("query_artifact_changed", "no implicit retry")
            info = fixture.database.stat()
            os.utime(fixture.database, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
            return VectorSearchUnavailable("changed", "stale")
        def close(self) -> None:
            pass
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("changed owner or artifact retried against the native scan")
    monkeypatch.setattr(repository, "_scan_exact_page", forbidden)
    with pytest.raises(repository.SemanticStateError, match="no implicit retry"):
        repository.search_exact_page(fixture.database, fixture.query, vector_backend=Changed())
