"""Focused recovery coverage for an interrupted Semantic-to-Code projection."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

from neocortex.code.code_contracts import CodeRouteConfig
from neocortex.code.code_route import CodeRoute
from neocortex.code.search.code_semantic_links import (
    CodeSemanticLinkError,
    deactivate_stale_code_embedding_links,
)
from neocortex.deduplication import FileSnapshot
from neocortex.semantic.semantic_generation_repository import (
    finalize_embedding_generation,
    prepare_embedding_generation,
    start_embedding_generation,
)
from neocortex.semantic.semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRole,
)
from neocortex.semantic.semantic_publication_heads import (
    PublicationHeadsRepairRequired,
    observe_integrated_owner_heads,
    observe_semantic_generation_heads,
)
from neocortex.semantic.semantic_schema import initialize_semantic_state
from neocortex.semantic.semantic_state import register_embedding_model


def _snapshot(path: Path) -> FileSnapshot:
    metadata = path.stat()
    return FileSnapshot(
        str(path),
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        getattr(metadata, "st_birthtime_ns", metadata.st_ctime_ns),
    )


class _Inventory:
    def __init__(self, paths: Iterable[Path]) -> None:
        self.paths = tuple(paths)

    def snapshots(self, scan_id: int) -> Iterable[FileSnapshot]:
        del scan_id
        return (_snapshot(path) for path in self.paths)


class _Framework:
    def begin_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        *,
        source_run_id: int | None = None,
    ) -> None:
        del run_id, route_name, phase_name, source_run_id

    def complete_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        summary: Mapping[str, object] | None = None,
    ) -> None:
        del run_id, route_name, phase_name, summary

    def fail_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        exc: BaseException,
    ) -> None:
        del run_id, route_name, phase_name
        raise AssertionError("Code fixture route failed") from exc


def _model() -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        "next-all-repair-model",
        "next-all-repair-space",
        EmbeddingModality.TEXT,
        "fixture/next-all-repair",
        "1",
        4,
        "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )


def _publish_empty_generation(
    database: Path,
    model: EmbeddingModelSpec,
    *,
    processing_signature: str,
    started_ns: int,
) -> int:
    generation_id = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature=processing_signature,
        started_ns=started_ns,
    )
    assert prepare_embedding_generation(
        database,
        generation_id,
        enumeration_complete=True,
    ) is None
    assert (
        finalize_embedding_generation(
            database,
            generation_id,
            completed_ns=started_ns + 10,
        ).status
        == "ready"
    )
    return generation_id


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, EmbeddingModelSpec, int, int]:
    state = tmp_path / "state"
    source = tmp_path / "fixture.py"
    source.write_text("def relay():\n    return 1\n", encoding="utf-8")
    code_database = state / "code.sqlite3"
    CodeRoute(
        CodeRouteConfig(
            state_path=code_database,
            dedup_path=state / "dedup.sqlite3",
        ),
        _Inventory((source,)),
        _Framework(),
        1,
        1,
    ).run()

    semantic_database = state / "semantic.sqlite3"
    initialize_semantic_state(semantic_database)
    model = _model()
    register_embedding_model(semantic_database, model, allow_test_provider=True)
    first_generation = _publish_empty_generation(
        semantic_database,
        model,
        processing_signature="next-all-v1",
        started_ns=100,
    )
    with sqlite3.connect(code_database) as connection:
        chunk_id = int(connection.execute("SELECT chunk_id FROM code_chunks LIMIT 1").fetchone()[0])
    return state, source, code_database, model, first_generation, chunk_id


def _insert_link(
    database: Path,
    *,
    chunk_id: int,
    model: EmbeddingModelSpec,
    generation_id: int,
    item_id: str = "item:next-all",
    active: int = 1,
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO embedding_links(
                chunk_id,semantic_item_id,model_signature,vector_space,
                generation_id,active,provenance_json)
            VALUES(?,?,?,?,?,?,?)""",
            (
                chunk_id,
                item_id,
                model.model_signature,
                model.vector_space,
                generation_id,
                active,
                '{"fixture":"next-all"}',
            ),
        )


def _link_rows(database: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                """SELECT chunk_id,semantic_item_id,model_signature,vector_space,
                    generation_id,active,provenance_json
                FROM embedding_links ORDER BY chunk_id,generation_id"""
            )
        ]


def test_head_advance_gap_is_repaired_without_deleting_code_state(tmp_path: Path) -> None:
    state, _source, code_database, model, first_generation, chunk_id = _fixture(tmp_path)
    _insert_link(
        code_database,
        chunk_id=chunk_id,
        model=model,
        generation_id=first_generation,
    )
    second_generation = _publish_empty_generation(
        state / "semantic.sqlite3",
        model,
        processing_signature="next-all-v2",
        started_ns=200,
    )

    with pytest.raises(PublicationHeadsRepairRequired, match="published Semantic head"):
        observe_integrated_owner_heads(state, include_code=True)
    with sqlite3.connect(code_database) as connection:
        before_counts = tuple(
            connection.execute(
                "SELECT COUNT(*) FROM code_chunks"
            ).fetchone()
        )
        before_generations = tuple(
            connection.execute(
                "SELECT COUNT(*) FROM graph_generations"
            ).fetchone()
        )

    changed = deactivate_stale_code_embedding_links(
        state,
        published_heads=((model.model_signature, second_generation),),
    )

    assert changed == 1
    assert observe_integrated_owner_heads(state, include_code=True)
    assert _link_rows(code_database)[0][5] == 0
    with sqlite3.connect(code_database) as connection:
        assert tuple(connection.execute("SELECT COUNT(*) FROM code_chunks").fetchone()) == before_counts
        assert tuple(connection.execute("SELECT COUNT(*) FROM graph_generations").fetchone()) == before_generations


def test_current_chunk_change_is_repaired_while_historical_rows_remain(
    tmp_path: Path,
) -> None:
    state, source, code_database, model, generation, chunk_id = _fixture(tmp_path)
    _insert_link(
        code_database,
        chunk_id=chunk_id,
        model=model,
        generation_id=generation,
    )
    with sqlite3.connect(code_database) as connection:
        original_chunk_count = int(
            connection.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0]
        )
    time.sleep(0.002)
    source.write_text("def relay():\n    return 2\n", encoding="utf-8")
    os.utime(source, ns=(time.time_ns(), time.time_ns()))
    CodeRoute(
        CodeRouteConfig(
            state_path=code_database,
            dedup_path=state / "dedup.sqlite3",
        ),
        _Inventory((source,)),
        _Framework(),
        2,
        2,
    ).run()

    with pytest.raises(PublicationHeadsRepairRequired, match="current Code chunk"):
        observe_integrated_owner_heads(state, include_code=True)
    assert (
        deactivate_stale_code_embedding_links(
            state,
            published_heads=((model.model_signature, generation),),
        )
        == 1
    )
    assert _link_rows(code_database)[0][5] == 0
    with sqlite3.connect(code_database) as connection:
        assert int(connection.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0]) >= original_chunk_count
        assert int(connection.execute("SELECT COUNT(*) FROM file_versions").fetchone()[0]) >= 2


def test_repair_preserves_valid_links_and_only_deactivates_stale_projection(
    tmp_path: Path,
) -> None:
    state, _source, code_database, model, first_generation, chunk_id = _fixture(tmp_path)
    second_generation = _publish_empty_generation(
        state / "semantic.sqlite3",
        model,
        processing_signature="next-all-v2",
        started_ns=200,
    )
    _insert_link(
        code_database,
        chunk_id=chunk_id,
        model=model,
        generation_id=first_generation,
        item_id="item:stale",
    )
    _insert_link(
        code_database,
        chunk_id=chunk_id,
        model=model,
        generation_id=second_generation,
        item_id="item:valid",
    )
    before = _link_rows(code_database)
    valid_before = next(row for row in before if row[1] == "item:valid")

    assert (
        deactivate_stale_code_embedding_links(
            state,
            published_heads=observe_semantic_generation_heads(state),
        )
        == 1
    )
    after = _link_rows(code_database)
    valid_after = next(row for row in after if row[1] == "item:valid")
    stale_after = next(row for row in after if row[1] == "item:stale")
    assert valid_after == valid_before
    assert stale_after[5] == 0


def test_repair_cancellation_rolls_back_the_update(tmp_path: Path) -> None:
    state, _source, code_database, model, _first_generation, _chunk_id = _fixture(tmp_path)
    second_generation = _publish_empty_generation(
        state / "semantic.sqlite3",
        model,
        processing_signature="next-all-v2",
        started_ns=200,
    )
    with sqlite3.connect(code_database) as connection:
        version_id = int(
            connection.execute(
                "SELECT version_id FROM file_versions ORDER BY version_id LIMIT 1"
            ).fetchone()[0]
        )
        connection.executemany(
            """INSERT INTO code_chunks(
                chunk_id,version_id,symbol_id,chunk_index,kind,start_line,end_line,
                start_byte,end_byte,text,text_xxh3_128)
            VALUES(?, ?, NULL, ?, 'function', 1, 1, 0, 1, 'x', ?)""",
            (
                (10_000 + offset, version_id, 10_000 + offset, "a" * 32)
                for offset in range(1_000)
            ),
        )
        connection.executemany(
            """INSERT INTO embedding_links(
                chunk_id,semantic_item_id,model_signature,vector_space,
                generation_id,active,provenance_json)
            VALUES(?,?,?,?,1,1,'{}')""",
            (
                (10_000 + offset, f"item:{offset}", model.model_signature, model.vector_space)
                for offset in range(1_000)
            ),
        )
    before = _link_rows(code_database)
    calls = 0

    def cancel_during_sql() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 25

    with pytest.raises(CodeSemanticLinkError, match="cancelled"):
        deactivate_stale_code_embedding_links(
            state,
            published_heads=((model.model_signature, second_generation),),
            cancellation_check=cancel_during_sql,
        )
    assert calls >= 25
    assert _link_rows(code_database) == before
