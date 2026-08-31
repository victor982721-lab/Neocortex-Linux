from __future__ import annotations

import argparse
import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

from neocortex.api.cli.cli_state_health import run_state_health
from neocortex.workflow.state_health import inspect_state_health


def _write_owner(path: Path, *, schema_version: int = 7) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES('schema_version',?)",
            (str(schema_version),),
        )
        connection.execute("CREATE TABLE sample(status TEXT NOT NULL)")
        connection.execute("INSERT INTO sample(status) VALUES('complete')")
        connection.commit()
    finally:
        connection.close()


def _args(state: Path, *, as_json: bool = True) -> argparse.Namespace:
    return argparse.Namespace(state_directory=state, state_health=True, state_health_json=as_json)


def test_state_health_reports_missing_and_healthy_owners_without_creating_state(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _write_owner(state / "framework.sqlite3", schema_version=22)
    before = (state / "framework.sqlite3").read_bytes()

    health = inspect_state_health(state)

    assert health.overall == "partial"
    assert health.healthy_count == 1
    assert health.missing_count == 12
    framework = next(owner for owner in health.owners if owner.name == "framework")
    assert framework.status == "healthy"
    assert framework.schema_version == 22
    assert framework.observations == {}
    assert (state / "framework.sqlite3").read_bytes() == before


def test_state_health_marks_orphaned_sidecars_without_opening_an_absent_owner(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    sidecar = state / "document_catalog.sqlite3-shm"
    sidecar.write_bytes(b"orphan")

    health = inspect_state_health(state)

    catalog = next(owner for owner in health.owners if owner.name == "catalog")
    assert catalog.status == "orphaned_sidecars"
    assert catalog.sidecars[0].suffix == "-shm"
    assert catalog.sidecars[0].size == len(b"orphan")
    assert sidecar.read_bytes() == b"orphan"


def test_state_health_blocks_nonempty_wal_before_immutable_read(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "framework.sqlite3"
    _write_owner(database)
    wal = state / "framework.sqlite3-wal"
    wal.write_bytes(b"active-wal")
    before = database.read_bytes()

    health = inspect_state_health(state)

    framework = next(owner for owner in health.owners if owner.name == "framework")
    assert framework.status == "blocked"
    assert "WAL" in (framework.detail or "")
    assert database.read_bytes() == before
    assert wal.read_bytes() == b"active-wal"


def test_state_health_json_is_structured_and_returns_two_for_partial_state(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _write_owner(state / "framework.sqlite3")
    output = io.StringIO()

    with redirect_stdout(output):
        exit_code = run_state_health(_args(state))

    payload = json.loads(output.getvalue())
    assert exit_code == 2
    assert payload["kind"] == "state-health"
    assert payload["overall"] == "partial"
    assert len(payload["owners"]) == 13
