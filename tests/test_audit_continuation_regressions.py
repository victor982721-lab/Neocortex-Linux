"""Focused regressions for the 2026-07-24 continuation audit."""

from __future__ import annotations


import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

import neocortex.documents.document_catalog as catalog_module
import neocortex.documents.document_catalog_schema as catalog_schema_module
from neocortex.deduplication.fingerprinting import FULL_ALGORITHM
from neocortex.documents import document_cache_sync
from neocortex.capabilities.formats.pdf import pdf_derived, pdf_isolation, pdf_route
from neocortex.runtime.orchestration import run_status as run_status_module
from neocortex.api.cli.cli_app import main as cli_main
from neocortex.documents.document_catalog import (
    CATALOG_SCHEMA_VERSION,
    initialize_document_catalog,
)
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state
from neocortex.runtime.orchestration.run_status import list_run_status
from neocortex.semantic.semantic_sources import iter_image_source_records
from neocortex.persistence.sqlite_schema_contract import SQLiteSchemaContractError
from neocortex.persistence.framework_state_writer import FrameworkState
from tests.internal_paths_test_support import begin_signed_normal_run


TEST_CAPABILITIES = ('documents',)


# region [01] Catalog v1 fixtures


_HISTORY_ROWS = (
    (
        "pdf",
        "1:2",
        "source-v1",
        "text-a",
        "classifier-v1",
        r"C:\Normativa\IEEE.pdf",
        '{"kind":"normativa"}',
        11,
    ),
    (
        "docx",
        "3:4",
        "source-v1",
        "text-b",
        "classifier-v1",
        r"C:\Proyecto\Memoria.docx",
        '{"kind":"memoria"}',
        12,
    ),
)


def _seed_catalog_v1(database: Path, anomaly: str | None = None) -> None:
    """Create an exact populated v1 history table plus one optional anomaly."""

    with closing(sqlite3.connect(database)) as connection:
        catalog_schema_module._migrate_to_v1(connection)
        catalog_schema_module._set_schema_version(connection, 1)
        connection.execute("INSERT INTO metadata(key,value) VALUES('audit_sentinel','preserved')")
        if anomaly == "column":
            connection.execute(
                """ALTER TABLE classification_history
                ADD COLUMN vendor_note TEXT NOT NULL DEFAULT 'preserved'"""
            )
        connection.executemany(
            """INSERT INTO classification_history(
            source_kind,file_key,processing_signature,text_fingerprint,
            classifier_signature,path,classification_json,classified_ns
            ) VALUES(?,?,?,?,?,?,?,?)""",
            _HISTORY_ROWS,
        )
        if anomaly == "trigger":
            connection.execute(
                """CREATE TRIGGER unexpected_history_trigger
                AFTER UPDATE ON classification_history BEGIN SELECT 1; END"""
            )
        elif anomaly == "reserved_table":
            connection.executescript(
                """CREATE TABLE classification_history_v2(
                    sentinel TEXT PRIMARY KEY
                ) WITHOUT ROWID;
                INSERT INTO classification_history_v2 VALUES('preserved');
                """
            )
        connection.commit()


def _history_rows(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(
            """SELECT source_kind,file_key,processing_signature,text_fingerprint,
            classifier_signature,path,classification_json,classified_ns
            FROM classification_history ORDER BY file_key"""
        )
    )


# endregion [01]


# region [02] Catalog migration preservation and abstention


def test_populated_catalog_v1_migration_preserves_history_and_metadata(
    tmp_path: Path,
) -> None:
    database = tmp_path / "document_catalog.sqlite3"
    _seed_catalog_v1(database)

    initialize_document_catalog(database)
    initialize_document_catalog(database)

    with closing(sqlite3.connect(database)) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        sentinel = connection.execute(
            "SELECT value FROM metadata WHERE key='audit_sentinel'"
        ).fetchone()[0]
        primary_key = tuple(
            str(row[1])
            for row in connection.execute("PRAGMA table_info(classification_history)")
            if int(row[5])
        )
        rows = _history_rows(connection)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert version == str(CATALOG_SCHEMA_VERSION)
    assert sentinel == "preserved"
    assert primary_key[-1] == "path"
    assert rows == tuple(sorted(_HISTORY_ROWS, key=lambda row: row[1]))
    assert integrity == "ok"
    assert foreign_keys == []


@pytest.mark.parametrize(
    ("anomaly", "message"),
    (
        ("column", "unknown structure"),
        ("trigger", "unknown trigger"),
        ("reserved_table", "reserved object"),
    ),
)
def test_catalog_v1_unknown_objects_abstain_and_roll_back_without_loss(
    tmp_path: Path,
    anomaly: str,
    message: str,
) -> None:
    database = tmp_path / f"catalog-{anomaly}.sqlite3"
    _seed_catalog_v1(database, anomaly)

    with pytest.raises(SQLiteSchemaContractError, match=message):
        initialize_document_catalog(database)

    with closing(sqlite3.connect(database)) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "1"
        )
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='audit_sentinel'").fetchone()[
                0
            ]
            == "preserved"
        )
        assert _history_rows(connection) == tuple(sorted(_HISTORY_ROWS, key=lambda row: row[1]))
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        if anomaly == "column":
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(classification_history)")
            }
            assert "vendor_note" in columns
            assert {
                str(row[0])
                for row in connection.execute("SELECT vendor_note FROM classification_history")
            } == {"preserved"}
        elif anomaly == "trigger":
            assert connection.execute(
                """SELECT 1 FROM sqlite_master WHERE type='trigger'
                AND name='unexpected_history_trigger'"""
            ).fetchone() == (1,)
        else:
            assert connection.execute(
                "SELECT sentinel FROM classification_history_v2"
            ).fetchone() == ("preserved",)


class _InjectedCatalogMigrationAbort(BaseException):
    pass


def test_catalog_v1_base_exception_rolls_back_every_migration_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "catalog-base-exception.sqlite3"
    _seed_catalog_v1(database)

    def abort_after_write(connection: sqlite3.Connection) -> None:
        connection.execute("UPDATE metadata SET value='partial' WHERE key='audit_sentinel'")
        raise _InjectedCatalogMigrationAbort

    monkeypatch.setattr(
        catalog_module,
        "_migrate_identity_text_to_decimal",
        abort_after_write,
    )

    with pytest.raises(_InjectedCatalogMigrationAbort):
        initialize_document_catalog(database)

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("1",)
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='audit_sentinel'"
        ).fetchone() == ("preserved",)
        assert _history_rows(connection) == tuple(sorted(_HISTORY_ROWS, key=lambda row: row[1]))
        assert connection.execute(
            """SELECT COUNT(*) FROM sqlite_master
            WHERE name IN ('catalog_generations','catalog_publications',
            'catalog_generation_documents')"""
        ).fetchone() == (0,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


# endregion [02]


# region [03] Run recovery and CLI exit contracts


def _begin_run_with_started_action(database: Path, root: Path) -> tuple[int, int]:
    with FrameworkState(database) as state:
        run_id = begin_signed_normal_run(state, root)
        action_id = state.begin_file_action(
            run_id,
            "trash_duplicate",
            str(root / "source.bin"),
            None,
            None,
            None,
            True,
        )
    return run_id, action_id


def _action_sandbox(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    return state_directory / "framework.sqlite3", root


def test_terminal_run_ignores_reused_pid_and_prefrontier_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, root = _action_sandbox(tmp_path)
    run_id, _action_id = _begin_run_with_started_action(database, root)
    with FrameworkState(database) as state:
        assert state.mark_abandoned_actions() == 1
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            """UPDATE initial_runs SET status='completed',completed_ns=99,
            current_phase='completed',owner_pid=?,heartbeat_ns=98 WHERE run_id=?""",
            (os.getpid(), run_id),
        )
        connection.commit()

    def reused_pid_must_not_be_evaluated(_process_id: int | None) -> bool | None:
        raise AssertionError("terminal runs must not evaluate a potentially reused PID")

    monkeypatch.setattr(
        run_status_module,
        "process_is_alive",
        reused_pid_must_not_be_evaluated,
    )
    status = list_run_status(database, run_id=run_id, limit=1)[0]

    assert status.status == "completed"
    assert status.owner_pid == os.getpid()
    assert status.owner_alive is None
    assert status.heartbeat_stale is None
    assert status.recovery_required_actions == 0


def test_abandoned_run_closes_prefrontier_action_without_uncertainty(
    tmp_path: Path,
) -> None:
    database, root = _action_sandbox(tmp_path)
    run_id, action_id = _begin_run_with_started_action(database, root)

    with FrameworkState(database) as state:
        assert state.mark_abandoned_runs() == 1
        assert state.mark_abandoned_actions() == 1

    with closing(sqlite3.connect(database)) as connection:
        run_status = connection.execute(
            "SELECT status FROM initial_runs WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        action = connection.execute(
            "SELECT status,completed_ns,detail FROM file_actions WHERE action_id=?",
            (action_id,),
        ).fetchone()

    assert run_status == "interrupted"
    assert action[0] == "failed"
    assert action[1] is not None
    assert "before the mutation frontier" in str(action[2])


def test_public_cli_maps_semantic_validation_failure_to_exit_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        cli_main(["--semantic-status", "--semantic-include-compact"])

    assert caught.value.code == 2
    assert "requires --semantic-prepare-models" in capsys.readouterr().err


# endregion [03]


# region [04] SQLite connection and cache synchronization contracts


def test_pdf_derived_connection_enables_foreign_keys(tmp_path: Path) -> None:
    with pdf_derived._database(tmp_path / "pdf.sqlite3") as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_isolated_pdf_reader_rejects_writes(tmp_path: Path) -> None:
    database = tmp_path / "pdf.sqlite3"
    initialize_pdf_state(database)

    with pdf_route._database(database, readonly=True) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden_write(value INTEGER)")


def test_profile_child_does_not_open_pdf_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the child target directly without starting a child process."""

    database = tmp_path / "pdf.sqlite3"
    initialize_pdf_state(database)
    messages: list[tuple[object, ...]] = []
    before_bytes = database.read_bytes()

    class _FakeTools:
        @staticmethod
        def mupdf_display_errors(_enabled: bool) -> None:
            return None

        @staticmethod
        def mupdf_display_warnings(_enabled: bool) -> None:
            return None

        @staticmethod
        def reset_mupdf_warnings() -> None:
            return None

        @staticmethod
        def mupdf_warnings(*, reset: bool) -> str:
            assert reset is True
            return ""

    class _FakeDocument:
        def __enter__(self) -> _FakeDocument:
            return self

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _traceback: object,
        ) -> None:
            return None

    class _FakeFitz:
        TOOLS = _FakeTools()

        @staticmethod
        def open(_path: str) -> _FakeDocument:
            return _FakeDocument()

    class _Channel:
        @staticmethod
        def put(message: tuple[object, ...]) -> None:
            messages.append(message)

    monkeypatch.setitem(sys.modules, "fitz", _FakeFitz())

    pdf_isolation._profile_child("unused-by-fake-fitz.pdf", (), _Channel())

    assert messages == [("done",)]
    assert database.read_bytes() == before_bytes


def test_document_cache_sync_enables_foreign_keys_and_rolls_back(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cache.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(
            """CREATE TABLE parent(parent_id INTEGER PRIMARY KEY);
            CREATE TABLE child(
                child_id INTEGER PRIMARY KEY,
                parent_id INTEGER NOT NULL REFERENCES parent(parent_id)
            );"""
        )
        connection.commit()

    def insert_orphan(connection: sqlite3.Connection) -> int:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        connection.execute("INSERT INTO child VALUES(1,999)")
        return 1

    result = document_cache_sync._synchronize_database(
        "fixture",
        database,
        required=True,
        operation=insert_orphan,
    )

    assert result.status == "error"
    assert "FOREIGN KEY constraint failed" in str(result.detail)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0


def test_document_cache_sync_does_not_reassign_incompatible_identity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "dedup.sqlite3"
    old_path = str(tmp_path / "source.pdf")
    new_path = str(tmp_path / "destination.pdf")
    incompatible_volume = (3).to_bytes(16, "little")
    incompatible_file = (4).to_bytes(16, "little")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            """CREATE TABLE files(
            path TEXT PRIMARY KEY COLLATE NOCASE,
            volume_id BLOB NOT NULL,
            file_id BLOB NOT NULL
            ) WITHOUT ROWID"""
        )
        connection.execute(
            "INSERT INTO files VALUES(?,?,?)",
            (old_path, incompatible_volume, incompatible_file),
        )
        connection.commit()

    def operation(connection: sqlite3.Connection) -> int:
        return document_cache_sync._sync_dedup_cache(
            connection,
            old_path=old_path,
            new_path=new_path,
            volume_id="1",
            file_id="2",
        )

    result = document_cache_sync._synchronize_database(
        "dedup",
        database,
        required=False,
        operation=operation,
    )

    assert result.status == "error"
    assert "belongs to another identity" in str(result.detail)
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute("SELECT path,volume_id,file_id FROM files").fetchone()
    assert row == (old_path, incompatible_volume, incompatible_file)


@pytest.mark.skipif(
    document_cache_sync._PATH_COLLATION != "BINARY",
    reason="POSIX path identity contract",
)
def test_document_cache_sync_does_not_casefold_distinct_linux_paths() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE files(
        scan_id INTEGER NOT NULL,
        path TEXT NOT NULL COLLATE BINARY,
        volume_id BLOB NOT NULL,
        file_id BLOB NOT NULL,
        PRIMARY KEY(scan_id,volume_id,file_id),
        UNIQUE(scan_id,path)
        ) WITHOUT ROWID"""
    )
    connection.executemany(
        "INSERT INTO files VALUES(1,?,?,?)",
        (
            ("/tmp/Case.pdf", (1).to_bytes(16, "little"), (11).to_bytes(16, "little")),
            ("/tmp/case.pdf", (1).to_bytes(16, "little"), (12).to_bytes(16, "little")),
        ),
    )

    updated = document_cache_sync._sync_dedup_cache(
        connection,
        old_path="/tmp/Case.pdf",
        new_path="/tmp/Renamed.pdf",
        volume_id="1",
        file_id="11",
    )

    assert updated == 1
    assert [
        str(row[0])
        for row in connection.execute("SELECT path FROM files ORDER BY path COLLATE BINARY")
    ] == ["/tmp/Renamed.pdf", "/tmp/case.pdf"]


# endregion [04]


# region [05] Semantic source generation selection


def test_semantic_image_source_uses_only_latest_published_v7_generation(
    tmp_path: Path,
) -> None:
    image_database = tmp_path / "image.sqlite3"
    dedup_database = tmp_path / "dedup.sqlite3"
    path = str(tmp_path / "fixture.jpg")
    size, mtime_ns, birthtime_ns = 100, 200, 300
    digests = {
        "previous": b"A" * 32,
        "published": b"B" * 32,
        "building": b"C" * 32,
    }
    with closing(sqlite3.connect(image_database)) as connection:
        connection.execute(
            """CREATE TABLE images(
            file_key TEXT PRIMARY KEY,path TEXT NOT NULL,size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,birthtime_ns INTEGER NOT NULL,
            processing_signature TEXT,category TEXT,document_candidate INTEGER,
            adult_classification TEXT,status TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO images VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "22:222",
                path,
                size,
                mtime_ns,
                birthtime_ns,
                "image-v1",
                "industrial",
                0,
                "safe",
                "done",
            ),
        )
        connection.commit()
    with closing(sqlite3.connect(dedup_database)) as connection:
        connection.executescript(
            """CREATE TABLE files(
                scan_id INTEGER NOT NULL,path TEXT NOT NULL COLLATE NOCASE,
                volume_id BLOB NOT NULL,file_id BLOB NOT NULL,size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,birthtime_ns INTEGER NOT NULL,
                PRIMARY KEY(scan_id,path)
            ) WITHOUT ROWID;
            CREATE TABLE inventory_checkpoints(
                root TEXT PRIMARY KEY,scan_id INTEGER NOT NULL,
                valid INTEGER NOT NULL,updated_ns INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE fingerprints(
                volume_id BLOB NOT NULL,file_id BLOB NOT NULL,size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,birthtime_ns INTEGER NOT NULL,
                algorithm TEXT NOT NULL,digest BLOB NOT NULL,
                PRIMARY KEY(volume_id,file_id,size,mtime_ns,algorithm)
            ) WITHOUT ROWID;
            """
        )
        identities = (
            (20, 11, 111, "previous"),
            (10, 22, 222, "published"),
            (30, 33, 333, "building"),
        )
        for scan_id, volume_id, file_id, label in identities:
            volume_blob = volume_id.to_bytes(16, "little")
            file_blob = file_id.to_bytes(16, "little")
            connection.execute(
                "INSERT INTO files VALUES(?,?,?,?,?,?,?)",
                (
                    scan_id,
                    path,
                    volume_blob,
                    file_blob,
                    size,
                    mtime_ns,
                    birthtime_ns,
                ),
            )
            connection.execute(
                "INSERT INTO fingerprints VALUES(?,?,?,?,?,?,?)",
                (
                    volume_blob,
                    file_blob,
                    size,
                    mtime_ns,
                    birthtime_ns,
                    FULL_ALGORITHM,
                    digests[label],
                ),
            )
        connection.executemany(
            "INSERT INTO inventory_checkpoints VALUES(?,?,?,?)",
            (
                ("previous-root", 20, 1, 100),
                ("published-root", 10, 1, 200),
                ("building-root", 30, 0, 300),
            ),
        )
        connection.commit()

    records = tuple(iter_image_source_records(tmp_path, verify_snapshots=False))

    assert len(records) == 1
    revision = records[0].item.source_revision
    assert revision["raw_content_xxh3_128"] == digests["published"].hex()
    assert records[0].item.provenance["fingerprint_acquisition"] == "dedup-cache"


# endregion [05]
