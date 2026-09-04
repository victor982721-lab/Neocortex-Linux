"""Focused read-only restore-preview tests over temporary fixtures only."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import neocortex.curation.recovery as recovery
from neocortex.curation.application import apply_authorization_grant
from neocortex.curation.recovery import restore_curation_preview
from neocortex.persistence.framework_state_writer import FrameworkState
from tests.internal_paths_test_support import begin_signed_normal_run
from tests.test_curation_application import FixtureTrashBackend, _fixture


def _owner_bytes(database: Path) -> dict[str, object]:
    """Capture only fixture owner bytes and exact sidecar presence/metadata."""

    result: dict[str, object] = {}
    for path in (
        database,
        Path(f"{database}-journal"),
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
    ):
        if not os.path.lexists(path):
            result[path.name] = None
            continue
        metadata = path.lstat()
        assert metadata.st_mode & 0o170000 == 0o100000
        result[path.name] = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            path.read_bytes(),
        )
    return result


def _prepared_restore_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, int]:
    state, corpus, trash, framework, grant_id = _fixture(tmp_path)
    with FrameworkState(framework) as framework_state:
        run_id = begin_signed_normal_run(framework_state, corpus)
        result = apply_authorization_grant(
            state,
            framework,
            grant_id,
            run_id=run_id,
            backend=FixtureTrashBackend(trash, structured=True),
            state=framework_state,
            clock_ns=lambda: 4_000,
        )
    action_id = result.effects[0].action_id
    assert action_id is not None
    return state, corpus, trash, framework, action_id


def test_restore_preview_does_not_construct_framework_state_or_write_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state, _corpus, _trash, framework, action_id = _prepared_restore_fixture(tmp_path)
    before = _owner_bytes(framework)

    def forbidden_framework_state(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("restore preview must not construct FrameworkState")

    monkeypatch.setattr(recovery, "FrameworkState", forbidden_framework_state)

    payload = restore_curation_preview(framework, action_id)

    assert payload["read_only"] is True
    assert payload["restorable"] is True
    assert _owner_bytes(framework) == before


def test_restore_preview_uses_snapshot_for_active_wal_without_touching_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state, _corpus, _trash, framework, action_id = _prepared_restore_fixture(tmp_path)
    writer = sqlite3.connect(framework)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "UPDATE file_actions SET detail='wal-probe' WHERE action_id=?",
            (action_id,),
        )
        writer.commit()
        wal = Path(f"{framework}-wal")
        assert wal.is_file() and wal.stat().st_size > 0
        before = _owner_bytes(framework)

        def forbidden_framework_state(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("restore preview must not construct FrameworkState")

        monkeypatch.setattr(recovery, "FrameworkState", forbidden_framework_state)

        payload = restore_curation_preview(framework, action_id)

        assert payload["read_only"] is True
        assert payload["restorable"] is True
        assert _owner_bytes(framework) == before
    finally:
        writer.close()
