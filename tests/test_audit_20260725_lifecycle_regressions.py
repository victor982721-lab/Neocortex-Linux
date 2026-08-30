"""Risk-driven regressions found by the 2026-07-25 continuation audit."""
# region [00] Contexto del módulo
# Módulo: tests/test_audit_20260725_lifecycle_regressions.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.deduplication import DedupIndex, InventoryCheckpoint, snapshot_path
from neocortex.workflow.actions.file_action_recovery import (
    effect_receipt_json,
    expected_identity_json,
    list_file_action_reconciliations,
)
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.persistence.framework_schema import SCHEMA_VERSION
from neocortex.workflow.retention.planner import (
    RetentionPlanningCancelled,
    RetentionPolicy,
    plan_retention,
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
from tests.internal_paths_test_support import begin_signed_normal_run
# endregion [01]

# region [02] Implementación


NOW_NS = 10_000_000_000


def _begin_run(state: FrameworkState, root: Path) -> int:
    return begin_signed_normal_run(state, root)


def _action_sandbox(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    return state_directory / "framework.sqlite3", root


def test_abandoned_intent_is_not_misclassified_as_uncertain_effect(
    tmp_path: Path,
) -> None:
    database, root = _action_sandbox(tmp_path)
    source = root / "source.bin"
    source.write_bytes(b"source")
    with FrameworkState(database) as state:
        run_id = _begin_run(state, root)
        started = state.begin_file_action(
            run_id,
            "correct_extension",
            str(source),
            str(source.with_suffix(".dat")),
            None,
            None,
            True,
        )
        applying = state.begin_file_action(
            run_id,
            "correct_extension",
            str(source),
            str(source.with_suffix(".txt")),
            None,
            None,
            True,
        )
        state.mark_file_actions_applying(
            (
                (
                    applying,
                    expected_identity_json(
                        snapshot_path(source),
                        source_path=str(source),
                        target_path=str(source.with_suffix(".txt")),
                    ),
                ),
            )
        )

        assert state.mark_abandoned_actions() == 2
        rows = dict(
            state._connection.execute(
                "SELECT action_id,status FROM file_actions ORDER BY action_id"
            )
        )

    assert rows[started] == "failed"
    assert rows[applying] == "recovery_required"


def test_repeated_terminal_transition_rejects_contradictory_evidence(
    tmp_path: Path,
) -> None:
    database, root = _action_sandbox(tmp_path)
    with FrameworkState(database) as state:
        run_id = _begin_run(state, root)
        action_id = state.begin_file_action(
            run_id, "fixture", str(root / "source"), None, None, None, False
        )
        state.finish_file_action(action_id, "failed", "first failure")
        state.finish_file_action(action_id, "failed", "first failure")
        with pytest.raises(RuntimeError, match="conflicting repeated file action"):
            state.finish_file_action(action_id, "failed", "different failure")


def test_trash_receipt_for_another_source_does_not_confirm_action(
    tmp_path: Path,
) -> None:
    database, root = _action_sandbox(tmp_path)
    source = root / "source.bin"
    source.write_bytes(b"source")
    snapshot = snapshot_path(source)
    identity = expected_identity_json(snapshot, source_path=str(source), target_path=None)
    source.unlink()
    receipt = effect_receipt_json(
        operation="trash",
        source_path=str(root / "different.bin"),
        target_path=None,
    )
    with FrameworkState(database) as state:
        run_id = _begin_run(state, root)
        action_id = state.begin_file_action(
            run_id, "trash_fixture", str(source), None, None, None, True
        )
        state.mark_file_actions_applying(((action_id, identity),))
        state.require_file_action_recovery((action_id,), "uncertain")
        state._connection.execute(
            "UPDATE file_actions SET effect_receipt_json=? WHERE action_id=?",
            (receipt, action_id),
        )
        state._connection.commit()

    result = list_file_action_reconciliations(database, limit=1)[0]
    assert result.classification == "ambiguous"
    assert "does not match" in result.detail


def test_recovery_reader_rejects_future_framework_schema(tmp_path: Path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION + 1),),
        )
        connection.commit()

    with pytest.raises(sqlite3.DatabaseError, match="newer than supported"):
        list_file_action_reconciliations(database)


def _retention_model() -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        "retention-audit-model-v1",
        "retention-audit-space-v1",
        EmbeddingModality.TEXT,
        "fixture/retention-audit",
        "1",
        4,
        "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )


def test_semantic_evidence_reference_blocks_generation_eligibility(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _retention_model()
    initialize_semantic_state(database)
    register_embedding_model(database, model, allow_test_provider=True)
    with semantic_database(database) as connection:
        connection.execute(
            """INSERT INTO semantic_items(
            item_id,source_kind,source_identity,identity_version,path,
            content_xxh3_128,content_bytes,content_xxh3_64_guard,
            provenance_json,refresh_token,active,updated_ns,source_revision_json)
            VALUES('item','pdf','source','v1','C:/fixture.pdf','abc',3,'guard',
            '{}','fixture',1,1,'{}')"""
        )
        connection.execute(
            """INSERT INTO embedding_generations(
            generation_id,model_signature,processing_signature,status,
            provenance_json,cursor_json,started_ns,completed_ns,base_clone_complete)
            VALUES(1,?,'fixture','failed','{}','{}',1,1,1)""",
            (model.model_signature,),
        )
        connection.execute(
            """INSERT INTO label_prototypes(
            prototype_id,ontology_id,ontology_version,concept_id,prototype_version,
            model_signature,vector_space,prototype_text,content_xxh3_128,
            content_bytes,content_xxh3_64_guard,dimensions,vector_dtype,vector_blob,
            original_norm,calibration_status,feedback_reference,provenance_json,
            active,created_ns,updated_ns)
            VALUES('prototype','ontology','1','concept','1',?,?,'fixture','abc',3,
            'guard',4,'float32',zeroblob(16),1.0,'uncalibrated',NULL,'{}',1,1,1)""",
            (model.model_signature, model.vector_space),
        )
        connection.execute(
            """INSERT INTO semantic_evidence(
            item_id,source_entity_id,ontology_id,ontology_version,concept_id,
            prototype_id,query_model_signature,indexed_model_signature,vector_space,
            score,rank,generation_id,calibration_status,disposition,
            feedback_reference,provenance_json,refresh_token,active,updated_ns)
            VALUES('item','entity','ontology','1','concept','prototype',?,?,?,0.5,1,
            1,'uncalibrated','advisory',NULL,'{}','fixture',1,1)""",
            (model.model_signature, model.model_signature, model.vector_space),
        )

    plan = plan_retention(
        tmp_path,
        policy=RetentionPolicy(minimum_age_ns=0),
        stores=("semantic",),
        now_ns=NOW_NS,
    )
    item = plan.stores[0].items[0]
    assert item.disposition == "blocked"
    assert "referenced_by_semantic_evidence" in item.reasons


def test_framework_retention_protects_last_completed_run(tmp_path: Path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    with sqlite3.connect(database) as connection:
        connection.executemany(
            """INSERT INTO initial_runs(
            run_id,root,started_ns,completed_ns,status,run_kind)
            VALUES(?,'C:/fixture',1,1,?,'initial')""",
            ((1, "completed"), (2, "failed"), (3, "cancelled")),
        )
        connection.commit()

    plan = plan_retention(
        tmp_path,
        policy=RetentionPolicy(minimum_age_ns=0),
        stores=("framework",),
        now_ns=NOW_NS,
    )
    first = next(item for item in plan.stores[0].items if item.key == 1)
    assert first.disposition == "protected"
    assert "last_completed_run" in first.reasons


def test_retention_query_interrupt_is_translated_to_domain_cancellation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _retention_model()
    initialize_semantic_state(database)
    register_embedding_model(database, model, allow_test_provider=True)
    with semantic_database(database) as connection:
        connection.executemany(
            """INSERT INTO embedding_generations(
            generation_id,model_signature,processing_signature,status,
            provenance_json,cursor_json,started_ns,completed_ns,base_clone_complete)
            VALUES(?,?,'fixture','failed','{}','{}',1,1,1)""",
            ((value, model.model_signature) for value in range(1, 501)),
        )

    calls = 0
    abort_after: int | None = None

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return abort_after is not None and calls >= abort_after

    def observer(_store: str, stage: str) -> None:
        nonlocal abort_after
        if stage == "snapshot_opened":
            abort_after = calls + 2

    with pytest.raises(RetentionPlanningCancelled):
        plan_retention(
            tmp_path,
            policy=RetentionPolicy(minimum_age_ns=0, batch_size=500),
            stores=("semantic",),
            now_ns=NOW_NS,
            cancelled=cancelled,
            observer=observer,
        )


def _checkpoint(root: Path, scan_id: int, next_usn: int) -> InventoryCheckpoint:
    return InventoryCheckpoint(str(root), scan_id, "fixture:", 7, next_usn)


def test_inventory_prune_preserves_previous_and_explicit_cross_store_holds(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "item.bin"
    source.write_bytes(b"one")
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        first = index.scan(root, excluded_paths=())
        index.bind_inventory_checkpoint(_checkpoint(root, first.scan_id, 10))
        source.write_bytes(b"two")
        second = index.scan(root, excluded_paths=())
        index.bind_inventory_checkpoint(_checkpoint(root, second.scan_id, 20))
        source.write_bytes(b"three")
        third = index.scan(root, excluded_paths=())
        index.bind_inventory_checkpoint(_checkpoint(root, third.scan_id, 30))

        index.prune_obsolete_state(protected_scan_ids=(first.scan_id,))

        assert index.file_count(first.scan_id) == 1
        assert index.file_count(second.scan_id) == 1
        assert index.file_count(third.scan_id) == 1


def test_inventory_prune_without_dependency_proof_fails_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "item.bin").write_bytes(b"one")
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        first = index.scan(root, excluded_paths=())
        index.bind_inventory_checkpoint(_checkpoint(root, first.scan_id, 10))
        (root / "item.bin").write_bytes(b"two")
        second = index.scan(root, excluded_paths=())
        index.bind_inventory_checkpoint(_checkpoint(root, second.scan_id, 20))

        removed = index.prune_obsolete_state()

        assert removed == {
            "plan_members": 0,
            "plan_groups": 0,
            "plan_summaries": 0,
            "files": 0,
            "fingerprints": 0,
        }
        assert index.file_count(first.scan_id) == 1
        assert index.file_count(second.scan_id) == 1


def test_inventory_prune_removes_only_older_unheld_payload(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "item.bin"
    source.write_bytes(b"one")
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        first = index.scan(root, excluded_paths=())
        index.bind_inventory_checkpoint(_checkpoint(root, first.scan_id, 10))
        source.write_bytes(b"two")
        second = index.scan(root, excluded_paths=())
        index.bind_inventory_checkpoint(_checkpoint(root, second.scan_id, 20))
        source.write_bytes(b"three")
        third = index.scan(root, excluded_paths=())
        index.bind_inventory_checkpoint(_checkpoint(root, third.scan_id, 30))

        removed = index.prune_obsolete_state(protected_scan_ids=())

        assert removed["files"] == 1
        assert index.file_count(first.scan_id) == 0
        assert index.file_count(second.scan_id) == 1
        assert index.file_count(third.scan_id) == 1


def test_framework_exposes_all_cross_store_inventory_holds(tmp_path: Path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        state._connection.executemany(
            """INSERT INTO initial_runs(
            run_id,root,started_ns,completed_ns,status,run_kind,scan_id)
            VALUES(?,'C:/fixture',1,1,'completed','initial',?)""",
            ((1, 7), (2, 3), (3, 7)),
        )
        state._connection.commit()

        assert state.referenced_inventory_scan_ids() == (3, 7)


# endregion [02]
