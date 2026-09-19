"""Large-owner retention uses bounded snapshots or fenced immutable reads."""
from __future__ import annotations
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import pytest
from neocortex.persistence import sqlite_immutable
from neocortex.persistence.sqlite_immutable import capture_sqlite_read_fence
from neocortex.semantic.semantic_schema import initialize_semantic_state
from neocortex.workflow.retention import planner as retention_module
from neocortex.workflow.retention.planner import RetentionPolicy, plan_retention

_LARGE_OWNER_BYTES = 256 * 1024 * 1024 + 1
_QUIESCENT_SHM_BYTES = 32768


def _inflate_sparse(path: Path) -> None:
    """Make a real SQLite owner exceed the copy budget without writing pages."""

    with path.open("r+b") as stream:
        stream.truncate(_LARGE_OWNER_BYTES)
    assert path.stat().st_size == _LARGE_OWNER_BYTES


def _residual_quiescent_sidecars(path: Path) -> None:
    """Install the exact closed-owner WAL/SHM residual under test."""

    Path(f"{path}-wal").write_bytes(b"")
    Path(f"{path}-shm").write_bytes(b"\0" * _QUIESCENT_SHM_BYTES)
    assert Path(f"{path}-wal").stat().st_size == 0
    assert Path(f"{path}-shm").stat().st_size == _QUIESCENT_SHM_BYTES


def _semantic_owner(state: Path, *, large: bool = True) -> Path:
    database = state / "semantic.sqlite3"
    initialize_semantic_state(database)
    if large:
        _inflate_sparse(database)
    return database


@contextmanager
def _external_live_writer(database: Path) -> Iterator[subprocess.Popen[str]]:
    """Hold a real WAL writer in another process until the test releases it."""

    script = """
import pathlib
import sqlite3
import sys

database = pathlib.Path(sys.argv[1])
connection = sqlite3.connect(database, timeout=5.0)
try:
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('BEGIN IMMEDIATE')
    wal = pathlib.Path(str(database) + '-wal')
    shm = pathlib.Path(str(database) + '-shm')
    if wal.stat().st_size != 0 or shm.stat().st_size != 32768:
        raise RuntimeError('fixture did not create the canonical WAL/SHM layout')
    print('READY', flush=True)
    sys.stdin.read(1)
    connection.rollback()
finally:
    connection.close()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(database)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    ready = process.stdout.readline().strip()
    if ready != "READY":
        stdout, stderr = process.communicate(timeout=5)
        raise AssertionError(
            f"live SQLite fixture failed to start: stdout={ready!r} {stdout!r}, stderr={stderr!r}"
        )
    try:
        assert process.poll() is None
        yield process
    finally:
        if process.poll() is None:
            assert process.stdin is not None
            process.stdin.write("x")
            process.stdin.flush()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        stderr = process.stderr.read() if process.stderr is not None else ""
        assert process.returncode == 0, stderr


def _copy_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Path]]:
    """Count detached snapshot copies while preserving the real copy seam."""

    calls: list[tuple[Path, Path]] = []
    real_copy = sqlite_immutable._copy_regular_file

    def spy(
        source: Path,
        destination: Path,
        *,
        budget_state: object | None = None,
    ) -> None:
        calls.append((Path(source), Path(destination)))
        real_copy(source, destination, budget_state=budget_state)  # type: ignore[arg-type]

    monkeypatch.setattr(sqlite_immutable, "_copy_regular_file", spy)
    return calls


def test_retention_large_quiescent_owner_without_sidecars_uses_strict_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large sidecar-free owner also bypasses the temporary byte budget."""

    state = tmp_path / "state"
    state.mkdir()
    database = _semantic_owner(state)
    assert not tuple(state.glob("semantic.sqlite3-*"))
    monkeypatch.setattr(retention_module, "DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES", 1)
    copies = _copy_spy(monkeypatch)

    plan = plan_retention(
        state,
        stores=("semantic",),
        now_ns=1_000,
        policy=RetentionPolicy(snapshot_max_temporary_bytes=1),
    )

    assert database.stat().st_size > 256 * 1024 * 1024
    assert plan.stores[0].status == "ready"
    assert plan.snapshot_metrics is not None
    assert plan.snapshot_metrics["prepared_views"] == 1
    assert plan.snapshot_metrics["peak_temporary_bytes"] == 0
    assert copies == []


def test_retention_large_quiescent_residual_uses_zero_copy_and_preserves_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retention can inspect a large closed owner without a temp snapshot."""

    state = tmp_path / "state"
    state.mkdir()
    database = _semantic_owner(state)
    _residual_quiescent_sidecars(database)
    before = capture_sqlite_read_fence(database)
    monkeypatch.setattr(retention_module, "DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES", 1)
    copies = _copy_spy(monkeypatch)

    plan = plan_retention(
        state,
        stores=("semantic",),
        now_ns=1_000,
        policy=RetentionPolicy(snapshot_max_temporary_bytes=1),
    )

    store = plan.stores[0]
    assert database.stat().st_size > 256 * 1024 * 1024
    assert store.status == "ready"
    assert store.database_bytes == _LARGE_OWNER_BYTES
    assert plan.snapshot_metrics is not None
    assert plan.snapshot_metrics["prepared_views"] == 1
    assert plan.snapshot_metrics["peak_temporary_bytes"] == 0
    assert plan.snapshot_metrics["retained_temporary_bytes"] == 0
    assert copies == []
    assert capture_sqlite_read_fence(database) == before
    assert Path(f"{database}-wal").stat().st_size == 0
    assert Path(f"{database}-shm").stat().st_size == _QUIESCENT_SHM_BYTES


@pytest.mark.parametrize(
    "layout",
    ("live-writer", "wal-only", "unexpected-shm", "nonempty-wal", "rollback-journal"),
)
def test_retention_active_or_ambiguous_large_owner_stays_within_temp_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
) -> None:
    """Unsafe ownership evidence remains fail-closed at the 256 MiB boundary."""

    state = tmp_path / "state"
    state.mkdir()
    database = _semantic_owner(state)
    monkeypatch.setattr(retention_module, "DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES", 1)
    copies = _copy_spy(monkeypatch)

    if layout == "live-writer":
        with _external_live_writer(database):
            plan = plan_retention(
                state,
                stores=("semantic",),
                now_ns=1_000,
                policy=RetentionPolicy(snapshot_max_temporary_bytes=1),
            )
    else:
        if layout == "wal-only":
            Path(f"{database}-wal").write_bytes(b"")
        elif layout == "unexpected-shm":
            Path(f"{database}-wal").write_bytes(b"")
            Path(f"{database}-shm").write_bytes(b"\0" * 16)
        elif layout == "nonempty-wal":
            Path(f"{database}-wal").write_bytes(b"active WAL")
        elif layout == "rollback-journal":
            Path(f"{database}-journal").write_bytes(b"active journal")
        else:  # pragma: no cover - parameter invariant
            raise AssertionError(layout)
        plan = plan_retention(
            state,
            stores=("semantic",),
            now_ns=1_000,
            policy=RetentionPolicy(snapshot_max_temporary_bytes=1),
        )

    store = plan.stores[0]
    assert store.status == "blocked"
    assert "temporary bytes budget exhausted" in (store.detail or "")
    assert plan.snapshot_metrics is not None
    assert plan.snapshot_metrics["prepared_views"] == 0
    assert plan.snapshot_metrics["peak_temporary_bytes"] == 0
    assert plan.snapshot_metrics["retained_temporary_bytes"] == 0
    assert copies == []
