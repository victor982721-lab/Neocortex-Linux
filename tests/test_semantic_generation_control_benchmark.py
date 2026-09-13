"""Unit characterization for the benchmark's nested SQLite instrumentation."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

import benchmarks.semantic_generation_control_benchmark as benchmark
from neocortex.persistence.sqlite_cancellation import sqlite_cancellation_scope
from neocortex.persistence.sqlite_cancellation import SQLiteCancellationBridge


class _SharedOwner:
    """Small same-connection owner with real commit/rollback on outer exit."""

    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.depth = 0

    @contextlib.contextmanager
    def opened(self) -> Iterator[sqlite3.Connection]:
        self.depth += 1
        try:
            yield self.connection
            if self.depth == 1:
                self.connection.commit()
        except BaseException:
            if self.depth == 1:
                self.connection.rollback()
            raise
        finally:
            self.depth -= 1
            if self.depth == 0:
                self.connection.close()


def _components(owner: _SharedOwner) -> SimpleNamespace:
    def database(*_args: object, **_kwargs: object):
        return owner.opened()

    text_index = SimpleNamespace(
        semantic_database=database,
        SQLiteCancellationBridge=SQLiteCancellationBridge,
        sqlite_cancellation_scope=sqlite_cancellation_scope,
    )
    return SimpleNamespace(
        text_index=text_index,
        item_repository=SimpleNamespace(semantic_database=database),
        generation_repository=SimpleNamespace(semantic_database=database),
        generation_worker=SimpleNamespace(semantic_database=database),
    )


def _large_query(connection: sqlite3.Connection) -> None:
    connection.execute(
        """WITH RECURSIVE seq(value) AS (
            SELECT 1 UNION ALL SELECT value + 1 FROM seq WHERE value < 2500
        ) SELECT sum(value) FROM seq"""
    ).fetchone()


def test_owned_connection_observes_commit_after_owner_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _SharedOwner()
    components = _components(owner)
    monkeypatch.setattr(benchmark, "COMPONENTS", components)
    trace = benchmark.SQLTrace()

    with benchmark._trace_connections(trace):
        with components.text_index.semantic_database() as connection:
            connection.execute("BEGIN")
            connection.execute("CREATE TABLE values_table(value INTEGER)")
            connection.execute("INSERT INTO values_table VALUES (1)")

    assert trace.commit_statements >= 1
    assert trace.as_dict()["transactions"] >= 1
    assert id(owner.connection) not in benchmark._TRACE_CONNECTION_STATES


def test_owner_rollback_on_error_is_observed_before_closed_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _SharedOwner()
    components = _components(owner)
    monkeypatch.setattr(benchmark, "COMPONENTS", components)
    trace = benchmark.SQLTrace()

    with pytest.raises(RuntimeError, match="fixture rollback"):
        with benchmark._trace_connections(trace):
            with components.text_index.semantic_database() as connection:
                connection.execute("BEGIN")
                connection.execute("CREATE TABLE rollback_table(value INTEGER)")
                raise RuntimeError("fixture rollback")

    assert trace.rollback_statements >= 1
    assert trace.as_dict()["transactions"] >= 1


def test_nested_same_connection_keeps_outer_trace_commit_and_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _SharedOwner()
    components = _components(owner)
    monkeypatch.setattr(benchmark, "COMPONENTS", components)
    trace = benchmark.SQLTrace()

    with benchmark._trace_connections(trace):
        with components.text_index.semantic_database() as outer:
            outer.execute("CREATE TABLE nested_table(value INTEGER)")
            with components.item_repository.semantic_database() as inner:
                assert inner is outer
                inner.execute("INSERT INTO nested_table VALUES (1)")

            state = benchmark._TRACE_CONNECTION_STATES[id(outer)]
            bridge = components.text_index.SQLiteCancellationBridge(lambda: None)
            with components.text_index.sqlite_cancellation_scope(outer, bridge):
                _large_query(outer)
                with components.text_index.sqlite_cancellation_scope(outer, bridge):
                    _large_query(outer)
                assert len(state.progress_stack) == 1
                progress_before = trace.progress_handler_calls
                _large_query(outer)
                assert trace.progress_handler_calls > progress_before
            outer.execute("SELECT COUNT(*) FROM nested_table").fetchone()

    assert trace.total > 0
    assert trace.commit_statements >= 1
    assert trace.progress_handler_calls > 0
    assert id(owner.connection) not in benchmark._TRACE_CONNECTION_STATES


def test_private_environment_preserves_inherited_audit_lab_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment = {
        "NEOCORTEX_AUDIT_LAB_ROOT": str(tmp_path),
        "NEOCORTEX_CORPUS_ROOT": "/not-a-benchmark-input",
    }
    # Exercise the environment mapping without replacing the test process's
    # real HOME/XDG or C-level environment for sibling fixtures.
    monkeypatch.setattr(benchmark.os, "environ", environment)
    private = tmp_path / "private-run"
    benchmark._private_environment(private)

    assert environment["NEOCORTEX_AUDIT_LAB_ROOT"] == str(tmp_path)
    assert "NEOCORTEX_CORPUS_ROOT" not in environment
    for name in ("HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "TMPDIR", "HF_HOME"):
        selected = Path(environment[name])
        assert selected.is_relative_to(private)
        assert selected.is_dir()
