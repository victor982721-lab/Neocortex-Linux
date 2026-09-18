"""Durability and migration contracts for owner-local review tasks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import neocortex.persistence.framework_schema as framework_schema
from neocortex.persistence.framework_schema import initialize_framework_schema


_NEW_TABLES = {
    "review_tasks": (
        "task_id",
        "logical_key",
        "task_version",
        "task_type",
        "scope",
        "source_kind",
        "source_input_id",
        "source_ref_json",
        "source_snapshot_fingerprint",
        "snapshot_json",
        "evidence_json",
        "reason_code",
        "uncertainty_json",
        "impact",
        "uncertainty",
        "irreversibility",
        "priority",
        "priority_algorithm",
        "suggestions_json",
        "batch_id",
        "supersedes_task_id",
        "supersession_event_id",
        "created_ns",
    ),
    "review_task_events": (
        "event_id",
        "event_key",
        "task_id",
        "sequence",
        "previous_event_id",
        "from_state",
        "to_state",
        "actor_kind",
        "actor_id",
        "provenance_json",
        "decision_json",
        "note",
        "observed_ns",
        "recorded_ns",
        "event_schema_version",
    ),
    "review_task_batch_memberships": (
        "membership_id",
        "batch_id",
        "task_id",
        "source_input_id",
        "recorded_ns",
        "membership_schema_version",
    ),
    "review_task_batches": (
        "batch_id",
        "batch_key",
        "scope",
        "task_type",
        "selector_signature",
        "source_snapshot_fingerprint",
        "source_snapshot_json",
        "cursor_before_json",
        "cursor_after_json",
        "previous_batch_id",
        "scan_revision",
        "page_size",
        "scanned_count",
        "selected_count",
        "cumulative_scanned_count",
        "cumulative_selected_count",
        "coverage",
        "evidence_complete",
        "evidence_reason",
        "cumulative_evidence_complete",
        "cumulative_evidence_reason",
        "producer_signature",
        "receipt_json",
        "confirmed_ns",
        "receipt_schema_version",
    ),
    "review_task_scan_progress": (
        "progress_id",
        "scope",
        "task_type",
        "selector_signature",
        "source_snapshot_fingerprint",
        "source_snapshot_json",
        "cursor_json",
        "last_batch_id",
        "scanned_count",
        "selected_count",
        "complete",
        "evidence_complete",
        "evidence_reason",
        "revision",
        "created_ns",
        "updated_ns",
    ),
    "review_task_source_publications": (
        "publication_id",
        "publication_key",
        "scope",
        "task_type",
        "selector_signature",
        "source_snapshot_fingerprint",
        "source_snapshot_json",
        "batch_id",
        "previous_publication_id",
        "revision",
        "confirmed_ns",
        "receipt_json",
        "receipt_schema_version",
    ),
}

_NEW_INDEXES = {
    "review_tasks_queue_idx",
    "review_tasks_source_idx",
    "review_task_events_current_idx",
    "review_task_batches_scan_idx",
    "review_task_source_publications_head_idx",
}

_NEW_TRIGGERS = {
    "review_tasks_validate_insert",
    "review_tasks_no_update",
    "review_tasks_no_delete",
    "review_task_events_validate_insert",
    "review_task_events_no_update",
    "review_task_events_no_delete",
    "review_task_batches_validate_insert",
    "review_task_batches_no_update",
    "review_task_batches_no_delete",
    "review_task_batch_memberships_validate_insert",
    "review_task_batch_memberships_no_update",
    "review_task_batch_memberships_no_delete",
    "review_task_scan_progress_validate_insert",
    "review_task_scan_progress_validate_update",
    "review_task_scan_progress_no_delete",
    "review_task_source_publications_validate_insert",
    "review_task_source_publications_no_update",
    "review_task_source_publications_no_delete",
}

_SOURCE_SNAPSHOT_JSON = '{"generation":1,"owner":"fixture"}'
_SOURCE_FINGERPRINT = (
    "review-task-source-snapshot-v1:sha256:"
    + hashlib.sha256(_SOURCE_SNAPSHOT_JSON.encode("utf-8")).hexdigest()
)
_CURSOR_JSON = '{"offset":10}'
_SOURCE_REF_JSON = (
    '{"fingerprint":"fixture-content-sha256",'
    '"fingerprint_algorithm":"sha256","input_id":"input-1",'
    '"kind":"review_task_input","schema_version":1}'
)
_TASK_SNAPSHOT_JSON = '{"candidate":"fixture"}'
_UNCERTAINTY_JSON = '{"basis":"controlled-fixture"}'
_RECEIPT_JSON = (
    '{"inputs":[{"input_id":"input-1"}],'
    '"kind":"review_task_publication","schema_version":1,'
    '"tasks":[{"source_input_id":"input-1","task_id":"task-1"}]}'
)
_PROVENANCE_JSON = '{"kind":"controlled-fixture","schema_version":1}'


class _InjectedBaseException(BaseException):
    """Controlled non-Exception failure used to prove transactional rollback."""


def _application_schema(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, str, str | None], ...]:
    return tuple(
        (str(name), str(kind), str(table), None if sql is None else str(sql))
        for name, kind, table, sql in connection.execute(
            """SELECT name,type,tbl_name,sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"""
        )
    )


def _create_version_20_database(
    database: Path,
    *,
    extra_schema: str = "",
) -> None:
    """Build the exact populated predecessor rather than relabeling current DDL."""

    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        framework_schema._build_v20_exact_schema(connection)
        connection.execute("INSERT INTO metadata VALUES('schema_version','20')")
        connection.execute(
            """INSERT INTO initial_runs(
            run_id,root,started_ns,status,run_kind,corpus_access_mode)
            VALUES(7,'fixture-root',100,'failed','initial','normal')"""
        )
        connection.execute(
            """INSERT INTO file_actions(
            action_id,run_id,action_type,source_path,target_path,detected_mime,
            evidence,apply_requested,status,detail,started_ns,completed_ns,
            idempotency_key,expected_identity_json,effect_receipt_json,applying_ns,
            corpus_access_mode)
            VALUES(41,7,'correct_extension','fixture-source','fixture-target',
            NULL,'fixture evidence',1,'recovery_required','uncertain',101,NULL,
            'fixture-action-key','{"schema_version":1}',NULL,102,'normal')"""
        )
        connection.execute(
            """INSERT INTO file_action_events(
            event_id,action_id,occurred_ns,from_status,to_status,stage,detail)
            VALUES(51,41,103,'applying','recovery_required',
            'recovery_required','uncertain')"""
        )
        connection.execute(
            """INSERT INTO file_action_reconciliation_events(
            reconciliation_event_id,action_id,sequence,previous_event_id,
            reconciliation_key,observed_ns,recorded_ns,action_status,
            reconciler_signature,event_schema_version,actor,provenance_json,
            classification,recommendation,detail,evidence_json)
            VALUES(61,41,1,NULL,'fixture-reconciliation-key',104,105,
            'recovery_required','fixture-reconciler-v1',1,'fixture-actor','{}',
            'ambiguous','preserve_evidence_and_review_manually',
            'fixture detail','{}')"""
        )
        connection.execute(
            """INSERT INTO review_candidates(
            route_name,volume_id,file_id,reason_code,path,size,mtime_ns,birthtime_ns,
            source_status,recommendation,retryable,confidence,evidence_json,
            detector_version,status,first_detected_ns,last_detected_ns,
            last_seen_run_id)
            VALUES('text','volume','file','uncertain','fixture.txt',1,2,-1,
            'uncertain','manual_review',0,0.4,'{}','fixture-v1','open',3,4,7)"""
        )
        connection.execute(
            """INSERT INTO review_decisions(
            decision_id,idempotency_key,route_name,volume_id,file_id,reason_code,
            candidate_generation,path,size,mtime_ns,birthtime_ns,source_status,
            recommendation,retryable,confidence,evidence_json,detector_version,
            status,actor,provenance_json,note,decided_ns,recorded_ns)
            VALUES(71,'fixture-decision-key','text','volume','file','uncertain',1,
            'fixture.txt',1,2,-1,'uncertain','manual_review',0,0.4,'{}',
            'fixture-v1','deferred','fixture-actor','{}','fixture',5,6)"""
        )
        connection.execute(
            """INSERT INTO review_evidence_examples(
            decision_id,idempotency_key,route_name,volume_id,file_id,path,size,
            mtime_ns,birthtime_ns,reason_code,candidate_generation,source_status,
            target_recommendation,retryable,confidence,evidence_json,
            detector_version,decision_status,actor,provenance_json,note,
            decided_ns,recorded_ns,outcome,candidate_evidence_complete,
            evidence_schema_version,materialized_ns)
            VALUES(71,'fixture-evidence-key','text','volume','file','fixture.txt',
            1,2,-1,'uncertain',1,'uncertain','manual_review',0,0.4,'{}',
            'fixture-v1','deferred','fixture-actor','{}','fixture',5,6,
            'abstained',1,1,7)"""
        )
        connection.execute(
            """INSERT INTO review_evidence_progress(
            pipeline_key,last_scanned_decision_id,updated_ns)
            VALUES('fixture-pipeline',71,8)"""
        )
        if extra_schema:
            connection.executescript(extra_schema)
        connection.commit()


def _insert_batch(
    connection: sqlite3.Connection,
    *,
    batch_id: str = "batch-1",
    batch_key: str = "batch-key-1",
    scope: str = "personal",
    task_type: str = "value-review",
    source_snapshot_fingerprint: str = _SOURCE_FINGERPRINT,
    source_snapshot_json: str = _SOURCE_SNAPSHOT_JSON,
    cursor_before_json: str | None = None,
    cursor_after_json: str | None = _CURSOR_JSON,
    previous_batch_id: str | None = None,
    scan_revision: int = 1,
    coverage: str = "partial",
    page_size: int = 10,
    selected_count: int = 1,
    cumulative_scanned_count: int | None = None,
    cumulative_selected_count: int | None = None,
    receipt_json: str | None = None,
    confirmed_ns: int = 100,
    evidence_complete: bool = True,
    evidence_reason: str | None = None,
    cumulative_evidence_complete: bool | None = None,
    cumulative_evidence_reason: str | None = None,
    receipt_task_id: str = "task-1",
    receipt_input_id: str = "input-1",
) -> None:
    previous = connection.execute(
        """SELECT batch_id,scan_revision,cursor_after_json,
        cumulative_scanned_count,cumulative_selected_count,
        cumulative_evidence_complete,cumulative_evidence_reason
        FROM review_task_batches
        WHERE scope=? AND task_type=? AND selector_signature='selector-v1'
          AND source_snapshot_fingerprint=?
        ORDER BY scan_revision DESC LIMIT 1""",
        (scope, task_type, source_snapshot_fingerprint),
    ).fetchone()
    if previous is not None and previous_batch_id is None and scan_revision == 1:
        previous_batch_id = str(previous[0])
        scan_revision = int(previous[1]) + 1
        if cursor_before_json is None:
            cursor_before_json = None if previous[2] is None else str(previous[2])
        if cumulative_scanned_count is None:
            cumulative_scanned_count = int(previous[3]) + page_size
        if cumulative_selected_count is None:
            cumulative_selected_count = int(previous[4]) + selected_count
        if not bool(int(previous[5])):
            cumulative_evidence_complete = False
            cumulative_evidence_reason = str(previous[6])
    if cumulative_scanned_count is None:
        cumulative_scanned_count = page_size
    if cumulative_selected_count is None:
        cumulative_selected_count = selected_count
    if cumulative_evidence_complete is None:
        cumulative_evidence_complete = evidence_complete
    if cumulative_evidence_reason is None and not cumulative_evidence_complete:
        cumulative_evidence_reason = evidence_reason
    if receipt_json is None:
        receipt_json = json.dumps(
            {
                "inputs": [{"input_id": receipt_input_id}],
                "kind": "review_task_publication",
                "schema_version": 1,
                "tasks": (
                    []
                    if selected_count == 0
                    else [
                        {
                            "source_input_id": receipt_input_id,
                            "task_id": receipt_task_id,
                        }
                    ]
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    connection.execute(
        """INSERT INTO review_task_batches(
        batch_id,batch_key,scope,task_type,selector_signature,
        source_snapshot_fingerprint,source_snapshot_json,cursor_before_json,
        cursor_after_json,previous_batch_id,scan_revision,page_size,scanned_count,
        selected_count,cumulative_scanned_count,cumulative_selected_count,
        coverage,evidence_complete,evidence_reason,cumulative_evidence_complete,
        cumulative_evidence_reason,producer_signature,receipt_json,confirmed_ns,
        receipt_schema_version)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
        'review-task-producer-v1',?,?,1)""",
        (
            batch_id,
            batch_key,
            scope,
            task_type,
            "selector-v1",
            source_snapshot_fingerprint,
            source_snapshot_json,
            cursor_before_json,
            cursor_after_json,
            previous_batch_id,
            scan_revision,
            page_size,
            page_size,
            selected_count,
            cumulative_scanned_count,
            cumulative_selected_count,
            coverage,
            int(evidence_complete),
            evidence_reason,
            int(cumulative_evidence_complete),
            cumulative_evidence_reason,
            receipt_json,
            confirmed_ns,
        ),
    )


def _insert_task(
    connection: sqlite3.Connection,
    *,
    task_id: str = "task-1",
    logical_key: str = "logical-1",
    task_version: int = 1,
    task_type: str = "value-review",
    scope: str = "personal",
    source_kind: str = "review-decision",
    source_input_id: str = "input-1",
    source_ref_json: str = _SOURCE_REF_JSON,
    source_snapshot_fingerprint: str = _SOURCE_FINGERPRINT,
    snapshot_json: str = _TASK_SNAPSHOT_JSON,
    evidence_json: str = "[]",
    uncertainty_json: str = _UNCERTAINTY_JSON,
    suggestions_json: str = "[]",
    batch_id: str = "batch-1",
    supersedes_task_id: str | None = None,
    supersession_event_id: str | None = None,
    created_ns: int = 101,
) -> None:
    connection.execute(
        """INSERT INTO review_tasks(
        task_id,logical_key,task_version,task_type,scope,source_kind,
        source_input_id,source_ref_json,source_snapshot_fingerprint,snapshot_json,
        evidence_json,reason_code,uncertainty_json,impact,uncertainty,
        irreversibility,priority,priority_algorithm,suggestions_json,batch_id,
        supersedes_task_id,supersession_event_id,created_ns)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,'uncertain',?,0.5,0.4,0.25,0.05,
        'impact-x-uncertainty-x-irreversibility-v1',?,?,?,?,?)""",
        (
            task_id,
            logical_key,
            task_version,
            task_type,
            scope,
            source_kind,
            source_input_id,
            source_ref_json,
            source_snapshot_fingerprint,
            snapshot_json,
            evidence_json,
            uncertainty_json,
            suggestions_json,
            batch_id,
            supersedes_task_id,
            supersession_event_id,
            created_ns,
        ),
    )


def _insert_event(
    connection: sqlite3.Connection,
    *,
    event_id: str = "event-1",
    event_key: str = "event-key-1",
    task_id: str = "task-1",
    sequence: int = 1,
    previous_event_id: str | None = None,
    from_state: str | None = None,
    to_state: str = "open",
    actor_kind: str = "system",
    actor_id: str = "review-task-producer-v1",
    provenance_json: str = _PROVENANCE_JSON,
    decision_json: str | None = None,
    note: str | None = None,
    observed_ns: int = 101,
    recorded_ns: int = 102,
) -> None:
    connection.execute(
        """INSERT INTO review_task_events(
        event_id,event_key,task_id,sequence,previous_event_id,from_state,to_state,
        actor_kind,actor_id,provenance_json,decision_json,note,observed_ns,
        recorded_ns,event_schema_version)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
        (
            event_id,
            event_key,
            task_id,
            sequence,
            previous_event_id,
            from_state,
            to_state,
            actor_kind,
            actor_id,
            provenance_json,
            decision_json,
            note,
            observed_ns,
            recorded_ns,
        ),
    )


def _insert_membership(
    connection: sqlite3.Connection,
    *,
    membership_id: str = "membership-1",
    batch_id: str = "batch-1",
    task_id: str = "task-1",
    source_input_id: str = "input-1",
    recorded_ns: int = 100,
) -> None:
    connection.execute(
        """INSERT INTO review_task_batch_memberships(
        membership_id,batch_id,task_id,source_input_id,recorded_ns,
        membership_schema_version) VALUES(?,?,?,?,?,1)""",
        (membership_id, batch_id, task_id, source_input_id, recorded_ns),
    )


def _insert_progress(connection: sqlite3.Connection) -> None:
    connection.execute(
        """INSERT INTO review_task_scan_progress(
        progress_id,scope,task_type,selector_signature,source_snapshot_fingerprint,
        source_snapshot_json,cursor_json,last_batch_id,scanned_count,
        selected_count,complete,evidence_complete,evidence_reason,revision,
        created_ns,updated_ns)
        VALUES('progress-1','personal','value-review','selector-v1',?,?,
        ?,'batch-1',10,1,0,1,NULL,1,100,102)""",
        (_SOURCE_FINGERPRINT, _SOURCE_SNAPSHOT_JSON, _CURSOR_JSON),
    )


def _insert_complete_progress(
    connection: sqlite3.Connection,
    *,
    progress_id: str,
    batch_id: str,
    source_snapshot_fingerprint: str,
    source_snapshot_json: str,
    scanned_count: int,
    selected_count: int,
    confirmed_ns: int,
    evidence_complete: bool = True,
    evidence_reason: str | None = None,
) -> None:
    connection.execute(
        """INSERT INTO review_task_scan_progress(
        progress_id,scope,task_type,selector_signature,source_snapshot_fingerprint,
        source_snapshot_json,cursor_json,last_batch_id,scanned_count,
        selected_count,complete,evidence_complete,evidence_reason,revision,
        created_ns,updated_ns)
        VALUES(?,'personal','value-review','selector-v1',?,?,NULL,?,?,?,1,?,?,1,
        ?,?)""",
        (
            progress_id,
            source_snapshot_fingerprint,
            source_snapshot_json,
            batch_id,
            scanned_count,
            selected_count,
            int(evidence_complete),
            evidence_reason,
            confirmed_ns,
            confirmed_ns,
        ),
    )


def _insert_source_publication(
    connection: sqlite3.Connection,
    *,
    publication_id: str,
    batch_id: str,
    source_snapshot_fingerprint: str,
    source_snapshot_json: str,
    revision: int,
    previous_publication_id: str | None,
    confirmed_ns: int,
) -> None:
    receipt = json.dumps(
        {
            "batch_id": batch_id,
            "confirmed_ns": confirmed_ns,
            "kind": "review_task_source_publication",
            "previous_publication_id": previous_publication_id,
            "publication_id": publication_id,
            "publication_key": f"key-{publication_id}",
            "revision": revision,
            "schema_version": 1,
            "scope": "personal",
            "selector_signature": "selector-v1",
            "source_snapshot": json.loads(source_snapshot_json),
            "source_snapshot_fingerprint": source_snapshot_fingerprint,
            "task_type": "value-review",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        """INSERT INTO review_task_source_publications(
        publication_id,publication_key,scope,task_type,selector_signature,
        source_snapshot_fingerprint,source_snapshot_json,batch_id,
        previous_publication_id,revision,confirmed_ns,receipt_json,
        receipt_schema_version)
        VALUES(?,?,'personal','value-review','selector-v1',?,?,?,?,?,?,?,1)""",
        (
            publication_id,
            f"key-{publication_id}",
            source_snapshot_fingerprint,
            source_snapshot_json,
            batch_id,
            previous_publication_id,
            revision,
            confirmed_ns,
            receipt,
        ),
    )


def _seed_review_page(connection: sqlite3.Connection) -> None:
    _insert_batch(connection)
    _insert_task(connection)
    _insert_event(connection)
    _insert_membership(connection)
    _insert_progress(connection)


def test_fresh_current_schema_has_exact_empty_review_task_schema(tmp_path: Path) -> None:
    database = tmp_path / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_framework_schema(connection, lambda: None)

        assert framework_schema.SCHEMA_VERSION == 24
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == (str(framework_schema.SCHEMA_VERSION),)
        for table, expected_columns in _NEW_TABLES.items():
            columns = tuple(
                str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
            )
            assert columns == expected_columns
            assert connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() == (0,)
        objects = {
            (str(name), str(kind))
            for name, kind in connection.execute(
                "SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        assert {(name, "index") for name in _NEW_INDEXES} <= objects
        assert {(name, "trigger") for name in _NEW_TRIGGERS} <= objects
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_populated_v20_migrates_to_current_and_preserves_review_and_actions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    _create_version_20_database(database)
    preserved_tables = (
        "initial_runs",
        "file_actions",
        "file_action_events",
        "file_action_reconciliation_events",
        "review_candidates",
        "review_decisions",
        "review_evidence_examples",
        "review_evidence_progress",
    )
    with closing(sqlite3.connect(database)) as connection:
        before = {
            table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
            for table in preserved_tables
        }
        initialize_framework_schema(connection, lambda: None)
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == (str(framework_schema.SCHEMA_VERSION),)
        assert {
            table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
            for table in preserved_tables
        } == before
        assert all(
            connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() == (0,)
            for table in _NEW_TABLES
        )
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_current_schema_reopen_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        initialize_framework_schema(connection, lambda: None)
        before = _application_schema(connection)

    with closing(sqlite3.connect(database)) as connection:
        initialize_framework_schema(connection, lambda: None)
        assert _application_schema(connection) == before
        assert all(
            connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() == (0,)
            for table in _NEW_TABLES
        )


def test_current_schema_publication_preserves_concurrent_v20_reader_snapshot(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    _create_version_20_database(database)
    with closing(sqlite3.connect(database)) as setup:
        assert setup.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)

    reader = sqlite3.connect(database)
    writer = sqlite3.connect(database)
    try:
        reader.execute("BEGIN")
        assert reader.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("20",)
        assert reader.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (1,)

        def observe_unpublished_schema() -> None:
            assert reader.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone() == ("20",)
            assert (
                reader.execute(
                    "SELECT name FROM sqlite_master WHERE name='review_tasks'"
                ).fetchone()
                is None
            )

        initialize_framework_schema(writer, observe_unpublished_schema)
        assert reader.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("20",)
        assert (
            reader.execute("SELECT name FROM sqlite_master WHERE name='review_tasks'").fetchone()
            is None
        )

        reader.rollback()
        assert reader.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == (str(framework_schema.SCHEMA_VERSION),)
        assert reader.execute(
            "SELECT name FROM sqlite_master WHERE name='review_tasks'"
        ).fetchone() == ("review_tasks",)
        assert reader.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (1,)
        assert reader.execute("SELECT COUNT(*) FROM review_tasks").fetchone() == (0,)
    finally:
        if reader.in_transaction:
            reader.rollback()
        reader.close()
        writer.close()


@pytest.mark.parametrize("failure_type", (KeyboardInterrupt, _InjectedBaseException))
def test_v20_to_current_rolls_back_ddl_and_version_on_base_exception(
    tmp_path: Path,
    failure_type: type[BaseException],
) -> None:
    database = tmp_path / "framework.sqlite3"
    _create_version_20_database(database)
    connection = sqlite3.connect(database)
    try:
        with pytest.raises(failure_type):
            initialize_framework_schema(
                connection,
                lambda: (_ for _ in ()).throw(failure_type()),
            )
        assert not connection.in_transaction
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("20",)
        assert connection.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (1,)
        assert all(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            is None
            for table in _NEW_TABLES
        )
    finally:
        connection.close()


def test_v20_with_unknown_ddl_fails_closed_and_rolls_back_current_objects(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    _create_version_20_database(
        database,
        extra_schema="CREATE TABLE owner_extension(value TEXT);",
    )
    with closing(sqlite3.connect(database)) as connection:
        before = _application_schema(connection)
        with pytest.raises(RuntimeError, match="schema contract validation failed"):
            initialize_framework_schema(connection, lambda: None)
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("20",)
        assert _application_schema(connection) == before


@pytest.mark.parametrize(
    "damage_sql",
    (
        "DROP INDEX review_decisions_status_idx",
        "DROP TRIGGER file_action_events_no_delete",
    ),
)
def test_v20_missing_contract_object_is_not_silently_repaired(
    tmp_path: Path,
    damage_sql: str,
) -> None:
    database = tmp_path / "framework.sqlite3"
    _create_version_20_database(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(damage_sql)
        connection.commit()
        damaged = _application_schema(connection)
        with pytest.raises(RuntimeError, match="v20 schema contract validation failed"):
            initialize_framework_schema(connection, lambda: None)
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("20",)
        assert _application_schema(connection) == damaged


def test_v20_foreign_key_violation_is_not_migrated(tmp_path: Path) -> None:
    database = tmp_path / "framework.sqlite3"
    _create_version_20_database(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """INSERT INTO file_action_events(
            event_id,action_id,occurred_ns,from_status,to_status,stage,detail)
            VALUES(999,999999,999,'applying','recovery_required','fixture','orphan')"""
        )
        connection.commit()
        damaged = _application_schema(connection)
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is not None
        with pytest.raises(RuntimeError, match="foreign-key integrity violation"):
            initialize_framework_schema(connection, lambda: None)
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("20",)
        assert _application_schema(connection) == damaged
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is not None


def test_future_schema_fails_closed_before_configuration_or_writes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    future_version = framework_schema.SCHEMA_VERSION + 1
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(
            """CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)
            WITHOUT ROWID;
            CREATE TABLE sentinel(value TEXT);
            INSERT INTO sentinel VALUES('preserve');"""
        )
        connection.execute(
            "INSERT INTO metadata VALUES('schema_version',?)", (str(future_version),)
        )
        connection.commit()
        before = _application_schema(connection)
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        with pytest.raises(RuntimeError, match=f"schema {future_version} is unsupported"):
            initialize_framework_schema(connection, lambda: None)
        assert _application_schema(connection) == before
        assert connection.execute("PRAGMA journal_mode").fetchone() == journal_mode
        assert connection.execute("SELECT value FROM sentinel").fetchone() == ("preserve",)


@pytest.mark.parametrize(
    "mutation",
    (
        "DROP TRIGGER review_tasks_no_update",
        "DROP INDEX review_tasks_queue_idx; "
        "CREATE INDEX review_tasks_queue_idx ON review_tasks(task_id)",
        "ALTER TABLE review_tasks ADD COLUMN unexpected TEXT",
    ),
)
def test_current_exact_contract_rejects_missing_or_unknown_ddl_without_repair(
    tmp_path: Path,
    mutation: str,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        initialize_framework_schema(connection, lambda: None)
        connection.executescript(mutation)
        before = _application_schema(connection)
        with pytest.raises(RuntimeError, match="schema contract validation failed"):
            initialize_framework_schema(connection, lambda: None)
        assert _application_schema(connection) == before


def test_review_task_rows_and_receipts_are_immutable_and_progress_is_cas(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_framework_schema(connection, lambda: None)
        _seed_review_page(connection)
        connection.commit()

        for statement in (
            "UPDATE review_tasks SET reason_code='changed' WHERE task_id='task-1'",
            "DELETE FROM review_tasks WHERE task_id='task-1'",
            "UPDATE review_task_events SET note='changed' WHERE event_id='event-1'",
            "DELETE FROM review_task_events WHERE event_id='event-1'",
            "UPDATE review_task_batches SET receipt_json='[]' WHERE batch_id='batch-1'",
            "DELETE FROM review_task_batches WHERE batch_id='batch-1'",
            "DELETE FROM review_task_scan_progress WHERE progress_id='progress-1'",
            """UPDATE review_task_scan_progress
            SET revision=3,updated_ns=103 WHERE progress_id='progress-1'""",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
            connection.rollback()

        _insert_batch(
            connection,
            batch_id="batch-2",
            batch_key="batch-key-2",
            cursor_before_json=_CURSOR_JSON,
            cursor_after_json=None,
            coverage="complete",
            page_size=4,
            selected_count=0,
            confirmed_ns=110,
        )
        connection.execute(
            """UPDATE review_task_scan_progress
            SET cursor_json=NULL,last_batch_id='batch-2',scanned_count=14,
                selected_count=1,complete=1,revision=2,updated_ns=111
            WHERE progress_id='progress-1' AND revision=1"""
        )
        connection.commit()
        assert connection.execute(
            """SELECT cursor_json,last_batch_id,scanned_count,selected_count,
            complete,revision FROM review_task_scan_progress"""
        ).fetchone() == (None, "batch-2", 14, 1, 1, 2)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """UPDATE review_task_scan_progress
                SET revision=3,updated_ns=112 WHERE progress_id='progress-1'"""
            )


def test_source_publication_head_is_complete_evidence_append_only_cas(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    second_snapshot = '{"generation":2,"owner":"fixture"}'
    second_fingerprint = (
        "review-task-source-snapshot-v1:sha256:"
        + hashlib.sha256(second_snapshot.encode("utf-8")).hexdigest()
    )
    third_snapshot = '{"generation":3,"owner":"fixture"}'
    third_fingerprint = (
        "review-task-source-snapshot-v1:sha256:"
        + hashlib.sha256(third_snapshot.encode("utf-8")).hexdigest()
    )
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_framework_schema(connection, lambda: None)
        _insert_batch(
            connection,
            coverage="complete",
            cursor_after_json=None,
            page_size=1,
            confirmed_ns=200,
        )
        _insert_task(connection, created_ns=201)
        _insert_event(connection, observed_ns=201, recorded_ns=202)
        _insert_membership(connection, recorded_ns=200)
        _insert_complete_progress(
            connection,
            progress_id="progress-1",
            batch_id="batch-1",
            source_snapshot_fingerprint=_SOURCE_FINGERPRINT,
            source_snapshot_json=_SOURCE_SNAPSHOT_JSON,
            scanned_count=1,
            selected_count=1,
            confirmed_ns=200,
        )
        _insert_source_publication(
            connection,
            publication_id="publication-1",
            batch_id="batch-1",
            source_snapshot_fingerprint=_SOURCE_FINGERPRINT,
            source_snapshot_json=_SOURCE_SNAPSHOT_JSON,
            revision=1,
            previous_publication_id=None,
            confirmed_ns=200,
        )

        _insert_batch(
            connection,
            batch_id="batch-2",
            batch_key="batch-key-2",
            source_snapshot_fingerprint=second_fingerprint,
            source_snapshot_json=second_snapshot,
            cursor_after_json=None,
            coverage="complete",
            page_size=0,
            selected_count=0,
            confirmed_ns=300,
        )
        _insert_complete_progress(
            connection,
            progress_id="progress-2",
            batch_id="batch-2",
            source_snapshot_fingerprint=second_fingerprint,
            source_snapshot_json=second_snapshot,
            scanned_count=0,
            selected_count=0,
            confirmed_ns=300,
        )
        _insert_source_publication(
            connection,
            publication_id="publication-2",
            batch_id="batch-2",
            source_snapshot_fingerprint=second_fingerprint,
            source_snapshot_json=second_snapshot,
            revision=2,
            previous_publication_id="publication-1",
            confirmed_ns=300,
        )

        _insert_batch(
            connection,
            batch_id="batch-3",
            batch_key="batch-key-3",
            source_snapshot_fingerprint=third_fingerprint,
            source_snapshot_json=third_snapshot,
            cursor_after_json=None,
            coverage="complete",
            page_size=0,
            selected_count=0,
            confirmed_ns=400,
        )
        _insert_complete_progress(
            connection,
            progress_id="progress-3",
            batch_id="batch-3",
            source_snapshot_fingerprint=third_fingerprint,
            source_snapshot_json=third_snapshot,
            scanned_count=0,
            selected_count=0,
            confirmed_ns=400,
        )
        with pytest.raises(sqlite3.IntegrityError, match="source publication"):
            _insert_source_publication(
                connection,
                publication_id="publication-3-wrong-head",
                batch_id="batch-3",
                source_snapshot_fingerprint=third_fingerprint,
                source_snapshot_json=third_snapshot,
                revision=3,
                previous_publication_id="publication-1",
                confirmed_ns=400,
            )
        _insert_source_publication(
            connection,
            publication_id="publication-3",
            batch_id="batch-3",
            source_snapshot_fingerprint=third_fingerprint,
            source_snapshot_json=third_snapshot,
            revision=3,
            previous_publication_id="publication-2",
            confirmed_ns=400,
        )

        assert connection.execute(
            "SELECT publication_id,revision FROM review_task_source_publications ORDER BY revision"
        ).fetchall() == [
            ("publication-1", 1),
            ("publication-2", 2),
            ("publication-3", 3),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE review_task_source_publications SET confirmed_ns=401 "
                "WHERE publication_id='publication-3'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM review_task_source_publications WHERE publication_id='publication-3'"
            )


def test_source_publication_head_rejects_incomplete_accumulated_evidence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_framework_schema(connection, lambda: None)
        _insert_batch(
            connection,
            coverage="complete",
            cursor_after_json=None,
            page_size=0,
            selected_count=0,
            evidence_complete=False,
            evidence_reason="source coverage is partial",
            confirmed_ns=200,
        )
        _insert_complete_progress(
            connection,
            progress_id="progress-1",
            batch_id="batch-1",
            source_snapshot_fingerprint=_SOURCE_FINGERPRINT,
            source_snapshot_json=_SOURCE_SNAPSHOT_JSON,
            scanned_count=0,
            selected_count=0,
            evidence_complete=False,
            evidence_reason="source coverage is partial",
            confirmed_ns=200,
        )
        with pytest.raises(sqlite3.IntegrityError, match="source publication"):
            _insert_source_publication(
                connection,
                publication_id="publication-1",
                batch_id="batch-1",
                source_snapshot_fingerprint=_SOURCE_FINGERPRINT,
                source_snapshot_json=_SOURCE_SNAPSHOT_JSON,
                revision=1,
                previous_publication_id=None,
                confirmed_ns=200,
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM review_task_source_publications"
        ).fetchone() == (0,)


def test_review_task_event_history_enforces_cas_and_terminal_states(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_framework_schema(connection, lambda: None)
        _seed_review_page(connection)
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO review_task_events(
                event_id,event_key,task_id,sequence,previous_event_id,from_state,
                to_state,actor_kind,actor_id,provenance_json,decision_json,note,
                observed_ns,recorded_ns,event_schema_version)
                VALUES('event-invalid','event-key-invalid','task-1',2,'event-1',
                'open','resolved','system','producer',
                '{"kind":"controlled-fixture"}','{"decision":"invalid"}',
                NULL,103,104,1)"""
            )
        connection.rollback()

        connection.execute(
            """INSERT INTO review_task_events(
            event_id,event_key,task_id,sequence,previous_event_id,from_state,
            to_state,actor_kind,actor_id,provenance_json,decision_json,note,
            observed_ns,recorded_ns,event_schema_version)
            VALUES('event-2','event-key-2','task-1',2,'event-1','open','in_review',
            'human','reviewer','{"kind":"controlled-fixture"}',
            NULL,NULL,103,104,1)"""
        )
        connection.execute(
            """INSERT INTO review_task_events(
            event_id,event_key,task_id,sequence,previous_event_id,from_state,
            to_state,actor_kind,actor_id,provenance_json,decision_json,note,
            observed_ns,recorded_ns,event_schema_version)
            VALUES('event-3','event-key-3','task-1',3,'event-2','in_review',
            'resolved','human','reviewer','{"kind":"controlled-fixture"}',
            '{"decision":"accept"}',NULL,
            105,106,1)"""
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO review_task_events(
                event_id,event_key,task_id,sequence,previous_event_id,from_state,
                to_state,actor_kind,actor_id,provenance_json,decision_json,note,
                observed_ns,recorded_ns,event_schema_version)
                VALUES('event-4','event-key-4','task-1',4,'event-3','resolved',
                'in_review','human','reviewer',
                '{"kind":"controlled-fixture"}',NULL,NULL,107,108,1)"""
            )


def _normalized_table_sql(connection: sqlite3.Connection, table: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    assert row is not None and row[0] is not None
    return "".join(str(row[0]).lower().split())


def test_review_task_ddl_bounds_and_types_every_json_and_note(tmp_path: Path) -> None:
    database = tmp_path / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        initialize_framework_schema(connection, lambda: None)
        expected = {
            "review_task_batches": {
                "source_snapshot_json": ("object", 65_536),
                "cursor_before_json": ("object", 65_536),
                "cursor_after_json": ("object", 65_536),
                "receipt_json": ("object", 8 * 1024 * 1024),
            },
            "review_tasks": {
                "source_ref_json": ("object", 65_536),
                "snapshot_json": ("object", 65_536),
                "evidence_json": ("array", 65_536),
                "uncertainty_json": ("object", 65_536),
                "suggestions_json": ("array", 65_536),
            },
            "review_task_events": {
                "provenance_json": ("object", 65_536),
                "decision_json": ("object", 65_536),
            },
            "review_task_scan_progress": {
                "source_snapshot_json": ("object", 65_536),
                "cursor_json": ("object", 65_536),
            },
        }
        for table, fields in expected.items():
            sql = _normalized_table_sql(connection, table)
            for field, (json_kind, maximum_bytes) in fields.items():
                assert f"json_valid({field})" in sql
                assert f"json_type({field})='{json_kind}'" in sql
                assert f"length(cast({field}asblob))between1and{maximum_bytes}" in sql
        event_sql = _normalized_table_sql(connection, "review_task_events")
        assert "length(cast(noteasblob))between1and8192" in event_sql


@pytest.mark.parametrize(
    (
        "source_ref_json",
        "snapshot_json",
        "evidence_json",
        "uncertainty_json",
        "suggestions_json",
    ),
    (
        ("[]", _TASK_SNAPSHOT_JSON, "[]", _UNCERTAINTY_JSON, "[]"),
        (_SOURCE_REF_JSON, "[]", "[]", _UNCERTAINTY_JSON, "[]"),
        (_SOURCE_REF_JSON, _TASK_SNAPSHOT_JSON, "{}", _UNCERTAINTY_JSON, "[]"),
        (_SOURCE_REF_JSON, _TASK_SNAPSHOT_JSON, "[]", "[]", "[]"),
        (_SOURCE_REF_JSON, _TASK_SNAPSHOT_JSON, "[]", _UNCERTAINTY_JSON, "{}"),
        (_SOURCE_REF_JSON, "{", "[]", _UNCERTAINTY_JSON, "[]"),
    ),
)
def test_review_task_table_rejects_invalid_json_or_shape(
    tmp_path: Path,
    source_ref_json: str,
    snapshot_json: str,
    evidence_json: str,
    uncertainty_json: str,
    suggestions_json: str,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_framework_schema(connection, lambda: None)
        _insert_batch(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_task(
                connection,
                source_ref_json=source_ref_json,
                snapshot_json=snapshot_json,
                evidence_json=evidence_json,
                uncertainty_json=uncertainty_json,
                suggestions_json=suggestions_json,
            )


@pytest.mark.parametrize(
    ("source_snapshot_json", "cursor_after_json", "receipt_json"),
    (
        ("[]", _CURSOR_JSON, _RECEIPT_JSON),
        (_SOURCE_SNAPSHOT_JSON, "[]", _RECEIPT_JSON),
        (_SOURCE_SNAPSHOT_JSON, _CURSOR_JSON, "[]"),
        (_SOURCE_SNAPSHOT_JSON, _CURSOR_JSON, "{"),
    ),
)
def test_review_task_batch_rejects_invalid_json_or_shape(
    tmp_path: Path,
    source_snapshot_json: str,
    cursor_after_json: str,
    receipt_json: str,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        initialize_framework_schema(connection, lambda: None)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_batch(
                connection,
                source_snapshot_json=source_snapshot_json,
                cursor_after_json=cursor_after_json,
                receipt_json=receipt_json,
            )


def test_review_task_events_reject_non_object_provenance_and_decision(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_framework_schema(connection, lambda: None)
        _insert_batch(connection)
        _insert_task(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_event(connection, provenance_json="[]")
        _insert_event(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_event(
                connection,
                event_id="event-2",
                event_key="event-key-2",
                sequence=2,
                previous_event_id="event-1",
                from_state="open",
                to_state="resolved",
                actor_kind="human",
                decision_json="[]",
                observed_ns=103,
                recorded_ns=104,
            )


def test_review_task_json_receipt_and_note_limits_are_utf8_bytes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    oversized_json = '{"payload":"' + ("é" * 32_768) + '"}'
    oversized_receipt = '{"payload":"' + ("x" * (8 * 1024 * 1024)) + '"}'
    oversized_note = "é" * 4_097
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_framework_schema(connection, lambda: None)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_batch(connection, receipt_json=oversized_receipt)
        _insert_batch(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_task(connection, snapshot_json=oversized_json)
        _insert_task(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_event(connection, note=oversized_note)


@pytest.mark.parametrize(
    "fingerprint",
    (
        "review-task-source-snapshot-v1:sha256:short",
        "review-task-source-snapshot-v1:sha256:" + ("A" * 64),
        "review-task-source-snapshot-v1:sha256:" + ("g" * 64),
    ),
)
def test_review_task_snapshot_fingerprint_rejects_noncanonical_digest_shape(
    tmp_path: Path,
    fingerprint: str,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        initialize_framework_schema(connection, lambda: None)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_batch(connection, source_snapshot_fingerprint=fingerprint)


def test_sqlite_enforces_fingerprint_shape_while_repository_owns_sha_match(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    format_only_fingerprint = "review-task-source-snapshot-v1:sha256:" + ("0" * 64)
    assert format_only_fingerprint != _SOURCE_FINGERPRINT
    with closing(sqlite3.connect(database)) as connection:
        initialize_framework_schema(connection, lambda: None)
        _insert_batch(
            connection,
            source_snapshot_fingerprint=format_only_fingerprint,
        )
        assert connection.execute(
            "SELECT source_snapshot_fingerprint FROM review_task_batches"
        ).fetchone() == (format_only_fingerprint,)


def _seed_open_predecessor(connection: sqlite3.Connection) -> None:
    _insert_batch(connection)
    _insert_task(connection)
    _insert_event(connection)


def test_review_task_version_requires_predecessor_already_superseded(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_framework_schema(connection, lambda: None)
        _insert_batch(connection)
        _insert_task(connection)
        _insert_event(connection)
        _insert_batch(
            connection,
            batch_id="batch-2",
            batch_key="batch-key-2",
            confirmed_ns=104,
            receipt_task_id="task-2",
            receipt_input_id="input-2",
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_task(
                connection,
                task_id="task-2",
                task_version=2,
                source_input_id="input-2",
                batch_id="batch-2",
                supersedes_task_id="task-1",
                created_ns=104,
            )


@pytest.mark.parametrize(
    ("scope", "task_type", "source_kind", "created_ns"),
    (
        ("other", "value-review", "review-decision", 104),
        ("personal", "other-review", "review-decision", 104),
        ("personal", "value-review", "other-source", 104),
        ("personal", "value-review", "review-decision", 101),
        ("personal", "value-review", "review-decision", 100),
    ),
)
def test_review_task_version_rejects_cross_domain_or_nonincreasing_time(
    tmp_path: Path,
    scope: str,
    task_type: str,
    source_kind: str,
    created_ns: int,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_framework_schema(connection, lambda: None)
        _seed_open_predecessor(connection)
        _insert_batch(
            connection,
            batch_id="batch-2",
            batch_key="batch-key-2",
            scope=scope,
            task_type=task_type,
            confirmed_ns=104,
            receipt_task_id="task-2",
            receipt_input_id="input-2",
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_task(
                connection,
                task_id="task-2",
                task_version=2,
                task_type=task_type,
                scope=scope,
                source_kind=source_kind,
                source_input_id="input-2",
                batch_id="batch-2",
                supersedes_task_id="task-1",
                supersession_event_id="event-superseded",
                created_ns=created_ns,
            )


def test_review_task_version_accepts_same_domain_after_durable_supersession(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_framework_schema(connection, lambda: None)
        _seed_open_predecessor(connection)
        _insert_batch(
            connection,
            batch_id="batch-2",
            batch_key="batch-key-2",
            confirmed_ns=104,
            receipt_task_id="task-2",
            receipt_input_id="input-2",
        )
        _insert_task(
            connection,
            task_id="task-2",
            task_version=2,
            source_input_id="input-2",
            batch_id="batch-2",
            supersedes_task_id="task-1",
            supersession_event_id="event-superseded",
            created_ns=104,
        )
        _insert_event(
            connection,
            event_id="event-task-2-open",
            event_key="event-key-task-2-open",
            task_id="task-2",
            observed_ns=104,
            recorded_ns=104,
        )
        _insert_event(
            connection,
            event_id="event-superseded",
            event_key="event-key-superseded",
            sequence=2,
            previous_event_id="event-1",
            from_state="open",
            to_state="superseded",
            actor_kind="system",
            actor_id="review-task-refresh",
            provenance_json=json.dumps(
                {
                    "reason_code": "replacement_task_published",
                    "replacement_task_id": "task-2",
                    "source_snapshot_fingerprint": _SOURCE_FINGERPRINT,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            observed_ns=104,
            recorded_ns=104,
        )
        connection.commit()
        assert connection.execute(
            "SELECT task_id,task_version FROM review_tasks ORDER BY task_version"
        ).fetchall() == [("task-1", 1), ("task-2", 2)]
