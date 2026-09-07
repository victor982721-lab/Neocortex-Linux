"""Read-only, bounded retention planning contracts."""
# region [00] Contexto del módulo
# Módulo: tests/test_retention_planner.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import inspect
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.deduplication.persistence import initialize_inventory_schema
from neocortex.persistence import framework_schema
from neocortex.workflow.retention import planner as retention_module
from neocortex.api.cli.cli_app import main as cli_main
from neocortex.documents.document_catalog import (
    document_catalog_database,
    initialize_document_catalog,
)
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.workflow.retention.planner import (
    RetentionItem,
    RetentionPlan,
    RetentionPlanningCancelled,
    RetentionPolicy,
    plan_retention,
)
from neocortex.workflow.review.review_task_contracts import (
    CanonicalJsonObject,
    ReviewTaskActorKind,
    ReviewTaskCoverage,
    ReviewTaskDraft,
    ReviewTaskInput,
    ReviewTaskPublication,
    ReviewTaskSourceFence,
    ReviewTaskState,
    ReviewTaskTransition,
)
from neocortex.workflow.review.review_task_repository import (
    append_review_task_event,
    list_current_review_tasks,
    publish_review_task_page,
)
from neocortex.semantic.semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRole,
)
from neocortex.semantic.semantic_state import (
    initialize_semantic_state,
    register_embedding_model,
    semantic_database,
)
# endregion [01]

# region [02] Implementación


NOW_NS = 10_000_000_000


def _semantic_model() -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        "retention-model-v1",
        "retention-space-v1",
        EmbeddingModality.TEXT,
        "fixture/retention",
        "1",
        4,
        "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )


def _populate_semantic(database: Path) -> None:
    model = _semantic_model()
    initialize_semantic_state(database)
    register_embedding_model(database, model, allow_test_provider=True)
    with semantic_database(database) as connection:
        connection.execute(
            """INSERT INTO semantic_items(
            item_id,source_kind,source_identity,identity_version,path,
            content_xxh3_128,content_bytes,content_xxh3_64_guard,
            provenance_json,refresh_token,active,updated_ns,source_revision_json)
            VALUES('fixture-item','pdf','fixture-identity','fixture-v1',
            'C:/fixture.pdf','abc',3,'guard','{}','fixture',1,1,'{}')"""
        )
        rows = (
            (1, "ready", None, 1),
            (2, "ready", 1, 1),
            (3, "ready", 2, 1),
            (4, "ready", 3, 1),
            (5, "ready", 4, 1),
            (6, "building", 5, None),
            (7, "building", 5, None),
            (8, "failed", 5, 1),
        )
        connection.executemany(
            """INSERT INTO embedding_generations(
            generation_id,model_signature,processing_signature,status,
            provenance_json,cursor_json,started_ns,completed_ns,
            base_generation_id,base_clone_complete)
            VALUES(?,'retention-model-v1','fixture',?,'{}','{}',1,?,?,1)""",
            ((generation, status, completed, base) for generation, status, base, completed in rows),
        )
        connection.execute("INSERT INTO published_embedding_heads VALUES('retention-model-v1',5,2)")
        connection.execute(
            """INSERT INTO embedding_jobs(
            generation_id,model_signature,role,entity_kind,entity_id,item_id,
            content_xxh3_128,content_bytes,content_xxh3_64_guard,status,
            attempts,max_attempts,available_ns,lease_owner,lease_until_ns,
            created_ns,updated_ns)
            VALUES(6,'retention-model-v1','passage','text_chunk','fixture-chunk',
            'fixture-item','abc',3,'guard','leased',1,3,1,'worker',?,1,1)""",
            (NOW_NS + 1,),
        )


def _populate_catalog(database: Path) -> None:
    initialize_document_catalog(database)
    with document_catalog_database(database) as connection:
        connection.executemany(
            """INSERT INTO catalog_runs(
            catalog_run_id,framework_run_id,source_kind,mode,status,started_ns,
            completed_ns)
            VALUES(?,NULL,'pdf','classify',?,1,?)""",
            (
                (1, "completed", 1),
                (2, "completed", 1),
                (3, "completed", 1),
                (4, "running", None),
                (5, "failed", 1),
                (6, "failed", 1),
            ),
        )
        connection.executemany(
            """INSERT INTO catalog_generations(
            generation_id,catalog_run_id,source_kind,base_generation_id,status,
            started_ns,completed_ns,published_ns)
            VALUES(?,?,'pdf',?,?,1,?,?)""",
            (
                (1, 1, None, "published", 1, 1),
                (2, 2, 1, "published", 1, 1),
                (3, 3, 2, "published", 1, 1),
                (4, 4, 3, "building", None, None),
                (5, 5, 3, "abandoned", 1, None),
                (6, 6, 3, "failed", 1, None),
            ),
        )
        connection.execute("INSERT INTO catalog_publications VALUES('pdf',3,2)")
        connection.execute(
            """INSERT INTO organization_plans(
            plan_id,catalog_run_id,source_kind,file_key,source_path,
            destination_path,organization_root,volume_id,file_id,size,mtime_ns,
            birthtime_ns,classifier_signature,primary_kind,confidence,status,
            reason,evidence_json,planned_ns)
            VALUES(1,5,'pdf','1:2','C:/source.pdf','C:/target.pdf','C:/organized',
            '1','2',3,4,5,'classifier','technical',0.9,'recovery_required',
            'uncertain','{}',1)"""
        )
        connection.commit()


def _populate_framework(database: Path) -> None:
    with FrameworkState(database):
        pass
    with sqlite3.connect(database) as connection:
        connection.executemany(
            """INSERT INTO initial_runs(
            run_id,root,started_ns,completed_ns,status,run_kind)
            VALUES(?,'C:/fixture',1,?,?,'initial')""",
            (
                (1, 1, "completed"),
                (2, 1, "completed"),
                (3, 1, "failed"),
                (4, 1, "cancelled"),
                (5, 1, "completed"),
                (6, None, "running"),
            ),
        )
        connection.execute(
            """INSERT INTO file_actions(
            action_id,run_id,action_type,source_path,apply_requested,status,
            started_ns,expected_identity_json)
            VALUES(1,3,'move','C:/source',1,'recovery_required',1,'{}')"""
        )
        connection.execute(
            """INSERT INTO file_actions(
            action_id,run_id,action_type,source_path,apply_requested,status,
            started_ns,completed_ns,idempotency_key,expected_identity_json)
            VALUES(2,4,'move','C:/reconciled',1,'completed',1,2,
            'reconciled-action','{}')"""
        )
        connection.execute(
            """INSERT INTO file_action_reconciliation_events(
            reconciliation_event_id,action_id,sequence,reconciliation_key,
            observed_ns,recorded_ns,action_status,reconciler_signature,
            event_schema_version,actor,provenance_json,classification,
            recommendation,detail,evidence_json)
            VALUES(1,2,1,'reconciliation-key',2,3,'recovery_required',
            'file-action-reconciler-v1',1,'test','{}','confirmed',
            'confirm_action_record','fixture','{}')"""
        )
        connection.execute(
            """INSERT INTO review_decisions(
            decision_id,idempotency_key,route_name,volume_id,file_id,reason_code,
            candidate_generation,path,size,mtime_ns,birthtime_ns,status,actor,
            provenance_json,decided_ns,recorded_ns)
            VALUES(1,'decision-key','pdf','1','2','fixture',2,'C:/fixture.pdf',
            3,4,5,'deferred','tester','{}',6,7)"""
        )
        connection.commit()


def _populate_review_task_holds(database: Path, *, multipage: bool = False) -> None:
    review_input = ReviewTaskInput("input-1", "sha256", "1" * 64)
    task = ReviewTaskDraft(
        task_id="task-1",
        logical_key="logical-1",
        task_version=1,
        task_type="value-review",
        scope="personal",
        source_kind="review-decision",
        source_input_id=review_input.input_id,
        snapshot=CanonicalJsonObject.from_mapping({"fixture": "retention-hold"}),
        evidence=(),
        reason_code="uncertain",
        uncertainty_detail=CanonicalJsonObject.from_mapping({"basis": "fixture"}),
        impact=0.5,
        uncertainty=0.4,
        irreversibility=0.25,
        suggestions=("inspect",),
        supersedes_task_id=None,
        created_ns=99,
    )
    fence = ReviewTaskSourceFence.create(
        scope="personal",
        task_type="value-review",
        selector_signature="selector-v1",
        source_snapshot={"fixture": "retention-hold"},
    )
    cursor = CanonicalJsonObject.from_mapping({"offset": 1}) if multipage else None
    publication = ReviewTaskPublication(
        batch_id="batch-1",
        batch_key="batch-key-1",
        fence=fence,
        cursor_before=None,
        cursor_after=cursor,
        inputs=(review_input,),
        tasks=(task,),
        coverage=(ReviewTaskCoverage.PARTIAL if multipage else ReviewTaskCoverage.COMPLETE),
        producer_signature="producer-v1",
        confirmed_ns=100,
    )
    first_result = publish_review_task_page(
        database,
        publication,
        expected_progress_revision=None,
    )
    if multipage:
        final = ReviewTaskPublication(
            batch_id="batch-2",
            batch_key="batch-key-2",
            fence=fence,
            cursor_before=cursor,
            cursor_after=None,
            inputs=(),
            tasks=(),
            coverage=ReviewTaskCoverage.COMPLETE,
            producer_signature="producer-v1",
            confirmed_ns=101,
        )
        publish_review_task_page(
            database,
            final,
            expected_progress_revision=first_result.progress.revision,
        )
    current = list_current_review_tasks(database, limit=1).items[0].current_event
    append_review_task_event(
        database,
        ReviewTaskTransition(
            event_id="event-2",
            event_key="event-key-2",
            task_id=task.task_id,
            expected_event_id=current.event_id,
            expected_state=ReviewTaskState.OPEN,
            to_state=ReviewTaskState.RESOLVED,
            actor_kind=ReviewTaskActorKind.HUMAN,
            actor_id="reviewer",
            provenance=CanonicalJsonObject.from_mapping({"surface": "fixture"}),
            decision=CanonicalJsonObject.from_mapping({"decision": "accept"}),
            note="accepted",
            observed_ns=103,
            recorded_ns=104,
        ),
    )


def _corrupt_published_review_task_receipt(database: Path, *, fact: str) -> None:
    with sqlite3.connect(database) as connection:
        if fact == "membership":
            trigger_name = "review_task_batch_memberships_no_delete"
            trigger_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                    (trigger_name,),
                ).fetchone()[0]
            )
            connection.execute("DROP TRIGGER review_task_batch_memberships_no_delete")
            connection.execute("DELETE FROM review_task_batch_memberships")
        elif fact == "progress":
            trigger_name = "review_task_scan_progress_no_delete"
            trigger_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                    (trigger_name,),
                ).fetchone()[0]
            )
            connection.execute("DROP TRIGGER review_task_scan_progress_no_delete")
            connection.execute("DELETE FROM review_task_scan_progress")
        elif fact == "progress_mismatch":
            trigger_name = "review_task_scan_progress_validate_update"
            trigger_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                    (trigger_name,),
                ).fetchone()[0]
            )
            connection.execute("DROP TRIGGER review_task_scan_progress_validate_update")
            connection.execute("UPDATE review_task_scan_progress SET scanned_count=scanned_count+1")
        elif fact == "membership_binding":
            trigger_name = "review_task_batch_memberships_no_update"
            trigger_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                    (trigger_name,),
                ).fetchone()[0]
            )
            connection.execute("DROP TRIGGER review_task_batch_memberships_no_update")
            connection.execute(
                "UPDATE review_task_batch_memberships SET source_input_id='wrong-input'"
            )
        elif fact == "batch_cursor":
            trigger_name = "review_task_batches_no_update"
            trigger_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                    (trigger_name,),
                ).fetchone()[0]
            )
            connection.execute("DROP TRIGGER review_task_batches_no_update")
            connection.execute(
                "UPDATE review_task_batches SET cursor_before_json=?",
                ('{"forged":true}',),
            )
        elif fact == "source_receipt":
            trigger_name = "review_task_source_publications_no_update"
            trigger_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                    (trigger_name,),
                ).fetchone()[0]
            )
            connection.execute("DROP TRIGGER review_task_source_publications_no_update")
            connection.execute("UPDATE review_task_source_publications SET receipt_json='{}'")
        else:  # pragma: no cover - fixture invariant
            raise AssertionError(f"unsupported ReviewTask receipt fact {fact}")
        connection.execute(trigger_sql)
        connection.commit()


def _populate_inventory(database: Path, framework: Path) -> None:
    initialize_inventory_schema(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executemany(
            """INSERT INTO scans(
            scan_id,root,started_ns,completed_ns,files_seen,directories_seen,
            bytes_seen,skipped_links,excluded_directories,errors,status)
            VALUES(?,'C:/fixture',1,?,?,?,?,?,?,?,?)""",
            (
                (1, 1, 0, 0, 0, 0, 0, 0, "complete"),
                (2, 1, 0, 0, 0, 0, 0, 0, "complete"),
                (3, 1, 0, 0, 0, 0, 0, 0, "complete"),
                (4, 1, 0, 0, 0, 0, 0, 0, "complete"),
                (5, None, None, None, None, None, None, None, "building"),
                (6, 1, 1, 0, 3, 0, 0, 1, "partial"),
            ),
        )
        connection.execute(
            """INSERT INTO inventory_checkpoints(
            root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
            VALUES('C:/fixture',3,'C:','1',10,1,1)"""
        )
        connection.execute(
            """INSERT INTO files(
            scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
            VALUES(6,'C:/fixture/partial.bin',?,?,3,4,5)""",
            (bytes(16), (2).to_bytes(16, "little")),
        )
        connection.commit()
    with FrameworkState(framework):
        pass
    with sqlite3.connect(framework) as connection:
        connection.execute(
            """INSERT INTO initial_runs(
            run_id,root,started_ns,completed_ns,status,run_kind,scan_id)
            VALUES(100,'C:/other',1,1,'completed','initial',1)"""
        )
        connection.commit()


def _by_key(plan: RetentionPlan, store: str) -> dict[int, RetentionItem]:
    selected = next(value for value in plan.stores if value.store == store)
    return {item.key: item for item in selected.items}


def test_absent_state_and_cli_json_do_not_create_directories(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "absent"
    plan = plan_retention(state, now_ns=NOW_NS)

    assert [store.status for store in plan.stores] == ["absent"] * 4
    assert not state.exists()

    assert (
        cli_main(
            [
                "--state-directory",
                str(state),
                "--retention-status",
                "--retention-json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["deletion_supported"] is False
    assert {store["status"] for store in payload["stores"]} == {"absent"}
    assert not state.exists()


def test_plan_retention_signature_and_snapshot_then_planning_phase_order(
    tmp_path: Path,
) -> None:
    assert str(inspect.signature(plan_retention)) == (
        "(state_directory: 'Path', *, policy: 'RetentionPolicy | None' = None, "
        "after: 'Mapping[str, int] | None' = None, "
        "stores: 'Sequence[str] | None' = None, now_ns: 'int', "
        "cancelled: 'Callable[[], bool] | None' = None, "
        "observer: 'RetentionObserver | None' = None) -> 'RetentionPlan'"
    )
    _populate_semantic(tmp_path / "semantic.sqlite3")
    _populate_catalog(tmp_path / "document_catalog.sqlite3")
    _populate_inventory(
        tmp_path / "dedup.sqlite3",
        tmp_path / "framework.sqlite3",
    )
    _populate_framework(tmp_path / "framework.sqlite3")
    events: list[tuple[str, str]] = []

    plan = plan_retention(
        tmp_path,
        policy=RetentionPolicy(minimum_age_ns=0),
        now_ns=NOW_NS,
        observer=lambda store, phase: events.append((store, phase)),
    )

    assert events == [
        ("semantic", "snapshot_opened"),
        ("catalog", "snapshot_opened"),
        ("inventory", "snapshot_opened"),
        ("framework", "snapshot_opened"),
        ("semantic", "planned"),
        ("catalog", "planned"),
        ("inventory", "planned"),
        ("framework", "planned"),
    ]
    assert tuple(store.store for store in plan.stores) == (
        "semantic",
        "catalog",
        "inventory",
        "framework",
    )
    assert all(store.status == "ready" for store in plan.stores)
    assert plan.dry_run
    assert not plan.deletion_supported


def test_policy_does_not_promise_an_unimplemented_publication_depth() -> None:
    with pytest.raises(ValueError, match="keep_published must be exactly 2"):
        RetentionPolicy(keep_published=3)
    with pytest.raises(ValueError, match="keep_published must be exactly 2"):
        RetentionPolicy(keep_published=1)
    with pytest.raises(ValueError, match="minimum_age_ns"):
        RetentionPolicy(minimum_age_ns=True)
    with pytest.raises(ValueError, match="batch_size"):
        RetentionPolicy(batch_size=True)


def test_plan_rejects_boolean_time_and_cursor_identifiers(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="now_ns"):
        plan_retention(tmp_path, now_ns=True)
    with pytest.raises(ValueError, match="cursors"):
        plan_retention(tmp_path, now_ns=NOW_NS, after={"semantic": True})


def test_foreign_key_activation_failure_blocks_and_closes_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    initialize_semantic_state(database)
    connection = sqlite3.connect(database)

    class ScalarResult:
        @staticmethod
        def fetchone() -> tuple[int]:
            return (0,)

    class ForeignKeysDisabledConnection:
        def __init__(self, wrapped: sqlite3.Connection) -> None:
            self.wrapped = wrapped

        @property
        def row_factory(self):
            return self.wrapped.row_factory

        @row_factory.setter
        def row_factory(self, value) -> None:
            self.wrapped.row_factory = value

        @property
        def in_transaction(self) -> bool:
            return self.wrapped.in_transaction

        def execute(self, statement: str, *parameters):
            if statement.strip().casefold() == "pragma foreign_keys":
                return ScalarResult()
            return self.wrapped.execute(statement, *parameters)

        def set_progress_handler(self, callback, instructions: int) -> None:
            self.wrapped.set_progress_handler(callback, instructions)

        def rollback(self) -> None:
            self.wrapped.rollback()

        def close(self) -> None:
            self.wrapped.close()

    proxy = ForeignKeysDisabledConnection(connection)
    original_connect = sqlite3.connect
    monkeypatch.setattr(
        retention_module,
        "sqlite3",
        SimpleNamespace(
            connect=lambda *args, **kwargs: proxy,
            OperationalError=sqlite3.OperationalError,
            Row=sqlite3.Row,
        ),
    )
    assert sqlite3.connect is original_connect

    plan = plan_retention(
        tmp_path,
        stores=("semantic",),
        now_ns=NOW_NS,
    )

    assert plan.stores[0].status == "blocked"
    assert "could not enforce foreign_keys" in str(plan.stores[0].detail)
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_snapshot_closes_connection_when_observer_raises_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    initialize_semantic_state(database)
    connection = sqlite3.connect(database)
    original_connect = sqlite3.connect
    monkeypatch.setattr(
        retention_module,
        "sqlite3",
        SimpleNamespace(
            connect=lambda *args, **kwargs: connection,
            OperationalError=sqlite3.OperationalError,
            Row=sqlite3.Row,
        ),
    )
    assert sqlite3.connect is original_connect

    class InjectedAbort(BaseException):
        pass

    def abort(_store: str, stage: str) -> None:
        if stage == "snapshot_opened":
            raise InjectedAbort

    with pytest.raises(InjectedAbort):
        plan_retention(
            tmp_path,
            stores=("semantic",),
            now_ns=NOW_NS,
            observer=abort,
        )
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_semantic_policy_protects_heads_builders_leases_and_base_chain(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    _populate_semantic(database)

    default = plan_retention(
        tmp_path,
        stores=("semantic",),
        now_ns=NOW_NS,
    )
    assert _by_key(default, "semantic")[8].disposition == "protected"
    assert "policy_not_configured" in _by_key(default, "semantic")[8].reasons

    plan = plan_retention(
        tmp_path,
        policy=RetentionPolicy(minimum_age_ns=0),
        stores=("semantic",),
        now_ns=NOW_NS,
    )
    items = _by_key(plan, "semantic")
    assert "current_published_generation" in items[5].reasons
    assert "previous_published_generation" in items[4].reasons
    # Completed generations, including failed/retired descendants, no longer
    # need their source generation to resume a base clone.  A stale lineage
    # pointer alone must not retain the whole ancestor chain.
    assert items[3].disposition == "eligible"
    assert "referenced_as_generation_base" not in items[3].reasons
    assert items[6].disposition == "protected"
    assert "live_worker_lease" in items[6].reasons
    assert items[7].disposition == "protected"
    assert "resumable_builder_no_durable_owner" in items[7].reasons
    assert items[8].disposition == "eligible"
    assert items[8].estimated_rows >= 1


def test_semantic_retention_releases_base_after_builder_is_terminal(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _semantic_model()
    initialize_semantic_state(database)
    register_embedding_model(database, model, allow_test_provider=True)
    with semantic_database(database) as connection:
        connection.executemany(
            """INSERT INTO embedding_generations(
            generation_id,model_signature,processing_signature,status,
            provenance_json,cursor_json,started_ns,completed_ns,
            base_generation_id,base_clone_complete)
            VALUES(?,?,'fixture',?,'{}','{}',1,?,?,?)""",
            (
                (1, model.model_signature, "failed", 2, None, 1),
                (2, model.model_signature, "building", None, 1, 0),
            ),
        )

    active = plan_retention(
        tmp_path,
        stores=("semantic",),
        now_ns=NOW_NS,
        policy=RetentionPolicy(minimum_age_ns=0),
    )
    active_items = _by_key(active, "semantic")
    assert active_items[1].disposition == "blocked"
    assert "referenced_as_generation_base" in active_items[1].reasons

    with semantic_database(database) as connection:
        connection.execute(
            "UPDATE embedding_generations SET status='failed',completed_ns=3 "
            "WHERE generation_id=2"
        )

    terminal = plan_retention(
        tmp_path,
        stores=("semantic",),
        now_ns=NOW_NS,
        policy=RetentionPolicy(minimum_age_ns=0),
    )
    terminal_items = _by_key(terminal, "semantic")
    assert terminal_items[1].disposition == "eligible"
    assert "referenced_as_generation_base" not in terminal_items[1].reasons


def test_semantic_retention_accounts_for_append_only_lineage_and_outbox(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    _populate_semantic(database)
    with semantic_database(database) as connection:
        cursor = connection.execute(
            """INSERT INTO semantic_work_receipts(
            receipt_key,contract_version,stage_id,stage_version,
            processing_signature,status,execution_mode,reproducibility_class,
            entity_kind,entity_id,generation_id,model_signature,receipt_json,
            committed_ns)
            VALUES('receipt-key','neocortex.work-receipt/v1','semantic.embedding',
            'v1','fixture','succeeded','executed','exact','text_chunk',
            'fixture-chunk',8,'retention-model-v1','{}',1)"""
        )
        connection.execute(
            """INSERT INTO semantic_derivation_outbox(
            receipt_id,event_kind,aggregate_kind,aggregate_id,payload_json,
            committed_ns)
            VALUES(1,'published','text_chunk','fixture-chunk','{}',1)"""
        )
        assert cursor.lastrowid == 1

    plan = plan_retention(
        tmp_path,
        stores=("semantic",),
        now_ns=NOW_NS,
        policy=RetentionPolicy(minimum_age_ns=0),
    )
    hold = next(
        item for item in plan.stores[0].holds if item.name == "semantic_lineage_and_outbox"
    )
    assert hold.rows == 2
    assert hold.estimated_bytes > 0


def test_catalog_protects_publications_builders_and_uncertain_actions(
    tmp_path: Path,
) -> None:
    _populate_catalog(tmp_path / "document_catalog.sqlite3")

    plan = plan_retention(
        tmp_path,
        policy=RetentionPolicy(minimum_age_ns=0),
        stores=("catalog",),
        now_ns=NOW_NS,
    )
    items = _by_key(plan, "catalog")
    assert "current_published_generation" in items[3].reasons
    assert "previous_published_generation" in items[2].reasons
    # A terminal child preserves its own lineage receipt, but it no longer
    # needs the ancestor as a live clone source.  Only an active catalog build
    # should keep that base generation blocked.
    assert items[1].disposition == "eligible"
    assert "referenced_as_generation_base" not in items[1].reasons
    assert items[4].disposition == "protected"
    assert "builder_liveness_unverifiable" in items[4].reasons
    assert items[5].disposition == "protected"
    assert "uncertain_organization_action" in items[5].reasons
    assert items[6].disposition == "eligible"
    store = plan.stores[0]
    assert (
        next(hold for hold in store.holds if hold.name == "uncertain_organization_actions").rows
        == 1
    )


def test_catalog_releases_terminal_base_chain_but_holds_active_builder(
    tmp_path: Path,
) -> None:
    database = tmp_path / "document_catalog.sqlite3"
    initialize_document_catalog(database)
    with document_catalog_database(database) as connection:
        connection.executemany(
            """INSERT INTO catalog_runs(
            catalog_run_id,framework_run_id,source_kind,mode,status,started_ns,
            completed_ns)
            VALUES(?,NULL,'pdf','classify',?,1,?)""",
            ((1, "completed", 2), (2, "running", None)),
        )
        connection.executemany(
            """INSERT INTO catalog_generations(
            generation_id,catalog_run_id,source_kind,base_generation_id,status,
            started_ns,completed_ns,published_ns)
            VALUES(?,?,'pdf',?,?,1,?,NULL)""",
            ((1, 1, None, "failed", 2), (2, 2, 1, "building", None)),
        )
        connection.commit()

    active = plan_retention(
        tmp_path,
        stores=("catalog",),
        now_ns=NOW_NS,
        policy=RetentionPolicy(minimum_age_ns=0),
    )
    active_item = _by_key(active, "catalog")[1]
    assert active_item.disposition == "blocked"
    assert "referenced_as_generation_base" in active_item.reasons

    with document_catalog_database(database) as connection:
        connection.execute(
            "UPDATE catalog_generations SET status='failed',completed_ns=3 "
            "WHERE generation_id=2"
        )
        connection.execute(
            "UPDATE catalog_runs SET status='failed',completed_ns=3 "
            "WHERE catalog_run_id=2"
        )
        connection.commit()

    terminal = plan_retention(
        tmp_path,
        stores=("catalog",),
        now_ns=NOW_NS,
        policy=RetentionPolicy(minimum_age_ns=0),
    )
    terminal_item = _by_key(terminal, "catalog")[1]
    assert terminal_item.disposition == "eligible"
    assert "referenced_as_generation_base" not in terminal_item.reasons


def test_inventory_protects_current_previous_builder_candidate_and_framework_use(
    tmp_path: Path,
) -> None:
    _populate_inventory(
        tmp_path / "dedup.sqlite3",
        tmp_path / "framework.sqlite3",
    )

    plan = plan_retention(
        tmp_path,
        policy=RetentionPolicy(minimum_age_ns=0),
        stores=("inventory",),
        now_ns=NOW_NS,
    )
    items = _by_key(plan, "inventory")
    assert "referenced_by_framework_run" in items[1].reasons
    assert "previous_published_inventory" in items[2].reasons
    assert "current_published_inventory" in items[3].reasons
    assert "complete_publication_candidate" in items[4].reasons
    assert "active_inventory_builder" in items[5].reasons
    assert items[6].disposition == "eligible"
    assert items[6].estimated_rows == 2


def test_framework_protects_uncertain_actions_and_human_evidence(
    tmp_path: Path,
) -> None:
    _populate_framework(tmp_path / "framework.sqlite3")
    _populate_review_task_holds(tmp_path / "framework.sqlite3")

    plan = plan_retention(
        tmp_path,
        policy=RetentionPolicy(minimum_age_ns=0),
        stores=("framework",),
        now_ns=NOW_NS,
    )
    items = _by_key(plan, "framework")
    assert items[1].disposition == "eligible"
    assert "human_evidence_provenance" in items[2].reasons
    assert "uncertain_file_action" in items[3].reasons
    assert "file_action_audit_evidence" in items[4].reasons
    assert items[4].disposition == "protected"
    assert items[5].disposition == "protected"
    assert items[6].disposition == "protected"
    holds = {hold.name: hold for hold in plan.stores[0].holds}
    # One legacy decision, one durable ReviewTask evidence record and one human
    # decision event are permanent. The producer batch, scan progress and
    # system-open event are reconstructible coordination state and stay out.
    assert holds["human_review_evidence"].rows == 3
    assert holds["file_action_audit_evidence"].rows == 3
    # The effective source head is derived, but it is published truth. Its
    # owner-local publication row, final batch receipt and progress receipt are
    # retained separately from irreconstructible human knowledge.
    assert holds["published_review_task_state"].rows == 4
    assert holds["published_review_task_state"].estimated_bytes > 0


def test_framework_retention_holds_and_validates_complete_review_batch_chain(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    _populate_framework(database)
    _populate_review_task_holds(database, multipage=True)

    plan = plan_retention(
        tmp_path,
        policy=RetentionPolicy(minimum_age_ns=0),
        stores=("framework",),
        now_ns=NOW_NS,
    )
    hold = next(item for item in plan.stores[0].holds if item.name == "published_review_task_state")
    assert hold.rows == 5
    _corrupt_published_review_task_receipt(database, fact="membership_binding")

    with pytest.raises(RuntimeError, match="ReviewTask"):
        plan_retention(
            tmp_path,
            policy=RetentionPolicy(minimum_age_ns=0),
            stores=("framework",),
            now_ns=NOW_NS,
        )


@pytest.mark.parametrize(
    "fact",
    (
        "membership",
        "progress",
        "progress_mismatch",
        "membership_binding",
        "batch_cursor",
        "source_receipt",
    ),
)
def test_framework_retention_fails_closed_on_incomplete_review_source_receipt(
    tmp_path: Path,
    fact: str,
) -> None:
    database = tmp_path / "framework.sqlite3"
    _populate_framework(database)
    _populate_review_task_holds(database)
    _corrupt_published_review_task_receipt(database, fact=fact)

    with pytest.raises(RuntimeError, match="ReviewTask"):
        plan_retention(
            tmp_path,
            policy=RetentionPolicy(minimum_age_ns=0),
            stores=("framework",),
            now_ns=NOW_NS,
        )


def test_framework_retention_holds_empty_review_source_head_without_memberships(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    publication = ReviewTaskPublication(
        batch_id="empty-batch",
        batch_key="empty-batch-key",
        fence=ReviewTaskSourceFence.create(
            scope="personal",
            task_type="value-review",
            selector_signature="selector-v1",
            source_snapshot={"fixture": "empty-retention-head"},
        ),
        cursor_before=None,
        cursor_after=None,
        inputs=(),
        tasks=(),
        coverage=ReviewTaskCoverage.COMPLETE,
        producer_signature="producer-v1",
        confirmed_ns=100,
    )
    publish_review_task_page(database, publication, expected_progress_revision=None)

    plan = plan_retention(
        tmp_path,
        policy=RetentionPolicy(minimum_age_ns=0),
        stores=("framework",),
        now_ns=NOW_NS,
    )

    hold = next(item for item in plan.stores[0].holds if item.name == "published_review_task_state")
    assert hold.rows == 3
    assert hold.estimated_bytes > 0


def test_keyset_page_is_bounded_repeatable_and_has_resume_cursor(
    tmp_path: Path,
) -> None:
    _populate_semantic(tmp_path / "semantic.sqlite3")
    policy = RetentionPolicy(minimum_age_ns=0, batch_size=3)

    first = plan_retention(
        tmp_path,
        policy=policy,
        stores=("semantic",),
        now_ns=NOW_NS,
    )
    repeated = plan_retention(
        tmp_path,
        policy=policy,
        stores=("semantic",),
        now_ns=NOW_NS,
    )
    store = first.stores[0]
    assert first == repeated
    assert tuple(item.key for item in store.items) == (1, 2, 3)
    assert store.truncated is True
    assert store.next_after == 3

    second = plan_retention(
        tmp_path,
        policy=policy,
        stores=("semantic",),
        after={"semantic": store.next_after},
        now_ns=NOW_NS,
    )
    assert tuple(item.key for item in second.stores[0].items) == (4, 5, 6)


def test_schema_drift_blocks_without_modifying_main_database(tmp_path: Path) -> None:
    database = tmp_path / "semantic.sqlite3"
    initialize_semantic_state(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("ALTER TABLE embedding_generations ADD COLUMN vendor TEXT")
        connection.commit()
    before_database = database.read_bytes()
    before_names = {path.name for path in tmp_path.iterdir()}

    plan = plan_retention(
        tmp_path,
        stores=("semantic",),
        now_ns=NOW_NS,
    )

    assert plan.stores[0].status == "blocked"
    assert "incompatible columns" in str(plan.stores[0].detail)
    assert database.read_bytes() == before_database
    assert plan.sqlite_read_snapshot_may_touch_shm is True
    assert {path.name for path in tmp_path.iterdir()} - before_names <= {
        "semantic.sqlite3-shm",
        "semantic.sqlite3-wal",
    }


def test_framework_retention_requires_exact_current_v22_without_migrating_legacy(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        framework_schema._build_v21_exact_schema(connection)
        connection.execute("INSERT INTO metadata VALUES('schema_version','21')")
        connection.commit()
    before = database.read_bytes()

    plan = plan_retention(tmp_path, stores=("framework",), now_ns=NOW_NS)

    assert plan.stores[0].status == "blocked"
    assert "framework schema is 21; expected 22" in str(plan.stores[0].detail)
    assert plan.stores[0].items == ()
    assert database.read_bytes() == before


def test_invalid_cross_store_dependencies_block_even_empty_primary_store(
    tmp_path: Path,
) -> None:
    inventory_state = tmp_path / "inventory-state"
    inventory_state.mkdir()
    initialize_inventory_schema(inventory_state / "dedup.sqlite3")
    framework_dependency = inventory_state / "framework.sqlite3"
    with FrameworkState(framework_dependency):
        pass
    with closing(sqlite3.connect(framework_dependency)) as connection:
        connection.execute("ALTER TABLE initial_runs ADD COLUMN vendor TEXT")
        connection.commit()

    inventory_plan = plan_retention(
        inventory_state,
        stores=("inventory",),
        now_ns=NOW_NS,
    )

    assert inventory_plan.stores[0].status == "blocked"
    assert inventory_plan.stores[0].items == ()
    assert inventory_plan.stores[0].detail == (
        "framework retention dependency could not be validated"
    )

    framework_state = tmp_path / "framework-state"
    framework_state.mkdir()
    with FrameworkState(framework_state / "framework.sqlite3"):
        pass
    catalog_dependency = framework_state / "document_catalog.sqlite3"
    initialize_document_catalog(catalog_dependency)
    with closing(sqlite3.connect(catalog_dependency)) as connection:
        connection.execute("ALTER TABLE catalog_generations ADD COLUMN vendor TEXT")
        connection.commit()

    framework_plan = plan_retention(
        framework_state,
        stores=("framework",),
        now_ns=NOW_NS,
    )

    assert framework_plan.stores[0].status == "blocked"
    assert framework_plan.stores[0].items == ()
    assert framework_plan.stores[0].detail == (
        "catalog retention dependency could not be validated"
    )


def test_reader_snapshot_does_not_mix_concurrent_semantic_commit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _semantic_model()
    initialize_semantic_state(database)
    register_embedding_model(database, model, allow_test_provider=True)
    with semantic_database(database) as connection:
        connection.execute(
            """INSERT INTO embedding_generations(
            generation_id,model_signature,processing_signature,status,
            provenance_json,cursor_json,started_ns,completed_ns,
            base_clone_complete)
            VALUES(1,'retention-model-v1','first','failed','{}','{}',1,1,1)"""
        )

    committed = False

    def commit_after_snapshot(store: str, event: str) -> None:
        nonlocal committed
        if store != "semantic" or event != "snapshot_opened" or committed:
            return
        with semantic_database(database) as connection:
            connection.execute(
                """INSERT INTO embedding_generations(
                generation_id,model_signature,processing_signature,status,
                provenance_json,cursor_json,started_ns,completed_ns,
                base_clone_complete)
                VALUES(2,'retention-model-v1','second','failed','{}','{}',1,1,1)"""
            )
        committed = True

    first = plan_retention(
        tmp_path,
        policy=RetentionPolicy(minimum_age_ns=0),
        stores=("semantic",),
        now_ns=NOW_NS,
        observer=commit_after_snapshot,
    )
    second = plan_retention(
        tmp_path,
        policy=RetentionPolicy(minimum_age_ns=0),
        stores=("semantic",),
        now_ns=NOW_NS,
    )

    assert tuple(item.key for item in first.stores[0].items) == (1,)
    assert tuple(item.key for item in second.stores[0].items) == (1, 2)


def test_cancellation_prevents_queries_and_preserves_database(tmp_path: Path) -> None:
    database = tmp_path / "semantic.sqlite3"
    _populate_semantic(database)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    with pytest.raises(RetentionPlanningCancelled):
        plan_retention(
            tmp_path,
            stores=("semantic",),
            now_ns=NOW_NS,
            cancelled=lambda: True,
        )

    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_cancellation_after_snapshot_open_closes_without_planning(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    _populate_semantic(database)
    before_database = database.read_bytes()
    cancel_requested = False

    def observe(_store: str, stage: str) -> None:
        nonlocal cancel_requested
        if stage == "snapshot_opened":
            cancel_requested = True

    with pytest.raises(RetentionPlanningCancelled):
        plan_retention(
            tmp_path,
            stores=("semantic",),
            now_ns=NOW_NS,
            cancelled=lambda: cancel_requested,
            observer=observe,
        )

    assert database.read_bytes() == before_database


def test_cli_human_output_exposes_bounded_read_only_contract(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _populate_semantic(tmp_path / "semantic.sqlite3")

    assert (
        cli_main(
            [
                "--state-directory",
                str(tmp_path),
                "--retention-status",
                "--retention-store",
                "semantic",
                "--retention-batch-size",
                "1",
                "--retention-min-age-days",
                "0",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "RETENTION_PLAN dry_run=1 deletion_supported=0" in output
    assert "keep_published=2 sqlite_shm_side_effect=possible" in output
    assert "RETENTION_STORE name=semantic status=ready" in output
    assert "items=1" in output
    assert "truncated=1 next_after=1" in output
    assert "RETENTION_HOLD store=semantic" in output
    assert "RETENTION_ITEM store=semantic" in output


def test_cli_rejects_unbounded_or_orphaned_retention_options() -> None:
    with pytest.raises(SystemExit) as invalid_batch:
        cli_main(["--retention-status", "--retention-batch-size", "1001"])
    assert invalid_batch.value.code == 2

    with pytest.raises(SystemExit) as orphaned:
        cli_main(["--retention-json"])
    assert orphaned.value.code == 2

    with pytest.raises(SystemExit) as destructive:
        cli_main(["--retention-status", "--apply"])
    assert destructive.value.code == 2

    with pytest.raises(SystemExit) as duplicate_store:
        cli_main(
            [
                "--retention-status",
                "--retention-store",
                "semantic",
                "--retention-store",
                "semantic",
            ]
        )
    assert duplicate_store.value.code == 2

    with pytest.raises(SystemExit) as orphaned_cursor:
        cli_main(
            [
                "--retention-status",
                "--retention-store",
                "semantic",
                "--retention-catalog-after",
                "1",
            ]
        )
    assert orphaned_cursor.value.code == 2

    with pytest.raises(SystemExit) as route:
        cli_main(["--retention-status", "--route", "pdf"])
    assert route.value.code == 2


# endregion [02]
