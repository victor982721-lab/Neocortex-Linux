"""Numeric grouping memory shares the same budget as models and scoring."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import numpy
import pytest

from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate,
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    ResourceSample,
    resource_scope,
)
from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded
from neocortex.semantic import semantic_exact_index_format as codec
from neocortex.semantic.semantic_resources import (
    CoordinatedEmbeddingBackend,
    retrieval_resource_scope,
    vector_workspace_scope,
)
from tests.semantic_exact_index_fixtures import published_text_fixture
from tests.test_semantic_exact_index_contract import _prepare, _query
from tests.test_semantic_exact_index_equivalence import _assert_page_equal

TEST_CAPABILITIES = ("inference",)
pytestmark = pytest.mark.capability("inference")
MIB = 1024 * 1024


def _coordinator(memory: int) -> GlobalResourceCoordinator:
    return GlobalResourceCoordinator(
        ("semantic",),
        GlobalResourceLimits(
            cpu_slots=2, memory_budget_bytes=memory,
            min_free_memory_bytes=0, min_free_commit_bytes=0,
            wait_timeout_seconds=0.1, poll_interval_seconds=0.001,
        ),
        cpu_load_probe=lambda: 0.0,
        resource_probe=lambda: ResourceSample(
            available_physical=128 * MIB, available_commit=128 * MIB,
            total_physical=128 * MIB, total_commit=128 * MIB,
        ),
    )


@pytest.mark.parametrize("evidence", (False, True))
def test_numeric_group_arrays_are_charged_before_allocation_and_match_native_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: bool,
) -> None:
    fixture = published_text_fixture(tmp_path / "owner", rows=24)
    expected = _query(fixture, evidence=evidence)
    coordinator = _coordinator(MIB)
    charged: list[int] = []
    original_full = numpy.full

    def allocated(*args, **kwargs):
        summary = coordinator.summary()
        charged.append(summary.transient_bytes)
        assert summary.transient_bytes > 0
        assert summary.native_threads == 0
        return original_full(*args, **kwargs)

    with _prepare(fixture, tmp_path / "index") as handle:
        monkeypatch.setattr(numpy, "full", allocated)
        with resource_scope(coordinator):
            actual = _query(fixture, handle=handle, evidence=evidence)
        _assert_page_equal(expected, actual)
        assert len(charged) == 2 and charged[0] == charged[1]
        assert handle.usage_summary()["used_queries"] == 1
    assert coordinator.summary().transient_bytes == 0
    assert coordinator.summary().native_threads == 0


def test_insufficient_numeric_workspace_abstains_before_arrays_and_handle_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = published_text_fixture(tmp_path / "owner", rows=24)
    expected = _query(fixture)
    coordinator = _coordinator(1)
    with _prepare(fixture, tmp_path / "index") as handle:
        with monkeypatch.context() as patch:
            patch.setattr(numpy, "full", lambda *_a, **_k: pytest.fail("workspace was not admitted"))
            with resource_scope(coordinator), pytest.raises(MemoryBudgetExceeded, match="workspace"):
                _query(fixture, handle=handle)
        assert coordinator.summary().transient_bytes == 0
        with resource_scope(_coordinator(MIB)):
            _assert_page_equal(expected, _query(fixture, handle=handle))


@pytest.mark.parametrize("failure", ("cancel", "score"))
def test_numeric_workspace_releases_on_cancellation_and_scoring_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    fixture = published_text_fixture(tmp_path / "owner", rows=24)
    coordinator = _coordinator(MIB)
    allocated = False
    original_full = numpy.full

    class OwnerCancelled(RuntimeError):
        pass

    def allocate(*args, **kwargs):
        nonlocal allocated
        allocated = True
        return original_full(*args, **kwargs)

    def check() -> None:
        if allocated and failure == "cancel":
            raise OwnerCancelled("cancelled with retained arrays")

    def fail_score(*_args, **_kwargs):
        assert coordinator.summary().transient_bytes > 0
        raise RuntimeError("synthetic scoring failure")

    with _prepare(fixture, tmp_path / "index") as handle:
        monkeypatch.setattr(numpy, "full", allocate)
        if failure == "score":
            monkeypatch.setattr(codec, "_numeric_scores", fail_score)
        with resource_scope(coordinator), pytest.raises(
            OwnerCancelled if failure == "cancel" else RuntimeError,
            match="cancelled with retained arrays" if failure == "cancel" else "synthetic scoring failure",
        ):
            _query(fixture, handle=handle, cancellation_check=check)
    assert allocated
    assert coordinator.summary().transient_bytes == 0
    assert coordinator.summary().native_threads == 0


def test_workspace_counts_resident_model_before_waiting_for_its_score_batch(tmp_path: Path) -> None:
    fixture = published_text_fixture(tmp_path / "owner", rows=8)
    coordinator = _coordinator(4 * MIB)
    with resource_scope(coordinator), retrieval_resource_scope("semantic"):
        backend = CoordinatedEmbeddingBackend(
            fixture.model, lambda _threads: pytest.fail("no model execution is required"),
            gate=CoordinatedMemoryGate(coordinator, "semantic"), resident_bytes=3 * MIB,
        )
        backend._reserve_model(0)
        with pytest.raises(MemoryBudgetExceeded, match="workspace and one score batch"):
            with vector_workspace_scope(MIB, score_bytes=1):
                pytest.fail("cannot wait behind an invocation-owned model")
        assert coordinator.summary().resident_bytes == 3 * MIB
        assert coordinator.summary().transient_bytes == 0
    assert coordinator.summary().resident_bytes == 0


@pytest.mark.parametrize("groups", (0, codec.MAX_VIEW_ROWS, codec.MAX_VIEW_ROWS + 1))
def test_numeric_group_limits_are_checked_before_large_array_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, groups: int,
) -> None:
    fixture = published_text_fixture(tmp_path / "owner", rows=8)
    with _prepare(fixture, tmp_path / "index") as handle:
        manifest = dict(handle._view.manifest)
        manifest["row_count"] = codec.MAX_VIEW_ROWS
        manifest["numeric_projection"] = {
            **manifest["numeric_projection"], "item_code_count": groups,
        }
        view = replace(handle._view, manifest=manifest)
        maps = {
            "rows.bin": bytes(codec.MAX_VIEW_ROWS * codec.ROW_STRUCT.size),
            "numeric-codes.bin": bytes(codec.MAX_VIEW_ROWS * codec.NUMERIC_CODE_STRUCT.size),
            "identity.bin": b"{}", "metadata.bin": b"{}",
        }

        @contextmanager
        def mapped(*_args, **_kwargs):
            yield maps

        monkeypatch.setattr(codec, "_mapped", mapped)
        monkeypatch.setattr(numpy, "full", lambda *_a, **_k: pytest.fail("large group arrays must be admitted first"))
        coordinator = _coordinator(MIB)
        expected = MemoryBudgetExceeded if groups == codec.MAX_VIEW_ROWS else codec.FallbackExactRequired
        with resource_scope(coordinator), pytest.raises(expected):
            codec._query_numeric(
                view,
                codec.ExactQuery(
                    fixture.model.model_signature, fixture.model.vector_space, 4,
                    (1.0, 0.0, 0.0, 0.0), "text",
                ),
                (1.0, 0.0, 0.0, 0.0), live_owner_binding=handle._owner.binding,
                selected_pairs={0}, signatures={fixture.model.model_signature: 1},
                limit=4, max_vectors=8, after_ref_id=0, batch_size=8,
                text_scope="content", evidence_mode=False,
                diagnostic_item_ids=(), diagnostics=None, cancellation_check=None,
                hydrate_provenance=True,
                workspace_scope=lambda size: vector_workspace_scope(size, score_bytes=1024),
            )
        assert coordinator.summary().transient_bytes == 0
