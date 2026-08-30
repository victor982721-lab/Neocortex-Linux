from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.code.code_experiment_planner import experiment_template
from neocortex.code.code_invariant_contracts import runtime_scenario
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
from neocortex.cli import entrypoint


SCENARIO_ID = "framework.review_task_protocol_acceptance"
PUBLIC_JOURNEY_NODEID = (
    "tests/test_code_framework_review_task_experiments.py::"
    "test_public_entrypoint_review_task_journey_preserves_cas_idempotency_"
    "and_human_authority"
)
SYNTHETIC_UNAUTHENTICATED_ACTOR = "synthetic-unauthenticated-reviewer"


def _publish_framework_task(state_directory: Path) -> tuple[str, str]:
    database = state_directory / "framework.sqlite3"
    state_directory.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as connection:
        initialize_framework_schema(connection, lambda: None)
    resource = ResourceRef(
        resource_id="resource-framework-public-journey",
        source_kind="self-analysis",
        owner="framework",
        physical_identity=PhysicalIdentityRef(
            scheme="fixture-identity",
            value="framework-public-journey:1",
            identity_version=1,
        ),
        current_path="/fixture/framework-public-journey.txt",
    )
    revision = RevisionRef(
        resource_id=resource.resource_id,
        revision_id="revision-framework-public-journey",
        producer="framework-public-journey-fixture",
        processing_signature="framework-public-journey-v1",
        generation=1,
        state=RevisionState.CURRENT,
    )
    source = ReviewTaskInput(
        input_id="input-framework-public-journey",
        fingerprint_algorithm="sha256",
        fingerprint="a" * 64,
        resource=resource,
        revision=revision,
    )
    task = ReviewTaskDraft(
        task_id="task-framework-public-journey",
        logical_key="logical-framework-public-journey",
        task_version=1,
        task_type="code-review",
        scope="framework",
        source_kind="code-analysis-question",
        source_input_id=source.input_id,
        snapshot=CanonicalJsonObject.from_mapping({"question": "review-task-protocol"}),
        evidence=(
            EvidenceRef(
                evidence_id="evidence-framework-public-journey",
                resource_id=resource.resource_id,
                revision_id=revision.revision_id,
                method=EvidenceMethod.STRUCTURAL,
                identifiers=(("fixture", "framework-public-journey"),),
            ),
        ),
        reason_code="human-decision-required",
        uncertainty_detail=CanonicalJsonObject.from_mapping(
            {"actor_authentication": "not_provided_by_this_protocol"}
        ),
        impact=0.8,
        uncertainty=0.5,
        irreversibility=0.25,
        suggestions=("inspect the bounded evidence",),
        supersedes_task_id=None,
        created_ns=100,
    )
    fence = ReviewTaskSourceFence.create(
        scope="framework",
        task_type="code-review",
        selector_signature="framework-public-journey-selector-v1",
        source_snapshot={"generation": 1, "owner": "framework"},
    )
    publication = ReviewTaskPublication(
        batch_id="batch-framework-public-journey",
        batch_key="batch-key-framework-public-journey",
        fence=fence,
        cursor_before=None,
        cursor_after=None,
        inputs=(source,),
        tasks=(task,),
        coverage=ReviewTaskCoverage.COMPLETE,
        producer_signature="framework-public-journey-v1",
        confirmed_ns=1_000,
    )
    publish_review_task_page(database, publication, expected_progress_revision=None)
    opened_event_id = list_current_review_tasks(database, limit=1).items[0].current_event.event_id
    return task.task_id, opened_event_id


def _public_json(
    capsys: pytest.CaptureFixture[str],
    *arguments: str,
) -> dict[str, object]:
    exit_code = entrypoint(("review", "task", *arguments, "--scope", "framework", "--json"))
    captured = capsys.readouterr()
    assert captured.err == ""
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    assert payload["scope"] == "framework"
    assert payload["exit_code"] == 0
    return payload


def test_public_entrypoint_review_task_journey_preserves_cas_idempotency_and_human_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    xdg_state_home = tmp_path / "state"
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    state_directory = xdg_state_home / "Neocortex" / "self-analysis"
    task_id, opened_event_id = _publish_framework_task(state_directory)

    shown = _public_json(capsys, "show", task_id)
    assert shown["status"] == "ready"
    assert shown["record"]["state"] == "open"  # type: ignore[index]

    claim_arguments = (
        "claim",
        task_id,
        "--expected-event-id",
        opened_event_id,
        "--actor",
        SYNTHETIC_UNAUTHENTICATED_ACTOR,
        "--note",
        "synthetic bounded claim",
    )
    claimed = _public_json(capsys, *claim_arguments)
    assert claimed["status"] == "complete"
    assert claimed["idempotent"] is False
    claimed_event = claimed["event"]
    assert isinstance(claimed_event, dict)
    assert claimed_event["actor_kind"] == "human"
    assert claimed_event["actor_id"] == SYNTHETIC_UNAUTHENTICATED_ACTOR
    claimed_event_id = claimed_event["event_id"]

    claim_retry = _public_json(capsys, *claim_arguments)
    assert claim_retry["status"] == "complete"
    assert claim_retry["idempotent"] is True
    assert claim_retry["event"] == claimed_event

    decision_arguments = (
        "decide",
        task_id,
        "--expected-event-id",
        str(claimed_event_id),
        "--decision",
        "resolved",
        "--decision-scope",
        "until-source-change",
        "--actor",
        SYNTHETIC_UNAUTHENTICATED_ACTOR,
        "--note",
        "synthetic bounded decision",
    )
    decided = _public_json(capsys, *decision_arguments)
    assert decided["status"] == "complete"
    assert decided["idempotent"] is False
    decided_event = decided["event"]
    assert isinstance(decided_event, dict)
    assert decided_event["actor_kind"] == "human"
    assert decided_event["actor_id"] == SYNTHETIC_UNAUTHENTICATED_ACTOR
    assert decided_event["decision"]["scope"] == "until-source-change"  # type: ignore[index]

    decision_retry = _public_json(capsys, *decision_arguments)
    assert decision_retry["status"] == "complete"
    assert decision_retry["idempotent"] is True
    assert decision_retry["event"] == decided_event

    history = _public_json(capsys, "history", task_id)
    assert history["status"] == "ready"
    events = history["events"]
    assert isinstance(events, list)
    assert [event["to_state"] for event in events] == ["open", "in_review", "resolved"]
    assert [event["actor_kind"] for event in events[1:]] == ["human", "human"]
    assert [event["actor_id"] for event in events[1:]] == [
        SYNTHETIC_UNAUTHENTICATED_ACTOR,
        SYNTHETIC_UNAUTHENTICATED_ACTOR,
    ]
    assert state_directory.is_relative_to(tmp_path)


def test_framework_review_task_scenario_and_template_are_exact_and_non_mutating() -> None:
    scenario = runtime_scenario(SCENARIO_ID)
    template = experiment_template(SCENARIO_ID)
    gate_nodeids = {
        nodeid for gate in scenario.gate_specs for nodeid in gate.test_nodeids
    }

    assert scenario.version == "v1"
    assert scenario.scenario_kind == "state_fixture"
    assert scenario.isolation == "pytest_tmp_path"
    assert len(scenario.test_nodeids) == 8
    assert PUBLIC_JOURNEY_NODEID in scenario.test_nodeids
    assert gate_nodeids == set(scenario.test_nodeids)
    assert all(Path(nodeid.partition("::")[0]).is_file() for nodeid in scenario.test_nodeids)
    assert template.version == "v1"
    assert template.executable is True
    assert template.max_items == len(scenario.test_nodeids)
    assert template.scenario_ids == (scenario.scenario_id,)
    assert template.acceptance_gates == tuple(gate.gate_id for gate in scenario.gate_specs)
    assert template.authority == "advisory"
    assert template.mutation_authority is False
    assert "process_death" not in scenario.scenario_kind
    assert "not_identity_authenticated" in scenario.limitation
