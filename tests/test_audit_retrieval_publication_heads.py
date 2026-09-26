"""Regression coverage for bounded Semantic publication-head snapshots."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

import neocortex.semantic.semantic_publication_heads as publication_heads
from neocortex.persistence.sqlite_immutable import SQLiteSnapshotBudget
from neocortex.semantic.semantic_publication_heads import (
    PublicationHeadsDriftError,
    PublicationHeadsStateError,
    _semantic_owner_lease,
    observe_integrated_owner_heads,
)
from neocortex.semantic.semantic_schema import initialize_semantic_state


def test_publication_heads_retry_one_transient_snapshot_budget_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A final WAL checkpoint may make the next bounded read strict and cheap."""

    database = tmp_path / "semantic.sqlite3"
    initialize_semantic_state(database)
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO metadata(key,value) VALUES('transient-wal','fixture')"
        )
        writer.commit()

        original_open = publication_heads.SQLiteReadSession.open
        calls = 0

        def fail_once(session: object):
            nonlocal calls
            calls += 1
            if calls == 1:
                from neocortex.persistence.sqlite_immutable import SQLiteSnapshotBudgetExceeded

                raise SQLiteSnapshotBudgetExceeded("temporary_bytes")
            return original_open(session)  # type: ignore[arg-type]

        monkeypatch.setattr(publication_heads.SQLiteReadSession, "open", fail_once)
        observed = observe_integrated_owner_heads(
            database.parent,
            snapshot_budget=SQLiteSnapshotBudget(max_temporary_bytes=64 * 1024 * 1024),
        )

        assert calls == 2
        assert observed[0].owner == "semantic"
        assert observed[0].revision == 0
    finally:
        writer.close()


def test_owner_lease_rejects_path_replacement_before_projection(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "semantic.sqlite3"
    initialize_semantic_state(database)
    replacement = tmp_path / "replacement.sqlite3"
    initialize_semantic_state(replacement)

    with pytest.raises(PublicationHeadsDriftError):
        with _semantic_owner_lease(state) as lease:
            assert lease is not None
            replacement.replace(database)
            observe_integrated_owner_heads(state, _writer_lease=lease)


def test_zero_byte_semantic_owner_keeps_empty_baseline_without_sidecars(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "semantic.sqlite3"
    database.touch()

    observed = observe_integrated_owner_heads(state)

    assert observed[0].owner == "semantic"
    assert observed[0].revision == 0
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
    with _semantic_owner_lease(state) as lease:
        assert lease is None


def test_owner_lease_rejects_replacement_between_connect_and_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "semantic.sqlite3"
    initialize_semantic_state(database)
    replacement = tmp_path / "replacement.sqlite3"
    initialize_semantic_state(replacement)
    original_connect = publication_heads.sqlite3.connect
    replaced = False

    def connect_then_replace(*args: object, **kwargs: object):
        nonlocal replaced
        connection = original_connect(*args, **kwargs)
        if not replaced:
            replaced = True
            replacement.replace(database)
        return connection

    monkeypatch.setattr(publication_heads.sqlite3, "connect", connect_then_replace)
    with pytest.raises(PublicationHeadsDriftError, match="immediately after lease connect"):
        with _semantic_owner_lease(state):
            pass
    assert replaced is True


def test_owner_lease_checkpoint_interrupts_preflight_before_waiting(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    initialize_semantic_state(state / "semantic.sqlite3")
    calls = 0

    def cancel() -> None:
        nonlocal calls
        calls += 1
        raise KeyboardInterrupt("fixture cancellation")

    with pytest.raises(KeyboardInterrupt, match="fixture cancellation"):
        with _semantic_owner_lease(state, checkpoint=cancel):
            pytest.fail("cancelled lease must not be yielded")
    assert calls == 1


def test_projection_rejects_unregistered_raw_sqlite_connection(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "semantic.sqlite3"
    initialize_semantic_state(database)
    connection = sqlite3.connect(database)
    try:
        with pytest.raises(PublicationHeadsStateError, match="authenticated lease"):
            observe_integrated_owner_heads(state, _writer_lease=connection)  # type: ignore[arg-type]
    finally:
        connection.close()


def test_lease_closes_connection_when_progress_restore_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    initialize_semantic_state(state / "semantic.sqlite3")
    from neocortex.persistence.sqlite_writer_snapshot import SQLiteProgressConnection

    original = SQLiteProgressConnection.set_progress_handler
    captured: list[sqlite3.Connection] = []

    def fail_restore(self, handler, n, /):
        if handler is None and n == 0:
            raise RuntimeError("fixture progress restore failure")
        return original(self, handler, n)

    monkeypatch.setattr(SQLiteProgressConnection, "set_progress_handler", fail_restore)
    with pytest.raises(RuntimeError, match="progress restore failure"):
        with _semantic_owner_lease(state) as lease:
            assert lease is not None
            captured.append(lease.connection)
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].execute("SELECT 1")


def test_public_heads_read_hot_rollback_journal_without_mutating_owner(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "semantic.sqlite3"
    initialize_semantic_state(database)
    writer = sqlite3.connect(database)
    try:
        assert str(writer.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower() == "delete"
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE metadata SET value='uncommitted' WHERE key='schema_version'"
        )
        before = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in state.iterdir()
            if path.is_file()
        }

        observed = observe_integrated_owner_heads(state)

        after = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in state.iterdir()
            if path.is_file()
        }
        assert observed[0].revision == 0
        assert after == before
    finally:
        writer.rollback()
        writer.close()
