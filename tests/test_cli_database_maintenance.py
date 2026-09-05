"""Stable human CLI contracts for state backup, restore and status."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.capabilities.formats.image.state import initialize_image_state
from neocortex.interface.entrypoint import entrypoint
from neocortex.persistence.database_purge import (
    DATABASE_RESTORE_CONFIRMATION,
)


def _create_database(path: Path, value: str = "before") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    initialize_image_state(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("INSERT INTO metadata VALUES('fixture_payload', ?)", (value,))


def _json_output(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def test_database_status_is_sidecar_safe_and_reports_publication_epoch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    before = tuple(item.name for item in state.iterdir())

    assert (
        entrypoint(
            (
                "databases",
                "status",
                "--state-directory",
                str(state),
                "--store",
                "image",
                "--json",
            )
        )
        == 4
    )

    payload = _json_output(capsys)
    assert payload["schema"] == "neocortex.state-maintenance/v1"
    assert payload["operation"] == "database-status"
    assert payload["read_only"] is True
    result = payload["result"]
    assert isinstance(result, dict)
    assert result["stores"] == ["image"]
    assert result["state_epoch"]["epoch"] == 0
    assert tuple(item.name for item in state.iterdir()) == before


def test_database_backup_preview_never_creates_destination(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    backup = tmp_path / "backup"

    assert (
        entrypoint(
            (
                "databases",
                "backup",
                "--state-directory",
                str(state),
                "--backup-directory",
                str(backup),
                "--store",
                "image",
                "--json",
            )
        )
        == 4
    )

    payload = _json_output(capsys)
    assert payload["operation"] == "database-backup"
    assert payload["read_only"] is True
    result = payload["result"]
    assert isinstance(result, dict)
    assert result["requires_confirmation"] is True
    assert not backup.exists()


def test_database_backup_apply_requires_explicit_confirmation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    backup = tmp_path / "backup"

    assert (
        entrypoint(
            (
                "databases",
                "backup",
                "--state-directory",
                str(state),
                "--backup-directory",
                str(backup),
                "--store",
                "image",
                "--apply",
                "--json",
            )
        )
        == 2
    )

    payload = _json_output(capsys)
    assert payload["error"]["code"] == "DatabasePurgeError"
    assert not backup.exists()


def test_database_backup_and_restore_cli_bind_digest_and_confirmation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "image.sqlite3"
    _create_database(database)
    backup = tmp_path / "backup"

    assert (
        entrypoint(
            (
                "database",
                "backup",
                "--state-directory",
                str(state),
                "--backup-directory",
                str(backup),
                "--store",
                "image",
                "--apply",
                "--confirm-database-backup",
                "BACKUP_DATABASES",
                "--json",
            )
        )
        == 0
    )
    backup_payload = _json_output(capsys)
    backup_result = backup_payload["result"]
    assert isinstance(backup_result, dict)
    manifest = Path(str(backup_result["manifest"]))
    manifest_sha256 = str(backup_result["manifest_sha256"])
    assert manifest.is_file()

    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE metadata SET value='changed' WHERE key='fixture_payload'")

    assert (
        entrypoint(
            (
                "databases",
                "restore",
                "--state-directory",
                str(state),
                "--backup-directory",
                str(backup),
                "--store",
                "image",
                "--manifest-sha256",
                manifest_sha256,
                "--json",
            )
        )
        == 0
    )
    preview_payload = _json_output(capsys)
    assert preview_payload["read_only"] is True
    assert preview_payload["result"]["mode"] == "preview"

    assert (
        entrypoint(
            (
                "databases",
                "restore",
                "--state-directory",
                str(state),
                "--backup-directory",
                str(backup),
                "--store",
                "image",
                "--manifest-sha256",
                manifest_sha256,
                "--apply",
                "--confirm-database-restore",
                DATABASE_RESTORE_CONFIRMATION,
                "--json",
            )
        )
        == 0
    )
    restore_payload = _json_output(capsys)
    assert restore_payload["read_only"] is False
    assert restore_payload["result"]["mode"] == "applied"
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='fixture_payload'"
        ).fetchone() == ("before",)
