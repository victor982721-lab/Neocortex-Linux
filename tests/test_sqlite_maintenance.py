from __future__ import annotations

import errno
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from neocortex import sqlite_cancellation as legacy_cancellation
from neocortex import sqlite_cancellation as shared_cancellation
from neocortex import sqlite_integrity as integrity_module
from neocortex.sqlite_backup import (
    MAX_BACKUP_PAGES_PER_STEP,
    SQLiteBackupPolicy,
    SQLiteBackupProgress,
    SQLiteBackupPublicationError,
    SQLiteBackupVerificationError,
    backup_sqlite_online,
)
from neocortex.sqlite_integrity import (
    MAX_REPORTED_ISSUES,
    SQLiteIntegrityPolicy,
    check_sqlite_integrity,
)


class _InjectedAbort(BaseException):
    pass


def _staging_artifacts(directory: Path) -> tuple[Path, ...]:
    return tuple(directory.glob(".neocortex-sqlite-backup-*"))


def _create_database(
    path: Path,
    *,
    row_count: int = 1,
    keep_open: bool = False,
) -> sqlite3.Connection | None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA page_size=512")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        "CREATE TABLE payload(identifier INTEGER PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO payload(value) VALUES(?)",
        ((f"row-{index}-" + "x" * 900,) for index in range(row_count)),
    )
    connection.commit()
    if keep_open:
        return connection
    connection.close()
    return None


# region [01] Canonical cancellation compatibility


def test_operational_cancellation_is_an_identity_preserving_facade() -> None:
    assert (
        legacy_cancellation.CancellationCheck is shared_cancellation.CancellationCheck
    )
    assert (
        legacy_cancellation.DEFAULT_PROGRESS_INSTRUCTIONS
        is shared_cancellation.DEFAULT_PROGRESS_INSTRUCTIONS
    )
    assert (
        legacy_cancellation.SQLiteCancellationBridge
        is shared_cancellation.SQLiteCancellationBridge
    )
    assert (
        legacy_cancellation.sqlite_cancellation_scope
        is shared_cancellation.sqlite_cancellation_scope
    )


def test_cancellation_scope_reraises_exact_base_exception_and_clears_handler() -> None:
    connection = sqlite3.connect(":memory:")
    abort = _InjectedAbort("stop SQLite")
    calls = 0

    def cancel() -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise abort

    bridge = shared_cancellation.SQLiteCancellationBridge(cancel)
    try:
        with pytest.raises(_InjectedAbort) as captured:
            with shared_cancellation.sqlite_cancellation_scope(
                connection,
                bridge,
                instructions=1,
            ):
                connection.execute(
                    "WITH RECURSIVE values_(value) AS ("
                    "VALUES(1) UNION ALL SELECT value + 1 FROM values_ "
                    "WHERE value < 100000) SELECT SUM(value) FROM values_"
                ).fetchone()
        assert captured.value is abort
        assert connection.execute("SELECT 1").fetchone() == (1,)
    finally:
        connection.close()


class _ProgressHandlerCleanupConnection:
    def __init__(self, clear_error: BaseException) -> None:
        self.clear_error = clear_error
        self.events: list[tuple[bool, int]] = []

    def set_progress_handler(self, callback: object, instructions: int) -> None:
        self.events.append((callback is not None, instructions))
        if callback is None:
            raise self.clear_error


@pytest.mark.parametrize("mapped_cancellation", (False, True))
def test_cancellation_scope_preserves_exact_primary_when_handler_clear_fails(
    mapped_cancellation: bool,
) -> None:
    primary = _InjectedAbort("stop SQLite")
    interrupted = sqlite3.OperationalError("interrupted")
    clear_error = RuntimeError("clear failed")
    connection: Any = _ProgressHandlerCleanupConnection(clear_error)

    def cancel() -> None:
        if mapped_cancellation:
            raise primary

    bridge = shared_cancellation.SQLiteCancellationBridge(cancel)
    with pytest.raises(_InjectedAbort) as raised:
        with shared_cancellation.sqlite_cancellation_scope(connection, bridge):
            if mapped_cancellation:
                assert bridge.sqlite_progress() == 1
                raise interrupted
            raise primary

    assert raised.value is primary
    if mapped_cancellation:
        assert primary.__cause__ is interrupted
    assert primary.__notes__ == [
        "SQLite progress handler cleanup failed: RuntimeError: clear failed"
    ]
    assert connection.events == [(True, 1_000), (False, 0)]


def test_cancellation_scope_propagates_exact_handler_clear_failure_without_primary() -> (
    None
):
    clear_error = RuntimeError("clear failed")
    connection: Any = _ProgressHandlerCleanupConnection(clear_error)
    bridge = shared_cancellation.SQLiteCancellationBridge(lambda: None)

    with pytest.raises(RuntimeError) as raised:
        with shared_cancellation.sqlite_cancellation_scope(connection, bridge):
            pass

    assert raised.value is clear_error
    assert getattr(clear_error, "__notes__", ()) == ()
    assert connection.events == [(True, 1_000), (False, 0)]


# endregion [01]


# region [02] Bounded integrity reports


def test_integrity_report_is_complete_read_only_and_handles_special_paths(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state #owner ü.sqlite3"
    _create_database(database, row_count=4)
    before = database.read_bytes()

    report = check_sqlite_integrity(database)

    assert report.database_path == database.resolve()
    assert report.quick_check_errors == ()
    assert report.quick_check_observed_error_count == 0
    assert report.quick_check_complete is True
    assert report.quick_check_truncated is False
    assert report.foreign_key_violations == ()
    assert report.foreign_key_observed_violation_count == 0
    assert report.foreign_key_check_complete is True
    assert report.foreign_key_check_truncated is False
    assert report.complete is True
    assert report.healthy is True
    assert database.read_bytes() == before
    with pytest.raises(FrozenInstanceError):
        report.quick_check_complete = False  # type: ignore[misc]


def test_integrity_foreign_key_details_are_bounded_and_explicitly_truncated(
    tmp_path: Path,
) -> None:
    database = tmp_path / "foreign-keys.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("CREATE TABLE parent(identifier INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE child("
            "identifier INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL "
            "REFERENCES parent(identifier))"
        )
        connection.executemany(
            "INSERT INTO child(parent_id) VALUES(?)",
            ((identifier,) for identifier in range(10, 15)),
        )
        connection.commit()
    finally:
        connection.close()

    report = check_sqlite_integrity(
        database,
        policy=SQLiteIntegrityPolicy(max_foreign_key_violations=2),
    )

    assert report.quick_check_errors == ()
    assert len(report.foreign_key_violations) == 2
    assert all(item.table == "child" for item in report.foreign_key_violations)
    assert report.foreign_key_observed_violation_count == 3
    assert report.foreign_key_check_complete is False
    assert report.foreign_key_check_truncated is True
    assert report.complete is False
    assert report.healthy is False


def test_integrity_quick_check_truncation_is_reported_through_public_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Cursor:
        def __init__(self, rows: list[tuple[Any, ...]]) -> None:
            self.rows = rows
            self.closed = False

        def __iter__(self) -> Iterator[tuple[Any, ...]]:
            return iter(self.rows)

        def close(self) -> None:
            self.closed = True

    class _Connection:
        def __init__(self) -> None:
            self.in_transaction = False
            self.closed = False
            self.handlers: list[tuple[object, int]] = []
            self.statements: list[str] = []

        def execute(self, statement: str) -> _Cursor:
            self.statements.append(statement)
            if statement == "BEGIN":
                self.in_transaction = True
                return _Cursor([])
            if statement.startswith("PRAGMA quick_check"):
                return _Cursor([("first",), ("second",), ("third",)])
            if statement == "PRAGMA foreign_key_check":
                return _Cursor([])
            raise AssertionError(statement)

        def rollback(self) -> None:
            self.in_transaction = False

        def close(self) -> None:
            self.closed = True

        def set_progress_handler(self, callback: object, steps: int) -> None:
            self.handlers.append((callback, steps))

    connection = _Connection()
    monkeypatch.setattr(
        integrity_module,
        "connect_sqlite",
        lambda *_args, **_kwargs: connection,
    )

    report = check_sqlite_integrity(
        tmp_path / "synthetic.sqlite3",
        policy=SQLiteIntegrityPolicy(max_quick_check_errors=2),
        cancellation_check=lambda: None,
    )

    assert report.quick_check_errors == ("first", "second")
    assert report.quick_check_observed_error_count == 3
    assert report.quick_check_complete is False
    assert report.quick_check_truncated is True
    assert report.healthy is False
    assert "PRAGMA quick_check(3)" in connection.statements
    assert connection.handlers[-1] == (None, 0)
    assert connection.closed is True


def test_integrity_cancellation_is_exact_and_missing_source_is_never_created(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    _create_database(database, row_count=100)
    abort = _InjectedAbort("cancel integrity")

    def cancel() -> None:
        raise abort

    with pytest.raises(_InjectedAbort) as captured:
        check_sqlite_integrity(database, cancellation_check=cancel)
    assert captured.value is abort

    missing = tmp_path / "missing" / "state.sqlite3"
    with pytest.raises(sqlite3.OperationalError, match="open database"):
        check_sqlite_integrity(missing)
    assert not missing.exists()
    assert not missing.parent.exists()


def test_integrity_rejects_corrupt_header_without_changing_bytes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "corrupt.sqlite3"
    original = b"not a sqlite database\x00" * 8
    database.write_bytes(original)

    with pytest.raises(sqlite3.DatabaseError):
        check_sqlite_integrity(database)

    assert database.read_bytes() == original


# endregion [02]


# region [03] Verified online backup and no-replace publication


def test_online_backup_includes_committed_wal_and_excludes_uncommitted_rows(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source #owner ü.sqlite3"
    destination = tmp_path / "backup #owner ü.sqlite3"
    writer = _create_database(source, row_count=180, keep_open=True)
    assert writer is not None
    progress: list[SQLiteBackupProgress] = []
    try:
        assert Path(f"{source}-wal").stat().st_size > 0
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO payload(value) VALUES('not committed')")

        result = backup_sqlite_online(
            source,
            destination,
            policy=SQLiteBackupPolicy(pages_per_step=3),
            progress_callback=progress.append,
        )
    finally:
        writer.rollback()
        writer.close()

    assert result.source_path == source.resolve()
    assert result.destination_path == destination.absolute()
    assert result.publication_method == "hard_link_no_replace"
    assert result.pages_per_step == 3
    assert result.progress_invocations == len(progress)
    assert result.progress_invocations > 1
    assert result.page_count > 3
    assert result.page_size_bytes == 512
    assert result.destination_size_bytes == destination.stat().st_size
    assert result.integrity.healthy is True
    assert progress[-1].sqlite_status == sqlite3.SQLITE_DONE
    assert progress[-1].remaining_pages == 0
    copied = [item.copied_pages for item in progress]
    assert all(
        later - earlier <= 3 for earlier, later in zip(copied, copied[1:], strict=False)
    )
    with sqlite3.connect(destination) as verification:
        assert verification.execute("SELECT COUNT(*) FROM payload").fetchone() == (180,)
        assert verification.execute("PRAGMA quick_check").fetchone() == ("ok",)
    assert _staging_artifacts(tmp_path) == ()


def test_backup_existing_destination_is_preserved_without_staging(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "existing.sqlite3"
    _create_database(source)
    marker = b"preserve exact destination"
    destination.write_bytes(marker)

    with pytest.raises(FileExistsError):
        backup_sqlite_online(source, destination)

    assert destination.read_bytes() == marker
    assert _staging_artifacts(tmp_path) == ()


def test_backup_publication_race_preserves_raced_destination_and_cleans_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "raced.sqlite3"
    _create_database(source, row_count=20)
    marker = b"won publication race"
    real_link = os.link

    def raced_link(
        staging: str | os.PathLike[str],
        target: str | os.PathLike[str],
        *,
        follow_symlinks: bool = True,
    ) -> None:
        Path(target).write_bytes(marker)
        real_link(staging, target, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "link", raced_link)

    with pytest.raises(FileExistsError):
        backup_sqlite_online(source, destination)

    assert destination.read_bytes() == marker
    assert _staging_artifacts(tmp_path) == ()


def test_backup_fails_closed_when_atomic_no_replace_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "unsupported.sqlite3"
    _create_database(source, row_count=20)

    def unsupported_link(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "hard links unavailable")

    monkeypatch.setattr(os, "link", unsupported_link)

    with pytest.raises(SQLiteBackupPublicationError, match="atomic hard-link"):
        backup_sqlite_online(source, destination)

    assert not destination.exists()
    assert _staging_artifacts(tmp_path) == ()


def test_backup_cooperative_cancellation_cleans_exact_stage_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "cancelled.sqlite3"
    _create_database(source, row_count=200)
    abort = _InjectedAbort("cancel backup")
    calls = 0

    def cancel() -> None:
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise abort

    with pytest.raises(_InjectedAbort) as captured:
        backup_sqlite_online(
            source,
            destination,
            policy=SQLiteBackupPolicy(pages_per_step=1),
            cancellation_check=cancel,
        )

    assert captured.value is abort
    assert calls == 3
    assert not destination.exists()
    assert _staging_artifacts(tmp_path) == ()


def test_backup_base_exception_from_progress_callback_cleans_exact_stage(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "interrupted.sqlite3"
    _create_database(source, row_count=100)
    abort = _InjectedAbort("progress failed")

    def fail_progress(_progress: object) -> None:
        raise abort

    with pytest.raises(_InjectedAbort) as captured:
        backup_sqlite_online(
            source,
            destination,
            policy=SQLiteBackupPolicy(pages_per_step=1),
            progress_callback=fail_progress,
        )

    assert captured.value is abort
    assert not destination.exists()
    assert _staging_artifacts(tmp_path) == ()


def test_backup_verification_rejects_foreign_key_violations_before_publication(
    tmp_path: Path,
) -> None:
    source = tmp_path / "invalid.sqlite3"
    destination = tmp_path / "invalid-backup.sqlite3"
    connection = sqlite3.connect(source)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("CREATE TABLE parent(identifier INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE child(parent_id INTEGER REFERENCES parent(identifier))"
        )
        connection.execute("INSERT INTO child VALUES(99)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SQLiteBackupVerificationError) as captured:
        backup_sqlite_online(source, destination)

    assert captured.value.report.healthy is False
    assert len(captured.value.report.foreign_key_violations) == 1
    assert not destination.exists()
    assert _staging_artifacts(tmp_path) == ()


def test_backup_missing_source_and_parent_never_create_state(tmp_path: Path) -> None:
    missing_source = tmp_path / "missing-source.sqlite3"
    destination = tmp_path / "unused.sqlite3"

    with pytest.raises(sqlite3.OperationalError, match="open database"):
        backup_sqlite_online(missing_source, destination)
    assert not missing_source.exists()
    assert not destination.exists()
    assert _staging_artifacts(tmp_path) == ()

    source = tmp_path / "source.sqlite3"
    _create_database(source)
    absent_parent = tmp_path / "absent" / "backup.sqlite3"
    with pytest.raises(FileNotFoundError, match="parent does not exist"):
        backup_sqlite_online(source, absent_parent)
    assert not absent_parent.exists()
    assert not absent_parent.parent.exists()


# endregion [03]


# region [04] Policy validation


@pytest.mark.parametrize("value", (0, -1, True, MAX_REPORTED_ISSUES + 1))
def test_integrity_policy_rejects_unbounded_issue_limits(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        SQLiteIntegrityPolicy(max_quick_check_errors=value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    (0, -1, True, MAX_BACKUP_PAGES_PER_STEP + 1),
)
def test_backup_policy_rejects_invalid_page_bounds(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        SQLiteBackupPolicy(pages_per_step=value)  # type: ignore[arg-type]


# endregion [04]
