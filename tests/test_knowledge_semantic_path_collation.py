"""Filesystem-path joins honor the platform SQLite collation policy."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.deduplication.fingerprinting import FULL_ALGORITHM
from _04_Nucleo_Operativo import (
    knowledge_exact,
    knowledge_search_inventory,
    knowledge_snapshot,
    semantic_sources,
)
from neocortex.platform_policy import sqlite_path_collation


_PLATFORM_CASES = (
    pytest.param("posix", False, id="linux-binary"),
    pytest.param("nt", True, id="windows-nocase"),
)


def _memory_database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    return connection


@pytest.mark.parametrize(("platform_name", "case_equivalent"), _PLATFORM_CASES)
def test_snapshot_inventory_root_join_uses_platform_path_collation(
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    case_equivalent: bool,
) -> None:
    monkeypatch.setattr(
        knowledge_snapshot,
        "_PATH_COLLATION",
        sqlite_path_collation(platform_name=platform_name),
    )
    with _memory_database() as connection:
        connection.executescript(
            """
            CREATE TABLE inventory_checkpoints(
                root TEXT,scan_id INTEGER,valid INTEGER,updated_ns INTEGER
            );
            CREATE TABLE scans(scan_id INTEGER,root TEXT,status TEXT);
            CREATE TABLE duplicate_plan_summaries(
                scan_id INTEGER,group_count INTEGER,redundant_files INTEGER,
                reclaimable_bytes INTEGER,completed_ns INTEGER
            );
            INSERT INTO scans VALUES(1,'/corpus/case','complete');
            INSERT INTO inventory_checkpoints VALUES('/corpus/Case',1,1,3);
            """
        )

        if not case_equivalent:
            with pytest.raises(RuntimeError, match="root-mismatched scan"):
                knowledge_snapshot._inventory_observation(connection)
            return

        observation = knowledge_snapshot._inventory_observation(connection)

    assert tuple(head.scope for head in observation.publications) == ("/corpus/Case",)


@pytest.mark.parametrize(("platform_name", "case_equivalent"), _PLATFORM_CASES)
@pytest.mark.parametrize(
    ("kind", "value"),
    (
        pytest.param(
            knowledge_exact.ExactLookupKind.PATH,
            "/corpus/Case.pdf",
            id="path",
        ),
        pytest.param(knowledge_exact.ExactLookupKind.NAME, "Case.pdf", id="basename"),
    ),
)
def test_exact_inventory_lookup_distinguishes_linux_case_twins(
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    case_equivalent: bool,
    kind: knowledge_exact.ExactLookupKind,
    value: str,
) -> None:
    monkeypatch.setattr(
        knowledge_exact,
        "_PATH_COLLATION",
        sqlite_path_collation(platform_name=platform_name),
    )
    identity = (1).to_bytes(16, "little")
    with _memory_database() as connection:
        connection.executescript(
            """
            CREATE TABLE scans(scan_id INTEGER,root TEXT,status TEXT);
            CREATE TABLE files(
                scan_id INTEGER,path TEXT,volume_id BLOB,file_id BLOB,
                size INTEGER,mtime_ns INTEGER,birthtime_ns INTEGER
            );
            INSERT INTO scans VALUES(1,'/corpus','complete');
            """
        )
        connection.executemany(
            "INSERT INTO files VALUES(1,?,?,?,?,?,?)",
            (
                ("/corpus/Case.pdf", identity, identity, 10, 11, -1),
                ("/corpus/case.pdf", identity, (2).to_bytes(16, "little"), 12, 13, -1),
            ),
        )
        control = knowledge_exact._QueryControl(32, 100_000, None)

        rows, _steps, truncated = knowledge_exact._inventory_term_rows(
            connection,
            control,
            (("/corpus", 1),),
            knowledge_exact.ExactLookupTerm(kind, value),
            10,
            None,
        )

    expected = {"/corpus/Case.pdf", "/corpus/case.pdf"} if case_equivalent else {"/corpus/Case.pdf"}
    assert {str(row["path"]) for row in rows} == expected
    assert truncated is False


@pytest.mark.parametrize(("platform_name", "case_equivalent"), _PLATFORM_CASES)
def test_exact_code_and_catalog_path_lookups_use_platform_collation(
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    case_equivalent: bool,
) -> None:
    monkeypatch.setattr(
        knowledge_exact,
        "_PATH_COLLATION",
        sqlite_path_collation(platform_name=platform_name),
    )
    term = knowledge_exact.ExactLookupTerm(
        knowledge_exact.ExactLookupKind.PATH,
        "/corpus/Case.py",
    )
    with _memory_database() as connection:
        connection.executescript(
            """
            CREATE TABLE files(
                current_version_id INTEGER,volume_id TEXT,physical_file_id TEXT,
                current_path TEXT,status TEXT
            );
            CREATE TABLE file_versions(
                version_id INTEGER,path_observed TEXT,size INTEGER,mtime_ns INTEGER,
                birthtime_ns INTEGER,raw_xxh3_128 TEXT,processing_signature TEXT,
                analyzer_id TEXT,analyzer_version TEXT,parser_kind TEXT,
                analysis_status TEXT,invalidated_ns INTEGER
            );
            INSERT INTO files VALUES(1,'1','1','/corpus/Case.py','current');
            INSERT INTO files VALUES(2,'1','2','/corpus/case.py','current');
            INSERT INTO file_versions VALUES(
                1,'/corpus/Case.py',10,11,-1,'aa','sig','ast','1','ast','complete',NULL
            );
            INSERT INTO file_versions VALUES(
                2,'/corpus/case.py',10,11,-1,'bb','sig','ast','1','ast','complete',NULL
            );
            """
        )
        code_rows, _steps, _truncated = knowledge_exact._code_term_rows(
            connection,
            knowledge_exact._QueryControl(32, 100_000, None),
            term,
            2,
            10,
            None,
        )

    catalog_columns = """
        generation_id INTEGER,source_kind TEXT,file_key TEXT,path TEXT,
        volume_id TEXT,file_id TEXT,birthtime_ns INTEGER,size INTEGER,
        mtime_ns INTEGER,source_status TEXT,processing_signature TEXT,
        classifier_signature TEXT,confidence REAL,uncertainty TEXT,
        standard_references_json TEXT,catalog_status TEXT,updated_ns INTEGER,
        last_seen_catalog_run_id INTEGER,active INTEGER
    """
    with _memory_database() as connection:
        connection.execute(f"CREATE TABLE catalog_generation_documents({catalog_columns})")
        rows = (
            (1, "pdf", "one", "/corpus/Case.py"),
            (1, "pdf", "two", "/corpus/case.py"),
        )
        connection.executemany(
            """INSERT INTO catalog_generation_documents VALUES(
            ?,?,?,?,'1','1',-1,10,11,'done','sig','classifier',1.0,'baja',
            '[]','classified',12,1,1)""",
            rows,
        )
        catalog_rows, _steps, _truncated = knowledge_exact._catalog_term_rows(
            connection,
            knowledge_exact._QueryControl(32, 100_000, None),
            (("pdf", 1),),
            term,
            10,
            None,
            None,
        )

    expected_count = 2 if case_equivalent else 1
    assert len(code_rows) == expected_count
    assert len(catalog_rows) == expected_count


@pytest.mark.parametrize(("platform_name", "case_equivalent"), _PLATFORM_CASES)
def test_non_ascii_path_warning_only_applies_to_windows_nocase(
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    case_equivalent: bool,
) -> None:
    monkeypatch.setattr(
        knowledge_exact,
        "_PATH_COLLATION",
        sqlite_path_collation(platform_name=platform_name),
    )
    term = knowledge_exact.ExactLookupTerm(
        knowledge_exact.ExactLookupKind.PATH,
        "/corpus/Árbol.pdf",
    )

    assert knowledge_exact._non_ascii_case_warning(term) == (
        ("sqlite_nocase_is_ascii_only",) if case_equivalent else ()
    )


@pytest.mark.parametrize(("platform_name", "case_equivalent"), _PLATFORM_CASES)
def test_inventory_relationship_joins_distinguish_linux_case_twins(
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    case_equivalent: bool,
) -> None:
    monkeypatch.setattr(
        knowledge_search_inventory,
        "_PATH_COLLATION",
        sqlite_path_collation(platform_name=platform_name),
    )
    volume = (1).to_bytes(16, "little")
    file_id = (2).to_bytes(16, "little")
    with _memory_database() as connection:
        connection.executescript(
            """
            CREATE TABLE files(
                scan_id INTEGER,path TEXT,volume_id BLOB,file_id BLOB,
                size INTEGER,birthtime_ns INTEGER
            );
            CREATE TABLE duplicate_plan_summaries(
                scan_id INTEGER,completed_ns INTEGER,group_count INTEGER,
                redundant_files INTEGER,reclaimable_bytes INTEGER
            );
            CREATE TABLE planned_duplicate_groups(
                group_id INTEGER,scan_id INTEGER,size INTEGER,redundant_count INTEGER,
                reclaimable_bytes INTEGER,full_fingerprint TEXT,keep_path TEXT
            );
            CREATE TABLE planned_duplicate_members(
                group_id INTEGER,member_order INTEGER,role TEXT,path TEXT,
                volume_id BLOB,file_id BLOB,size INTEGER,birthtime_ns INTEGER
            );
            CREATE INDEX files_identity_birth_scan_idx
            ON files(volume_id,file_id,birthtime_ns,scan_id);
            CREATE INDEX planned_members_identity_idx
            ON planned_duplicate_members(volume_id,file_id,birthtime_ns);
            """
        )
        connection.execute(
            "INSERT INTO files VALUES(1,'/corpus/Case.pdf',?,?,10,-1)",
            (volume, file_id),
        )
        connection.execute("INSERT INTO duplicate_plan_summaries VALUES(1,2,1,0,0)")
        connection.execute(
            "INSERT INTO planned_duplicate_groups VALUES(1,1,10,0,0,'digest','/corpus/case.pdf')"
        )
        connection.execute(
            """INSERT INTO planned_duplicate_members
            VALUES(1,0,'keep','/corpus/case.pdf',?,?,10,-1)""",
            (volume, file_id),
        )

        rows = knowledge_search_inventory._inventory_rows(
            connection,
            ((1, 2, -1),),
            ((1, 2, 1, 0, 0),),
            10,
            lambda value: value.to_bytes(16, "little"),
        )

    assert len(rows) == 1
    assert int(rows[0]["member_present"]) == int(case_equivalent)
    assert int(rows[0]["keep_path_matches"]) == int(case_equivalent)
    assert (rows[0]["keeper_file_path"] is not None) is case_equivalent


@pytest.mark.parametrize(("platform_name", "case_equivalent"), _PLATFORM_CASES)
@pytest.mark.parametrize("isolated_generations", (False, True), ids=("legacy", "isolated"))
def test_semantic_image_dedup_join_uses_platform_path_collation(
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    case_equivalent: bool,
    isolated_generations: bool,
) -> None:
    monkeypatch.setattr(
        semantic_sources,
        "_PATH_COLLATION",
        sqlite_path_collation(platform_name=platform_name),
    )
    digest = b"d" * 16
    with _memory_database() as connection:
        connection.execute("ATTACH DATABASE ':memory:' AS dedup")
        connection.executescript(
            """
            CREATE TABLE images(
                file_key TEXT,path TEXT,size INTEGER,mtime_ns INTEGER,
                birthtime_ns INTEGER,processing_signature TEXT,category TEXT,
                document_candidate INTEGER,adult_classification TEXT,status TEXT
            );
            INSERT INTO images VALUES(
                'image-key','/corpus/Case.jpg',10,11,-1,'sig','photo',0,'safe','done'
            );
            CREATE TABLE dedup.fingerprints(
                volume_id BLOB,file_id BLOB,size INTEGER,mtime_ns INTEGER,
                birthtime_ns INTEGER,algorithm TEXT,digest BLOB
            );
            """
        )
        scan_column = "scan_id INTEGER," if isolated_generations else ""
        primary_key = ",PRIMARY KEY(scan_id,path)" if isolated_generations else ""
        connection.execute(
            f"""CREATE TABLE dedup.files(
            {scan_column}path TEXT,volume_id BLOB,file_id BLOB,size INTEGER,
            mtime_ns INTEGER,birthtime_ns INTEGER{primary_key})"""
        )
        if isolated_generations:
            connection.executescript(
                """
                CREATE TABLE dedup.inventory_checkpoints(
                    scan_id INTEGER,valid INTEGER,updated_ns INTEGER
                );
                INSERT INTO dedup.inventory_checkpoints VALUES(1,1,20);
                """
            )
            connection.execute(
                "INSERT INTO dedup.files VALUES(1,'/corpus/case.jpg',?,?,?,?,?)",
                (b"v", b"f", 10, 11, -1),
            )
        else:
            connection.execute(
                "INSERT INTO dedup.files VALUES('/corpus/case.jpg',?,?,?,?,?)",
                (b"v", b"f", 10, 11, -1),
            )
        connection.execute(
            "INSERT INTO dedup.fingerprints VALUES(?,?,?,?,?,?,?)",
            (b"v", b"f", 10, 11, -1, FULL_ALGORITHM, digest),
        )

        rows = tuple(
            semantic_sources._image_rows(
                Path("image.sqlite3"),
                Path("dedup.sqlite3"),
                connection,
                dedup_attached=True,
            )
        )

    assert len(rows) == 1
    assert rows[0]["full_digest"] == (digest if case_equivalent else None)
