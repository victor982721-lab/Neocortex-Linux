from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.persistence.framework_schema import initialize_framework_schema
from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod,
    EvidenceRef,
    PhysicalIdentityRef,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from neocortex.workflow.review.review_task_contracts import (
    CanonicalJsonObject,
    ReviewTaskCoverage,
    ReviewTaskDraft,
    ReviewTaskInput,
    ReviewTaskPublication,
    ReviewTaskSourceFence,
)
from neocortex.workflow.review.review_task_repository import (
    list_current_review_tasks,
    publish_review_task_page,
)
from neocortex import review_task_cli_adapter as adapter
from neocortex.read_api import ReadScope, ScopeBinding


def _published_task(state_directory: Path) -> tuple[Path, str, str]:
    database = state_directory / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        initialize_framework_schema(connection, lambda: None)
    resource = ResourceRef(
        resource_id="resource-cli-1",
        source_kind="inventory",
        owner="inventory",
        physical_identity=PhysicalIdentityRef(
            scheme="fixture-identity", value="volume:1", identity_version=1
        ),
        current_path="/fixture/cli.txt",
    )
    revision = RevisionRef(
        resource_id=resource.resource_id,
        revision_id="revision-cli-1",
        producer="fixture",
        processing_signature="fixture-v1",
        generation=1,
        state=RevisionState.CURRENT,
    )
    source = ReviewTaskInput(
        input_id="input-cli-1",
        fingerprint_algorithm="sha256",
        fingerprint="1" * 64,
        resource=resource,
        revision=revision,
    )
    task = ReviewTaskDraft(
        task_id="task-cli-1",
        logical_key="logical-cli-1",
        task_version=1,
        task_type="value-review",
        scope="personal",
        source_kind="review-decision",
        source_input_id=source.input_id,
        snapshot=CanonicalJsonObject.from_mapping({"candidate": 1}),
        evidence=(
            EvidenceRef(
                evidence_id="evidence-cli-1",
                resource_id=resource.resource_id,
                revision_id=revision.revision_id,
                method=EvidenceMethod.STRUCTURAL,
                identifiers=(("fixture", "cli"),),
            ),
        ),
        reason_code="uncertain-value",
        uncertainty_detail=CanonicalJsonObject.from_mapping({"basis": "fixture"}),
        impact=0.8,
        uncertainty=0.5,
        irreversibility=0.25,
        suggestions=("review evidence",),
        supersedes_task_id=None,
        created_ns=100,
    )
    fence = ReviewTaskSourceFence.create(
        scope="personal",
        task_type="value-review",
        selector_signature="value-review-selector-cli-v1",
        source_snapshot={"generation": 1, "owner": "inventory"},
    )
    publication = ReviewTaskPublication(
        batch_id="batch-cli-1",
        batch_key="batch-key-cli-1",
        fence=fence,
        cursor_before=None,
        cursor_after=None,
        inputs=(source,),
        tasks=(task,),
        coverage=ReviewTaskCoverage.COMPLETE,
        producer_signature="fixture-cli-v1",
        confirmed_ns=1_000,
    )
    publish_review_task_page(database, publication, expected_progress_revision=None)
    opened_event_id = list_current_review_tasks(database, limit=1).items[0].current_event.event_id
    return database, task.task_id, opened_event_id


def _bind(monkeypatch: pytest.MonkeyPatch, state_directory: Path) -> None:
    monkeypatch.setattr(
        adapter,
        "scope_bindings",
        lambda _scope: (ScopeBinding(ReadScope.PERSONAL, state_directory),),
    )


def test_review_task_cli_claim_decide_history_and_exact_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, task_id, opened_event_id = _published_task(tmp_path)
    _bind(monkeypatch, tmp_path)

    shown = adapter.review_task_show_payload(task_id, "personal")
    assert shown["status"] == "ready"
    claim = adapter.review_task_claim_payload(
        task_id,
        "personal",
        expected_event_id=opened_event_id,
        actor="victor",
        note="inicio",
        clock_ns=lambda: 2_000,
    )
    assert claim["status"] == "complete"
    assert claim["idempotent"] is False
    claimed_event_id = claim["event"]["event_id"]
    retry = adapter.review_task_claim_payload(
        task_id,
        "personal",
        expected_event_id=opened_event_id,
        actor="victor",
        note="inicio",
        clock_ns=lambda: 9_999,
    )
    assert retry["status"] == "complete"
    assert retry["idempotent"] is True

    decided = adapter.review_task_decide_payload(
        task_id,
        "personal",
        expected_event_id=claimed_event_id,
        decision="resolved",
        decision_scope="until-source-change",
        actor="victor",
        note="evidencia confirmada",
        clock_ns=lambda: 3_000,
    )
    assert decided["status"] == "complete"
    assert decided["idempotent"] is False
    decision = decided["event"]["decision"]
    assert decision["scope"] == "until-source-change"
    retry_decision = adapter.review_task_decide_payload(
        task_id,
        "personal",
        expected_event_id=claimed_event_id,
        decision="resolved",
        decision_scope="until-source-change",
        actor="victor",
        note="evidencia confirmada",
        clock_ns=lambda: 9_999,
    )
    assert retry_decision["status"] == "complete"
    assert retry_decision["idempotent"] is True

    history = adapter.review_task_history_payload(task_id, "personal")
    assert history["status"] == "ready"
    assert [event["to_state"] for event in history["events"]] == [
        "open",
        "in_review",
        "resolved",
    ]


def test_review_task_cli_changed_command_is_not_mistaken_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, task_id, opened_event_id = _published_task(tmp_path)
    _bind(monkeypatch, tmp_path)
    first = adapter.review_task_claim_payload(
        task_id,
        "personal",
        expected_event_id=opened_event_id,
        actor="victor",
        note="primera nota",
        clock_ns=lambda: 2_000,
    )
    assert first["status"] == "complete"
    changed = adapter.review_task_claim_payload(
        task_id,
        "personal",
        expected_event_id=opened_event_id,
        actor="victor",
        note="otra nota",
        clock_ns=lambda: 3_000,
    )
    assert changed["status"] == "snapshot_changed"


@pytest.mark.parametrize(
    "payload",
    (
        lambda task_id: adapter.review_task_show_payload(task_id, "personal"),
        lambda task_id: adapter.review_task_history_payload(task_id, "personal"),
        lambda task_id: adapter.review_task_claim_payload(
            task_id,
            "personal",
            expected_event_id="missing-event",
            actor="victor",
            note=None,
        ),
        lambda task_id: adapter.review_task_decide_payload(
            task_id,
            "personal",
            expected_event_id="missing-event",
            decision="dismissed",
            decision_scope="permanent",
            actor="victor",
            note=None,
        ),
    ),
)
def test_review_task_cli_absent_state_fails_closed_without_creating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload,
) -> None:
    missing = tmp_path / "absent"
    _bind(monkeypatch, missing)
    result = payload("task-missing")
    assert result["status"] == "unavailable"
    assert result["exit_code"] != 0
    assert not missing.exists()
