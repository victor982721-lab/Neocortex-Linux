from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.api import curation_lifecycle_api
from neocortex.api import curation_authorization_api
import neocortex.curation.lifecycle as lifecycle
from neocortex.curation.authorization import (
    CurationAuthorizationError,
    authorize_curation_items,
)
from neocortex.workflow.authorization.repository import (
    AuthorizationGrantConflict,
    issue_authorization_grant,
    read_authorization_grant,
)
from neocortex.curation.preview import build_curation_plan_page
from neocortex.deduplication import DedupIndex, DedupPlanner, InventoryCheckpoint
from neocortex.documents.document_catalog import initialize_document_catalog
from neocortex.persistence.framework_schema import initialize_framework_schema


def _state(tmp_path: Path) -> tuple[Path, Path, str]:
    state = tmp_path / "state"
    corpus = tmp_path / "corpus"
    state.mkdir()
    corpus.mkdir()
    (corpus / "keep.txt").write_bytes(b"same")
    (corpus / "duplicate.txt").write_bytes(b"same")
    (corpus / "empty.txt").write_bytes(b"")
    with DedupIndex(state / "dedup.sqlite3") as index:
        summary = index.scan(corpus)
        index.bind_inventory_checkpoint(
            InventoryCheckpoint(str(corpus), summary.scan_id, None, None, None, True)
        )
        DedupPlanner(index, partial_threshold=0).plan(summary.scan_id, exact_compare=False)

    catalog = state / "document_catalog.sqlite3"
    initialize_document_catalog(catalog)
    with closing(sqlite3.connect(catalog)) as connection:
        connection.execute(
            """INSERT INTO catalog_runs(
            catalog_run_id,source_kind,mode,status,started_ns,completed_ns,summary_json)
            VALUES (1,'all','plan','completed',1,2,'{}')"""
        )
        connection.execute(
            """INSERT INTO organization_plans(
            catalog_run_id,source_kind,file_key,source_path,destination_path,
            organization_root,volume_id,file_id,size,mtime_ns,birthtime_ns,
            classifier_signature,primary_kind,confidence,status,reason,
            evidence_json,planned_ns)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                1,
                "text",
                "text:1",
                str(corpus / "keep.txt"),
                str(state / "organized" / "keep.txt"),
                str(state / "organized"),
                "1",
                "2",
                4,
                1,
                -1,
                "fixture-classifier-v1",
                "text",
                0.9,
                "planned",
                "classification_above_threshold",
                '{"uncertainty":"low"}',
                1,
            ),
        )
        connection.commit()

    framework = state / "framework.sqlite3"
    with closing(sqlite3.connect(framework)) as connection:
        initialize_framework_schema(connection, lambda: None)
    page = build_curation_plan_page(state, 1)
    assert page.coverage == "complete"
    assert page.plan_digest.startswith("sha256:")
    return state, framework, page.plan_digest


def _counts(database: Path) -> tuple[int, int, int, int]:
    with closing(sqlite3.connect(database)) as connection:
        return tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "review_task_batches",
                "review_tasks",
                "review_task_events",
                "file_actions",
            )
        )  # type: ignore[return-value]


def test_review_publishes_pages_replays_and_never_creates_file_actions(tmp_path: Path) -> None:
    state, framework, plan_digest = _state(tmp_path)
    first = lifecycle.review_curation_page(
        state,
        framework,
        plan_digest=plan_digest,
        limit=1,
        clock_ns=lambda: 1_000,
    )
    assert first.status == "complete"
    assert first.next_cursor is not None
    assert first.publication is not None
    assert first.publication.idempotent is False
    assert first.items[0].task_id is not None
    assert first.items[0].state.value == "open"  # type: ignore[union-attr]
    retry = lifecycle.review_curation_page(
        state,
        framework,
        plan_digest=plan_digest,
        limit=1,
        clock_ns=lambda: 9_000,
    )
    assert retry.publication is not None
    assert retry.publication.idempotent is True
    second = lifecycle.review_curation_page(
        state,
        framework,
        plan_digest=plan_digest,
        limit=1,
        cursor=first.next_cursor,
        clock_ns=lambda: 2_000,
    )
    assert second.publication is not None
    assert second.publication.idempotent is False
    assert second.plan_digest == first.plan_digest
    batches, tasks, events, file_actions = _counts(framework)
    assert batches == 2
    assert tasks == 2
    assert events == 2
    assert file_actions == 0


def test_decide_is_bound_to_plan_digest_cas_and_idempotent(tmp_path: Path) -> None:
    state, framework, plan_digest = _state(tmp_path)
    reviewed = lifecycle.review_curation_page(
        state,
        framework,
        plan_digest=plan_digest,
        limit=1,
        clock_ns=lambda: 1_000,
    )
    item = reviewed.items[0]
    assert item.current_event_id is not None
    decided = lifecycle.decide_curation_item(
        state,
        framework,
        plan_digest=plan_digest,
        item_id=item.item.item_id,
        expected_event_id=item.current_event_id,
        decision="resolved",
        decision_scope="until-source-change",
        actor="victor",
        note="evidence reviewed",
        clock_ns=lambda: 2_000,
    )
    assert decided.to_state.value == "resolved"
    replay = lifecycle.decide_curation_item(
        state,
        framework,
        plan_digest=plan_digest,
        item_id=item.item.item_id,
        expected_event_id=item.current_event_id,
        decision="resolved",
        decision_scope="until-source-change",
        actor="victor",
        note="evidence reviewed",
        clock_ns=lambda: 9_000,
    )
    assert replay.event_id == decided.event_id
    with pytest.raises(lifecycle.CurationLifecycleSnapshotChanged, match="event head"):
        lifecycle.decide_curation_item(
            state,
            framework,
            plan_digest=plan_digest,
            item_id=item.item.item_id,
            expected_event_id=item.current_event_id,
            decision="dismissed",
            decision_scope="permanent",
            actor="victor",
            note="changed command",
            clock_ns=lambda: 3_000,
        )
    assert _counts(framework)[2] == 2


def test_plan_change_invalidates_old_decision_without_new_event(tmp_path: Path) -> None:
    state, framework, plan_digest = _state(tmp_path)
    reviewed = lifecycle.review_curation_page(
        state,
        framework,
        plan_digest=plan_digest,
        limit=1,
        clock_ns=lambda: 1_000,
    )
    item = reviewed.items[0]
    assert item.current_event_id is not None
    catalog = state / "document_catalog.sqlite3"
    with closing(sqlite3.connect(catalog)) as connection:
        connection.execute(
            "UPDATE organization_plans SET reason='classification_changed' WHERE plan_id=1"
        )
        connection.commit()
    with pytest.raises(lifecycle.CurationLifecycleSnapshotChanged, match="digest"):
        lifecycle.decide_curation_item(
            state,
            framework,
            plan_digest=plan_digest,
            item_id=item.item.item_id,
            expected_event_id=item.current_event_id,
            decision="resolved",
            decision_scope="until-source-change",
            actor="victor",
            note="stale plan",
            clock_ns=lambda: 2_000,
        )
    assert _counts(framework)[2] == 1


def test_partial_plan_is_advisory_only_and_does_not_publish(tmp_path: Path, monkeypatch) -> None:
    state, framework, plan_digest = _state(tmp_path)
    original = build_curation_plan_page(state, 1)
    partial = replace(original, coverage="partial")
    monkeypatch.setattr(lifecycle, "build_curation_plan_page", lambda *_args, **_kwargs: partial)
    with pytest.raises(lifecycle.CurationLifecycleUnavailable, match="coverage"):
        lifecycle.review_curation_page(
            state,
            framework,
            plan_digest=plan_digest,
            limit=1,
            clock_ns=lambda: 1_000,
        )
    assert _counts(framework) == (0, 0, 0, 0)


def test_public_lifecycle_api_exposes_state_only_effects(tmp_path: Path, monkeypatch) -> None:
    state, framework, plan_digest = _state(tmp_path)
    monkeypatch.setattr(curation_lifecycle_api, "default_state_directory", lambda: state)
    reviewed = curation_lifecycle_api.curation_review_payload(
        plan_digest,
        limit=1,
        request_id="fixture-review",
        clock_ns=lambda: 1_000,
    )
    assert reviewed["schema"] == "neocortex.curation-review/v1"
    assert reviewed["effects"] == {
        "state": "review_task_publication",
        "corpus": "none",
        "external": "none",
    }
    assert reviewed["trust"]["actions_authorized"] is False
    linked = reviewed["page"]["items"][0]
    decided = curation_lifecycle_api.curation_decide_payload(
        plan_digest,
        linked["item_id"],
        expected_event_id=linked["current_event_id"],
        decision="dismissed",
        decision_scope="permanent",
        actor="victor",
        note="fixture decision",
        request_id="fixture-decision",
        clock_ns=lambda: 2_000,
    )
    assert decided["schema"] == "neocortex.curation-decision/v1"
    assert decided["idempotent"] is False
    assert decided["effects"]["corpus"] == "none"
    assert decided["trust"]["actions_authorized"] is False
    assert framework.is_file()


def test_authorization_grant_requires_review_and_is_append_only(tmp_path: Path) -> None:
    state, framework, plan_digest = _state(tmp_path)
    reviewed = lifecycle.review_curation_page(
        state,
        framework,
        plan_digest=plan_digest,
        limit=100,
        clock_ns=lambda: 1_000,
    )
    for offset, item in enumerate(reviewed.items, start=1):
        assert item.current_event_id is not None
        lifecycle.decide_curation_item(
            state,
            framework,
            plan_digest=plan_digest,
            item_id=item.item.item_id,
            expected_event_id=item.current_event_id,
            decision="resolved",
            decision_scope="permanent",
            actor="victor",
            note="reviewed fixture evidence",
            clock_ns=lambda offset=offset: 2_000 + offset,
        )
    organization = next(item for item in reviewed.items if item.item.kind == "organization_plan")
    outcome = authorize_curation_items(
        state,
        framework,
        plan_digest=plan_digest,
        item_ids=(organization.item.item_id,),
        action="move",
        actor="victor",
        expires_ns=10_000,
        max_bytes=4,
        clock_ns=lambda: 4_000,
    )
    assert outcome.idempotent is False
    assert outcome.grant.action == "move"
    assert outcome.grant.item_ids == (organization.item.item_id,)
    assert outcome.grant.review_task_heads is not None
    assert len(outcome.grant.review_task_heads) == 1
    captured_head = outcome.grant.review_task_heads[0]
    assert captured_head.item_id == organization.item.item_id
    assert captured_head.task_id == outcome.grant.task_ids[0]
    assert captured_head.task_version == 1
    assert captured_head.state == "resolved"
    assert captured_head.event_id
    assert captured_head.head_digest is not None
    assert outcome.grant.review_task_heads_digest is not None
    retry = authorize_curation_items(
        state,
        framework,
        plan_digest=plan_digest,
        item_ids=(organization.item.item_id,),
        action="move",
        actor="victor",
        expires_ns=10_000,
        max_bytes=4,
        clock_ns=lambda: 9_000,
    )
    assert retry.idempotent is True
    assert retry.grant.review_task_heads == outcome.grant.review_task_heads
    assert retry.grant.review_task_heads_digest == outcome.grant.review_task_heads_digest
    loaded = read_authorization_grant(framework, grant_id=outcome.grant.grant_id)
    assert loaded == outcome.grant
    with closing(sqlite3.connect(framework)) as connection:
        receipt_json = str(
            connection.execute(
                "SELECT receipt_json FROM curation_authorization_grants WHERE grant_id=?",
                (outcome.grant.grant_id,),
            ).fetchone()[0]
        )
        receipt = json.loads(receipt_json)
        assert receipt["review_task_heads"] == [captured_head.to_dict()]
        assert receipt["review_task_heads_digest"] == outcome.grant.review_task_heads_digest
        assert connection.execute(
            "SELECT COUNT(*) FROM curation_authorization_grants"
        ).fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE curation_authorization_grants SET actor='other' WHERE grant_id=?",
                (outcome.grant.grant_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM curation_authorization_grants WHERE grant_id=?",
                (outcome.grant.grant_id,),
            )
        connection.rollback()


def test_authorization_replay_rejects_changed_review_head_without_mutating_receipt(
    tmp_path: Path,
) -> None:
    state, framework, plan_digest = _state(tmp_path)
    reviewed = lifecycle.review_curation_page(
        state,
        framework,
        plan_digest=plan_digest,
        limit=100,
        clock_ns=lambda: 1_000,
    )
    for offset, item in enumerate(reviewed.items, start=1):
        assert item.current_event_id is not None
        lifecycle.decide_curation_item(
            state,
            framework,
            plan_digest=plan_digest,
            item_id=item.item.item_id,
            expected_event_id=item.current_event_id,
            decision="resolved",
            decision_scope="permanent",
            actor="victor",
            clock_ns=lambda offset=offset: 2_000 + offset,
        )
    organization = next(item for item in reviewed.items if item.item.kind == "organization_plan")
    outcome = authorize_curation_items(
        state,
        framework,
        plan_digest=plan_digest,
        item_ids=(organization.item.item_id,),
        action="move",
        actor="victor",
        expires_ns=10_000,
        max_bytes=4,
        clock_ns=lambda: 4_000,
    )
    assert outcome.grant.review_task_heads is not None
    with closing(sqlite3.connect(framework)) as connection:
        original_receipt = str(
            connection.execute(
                "SELECT receipt_json FROM curation_authorization_grants WHERE grant_id=?",
                (outcome.grant.grant_id,),
            ).fetchone()[0]
        )
    changed_head = replace(
        outcome.grant.review_task_heads[0],
        event_id=outcome.grant.review_task_heads[0].event_id + "-changed",
        head_digest=None,
    )
    changed_grant = replace(
        outcome.grant,
        review_task_heads=(changed_head,),
        review_task_heads_digest=None,
    )
    with pytest.raises(AuthorizationGrantConflict, match="idempotency key changed payload"):
        issue_authorization_grant(framework, changed_grant)
    with closing(sqlite3.connect(framework)) as connection:
        assert connection.execute(
            "SELECT receipt_json FROM curation_authorization_grants WHERE grant_id=?",
            (outcome.grant.grant_id,),
        ).fetchone() == (original_receipt,)
        assert connection.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (0,)


def test_authorization_rejects_unverified_trash_and_public_api_projects_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, framework, plan_digest = _state(tmp_path)
    reviewed = lifecycle.review_curation_page(
        state,
        framework,
        plan_digest=plan_digest,
        limit=100,
        clock_ns=lambda: 1_000,
    )
    duplicate = next(item for item in reviewed.items if item.item.kind == "duplicate_group")
    organization = next(item for item in reviewed.items if item.item.kind == "organization_plan")
    for offset, item in enumerate(reviewed.items, start=1):
        assert item.current_event_id is not None
        lifecycle.decide_curation_item(
            state,
            framework,
            plan_digest=plan_digest,
            item_id=item.item.item_id,
            expected_event_id=item.current_event_id,
            decision="resolved",
            decision_scope="permanent",
            actor="victor",
            clock_ns=lambda offset=offset: 2_000 + offset,
        )
    with pytest.raises(CurationAuthorizationError, match="full-hash"):
        authorize_curation_items(
            state,
            framework,
            plan_digest=plan_digest,
            item_ids=(duplicate.item.item_id,),
            action="trash",
            actor="victor",
            expires_ns=10_000,
            max_bytes=4,
            clock_ns=lambda: 4_000,
        )
    monkeypatch.setattr(curation_authorization_api, "default_state_directory", lambda: state)
    payload = curation_authorization_api.curation_authorize_payload(
        plan_digest,
        [organization.item.item_id],
        action="move",
        actor="victor",
        expires_ns=12_000,
        max_bytes=4,
        request_id="fixture-authorize",
        clock_ns=lambda: 5_000,
    )
    assert payload["schema"] == "neocortex.authorization-grant/v1"
    assert payload["status"] == "complete"
    assert payload["grant"]["action"] == "move"
    assert payload["effects"] == {
        "state": "authorization_grant",
        "corpus": "none",
        "external": "none",
    }
    assert payload["trust"]["actions_authorized"] is True
    assert payload["trust"]["physical_effect_applied"] is False
