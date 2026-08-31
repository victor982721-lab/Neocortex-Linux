from __future__ import annotations

import argparse
import io
import shutil
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from neocortex.api.cli.cli_direct import run_operational_status
from neocortex.interface.read.status import StatusRepository
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.orchestration.run_status import list_run_status
from neocortex.enumeration import JournalCursor
from neocortex.persistence.sqlite_immutable import ImmutableSQLiteUnavailable


def _clean_framework_copy(tmp_path: Path) -> Path:
    source = tmp_path / "source.sqlite3"
    with FrameworkState(source) as state:
        state.begin_initial_run(tmp_path, JournalCursor("C:", 1, 1))
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    state_directory = tmp_path / "state"
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"
    shutil.copyfile(source, database)
    for suffix in ("-journal", "-wal", "-shm"):
        Path(f"{database}{suffix}").unlink(missing_ok=True)
    return database


def _cli_args(state_directory: Path, *, status_json: bool = True) -> argparse.Namespace:
    return argparse.Namespace(
        state_directory=state_directory,
        status_limit=5,
        status_run=None,
        status_json=status_json,
    )


def test_status_queries_do_not_create_sqlite_sidecars(tmp_path: Path) -> None:
    database = _clean_framework_copy(tmp_path)
    state_directory = database.parent

    assert list_run_status(database, limit=1)
    assert StatusRepository(state_directory).recent_runs(limit=1)
    assert StatusRepository(state_directory).latest_event_details(1, "inventory") == {}

    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def test_cli_status_reports_active_wal_without_touching_it(tmp_path: Path) -> None:
    database = _clean_framework_copy(tmp_path)
    wal = Path(f"{database}-wal")
    wal.write_bytes(b"active-wal")

    with pytest.raises(ImmutableSQLiteUnavailable, match="non-empty WAL"):
        list_run_status(database, limit=1)

    output = io.StringIO()
    with redirect_stdout(output):
        exit_code = run_operational_status(_cli_args(database.parent))

    assert exit_code == 2
    assert "non-empty WAL" in output.getvalue()
    assert wal.read_bytes() == b"active-wal"
    assert wal.stat().st_size == len(b"active-wal")

