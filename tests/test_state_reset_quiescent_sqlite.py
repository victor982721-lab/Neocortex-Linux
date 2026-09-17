"""Regression coverage for large, quiescent SQLite owners.

These tests use only temporary fixture state.  The sparse-file enlargement is
intentional: it exercises the real byte-size boundary without allocating a
multi-gigabyte database.  Residual WAL/SHM files are created as fixture input;
the tests never clean or alter a real NeoCortex state owner.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.persistence import sqlite_immutable
from neocortex.persistence.sqlite_immutable import (
    SQLiteReadMode,
    capture_sqlite_read_fence,
    preferred_sqlite_read_mode,
)
from neocortex.persistence.state_reset import (
    STATE_RESET_CONFIRMATION,
    StateResetResult,
    execute_state_reset,
    plan_state_reset,
)
from neocortex.semantic.semantic_schema import initialize_semantic_state
from neocortex.workflow.retention import planner as retention_module
from neocortex.workflow.retention.planner import RetentionPolicy, plan_retention
from tests.internal_paths_test_support import begin_signed_normal_run


_PLAN_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_LARGE_OWNER_BYTES = 256 * 1024 * 1024 + 1
_QUIESCENT_SHM_BYTES = 32_768


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


def _framework_owner(state: Path) -> Path:
    database = state / "framework.sqlite3"
    with FrameworkState(database):
        pass
    return database


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
            "live SQLite fixture failed to start: "
            f"stdout={ready!r} {stdout!r}, stderr={stderr!r}"
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


def test_state_reset_all_large_quiescent_residual_generates_digest_without_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reset planner must use immutable strict reads for this owner."""

    state = tmp_path / "state"
    state.mkdir()
    database = _framework_owner(state)
    _inflate_sparse(database)
    _residual_quiescent_sidecars(database)
    (state / "runtime-cache").mkdir()
    (state / "runtime-cache" / "fixture.bin").write_bytes(b"cache")

    copies = _copy_spy(monkeypatch)
    assert preferred_sqlite_read_mode(database) is SQLiteReadMode.IMMUTABLE_STRICT

    plan = plan_state_reset(state, scope="all")

    assert _PLAN_DIGEST.fullmatch(plan.plan_digest)
    assert database.stat().st_size > 256 * 1024 * 1024
    assert copies == []
    assert {target.target_id for target in plan.targets} >= {
        "sqlite:framework",
        "runtime-cache",
    }
    assert all(entry.path.is_relative_to(state) for entry in plan.entries)
    assert Path(f"{database}-wal").stat().st_size == 0
    assert Path(f"{database}-shm").stat().st_size == _QUIESCENT_SHM_BYTES


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


def test_scope_all_preserves_recovery_evidence_while_resetting_regenerable_state(
    tmp_path: Path,
) -> None:
    """A preserved uncertain file action is not discarded or a global block."""

    state = tmp_path / "state"
    state.mkdir()
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "source.fixture"
    source.write_text("fixture", encoding="utf-8")
    database = state / "framework.sqlite3"
    with FrameworkState(database) as framework:
        run_id = begin_signed_normal_run(framework, corpus)
        framework.fail_initial_run(run_id)
        action_id = framework.begin_file_action(
            run_id,
            "fixture",
            str(source),
            str(corpus / "target.fixture"),
            None,
            "fixture",
            True,
        )
        framework.require_file_action_recovery((action_id,), "fixture uncertain")

    plan = plan_state_reset(state, scope="all")
    assert plan.active_action_ids == ()
    assert plan.preserved_recovery_action_ids == (action_id,)
    result = execute_state_reset(
        state,
        scope="all",
        apply=True,
        plan_digest=plan.plan_digest,
        confirmation=STATE_RESET_CONFIRMATION,
    )
    assert isinstance(result, StateResetResult)
    assert result.status == "applied"
    with FrameworkState(database, existing_only=True) as framework:
        row = framework._connection.execute(
            "SELECT status FROM file_actions WHERE action_id=?", (action_id,)
        ).fetchone()
    assert row is not None and row[0] == "recovery_required"


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


def test_cli_state_reset_all_preview_payload_binds_targets_to_fixture_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The canonical CLI emits a digest and only state-directory targets."""

    from neocortex.api.cli import human

    state = tmp_path / "state"
    state.mkdir()
    database = _framework_owner(state)
    _inflate_sparse(database)
    _residual_quiescent_sidecars(database)
    (state / "runtime-cache").mkdir()
    (state / "runtime-cache" / "fixture.bin").write_bytes(b"cache")
    (state / "curation" / "checkpoints").mkdir(parents=True)
    (state / "curation" / "checkpoints" / "fixture.json").write_text(
        "{}", encoding="utf-8"
    )
    copies = _copy_spy(monkeypatch)

    exit_code = human.run_human_command(
        (
            "state",
            "reset",
            "--state-directory",
            str(state),
            "--scope",
            "all",
            "--json",
        )
    )

    output = capsys.readouterr()
    assert exit_code == 0, output.err
    payload = json.loads(output.out)
    assert payload["kind"] == "state-reset"
    assert payload["status"] == "preview"
    result = payload["result"]
    assert isinstance(result, dict)
    assert result["scope"] == "all"
    assert result["mode"] == "preview"
    assert _PLAN_DIGEST.fullmatch(result["plan_digest"])
    assert f"--plan-digest {result['plan_digest']}" in result["apply_options"]
    assert "backup_directory" not in result
    target_ids = {target["target_id"] for target in result["targets"]}
    assert target_ids >= {
        "sqlite:framework",
        "runtime-cache",
        "curation-checkpoints",
    }
    for target in result["targets"]:
        for entry in target["entries"]:
            assert Path(entry["path"]).is_relative_to(state)
    assert copies == []
    assert database.stat().st_size > 256 * 1024 * 1024
