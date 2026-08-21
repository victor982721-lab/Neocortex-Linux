from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from neocortex.deduplication.schema import initialize_inventory_schema
from _04_Nucleo_Operativo import review_task_repository, value_review_repository
from _04_Nucleo_Operativo.document_catalog_schema import (
    CATALOG_SCHEMA_VERSION,
    create_document_catalog_schema,
)
from _04_Nucleo_Operativo.framework_state_writer import FrameworkState
from _04_Nucleo_Operativo.text_state import initialize_text_state
from _04_Nucleo_Operativo.review_task_contracts import (
    CanonicalJsonObject,
    ReviewTaskActorKind,
    ReviewTaskState,
    ReviewTaskTransition,
)
from _04_Nucleo_Operativo.review_task_repository import (
    append_review_task_event,
    list_current_review_tasks,
)
from _04_Nucleo_Operativo.value_review import preview_value_review
from _04_Nucleo_Operativo.value_review_contracts import (
    ValueDimensionName,
    ValueReviewAvailability,
    ValueReviewPaths,
    ValueReviewQuery,
    ValueReviewState,
)
from _04_Nucleo_Operativo.value_review_tasks import (
    ValueReviewTaskQueueStatus,
    ValueReviewTaskStateError,
    read_value_review_task_queue,
    refresh_value_review_tasks,
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


def test_value_review_keyset_pages_cover_each_resource_once_without_writes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    before = _filesystem_snapshot(root)
    source_before = value_review_repository.read_value_review_source_snapshot(paths)
    query = ValueReviewQuery(limit=100, reference_time_ns=REFERENCE_NS)

    cursor = None
    page_sizes: list[int] = []
    resource_ids: list[str] = []
    for _ in range(4):
        page = value_review_repository.load_value_review_observation_page(
            paths,
            query,
            page_size=3,
            after=cursor,
        )
        assert page.availability is ValueReviewAvailability.READY
        assert page.cursor_before == cursor
        assert page.scanned_count == len(page.observations)
        page_sizes.append(page.scanned_count)
        resource_ids.extend(item.resource_id for item in page.observations)
        if page.complete:
            assert page.cursor_after is None
            break
        assert page.cursor_after is not None
        cursor = page.cursor_after
    else:  # pragma: no cover - bounded loop defense
        raise AssertionError("keyset scan did not finish")

    source_after = value_review_repository.read_value_review_source_snapshot(paths)
    assert page_sizes == [3, 3, 1]
    assert len(resource_ids) == len(set(resource_ids)) == 7
    assert source_before.payload_json == source_after.payload_json
    assert source_before.availability is ValueReviewAvailability.READY
    assert before == _filesystem_snapshot(root)


def test_value_review_source_snapshot_changes_when_published_inventory_head_changes(
    tmp_path: Path,
) -> None:
    paths = _create_state(tmp_path / "state")
    before = value_review_repository.read_value_review_source_snapshot(paths)

    with sqlite3.connect(paths.inventory) as connection:
        connection.execute(
            "UPDATE inventory_checkpoints SET updated_ns=updated_ns+1 WHERE root='/corpus'"
        )

    after = value_review_repository.read_value_review_source_snapshot(paths)
    assert before.payload_json != after.payload_json
    assert before.to_dict()["inventory"] != after.to_dict()["inventory"]


@pytest.mark.parametrize("page_size", (0, 1_001, True))
def test_value_review_page_rejects_unbounded_page_sizes(
    tmp_path: Path,
    page_size: object,
) -> None:
    paths = _create_state(tmp_path / "state")
    with pytest.raises(ValueError, match="page_size"):
        value_review_repository.load_value_review_observation_page(
            paths,
            ValueReviewQuery(reference_time_ns=REFERENCE_NS),
            page_size=page_size,  # type: ignore[arg-type]
        )


def test_value_review_tasks_publish_once_and_queue_reads_are_replay_safe(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    framework = root / "framework.sqlite3"

    first = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS,
    )
    assert first.status == "complete"
    assert first.wrote_state is True
    assert first.publication is not None
    assert first.publication.progress.scanned_count == 7
    assert first.publication.progress.selected_count == 3
    assert first.publication.progress.complete is True
    before_replay = framework.read_bytes()

    queue = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS,
    )
    assert queue.status is ValueReviewTaskQueueStatus.READY
    assert queue.complete is True
    assert {record.task.reason_code for record in queue.records} == {
        "archive_candidate",
        "exact_duplicate_candidate",
        "review_low_value",
    }
    assert all(record.state.value == "open" for record in queue.records)
    report = queue.report_dict()
    assert report["advisory_only"] is True
    assert report["mutation_authorized"] is False
    assert report["returned_count"] == 3
    assert report["matched_count"] == 3
    report_items = report["items"]
    assert isinstance(report_items, list)
    for item in report_items:
        assert isinstance(item, dict)
        review_task = item.get("review_task")
        assert isinstance(review_task, dict)
        assert review_task["state"] == "open"
        assert review_task["task_id"]

    replay = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS,
    )
    assert replay.status == "complete"
    assert replay.wrote_state is False
    assert replay.publication is None
    assert framework.read_bytes() == before_replay


def test_corrupt_framework_state_fails_closed_for_queue_and_refresh(tmp_path: Path) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    framework = root / "framework.sqlite3"
    framework.write_bytes(b"not-a-sqlite-database")

    with pytest.raises(ValueReviewTaskStateError, match="framework_state_corrupt"):
        read_value_review_task_queue(
            framework,
            paths,
            scope="personal",
            limit=10,
            reference_time_ns=REFERENCE_NS,
        )
    with pytest.raises(ValueReviewTaskStateError, match="framework_state_corrupt"):
        refresh_value_review_tasks(
            framework,
            paths,
            scope="personal",
            clock_ns=lambda: REFERENCE_NS,
        )
    assert framework.read_bytes() == b"not-a-sqlite-database"


def test_value_review_queue_fails_stale_until_changed_source_is_refreshed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    framework = root / "framework.sqlite3"
    first = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS,
    )
    assert first.publication is not None

    with sqlite3.connect(paths.inventory) as connection:
        connection.execute(
            "UPDATE inventory_checkpoints SET updated_ns=updated_ns+1 WHERE root='/corpus'"
        )

    stale = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS,
    )
    assert stale.status is ValueReviewTaskQueueStatus.STALE
    assert stale.reason == "review_task_source_changed"
    assert stale.records
    assert stale.fence is not None
    assert stale.fence.source_snapshot_fingerprint == (first.fence.source_snapshot_fingerprint)

    refreshed = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS + 1,
    )
    assert refreshed.publication is not None
    current = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS,
    )
    assert current.status is ValueReviewTaskQueueStatus.READY
    assert {record.task.task_version for record in current.records} == {2}


def test_value_review_refresh_never_reopens_human_terminal_task(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    framework = root / "framework.sqlite3"
    first = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS,
    )
    assert first.publication is not None
    before = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS,
    )
    resolved_keys = tuple(record.task.logical_key for record in before.records)
    for index, record in enumerate(before.records, start=1):
        append_review_task_event(
            framework,
            ReviewTaskTransition(
                event_id=f"value-human-resolved-{index}",
                event_key=f"value-human-resolved-key-{index}",
                task_id=record.task.task_id,
                expected_event_id=record.current_event.event_id,
                expected_state=ReviewTaskState.OPEN,
                to_state=ReviewTaskState.RESOLVED,
                actor_kind=ReviewTaskActorKind.HUMAN,
                actor_id="fixture-reviewer",
                provenance=CanonicalJsonObject.from_mapping({"surface": "test"}),
                decision=CanonicalJsonObject.from_mapping({"outcome": "confirmed"}),
                note="confirmed",
                observed_ns=REFERENCE_NS + index,
                recorded_ns=REFERENCE_NS + index,
            ),
        )
    with sqlite3.connect(paths.inventory) as connection:
        connection.execute(
            "UPDATE inventory_checkpoints SET updated_ns=updated_ns+1 WHERE root='/corpus'"
        )

    stale = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS,
    )
    assert stale.status is ValueReviewTaskQueueStatus.STALE

    refreshed = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS + 2,
    )
    assert refreshed.publication is not None
    current = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS,
    )
    assert current.records == ()
    assert current.report_dict()["matched_count"] == 0
    with sqlite3.connect(framework) as connection:
        versions = {
            key: connection.execute(
                "SELECT task_version FROM review_tasks WHERE logical_key=?",
                (key,),
            ).fetchall()
            for key in resolved_keys
        }
    assert all(value == [(1,)] for value in versions.values())


def test_value_review_reopens_until_source_change_but_not_permanent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    framework = root / "framework.sqlite3"
    first = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS,
    )
    assert first.status == "complete"
    before = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS,
    )
    records = tuple(before.records[:2])
    assert len(records) == 2
    for index, (record, decision_scope) in enumerate(
        zip(records, ("until-source-change", "permanent"), strict=True),
        start=1,
    ):
        append_review_task_event(
            framework,
            ReviewTaskTransition(
                event_id=f"value-scoped-terminal-{index}",
                event_key=f"value-scoped-terminal-key-{index}",
                task_id=record.task.task_id,
                expected_event_id=record.current_event.event_id,
                expected_state=ReviewTaskState.OPEN,
                to_state=ReviewTaskState.RESOLVED,
                actor_kind=ReviewTaskActorKind.HUMAN,
                actor_id="fixture-reviewer",
                provenance=CanonicalJsonObject.from_mapping({"surface": "test"}),
                decision=CanonicalJsonObject.from_mapping(
                    {
                        "decision": "resolved",
                        "schema": "neocortex.review-task-decision/v1",
                        "scope": decision_scope,
                        "selector_signature": record.selector_signature,
                        "source_input_fingerprint": record.source.fingerprint,
                        "source_snapshot_fingerprint": (record.source_snapshot_fingerprint),
                    }
                ),
                note="confirmed",
                observed_ns=REFERENCE_NS + index,
                recorded_ns=REFERENCE_NS + index,
            ),
        )

    with sqlite3.connect(paths.inventory) as connection:
        connection.execute(
            "UPDATE inventory_checkpoints SET updated_ns=updated_ns+1 WHERE root='/corpus'"
        )
        for record in records:
            assert record.source.resource is not None
            path = record.source.resource.current_path
            connection.execute(
                "UPDATE files SET mtime_ns=mtime_ns+1 WHERE path=?",
                (path,),
            )
            connection.execute(
                "UPDATE planned_duplicate_members SET mtime_ns=mtime_ns+1 WHERE path=?",
                (path,),
            )

    with sqlite3.connect(paths.catalog) as connection:
        for record in records:
            assert record.source.resource is not None
            connection.execute(
                "UPDATE catalog_generation_documents SET mtime_ns=mtime_ns+1 WHERE path=?",
                (record.source.resource.current_path,),
            )

    refreshed = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS + 10,
    )
    assert refreshed.publication is not None
    assert refreshed.publication.progress.complete
    heads = review_task_repository.lookup_review_task_version_heads(
        framework,
        tuple(record.task.logical_key for record in records),
        scope="personal",
        task_type="value-review",
    )
    by_key = {head.logical_key: head for head in heads}
    assert by_key[records[0].task.logical_key].task_version == 2
    assert by_key[records[0].task.logical_key].state is ReviewTaskState.OPEN
    assert by_key[records[1].task.logical_key].task_version == 1
    assert by_key[records[1].task.logical_key].state is ReviewTaskState.RESOLVED


def test_empty_durable_scan_becomes_stale_when_its_source_head_changes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    paths = ValueReviewPaths.from_directory(root)
    initialize_inventory_schema(paths.inventory)
    with sqlite3.connect(paths.inventory) as connection:
        connection.execute(
            """INSERT INTO scans(
            scan_id,root,started_ns,completed_ns,files_seen,directories_seen,
            bytes_seen,skipped_links,excluded_directories,errors,status,
            inventory_policy_signature)
            VALUES(1,'/corpus',1,2,0,1,0,0,0,0,'complete','fixture-v1')"""
        )
        connection.execute(
            """INSERT INTO inventory_checkpoints(
            root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
            VALUES('/corpus',1,NULL,NULL,NULL,1,3)"""
        )

    framework = root / "framework.sqlite3"
    refreshed = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS,
    )
    assert refreshed.status == "partial"
    assert refreshed.reason == "catalog_state_absent"
    assert refreshed.publication is not None
    assert refreshed.publication.progress.selected_count == 0
    assert refreshed.publication.progress.complete is True
    assert refreshed.publication.progress.evidence_complete is False
    current = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS,
    )
    assert current.status is ValueReviewTaskQueueStatus.PARTIAL
    assert current.reason == "catalog_state_absent"
    assert current.complete is False
    assert current.records == ()

    with sqlite3.connect(paths.inventory) as connection:
        connection.execute("UPDATE inventory_checkpoints SET updated_ns=4 WHERE root='/corpus'")
    stale = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS,
    )
    assert stale.status is ValueReviewTaskQueueStatus.STALE
    assert stale.records == ()


def test_missing_published_inventory_head_is_partial_and_never_retires_open_tasks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    framework = root / "framework.sqlite3"
    initial = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS,
    )
    assert initial.status == "complete"
    before = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS,
    )
    old_task_ids = {record.task.task_id for record in before.records}
    assert len(old_task_ids) == 3

    with sqlite3.connect(paths.inventory) as connection:
        connection.execute("UPDATE inventory_checkpoints SET valid=0 WHERE root='/corpus'")
    refreshed = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS + 1,
    )
    assert refreshed.status == "partial"
    assert refreshed.reason == "inventory_has_no_published_scans"
    assert refreshed.publication is not None
    assert refreshed.publication.progress.complete is True
    assert refreshed.publication.progress.evidence_complete is False
    assert refreshed.publication.progress.evidence_reason == ("inventory_has_no_published_scans")

    records = list_current_review_tasks(
        framework,
        limit=100,
        scope="personal",
        task_type="value-review",
        states=tuple(ReviewTaskState),
    )
    states_by_id = {
        record.task.task_id: record.state
        for record in records.items
        if record.task.task_id in old_task_ids
    }
    assert states_by_id == dict.fromkeys(old_task_ids, ReviewTaskState.OPEN)
    queue = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS,
    )
    assert queue.status is ValueReviewTaskQueueStatus.PARTIAL
    assert queue.reason == "inventory_has_no_published_scans"
    assert queue.records == ()


def test_catalog_row_mismatch_is_durable_partial_evidence_not_ready(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    with sqlite3.connect(paths.catalog) as connection:
        connection.execute("UPDATE catalog_generation_documents SET size=size+1 WHERE file_id='4'")

    framework = root / "framework.sqlite3"
    refreshed = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS,
    )

    assert refreshed.status == "partial"
    assert refreshed.reason == "catalog_snapshot_mismatch"
    assert refreshed.publication is not None
    assert refreshed.publication.progress.complete is True
    assert refreshed.publication.progress.evidence_complete is False
    assert refreshed.publication.progress.evidence_reason == "catalog_snapshot_mismatch"
    refresh_payload = refreshed.to_dict()
    progress_payload = refresh_payload["progress"]
    assert isinstance(progress_payload, dict)
    assert progress_payload["evidence_complete"] is False
    assert progress_payload["evidence_reason"] == "catalog_snapshot_mismatch"
    queue = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS,
    )
    assert queue.status is ValueReviewTaskQueueStatus.PARTIAL
    assert queue.reason == "catalog_snapshot_mismatch"
    assert queue.complete is False
    report = queue.report_dict()
    assert report["availability"] == ValueReviewAvailability.PARTIAL.value
    assert report["complete"] is False
    queue_payload = report["queue"]
    assert isinstance(queue_payload, dict)
    assert queue_payload["scan_complete"] is True
    assert queue_payload["evidence_complete"] is False
    assert queue_payload["evidence_reason"] == "catalog_snapshot_mismatch"


def test_partial_refresh_never_supersedes_findings_missing_with_catalog_owner(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    framework = root / "framework.sqlite3"
    initial = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS,
    )
    assert initial.status == "complete"
    before = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS,
    )
    protected = {
        record.task.task_id: record.task.reason_code
        for record in before.records
        if record.task.reason_code in {"archive_candidate", "review_low_value"}
    }
    assert set(protected.values()) == {"archive_candidate", "review_low_value"}

    paths.catalog.rename(root / "catalog.missing")
    refreshed = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS + 1,
    )
    assert refreshed.status == "partial"
    assert refreshed.reason == "catalog_state_absent"
    assert refreshed.publication is not None
    assert refreshed.publication.progress.evidence_complete is False

    records = list_current_review_tasks(
        framework,
        limit=100,
        scope="personal",
        task_type="value-review",
        states=tuple(ReviewTaskState),
    )
    states_by_id = {
        record.task.task_id: record.state
        for record in records.items
        if record.task.task_id in protected
    }
    assert states_by_id == dict.fromkeys(protected, ReviewTaskState.OPEN)
    queue = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS,
    )
    assert queue.status is ValueReviewTaskQueueStatus.PARTIAL
    assert queue.reason == "catalog_state_absent"
    assert queue.complete is False


def test_invalid_duplicate_evidence_never_retires_open_exact_finding(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    framework = root / "framework.sqlite3"
    initial = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS,
    )
    assert initial.status == "complete"
    before = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS,
    )
    exact = next(
        record
        for record in before.records
        if record.task.reason_code == "exact_duplicate_candidate"
    )

    with sqlite3.connect(paths.inventory) as connection:
        connection.execute(
            "UPDATE planned_duplicate_groups SET full_fingerprint='invalid' WHERE group_id=10"
        )
    refreshed = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS + 1,
    )
    assert refreshed.status == "partial"
    assert refreshed.reason == "duplicate_evidence_incomplete"
    assert refreshed.publication is not None
    assert refreshed.publication.progress.evidence_complete is False

    records = list_current_review_tasks(
        framework,
        limit=100,
        scope="personal",
        task_type="value-review",
        states=tuple(ReviewTaskState),
    )
    original = next(record for record in records.items if record.task.task_id == exact.task.task_id)
    assert original.state is ReviewTaskState.OPEN


def test_empty_scan_without_duplicate_plan_cannot_publish_complete_absence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    framework = root / "framework.sqlite3"
    initial = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS,
    )
    assert initial.status == "complete"
    before = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS,
    )
    old_task_ids = {record.task.task_id for record in before.records}
    assert len(old_task_ids) == 3

    with sqlite3.connect(paths.inventory) as connection:
        connection.execute(
            """INSERT INTO scans(
            scan_id,root,started_ns,completed_ns,files_seen,directories_seen,
            bytes_seen,skipped_links,excluded_directories,errors,status,
            inventory_policy_signature)
            VALUES(2,'/corpus',5,6,0,1,0,0,0,0,'complete','fixture-v2')"""
        )
        connection.execute(
            "UPDATE inventory_checkpoints SET scan_id=2,updated_ns=7 WHERE root='/corpus'"
        )

    refreshed = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS + 1,
    )
    assert refreshed.status == "partial"
    assert refreshed.reason == "duplicate_evidence_incomplete"
    assert refreshed.publication is not None
    assert refreshed.publication.progress.complete is True
    assert refreshed.publication.progress.evidence_complete is False
    assert refreshed.publication.progress.evidence_reason == ("duplicate_evidence_incomplete")

    records = list_current_review_tasks(
        framework,
        limit=100,
        scope="personal",
        task_type="value-review",
        states=tuple(ReviewTaskState),
    )
    states_by_id = {
        record.task.task_id: record.state
        for record in records.items
        if record.task.task_id in old_task_ids
    }
    assert states_by_id == dict.fromkeys(old_task_ids, ReviewTaskState.OPEN)


def test_value_review_queue_stales_when_ranked_source_owner_disappears(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    framework = root / "framework.sqlite3"
    refreshed = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS,
    )
    assert refreshed.fence is not None
    original_fingerprint = refreshed.fence.source_snapshot_fingerprint

    paths.text.rename(root / "text.missing")

    stale = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS,
    )
    assert stale.status is ValueReviewTaskQueueStatus.STALE
    assert stale.fence is not None
    assert stale.fence.source_snapshot_fingerprint == original_fingerprint
    assert stale.reason == "review_task_source_changed"
    assert stale.records


def test_value_review_refresh_abstains_if_source_changes_before_owner_commit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    framework = root / "framework.sqlite3"
    checkpoints = 0

    def mutate_between_read_and_publish() -> None:
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 2:
            with sqlite3.connect(paths.inventory) as connection:
                connection.execute(
                    "UPDATE inventory_checkpoints SET updated_ns=updated_ns+1 WHERE root='/corpus'"
                )

    result = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS,
        cancellation_check=mutate_between_read_and_publish,
    )

    assert result.status == "snapshot_changed"
    assert result.wrote_state is False
    assert not framework.exists()


def test_value_review_refresh_maps_progress_cas_race_without_partial_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    framework = root / "framework.sqlite3"
    with FrameworkState(framework):
        pass

    def lose_progress_cas(*_args: object, **_kwargs: object) -> object:
        raise review_task_repository.ReviewTaskCASConflict("concurrent refresh won")

    monkeypatch.setattr(
        review_task_repository,
        "publish_review_task_page",
        lose_progress_cas,
    )
    result = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS,
    )

    assert result.status == "snapshot_changed"
    assert result.reason == "review_task_progress_changed_before_publication"
    assert result.publication is None
    assert result.wrote_state is False
    with sqlite3.connect(framework) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_task_batches").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM review_tasks").fetchone() == (0,)


def test_value_review_refresh_pages_scope_larger_than_legacy_safety_limit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    paths = ValueReviewPaths.from_directory(root)
    initialize_inventory_schema(paths.inventory)
    file_count = value_review_repository.MAX_SQLITE_CANDIDATES + 1
    with sqlite3.connect(paths.inventory) as connection:
        connection.execute(
            """INSERT INTO scans(
            scan_id,root,started_ns,completed_ns,files_seen,directories_seen,
            bytes_seen,skipped_links,excluded_directories,errors,status,
            inventory_policy_signature)
            VALUES(1,'/corpus',1,2,?,1,?,0,0,0,'complete','fixture-v1')""",
            (file_count, file_count),
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
            (
                (
                    f"/corpus/{index:05d}.bin",
                    _blob(11),
                    _blob(index + 1),
                    1,
                    OLD_MTIME_NS,
                )
                for index in range(file_count)
            ),
        )

    with sqlite3.connect(paths.inventory) as connection:
        plan = tuple(
            str(row[3])
            for row in connection.execute(
                """EXPLAIN QUERY PLAN
                SELECT f.volume_id,f.file_id,f.birthtime_ns
                FROM inventory_checkpoints c
                JOIN scans s ON s.scan_id=c.scan_id AND s.root=c.root
                JOIN files f ON f.scan_id=c.scan_id
                WHERE c.valid=1 AND s.status='complete'
                GROUP BY f.volume_id,f.file_id,f.birthtime_ns
                ORDER BY f.volume_id,f.file_id,f.birthtime_ns
                LIMIT 1001"""
            )
        )
    assert any("files_identity_birth_scan_idx" in detail for detail in plan)
    assert all("TEMP B-TREE FOR ORDER BY" not in detail for detail in plan)

    wide_filter_page = value_review_repository.load_value_review_observation_page(
        paths,
        ValueReviewQuery(
            reference_time_ns=REFERENCE_NS,
            extensions=(".bin", *(f".unused-{index}" for index in range(63))),
        ),
        page_size=1_000,
    )
    assert wide_filter_page.scanned_count == 1_000
    assert wide_filter_page.cursor_after is not None

    cursor = None
    seen_resources: set[str] = set()
    page_sizes: list[int] = []
    for _ in range(27):
        page = value_review_repository.load_value_review_observation_page(
            paths,
            ValueReviewQuery(reference_time_ns=REFERENCE_NS),
            page_size=1_000,
            after=cursor,
        )
        assert page.cursor_before == cursor
        page_resources = {item.resource_id for item in page.observations}
        assert seen_resources.isdisjoint(page_resources)
        seen_resources.update(page_resources)
        page_sizes.append(page.scanned_count)
        cursor = page.cursor_after
        if cursor is None:
            break
    else:  # pragma: no cover - protects the bounded test itself
        pytest.fail("Value keyset traversal did not converge within 27 pages")
    assert len(seen_resources) == file_count
    assert page_sizes == [1_000] * 25 + [1]

    first = refresh_value_review_tasks(
        root / "framework.sqlite3",
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS,
    )
    assert first.status == "partial"
    assert first.publication is not None
    assert first.publication.progress.scanned_count == 100
    assert first.publication.progress.selected_count == 0
    assert first.publication.progress.complete is False

    second = refresh_value_review_tasks(
        root / "framework.sqlite3",
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS + DAY_NS + 1,
    )
    assert second.publication is not None
    assert second.publication.progress.scanned_count == 200
    assert second.fence is not None
    assert second.fence.source_snapshot_fingerprint == first.fence.source_snapshot_fingerprint
    assert second.fence.source_snapshot.to_dict()["reference_day_ns"] == REFERENCE_NS


def test_complete_value_queue_becomes_policy_stale_on_the_next_day(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    framework = root / "framework.sqlite3"
    refreshed = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS,
    )
    assert refreshed.status == "complete"

    stale = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=10,
        reference_time_ns=REFERENCE_NS + DAY_NS,
    )

    assert stale.status is ValueReviewTaskQueueStatus.STALE
    assert stale.reason == "review_task_policy_time_stale"
    assert stale.progress is not None and stale.progress.complete
    assert stale.records

    reevaluated = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS + DAY_NS,
    )
    assert reevaluated.status == "complete"
    assert reevaluated.fence is not None
    assert reevaluated.fence.source_snapshot_fingerprint != (
        refreshed.fence.source_snapshot_fingerprint
    )


def test_last_complete_value_queue_remains_visible_while_new_epoch_is_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    monkeypatch.setattr(
        "_04_Nucleo_Operativo.value_review_tasks.VALUE_REVIEW_TASK_PAGE_SIZE",
        4,
    )
    framework = root / "framework.sqlite3"
    first_day = None
    for page_index in range(3):
        result = refresh_value_review_tasks(
            framework,
            paths,
            scope="personal",
            clock_ns=lambda page_index=page_index: REFERENCE_NS + page_index,
        )
        first_day = result
        if result.publication is not None and result.publication.progress.complete:
            break
    assert first_day is not None
    assert first_day.publication is not None and first_day.publication.progress.complete
    old_queue = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=100,
        reference_time_ns=REFERENCE_NS,
    )
    assert old_queue.status is ValueReviewTaskQueueStatus.READY
    old_fingerprint = old_queue.fence.source_snapshot_fingerprint

    started = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS + DAY_NS,
    )
    assert started.publication is not None
    assert not started.publication.progress.complete

    visible = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=100,
        reference_time_ns=REFERENCE_NS + DAY_NS,
    )
    assert visible.status is ValueReviewTaskQueueStatus.STALE
    assert visible.reason == "review_task_policy_time_stale"
    assert visible.progress is not None and visible.progress.complete
    assert visible.fence.source_snapshot_fingerprint == old_fingerprint
    assert {record.task.task_id for record in visible.records} == {
        record.task.task_id for record in old_queue.records
    }


def test_human_resolution_after_complete_head_remains_terminal_during_partial_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    paths = _create_state(root)
    monkeypatch.setattr(
        "_04_Nucleo_Operativo.value_review_tasks.VALUE_REVIEW_TASK_PAGE_SIZE",
        4,
    )
    framework = root / "framework.sqlite3"
    for page_index in range(3):
        result = refresh_value_review_tasks(
            framework,
            paths,
            scope="personal",
            clock_ns=lambda page_index=page_index: REFERENCE_NS + page_index,
        )
        assert result.publication is not None
        if result.publication.progress.complete:
            break
    else:  # pragma: no cover - protects bounded test convergence
        pytest.fail("Value Review fixture did not complete within three pages")
    current = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=100,
        reference_time_ns=REFERENCE_NS,
    )
    resolved = current.records[0]
    append_review_task_event(
        framework,
        ReviewTaskTransition(
            event_id="value-human-resolution-after-head",
            event_key="value-human-resolution-after-head-key",
            task_id=resolved.task.task_id,
            expected_event_id=resolved.current_event.event_id,
            expected_state=ReviewTaskState.OPEN,
            to_state=ReviewTaskState.RESOLVED,
            actor_kind=ReviewTaskActorKind.HUMAN,
            actor_id="victor",
            provenance=CanonicalJsonObject.from_mapping({"surface": "test"}),
            decision=CanonicalJsonObject.from_mapping(
                {
                    "decision": "resolved",
                    "schema": "neocortex.review-task-decision/v1",
                    "scope": "permanent",
                    "selector_signature": resolved.selector_signature,
                    "source_input_fingerprint": resolved.source.fingerprint,
                    "source_snapshot_fingerprint": resolved.source_snapshot_fingerprint,
                }
            ),
            note="confirmed after publication",
            observed_ns=REFERENCE_NS + 10,
            recorded_ns=REFERENCE_NS + 10,
        ),
    )

    partial = refresh_value_review_tasks(
        framework,
        paths,
        scope="personal",
        clock_ns=lambda: REFERENCE_NS + DAY_NS,
    )
    assert partial.publication is not None and not partial.publication.progress.complete
    stale = read_value_review_task_queue(
        framework,
        paths,
        scope="personal",
        limit=100,
        reference_time_ns=REFERENCE_NS + DAY_NS,
    )
    assert stale.status is ValueReviewTaskQueueStatus.STALE
    assert resolved.task.task_id not in {record.task.task_id for record in stale.records}


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
