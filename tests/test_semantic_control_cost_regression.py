"""Constant-cost base extent checks without weakening clone publication guards."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.semantic import semantic_generation_repository as repository
from neocortex.semantic.semantic_models import EmbeddingModelSpec, ExactSearchQuery
from neocortex.semantic.semantic_state import (
    SemanticStateError,
    enqueue_text_chunk_jobs,
    finalize_embedding_generation,
    prepare_embedding_generation,
    register_embedding_model,
    search_exact_page,
    semantic_database,
    start_embedding_generation,
)
from neocortex.semantic.semantic_work_budget import (
    SemanticIndexDeadlineExceeded,
    SemanticWorkBudget,
)
from tests.test_semantic_generation_publication_v6 import (
    _complete_jobs,
    _initialize,
    _stage,
)


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")
_MEMBER_COUNT = 21


def _published_base(path: Path) -> tuple[EmbeddingModelSpec, int, int]:
    """Use real chunk/revision/payload references, never duplicate fake members."""

    model = _initialize(path)
    chunks = tuple(
        _stage(path, f"document-{index}", f"transformer inspection record {index}", 1)
        for index in range(_MEMBER_COUNT)
    )
    baseline = start_embedding_generation(
        path,
        model_signature=model.model_signature,
        processing_signature="control-cost-base-v1",
        provenance={"fixture": "control-cost-base"},
        started_ns=100,
    )
    enqueue_text_chunk_jobs(path, baseline, tuple(chunk.chunk_id for chunk in chunks), now_ns=101)
    _complete_jobs(path, baseline, now_ns=102)
    finalize_embedding_generation(path, baseline, completed_ns=150)
    candidate = start_embedding_generation(
        path,
        model_signature=model.model_signature,
        processing_signature="control-cost-successor-v1",
        provenance={"fixture": "control-cost-successor"},
        materialize_base=False,
        started_ns=160,
    )
    return model, baseline, candidate


@contextmanager
def _clone_costs(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, int]]:
    original = repository.semantic_database
    counts = {"extent": 0, "contract": 0, "pages": 0}

    def observe(statement: str) -> None:
        sql = " ".join(statement.lower().split())
        if sql.startswith(
            "select coalesce(max(member_id),0),count(*) from embedding_generation_members"
        ):
            counts["extent"] += 1
        elif sql.startswith("select status,model_signature from embedding_generations"):
            counts["contract"] += 1
        elif "as has_target_job from embedding_generation_members base_member" in sql:
            counts["pages"] += 1

    @contextmanager
    def traced(path: Path, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        with original(path, readonly=readonly) as connection:
            connection.set_trace_callback(observe)
            try:
                yield connection
            finally:
                connection.set_trace_callback(None)

    with monkeypatch.context() as scoped:
        scoped.setattr(repository, "semantic_database", traced)
        yield counts


def _head(path: Path) -> int:
    with semantic_database(path, readonly=True) as connection:
        return int(
            connection.execute("SELECT generation_id FROM published_embedding_heads").fetchone()[0]
        )


def _published_bytes(path: Path, baseline: int) -> tuple[tuple[tuple[object, ...], ...], ...]:
    with semantic_database(path, readonly=True) as connection:
        return (
            tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM embedding_generation_members WHERE generation_id=? ORDER BY member_id",
                    (baseline,),
                )
            ),
            tuple(
                tuple(row)
                for row in connection.execute("SELECT * FROM vector_payloads ORDER BY payload_id")
            ),
        )


@pytest.mark.parametrize("page_size", (1, 4, 7))
def test_base_extent_cost_is_constant_across_pages_and_preserves_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_size: int,
) -> None:
    path = tmp_path / "semantic.sqlite3"
    model, baseline, candidate = _published_base(path)
    before = _published_bytes(path, baseline)
    query = ExactSearchQuery(
        model.model_signature,
        model.vector_space,
        model.dimensions,
        (1.0, 0.0, 0.0, 0.0),
        model.modality,
    )
    old_page = search_exact_page(path, query, limit=_MEMBER_COUNT, max_vectors=_MEMBER_COUNT)
    monkeypatch.setattr(repository, "MAX_WRITE_BATCH", page_size)

    with _clone_costs(monkeypatch) as counts:
        assert prepare_embedding_generation(path, candidate, enumeration_complete=True) is None

    expected_pages = (_MEMBER_COUNT + page_size - 1) // page_size + 1
    assert counts == {"extent": 2, "contract": expected_pages, "pages": expected_pages}
    assert _head(path) == baseline
    assert _published_bytes(path, baseline) == before
    with semantic_database(path, readonly=True) as connection:
        generation = connection.execute(
            "SELECT base_clone_complete,cursor_json FROM embedding_generations WHERE generation_id=?",
            (candidate,),
        ).fetchone()
        assert generation is not None and generation["base_clone_complete"] == 1
        assert (
            json.loads(generation["cursor_json"])["base_clone"]["scanned_members"] == _MEMBER_COUNT
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM embedding_generation_members WHERE generation_id=?",
                (candidate,),
            ).fetchone()[0]
            == _MEMBER_COUNT
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    # A completed clone cannot manufacture another copy or another receipt page.
    with _clone_costs(monkeypatch) as repeated:
        assert prepare_embedding_generation(path, candidate, enumeration_complete=True) is None
    assert repeated == {"extent": 0, "contract": 0, "pages": 0}
    assert finalize_embedding_generation(path, candidate, completed_ns=200).status == "ready"
    assert _head(path) == candidate
    assert _published_bytes(path, baseline) == before
    new_page = search_exact_page(path, query, limit=_MEMBER_COUNT, max_vectors=_MEMBER_COUNT)
    assert new_page.complete and new_page.scanned == old_page.scanned == _MEMBER_COUNT
    assert [(hit.item_id, hit.entity_id, hit.score) for hit in new_page.hits] == [
        (hit.item_id, hit.entity_id, hit.score) for hit in old_page.hits
    ]


def test_resume_revalidates_extent_once_then_finishes_without_duplicate_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "semantic.sqlite3"
    _model, baseline, candidate = _published_base(path)
    monkeypatch.setattr(repository, "MAX_WRITE_BATCH", 3)
    ticks = iter((0.0, 2.0))
    budget = SemanticWorkBudget(deadline=1.0, _clock=lambda: next(ticks))
    with _clone_costs(monkeypatch) as paused:
        with pytest.raises(SemanticIndexDeadlineExceeded):
            prepare_embedding_generation(
                path, candidate, enumeration_complete=True, work_budget=budget
            )
    assert paused == {"extent": 1, "contract": 1, "pages": 1}
    assert _head(path) == baseline

    with _clone_costs(monkeypatch) as resumed:
        assert prepare_embedding_generation(path, candidate, enumeration_complete=True) is None
    assert resumed == {"extent": 2, "contract": 7, "pages": 7}
    with semantic_database(path, readonly=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*),COUNT(DISTINCT base_member_id) FROM embedding_generation_members WHERE generation_id=?",
            (candidate,),
        ).fetchone()[:] == (_MEMBER_COUNT, _MEMBER_COUNT)
    assert _head(path) == baseline


def test_extent_drift_midclone_is_rejected_before_publication_and_on_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "semantic.sqlite3"
    _model, baseline, candidate = _published_base(path)
    monkeypatch.setattr(repository, "MAX_WRITE_BATCH", 3)
    original = repository._persist_base_clone_page
    changed = False

    def change_extent(
        connection: sqlite3.Connection,
        generation_id: int,
        cursor: dict[str, object],
        clone_cursor: dict[str, object],
        rows: Sequence[sqlite3.Row],
    ) -> None:
        nonlocal changed
        original(connection, generation_id, cursor, clone_cursor, rows)
        if not changed:
            connection.execute(
                "DELETE FROM embedding_generation_members WHERE member_id=(SELECT MAX(member_id) FROM embedding_generation_members WHERE generation_id=?)",
                (baseline,),
            )
            changed = True

    monkeypatch.setattr(repository, "_persist_base_clone_page", change_extent)
    with pytest.raises(SemanticStateError, match="base snapshot changed during resumable clone"):
        prepare_embedding_generation(path, candidate, enumeration_complete=True)
    assert changed and _head(path) == baseline
    with pytest.raises(SemanticStateError, match="base snapshot is not fully cloned"):
        finalize_embedding_generation(path, candidate, completed_ns=200)
    with _clone_costs(monkeypatch) as resumed:
        with pytest.raises(
            SemanticStateError, match="base snapshot changed during resumable clone"
        ):
            prepare_embedding_generation(path, candidate, enumeration_complete=True)
    assert resumed == {"extent": 1, "contract": 1, "pages": 0}
    assert _head(path) == baseline


@pytest.mark.parametrize("contract_change", ("status", "model"))
def test_base_contract_is_revalidated_on_every_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contract_change: str,
) -> None:
    path = tmp_path / "semantic.sqlite3"
    model, baseline, candidate = _published_base(path)
    alternate = replace(model, model_signature="alternate-control-cost-model")
    register_embedding_model(path, alternate, allow_test_provider=True)
    monkeypatch.setattr(repository, "MAX_WRITE_BATCH", 3)
    original = repository._persist_base_clone_page

    def change_contract(
        connection: sqlite3.Connection,
        generation_id: int,
        cursor: dict[str, object],
        clone_cursor: dict[str, object],
        rows: Sequence[sqlite3.Row],
    ) -> None:
        original(connection, generation_id, cursor, clone_cursor, rows)
        if contract_change == "status":
            connection.execute(
                "UPDATE embedding_generations SET status='failed' WHERE generation_id=?",
                (baseline,),
            )
        else:
            connection.execute(
                "UPDATE embedding_generations SET model_signature=? WHERE generation_id=?",
                (alternate.model_signature, baseline),
            )

    monkeypatch.setattr(repository, "_persist_base_clone_page", change_contract)
    with _clone_costs(monkeypatch) as counts:
        with pytest.raises(SemanticStateError, match=r"not immutable-ready|model differs"):
            prepare_embedding_generation(path, candidate, enumeration_complete=True)
    assert counts == {"extent": 1, "contract": 2, "pages": 1}
    assert _head(path) == baseline
    with semantic_database(path, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT base_clone_complete FROM embedding_generations WHERE generation_id=?",
                (candidate,),
            ).fetchone()[0]
            == 0
        )
