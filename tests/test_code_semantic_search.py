"""Optional semantic-code search integration without downloading a model."""
# region [00] Contexto del módulo
# Módulo: tests/test_code_semantic_search.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import inspect
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable, Mapping, Sequence

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.code.search import code_search as code_search_implementation
from neocortex.semantic import semantic_service
from neocortex.semantic import semantic_search_service as semantic_search_implementation
from neocortex.code.code_contracts import CodeRouteConfig, CodeSearchQuery
from neocortex.code.code_route import CodeRoute
from neocortex.code.code_schema import readonly_code_database
from neocortex.code.search.code_search import search_code
from neocortex.code.search.code_semantic_links import (
    CodeSemanticLinkError,
    code_semantic_search_availability,
    synchronize_code_embedding_links,
)
from neocortex.semantic.semantic_models import (
    BackendEmbedding,
    EmbeddingModelSpec,
    EmbeddingRequest,
)
from neocortex.persistence.sqlite_cancellation import SQLiteCancellationBridge
# endregion [01]

# region [02] Implementación


def _snapshot(path: Path) -> FileSnapshot:
    observed = path.stat()
    return FileSnapshot(
        str(path),
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
        getattr(observed, "st_birthtime_ns", observed.st_ctime_ns),
    )


class _Inventory:
    def __init__(self, paths: Iterable[Path]):
        self.paths = tuple(paths)

    def snapshots(self, scan_id: int):
        del scan_id
        return iter(_snapshot(path) for path in self.paths)


class _FrameworkState:
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
        del run_id, route_name, phase_name
        assert summary is not None

    def fail_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        exc: BaseException,
    ) -> None:
        del run_id, route_name, phase_name, exc
        raise AssertionError("semantic search fixture route must not fail")


class _ConstantBackend:
    def __init__(self, model: EmbeddingModelSpec) -> None:
        self.model = model

    @property
    def max_batch_size(self) -> int:
        return 16

    def embed(
        self,
        requests: Sequence[EmbeddingRequest],
    ) -> Sequence[BackendEmbedding]:
        vector = (1.0,) + (0.0,) * (self.model.dimensions - 1)
        return tuple(
            BackendEmbedding(
                request_id=request.request_id,
                vector=vector,
                provenance={"backend": "code-cancellation-fixture"},
            )
            for request in requests
        )

    def text_token_counts(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[int, ...], int]:
        return tuple(len(text.split()) + 2 for text in texts), 512

    def text_tokenizer_contract(self) -> tuple[str, int]:
        return "code-semantic-fixture-tokenizer-v1", 512


def test_semantic_rows_abstain_before_search_without_prerequisites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert str(inspect.signature(code_search_implementation._semantic_rows)) == (
        "(code_database: 'Path', connection: 'sqlite3.Connection', query: "
        "'CodeSearchQuery', fetch_limit: 'int', cancellation: "
        "'SQLiteCancellationBridge', *, model_cache: 'Path | None', threads: "
        "'int | None', row_admission: 'Callable[[int], None] | None' = None) -> 'tuple[_SearchRow, ...]'"
    )
    source = tmp_path / "prerequisite.py"
    source.write_text("def guarded_search():\n    return 'ready'\n", encoding="utf-8")
    state_path = tmp_path / "state" / "code.sqlite3"
    CodeRoute(
        CodeRouteConfig(
            state_path=state_path,
            dedup_path=tmp_path / "state" / "dedup.sqlite3",
        ),
        _Inventory((source,)),
        _FrameworkState(),
        1,
        1,
    ).run()
    calls = 0

    def unexpected_search(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("semantic model search must remain gated")

    monkeypatch.setattr(
        semantic_service,
        "search_semantic_index",
        unexpected_search,
    )
    assert (
        search_code(
            state_path,
            CodeSearchQuery(text="guarded search", modes=("semantic",)),
        )
        == ()
    )

    (state_path.parent / "semantic.sqlite3").touch()
    assert search_code(state_path, CodeSearchQuery(modes=("semantic",))) == ()
    assert (
        search_code(
            state_path,
            CodeSearchQuery(text="guarded search", modes=("semantic",)),
        )
        == ()
    )
    assert calls == 0


def test_semantic_hits_resolve_to_current_rows_and_apply_filters(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "access.py"
    source_text = (
        "import sqlite3\n\n"
        + "\n".join(f"PREFIX_{index} = {index}" for index in range(160))
        + "\n\ndef validate_access(path):\n"
        + "    return sqlite3.connect(path).execute('PRAGMA quick_check').fetchone()\n"
    )
    source.write_text(source_text, encoding="utf-8")
    state_path = tmp_path / "state" / "code.sqlite3"
    config = CodeRouteConfig(
        state_path=state_path,
        dedup_path=tmp_path / "state" / "dedup.sqlite3",
        chunk_chars=1024,
    )
    CodeRoute(
        config,
        _Inventory((source,)),
        _FrameworkState(),
        1,
        1,
    ).run()
    with closing(sqlite3.connect(state_path)) as connection:
        target = connection.execute(
            """SELECT f.volume_id || ':' || f.physical_file_id,v.version_id,
            c.chunk_index,c.kind,c.start_line,c.end_line,s.qualified_name,c.chunk_id
            FROM files f JOIN file_versions v ON v.version_id=f.current_version_id
            JOIN code_chunks c ON c.version_id=v.version_id
            LEFT JOIN symbols s ON s.symbol_id=c.symbol_id
            WHERE c.chunk_index>0 ORDER BY c.chunk_index DESC LIMIT 1"""
        ).fetchone()
    assert target is not None
    source_identity = str(target[0])
    target_version_id = int(target[1])
    target_chunk_index = int(target[2])
    target_kind = str(target[3])
    target_start_line = int(target[4])
    target_end_line = int(target[5])
    target_symbol = None if target[6] is None else str(target[6])
    target_chunk_id = int(target[7])

    (state_path.parent / "semantic.sqlite3").touch()
    semantic_item_id = f"item:code:{source_identity}"
    with closing(sqlite3.connect(state_path)) as connection:
        connection.execute(
            """INSERT INTO embedding_links(
            chunk_id,semantic_item_id,model_signature,vector_space,generation_id,
            active,provenance_json) VALUES(?,?,?,?,?,1,'{}')""",
            (
                target_chunk_id,
                semantic_item_id,
                "fixture-model-v1",
                "fixture-space-v1",
                4,
            ),
        )
        connection.commit()
    resolved = SimpleNamespace(
        source_kind="code",
        source_identity=source_identity,
        section_kind=f"code_{target_kind}",
        section_id=str(target_chunk_index),
        source_revision={"version_id": target_version_id},
        snippet="Validate SQLite access with PRAGMA quick_check",
        hit=SimpleNamespace(
            item_id=semantic_item_id,
            indexed_model_signature="fixture-model-v1",
            vector_space="fixture-space-v1",
            score=0.875,
            generation_id=4,
        ),
    )
    result = SimpleNamespace(
        rankings=(
            SimpleNamespace(
                name="semantic_text",
                available=True,
                resolved=(resolved,),
            ),
        )
    )
    search_kwargs: dict[str, object] = {}

    def semantic_result(*_args, **kwargs):
        search_kwargs.update(kwargs)
        return result

    monkeypatch.setattr(semantic_service, "search_semantic_index", semantic_result)

    model_cache = tmp_path / "model-cache"
    lower_score_hit = SimpleNamespace(**(vars(resolved.hit) | {"score": 0.625}))
    lower_score_resolved = SimpleNamespace(
        **(
            vars(resolved)
            | {
                "snippet": "Secondary SQLite access evidence",
                "hit": lower_score_hit,
            }
        )
    )
    ordered_result = SimpleNamespace(
        rankings=(
            SimpleNamespace(
                name="semantic_text",
                available=True,
                resolved=(resolved, lower_score_resolved),
            ),
        )
    )

    def ordered_semantic_result(*_args, **kwargs):
        search_kwargs.update(kwargs)
        return ordered_result

    monkeypatch.setattr(
        semantic_service,
        "search_semantic_index",
        ordered_semantic_result,
    )
    checkpoints = 0

    def checkpoint() -> None:
        nonlocal checkpoints
        checkpoints += 1

    query = CodeSearchQuery(
        text="where is SQLite access validated",
        modes=("semantic",),
        path="access.py",
        language="python",
    )
    with readonly_code_database(state_path) as connection:
        before_changes = connection.total_changes
        ordered_rows = code_search_implementation._semantic_rows(
            state_path,
            connection,
            query,
            7,
            SQLiteCancellationBridge(checkpoint),
            model_cache=model_cache,
            threads=2,
        )
        assert connection.total_changes == before_changes == 0
    assert checkpoints == 2
    assert search_kwargs["limit"] == 7
    assert search_kwargs["max_vectors"] == 500_000
    assert search_kwargs["include_text"] is True
    assert search_kwargs["include_images"] is False
    assert search_kwargs["include_lexical"] is False
    assert search_kwargs["model_cache"] == model_cache
    assert search_kwargs["local_files_only"] is True
    assert search_kwargs["threads"] == 2
    assert callable(search_kwargs["cancellation_check"])
    assert tuple(row.snippet for row in ordered_rows) == (
        "Validate SQLite access with PRAGMA quick_check",
        "Secondary SQLite access evidence",
    )
    assert tuple(row.evidence for row in ordered_rows) == (
        "semantic:fixture-model-v1:0.87500000:generation=4:"
        "space=fixture-space-v1:calibration=uncalibrated",
        "semantic:fixture-model-v1:0.62500000:generation=4:"
        "space=fixture-space-v1:calibration=uncalibrated",
    )
    assert all(row.version_id == target_version_id for row in ordered_rows)

    search_kwargs.clear()
    monkeypatch.setattr(semantic_service, "search_semantic_index", semantic_result)
    hits = search_code(
        state_path,
        query,
        semantic_model_cache=model_cache,
        semantic_threads=2,
    )
    assert search_kwargs["model_cache"] == model_cache
    assert search_kwargs["threads"] == 2
    rejected = search_code(
        state_path,
        CodeSearchQuery(
            text="where is SQLite access validated",
            modes=("semantic",),
            language="rust",
        ),
    )

    semantic_hits = tuple(hit for hit in hits if "semantic" in hit.match_types)
    assert len(semantic_hits) == 1
    assert semantic_hits[0].path == str(source)
    assert semantic_hits[0].language == "python"
    assert semantic_hits[0].version_id == target_version_id
    assert semantic_hits[0].start_line == target_start_line
    assert semantic_hits[0].end_line == target_end_line
    assert semantic_hits[0].symbol == target_symbol
    assert any(
        evidence.startswith("semantic:fixture-model-v1:0.87500000")
        for evidence in semantic_hits[0].evidence
    )
    assert rejected == ()

    base_resolved = vars(resolved)
    invalid_overrides: tuple[dict[str, object], ...] = (
        {"source_kind": "pdf"},
        {"section_kind": 1},
        {"section_kind": "code_"},
        {"section_id": target_chunk_index},
        {"section_id": f"0{target_chunk_index}"},
        {"section_id": "-1"},
        {"section_id": "not-a-chunk"},
        {"section_id": "9223372036854775808"},
        {"section_id": str(target_chunk_index + 10_000)},
        {"section_kind": "text"},
        {"section_kind": f"code_{target_kind}_missing"},
        {"source_revision": []},
        {"source_revision": {}},
        {"source_revision": {"version_id": True}},
        {"source_revision": {"version_id": target_version_id + 10_000}},
        {"source_identity": f"{source_identity}:missing"},
    )
    for overrides in invalid_overrides:
        invalid = SimpleNamespace(**(base_resolved | overrides))
        invalid_result = SimpleNamespace(
            rankings=(
                SimpleNamespace(
                    name="semantic_text",
                    available=True,
                    resolved=(invalid,),
                ),
            )
        )
        monkeypatch.setattr(
            semantic_service,
            "search_semantic_index",
            lambda *_args, _result=invalid_result, **_kwargs: _result,
        )
        assert (
            search_code(
                state_path,
                CodeSearchQuery(text="SQLite access", modes=("semantic",)),
            )
            == ()
        )

    for hit_overrides in (
        {"item_id": ""},
        {"indexed_model_signature": ""},
        {"vector_space": ""},
        {"generation_id": True},
        {"generation_id": 0},
        {"generation_id": 9_223_372_036_854_775_808},
    ):
        invalid_hit = SimpleNamespace(**(vars(resolved.hit) | hit_overrides))
        invalid = SimpleNamespace(**(base_resolved | {"hit": invalid_hit}))
        invalid_result = SimpleNamespace(
            rankings=(
                SimpleNamespace(
                    name="semantic_text",
                    available=True,
                    resolved=(invalid,),
                ),
            )
        )
        monkeypatch.setattr(
            semantic_service,
            "search_semantic_index",
            lambda *_args, _result=invalid_result, **_kwargs: _result,
        )
        assert (
            search_code(
                state_path,
                CodeSearchQuery(text="SQLite access", modes=("semantic",)),
            )
            == ()
        )

    unavailable_rankings = (
        (),
        (
            SimpleNamespace(
                name="semantic_image",
                available=True,
                resolved=(resolved,),
            ),
        ),
        (
            SimpleNamespace(
                name="semantic_text",
                available=False,
                resolved=(resolved,),
            ),
        ),
    )
    for rankings in unavailable_rankings:
        unavailable_result = SimpleNamespace(rankings=rankings)
        monkeypatch.setattr(
            semantic_service,
            "search_semantic_index",
            lambda *_args, _result=unavailable_result, **_kwargs: _result,
        )
        assert (
            search_code(
                state_path,
                CodeSearchQuery(text="SQLite access", modes=("semantic",)),
            )
            == ()
        )

    source.write_text(source_text + "\n# changed revision\n", encoding="utf-8")
    CodeRoute(
        config,
        _Inventory((source,)),
        _FrameworkState(),
        2,
        2,
    ).run()
    monkeypatch.setattr(
        semantic_service,
        "search_semantic_index",
        lambda *_args, **_kwargs: result,
    )
    assert (
        search_code(
            state_path,
            CodeSearchQuery(text="SQLite access", modes=("semantic",)),
        )
        == ()
    )


def test_code_semantic_search_cancels_inside_exact_vector_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "breaker.py"
    source.write_text(
        "def inspect_breaker():\n    return 'protection breaker'\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "state" / "code.sqlite3"
    config = CodeRouteConfig(
        state_path=state_path,
        dedup_path=tmp_path / "state" / "dedup.sqlite3",
    )
    CodeRoute(config, _Inventory((source,)), _FrameworkState(), 1, 1).run()
    monkeypatch.setattr(
        semantic_service,
        "_backend",
        lambda model, **_kwargs: _ConstantBackend(model),
    )
    semantic_service.index_text_embeddings(
        state_path.parent,
        source_kinds=("code",),
    )
    assert not state_path.with_name(f"{state_path.name}-wal").exists()
    assert not state_path.with_name(f"{state_path.name}-shm").exists()
    with closing(sqlite3.connect(state_path)) as connection:
        active_links = connection.execute(
            """SELECT e.provenance_json FROM embedding_links e
            WHERE e.active=1 ORDER BY e.chunk_id"""
        ).fetchall()
        current_chunks = int(
            connection.execute(
                """SELECT COUNT(*) FROM code_chunks c JOIN file_versions v
                ON v.version_id=c.version_id WHERE v.invalidated_ns IS NULL
                AND trim(c.text)<>''"""
            ).fetchone()[0]
        )
    assert len(active_links) == current_chunks > 0
    assert all(
        json.loads(str(row[0]))["link_protocol"] == "code-semantic-link-v1" for row in active_links
    )

    original_search = semantic_search_implementation.search_exact_page
    entered_vector_scan = False

    def observing_search(*args, **kwargs):
        nonlocal entered_vector_scan
        entered_vector_scan = True
        return original_search(*args, **kwargs)

    monkeypatch.setattr(
        semantic_search_implementation,
        "search_exact_page",
        observing_search,
    )

    class SearchCancelled(Exception):
        pass

    cancellation = SearchCancelled("cancel code semantic vector scan")
    repository_checkpoints = 0

    def cancellation_check() -> None:
        nonlocal repository_checkpoints
        if not entered_vector_scan:
            return
        repository_checkpoints += 1
        if repository_checkpoints == 2:
            raise cancellation

    with pytest.raises(SearchCancelled) as raised:
        search_code(
            state_path,
            CodeSearchQuery(text="protection breaker", modes=("semantic",)),
            cancellation_check=cancellation_check,
        )

    assert raised.value is cancellation
    assert entered_vector_scan
    assert repository_checkpoints == 2


def test_code_semantic_links_publish_replay_and_follow_current_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "relay.py"
    original = "def calculate_trip_threshold(current: float) -> float:\n    return current * 1.25\n"
    source.write_text(original, encoding="utf-8")
    state_path = tmp_path / "state" / "code.sqlite3"
    config = CodeRouteConfig(
        state_path=state_path,
        dedup_path=tmp_path / "state" / "dedup.sqlite3",
    )
    CodeRoute(config, _Inventory((source,)), _FrameworkState(), 1, 1).run()
    monkeypatch.setattr(
        semantic_service,
        "_backend",
        lambda model, **_kwargs: _ConstantBackend(model),
    )

    first = semantic_service.index_text_embeddings(
        state_path.parent,
        source_kinds=("code",),
    )
    first_generation = first.generations[0].summary.generation_id
    assert not state_path.with_name(f"{state_path.name}-wal").exists()
    assert not state_path.with_name(f"{state_path.name}-shm").exists()
    with closing(sqlite3.connect(state_path)) as connection:
        first_links = connection.execute(
            """SELECT chunk_id,semantic_item_id,model_signature,vector_space,
            generation_id,active,provenance_json FROM embedding_links
            ORDER BY chunk_id,model_signature,generation_id"""
        ).fetchall()
    assert first.complete
    assert first_links
    first_availability = code_semantic_search_availability(
        state_path.parent,
        verify_model_cache=False,
    )
    assert first_availability.available
    assert first_availability.generation_id == first_generation
    assert first_availability.current_links == len(first_links)
    assert {int(row[4]) for row in first_links if int(row[5]) == 1} == {first_generation}

    replay = semantic_service.index_text_embeddings(
        state_path.parent,
        source_kinds=("code",),
    )
    assert not state_path.with_name(f"{state_path.name}-wal").exists()
    assert not state_path.with_name(f"{state_path.name}-shm").exists()
    with closing(sqlite3.connect(state_path)) as connection:
        replay_links = connection.execute(
            """SELECT chunk_id,semantic_item_id,model_signature,vector_space,
            generation_id,active,provenance_json FROM embedding_links
            ORDER BY chunk_id,model_signature,generation_id"""
        ).fetchall()
    assert replay.complete
    assert replay.generations[0].summary.generation_id == first_generation
    assert replay.generations[0].queued == 0
    assert replay.generations[0].embedded == 0
    assert replay_links == first_links

    changed = original + "\ndef breaker_health(score: float) -> bool:\n    return score > 0.8\n"
    source.write_text(changed, encoding="utf-8")
    CodeRoute(config, _Inventory((source,)), _FrameworkState(), 2, 2).run()
    stale_availability = code_semantic_search_availability(
        state_path.parent,
        verify_model_cache=False,
    )
    assert not stale_availability.available
    assert stale_availability.reason == "no_current_default_profile_links"
    with closing(sqlite3.connect(state_path)) as connection:
        links_before_stale_sync = connection.execute(
            """SELECT chunk_id,semantic_item_id,model_signature,vector_space,
            generation_id,active,provenance_json FROM embedding_links
            ORDER BY chunk_id,model_signature,generation_id"""
        ).fetchall()
    with pytest.raises(CodeSemanticLinkError, match=r"current Code row|unlinked"):
        synchronize_code_embedding_links(
            state_path.parent,
            generation_id=first_generation,
            model_signature=first.generations[0].summary.model_signature,
        )
    with closing(sqlite3.connect(state_path)) as connection:
        assert (
            connection.execute(
                """SELECT chunk_id,semantic_item_id,model_signature,vector_space,
            generation_id,active,provenance_json FROM embedding_links
            ORDER BY chunk_id,model_signature,generation_id"""
            ).fetchall()
            == links_before_stale_sync
        )

    refreshed = semantic_service.index_text_embeddings(
        state_path.parent,
        source_kinds=("code",),
    )
    refreshed_generation = refreshed.generations[0].summary.generation_id
    assert not state_path.with_name(f"{state_path.name}-wal").exists()
    assert not state_path.with_name(f"{state_path.name}-shm").exists()
    with closing(sqlite3.connect(state_path)) as connection:
        active_generations = {
            int(row[0])
            for row in connection.execute(
                "SELECT DISTINCT generation_id FROM embedding_links WHERE active=1"
            )
        }
        active_current = int(
            connection.execute(
                """SELECT COUNT(*) FROM embedding_links e
                JOIN code_chunks c ON c.chunk_id=e.chunk_id
                JOIN file_versions v ON v.version_id=c.version_id
                JOIN files f ON f.current_version_id=v.version_id
                WHERE e.active=1 AND f.status='current'
                AND v.invalidated_ns IS NULL"""
            ).fetchone()[0]
        )
        current_chunks = int(
            connection.execute(
                """SELECT COUNT(*) FROM code_chunks c JOIN file_versions v
                ON v.version_id=c.version_id WHERE v.invalidated_ns IS NULL
                AND trim(c.text)<>''"""
            ).fetchone()[0]
        )
        inactive_history = int(
            connection.execute("SELECT COUNT(*) FROM embedding_links WHERE active=0").fetchone()[0]
        )
    assert refreshed.complete
    assert refreshed_generation > first_generation
    assert active_generations == {refreshed_generation}
    assert active_current == current_chunks > 0
    assert inactive_history == len(first_links)
    assert source.read_text(encoding="utf-8") == changed
    refreshed_availability = code_semantic_search_availability(
        state_path.parent,
        verify_model_cache=False,
    )
    assert refreshed_availability.available
    assert refreshed_availability.generation_id == refreshed_generation


# endregion [02]
