"""Product exact-index equivalence tests.

These tests exercise the public owner adapter and repository/service facades,
not the LAB codec directly.  The fixture is temporary and deterministic; no
encoder or external model is loaded.  A positive indexed query is guarded by
a spy that forbids entry to the native exact scan, while unsupported requests
must be reported by the handle as fallbacks and produce the same public
result as the native path.
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
from typing import Any

import pytest

from neocortex.semantic import semantic_search_repository as repository
from neocortex.semantic import semantic_service
from neocortex.semantic import semantic_exact_index_format as index_format
from neocortex.semantic.semantic_exact_index import (
    ExactIndexUnavailable,
    open_exact_index,
    prepare_exact_index,
)
from neocortex.semantic.semantic_models import (
    BackendEmbedding,
    EmbeddingModality,
    EmbeddingRequest,
)
from neocortex.semantic.semantic_state import resolve_search_hits

from tests.semantic_exact_index_fixtures import PublishedTextFixture, published_text_fixture


TEST_CAPABILITIES = ("inference",)
pytestmark = pytest.mark.capability("inference")


def _owner_files(database: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in database.parent.glob(f"{database.name}*")
        if path.is_file()
    }


def _page_oracle(page: Any) -> tuple[object, ...]:
    hits = []
    for hit in page.hits:
        modality = hit.modality.value if hasattr(hit.modality, "value") else hit.modality
        hits.append(
            (
                hit.ref_id,
                hit.entity_id,
                hit.item_id,
                hit.indexed_model_signature,
                hit.vector_space,
                modality,
                hit.score.hex(),
                hit.generation_id,
                json.dumps(
                    dict(hit.provenance),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                hit.query_model_signature,
            )
        )
    return tuple(hits), page.scanned, page.next_cursor, page.complete


def _assert_page_equal(reference: Any, candidate: Any) -> None:
    assert _page_oracle(reference) == _page_oracle(candidate)
    assert tuple(hit.score.hex() for hit in reference.hits) == tuple(
        hit.score.hex() for hit in candidate.hits
    )


def _search(
    fixture: PublishedTextFixture,
    *,
    exact_index: Any = None,
    evidence_mode: bool = False,
    limit: int = 24,
    max_vectors: int = 64,
    after_ref_id: int = 0,
    batch_size: int = 8,
    text_scope: str = "content",
    diagnostic_item_ids: tuple[str, ...] = (),
    diagnostics: dict[str, object] | None = None,
) -> Any:
    search = repository.search_exact_evidence_page if evidence_mode else repository.search_exact_page
    kwargs: dict[str, object] = {
        "limit": limit,
        "max_vectors": max_vectors,
        "after_ref_id": after_ref_id,
        "batch_size": batch_size,
        "text_scope": text_scope,
        "diagnostic_item_ids": diagnostic_item_ids,
        "diagnostics": diagnostics,
    }
    if exact_index is not None:
        kwargs["exact_index"] = exact_index
    return search(fixture.database, fixture.query, **kwargs)


def _build_and_open(
    fixture: PublishedTextFixture,
    directory: Path,
    *,
    text_scope: str = "content",
) -> Any:
    prepared = prepare_exact_index(
        fixture.database,
        directory,
        model_signature=fixture.model.model_signature,
        text_scope=text_scope,
        max_rows=500_000,
        max_total_bytes=4_000_000_000,
    )
    assert callable(prepared.summary) and callable(prepared.usage_summary)
    opened = open_exact_index(
        fixture.database,
        directory,
        max_rows=500_000,
        max_total_bytes=4_000_000_000,
    )
    return opened


def _assert_summary(handle: Any, fixture: PublishedTextFixture, *, text_scope: str) -> None:
    summary = handle.summary()
    assert summary["row_count"] == fixture.row_count
    assert summary["model_signature"] == fixture.model.model_signature
    assert summary.get("text_scope", summary.get("scope")) == text_scope
    usage = handle.usage_summary()
    for key in ("used_queries", "fallback_queries", "rows_scanned", "last_fallback_reason"):
        assert key in usage
    assert usage["used_queries"] == 0
    assert usage["fallback_queries"] == 0
    assert usage["rows_scanned"] == 0
    assert usage["last_fallback_reason"] in (None, "")


def _forbid_native_sql(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("eligible exact-index query entered native exact SQL")


@pytest.mark.parametrize(
    ("dtype", "evidence_mode"),
    (("float16", False), ("float32", False), ("float16", True), ("float32", True)),
)
def test_exact_index_full_page_and_resolved_equivalence_for_both_dtypes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dtype: str,
    evidence_mode: bool,
) -> None:
    fixture = published_text_fixture(tmp_path, rows=24, dtype=dtype)
    owner_before = _owner_files(fixture.database)
    handle = _build_and_open(fixture, tmp_path / "exact-index")
    _assert_summary(handle, fixture, text_scope="content")

    native = _search(fixture, evidence_mode=evidence_mode)
    with monkeypatch.context() as guarded:
        # The index dispatcher lives inside _search_exact_page itself.  Patch
        # the native SQL construction instead of the dispatcher, otherwise a
        # valid indexed query is rejected before it can reach the handle.
        guarded.setattr(repository, "_search_sql", _forbid_native_sql)
        indexed = _search(fixture, exact_index=handle, evidence_mode=evidence_mode)

    _assert_page_equal(native, indexed)
    native_resolved = resolve_search_hits(fixture.database, native.hits)
    indexed_resolved = resolve_search_hits(fixture.database, indexed.hits)
    assert indexed_resolved == native_resolved
    duplicate_hits = [
        hit for hit in indexed.hits if hit.item_id == "exact-index-duplicate"
    ]
    if evidence_mode:
        assert len(duplicate_hits) == 2
    else:
        assert len(duplicate_hits) == 1
        assert duplicate_hits[0].ref_id == max(
            hit.ref_id for hit in native.hits if hit.item_id == "exact-index-duplicate"
        )
    usage = handle.usage_summary()
    assert usage["used_queries"] == 1
    assert usage["fallback_queries"] == 0
    assert usage["rows_scanned"] == indexed.scanned
    assert usage["last_fallback_reason"] in (None, "")
    assert _owner_files(fixture.database) == owner_before


def test_exact_index_content_scope_and_title_all_fallback_are_observable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = published_text_fixture(tmp_path, rows=24, dtype="float16", with_titles=True)
    handle = _build_and_open(fixture, tmp_path / "exact-index-content", text_scope="content")

    # Four of the 24 rows are titles. Two ten-row numeric batches avoid the
    # intentionally unsupported four-row scalar tail of a batch_size=8 scan.
    native_content = _search(fixture, text_scope="content", batch_size=10)
    with monkeypatch.context() as guarded:
        guarded.setattr(repository, "_search_sql", _forbid_native_sql)
        indexed_content = _search(
            fixture, exact_index=handle, text_scope="content", batch_size=10,
        )
    _assert_page_equal(native_content, indexed_content)
    assert handle.usage_summary()["used_queries"] == 1
    assert handle.usage_summary()["fallback_queries"] == 0

    for scope in ("all", "title"):
        native = _search(fixture, text_scope=scope)
        indexed = _search(fixture, exact_index=handle, text_scope=scope)
        _assert_page_equal(native, indexed)
    usage = handle.usage_summary()
    assert usage["fallback_queries"] == 2
    assert usage["last_fallback_reason"]


def test_exact_index_cursor_effective_tail_and_small_batch_match_legacy_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = published_text_fixture(tmp_path, rows=24)
    handle = _build_and_open(fixture, tmp_path / "exact-index-pages")

    native_small = _search(fixture, limit=2, max_vectors=1, batch_size=1)
    indexed_small = _search(
        fixture, exact_index=handle, limit=2, max_vectors=1, batch_size=1
    )
    _assert_page_equal(native_small, indexed_small)

    native_first = _search(fixture, limit=3, max_vectors=8, batch_size=8)
    with monkeypatch.context() as guarded:
        guarded.setattr(repository, "_search_sql", _forbid_native_sql)
        indexed_first = _search(
            fixture, exact_index=handle, limit=3, max_vectors=8, batch_size=8
        )
    _assert_page_equal(native_first, indexed_first)
    assert indexed_first.complete is False and indexed_first.next_cursor is not None

    native_second = _search(
        fixture,
        limit=3,
        max_vectors=8,
        batch_size=8,
        after_ref_id=native_first.next_cursor,
    )
    with monkeypatch.context() as guarded:
        guarded.setattr(repository, "_search_sql", _forbid_native_sql)
        indexed_second = _search(
            fixture,
            exact_index=handle,
            limit=3,
            max_vectors=8,
            batch_size=8,
            after_ref_id=indexed_first.next_cursor,
        )
    _assert_page_equal(native_second, indexed_second)

    native_tail = _search(fixture, limit=3, max_vectors=8, batch_size=8, after_ref_id=23)
    indexed_tail = _search(
        fixture,
        exact_index=handle,
        limit=3,
        max_vectors=8,
        batch_size=8,
        after_ref_id=23,
    )
    _assert_page_equal(native_tail, indexed_tail)

    native_wide_batch = _search(fixture, limit=3, max_vectors=8, batch_size=513)
    indexed_wide_batch = _search(
        fixture, exact_index=handle, limit=3, max_vectors=8, batch_size=513
    )
    _assert_page_equal(native_wide_batch, indexed_wide_batch)
    assert handle.usage_summary()["fallback_queries"] >= 3


def test_exact_index_diagnostics_are_explicit_fallbacks_not_fake_index_passes(
    tmp_path: Path,
) -> None:
    fixture = published_text_fixture(tmp_path, rows=24)
    handle = _build_and_open(fixture, tmp_path / "exact-index-diagnostics")
    diagnostics_native: dict[str, object] = {}
    diagnostics_indexed: dict[str, object] = {}
    target = (fixture.item_ids[0],)
    native = _search(
        fixture,
        limit=3,
        max_vectors=64,
        diagnostic_item_ids=target,
        diagnostics=diagnostics_native,
    )
    indexed = _search(
        fixture,
        exact_index=handle,
        limit=3,
        max_vectors=64,
        diagnostic_item_ids=target,
        diagnostics=diagnostics_indexed,
    )
    _assert_page_equal(native, indexed)
    assert diagnostics_indexed == diagnostics_native
    usage = handle.usage_summary()
    assert usage["used_queries"] == 0
    assert usage["fallback_queries"] == 1
    assert usage["last_fallback_reason"]


def test_exact_index_image_and_repeated_signature_requests_fallback_like_native(
    tmp_path: Path,
) -> None:
    fixture = published_text_fixture(tmp_path, rows=24)
    handle = _build_and_open(fixture, tmp_path / "exact-index-unsupported")
    image_query = replace(fixture.query, target_modality=EmbeddingModality.IMAGE)
    repeated_query = replace(
        fixture.query,
        indexed_model_signatures=(
            fixture.model.model_signature,
            fixture.model.model_signature,
        ),
    )

    def outcome(query: Any, *, exact_index: Any = None) -> tuple[object, ...]:
        try:
            kwargs: dict[str, object] = {"limit": 3, "max_vectors": 64, "batch_size": 8}
            if exact_index is not None:
                kwargs["exact_index"] = exact_index
            page = repository.search_exact_page(fixture.database, query, **kwargs)
            return ("ok", _page_oracle(page))
        except Exception as exc:  # public fallback must preserve native outcome
            return ("error", type(exc).__name__, str(exc))

    assert outcome(image_query, exact_index=handle) == outcome(image_query)
    assert outcome(repeated_query, exact_index=handle) == outcome(repeated_query)
    usage = handle.usage_summary()
    assert usage["used_queries"] == 0
    # The image query fails owner model validation before the handle's
    # fallback counter is reached; only the repeated-signature request reaches
    # the explicit published-pairs fallback boundary.
    assert usage["fallback_queries"] == 1


def test_exact_index_mid_query_file_drift_abstains_without_native_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = published_text_fixture(tmp_path, rows=24)
    directory = tmp_path / "exact-index-drift"
    handle = _build_and_open(fixture, directory)
    rows_path = directory / "rows.bin"
    original = rows_path.stat()
    mutated = False
    original_scores = index_format._numeric_scores

    def drift_during_scoring(*args: object, **kwargs: object) -> Any:
        nonlocal mutated
        result = original_scores(*args, **kwargs)
        if not mutated:
            mutated = True
            os.utime(rows_path, ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000))
        return result

    monkeypatch.setattr(index_format, "_numeric_scores", drift_during_scoring)
    monkeypatch.setattr(repository, "_search_sql", _forbid_native_sql)
    try:
        with pytest.raises(ExactIndexUnavailable):
            _search(fixture, exact_index=handle, limit=3, max_vectors=64, batch_size=8)
    finally:
        os.utime(rows_path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert mutated is True


class _ConstantFixtureBackend:
    def __init__(self, model: Any) -> None:
        self.model = model
        self.requests: list[EmbeddingRequest] = []

    @property
    def max_batch_size(self) -> int:
        return 16

    def embed(self, requests: tuple[EmbeddingRequest, ...] | list[EmbeddingRequest]) -> tuple[BackendEmbedding, ...]:
        self.requests.extend(requests)
        return tuple(
            BackendEmbedding(
                request_id=request.request_id,
                vector=(1.0,) + (0.0,) * (self.model.dimensions - 1),
                provenance={"backend": "exact-index-equivalence-fixture"},
            )
            for request in requests
        )

    def text_token_counts(self, texts: tuple[str, ...] | list[str]) -> tuple[tuple[int, ...], int]:
        return tuple(len(text.split()) + 2 for text in texts), 512

    def text_tokenizer_contract(self) -> tuple[str, int]:
        return "exact-index-equivalence-fixture-tokenizer-v1", 512


@pytest.mark.parametrize("evidence_mode", (False, True))
def test_exact_index_public_semantic_service_facade_matches_native_dtos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence_mode: bool,
) -> None:
    fixture = published_text_fixture(tmp_path, rows=24)
    handle = _build_and_open(fixture, tmp_path / "exact-index-service")
    backend = _ConstantFixtureBackend(fixture.model)
    monkeypatch.setattr(semantic_service, "_backend", lambda model, **_kwargs: backend)
    common: dict[str, object] = {
        "state_directory": tmp_path,
        "query": "exact index fixture owner evidence",
        "limit": 5,
        "max_vectors": 64,
        "include_text": True,
        "include_title": False,
        "include_images": False,
        "include_lexical": False,
        "semantic_database": fixture.database,
        "text_model": fixture.model,
        "local_files_only": True,
        "evidence_mode": evidence_mode,
    }
    native = semantic_service.search_semantic_index(**common)
    with monkeypatch.context() as guarded:
        guarded.setattr(repository, "_search_sql", _forbid_native_sql)
        indexed = semantic_service.search_semantic_index(
            **common,
            exact_index=handle,
        )
    assert indexed.rankings == native.rankings
    assert indexed.fused == native.fused
    assert indexed.complete == native.complete
    usage = handle.usage_summary()
    assert usage["used_queries"] == 1
    assert usage["fallback_queries"] == 0


__all__ = ["TEST_CAPABILITIES"]
