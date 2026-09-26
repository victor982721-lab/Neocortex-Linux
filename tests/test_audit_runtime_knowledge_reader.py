"""Knowledge reader regressions for quiescent residual SQLite sidecars."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from neocortex.knowledge import knowledge_snapshot as snapshot_module
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    capture_sqlite_immutable_fence,
)
from neocortex.semantic.semantic_schema import initialize_semantic_state


def _semantic_spec():
    return snapshot_module._owner_spec(
        "semantic",
        snapshot_module._validate_semantic,
        snapshot_module._OWNER_VALIDATORS["semantic"][1],
    )


def _residual_semantic_owner(root: Path) -> Path:
    database = root / "semantic.sqlite3"
    initialize_semantic_state(database)
    # A quiescent WAL owner may retain this exact residual layout.  These
    # sidecars are fixture evidence only; no production owner is opened.
    Path(f"{database}-wal").write_bytes(b"")
    Path(f"{database}-shm").write_bytes(b"\0" * 32_768)
    return database


def _owner_bytes(database: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in database.parent.glob(f"{database.name}*")
        if path.is_file()
    }


def test_quiescent_residual_wal_reuses_its_held_reader_without_self_blocking(
    tmp_path: Path,
) -> None:
    database = _residual_semantic_owner(tmp_path)
    before = _owner_bytes(database)
    cancellation = snapshot_module._CancellationController(None)

    owner, models = snapshot_module._capture_available_owner(
        database,
        _semantic_spec(),
        attempt=1,
        between_observations=None,
        cancellation=cancellation,
        immutable=None,
    )

    assert owner.state.value == "available"
    assert models == ()
    assert _owner_bytes(database) == before


def test_residual_wal_with_a_live_writer_still_abstains(
    tmp_path: Path,
) -> None:
    database = _residual_semantic_owner(tmp_path)
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL").fetchone()
        writer.execute(
            "INSERT INTO metadata(key,value) VALUES('live-fixture','1')"
        )
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        with pytest.raises(ImmutableSQLiteUnavailable, match="owner process is active"):
            capture_sqlite_immutable_fence(database)
    finally:
        writer.close()
