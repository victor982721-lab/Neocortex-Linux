"""Work bounds, immutable replay and legacy-compatible Code publication."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from neocortex.code import code_schema
from neocortex.code.code_contracts import CodeRouteConfig, CodeSearchQuery
from neocortex.code.code_graph_generations import CodeGraphGenerationStore, CodeInput, GenerationConflict
from neocortex.code.code_graph_revision import graph_revision
from neocortex.code.code_route import CodeRoute
from neocortex.code.code_retention import CodeRetentionPolicy
from neocortex.code.code_state import CODE_GRAPH_RESOLVER_SIGNATURE, CodeState
from neocortex.code.search.code_search import search_code
from neocortex.deduplication import FileSnapshot
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken


class Inventory:
    def __init__(self, paths: tuple[Path, ...]):
        self.paths = paths

    def snapshots(self, scan_id: int):
        for path in self.paths:
            current = path.stat()
            yield FileSnapshot(str(path), current.st_dev, current.st_ino, current.st_size,
                               current.st_mtime_ns, getattr(current, "st_birthtime_ns", current.st_ctime_ns))


class Framework:
    def begin_route_phase(self, *args, **kwargs):
        pass

    def complete_route_phase(self, *args, **kwargs):
        pass

    def fail_route_phase(self, *args, **kwargs):
        pass


def _route(tmp_path: Path, count: int = 1):
    source = tmp_path / "source"
    source.mkdir()
    paths = tuple(source / f"file_{index}.py" for index in range(count))
    for index, path in enumerate(paths):
        path.write_text(f"def function_{index}(arg):\n    return arg + {index}\n")
    config = CodeRouteConfig(state_path=tmp_path / "code.sqlite3", dedup_path=tmp_path / "dedup.sqlite3")
    inventory = Inventory(paths)
    return config, inventory


def test_large_code_route_publishes_and_replays_without_copying_graph(tmp_path: Path, monkeypatch) -> None:
    config, inventory = _route(tmp_path, 1024)
    first = CodeRoute(config, inventory, Framework(), 1, 1).run()
    assert first.processed == 1024
    with CodeState(config.state_path) as state:
        original = state.graph_generation_store.read_published()
        original_revision = graph_revision(state.connection)
    assert original is not None

    def unnecessary_capture(*args, **kwargs):
        raise AssertionError("unchanged replay must reuse the immutable generation")

    monkeypatch.setattr(CodeGraphGenerationStore, "_legacy_input_snapshot_items", unnecessary_capture)
    monkeypatch.setattr(CodeGraphGenerationStore, "_legacy_graph_memberships", unnecessary_capture)
    for run in (2, 3):
        replay = CodeRoute(config, inventory, Framework(), run, run).run()
        assert replay.cache_hits == 1024 and replay.bytes_read == replay.processed == 0
        assert replay.graph_generation_reused == 1
        with CodeState(config.state_path) as state:
            assert state.graph_generation_store.get_head() == original.head
            assert graph_revision(state.connection) == original_revision
            assert state.connection.execute("SELECT COUNT(*) FROM graph_generations").fetchone()[0] == 1
            observed = state.graph_generation_store.reusable_observation(
                run, processing_signature=replay.processing_signature,
                resolver_signature=CODE_GRAPH_RESOLVER_SIGNATURE,
            )
            assert observed is not None
            assert observed.observer_run_id == run and observed.producer_run_id == 1


def test_streamed_snapshot_preserves_historical_digest_and_per_item_limit(tmp_path: Path) -> None:
    items = tuple(CodeInput(f"key:{index:05}", "digest", index, "á/" + "x" * 200,
                           {"unicode": "漢字", "nested": {2: "two", 10: "ten"}}) for index in range(1500))
    payload = [{"key": item.key, "content_digest": item.content_digest,
                "source_version_id": item.source_version_id, "observed_path": item.observed_path,
                "metadata": dict(item.metadata)} for item in items]
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    assert len(encoded) > 256 * 1024
    with CodeState(tmp_path / "code.sqlite3") as state:
        store = state.graph_generation_store
        snapshot = store.create_input_snapshot("large", 1, reversed(items))
        assert snapshot.input_digest == hashlib.sha256(encoded).hexdigest()
        assert store.create_input_snapshot("large", 1, iter(items)) == snapshot
        with pytest.raises(ValueError, match="input metadata exceeds"):
            store.create_input_snapshot("oversize-item", 1, (CodeInput("a", "b", metadata={"x": "y" * (256 * 1024)}),))
        with pytest.raises(ValueError, match="keys must be unique"):
            store.create_input_snapshot("duplicate", 1, (items[0], items[0]))
        assert store.get_input_snapshot("oversize-item") is None
        assert store.get_input_snapshot("duplicate") is None
        assert not state.connection.in_transaction


def test_reused_graph_keeps_original_producer_through_run_retention(tmp_path: Path) -> None:
    config, inventory = _route(tmp_path)
    config = replace(config, retention_policy=CodeRetentionPolicy(
        keep_completed_runs=1, max_terminal_runs=1, batch_size=100,
    ))
    for run in range(1, 5):
        summary = CodeRoute(config, inventory, Framework(), run, run).run()
        assert summary.graph_generation_reused == int(run > 1)
    with CodeState(config.state_path) as state:
        runs = tuple(row[0] for row in state.connection.execute(
            "SELECT analysis_run_id FROM analysis_runs ORDER BY analysis_run_id"
        ))
        assert runs == (1, 4)
        observed = state.graph_generation_store.reusable_observation(
            4, processing_signature=summary.processing_signature,
            resolver_signature=CODE_GRAPH_RESOLVER_SIGNATURE,
        )
        assert observed is not None and observed.producer_run_id == 1
        assert state.graph_generation_store.read_published() is not None


def test_snapshot_stream_cancellation_rolls_back_staging(tmp_path: Path) -> None:
    with CodeState(tmp_path / "code.sqlite3") as state:
        checks = 0

        def cancel():
            nonlocal checks
            checks += 1
            if checks == 300:
                raise CancellationRequested("fixture")

        with pytest.raises(CancellationRequested):
            state.graph_generation_store.create_input_snapshot(
                "cancelled", 1, (CodeInput(f"key:{n}", "digest") for n in range(1000)),
                cancellation_check=cancel,
            )
        assert state.graph_generation_store.get_input_snapshot("cancelled") is None
        assert state.connection.execute("SELECT name FROM sqlite_temp_master WHERE name='_code_snapshot_inputs'").fetchone() is None
        assert not state.connection.in_transaction


@pytest.mark.parametrize("table,assignment", (
    ("graph_member_block_items", "item_digest='corrupt'"),
    ("symbols", "signature='changed'"),
))
def test_changed_graph_rows_invalidate_replay(tmp_path: Path, table: str, assignment: str) -> None:
    config, inventory = _route(tmp_path)
    CodeRoute(config, inventory, Framework(), 1, 1).run()
    with CodeState(config.state_path) as state:
        before = graph_revision(state.connection)
        state.connection.execute(f"UPDATE {table} SET {assignment}")
        state.connection.commit()
        assert graph_revision(state.connection) > before
    replay = CodeRoute(config, inventory, Framework(), 2, 2).run()
    assert replay.graph_generation_reused == 0
    with CodeState(config.state_path) as state:
        assert state.graph_generation_store.get_head().generation_id == "analysis:2:graph"


def test_graph_change_after_reuse_proof_cannot_advance_fence(tmp_path: Path) -> None:
    config, inventory = _route(tmp_path)
    summary = CodeRoute(config, inventory, Framework(), 1, 1).run()
    with CodeState(config.state_path) as state:
        head = state.graph_generation_store.get_head()
        run_id = state.begin_run(2, 2, summary.processing_signature)
        assert state.reusable_graph_project_count(run_id, summary.processing_signature) is not None
        state.connection.execute("DELETE FROM graph_member_block_items WHERE item_key=(SELECT MIN(item_key) FROM graph_member_block_items)")
        state.connection.commit()
        with pytest.raises(GenerationConflict, match="changed after its reuse proof"):
            state.complete_run(run_id, {"candidates": 1, "cache_hits": 1, "graph_milliseconds": 0}, partial=False, graph_current=True)
        assert state.graph_generation_store.get_head() == head
        fence = json.loads(state.connection.execute("SELECT value FROM metadata WHERE key='code_graph_completion_v3'").fetchone()[0])
        assert fence["analysis_run_id"] == 1


@pytest.mark.parametrize("mutation", (
    "INSERT INTO graph_member_block_items SELECT block_id,item_key||':copy',item_digest,source_version_id,metadata_json FROM graph_member_block_items LIMIT 1",
    "DELETE FROM graph_member_block_items WHERE item_key=(SELECT MIN(item_key) FROM graph_member_block_items)",
    "UPDATE graph_heads SET revision=revision+1",
))
def test_graph_revision_and_projection_roll_back_together(tmp_path: Path, mutation: str) -> None:
    config, inventory = _route(tmp_path)
    CodeRoute(config, inventory, Framework(), 1, 1).run()
    with CodeState(config.state_path) as state:
        before = graph_revision(state.connection)
        state.connection.execute(mutation)
        assert graph_revision(state.connection) > before
        state.connection.rollback()
        assert graph_revision(state.connection) == before
    assert CodeRoute(config, inventory, Framework(), 2, 2).run().graph_generation_reused == 1


def test_cancellation_during_graph_capture_preserves_previous_head(tmp_path: Path, monkeypatch) -> None:
    config, inventory = _route(tmp_path)
    CodeRoute(config, inventory, Framework(), 1, 1).run()
    with CodeState(config.state_path) as state:
        head = state.graph_generation_store.get_head()
    inventory.paths[0].write_text("def changed():\n    return 2\n")
    cancellation = CancellationToken()
    original = CodeGraphGenerationStore._legacy_graph_memberships

    def cancelled_capture(store, *, cancellation_check=None):
        cancellation.cancel()
        return original(store, cancellation_check=cancellation_check)

    monkeypatch.setattr(CodeGraphGenerationStore, "_legacy_graph_memberships", cancelled_capture)
    with pytest.raises(CancellationRequested):
        CodeRoute(config, inventory, Framework(), 2, 2, cancellation=cancellation).run()
    with CodeState(config.state_path) as state:
        assert state.graph_generation_store.get_head() == head
        assert state.connection.execute("SELECT COUNT(*) FROM graph_input_snapshots").fetchone()[0] == 1
        assert state.connection.execute("SELECT status FROM analysis_runs WHERE analysis_run_id=2").fetchone()[0] == "cancelled"


def _steps(connection: sqlite3.Connection, sql: str, parameters: tuple[int, ...]):
    count = 0

    def step():
        nonlocal count
        count += 1
        return 0

    connection.set_progress_handler(step, 1)
    try:
        rows = connection.execute(sql, parameters).fetchall()
    finally:
        connection.set_progress_handler(None, 0)
    return count, rows


def test_fts_lookup_avoids_global_scan_and_preserves_duplicates_and_rollback(tmp_path: Path) -> None:
    config, inventory = _route(tmp_path)
    CodeRoute(config, inventory, Framework(), 1, 1).run()
    with CodeState(config.state_path) as state:
        connection = state.connection
        assert state._repair_cached_fts(1) == 0
        predicate, parameters = state._fts_lookup.predicate(1)
        sql = f"SELECT rowid FROM code_fts WHERE {predicate}"
        small, expected = _steps(connection, sql, parameters)
        connection.executemany("INSERT INTO code_fts(version_id,chunk_id,body) VALUES(?,?,?)",
                               ((n + 100, n + 100, "unrelated") for n in range(2000)))
        connection.commit()
        assert state._repair_cached_fts(1) == 0
        large, actual = _steps(connection, sql, parameters)
        assert actual == expected
        assert large < small * 3
        # New duplicate and wrong-version alias must be found after lookup creation.
        connection.execute("INSERT INTO code_fts SELECT chunk_id,version_id,path,project,language,symbol,signature,body FROM code_fts WHERE rowid=?", (expected[0][0],))
        connection.execute("INSERT INTO code_fts(chunk_id,version_id,body) VALUES(1,99999,'wrong version')")
        connection.commit()
        connection.execute("BEGIN")
        assert state._repair_cached_fts(1) == 3
        connection.rollback()
        assert state._repair_cached_fts(1) == 3
        connection.commit()
        assert state._repair_cached_fts(1) == 0


def test_code_v7_migration_preserves_rows_and_rejects_missing_v8_guard(tmp_path: Path) -> None:
    database = tmp_path / "v7.sqlite3"
    with sqlite3.connect(database) as connection:
        code_schema._execute(connection, code_schema._CURRENT_V1_DDL)
        code_schema._execute(connection, code_schema._PRODUCT_V2_DDL)
        code_schema._execute(connection, code_schema._GRAPH_GENERATION_DDL)
        for version in range(1, 8):
            code_schema._record_migration(connection, version, "fixture", version)
        connection.execute("INSERT INTO metadata VALUES('fixture','preserved')")
    code_schema.initialize_code_state(database)
    with CodeState(database) as state:
        assert state.connection.execute("PRAGMA user_version").fetchone()[0] == code_schema.CODE_SCHEMA_VERSION
        assert state.connection.execute("SELECT value FROM metadata WHERE key='fixture'").fetchone()[0] == "preserved"
        state.connection.execute("DROP TRIGGER code_graph_revision_graph_memberships_update")
        state.connection.commit()
    with pytest.raises(RuntimeError):
        code_schema.initialize_code_state(database)


def test_search_row_admission_stops_before_rank_materialization(tmp_path: Path) -> None:
    config, inventory = _route(tmp_path, 8)
    CodeRoute(config, inventory, Framework(), 1, 1).run()
    allowed = 2
    error = RuntimeError("row budget exhausted")

    def admit(count: int):
        nonlocal allowed
        if count > allowed:
            raise error
        allowed -= count

    with pytest.raises(RuntimeError) as caught:
        search_code(config.state_path, CodeSearchQuery(text="return", modes=("literal",)), row_admission=admit)
    assert caught.value is error and allowed == 0
    assert len(search_code(config.state_path, CodeSearchQuery(text="return", modes=("literal",)))) == 8
