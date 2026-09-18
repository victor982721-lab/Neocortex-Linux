"""Public Code publication, immutable sharing, corruption and lifecycle contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.code import code_schema
from neocortex.code.code_contracts import CodeRouteConfig, CodeSearchQuery
from neocortex.code.code_graph_blocks import GRAPH_BLOCK_TABLES, partition_keys
from neocortex.code.code_graph_generations import (
    CodeGraphGenerationStore,
    CodeInput,
    GenerationSchemaError,
    GraphMembership,
)
from neocortex.code.code_route import CodeRoute
from neocortex.code.code_state import CodeState
from neocortex.code.search.code_search import search_code
from neocortex.deduplication import FileSnapshot
from neocortex.runtime.control.cancellation import CancellationRequested


class _Inventory:
    def __init__(self, paths: tuple[Path, ...]):
        self.paths = paths

    def snapshots(self, _scan_id: int):
        for path in self.paths:
            item = path.stat()
            yield FileSnapshot(
                str(path), item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns,
                getattr(item, "st_birthtime_ns", item.st_ctime_ns),
            )


class _Framework:
    def begin_route_phase(self, *args, **kwargs):
        pass

    def complete_route_phase(self, *args, **kwargs):
        pass

    def fail_route_phase(self, *args, **kwargs):
        pass


def _fixture(tmp_path: Path, count: int = 8):
    source = tmp_path / "source"
    source.mkdir()
    paths = tuple(source / f"unit_{index}.py" for index in range(count))
    for index, path in enumerate(paths):
        path.write_text(f"def function_{index}(arg):\n    return arg + {index}\n")
    config = CodeRouteConfig(state_path=tmp_path / "code.sqlite3", dedup_path=tmp_path / "dedup.sqlite3")
    return config, _Inventory(paths)


def _run(config, inventory, run_id):
    return CodeRoute(config, inventory, _Framework(), run_id, run_id).run()


def _ledger(connection):
    return {
        table: tuple(tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY 1,2'))
        for table in (*GRAPH_BLOCK_TABLES, "graph_input_snapshots", "graph_generations", "graph_heads")
    }


def test_route_change_and_deletion_share_payloads_with_complete_independent_generations(tmp_path: Path):
    config, inventory = _fixture(tmp_path, 512)
    assert _run(config, inventory, 1).processed == 512
    with CodeState(config.state_path) as state:
        store = state.graph_generation_store
        old = store.read_published()
        assert old is not None
        old_inputs = store.get_input_items(old.generation.snapshot_id)
        old_blocks = {row[0] for row in state.connection.execute("SELECT block_id FROM graph_member_blocks")}
        assert len(old_blocks) > 2
    assert _run(config, inventory, 2).graph_generation_reused == 1
    inventory.paths[0].write_text("def optimized_delta(arg):\n    return arg + 99991\n")
    changed = _run(config, inventory, 3)
    assert changed.processed == 1 and changed.cache_hits == 511
    assert search_code(config.state_path, CodeSearchQuery(text="optimized_delta", modes=("literal",)))
    with CodeState(config.state_path) as state:
        store = state.graph_generation_store
        current = store.read_published()
        assert current is not None and current.head.revision == old.head.revision + 1
        assert store.list_memberships(old.head.generation_id) == old.memberships
        assert store.get_input_items(old.generation.snapshot_id) == old_inputs
        member_writes = state.connection.execute(
            "SELECT SUM(item_count) FROM graph_member_blocks WHERE source_generation_id=?",
            (current.head.generation_id,),
        ).fetchone()[0]
        input_writes = state.connection.execute(
            "SELECT SUM(item_count) FROM graph_input_blocks WHERE source_snapshot_id=?",
            (current.generation.snapshot_id,),
        ).fetchone()[0]
        assert 0 < member_writes < len(current.memberships)
        assert 0 < input_writes <= 256 < len(old_inputs)
        current_blocks = {row[0] for row in state.connection.execute(
            "SELECT block_id FROM graph_batch_blocks WHERE generation_id=?", (current.head.generation_id,),
        )}
        assert current_blocks & old_blocks
        current_ids = {f"version:{row[0]}" for row in state.connection.execute(
            "SELECT version_id FROM file_versions WHERE invalidated_ns IS NULL"
        )}
        assert {m.item_key for m in current.memberships if m.metadata['table'] == 'file_versions'} == current_ids
        current_inputs = store.get_input_items(current.generation.snapshot_id)
    inventory.paths[1].unlink()
    inventory.paths = (inventory.paths[0], *inventory.paths[2:])
    assert _run(config, inventory, 4).cache_hits == 511
    with CodeState(config.state_path) as state:
        store = state.graph_generation_store
        newest = store.read_published()
        assert newest is not None
        assert len(store.get_input_items(newest.generation.snapshot_id)) == 511
        assert store.get_input_items(current.generation.snapshot_id) == current_inputs
        assert store.get_generation(old.head.generation_id).status == "pruned"
        assert store.list_memberships(old.head.generation_id) == ()
        assert state.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for objects, refs in (("graph_input_blocks", "graph_snapshot_blocks"), ("graph_member_blocks", "graph_batch_blocks")):
            assert state.connection.execute(
                f"SELECT COUNT(*) FROM {objects} o WHERE NOT EXISTS(SELECT 1 FROM {refs} r WHERE r.block_id=o.block_id)"
            ).fetchone()[0] == 0


@pytest.mark.parametrize("mutation", (
    "UPDATE graph_member_block_items SET item_digest='corrupt' WHERE item_key=(SELECT MIN(item_key) FROM graph_member_block_items)",
    "DELETE FROM graph_member_block_items WHERE item_key=(SELECT MIN(item_key) FROM graph_member_block_items)",
    "DELETE FROM graph_batch_blocks WHERE batch_index=0",
    "UPDATE graph_member_blocks SET block_digest='x' || substr(block_digest,2)",
))
def test_corrupt_shared_publication_abstains_and_rebuilds_from_current_source(tmp_path: Path, mutation: str):
    config, inventory = _fixture(tmp_path)
    _run(config, inventory, 1)
    with CodeState(config.state_path) as state:
        old = state.graph_generation_store.read_published()
        state.connection.execute(mutation)
        state.connection.commit()
        with pytest.raises(GenerationSchemaError):
            state.graph_generation_store.read_published()
    assert _run(config, inventory, 2).graph_generation_reused == 0
    with CodeState(config.state_path) as state:
        published = state.graph_generation_store.read_published()
        assert published is not None
        assert {item.item_key for item in published.memberships} == {item.item_key for item in old.memberships}
        # Project observations legitimately advance last_seen_run_id while the
        # resolver rebuilds the generation. All content evidence stays intact.
        assert tuple(item for item in published.memberships if item.metadata['table'] != 'projects') == tuple(
            item for item in old.memberships if item.metadata['table'] != 'projects'
        )
        assert published.head.generation_id != old.head.generation_id


@pytest.mark.parametrize("mutation", (
    "UPDATE graph_input_block_items SET content_digest='corrupt'",
    "DELETE FROM graph_snapshot_blocks",
))
def test_corrupt_shared_inputs_are_rejected_and_never_borrowed(tmp_path: Path, mutation: str):
    config, inventory = _fixture(tmp_path)
    _run(config, inventory, 1)
    with CodeState(config.state_path) as state:
        store = state.graph_generation_store
        snapshot_id = store.read_published().generation.snapshot_id
        inputs = store.get_input_items(snapshot_id)
        state.connection.execute(mutation)
        state.connection.commit()
        with pytest.raises(GenerationSchemaError):
            store.get_input_items(snapshot_id)
        with pytest.raises(GenerationSchemaError):
            store.read_published()
    assert _run(config, inventory, 2).graph_generation_reused == 0
    with CodeState(config.state_path) as state:
        store = state.graph_generation_store
        assert store.get_input_items(store.read_published().generation.snapshot_id) == inputs


@pytest.mark.parametrize("kind,table,column,wrong", (
    ("input", "graph_input_blocks", "source_snapshot_id", "wrong:snapshot"),
    ("member", "graph_member_blocks", "source_generation_id", "wrong:generation"),
))
def test_original_block_producer_is_validated_before_read_and_reuse(tmp_path: Path, kind, table, column, wrong):
    config, inventory = _fixture(tmp_path, 64)
    _run(config, inventory, 1)
    with CodeState(config.state_path) as state:
        store = state.graph_generation_store
        store.create_input_snapshot("wrong:snapshot", 1, ())
        store.start_generation("wrong:snapshot", "wrong:generation")
        state.connection.execute(f"UPDATE {table} SET {column}=?", (wrong,))
        state.connection.commit()
        assert state.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(GenerationSchemaError, match="original producer"):
            store.read_published()
    assert _run(config, inventory, 2).graph_generation_reused == 0
    with CodeState(config.state_path) as state:
        store = state.graph_generation_store
        published = store.read_published()
        if kind == "input":
            refs, owner_column, owner = 'graph_snapshot_blocks', 'snapshot_id', published.generation.snapshot_id
        else:
            refs, owner_column, owner = 'graph_batch_blocks', 'generation_id', published.head.generation_id
        assert state.connection.execute(
            f"SELECT COUNT(*) FROM {refs} r JOIN {table} o ON o.block_id=r.block_id "
            f"WHERE r.{owner_column}=? AND o.{column}=?", (owner, wrong),
        ).fetchone()[0] == 0


def test_published_snapshot_validation_obeys_cancellation_during_input_hash(tmp_path: Path):
    with CodeState(tmp_path / 'code.sqlite3') as state:
        store = state.graph_generation_store
        store.create_input_snapshot('input', 1, (CodeInput(f'key:{n}', 'value') for n in range(600)), shared_blocks=True)
        store.start_generation('input', 'generation')
        store.complete_generation('generation')
        store.compare_and_swap_head('default', expected_revision=0, expected_generation_id=None, generation_id='generation')
        checks = 0

        def cancelled():
            nonlocal checks
            checks += 1
            if checks == 30:
                raise CancellationRequested('while hashing snapshot rows')

        with pytest.raises(CancellationRequested):
            store.read_published(cancellation_check=cancelled)
        assert checks == 30
        assert store.read_published() is not None


def test_cancellation_after_block_write_rolls_back_manifest_and_payloads(tmp_path: Path, monkeypatch):
    config, inventory = _fixture(tmp_path)
    _run(config, inventory, 1)
    with CodeState(config.state_path) as state:
        ledger = _ledger(state.connection)
    inventory.paths[0].write_text("def changed_after_cancel():\n    return 42\n")
    original = CodeGraphGenerationStore._append_shared_batch

    def interrupted(store, *args, **kwargs):
        original(store, *args, **kwargs)
        raise CancellationRequested("after actual block writes")

    with monkeypatch.context() as patch:
        patch.setattr(CodeGraphGenerationStore, "_append_shared_batch", interrupted)
        with pytest.raises(CancellationRequested):
            _run(config, inventory, 2)
    with CodeState(config.state_path) as state:
        assert _ledger(state.connection) == ledger
        assert state.connection.execute("SELECT status FROM analysis_runs WHERE analysis_run_id=2").fetchone()[0] == "cancelled"
    assert _run(config, inventory, 3).graph_generation_reused == 0
    with CodeState(config.state_path) as state:
        assert state.graph_generation_store.read_published().generation.metadata['source_run_id'] == 3


def test_block_publication_holds_the_source_writer_transaction(tmp_path: Path, monkeypatch):
    config, inventory = _fixture(tmp_path)
    original = CodeGraphGenerationStore._append_shared_batch
    attempts = []

    def concurrent_writer(store, *args, **kwargs):
        original(store, *args, **kwargs)
        if attempts:
            return
        with sqlite3.connect(config.state_path, timeout=0) as writer:
            with pytest.raises(sqlite3.OperationalError) as failed:
                writer.execute("UPDATE symbols SET signature='concurrent change'")
            attempts.append(failed.value.sqlite_errorcode)

    monkeypatch.setattr(CodeGraphGenerationStore, "_append_shared_batch", concurrent_writer)
    assert _run(config, inventory, 1).processed == 8
    assert attempts == [sqlite3.SQLITE_BUSY]
    assert _run(config, inventory, 2).graph_generation_reused == 1


def test_pinned_head_keeps_original_blocks_after_normal_retention(tmp_path: Path):
    config, inventory = _fixture(tmp_path)
    _run(config, inventory, 1)
    with CodeState(config.state_path) as state:
        store = state.graph_generation_store
        original = store.read_published()
        store.compare_and_swap_head("pinned", expected_revision=0, expected_generation_id=None,
                                    generation_id=original.head.generation_id)
    for run_id in (2, 3, 4):
        inventory.paths[0].write_text(f"def changed_{run_id}():\n    return {run_id}\n")
        _run(config, inventory, run_id)
    with CodeState(config.state_path) as state:
        store = state.graph_generation_store
        assert store.read_published("pinned").memberships == original.memberships
        assert store.get_generation(original.head.generation_id).status == "published"
        assert state.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_snapshot_sharing_preserves_canonical_metadata_and_rejects_overlap(tmp_path: Path):
    inputs = tuple(CodeInput(f"file:{n}", "digest", n, metadata={"nested": {2: "two", 10: "ten"}}) for n in range(600))
    with CodeState(tmp_path / "code.sqlite3") as state:
        store = state.graph_generation_store
        legacy = store.create_input_snapshot("legacy", 1, inputs)
        shared = store.create_input_snapshot("shared", 2, reversed(inputs), shared_blocks=True, block_size=64)
        assert shared.input_digest == legacy.input_digest
        assert store.get_input_items("shared") == store.get_input_items("legacy")
        replay = store.create_input_snapshot("next", 3, inputs, shared_blocks=True, block_size=64)
        assert replay.input_digest == shared.input_digest
        assert state.connection.execute("SELECT COUNT(*) FROM graph_input_block_items").fetchone()[0] == 600
        state.connection.execute(
            "INSERT INTO graph_snapshot_inputs SELECT 'next',input_key,content_digest,source_version_id,observed_path,metadata_json "
            "FROM graph_input_block_items LIMIT 1"
        )
        state.connection.commit()
        with pytest.raises(GenerationSchemaError, match="membership count"):
            store.get_input_items("next")


def test_pruning_one_generation_preserves_a_snapshot_used_by_another(tmp_path: Path):
    with CodeState(tmp_path / "code.sqlite3") as state:
        store = state.graph_generation_store
        store.create_input_snapshot("shared", 1, (CodeInput("a", "digest"),), shared_blocks=True)
        for ordinal in (1, 2):
            generation_id = f"generation:{ordinal}"
            store.start_generation("shared", generation_id, created_ns=ordinal)
            store.complete_generation(generation_id)
            current = store.get_head()
            store.compare_and_swap_head("default", expected_revision=0 if current is None else current.revision,
                                        expected_generation_id=None if current is None else current.generation_id,
                                        generation_id=generation_id)
        assert store.prune_generations(keep=1) == ("generation:1",)
        assert store.get_input_items("shared") == (CodeInput("a", "digest"),)
        assert store.read_published() is not None


def test_v8_migration_keeps_every_legacy_generation_row_and_digest(tmp_path: Path):
    path = tmp_path / "v8.sqlite3"
    with CodeState(path) as state:
        store = state.graph_generation_store
        store.create_input_snapshot("old", 1, (CodeInput("a", "digest"),))
        store.start_generation("old", "generation:old")
        store.append_batch("generation:old", 0, (GraphMembership("a", "digest"),), cursor="a")
        store.checkpoint("generation:old", 0, "a")
        store.complete_generation("generation:old")
        store.compare_and_swap_head("default", expected_revision=0, expected_generation_id=None,
                                    generation_id="generation:old")
        original = store.read_published()
    with sqlite3.connect(path) as connection:
        for table in reversed(GRAPH_BLOCK_TABLES):
            connection.execute(f'DROP TABLE "{table}"')
        connection.execute("DELETE FROM graph_generation_migrations WHERE version=2")
        connection.execute("UPDATE graph_generation_metadata SET value='1' WHERE key='schema_version'")
        connection.execute("DELETE FROM schema_migrations WHERE version=9")
        connection.execute("UPDATE metadata SET value='8' WHERE key='schema_version'")
        connection.execute("PRAGMA user_version=8")
        connection.commit()
        code_schema.validate_code_schema_v8(connection)
        legacy_tables = sorted(code_schema._GRAPH_GENERATION_TABLES - {
            'graph_generation_metadata', 'graph_generation_migrations',
        })
        before = {table: connection.execute(f'SELECT * FROM "{table}" ORDER BY 1,2').fetchall() for table in legacy_tables}
    before_bytes = path.read_bytes()
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as connection:
        legacy_store = CodeGraphGenerationStore(connection)
        assert legacy_store.read_published() == original
        assert legacy_store.membership_materialization('generation:old', limit=2) == (1, 9)
        with pytest.raises(GenerationSchemaError, match='graph schema v2'):
            legacy_store.create_input_snapshot('no-upgrade', 2, (), shared_blocks=True)
        assert connection.total_changes == 0
    assert path.read_bytes() == before_bytes
    code_schema.initialize_code_state(path)
    with CodeState(path) as state:
        assert state.graph_generation_store.read_published() == original
        after = {table: [tuple(row) for row in state.connection.execute(f'SELECT * FROM "{table}" ORDER BY 1,2')] for table in legacy_tables}
        assert after == before
        assert tuple(row[0] for row in state.connection.execute("SELECT version FROM graph_generation_migrations ORDER BY version")) == (1, 2)
        assert all(state.connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0 for table in GRAPH_BLOCK_TABLES)


def test_key_partitioning_limits_payload_and_keeps_unaffected_leaves_stable():
    keys = tuple(f"symbol:{n}" for n in range(4096))
    before = {frozenset(group) for group in partition_keys(keys, 64)}
    after = {frozenset(group) for group in partition_keys((*keys, "symbol:new"), 64)}
    assert max(map(len, after)) <= 64
    assert set().union(*after) == {*keys, "symbol:new"}
    assert sum(map(len, after - before)) <= 65
