"""Operation-scoped semantic reads reuse views without mixing publications."""

import sqlite3
from pathlib import Path

import pytest

from neocortex.semantic.semantic_schema import (
    SemanticReadContext,
    SemanticStateError,
    semantic_database,
    semantic_read_context,
)


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE fixture(value INTEGER)")
        connection.execute("INSERT INTO fixture VALUES(1)")


def _read(path: Path, context: SemanticReadContext | None = None) -> int:
    with semantic_database(
        path, readonly=True, read_mode="snapshot_temp", read_context=context,
    ) as connection:
        return int(connection.execute("SELECT value FROM fixture").fetchone()[0])


def test_nested_scope_reuses_one_snapshot_and_releases_at_operation_end(tmp_path: Path) -> None:
    path = tmp_path / "semantic.sqlite3"
    _database(path)
    original = path.read_bytes()
    with semantic_read_context() as context:
        assert _read(path) == 1
        with semantic_read_context() as nested:
            assert nested is context
            assert _read(path) == 1
        assert context.metrics["prepared_views"] == 1
        assert context.metrics["reused_views"] == 1
        assert context.metrics["retained_views"] == 1
    assert context.metrics["retained_views"] == 0
    assert context.metrics["retained_temporary_bytes"] == 0
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["semantic.sqlite3"]


def test_fence_drift_is_rejected_in_scope_but_new_operation_reads_new_publication(
    tmp_path: Path,
) -> None:
    path = tmp_path / "semantic.sqlite3"
    _database(path)
    with semantic_read_context() as context:
        assert _read(path) == 1
        with sqlite3.connect(path) as writer:
            writer.execute("UPDATE fixture SET value=2")
        with pytest.raises(SemanticStateError, match="changed within"):
            _read(path)
        # A facade may have translated the preceding error into a partial
        # response; the enclosing operation still detects publication drift.
        with pytest.raises(SemanticStateError, match="changed within"):
            context.verify_owner_fences()
        assert context.metrics["prepared_views"] == 1
    with semantic_read_context() as fresh:
        assert _read(path) == 2
        fresh.verify_owner_fences()
        assert fresh.metrics["prepared_views"] == 1


def test_explicit_context_and_exception_cleanup_do_not_leak_ambient_scope(tmp_path: Path) -> None:
    path = tmp_path / "semantic.sqlite3"
    _database(path)
    context = SemanticReadContext(generation=("fixture", 1))
    with pytest.raises(RuntimeError, match="fixture abort"):
        with semantic_read_context(context):
            assert _read(path, context) == 1
            raise RuntimeError("fixture abort")
    assert context.metrics["retained_views"] == 0
    with pytest.raises(RuntimeError, match="closed"):
        _read(path, context)
    with semantic_read_context() as fresh:
        assert fresh is not context
        assert _read(path) == 1


def test_snapshot_hit_rechecks_operation_cancellation(tmp_path: Path) -> None:
    from neocortex.persistence.sqlite_immutable import SQLiteSnapshotBudgetExceeded

    path = tmp_path / "semantic.sqlite3"
    _database(path)
    cancelled = [False]
    context = SemanticReadContext(cancellation_check=lambda: cancelled[0])
    with semantic_read_context(context):
        assert _read(path) == 1
        cancelled[0] = True
        with pytest.raises(SQLiteSnapshotBudgetExceeded, match="cancelled"):
            _read(path)
    assert context.metrics["retained_views"] == 0
