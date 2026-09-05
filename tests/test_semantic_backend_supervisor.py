from __future__ import annotations

import time
from pathlib import Path

import pytest

from neocortex.semantic import semantic_backend_supervisor as supervisor
from neocortex.semantic.semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRequest,
    EmbeddingRole,
    fingerprint_text,
)
from neocortex.semantic.semantic_work_budget import (
    SemanticIndexDeadlineExceeded,
    SemanticWorkBudget,
)


TEST_CAPABILITIES = ("base", 'inference')
pytestmark = pytest.mark.capability("base", 'inference')


def _model() -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        "deadline-supervisor-model-v1",
        "deadline-supervisor-space-v1",
        EmbeddingModality.TEXT,
        "fixture/deadline-supervisor",
        "1",
        4,
        "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )


def _blocked_startup_worker(*_args) -> None:
    time.sleep(60.0)


def _blocked_inference_worker(task_channel, result_channel, *_args) -> None:
    result_channel.put(("ready", 1))
    task_channel.get()
    time.sleep(60.0)


def _token_count_worker(task_channel, result_channel, *_args) -> None:
    result_channel.put(("ready", 8))
    while True:
        task = task_channel.get()
        if task is None:
            return
        operation, request_id, payload = task
        if operation == "text_tokenizer_contract":
            result_channel.put(
                ("ok", request_id, ("fixture-tokenizer-revision-v1", 512))
            )
            continue
        if operation != "text_token_counts":
            result_channel.put(("error", request_id, "runtime_error", "unexpected"))
            continue
        counts = tuple(len(text.split()) + 2 for text in payload)
        result_channel.put(("ok", request_id, (counts, 512)))


def _termination_observer(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    observed: list[bool] = []
    real_terminate = supervisor.terminate_isolated_process

    def terminate(process, timeout_seconds: float = 5.0) -> None:
        real_terminate(process, timeout_seconds)
        observed.append(not process.is_alive())

    monkeypatch.setattr(supervisor, "terminate_isolated_process", terminate)
    return observed


def test_deadline_terminates_backend_blocked_during_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated = _termination_observer(monkeypatch)
    budget = SemanticWorkBudget(deadline=time.monotonic() + 0.2)
    started = time.monotonic()

    with pytest.raises(SemanticIndexDeadlineExceeded):
        supervisor.DeadlineEmbeddingBackend(
            _model(),
            cache_dir=tmp_path,
            local_files_only=True,
            threads=1,
            work_budget=budget,
            worker_target=_blocked_startup_worker,
        )

    assert time.monotonic() - started < 5.0
    assert terminated == [True]


def test_deadline_terminates_backend_blocked_during_probe_or_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated = _termination_observer(monkeypatch)
    budget = SemanticWorkBudget(deadline=time.monotonic() + 5.0)
    backend = supervisor.DeadlineEmbeddingBackend(
        _model(),
        cache_dir=tmp_path,
        local_files_only=True,
        threads=1,
        work_budget=budget,
        worker_target=_blocked_inference_worker,
    )
    text = "semantic deadline probe"
    request = EmbeddingRequest(
        "deadline-probe",
        EmbeddingRole.QUERY,
        fingerprint_text(text),
        text=text,
    )
    budget.deadline = time.monotonic() + 0.2
    started = time.monotonic()

    with pytest.raises(SemanticIndexDeadlineExceeded):
        backend.embed((request,))

    assert time.monotonic() - started < 5.0
    assert terminated == [True]
    assert budget.truncation_reason == "time_budget"


def test_deadline_backend_counts_tokens_in_model_owning_child(tmp_path: Path) -> None:
    backend = supervisor.DeadlineEmbeddingBackend(
        _model(),
        cache_dir=tmp_path,
        local_files_only=True,
        threads=1,
        work_budget=SemanticWorkBudget(deadline=time.monotonic() + 5.0),
        worker_target=_token_count_worker,
    )
    try:
        contract = backend.text_tokenizer_contract()
        counts, limit = backend.text_token_counts(
            ("protección diferencial", "IEC 61850 GOOSE"),
        )
    finally:
        backend.close()

    assert contract == ("fixture-tokenizer-revision-v1", 512)
    assert counts == (4, 5)
    assert limit == 512
