"""Filesystem path-equivalence regressions for Framework and Code owners."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.code import code_schema
from neocortex.persistence import framework_schema
from neocortex.code.code_state import CodeState
from neocortex.safety.route_filters import (
    CandidateSelection,
    framework_selection_predicate,
)
from neocortex.workflow.self_analysis.self_analysis_status import quiescent_sqlite_database
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.platform_policy import sqlite_path_collation


pytestmark = pytest.mark.skipif(
    sqlite_path_collation() != "BINARY",
    reason="the live Linux path contract is required for these migration regressions",
)


def _index_collations(
    connection: sqlite3.Connection,
    index: str,
) -> tuple[str, ...]:
    return tuple(
        str(row[4])
        for row in connection.execute(f'PRAGMA index_xinfo("{index}")')
        if int(row[5]) == 1
    )


def _create_framework_v21(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        framework_schema._build_v21_exact_schema(connection)
        connection.execute("INSERT INTO metadata VALUES('schema_version','21')")


def _create_code_v4(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        code_schema._build_legacy_schema(connection, 4)
        connection.execute("INSERT INTO metadata VALUES('schema_version','4')")
        connection.executemany(
            "INSERT INTO schema_migrations(version,description,applied_ns) VALUES(?,?,?)",
            ((version, f"fixture-v{version}", version) for version in range(1, 5)),
        )
        connection.execute("PRAGMA user_version=4")


@pytest.mark.parametrize(
    "paths",
    (("/fixture/Case.py", "/fixture/case.py"), ("/fixture/case.py", "/fixture/Case.py")),
)
def test_linux_framework_and_code_keep_case_distinct_in_both_orders(
    tmp_path: Path,
    paths: tuple[str, str],
) -> None:
    framework_path = tmp_path / "framework.sqlite3"
    with FrameworkState(framework_path) as state:
        state.store_route_candidates(
            7,
            (
                ("text/x-python", FileSnapshot(path, 1, ordinal, 10, 20, -1))
                for ordinal, path in enumerate(paths, start=1)
            ),
        )
        assert [item.path for item in state.iter_route_candidates(7, "text/x-python")] == sorted(
            paths
        )

    code_path = tmp_path / "code.sqlite3"
    with CodeState(code_path) as state:
        state.connection.executemany(
            """INSERT INTO files(
            volume_id,physical_file_id,current_path,status,
            first_seen_run_id,last_seen_run_id)
            VALUES(?,?,?,'current',1,1)""",
            (("volume", f"file-{ordinal}", path) for ordinal, path in enumerate(paths)),
        )
        state.connection.commit()
        assert (
            tuple(
                str(row[0])
                for row in state.connection.execute(
                    "SELECT current_path FROM files ORDER BY file_id"
                )
            )
            == paths
        )


@pytest.mark.parametrize(
    "paths",
    (("C:/fixture/Case.py", "C:/fixture/case.py"), ("C:/fixture/case.py", "C:/fixture/Case.py")),
)
def test_windows_owner_ddl_rejects_case_only_identity_duplicates_in_both_orders(
    paths: tuple[str, str],
) -> None:
    with sqlite3.connect(":memory:") as framework:
        framework.execute(framework_schema._route_candidates_table_statement("NOCASE"))
        framework.execute(
            "INSERT INTO route_candidates VALUES(1,'text/x-python',?,'1','1',1,1,-1)",
            (paths[0],),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            framework.execute(
                "INSERT INTO route_candidates VALUES(1,'text/x-python',?,'1','2',1,1,-1)",
                (paths[1],),
            )

    with sqlite3.connect(":memory:") as code:
        code.execute(code_schema._files_table_ddl("NOCASE"))
        code.execute(code_schema._FILES_CURRENT_PATH_INDEX_DDL)
        code.execute(
            """INSERT INTO files(volume_id,physical_file_id,current_path,status,
            first_seen_run_id,last_seen_run_id) VALUES('volume','one',?,'current',1,1)""",
            (paths[0],),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            code.execute(
                """INSERT INTO files(volume_id,physical_file_id,current_path,status,
                first_seen_run_id,last_seen_run_id)
                VALUES('volume','two',?,'current',1,1)""",
                (paths[1],),
            )


def test_code_path_reuse_and_case_only_rename_are_linux_exact(tmp_path: Path) -> None:
    database = tmp_path / "code.sqlite3"
    with CodeState(database) as state:
        state.connection.executemany(
            """INSERT INTO files(file_id,volume_id,physical_file_id,current_path,
            status,first_seen_run_id,last_seen_run_id)
            VALUES(?,?,?,?,'current',1,1)""",
            (
                (1, "1", "a", "/fixture/Case.py"),
                (2, "1", "b", "/fixture/case.py"),
            ),
        )
        state.connection.commit()

        assert state._release_path_owner("/fixture/case.py", "1", "new", 2) == ()
        assert tuple(
            (str(row[0]), str(row[1]))
            for row in state.connection.execute(
                "SELECT current_path,status FROM files ORDER BY file_id"
            )
        ) == (("/fixture/Case.py", "current"), ("/fixture/case.py", "stale"))

        file_id, previous, conflicts = state._claim_file(
            FileSnapshot("/fixture/cASE.py", 1, 10, 1, 2, -1),
            3,
        )
        state.connection.commit()
        assert (file_id, previous, conflicts) == (1, None, ())
        assert tuple(
            state.connection.execute(
                "SELECT current_path,status FROM files WHERE file_id=1"
            ).fetchone()
        ) == ("/fixture/cASE.py", "current")


@pytest.mark.parametrize(
    ("before", "after"),
    (
        ("/fixture/CaseOnly.py", "/fixture/caseonly.py"),
        ("/fixture/caseonly.py", "/fixture/CaseOnly.py"),
    ),
)
def test_code_case_only_rename_preserves_physical_owner_in_both_orders(
    tmp_path: Path,
    before: str,
    after: str,
) -> None:
    database = tmp_path / "code-case-rename.sqlite3"
    with CodeState(database) as state:
        state.connection.execute(
            """INSERT INTO files(file_id,volume_id,physical_file_id,current_path,
            status,first_seen_run_id,last_seen_run_id)
            VALUES(1,'1','a',?,'current',1,1)""",
            (before,),
        )
        state.connection.commit()
        claimed = state._claim_file(FileSnapshot(after, 1, 10, 1, 2, -1), 2)
        state.connection.commit()
        assert claimed == (1, None, ())
        assert tuple(
            state.connection.execute("SELECT file_id,current_path,status FROM files").fetchone()
        ) == (1, after, "current")


def test_framework_root_reuse_and_path_filtering_are_linux_exact(tmp_path: Path) -> None:
    database = tmp_path / "framework.sqlite3"
    upper = tmp_path / "CaseRoot"
    lower = tmp_path / "caseroot"
    upper.mkdir()
    lower.mkdir()
    with FrameworkState(database) as state:
        state._connection.executemany(
            """INSERT INTO initial_runs(
            run_id,root,started_ns,completed_ns,status,run_kind,scan_id,
            corpus_access_mode)
            VALUES(?,?,1,2,'completed','initial',?,'normal')""",
            ((1, str(upper), 101), (2, str(lower), 202)),
        )
        state._connection.commit()
        assert state.latest_durable_inventory_binding(upper).run_id == 1
        assert state.latest_durable_inventory_binding(lower).run_id == 2

    predicate, parameters = framework_selection_predicate(
        CandidateSelection.from_values(paths=(upper / "Case.py",)),
        route_name="code",
    )
    assert "path COLLATE BINARY IN (?)" in predicate
    assert parameters == (str(upper / "Case.py"),)


def test_framework_protected_root_cas_uses_linux_path_equivalence(tmp_path: Path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        state._connection.execute(
            """INSERT INTO initial_runs(
            run_id,root,started_ns,status,run_kind,corpus_access_mode,
            root_device_id_hex,root_file_id_hex,root_birthtime_ns)
            VALUES(1,'/fixture/Case',1,'running','self_analysis','analyze_only',
            'aa','bb',-1)"""
        )
        parameters = (
            1,
            "inspect",
            "/fixture/source",
            0,
            "planned",
            1,
            "analyze_only",
            "/fixture/case",
            "aa",
            "bb",
            -1,
        )
        with pytest.raises(sqlite3.IntegrityError, match="corpus policy mismatch"):
            state._connection.execute(
                """INSERT INTO file_actions(
                run_id,action_type,source_path,apply_requested,status,started_ns,
                corpus_access_mode,protected_root,protected_root_device_id_hex,
                protected_root_file_id_hex,protected_root_birthtime_ns)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                parameters,
            )
        state._connection.execute(
            """INSERT INTO file_actions(
            run_id,action_type,source_path,apply_requested,status,started_ns,
            corpus_access_mode,protected_root,protected_root_device_id_hex,
            protected_root_file_id_hex,protected_root_birthtime_ns)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (*parameters[:7], "/fixture/Case", *parameters[8:]),
        )
        state._connection.commit()


def test_populated_framework_v21_migrates_exactly_without_reviewtask_changes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    _create_framework_v21(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO route_candidates(
            run_id,mime,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
            VALUES(7,'text/plain','/fixture/Case.txt','a','b',10,20,-1)"""
        )
        review_sql = tuple(
            connection.execute(
                """SELECT name,sql FROM sqlite_master
                WHERE type IN ('table','index','trigger')
                AND name LIKE 'review_task%' ORDER BY type,name"""
            )
        )

    with FrameworkState(database):
        pass

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("22",)
        assert connection.execute(
            "SELECT path,volume_id,file_id FROM route_candidates"
        ).fetchone() == ("/fixture/Case.txt", "a", "b")
        assert _index_collations(connection, "sqlite_autoindex_route_candidates_1") == (
            "BINARY",
            "BINARY",
        )
        migrated_review_sql = tuple(
            connection.execute(
                """SELECT name,sql FROM sqlite_master
                WHERE type IN ('table','index','trigger')
                AND name LIKE 'review_task%' ORDER BY type,name"""
            )
        )
        changed = {
            "review_tasks_validate_insert",
            "review_task_events_validate_insert",
        }
        assert tuple(row for row in migrated_review_sql if row[0] not in changed) == tuple(
            row for row in review_sql if row[0] not in changed
        )
        assert {row[0] for row in migrated_review_sql if row[0] in changed} == changed
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        framework_schema.validate_framework_schema_v22(connection)


def test_framework_v21_migration_failure_rolls_back_schema_version_and_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "framework.sqlite3"
    _create_framework_v21(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO route_candidates VALUES(1,'x','/fixture/Case','1','2',3,4,-1)"
        )
    monkeypatch.setattr(
        framework_schema,
        "_ROUTE_CANDIDATES_TABLE_STATEMENT",
        "CREATE TABLE route_candidates(",
    )

    with pytest.raises(RuntimeError, match="initialization from version 21 failed"):
        FrameworkState(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("21",)
        assert connection.execute("SELECT path FROM route_candidates").fetchone() == (
            "/fixture/Case",
        )
        framework_schema.validate_framework_schema_v21(connection)


def test_populated_code_v4_migrates_exactly_and_preserves_foreign_keys(
    tmp_path: Path,
) -> None:
    database = tmp_path / "code.sqlite3"
    _create_code_v4(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO files(file_id,volume_id,physical_file_id,current_path,
            status,first_seen_run_id,last_seen_run_id)
            VALUES(7,'volume','physical','/fixture/Case.py','current',1,1)"""
        )

    code_schema.initialize_code_state(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == (str(code_schema.CODE_SCHEMA_VERSION),)
        assert connection.execute("SELECT file_id,current_path,status FROM files").fetchone() == (
            7,
            "/fixture/Case.py",
            "current",
        )
        assert _index_collations(connection, "files_current_path_idx") == ("BINARY",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        code_schema.validate_code_schema(connection)


def test_code_v4_migration_failure_rolls_back_schema_version_and_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "code.sqlite3"
    _create_code_v4(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO files(file_id,volume_id,physical_file_id,current_path,
            status,first_seen_run_id,last_seen_run_id)
            VALUES(7,'volume','physical','/fixture/Case.py','current',1,1)"""
        )
    monkeypatch.setattr(code_schema, "_FILES_TABLE_DDL", "CREATE TABLE files(")

    with pytest.raises(sqlite3.OperationalError):
        code_schema.initialize_code_state(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("4",)
        assert connection.execute("SELECT file_id,current_path FROM files").fetchone() == (
            7,
            "/fixture/Case.py",
        )
        code_schema._validate_legacy_code_schema(connection, 4)


def test_code_future_schema_is_rejected_read_only_without_sidecars(tmp_path: Path) -> None:
    database = tmp_path / "future-code.sqlite3"
    future_version = code_schema.CODE_SCHEMA_VERSION + 1
    with sqlite3.connect(database) as connection:
        connection.executescript(
            f"""
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
            INSERT INTO metadata VALUES('schema_version','{future_version}');
            CREATE TABLE sentinel(value TEXT);
            INSERT INTO sentinel VALUES('preserve');
            PRAGMA user_version={future_version};
            """
        )
    before = database.read_bytes()

    with pytest.raises(RuntimeError, match=f"schema {future_version} is unsupported"):
        code_schema.initialize_code_state(database)

    assert database.read_bytes() == before
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone() == ("preserve",)


def test_current_owner_validation_is_read_only_and_sidecar_free(tmp_path: Path) -> None:
    code_path = tmp_path / "code.sqlite3"
    code_schema.initialize_code_state(code_path)
    code_before = code_path.read_bytes()
    code_schema.initialize_code_state(code_path)
    assert code_path.read_bytes() == code_before
    assert not Path(f"{code_path}-wal").exists()
    assert not Path(f"{code_path}-shm").exists()

    framework_path = tmp_path / "framework.sqlite3"
    with FrameworkState(framework_path):
        pass
    framework_before = framework_path.read_bytes()
    with quiescent_sqlite_database(framework_path) as connection:
        framework_schema.validate_framework_schema_v22(connection)
    assert framework_path.read_bytes() == framework_before
    assert not Path(f"{framework_path}-wal").exists()
    assert not Path(f"{framework_path}-shm").exists()


def test_current_code_open_skips_full_store_scan_but_explicit_audit_retains_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code_path = tmp_path / "code.sqlite3"
    code_schema.initialize_code_state(code_path)
    observed: list[str] = []

    def record_integrity(_connection, *, label: str) -> None:
        observed.append(label)

    monkeypatch.setattr(code_schema, "_validate_code_storage_integrity", record_integrity)

    code_schema.initialize_code_state(code_path)
    assert observed == []

    code_schema.verify_code_storage_integrity(code_path)
    assert observed == ["code current state"]
    assert not Path(f"{code_path}-wal").exists()
    assert not Path(f"{code_path}-shm").exists()


def test_legacy_integrity_violation_fails_closed_before_migration(tmp_path: Path) -> None:
    database = tmp_path / "invalid-code.sqlite3"
    _create_code_v4(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """INSERT INTO files(file_id,volume_id,physical_file_id,current_path,
            current_version_id,status,first_seen_run_id,last_seen_run_id)
            VALUES(1,'volume','physical','/fixture/a.py',999,'current',1,1)"""
        )
    before = database.read_bytes()

    with pytest.raises(RuntimeError, match="foreign-key integrity violation"):
        code_schema.initialize_code_state(database)

    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("4",)


def test_framework_v21_integrity_violation_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "invalid-framework.sqlite3"
    _create_framework_v21(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """INSERT INTO file_action_events(
            event_id,action_id,occurred_ns,to_status,stage)
            VALUES(1,999,1,'planned','fixture')"""
        )
    before = database.read_bytes()

    with pytest.raises(RuntimeError, match="foreign-key integrity violation"):
        FrameworkState(database)

    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("21",)
