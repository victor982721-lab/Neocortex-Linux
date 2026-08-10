from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from _02_Deduplicacion.inventory_schema import initialize_inventory_schema
from _04_Nucleo_Operativo import value_review_repository
from _04_Nucleo_Operativo.document_catalog_schema import (
    CATALOG_SCHEMA_VERSION,
    create_document_catalog_schema,
)
from _04_Nucleo_Operativo.text_state import initialize_text_state
from _04_Nucleo_Operativo.value_review import preview_value_review
from _04_Nucleo_Operativo.value_review_contracts import (
    ValueDimensionName,
    ValueReviewAvailability,
    ValueReviewPaths,
    ValueReviewQuery,
    ValueReviewState,
)


DAY_NS = 86_400_000_000_000
REFERENCE_NS = 2_000 * DAY_NS
OLD_MTIME_NS = REFERENCE_NS - 365 * DAY_NS
UNIQUE_TEXT_HASH = "c" * 32
REPEATED_TEXT_HASH = "d" * 32


def _blob(value: int) -> bytes:
    return value.to_bytes(16, "little", signed=False)


def _fixture_files() -> tuple[dict[str, object], ...]:
    return (
        {"id": 1, "path": "/corpus/docs/keeper.txt", "size": 100},
        {"id": 2, "path": "/corpus/copies/redundant.txt", "size": 100},
        {"id": 3, "path": "/corpus/docs/unique.txt", "size": 800},
        {"id": 4, "path": "/corpus/tmp/repeated.log", "size": 400},
        {"id": 5, "path": "/corpus/archive/repeated.txt", "size": 450},
        {"id": 6, "path": "/corpus/docs/unknown.bin", "size": 900},
        {"id": 7, "path": "/corpus/tmp/empty.tmp", "size": 0},
    )


def _create_inventory(path: Path, *, invalid_duplicate_hash: bool = False) -> None:
    initialize_inventory_schema(path)
    files = _fixture_files()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO scans(
            scan_id,root,started_ns,completed_ns,files_seen,directories_seen,
            bytes_seen,skipped_links,excluded_directories,errors,status,
            inventory_policy_signature)
            VALUES(1,'/corpus',1,2,?,?,?,0,0,0,'complete','fixture-v1')""",
            (len(files), 5, sum(int(value["size"]) for value in files)),
        )
        connection.execute(
            """INSERT INTO inventory_checkpoints(
            root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
            VALUES('/corpus',1,NULL,NULL,NULL,1,3)"""
        )
        connection.executemany(
            """INSERT INTO files(
            scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
            VALUES(1,?,?,?,?,?,-1)""",
            tuple(
                (
                    str(value["path"]),
                    _blob(11),
                    _blob(int(value["id"])),
                    int(value["size"]),
                    OLD_MTIME_NS,
                )
                for value in files
            ),
        )
        connection.execute(
            """INSERT INTO duplicate_plan_summaries(
            scan_id,group_count,redundant_files,reclaimable_bytes,completed_ns)
            VALUES(1,1,1,100,4)"""
        )
        connection.execute(
            """INSERT INTO planned_duplicate_groups(
            group_id,scan_id,size,keep_path,redundant_count,reclaimable_bytes,
            full_fingerprint) VALUES(10,1,100,'/corpus/docs/keeper.txt',1,100,?)""",
            ("invalid" if invalid_duplicate_hash else "a" * 32,),
        )
        connection.executemany(
            """INSERT INTO planned_duplicate_members(
            group_id,member_order,role,path,volume_id,file_id,size,mtime_ns,
            birthtime_ns) VALUES(10,?,?,?,?,?,?,?,-1)""",
            (
                (0, "keep", "/corpus/docs/keeper.txt", _blob(11), _blob(1), 100, OLD_MTIME_NS),
                (
                    1,
                    "redundant",
                    "/corpus/copies/redundant.txt",
                    _blob(11),
                    _blob(2),
                    100,
                    OLD_MTIME_NS,
                ),
            ),
        )


def _insert_catalog_document(
    connection: sqlite3.Connection,
    value: dict[str, object],
    *,
    text_fingerprint: str,
    source_status: str = "complete",
    catalog_status: str = "classified",
    error_type: str | None = None,
    error_message: str | None = None,
) -> None:
    file_id = int(value["id"])
    connection.execute(
        """INSERT INTO catalog_generation_documents(
        generation_id,source_kind,file_key,path,volume_id,file_id,size,mtime_ns,
        birthtime_ns,source_status,processing_signature,text_fingerprint,
        classifier_signature,primary_kind,primary_subtype,primary_authority,
        primary_organization,primary_client,primary_project,primary_workstream,
        confidence,uncertainty,standard_references_json,organizations_json,
        clients_json,projects_json,workstreams_json,topics_json,equipment_json,
        activities_json,classification_json,catalog_status,error_type,error_message,
        active,last_seen_catalog_run_id,updated_ns)
        VALUES(1,'text',?,?,?,?,?,?,-1,?,'extract-v1',?,'classifier-v1',
        'otro',NULL,NULL,NULL,NULL,NULL,NULL,0.9,'baja','[]','[]','[]','[]','[]',
        '[]','[]','[]','{}',?,?,?,1,1,?)""",
        (
            f"11:{file_id}",
            str(value["path"]),
            "11",
            str(file_id),
            int(value["size"]),
            OLD_MTIME_NS,
            source_status,
            text_fingerprint,
            catalog_status,
            error_type,
            error_message,
            REFERENCE_NS,
        ),
    )


def _create_catalog(path: Path) -> None:
    files = {int(value["id"]): value for value in _fixture_files()}
    with sqlite3.connect(path) as connection:
        create_document_catalog_schema(connection)
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES('schema_version',?)",
            (str(CATALOG_SCHEMA_VERSION),),
        )
        connection.execute(
            """INSERT INTO catalog_generations(
            generation_id,catalog_run_id,source_kind,base_generation_id,status,
            started_ns,completed_ns,published_ns,error_type,error_message)
            VALUES(1,NULL,'text',NULL,'published',1,2,3,NULL,NULL)"""
        )
        connection.execute(
            """INSERT INTO catalog_publications(source_kind,generation_id,published_ns)
            VALUES('text',1,3)"""
        )
        _insert_catalog_document(
            connection,
            files[3],
            text_fingerprint=UNIQUE_TEXT_HASH,
        )
        _insert_catalog_document(
            connection,
            files[4],
            text_fingerprint=REPEATED_TEXT_HASH,
        )
        _insert_catalog_document(
            connection,
            files[5],
            text_fingerprint=REPEATED_TEXT_HASH,
        )


def _create_state(
    root: Path,
    *,
    invalid_duplicate_hash: bool = False,
) -> ValueReviewPaths:
    root.mkdir()
    paths = ValueReviewPaths.from_directory(root)
    _create_inventory(paths.inventory, invalid_duplicate_hash=invalid_duplicate_hash)
    _create_catalog(paths.catalog)
    assert paths.text is not None
    initialize_text_state(paths.text)
    return paths


def _filesystem_snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    result: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        payload = path.read_bytes()
        result[path.name] = (
            path.stat().st_mtime_ns,
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        )
    return result


def _states_by_path(report: object) -> dict[str, ValueReviewState]:
    return {item.path: item.state for item in report.items}  # type: ignore[attr-defined]


def test_sqlite_preview_uses_only_published_evidence_and_all_five_states(
    tmp_path: Path,
) -> None:
    paths = _create_state(tmp_path / "state")
    report = preview_value_review(
        paths,
        ValueReviewQuery(limit=100, reference_time_ns=REFERENCE_NS),
    )
    states = _states_by_path(report)

    assert report.availability is ValueReviewAvailability.READY
    assert report.complete is True
    assert report.candidate_count == 7
    assert states["/corpus/docs/keeper.txt"] is ValueReviewState.KEEP
    assert states["/corpus/copies/redundant.txt"] is ValueReviewState.EXACT_DUPLICATE_CANDIDATE
    assert states["/corpus/docs/unique.txt"] is ValueReviewState.KEEP
    assert states["/corpus/tmp/repeated.log"] is ValueReviewState.REVIEW_LOW_VALUE
    assert states["/corpus/archive/repeated.txt"] is ValueReviewState.ARCHIVE_CANDIDATE
    assert states["/corpus/docs/unknown.bin"] is ValueReviewState.UNKNOWN
    assert states["/corpus/tmp/empty.tmp"] is ValueReviewState.UNKNOWN
    assert set(states.values()) == set(ValueReviewState)


def test_sqlite_preview_is_replay_deterministic_and_does_not_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    before = _filesystem_snapshot(root)
    query = ValueReviewQuery(limit=100, reference_time_ns=REFERENCE_NS)

    first = preview_value_review(paths, query)
    middle = _filesystem_snapshot(root)
    second = preview_value_review(paths, query)
    after = _filesystem_snapshot(root)

    assert first.to_json() == second.to_json()
    assert before == middle == after
    assert sorted(before) == ["dedup.sqlite3", "document_catalog.sqlite3", "text.sqlite3"]


def test_absent_state_is_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "missing-state"
    paths = ValueReviewPaths.from_directory(missing)

    report = preview_value_review(paths, ValueReviewQuery())

    assert report.availability is ValueReviewAvailability.UNAVAILABLE
    assert report.reason == "inventory_state_absent"
    assert report.items == ()
    assert not missing.exists()


def test_absent_catalog_is_not_created_and_absence_is_not_low_value(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    paths = ValueReviewPaths.from_directory(root)
    _create_inventory(paths.inventory)
    before = _filesystem_snapshot(root)

    report = preview_value_review(
        paths,
        ValueReviewQuery(limit=100, reference_time_ns=REFERENCE_NS),
    )
    states = _states_by_path(report)

    assert report.availability is ValueReviewAvailability.PARTIAL
    assert report.reason == "catalog_state_absent"
    assert states["/corpus/tmp/empty.tmp"] is ValueReviewState.UNKNOWN
    assert not paths.catalog.exists()
    assert _filesystem_snapshot(root) == before


def test_inactive_empty_wal_and_32k_shm_are_allowed_without_writes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    assert paths.text is not None
    for database in (paths.inventory, paths.catalog, paths.text):
        Path(f"{database}-wal").write_bytes(b"")
        Path(f"{database}-shm").write_bytes(b"\0" * 32_768)
    before = _filesystem_snapshot(root)

    report = preview_value_review(
        paths,
        ValueReviewQuery(limit=100, reference_time_ns=REFERENCE_NS),
    )

    assert report.availability is ValueReviewAvailability.READY
    assert report.candidate_count == 7
    assert _filesystem_snapshot(root) == before


@pytest.mark.parametrize("suffix", ("-wal", "-journal"))
def test_non_empty_wal_or_journal_fails_closed_without_changing_files(
    tmp_path: Path,
    suffix: str,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    Path(f"{paths.inventory}{suffix}").write_bytes(b"active-frame-or-journal")
    before = _filesystem_snapshot(root)

    report = preview_value_review(paths, ValueReviewQuery())

    assert report.availability is ValueReviewAvailability.UNAVAILABLE
    assert report.reason == "inventory_state_invalid"
    assert _filesystem_snapshot(root) == before


@pytest.mark.parametrize("changed_target", ("main", "shm"))
def test_main_or_sidecar_change_during_immutable_read_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_target: str,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    wal = Path(f"{paths.inventory}-wal")
    shm = Path(f"{paths.inventory}-shm")
    wal.write_bytes(b"")
    shm.write_bytes(b"\0" * 32_768)
    original_snapshot = value_review_repository._sqlite_read_snapshot
    calls = 0

    def changing_snapshot(path: Path) -> object:
        nonlocal calls
        calls += 1
        if calls == 3:
            target = paths.inventory if changed_target == "main" else shm
            # A one-nanosecond ``utime`` delta can round back to the same NTFS
            # timestamp on Windows.  Changing size gives the identity fence a
            # deterministic, cross-platform mutation to reject.
            with target.open("ab") as stream:
                stream.write(b"\0")
        return original_snapshot(path)

    monkeypatch.setattr(
        value_review_repository,
        "_sqlite_read_snapshot",
        changing_snapshot,
    )

    report = preview_value_review(paths, ValueReviewQuery())

    assert calls == 3
    assert report.availability is ValueReviewAvailability.UNAVAILABLE
    assert report.reason == "inventory_state_invalid"


def test_future_catalog_schema_protects_every_file_and_is_not_changed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    with sqlite3.connect(paths.catalog) as connection:
        connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
    before = _filesystem_snapshot(root)

    report = preview_value_review(
        paths,
        ValueReviewQuery(limit=100, reference_time_ns=REFERENCE_NS),
    )

    assert report.availability is ValueReviewAvailability.PARTIAL
    assert report.reason == "catalog_state_incompatible"
    assert all(
        item.state in {ValueReviewState.UNKNOWN, ValueReviewState.KEEP} for item in report.items
    )
    assert _filesystem_snapshot(root) == before


def test_incompatible_source_owner_never_becomes_low_value(
    tmp_path: Path,
) -> None:
    paths = _create_state(tmp_path / "state")
    assert paths.text is not None
    with sqlite3.connect(paths.text) as connection:
        connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")

    report = preview_value_review(
        paths,
        ValueReviewQuery(limit=100, reference_time_ns=REFERENCE_NS),
    )
    states = _states_by_path(report)

    assert report.availability is ValueReviewAvailability.PARTIAL
    assert report.reason == "source_owner_health_incomplete"
    assert states["/corpus/tmp/repeated.log"] is ValueReviewState.UNKNOWN
    assert states["/corpus/archive/repeated.txt"] is ValueReviewState.UNKNOWN


def test_partial_and_encrypted_rows_are_protected(tmp_path: Path) -> None:
    paths = _create_state(tmp_path / "state")
    with sqlite3.connect(paths.catalog) as connection:
        connection.execute(
            """UPDATE catalog_generation_documents
            SET source_status='partial' WHERE file_id='4'"""
        )
        connection.execute(
            """UPDATE catalog_generation_documents
            SET catalog_status='error',error_type='EncryptedFile',
            error_message='password required' WHERE file_id='5'"""
        )

    report = preview_value_review(
        paths,
        ValueReviewQuery(limit=100, reference_time_ns=REFERENCE_NS),
    )
    states = _states_by_path(report)

    assert states["/corpus/tmp/repeated.log"] is ValueReviewState.UNKNOWN
    assert states["/corpus/archive/repeated.txt"] is ValueReviewState.UNKNOWN


def test_catalog_snapshot_mismatch_protects_the_affected_file(tmp_path: Path) -> None:
    paths = _create_state(tmp_path / "state")
    with sqlite3.connect(paths.catalog) as connection:
        connection.execute(
            """UPDATE catalog_generation_documents
            SET size=size+1 WHERE file_id='4'"""
        )

    report = preview_value_review(
        paths,
        ValueReviewQuery(limit=100, reference_time_ns=REFERENCE_NS),
    )
    states = _states_by_path(report)

    assert report.availability is ValueReviewAvailability.PARTIAL
    assert report.reason == "catalog_snapshot_mismatch"
    assert states["/corpus/tmp/repeated.log"] is ValueReviewState.UNKNOWN


def test_root_scope_is_a_real_sql_prefilter(tmp_path: Path) -> None:
    paths = _create_state(tmp_path / "state")

    report = preview_value_review(
        paths,
        ValueReviewQuery(scope="/", limit=100, reference_time_ns=REFERENCE_NS),
    )

    assert report.candidate_count == 7


def test_invalid_duplicate_plan_hash_cannot_create_exact_candidate(
    tmp_path: Path,
) -> None:
    paths = _create_state(tmp_path / "state", invalid_duplicate_hash=True)

    report = preview_value_review(
        paths,
        ValueReviewQuery(limit=100, reference_time_ns=REFERENCE_NS),
    )
    states = _states_by_path(report)

    assert states["/corpus/copies/redundant.txt"] is ValueReviewState.UNKNOWN
    assert any("duplicate_plan_invalid" in item for item in report.uncertainties)


def test_sqlite_scope_extensions_source_and_state_prefilters(tmp_path: Path) -> None:
    paths = _create_state(tmp_path / "state")

    report = preview_value_review(
        paths,
        ValueReviewQuery(
            scope="/corpus/tmp",
            extensions=("log",),
            source_kinds=("text",),
            states=(ValueReviewState.REVIEW_LOW_VALUE,),
            reference_time_ns=REFERENCE_NS,
        ),
    )

    assert report.candidate_count == 1
    assert [item.path for item in report.items] == ["/corpus/tmp/repeated.log"]


def test_no_history_dimensions_remain_unknown_in_sqlite_preview(tmp_path: Path) -> None:
    paths = _create_state(tmp_path / "state")
    report = preview_value_review(
        paths,
        ValueReviewQuery(limit=100, reference_time_ns=REFERENCE_NS),
    )
    duplicate = next(item for item in report.items if item.path == "/corpus/copies/redundant.txt")
    dimensions = {item.name: item for item in duplicate.dimensions}

    assert dimensions[ValueDimensionName.COVERAGE].value is None
    assert dimensions[ValueDimensionName.CITATIONS].value is None
    assert dimensions[ValueDimensionName.USAGE].value is None
    assert duplicate.mutation_authorized is False
