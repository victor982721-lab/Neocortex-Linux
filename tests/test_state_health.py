from __future__ import annotations

import argparse
import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

from neocortex.api.cli.cli_state_health import run_state_health
from neocortex.workflow.state_health import inspect_state_health
from neocortex.safety.state_topology_contracts import STATE_STORE_REGISTRY


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


def _write_valid_framework_owner(path: Path) -> None:
    """Create the real current Framework contract in an isolated fixture."""

    from neocortex.persistence.framework_schema import initialize_framework_schema

    connection = sqlite3.connect(path)
    try:
        initialize_framework_schema(connection, lambda: None)
        # The fixture is closed before health observes it, so truncate any
        # writer-owned WAL left by schema creation rather than testing the
        # active-owner branch here.
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    for suffix in ("-wal", "-shm", "-journal"):
        (Path(f"{path}{suffix}")).unlink(missing_ok=True)


def _write_valid_pdf_owner(path: Path) -> None:
    """Create one canonical FTS-bearing owner in the isolated fixture."""

    from neocortex.capabilities.formats.pdf.pdf_schema import create_fresh_pdf_schema

    connection = sqlite3.connect(path)
    try:
        create_fresh_pdf_schema(connection)
        connection.commit()
    finally:
        connection.close()
    for suffix in ("-wal", "-shm", "-journal"):
        (Path(f"{path}{suffix}")).unlink(missing_ok=True)


def _args(state: Path, *, as_json: bool = True) -> argparse.Namespace:
    return argparse.Namespace(state_directory=state, state_health=True, state_health_json=as_json)


def test_state_health_reports_missing_and_healthy_owners_without_creating_state(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _write_valid_framework_owner(state / "framework.sqlite3")
    before = (state / "framework.sqlite3").read_bytes()

    health = inspect_state_health(state)

    assert health.overall == "partial"
    assert health.healthy_count == 1
    assert health.missing_count == 10
    framework = next(owner for owner in health.owners if owner.name == "framework")
    assert framework.status == "healthy"
    assert (
        framework.schema_version
        == STATE_STORE_REGISTRY.by_owner("framework").expected_schema_version
    )
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
    assert framework.status == "active"
    assert health.active_count == 1
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
    assert payload["schema_version"] == 2
    assert payload["overall"] == "partial"
    assert len(payload["owners"]) == 11


def test_state_health_uses_canonical_topology_for_schema_and_unknown_entries(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    pdf_schema = STATE_STORE_REGISTRY.by_owner("pdf").expected_schema_version
    docx_schema = STATE_STORE_REGISTRY.by_owner("docx").expected_schema_version
    _write_owner(state / "pdf.sqlite3", schema_version=pdf_schema + 1)
    _write_owner(state / "docx.sqlite3", schema_version=docx_schema - 1)
    (state / "audio.sqlite3").write_bytes(b"not a sqlite database")
    _write_owner(state / "unregistered.sqlite3", schema_version=1)

    health = inspect_state_health(state)
    owners = {owner.name: owner for owner in health.owners}

    assert owners["pdf"].status == "future"
    assert owners["docx"].status == "incompatible"
    assert owners["audio"].status == "corrupt"
    assert owners["unknown:unregistered.sqlite3"].status == "unknown"
    assert health.corrupt_count == 1
    assert health.unknown_count == 1
    assert health.overall == "partial"


def test_state_health_rejects_arbitrary_database_with_current_metadata(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    # The metadata integer alone must never be enough to claim a healthy owner.
    _write_owner(
        state / "framework.sqlite3",
        schema_version=STATE_STORE_REGISTRY.by_owner("framework").expected_schema_version,
    )

    health = inspect_state_health(state)

    framework = next(owner for owner in health.owners if owner.name == "framework")
    assert framework.status == "incompatible"
    assert "schema" in (framework.detail or "").casefold()


def test_state_health_rejects_broken_fts_shadow_as_corrupt(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "pdf.sqlite3"
    _write_valid_pdf_owner(database)
    connection = sqlite3.connect(database)
    try:
        shadow = connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='table' AND name LIKE 'page_fts_%' "
            "ORDER BY name LIMIT 1"
        ).fetchone()
        assert shadow is not None
        connection.execute('DROP TABLE "' + str(shadow[0]).replace('"', '""') + '"')
        connection.commit()
    finally:
        connection.close()

    health = inspect_state_health(state)

    pdf = next(owner for owner in health.owners if owner.name == "pdf")
    assert pdf.status in {"corrupt", "incompatible"}
    assert "fts" in (pdf.detail or "").casefold() or "schema" in (pdf.detail or "").casefold()


def test_state_health_blocks_sidecar_symlink_without_following_target(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "framework.sqlite3"
    _write_valid_framework_owner(database)
    target = tmp_path / "external-wal"
    target.write_bytes(b"must remain untouched")
    symlink = Path(f"{database}-wal")
    symlink.symlink_to(target)

    health = inspect_state_health(state)

    framework = next(owner for owner in health.owners if owner.name == "framework")
    assert framework.status == "blocked"
    assert "symlink" in (framework.detail or "").casefold()
    assert target.read_bytes() == b"must remain untouched"
    assert symlink.is_symlink()
