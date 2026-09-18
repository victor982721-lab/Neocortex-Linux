"""Canonical digest compatibility and bounded serialization for Code graphs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from neocortex.code import code_graph_generations as graphs
from neocortex.code.code_graph_generations import CodeInput, GraphMembership
from neocortex.code.code_state import CodeState
from neocortex.runtime.control.cancellation import CancellationRequested


def _historical_digest(value: object) -> str:
    encoder = json.JSONEncoder(
        ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256()
    for piece in encoder.iterencode(value):
        digest.update(piece.encode("utf-8"))
    return digest.hexdigest()


@pytest.mark.parametrize("payload", (
    [],
    {"table": "symbols", "row": {"0": 1, "1": "café / 東京 / 🙂", "2": None}},
    [{"metadata": {"numeric_keys": {2: "two", 10: "ten", -1: "minus"}}, "value": True}],
    [{"values": [0.0, -0.0, 1e-300, 1e300, 1.7976931348623157e308, 5e-324]}],
    [{"escaped": "\"\\\n\r\t\b\f\x00\x1f"}, {"tuple": (None, False, 10**400)}],
    [{"large": "🙂" * 20_000}, {"node_limit": list(range(300))}],
))
def test_fragmented_hash_matches_historical_canonical_digest(payload: object) -> None:
    assert graphs._hash(payload, "fixture") == _historical_digest(payload)


@pytest.mark.parametrize("payload", (
    [{"invalid": float("nan")}],
    [{"invalid": float("inf")}],
    [{"invalid": float("-inf")}],
    [{"invalid": object()}],
    [{"invalid": "\ud800"}],
    [{"mixed_keys": {1: "one", "two": 2}}],
))
def test_fragmented_hash_keeps_json_failures_typed(payload: object) -> None:
    with pytest.raises(ValueError, match="fixture must be JSON-serializable"):
        graphs._hash(payload, "fixture")


def test_fragment_inspection_bounds_cycles_and_preserves_deep_metadata() -> None:
    nested: dict[str, object] = {"value": "leaf"}
    for _ in range(400):
        nested = {"nested": nested}
    assert graphs._hash([nested], "deep") == _historical_digest([nested])
    cycle: list[object] = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="cycle must be JSON-serializable"):
        graphs._hash(cycle, "cycle")


def test_large_batch_only_materializes_bounded_json_fragments(monkeypatch: pytest.MonkeyPatch) -> None:
    small = {"item_key": "symbol:東京", "metadata": {"kind": "function", "line": 10}}
    payload = [small for _ in range(2000)]
    payload.extend(({"large": "\x00" * 20_000}, {"nested": list(range(1000))}))
    expected = _historical_digest(payload)
    original = json.JSONEncoder
    encoded_sizes = []

    class ObservedEncoder(original):
        def encode(self, value):
            result = super().encode(value)
            encoded_sizes.append(len(result.encode("utf-8")))
            assert encoded_sizes[-1] <= graphs._HASH_FRAGMENT_BYTES
            return result

    monkeypatch.setattr(graphs.json, "JSONEncoder", ObservedEncoder)
    assert graphs._hash(payload, "large batch") == expected
    assert len(encoded_sizes) == 2000
    assert sum(encoded_sizes) > graphs._HASH_FRAGMENT_BYTES


def test_public_generation_keeps_metadata_limit_and_large_member_digest(tmp_path: Path) -> None:
    prefix = "café / 東京 / 🙂 "
    empty = graphs._json({"text": prefix}, "metadata")
    metadata = {"text": prefix + "x" * (graphs._MAX_JSON_BYTES - len(empty.encode("utf-8")))}
    assert len(graphs._json(metadata, "metadata").encode("utf-8")) == graphs._MAX_JSON_BYTES
    member = GraphMembership("symbol:large", "digest", metadata=metadata)
    oversized = GraphMembership("symbol:large", "digest", metadata={"text": metadata["text"] + "x"})
    with CodeState(tmp_path / "code.sqlite3") as state:
        store = state.graph_generation_store
        store.create_input_snapshot("input", 1, (CodeInput("file:one", "digest"),))
        store.start_generation("input", "generation")
        with pytest.raises(ValueError, match="membership metadata exceeds the metadata limit"):
            store.append_batch("generation", 0, (oversized,), cursor="one")
        assert state.connection.execute("SELECT COUNT(*) FROM graph_batches").fetchone()[0] == 0
        batch = store.append_batch("generation", 0, (member,), cursor="one")
        expected_batch = _historical_digest([{
            "item_key": member.item_key, "item_digest": member.item_digest,
            "source_version_id": None, "metadata": metadata,
        }])
        assert batch.batch_digest == expected_batch
        store.checkpoint("generation", 0, "one")
        completed = store.complete_generation("generation")
        snapshot = store.get_input_snapshot("input")
        assert snapshot is not None
        assert completed.generation_digest == _historical_digest({
            "snapshot_digest": snapshot.input_digest,
            "batches": [{"batch_index": 0, "batch_digest": expected_batch, "item_count": 1}],
        })
        store.compare_and_swap_head(
            "default", expected_revision=0, expected_generation_id=None, generation_id="generation",
        )
        published = store.read_published()
        assert published is not None and published.memberships == (member,)


def test_fragmented_member_validation_preserves_cancellation_and_retry(tmp_path: Path) -> None:
    members = tuple(GraphMembership(f"symbol:{n:03}", "digest", metadata={"line": n}) for n in range(80))
    with CodeState(tmp_path / "code.sqlite3") as state:
        store = state.graph_generation_store
        store.create_input_snapshot("input", 1, ())
        store.start_generation("input", "generation")
        store.append_batch("generation", 0, members, cursor="one")
        store.checkpoint("generation", 0, "one")
        store.complete_generation("generation")
        store.compare_and_swap_head(
            "default", expected_revision=0, expected_generation_id=None, generation_id="generation",
        )
        checks = 0

        def cancelled() -> None:
            nonlocal checks
            checks += 1
            if checks == 20:
                raise CancellationRequested("during member validation")

        with pytest.raises(CancellationRequested, match="during member validation"):
            store.read_published(cancellation_check=cancelled)
        assert checks == 20
        published = store.read_published()
        assert published is not None and published.memberships == members
