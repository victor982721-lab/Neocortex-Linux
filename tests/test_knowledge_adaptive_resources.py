from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.knowledge import knowledge_search as search
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate, GlobalResourceCoordinator, GlobalResourceLimits,
    ResourceSample, resource_scope,
)
from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded
from neocortex.semantic.semantic_resources import CoordinatedEmbeddingBackend
from tests.test_knowledge_search import (
    KnowledgeQuery, KnowledgeStatePaths, OwnerAvailability, OwnerSnapshot,
    _candidate, _snapshot, plan_knowledge_query,
)
from tests.test_semantic_adaptive_resources import _model, _request

TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")
MIB = 1024 * 1024


@pytest.mark.parametrize("oversized_model,fail_phase", ((False, False), (True, False), (False, True)))
def test_candidates_keep_ram_without_cpu_across_semantic_phase_and_release_on_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    oversized_model: bool, fail_phase: bool,
) -> None:
    paths = KnowledgeStatePaths.from_directory(tmp_path)
    snapshot = _snapshot(OwnerSnapshot("pdf", OwnerAvailability.AVAILABLE, 2, 2))
    plan = plan_knowledge_query(KnowledgeQuery("transformador", source_kinds=("pdf",), limit=5))
    candidate = _candidate(evidence_id="retained", section_id="1", start_char=0, end_char=50,
                           ranking="fts_pdf", source_rank=1)
    coordinator = GlobalResourceCoordinator(("knowledge", "semantic"), GlobalResourceLimits(
        cpu_slots=1, memory_budget_bytes=64 * MIB, min_free_memory_bytes=0,
        min_free_commit_bytes=0, wait_timeout_seconds=0.2, poll_interval_seconds=0.001,
    ), cpu_load_probe=lambda: 0, resource_probe=lambda: ResourceSample(
        available_physical=512 * MIB, available_commit=512 * MIB,
        total_physical=512 * MIB, total_commit=512 * MIB,
    ))
    original_semantic = search._run_semantic_phase
    observed = []

    class OwnerFailure(RuntimeError):
        pass

    def semantic(execution):
        summary = coordinator.summary()
        assert execution.rankings["fts_pdf"] == (candidate,)
        assert summary.routes["knowledge"].transient_bytes > 0
        assert summary.native_threads == 0
        observed.append(True)
        if oversized_model:
            backend = CoordinatedEmbeddingBackend(
                _model(), lambda _n: pytest.fail("cannot load impossible dependent model"),
                gate=CoordinatedMemoryGate(coordinator, "semantic"), resident_bytes=56 * MIB,
            )
            try:
                with pytest.raises(MemoryBudgetExceeded, match="dependent work"):
                    backend.embed((_request(),))
            finally:
                backend.close()
        if fail_phase:
            raise OwnerFailure("injected phase failure")
        return original_semantic(execution)

    monkeypatch.setattr(search, "_lexical_rankings", lambda *args, **kwargs: ({"fts_pdf": (candidate,)}, []))
    monkeypatch.setattr(search, "_run_semantic_phase", semantic)
    with resource_scope(coordinator):
        if fail_phase:
            with pytest.raises(OwnerFailure):
                search.execute_knowledge_search(paths, plan, snapshot)
        else:
            result = search.execute_knowledge_search(paths, plan, snapshot)
            assert len(result.hits) == 1
    assert observed == [True]
    assert coordinator.summary().resident_bytes == coordinator.summary().transient_bytes == 0
