"""Publication-local equality evidence preserves complete graph validation."""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path

import pytest

from neocortex.code import code_graph_generations as graphs
from neocortex.code.code_contracts import CodeRouteConfig, CodeSearchQuery
from neocortex.code.code_graph_blocks import GRAPH_BLOCK_TABLES
from neocortex.code.code_route import CodeRoute
from neocortex.code.code_state import CodeState
from neocortex.code.search.code_search import search_code
from neocortex.deduplication import FileSnapshot
from neocortex.runtime.control.cancellation import CancellationRequested


class _Inventory:
    def __init__(self, paths):
        self.paths = paths

    def snapshots(self, _scan_id):
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


def _fixture(tmp_path, count=32):
    source = tmp_path / "source"
    source.mkdir()
    paths = tuple(source / f"unit_{index}.py" for index in range(count))
    for index, path in enumerate(paths):
        path.write_text(f"def function_{index}(arg):\n    return arg + {index}\n")
    return _config(tmp_path), _Inventory(paths)


def _config(root):
    return CodeRouteConfig(state_path=root / "code.sqlite3", dedup_path=root / "dedup.sqlite3")


def _run(config, inventory, run_id):
    return CodeRoute(config, inventory, _Framework(), run_id, run_id).run()


def _ledger(connection):
    return {
        table: tuple(tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY 1,2'))
        for table in (*GRAPH_BLOCK_TABLES, "graph_input_snapshots", "graph_generations", "graph_heads")
    }


def _published(config):
    with CodeState(config.state_path) as state:
        store = state.graph_generation_store
        publication = store.read_published()
        assert publication is not None
        inputs = store.get_input_items(publication.generation.snapshot_id)
        summary = state.connection.execute(
            "SELECT summary_json FROM analysis_runs WHERE status='completed' ORDER BY analysis_run_id DESC LIMIT 1"
        ).fetchone()
        observation = json.loads(summary[0])["graph_publication"]
        return publication, inputs, _ledger(state.connection), observation


def test_public_route_reuses_validation_with_identical_inputs_digests_and_rows(tmp_path: Path, monkeypatch):
    enabled, inventory = _fixture(tmp_path)
    disabled = _config(tmp_path / "uncached")
    (tmp_path / "uncached").mkdir()
    original_hash = graphs._hash
    active: dict[str, int] | None = None

    def counted(value, name):
        if active is not None and name == "graph batch":
            active["hashed_memberships"] += len(value)
        return original_hash(value, name)

    monkeypatch.setattr(graphs, "_hash", counted)
    for run_id, phase in enumerate(("cold", "replay", "change", "deletion"), 1):
        monkeypatch.setattr(graphs.time, "time_ns", lambda ordinal=run_id: 1_700_000_000_000_000_000 + ordinal)
        if phase == "change":
            inventory.paths[0].write_text("def changed(arg):\n    return arg + 991\n")
        if phase == "deletion":
            inventory.paths[1].unlink()
            inventory.paths = (inventory.paths[0], *inventory.paths[2:])
        summaries = []
        phase_work = []
        for config, reuse in ((enabled, True), (disabled, False)):
            active = {"hashed_memberships": 0}
            with monkeypatch.context() if not reuse else nullcontext() as patch:
                if patch is not None:
                    patch.setattr(graphs, "_PUBLICATION_VALIDATION_BYTES", 0)
                summaries.append(_run(config, inventory, run_id))
            phase_work.append(active)
            active = None
        current = _published(enabled)
        assert current == _published(disabled)
        for text in ("function_1", "changed"):
            query = CodeSearchQuery(text=text, modes=("literal",))
            assert search_code(enabled.state_path, query) == search_code(disabled.state_path, query)
        assert summaries[0].processed == summaries[1].processed
        assert summaries[0].cache_hits == summaries[1].cache_hits
        assert summaries[0].graph_generation_reused == summaries[1].graph_generation_reused
        count = 0 if phase == "replay" else len(current[0].memberships)
        assert phase_work == [{"hashed_memberships": count}, {"hashed_memberships": 2 * count}]


def test_evidence_budget_and_publication_lifetime_keep_full_fallback(tmp_path: Path, monkeypatch):
    config, inventory = _fixture(tmp_path)
    stores = []
    observed = []
    original = graphs.CodeGraphGenerationStore.complete_generation

    def checked(store, *args, **kwargs):
        stores.append(store)
        observed.append((len(store._publication_batch_rows), store._publication_batch_bytes))
        return original(store, *args, **kwargs)

    monkeypatch.setattr(graphs.CodeGraphGenerationStore, "complete_generation", checked)
    monkeypatch.setattr(graphs, "_PUBLICATION_VALIDATION_BYTES", 1024)
    _run(config, inventory, 1)
    assert observed == [(0, 0)]
    assert stores[0]._publication_batch_rows is None
    assert stores[0]._publication_batch_bytes == 0
    assert _published(config)[0].memberships
    monkeypatch.setattr(graphs, "_PUBLICATION_VALIDATION_BYTES", 16 * 1024 * 1024)
    inventory.paths[0].write_text("def changed():\n    return 99\n")
    _run(config, inventory, 2)
    assert observed[1][0] > 0 and 0 < observed[1][1] <= graphs._PUBLICATION_VALIDATION_BYTES
    assert stores[1]._publication_batch_rows is None
    assert stores[1]._publication_batch_bytes == 0


@pytest.mark.parametrize("mutation", (
    "UPDATE graph_member_block_items SET item_digest='corrupt' WHERE block_id=?",
    "UPDATE graph_member_block_items SET metadata_json='{\"table\":\"tampered\"}' WHERE block_id=?",
    "UPDATE graph_member_block_items SET source_version_id=NULL WHERE block_id=?",
    "UPDATE graph_member_block_items SET item_key='changed:' || item_key WHERE block_id=?",
    "UPDATE graph_member_blocks SET source_generation_id='analysis:1:graph' WHERE block_id=?",
))
def test_tampering_after_row_evidence_cannot_publish(tmp_path: Path, monkeypatch, mutation):
    config, inventory = _fixture(tmp_path)
    _run(config, inventory, 1)
    old = _published(config)
    inventory.paths[0].write_text("def changed():\n    return 99\n")
    original = graphs.CodeGraphGenerationStore.complete_generation
    stores = []

    def tampered(store, generation_id, **kwargs):
        stores.append(store)
        block = store.connection.execute(
            "SELECT block_id FROM graph_member_blocks WHERE source_generation_id=? LIMIT 1", (generation_id,),
        ).fetchone()
        assert block is not None and store._publication_batch_rows
        store.connection.execute(mutation, (block[0],))
        return original(store, generation_id, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(graphs.CodeGraphGenerationStore, "complete_generation", tampered)
        with pytest.raises(graphs.GenerationSchemaError):
            _run(config, inventory, 2)
    assert stores[0]._publication_batch_rows is None
    assert stores[0]._publication_batch_bytes == 0
    assert _published(config) == old
    assert _run(config, inventory, 3).graph_generation_reused == 0
    assert _published(config)[0].generation.metadata["source_run_id"] == 3


@pytest.mark.parametrize("last_batch", (False, True))
@pytest.mark.parametrize("stop_error", (CancellationRequested, TimeoutError))
def test_stop_inside_exact_comparison_rolls_back_and_retries(tmp_path: Path, monkeypatch, last_batch, stop_error):
    config, inventory = _fixture(tmp_path)
    _run(config, inventory, 1)
    old = _published(config)
    inventory.paths[0].write_text("def changed():\n    return 99\n")
    original = graphs.CodeGraphGenerationStore._publication_batch_matches
    stopped = []
    stores = []

    def interrupted(store, generation_id, batch_index, batch_digest, rows, cancellation_check):
        stores.append(store)
        final_index = store.connection.execute(
            "SELECT MAX(batch_index) FROM graph_batches WHERE generation_id=?", (generation_id,),
        ).fetchone()[0]
        checks = 0

        def cancel():
            nonlocal checks
            checks += 1
            if cancellation_check is not None:
                cancellation_check()
            if (not last_batch and checks == 2) or (last_batch and batch_index == final_index and checks == len(rows)):
                stopped.append((batch_index, checks, len(rows), final_index))
                raise stop_error("inside exact publication row comparison")

        return original(store, generation_id, batch_index, batch_digest, rows, cancel)

    with monkeypatch.context() as patch:
        patch.setattr(graphs.CodeGraphGenerationStore, "_publication_batch_matches", interrupted)
        with pytest.raises(stop_error, match="exact publication row comparison"):
            _run(config, inventory, 2)
    assert len(stopped) == 1
    assert all(store._publication_batch_rows is None for store in stores)
    assert _published(config) == old
    assert _run(config, inventory, 3).graph_generation_reused == 0
    assert _published(config)[0].generation.metadata["source_run_id"] == 3


def test_raw_row_equality_preserves_types_order_and_canonical_metadata(tmp_path: Path):
    with CodeState(tmp_path / "code.sqlite3") as state:
        store = state.graph_generation_store
        rows = (("a", "digest", 1, '{"table":"symbols"}'), ("b", "digest", None, '{}'))
        with store._publication_validation_scope():
            store._remember_publication_batch("generation", 0, "digest", rows)
            assert store._publication_batch_matches("generation", 0, "digest", rows, None)
            assert not store._publication_batch_matches("generation", 0, "digest", tuple(reversed(rows)), None)
            assert not store._publication_batch_matches("generation", 0, "digest", (
                ("a", "digest", 1.0, '{"table":"symbols"}'), rows[1],
            ), None)
            assert not store._publication_batch_matches("generation", 0, "digest", rows[:1], None)
            store._remember_publication_batch("generation", 1, "digest", (
                ("c", "digest", 1, '{"nested":{"2":"two","10":"ten"}}'),
            ))
            assert store._publication_batch_rows is not None
            assert ("generation", 1) not in store._publication_batch_rows
        assert store._publication_batch_rows is None
