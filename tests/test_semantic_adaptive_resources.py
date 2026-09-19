from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Sequence
from pathlib import Path

import pytest

from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate, GlobalResourceCoordinator, GlobalResourceLimits,
    ResourceSample, current_resource_grant, resource_scope,
)
from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded
from neocortex.semantic.semantic_backend_supervisor import DeadlineEmbeddingBackend
from neocortex.semantic.semantic_backends import FastEmbedBackend, SemanticExecutionProviderMismatch
from neocortex.semantic.semantic_chunking import TextChunkingConfig, iter_text_chunks
from neocortex.semantic.semantic_config import multilingual_text_model
from neocortex.semantic.semantic_models import (
    BackendEmbedding, EmbeddingModality, EmbeddingModelSpec, EmbeddingRequest,
    EmbeddingRole, TextSection, fingerprint_text,
)
from neocortex.semantic.semantic_resources import (
    CoordinatedEmbeddingBackend, governed_staging, retained_dependency, retrieval_resource_scope,
)
from neocortex.semantic.semantic_work_budget import SemanticWorkBudget

TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")
MIB = 1024 * 1024


def _model() -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        "adaptive-model-v1", "adaptive-space-v1", EmbeddingModality.TEXT,
        "fixture/adaptive", "1", 4, "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )


def _coordinator(cpus: list[int], pressure: list[float] | None = None) -> GlobalResourceCoordinator:
    return GlobalResourceCoordinator(
        ("semantic", "knowledge"),
        GlobalResourceLimits(
            memory_budget_bytes=256 * MIB, min_free_memory_bytes=0,
            min_free_commit_bytes=0, wait_timeout_seconds=1,
            sample_interval_seconds=0.001, poll_interval_seconds=0.001,
        ), cpu_load_probe=lambda: 0.0, effective_cpu_probe=lambda: cpus[0],
        resource_probe=lambda: ResourceSample(
            available_physical=512 * MIB, available_commit=512 * MIB,
            total_physical=512 * MIB, total_commit=512 * MIB,
            memory_pressure_some_percent=0 if pressure is None else pressure[0],
        ),
    )


class _Backend:
    max_batch_size = 64

    def __init__(self, threads: int, events: list[tuple[str, int]]) -> None:
        self.events = events
        self.events.append(("create", threads))
        self.closed = False

    @property
    def model(self) -> EmbeddingModelSpec:
        return _model()

    def configure_resources(self, *, threads: int) -> None:
        self.events.append(("configure", threads))

    def embed(self, requests: Sequence[EmbeddingRequest]) -> Sequence[BackendEmbedding]:
        grant = current_resource_grant()
        assert grant is not None
        self.events.append(("embed", grant.native_threads))
        return tuple(BackendEmbedding(r.request_id, (1.0, 0.0, 0.0, 0.0)) for r in requests)

    def text_token_counts(self, texts: Sequence[str]) -> tuple[tuple[int, ...], int]:
        grant = current_resource_grant()
        assert grant is not None and grant.native_threads == 1
        return tuple(len(t.split()) for t in texts), 512

    def close(self) -> None:
        self.closed = True


def _request() -> EmbeddingRequest:
    return EmbeddingRequest("stable-request", EmbeddingRole.PASSAGE,
                            fingerprint_text("same text"), text="same text")


def test_backend_yields_recovers_and_retains_result_ram_without_changing_identity() -> None:
    cpus, events = [3], []
    coordinator = _coordinator(cpus)
    with resource_scope(coordinator), retrieval_resource_scope("semantic"):
        backend = CoordinatedEmbeddingBackend(
            _model(), lambda threads: _Backend(threads, events),
            gate=CoordinatedMemoryGate(coordinator, "semantic"), resident_bytes=16 * MIB,
        )
        outputs = []
        for capacity in (3, 1, 12):
            cpus[0] = capacity
            assert backend.max_batch_size == capacity
            outputs.append(backend.embed((_request(),)))
            idle = coordinator.summary()
            assert idle.resident_bytes == 16 * MIB
            assert 0 < idle.transient_bytes < MIB
            assert idle.native_threads == idle.active_execution_requests == 0
            backend.text_token_counts(("exact tokens",))
        assert outputs[0] == outputs[1] == outputs[2]
        assert backend.model.model_signature == "adaptive-model-v1"
        assert events == [("create", 3), ("embed", 3), ("configure", 1),
                          ("embed", 1), ("configure", 12), ("embed", 12)]
    assert coordinator.summary().resident_bytes == 0
    assert coordinator.summary().transient_bytes == 0


def test_backend_honors_explicit_cap_and_releases_inactive_model() -> None:
    coordinator, events = _coordinator([24]), []
    with resource_scope(coordinator), retrieval_resource_scope("semantic"):
        gate = CoordinatedMemoryGate(coordinator, "semantic")
        first = CoordinatedEmbeddingBackend(_model(), lambda n: _Backend(n, events),
                                             gate=gate, max_threads=2, resident_bytes=16 * MIB)
        second = CoordinatedEmbeddingBackend(_model(), lambda n: _Backend(n, events),
                                              gate=gate, resident_bytes=32 * MIB)
        first.embed((_request(),))
        assert events == [("create", 2), ("embed", 2)]
        second.embed((_request(),))
        assert coordinator.summary().resident_bytes == 32 * MIB
        assert first._backend is None


def test_model_plus_one_batch_cannot_wait_behind_its_own_memory() -> None:
    coordinator = _coordinator([3])
    backend = CoordinatedEmbeddingBackend(
        _model(), lambda _n: pytest.fail("cannot load an oversized model"),
        gate=CoordinatedMemoryGate(coordinator, "semantic"), resident_bytes=252 * MIB,
    )
    try:
        with pytest.raises(MemoryBudgetExceeded, match=r"model and .*work.*budget"):
            backend.embed((_request(),))
    finally:
        backend.close()
    assert coordinator.summary().resident_bytes == 0


def test_backend_cancellation_during_admission_keeps_other_work_alive() -> None:
    coordinator = _coordinator([3])
    gate = CoordinatedMemoryGate(coordinator, "semantic")
    calls = 0
    class OwnerCancelled(RuntimeError):
        pass
    def check():
        nonlocal calls
        calls += 1
        if calls >= 10:
            raise OwnerCancelled("invocation cancelled")
    backend = CoordinatedEmbeddingBackend(
        _model(), lambda _n: pytest.fail("cancelled work cannot load the model"),
        gate=gate, resident_bytes=16 * MIB, checkpoint=check,
    )
    with gate.admit(1024, cpu_slots=3, native_threads=3):
        try:
            with pytest.raises(OwnerCancelled):
                backend.embed((_request(),))
        finally:
            backend.close()
        remaining = coordinator.summary()
        assert remaining.native_threads == 3
        assert remaining.transient_bytes == 1024
        assert remaining.resident_bytes == 0
        assert not coordinator.cancellation.is_cancelled


def test_cuda_cannot_be_persisted_under_a_cpu_model_identity(tmp_path: Path) -> None:
    with pytest.raises(SemanticExecutionProviderMismatch, match="separately supported"):
        FastEmbedBackend(multilingual_text_model(), cache_dir=tmp_path,
                         providers=("CUDAExecutionProvider", "CPUExecutionProvider"))
    assert not tuple(tmp_path.iterdir())


def test_oversized_staging_window_fails_without_waiting_behind_resident_model() -> None:
    coordinator = GlobalResourceCoordinator(("semantic",), GlobalResourceLimits(
        cpu_slots=1, memory_budget_bytes=64 * MIB, min_free_memory_bytes=0,
        min_free_commit_bytes=0, wait_timeout_seconds=0.1, poll_interval_seconds=0.001,
    ), cpu_load_probe=lambda: 0)
    gate = CoordinatedMemoryGate(coordinator, "semantic")
    @governed_staging
    def stage(**_kwargs):
        pytest.fail("oversized staging must fail before entering the writer")
    with resource_scope(coordinator), gate.resident(54 * MIB, resident_key="model"):
        with pytest.raises(MemoryBudgetExceeded, match="staging window"):
            stage(chunking=TextChunkingConfig(max_chars=1_000_000))
        assert coordinator.summary().resident_bytes == 54 * MIB
        assert coordinator.summary().transient_bytes == 0


def test_pressure_unloads_idle_model_but_retains_results_and_recovers() -> None:
    cpus, pressure, events = [3], [0.0], []
    coordinator = _coordinator(cpus, pressure)
    with resource_scope(coordinator), retrieval_resource_scope("semantic"):
        backend = CoordinatedEmbeddingBackend(
            _model(), lambda n: _Backend(n, events),
            gate=CoordinatedMemoryGate(coordinator, "semantic"), resident_bytes=16 * MIB,
        )
        first = backend.embed((_request(),))
        pressure[0] = 25.0
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: backend.max_batch_size)
            try:
                deadline = time.monotonic() + 1
                while coordinator.summary().resident_bytes and time.monotonic() < deadline:
                    time.sleep(0.005)
                suspended = coordinator.summary()
                assert suspended.resident_bytes == 0
                assert suspended.transient_bytes > 0
                assert suspended.native_threads == 0
                assert not future.done()
            finally:
                pressure[0] = 0.0
            assert future.result(timeout=2) == 3
        assert backend.embed((_request(),)) == first
        assert [event for event in events if event[0] == "create"] == [("create", 3)] * 2
    assert coordinator.summary().resident_bytes == coordinator.summary().transient_bytes == 0


def test_tokenization_guard_uses_its_actual_workspace_not_embedding_batch_estimate() -> None:
    coordinator = GlobalResourceCoordinator(("semantic",), GlobalResourceLimits(
        cpu_slots=1, memory_budget_bytes=64 * MIB, min_free_memory_bytes=0,
        min_free_commit_bytes=0, wait_timeout_seconds=0.1,
    ), cpu_load_probe=lambda: 0)
    gate = CoordinatedMemoryGate(coordinator, "semantic")
    backend = CoordinatedEmbeddingBackend(_model(), lambda n: _Backend(n, []),
                                          gate=gate, resident_bytes=50 * MIB)
    with gate.admit(7 * MIB, cpu_slots=0, native_threads=0), retained_dependency(7 * MIB):
        try:
            assert backend.text_token_counts(("two words",)) == ((2,), 512)
        finally:
            backend.close()
    assert coordinator.summary().resident_bytes == coordinator.summary().transient_bytes == 0


def test_new_model_accounts_pending_results_of_a_previous_model() -> None:
    coordinator = GlobalResourceCoordinator(("semantic",), GlobalResourceLimits(
        cpu_slots=1, memory_budget_bytes=64 * MIB, min_free_memory_bytes=0,
        min_free_commit_bytes=0, wait_timeout_seconds=0.1,
    ), cpu_load_probe=lambda: 0)
    gate = CoordinatedMemoryGate(coordinator, "semantic")
    with resource_scope(coordinator), retrieval_resource_scope("semantic"):
        first = CoordinatedEmbeddingBackend(_model(), lambda n: _Backend(n, []),
                                            gate=gate, resident_bytes=16 * MIB)
        second = CoordinatedEmbeddingBackend(
            _model(), lambda _n: pytest.fail("cannot discard another result to load a model"),
            gate=gate, resident_bytes=63 * MIB,
        )
        first.embed((_request(),))
        with pytest.raises(MemoryBudgetExceeded):
            second.text_token_counts(("two words",))
        assert coordinator.summary().transient_bytes > 0
    assert coordinator.summary().resident_bytes == coordinator.summary().transient_bytes == 0


def _token_worker(tasks, results, *_args) -> None:
    results.put(("ready", 64))
    while (task := tasks.get()) is not None:
        operation, request_id, payload = task
        assert operation == "text_token_counts"
        results.put(("ok", request_id, (tuple(len(t.split()) for t in payload), 512)))


def _projection_hash(chunks) -> str:
    projection = [(c.chunk_id, c.ordinal, c.start_char, c.end_char, c.text,
                   c.fingerprint.xxh3_128, c.fingerprint.byte_count,
                   c.fingerprint.xxh3_64_guard, c.chunking_signature) for c in chunks]
    return hashlib.sha256(json.dumps(projection, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def test_exact_chunking_batches_real_supervisor_rpcs_without_changing_published_identity(
    tmp_path: Path,
) -> None:
    backend = DeadlineEmbeddingBackend(
        _model(), cache_dir=tmp_path, local_files_only=True, threads=1,
        work_budget=SemanticWorkBudget(deadline=time.monotonic() + 15),
        worker_target=_token_worker,
    )
    sizes = []
    def counter(texts):
        sizes.append(len(texts))
        return backend.text_token_counts(texts)
    config = TextChunkingConfig(
        max_chars=1600, max_terms=280, overlap_chars=192, overlap_terms=40,
        min_natural_break_chars=128, model_token_limit=512,
        tokenizer_signature="synthetic-audit-v1",
    )
    try:
        chunks = tuple(iter_text_chunks(
            "synthetic-doc", (TextSection("page", "1",
            "Documento técnico de prueba sin datos reales. " * 10000),),
            config, token_counter=counter,
        ))
        assert backend._request_id == 11
    finally:
        backend.close()
    assert len(chunks) == 327
    assert sizes == [32] * 10 + [7]
    # Baseline 453f743: complete IDs, offsets, normalized text and fingerprints.
    assert _projection_hash(chunks) == "f21c276721fb9c4d94663e1e34b6cfbdc0b83c899ad8355029deb88e2c6c3125"


@pytest.mark.parametrize("text,max_chars,limit,mode,count,digest", (
    ("Δίκτυο 漢字🙂 e\u0301\t protección\n\n eléctrica! " * 90, 160, 37, "chars", 181,
     "c83d4593a76a2ee32dd8e5957abed64bc95d9e552e5b88ba6d83d3177cb2590e"),
    ("Árbol nube energía, λ界\n" * 120, 128, 25, "nonmonotonic", 120,
     "ae587d4c6a96f469aa5a028f70e1f007777dec5c18140e58b1474125e1c915b6"),
))
def test_exact_shrinking_keeps_unicode_offsets_and_nonmonotonic_token_counts(
    text: str, max_chars: int, limit: int, mode: str, count: int, digest: str,
) -> None:
    sizes = []
    def counter(texts):
        sizes.append(len(texts))
        return tuple(len(t) if mode == "chars" else len(t) // 2 +
                     (11 if len(t) % 7 == 0 else 0) for t in texts), limit
    config = TextChunkingConfig(
        max_chars=max_chars, max_terms=32, overlap_chars=17, overlap_terms=5,
        min_natural_break_chars=32, model_token_limit=limit,
        tokenizer_signature="synthetic-audit-v1",
    )
    chunks = tuple(iter_text_chunks("synthetic-doc", (TextSection("page", "1", text),),
                                   config, token_counter=counter))
    assert len(chunks) == count
    assert _projection_hash(chunks) == digest
    assert sum(sizes) < 4 * count
