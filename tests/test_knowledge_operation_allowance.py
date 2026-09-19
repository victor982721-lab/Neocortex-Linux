"""Public read limits stop work during preparation, SQL and vector batches."""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from neocortex.knowledge.knowledge_planner import KnowledgeQuery
from neocortex.knowledge.knowledge_read_budget import KnowledgeReadBudget, KnowledgeReadBudgetExceeded
from neocortex.knowledge.knowledge_read_operation import (
    knowledge_read_operation, read_checkpoint, read_query_limit, read_rows,
)
from neocortex.knowledge.knowledge_service import KnowledgeSearchService
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
from neocortex.semantic.semantic_schema import SemanticReadContext, semantic_database, semantic_read_context
from tests.test_knowledge_service import _result, _snapshot


def test_snapshot_exhausts_deadline_before_executor_starts(tmp_path):
    now = [0]
    calls = []
    def collect(*args, **kwargs):
        now[0] = 2_000_000_000
        return _snapshot("same")
    def execute(*args, **kwargs):
        calls.append("executor")
        raise AssertionError("deadline must be checked after snapshot")
    service = KnowledgeSearchService(KnowledgeStatePaths.from_directory(tmp_path),
                                     snapshot_collector=collect, search_executor=execute)
    with pytest.raises(KnowledgeReadBudgetExceeded, match="deadline"):
        service.search(KnowledgeQuery("fixture"), read_budget=KnowledgeReadBudget(
            deadline_seconds=1, monotonic_clock=lambda: now[0]))
    assert calls == []


def test_budget_cancel_reaches_snapshot_checkpoint(tmp_path):
    cancelled = [False]
    def collect(*args, cancellation_check=None, **kwargs):
        cancelled[0] = True
        cancellation_check()
        pytest.fail("budget cancellation did not reach snapshot")
    service = KnowledgeSearchService(KnowledgeStatePaths.from_directory(tmp_path), snapshot_collector=collect)
    with pytest.raises(KnowledgeReadBudgetExceeded, match="cancelled"):
        service.search(KnowledgeQuery("fixture"), read_budget=KnowledgeReadBudget(
            cancellation_check=lambda: cancelled[0]))


def test_retry_keeps_spent_vector_allowance(tmp_path):
    snapshots = iter((_snapshot("old"), _snapshot("new"), _snapshot("new")))
    batches = []
    def execute(paths, plan, snapshot, **kwargs):
        read_checkpoint(vectors=2)
        batches.append("admitted")
        return _result(plan, snapshot, "fixture")
    budget = KnowledgeReadBudget(max_vectors=3)
    service = KnowledgeSearchService(KnowledgeStatePaths.from_directory(tmp_path),
        snapshot_collector=lambda *args, **kwargs: next(snapshots), search_executor=execute)
    with pytest.raises(KnowledgeReadBudgetExceeded, match="vectors"):
        service.search(KnowledgeQuery("fixture"), read_budget=budget)
    assert batches == ["admitted"]
    assert budget.vectors_used == 2


def test_data_rows_are_bounded_inside_cursor_batches():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("CREATE TABLE fixture(value)")
        connection.executemany("INSERT INTO fixture VALUES(?)", ((n,) for n in range(1000)))
        budget = KnowledgeReadBudget(max_rows=17)
        with pytest.raises(KnowledgeReadBudgetExceeded, match="rows"):
            with knowledge_read_operation(budget, None):
                read_rows(connection.execute("SELECT value FROM fixture"))
        assert budget.rows_used == 17


def test_exact_row_allowance_accepts_exhausted_cursor():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("CREATE TABLE fixture(value)")
        connection.executemany("INSERT INTO fixture VALUES(?)", ((n,) for n in range(17)))
        budget = KnowledgeReadBudget(max_rows=17)
        with knowledge_read_operation(budget, None):
            rows = read_rows(connection.execute("SELECT value FROM fixture ORDER BY value"))
        assert rows == [(n,) for n in range(17)]
        assert budget.rows_used == 17


@pytest.mark.parametrize("row_count", (17, 18))
def test_public_service_exact_row_boundary(tmp_path, row_count):
    admitted = []
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("CREATE TABLE fixture(value)")
        connection.executemany("INSERT INTO fixture VALUES(?)", ((n,) for n in range(row_count)))
        def execute(paths, plan, snapshot, **kwargs):
            rows = read_rows(connection.execute(
                "SELECT value FROM fixture ORDER BY value LIMIT ?", (read_query_limit(100),)))
            admitted.extend(rows)
            return _result(plan, snapshot, "fixture")
        service = KnowledgeSearchService(KnowledgeStatePaths.from_directory(tmp_path),
            snapshot_collector=lambda *args, **kwargs: _snapshot("same"), search_executor=execute)
        budget = KnowledgeReadBudget(max_rows=17)
        if row_count == 17:
            result = service.search(KnowledgeQuery("fixture"), read_budget=budget)
            assert result.snapshot.snapshot_id == _snapshot("same").snapshot_id
            assert admitted == [(n,) for n in range(17)]
        else:
            with pytest.raises(KnowledgeReadBudgetExceeded, match="rows"):
                service.search(KnowledgeQuery("fixture"), read_budget=budget)
            assert admitted == []
        assert budget.rows_used == 17


def test_expensive_sql_observes_the_shared_deadline(tmp_path):
    path = tmp_path / "owner.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE fixture(value)")
        connection.commit()
    now = [0]
    def step():
        now[0] += 10_000_000
    budget = KnowledgeReadBudget(deadline_seconds=1, monotonic_clock=lambda: now[0])
    with pytest.raises(KnowledgeReadBudgetExceeded, match="deadline"):
        with knowledge_read_operation(budget, step), semantic_read_context():
            with semantic_database(path, readonly=True) as connection:
                connection.execute("""WITH RECURSIVE n(x) AS
                    (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100000000)
                    SELECT SUM(x) FROM n""").fetchone()
    assert budget.checkpoints > 10


def test_temporary_limit_rejects_before_snapshot_open(tmp_path, monkeypatch):
    from neocortex.persistence.sqlite_immutable import SQLiteReadSession
    path = tmp_path / "owner.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE fixture(value)")
        connection.commit()
    opened = []
    def forbidden(self):
        opened.append(self.path)
        raise AssertionError("snapshot must be rejected before copying")
    monkeypatch.setattr(SQLiteReadSession, "open", forbidden)
    with pytest.raises(KnowledgeReadBudgetExceeded, match="temporary"):
        with knowledge_read_operation(KnowledgeReadBudget(max_temporary_bytes=1), None):
            with semantic_read_context(SemanticReadContext()) as context:
                with context.acquire(path, mode="snapshot_temp"):
                    pytest.fail("snapshot unexpectedly opened")
    assert opened == []


def test_zero_temporary_allowance_permits_strict_zero_copy(tmp_path):
    path = tmp_path / "owner.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE fixture(value)")
        connection.commit()
    budget = KnowledgeReadBudget(max_temporary_bytes=0)
    with knowledge_read_operation(budget, None), semantic_read_context():
        with semantic_database(path, readonly=True) as connection:
            assert connection.execute("SELECT COUNT(*) FROM fixture").fetchone()[0] == 0
    assert budget.temporary_bytes_used == 0


def test_local_snapshot_cancellation_survives_ambient_allowance(tmp_path):
    from neocortex.persistence.sqlite_immutable import SQLiteSnapshotBudgetExceeded
    path = tmp_path / "owner.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE fixture(value)")
        connection.commit()
    local_calls = []
    def local_cancel():
        local_calls.append(1)
        return True
    with knowledge_read_operation(KnowledgeReadBudget(max_rows=10), None):
        context = SemanticReadContext(timeout_seconds=0.5, cancellation_check=local_cancel)
        assert context._budget.prepare_timeout_seconds == 0.5
        with pytest.raises(SQLiteSnapshotBudgetExceeded, match="cancelled"):
            with semantic_read_context(context), context.acquire(path, mode="snapshot_temp"):
                pytest.fail("local cancellation must remain authoritative")
    assert local_calls


def test_row_only_allowance_keeps_the_cached_backend(tmp_path, monkeypatch):
    from neocortex.semantic import semantic_service
    from tests.test_semantic_backend_supervisor import _model
    cached = object()
    monkeypatch.setattr(semantic_service._preparation, "backend", lambda *args, **kwargs: cached)
    with knowledge_read_operation(KnowledgeReadBudget(max_rows=10), None):
        assert semantic_service._backend(_model(), cache_dir=tmp_path,
            local_files_only=True, threads=1) is cached


def test_public_cancellation_terminates_blocked_encoder(tmp_path, monkeypatch):
    from neocortex.semantic import semantic_service
    from neocortex.semantic import semantic_backend_supervisor as supervisor
    from tests.test_semantic_backend_supervisor import _model, _blocked_startup_worker, _termination_observer
    terminated = _termination_observer(monkeypatch)
    now = [0]
    checkpoints = [0]
    def clock():
        return now[0]
    real_factory = supervisor.DeadlineEmbeddingBackend
    def create(*args, work_budget, **kwargs):
        check = work_budget.cancellation_check
        def expire():
            checkpoints[0] += 1
            if checkpoints[0] == 2:
                now[0] = 2_000_000_000
            return check()
        work_budget.cancellation_check = expire
        return real_factory(*args, work_budget=work_budget,
            worker_target=_blocked_startup_worker, **kwargs)
    monkeypatch.setattr(semantic_service, "DeadlineEmbeddingBackend", create)
    def execute(paths, plan, snapshot, **kwargs):
        backend = semantic_service._backend(
            _model(), cache_dir=tmp_path, local_files_only=True, threads=1,
        )
        # Coordinated models start only when useful work is requested.  Enter
        # that real supervised startup before expiring the public allowance.
        backend.text_token_counts(("deadline probe",))
        pytest.fail("expired encoder must not return a result")
    service = KnowledgeSearchService(KnowledgeStatePaths.from_directory(tmp_path),
        snapshot_collector=lambda *args, **kwargs: _snapshot("same"), search_executor=execute)
    with pytest.raises(KnowledgeReadBudgetExceeded, match="deadline"):
        service.search(KnowledgeQuery("fixture"), read_budget=KnowledgeReadBudget(
            deadline_seconds=1, monotonic_clock=clock))
    assert terminated == [True]
