"""The singleton exact scan must not sort wide rows or scan old generations."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from neocortex.semantic import semantic_search_repository as repository
from neocortex.semantic.semantic_models import EmbeddingModality, ExactSearchQuery
from neocortex.semantic.semantic_schema import semantic_database
from tests.test_retrieval_target_diagnostics import _published_fixture


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _add_unpublished_history(database: Path, copies: int) -> None:
    """Search-only cost fixture; never claims full lineage/restore equivalence."""
    with semantic_database(database) as connection:
        head = connection.execute(
            "SELECT generation_id FROM published_embedding_heads",
        ).fetchone()[0]
        generation_columns = [
            str(row[1]) for row in connection.execute("PRAGMA table_info(embedding_generations)")
            if row[1] != "generation_id"
        ]
        member_columns = [
            str(row[1]) for row in connection.execute("PRAGMA table_info(embedding_generation_members)")
            if row[1] != "member_id"
        ]
        generations = ",".join(generation_columns)
        members = ",".join(member_columns)
        selected_members = ",".join(
            "?" if column == "generation_id" else column for column in member_columns
        )
        connection.execute("BEGIN IMMEDIATE")
        for _ in range(copies):
            generation = connection.execute(
                f"INSERT INTO embedding_generations({generations}) "
                f"SELECT {generations} FROM embedding_generations WHERE generation_id=? "
                "RETURNING generation_id",
                (head,),
            ).fetchone()[0]
            connection.execute(
                f"INSERT INTO embedding_generation_members({members}) "
                f"SELECT {selected_members} FROM embedding_generation_members WHERE generation_id=?",
                (generation, head),
            )
        connection.commit()
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_singleton_exact_plan_is_ordered_and_history_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _published_fixture(database)
    query = ExactSearchQuery(
        query_model_signature=model.model_signature,
        vector_space=model.vector_space,
        dimensions=model.dimensions,
        vector=(1.0, 0.0, 0.0, 0.0),
        target_modality=EmbeddingModality.TEXT,
        indexed_model_signatures=(model.model_signature,),
    )
    original_database = repository.semantic_database
    steps = 0

    def progress() -> int:
        nonlocal steps
        steps += 1
        return 0

    @contextmanager
    def traced(*args, **kwargs):
        with original_database(*args, **kwargs) as connection:
            connection.set_progress_handler(progress, 1)
            try:
                yield connection
            finally:
                connection.set_progress_handler(None, 0)

    monkeypatch.setattr(repository, "semantic_database", traced)
    before = repository.search_exact_page(database, query, max_vectors=100, batch_size=2)
    before_steps = steps
    _add_unpublished_history(database, 300)
    steps = 0
    after = repository.search_exact_page(database, query, max_vectors=100, batch_size=2)
    assert after == before
    assert after.complete and after.scanned == 7
    assert 0 < steps <= before_steps + 50

    with semantic_database(database, readonly=True) as connection:
        head = connection.execute("SELECT generation_id FROM published_embedding_heads").fetchone()[0]
        plan = [str(row[-1]) for row in connection.execute(
            "EXPLAIN QUERY PLAN " + repository._search_sql(EmbeddingModality.TEXT, 1),
            (model.model_signature, head, 0, 101),
        )]
    assert any("embedding_generation_members_search_idx" in detail for detail in plan)
    assert not any("TEMP B-TREE" in detail for detail in plan)
