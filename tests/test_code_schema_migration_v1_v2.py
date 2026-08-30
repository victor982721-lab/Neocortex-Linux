"""Deterministic preservation tests for the code-state v1 to v2 migration."""

from __future__ import annotations

import sqlite3
import zlib
from pathlib import Path

import pytest

from neocortex.code import code_schema

# region [01] Version-one fixture and snapshots


_V1_CONTENT_QUERIES = (
    (
        "analysis_runs",
        "SELECT * FROM analysis_runs ORDER BY analysis_run_id",
    ),
    ("files", "SELECT * FROM files ORDER BY file_id"),
    ("file_versions", "SELECT * FROM file_versions ORDER BY version_id"),
    (
        "invalidation_history",
        "SELECT * FROM invalidation_history ORDER BY invalidation_id",
    ),
    ("symbols", "SELECT * FROM symbols ORDER BY symbol_id"),
    (
        "code_references",
        "SELECT * FROM code_references ORDER BY reference_id",
    ),
    ("dependencies", "SELECT * FROM dependencies ORDER BY dependency_id"),
    ("diagnostics", "SELECT * FROM diagnostics ORDER BY diagnostic_id"),
    ("metrics", "SELECT * FROM metrics ORDER BY metric_id"),
    ("code_chunks", "SELECT * FROM code_chunks ORDER BY chunk_id"),
    (
        "code_fts",
        """SELECT rowid,chunk_id,version_id,path,project,language,symbol,
        signature,body FROM code_fts ORDER BY rowid""",
    ),
    (
        "version_relations",
        "SELECT * FROM version_relations ORDER BY relation_id",
    ),
)

_CURRENT_CONTENT_QUERIES = (
    ("metadata", "SELECT * FROM metadata ORDER BY key"),
    (
        "schema_migrations",
        "SELECT * FROM schema_migrations ORDER BY version",
    ),
    *_V1_CONTENT_QUERIES,
    ("projects", "SELECT * FROM projects ORDER BY project_id"),
    (
        "project_memberships",
        "SELECT * FROM project_memberships ORDER BY project_id,version_id",
    ),
    (
        "project_edges",
        """SELECT * FROM project_edges
        ORDER BY source_project_id,dependency_name,edge_kind""",
    ),
    (
        "embedding_links",
        """SELECT * FROM embedding_links
        ORDER BY chunk_id,model_signature,generation_id""",
    ),
    (
        "external_tool_runs",
        "SELECT * FROM external_tool_runs ORDER BY tool_run_id",
    ),
    (
        "external_run_contracts",
        "SELECT * FROM external_run_contracts ORDER BY tool_run_id",
    ),
    (
        "external_run_inputs",
        "SELECT * FROM external_run_inputs ORDER BY tool_run_id,version_id",
    ),
    (
        "external_findings",
        "SELECT * FROM external_findings ORDER BY tool_run_id,portable_finding_id",
    ),
    (
        "external_run_replays",
        "SELECT * FROM external_run_replays ORDER BY tool_run_id",
    ),
    (
        "external_run_counters",
        "SELECT * FROM external_run_counters ORDER BY tool_run_id,name",
    ),
    (
        "external_metrics",
        "SELECT * FROM external_metrics ORDER BY tool_run_id,portable_metric_id",
    ),
    (
        "external_relations",
        "SELECT * FROM external_relations ORDER BY tool_run_id,portable_relation_id",
    ),
)


def _snapshot_rows(
    connection: sqlite3.Connection,
    queries: tuple[tuple[str, str], ...],
) -> dict[str, tuple[tuple[object, ...], ...]]:
    return {name: tuple(tuple(row) for row in connection.execute(query)) for name, query in queries}


def _insert_version(
    connection: sqlite3.Connection,
    *,
    version_id: int,
    file_id: int,
    path: str,
    text: str,
    invalidated_ns: int | None = None,
    invalidation_reason: str | None = None,
) -> None:
    raw = text.encode("utf-8")
    connection.execute(
        """INSERT INTO file_versions(
        version_id,file_id,path_observed,size,mtime_ns,birthtime_ns,
        raw_xxh3_128,raw_xxh3_64_guard,text_xxh3_128,text_xxh3_64_guard,
        normalized_xxh3_128,token_xxh3_128,structure_xxh3_128,encoding,
        language,artifact_kind,generated,vendored,classification_confidence,
        classification_evidence_json,analysis_status,processing_signature,
        analyzer_id,analyzer_version,parser_kind,text_zlib,text_chars,
        text_truncated,provenance_json,first_observed_run_id,
        last_observed_run_id,valid_from_ns,invalidated_ns,invalidation_reason)
        VALUES(
        :version_id,:file_id,:path,:size,:mtime_ns,50,:raw_128,:raw_guard,
        :text_128,:text_guard,:normalized_128,:token_128,:structure_128,
        'utf-8','python','source',0,0,1.0,'["fixture-v1"]','complete',
        'fixture-signature','fixture-analyzer','1','python-ast',:text_zlib,
        :text_chars,0,'{"fixture":"v1"}',1,1,:valid_from_ns,
        :invalidated_ns,:invalidation_reason)""",
        {
            "version_id": version_id,
            "file_id": file_id,
            "path": path,
            "size": len(raw),
            "mtime_ns": version_id * 100,
            "raw_128": f"{version_id:032x}",
            "raw_guard": f"{version_id:016x}",
            "text_128": f"{version_id + 10:032x}",
            "text_guard": f"{version_id + 10:016x}",
            "normalized_128": f"{version_id + 20:032x}",
            "token_128": f"{version_id + 30:032x}",
            "structure_128": f"{version_id + 40:032x}",
            "text_zlib": sqlite3.Binary(zlib.compress(raw)),
            "text_chars": len(text),
            "valid_from_ns": 1_000 + version_id,
            "invalidated_ns": invalidated_ns,
            "invalidation_reason": invalidation_reason,
        },
    )


def _create_version_one_fixture(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in code_schema._V1_DDL:
            connection.execute(statement)
        connection.execute("INSERT INTO metadata(key,value) VALUES('schema_version','1')")
        connection.execute("INSERT INTO metadata(key,value) VALUES('preserved_marker','keep-me')")
        connection.execute(
            """INSERT INTO schema_migrations(version,description,applied_ns)
            VALUES(1,'fixture version one',11)"""
        )
        connection.execute("PRAGMA user_version=1")
        connection.execute(
            """INSERT INTO analysis_runs(
            analysis_run_id,framework_run_id,scan_id,processing_signature,status,
            started_ns,completed_ns,candidates,processed,cache_hits,errors,
            summary_json)
            VALUES(7,70,700,'fixture-signature','completed',100,200,3,3,0,0,
            '{"fixture":true}')"""
        )
        connection.executemany(
            """INSERT INTO files(
            file_id,volume_id,physical_file_id,current_path,current_version_id,
            status,first_seen_run_id,last_seen_run_id)
            VALUES(?,?,?,?,NULL,'current',1,1)""",
            (
                (1, "volume", "target-physical", "C:/fixture/target.py"),
                (2, "volume", "caller-physical", "C:/fixture/caller.py"),
            ),
        )
        _insert_version(
            connection,
            version_id=1,
            file_id=1,
            path="C:/fixture/target.py",
            text="def target_old(): pass\n",
            invalidated_ns=2_001,
            invalidation_reason="superseded_observation",
        )
        _insert_version(
            connection,
            version_id=2,
            file_id=1,
            path="C:/fixture/target.py",
            text="def target(): pass\n",
        )
        _insert_version(
            connection,
            version_id=3,
            file_id=2,
            path="C:/fixture/caller.py",
            text="target()  # validate sqlite access\n",
        )
        connection.execute("UPDATE files SET current_version_id=2 WHERE file_id=1")
        connection.execute("UPDATE files SET current_version_id=3 WHERE file_id=2")
        connection.execute(
            """INSERT INTO invalidation_history(
            invalidation_id,version_id,invalidated_ns,reason,
            replacement_version_id,evidence_json)
            VALUES(80,1,2001,'superseded_observation',2,'{"fixture":true}')"""
        )
        connection.executemany(
            """INSERT INTO symbols(
            symbol_id,version_id,parent_symbol_id,kind,name,qualified_name,
            signature,visibility,docstring,confirmed,complexity,start_line,
            start_column,end_line,end_column,start_byte,end_byte,metadata_json)
            VALUES(?,?,NULL,'function',?,?,?,'public',?,1,1,1,0,1,20,0,20,
            '{"fixture":true}')""",
            (
                (10, 2, "target", "pkg.target", "target()", "target docs"),
                (11, 3, "caller", "pkg.caller", "caller()", "caller docs"),
            ),
        )
        connection.execute(
            """INSERT INTO code_references(
            reference_id,version_id,source_symbol_id,target_symbol_id,
            target_version_id,kind,name,target_hint,confirmed,confidence,evidence,
            start_line,start_column,end_line,end_column,start_byte,end_byte)
            VALUES(20,3,11,10,2,'call','target','pkg.target',1,1.0,
            'fixture-reference',1,0,1,8,0,8)"""
        )
        connection.execute(
            """INSERT INTO dependencies(
            dependency_id,version_id,resolved_version_id,name,kind,scope,
            version_spec,confirmed,confidence,evidence,start_line,start_column,
            end_line,end_column,start_byte,end_byte)
            VALUES(30,3,2,'target','python_import','runtime',NULL,1,1.0,
            'fixture-dependency',1,0,1,6,0,6)"""
        )
        connection.execute(
            """INSERT INTO diagnostics(
            diagnostic_id,version_id,source,code,severity,message,tool_name,
            tool_version,confirmed,confidence,start_line,start_column,end_line,
            end_column,start_byte,end_byte,metadata_json)
            VALUES(40,3,'fixture','fixture-warning','warning','preserve me',
            'fixture-tool','1',1,0.9,1,0,1,8,0,8,'{"fixture":true}')"""
        )
        connection.execute(
            """INSERT INTO metrics(
            metric_id,version_id,symbol_id,name,value,confirmed,provenance)
            VALUES(50,3,11,'cyclomatic_complexity',1.0,1,'fixture-tool:1')"""
        )
        connection.executemany(
            """INSERT INTO code_chunks(
            chunk_id,version_id,symbol_id,chunk_index,kind,start_line,end_line,
            start_byte,end_byte,text,text_xxh3_128)
            VALUES(?,?,?,0,'symbol',1,1,0,?,?,?)""",
            (
                (60, 2, 10, 20, "def target(): pass", f"{60:032x}"),
                (
                    61,
                    3,
                    11,
                    34,
                    "target()  # validate sqlite access",
                    f"{61:032x}",
                ),
            ),
        )
        connection.executemany(
            """INSERT INTO code_fts(
            chunk_id,version_id,path,project,language,symbol,signature,body)
            VALUES(?,?,?,'','python',?,?,?)""",
            (
                (
                    60,
                    2,
                    "C:/fixture/target.py",
                    "pkg.target",
                    "target()",
                    "def target(): pass",
                ),
                (
                    61,
                    3,
                    "C:/fixture/caller.py",
                    "pkg.caller",
                    "caller()",
                    "target() validate sqlite access",
                ),
            ),
        )
        connection.executemany(
            """INSERT INTO version_relations(
            relation_id,left_version_id,right_version_id,relation_kind,
            confidence,evidence_json,created_ns)
            VALUES(?,?,?,?,?,?,?)""",
            (
                (70, 1, 2, "predecessor", 1.0, '{"fixture":true}', 3_001),
                (
                    71,
                    2,
                    3,
                    "normalized_duplicate",
                    0.8,
                    '{"fixture":true}',
                    3_002,
                ),
            ),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _insert_version_two_relations(connection: sqlite3.Connection) -> None:
    connection.execute(
        """INSERT INTO projects(
        project_id,project_key,name,ecosystem,probable_root,manifest_kind,
        confidence,evidence_json,first_seen_run_id,last_seen_run_id,status)
        VALUES(90,'project-instance','fixture-project','python','C:/fixture',
        'pyproject',1.0,'{"fixture":true}',1,1,'current')"""
    )
    connection.executemany(
        """INSERT INTO project_memberships(
        project_id,version_id,proposed_path,relation,confidence,selected,
        conflict_group,evidence_json)
        VALUES(90,?,?, 'manifest',1.0,1,NULL,'{"fixture":true}')""",
        ((2, "target.py"), (3, "caller.py")),
    )
    connection.execute(
        """INSERT INTO project_edges(
        source_project_id,target_project_id,dependency_name,edge_kind,
        confidence,evidence_json)
        VALUES(90,90,'fixture-project','python_import',1.0,'{"fixture":true}')"""
    )
    connection.execute(
        """INSERT INTO embedding_links(
        chunk_id,semantic_item_id,model_signature,vector_space,generation_id,
        active,provenance_json)
        VALUES(60,'semantic-60','model-v1','code-text',1,1,'{"fixture":true}')"""
    )
    connection.execute(
        """INSERT INTO external_tool_runs(
        tool_run_id,analysis_run_id,project_id,tool_name,tool_version,
        configuration_signature,status,started_ns,completed_ns,provenance_json)
        VALUES(100,7,90,'ruff','1','config-v1','completed',4001,4002,
        '{"fixture":true}')"""
    )


# endregion [01]


# region [02] Migration preservation and rollback


def test_v1_to_current_migration_preserves_rows_relations_and_fts(tmp_path: Path) -> None:
    database = tmp_path / "code.sqlite3"
    _create_version_one_fixture(database)
    with sqlite3.connect(database) as connection:
        before = _snapshot_rows(connection, _V1_CONTENT_QUERIES)

    code_schema.initialize_code_state(database)

    with sqlite3.connect(database) as connection:
        after = _snapshot_rows(connection, _V1_CONTENT_QUERIES)
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        migrations = connection.execute(
            """SELECT version,description,applied_ns
            FROM schema_migrations ORDER BY version"""
        ).fetchall()

        assert after == before
        assert metadata == {
            "preserved_marker": "keep-me",
            "schema_version": str(code_schema.CODE_SCHEMA_VERSION),
        }
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == (
            code_schema.CODE_SCHEMA_VERSION
        )
        assert migrations[0] == (1, "fixture version one", 11)
        assert migrations[1][0:2] == (
            2,
            "probable projects, reconstruction provenance and semantic links",
        )
        assert int(migrations[1][2]) > 0
        assert migrations[2][0:2] == (
            3,
            "normalized multi-provider external code evidence",
        )
        assert int(migrations[2][2]) > 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            """SELECT source_symbol_id,target_symbol_id,target_version_id
            FROM code_references WHERE reference_id=20"""
        ).fetchone() == (11, 10, 2)
        assert connection.execute(
            """SELECT version_id,replacement_version_id
            FROM invalidation_history WHERE invalidation_id=80"""
        ).fetchone() == (1, 2)
        assert connection.execute(
            "SELECT version_id FROM code_fts WHERE code_fts MATCH 'sqlite'"
        ).fetchall() == [(3,)]
        assert {
            str(row[0])
            for row in connection.execute(
                """SELECT name FROM sqlite_master WHERE type='table'
                AND name IN ('projects','project_memberships','project_edges',
                'embedding_links','external_tool_runs','external_run_contracts',
                'external_run_inputs','external_findings','external_run_replays',
                'external_run_counters','external_metrics','external_relations')"""
            )
        } == {
            "projects",
            "project_memberships",
            "project_edges",
            "embedding_links",
            "external_tool_runs",
            "external_run_contracts",
            "external_run_inputs",
            "external_findings",
            "external_run_replays",
            "external_run_counters",
            "external_metrics",
            "external_relations",
        }

        _insert_version_two_relations(connection)
        connection.commit()
        code_schema.validate_code_schema(connection)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        before_reopen = _snapshot_rows(connection, _CURRENT_CONTENT_QUERIES)

    code_schema.initialize_code_state(database)

    with sqlite3.connect(database) as connection:
        assert _snapshot_rows(connection, _CURRENT_CONTENT_QUERIES) == before_reopen
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v1_to_current_migration_failure_rolls_back_ddl_and_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "code.sqlite3"
    _create_version_one_fixture(database)
    with sqlite3.connect(database) as connection:
        before = _snapshot_rows(connection, _V1_CONTENT_QUERIES)

    monkeypatch.setattr(
        code_schema,
        "_V2_DDL",
        (code_schema._V2_DDL[0], "CREATE TABLE deliberately_incomplete("),
    )
    with pytest.raises(sqlite3.OperationalError):
        code_schema.initialize_code_state(database)

    with sqlite3.connect(database) as connection:
        assert _snapshot_rows(connection, _V1_CONTENT_QUERIES) == before
        assert dict(connection.execute("SELECT key,value FROM metadata")) == {
            "preserved_marker": "keep-me",
            "schema_version": "1",
        }
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 1
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,)]
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name='projects'").fetchone()
            is None
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _create_version_two_fixture(database: Path) -> None:
    _create_version_one_fixture(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        code_schema._execute(connection, code_schema._V2_DDL)
        code_schema._record_migration(
            connection,
            2,
            "probable projects, reconstruction provenance and semantic links",
            12,
        )
        _insert_version_two_relations(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def test_v2_to_v3_migration_preserves_legacy_external_runs(tmp_path: Path) -> None:
    database = tmp_path / "code.sqlite3"
    _create_version_two_fixture(database)
    with sqlite3.connect(database) as connection:
        before = _snapshot_rows(
            connection,
            tuple(
                item
                for item in _CURRENT_CONTENT_QUERIES
                if not item[0].startswith("external_run_")
                and item[0] not in {"external_findings", "external_metrics", "external_relations"}
            ),
        )

    code_schema.initialize_code_state(database)

    with sqlite3.connect(database) as connection:
        after = _snapshot_rows(
            connection,
            tuple(
                item
                for item in _CURRENT_CONTENT_QUERIES
                if not item[0].startswith("external_run_")
                and item[0] not in {"external_findings", "external_metrics", "external_relations"}
            ),
        )
        assert after["external_tool_runs"] == before["external_tool_runs"]
        assert dict(connection.execute("SELECT key,value FROM metadata")) == {
            "preserved_marker": "keep-me",
            "schema_version": str(code_schema.CODE_SCHEMA_VERSION),
        }
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == (
            code_schema.CODE_SCHEMA_VERSION
        )
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in range(1, code_schema.CODE_SCHEMA_VERSION + 1)]
        for table in (
            "external_run_contracts",
            "external_run_inputs",
            "external_findings",
            "external_run_replays",
            "external_run_counters",
            "external_metrics",
            "external_relations",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        code_schema.validate_code_schema(connection)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v2_to_v3_migration_failure_rolls_back_ddl_and_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "code.sqlite3"
    _create_version_two_fixture(database)
    monkeypatch.setattr(
        code_schema,
        "_V3_DDL",
        (code_schema._V3_DDL[0], "CREATE TABLE deliberately_incomplete("),
    )

    with pytest.raises(sqlite3.OperationalError):
        code_schema.initialize_code_state(database)

    with sqlite3.connect(database) as connection:
        assert dict(connection.execute("SELECT key,value FROM metadata")) == {
            "preserved_marker": "keep-me",
            "schema_version": "2",
        }
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 2
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,)]
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM external_tool_runs WHERE tool_run_id=100"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='external_run_contracts'"
            ).fetchone()
            is None
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


# endregion [02]
