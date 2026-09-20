"""Public handling for an old, non-migratable Framework state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from neocortex.interface.entrypoint import entrypoint
from neocortex.persistence.framework_schema import (
    FrameworkStateIncompatible,
    SCHEMA_VERSION,
    initialize_framework_schema,
)


def _legacy_state(tmp_path: Path, version: int = 24) -> tuple[Path, Path]:
    root = tmp_path / "corpus"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    database = state / "framework.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            f"""
            CREATE TABLE metadata(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            INSERT INTO metadata VALUES('schema_version','{version}');
            CREATE TABLE sentinel(value TEXT NOT NULL);
            INSERT INTO sentinel VALUES('preserve');
            """
        )
    return root, state


def _read_legacy_markers(database: Path) -> tuple[str, str]:
    with sqlite3.connect(database) as connection:
        return (
            str(connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]),
            str(connection.execute("SELECT value FROM sentinel").fetchone()[0]),
        )


def test_initialize_old_schema_raises_typed_error_without_mutating_state(tmp_path: Path) -> None:
    _root, state = _legacy_state(tmp_path)
    database = state / "framework.sqlite3"
    before = database.read_bytes()
    connection = sqlite3.connect(database)
    try:
        with pytest.raises(FrameworkStateIncompatible) as raised:
            initialize_framework_schema(connection, lambda: pytest.fail("must not initialize"))
        assert raised.value.observed_schema == 24
        assert raised.value.expected_schema == SCHEMA_VERSION == 25
        assert raised.value.action == "factory_reset_required"
        assert raised.value.code == "factory_reset_required"
    finally:
        connection.close()
    assert database.read_bytes() == before
    assert _read_legacy_markers(database) == ("24", "preserve")


def test_public_cli_reports_factory_reset_without_traceback_or_auto_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, state = _legacy_state(tmp_path)
    database = state / "framework.sqlite3"
    before = database.read_bytes()
    monkeypatch.setenv("NEOCORTEX_PROGRESS_STREAM", "1")

    assert entrypoint(["--all", "--root", str(root), "--state-directory", str(state)]) == 2

    output = capsys.readouterr()
    combined = output.out + output.err
    assert "ERROR factory_reset_required" in output.err
    assert "schema 24" in output.err
    assert "schema 25" in output.err
    assert "Neocortex --factory-reset" in output.err
    assert "Traceback" not in combined
    assert _read_legacy_markers(database) == ("24", "preserve")
    assert database.read_bytes() == before


def test_public_cli_json_reports_typed_schema_error(tmp_path: Path, monkeypatch, capsys) -> None:
    root, state = _legacy_state(tmp_path)
    database = state / "framework.sqlite3"
    before = database.read_bytes()
    monkeypatch.setenv("NEOCORTEX_PROGRESS_STREAM", "1")

    assert (
        entrypoint(
            [
                "--all",
                "--json",
                "--root",
                str(root),
                "--state-directory",
                str(state),
            ]
        )
        == 2
    )

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["status"] == "failed"
    assert payload["completion"] == "incomplete"
    assert payload["error_code"] == "factory_reset_required"
    assert payload["observed_schema"] == 24
    assert payload["expected_schema"] == 25
    assert payload["action"] == "factory_reset_required"
    assert payload["exit_code"] == 2
    assert "Traceback" not in output.out + output.err
    assert _read_legacy_markers(database) == ("24", "preserve")
    assert database.read_bytes() == before


def test_future_schema_remains_a_distinct_unsupported_error(tmp_path: Path) -> None:
    _root, state = _legacy_state(tmp_path, version=SCHEMA_VERSION + 1)
    with sqlite3.connect(state / "framework.sqlite3") as connection:
        with pytest.raises(RuntimeError, match="unsupported") as raised:
            initialize_framework_schema(connection, lambda: None)
    assert not isinstance(raised.value, FrameworkStateIncompatible)
