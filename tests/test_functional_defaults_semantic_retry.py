"""One local retry shares the original leases and budget without replaying successes."""

from pathlib import Path

import pytest

from neocortex.semantic import semantic_generation_worker as worker
from neocortex.semantic.semantic_models import BackendEmbedding
from neocortex.semantic.semantic_schema import semantic_database
from neocortex.semantic.semantic_work_budget import SemanticWorkBudget
from tests.test_semantic_generation_worker_batch import _fixture_generation


class _Backend:
    max_batch_size = 2

    def __init__(self, model, errors):
        self.model = model
        self.errors = iter(errors)
        self.calls = 0

    def embed(self, requests):
        self.calls += 1
        error = next(self.errors, None)
        if error is not None:
            raise error
        return tuple(BackendEmbedding(request.request_id, (1.0, 0.0, 0.0, 0.0), {}) for request in requests)


def test_recoverable_inference_is_retried_once_with_same_durable_jobs(tmp_path: Path):
    database, generation, model = _fixture_generation(tmp_path, count=2)
    backend = _Backend(model, (OSError("temporary input failure"), None))
    budget = SemanticWorkBudget(retry_recoverable_errors=True)
    result = worker.run_generation(database, generation, backend, queued=2, work_budget=budget)
    assert backend.calls == 2 and result.embedded == 2 and result.failed == 0
    assert result.summary.status == "ready" and result.stop_reason is None
    with semantic_database(database, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM embedding_jobs").fetchone()[0] == 2
        assert connection.execute("SELECT MAX(attempts) FROM embedding_jobs").fetchone()[0] == 1
    # The inline retry did not admit another item/job or reset the shared limits.
    assert budget.items_admitted == budget.new_jobs_admitted == 0


@pytest.mark.parametrize("error,expected_calls,reason", (
    (OSError("still unavailable"), 2, "retry_required"),
    (ValueError("permanent input failure"), 1, "review_required"),
))
def test_persistent_or_permanent_failure_stops_only_the_affected_model(
    tmp_path: Path, error, expected_calls: int, reason: str,
):
    database, generation, model = _fixture_generation(tmp_path, count=1)
    backend = _Backend(model, (error, error, error))
    budget = SemanticWorkBudget(retry_recoverable_errors=True)
    result = worker.run_generation(database, generation, backend, queued=1, work_budget=budget)
    assert backend.calls == expected_calls and result.stop_reason == reason
    assert result.summary.status != "ready"
    assert not budget.truncated  # the independent visual model retains its budget


def test_no_progress_exits_without_publishing_or_spinning(tmp_path: Path, monkeypatch):
    database, generation, model = _fixture_generation(tmp_path, count=1)
    backend = _Backend(model, ())
    calls = []

    def no_progress(*_args, **_kwargs):
        calls.append(1)
        return 0, 0, False

    monkeypatch.setattr(worker, "_run_generation_batch", no_progress)
    result = worker.run_generation(database, generation, backend, queued=1)
    assert calls == [1] and result.stop_reason == "no_progress"
    assert result.summary.pending == 1 and result.summary.status == "building"


def test_cancellation_between_retry_attempts_preserves_pending_job(tmp_path: Path):
    database, generation, model = _fixture_generation(tmp_path, count=1)
    backend = _Backend(model, (OSError("temporary"),))
    budget = SemanticWorkBudget(retry_recoverable_errors=True,
                                cancellation_check=lambda: backend.calls > 0)
    with pytest.raises(KeyboardInterrupt):
        worker.run_generation(database, generation, backend, queued=1, work_budget=budget)
    assert backend.calls == 1
    with semantic_database(database, readonly=True) as connection:
        assert connection.execute("SELECT status FROM embedding_jobs").fetchone()[0] == "pending"
