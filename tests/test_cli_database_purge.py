"""Canonical CLI contracts for the explicit NeoCortex database purge."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from neocortex.api.cli import human
from neocortex.interface.entrypoint import entrypoint
from neocortex.persistence.database_purge import DATABASE_PURGE_CONFIRMATION


def _create_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE payload(value TEXT NOT NULL)")
        connection.execute("INSERT INTO payload(value) VALUES('ok')")


def test_database_purge_defaults_to_a_read_only_json_preview(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _create_database(state / "image.sqlite3")

    assert entrypoint(
        (
            "databases",
            "purge",
            "--state-directory",
            str(state),
            "--store",
            "image",
            "--json",
        )
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "preview"
    assert payload["stores"] == ["image"]
    assert payload["file_count"] == 1
    assert (state / "image.sqlite3").is_file()


def test_database_purge_preview_returns_two_when_a_writer_holds_framework_lock(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _create_database(state / "image.sqlite3")
    lock = state / "framework.lock"
    stream = lock.open("a+b", buffering=0)
    try:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert entrypoint(
            (
                "databases",
                "purge",
                "--state-directory",
                str(state),
                "--store",
                "image",
                "--json",
            )
        ) == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["lock_conflicts"] == [str(lock)]
    finally:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def test_database_purge_requires_apply_and_exact_confirmation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _create_database(state / "image.sqlite3")

    assert entrypoint(
        (
            "database",
            "purge",
            "--state-directory",
            str(state),
            "--store",
            "image",
            "--apply",
            "--json",
        )
    ) == 2
    assert "DELETE_DATABASES" in capsys.readouterr().err
    assert (state / "image.sqlite3").is_file()


def test_database_purge_cli_applies_with_confirmation_and_reports_backup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _create_database(state / "image.sqlite3")
    backup = tmp_path / "backup"

    assert entrypoint(
        (
            "databases",
            "purge",
            "--state-directory",
            str(state),
            "--store",
            "image",
            "--backup-directory",
            str(backup),
            "--apply",
            "--confirm-database-purge",
            DATABASE_PURGE_CONFIRMATION,
            "--json",
        )
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "applied"
    assert payload["deleted_file_count"] == 1
    assert payload["backup_directory"] == str(backup)
    assert not (state / "image.sqlite3").exists()


def test_database_purge_parser_exposes_both_command_spellings() -> None:
    parser = human.build_human_parser()
    action = parser._subparsers._group_actions[0]
    choices = action.choices
    assert "databases" in choices
    assert "database" in choices
    assert "purge" in choices["databases"]._subparsers._group_actions[0].choices
