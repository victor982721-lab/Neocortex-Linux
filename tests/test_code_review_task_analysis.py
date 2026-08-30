from __future__ import annotations

from dataclasses import asdict, replace

import pytest

from neocortex.code.code_analysis_epistemics import (
    AnalysisQuestionEvaluation,
    analysis_identity,
)
from neocortex.code.code_review_task_analysis import (
    CODE_REVIEW_TASK_PROTOCOL_ANALYSIS_SCHEMA,
    CODE_REVIEW_TASK_PROTOCOL_POLICY,
    FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION,
    FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION_ID,
    FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION_VERSION,
    FRAMEWORK_REVIEW_TASK_PROTOCOL_SUBJECT_KEY,
    build_framework_review_task_protocol_analysis,
    framework_review_task_questions,
)


def _facts_by_record_kind(
    evaluation: AnalysisQuestionEvaluation,
) -> dict[str, dict[str, object]]:
    return {
        item.source_record_kind: {fact.name: fact.value for fact in item.facts}
        for item in evaluation.evidence
    }


def test_framework_review_task_protocol_constructor_fails_closed() -> None:
    analysis = build_framework_review_task_protocol_analysis()

    with pytest.raises(
        ValueError,
        match="Framework ReviewTask protocol analysis fields are incompatible",
    ):
        replace(analysis, state_owner_id="semantic")

    with pytest.raises(
        ValueError,
        match="Framework ReviewTask protocol analysis fields are incompatible",
    ):
        replace(analysis, superseded_repository_only=False)

    with pytest.raises(
        ValueError,
        match="Framework ReviewTask protocol analysis identity is invalid",
    ):
        replace(analysis, analysis_id="code-review-task-protocol-analysis-v1:invalid")


def test_framework_review_task_protocol_payload_and_digest_are_canonical() -> None:
    analysis = build_framework_review_task_protocol_analysis()
    repeated = build_framework_review_task_protocol_analysis()
    payload = analysis.as_payload()
    identity_payload = {
        key: value for key, value in asdict(analysis).items() if key != "analysis_id"
    }

    assert repeated == analysis
    assert payload == {"schema": CODE_REVIEW_TASK_PROTOCOL_ANALYSIS_SCHEMA, **asdict(analysis)}
    assert analysis.analysis_id == analysis_identity(
        "code-review-task-protocol-analysis-v1",
        identity_payload,
    )
    assert analysis.policy_id == CODE_REVIEW_TASK_PROTOCOL_POLICY
    assert analysis.logical_owner_id == "review"
    assert analysis.state_owner_id == "framework"
    assert analysis.state_store_id == "sqlite:framework.sqlite3"
    assert analysis.database_name == "framework.sqlite3"
    assert analysis.framework_schema_version == 22
    assert analysis.review_task_contract_version == 1
    assert analysis.public_adapter_module == "neocortex.api.cli.review_task"
    assert analysis.public_port_module == "neocortex.workflow.review.value_review_port"
    assert analysis.terminal_states == ("dismissed", "resolved")
    assert analysis.terminal_decisions_require_human is True
    assert analysis.superseded_repository_only is True
    assert analysis.authority == "advisory"
    assert analysis.mutation_authority is False


def test_framework_review_task_question_requires_isolated_experiment() -> None:
    specs, evaluations = framework_review_task_questions(
        snapshot_id="snapshot:test-review-task",
        snapshot_freshness="current",
        rank=1,
    )
    assert specs == (FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION,)
    assert len(evaluations) == 1
    evaluation = evaluations[0]

    assert evaluation.question_id == FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION_ID
    assert evaluation.question_version == FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION_VERSION
    assert evaluation.subject.subject_kind == "contract"
    assert evaluation.subject.subject_key == FRAMEWORK_REVIEW_TASK_PROTOCOL_SUBJECT_KEY
    assert evaluation.observation_status == "confirmed"
    assert evaluation.question_readiness == "ready"
    assert evaluation.decision_readiness == "experiment_required"
    assert evaluation.counterevidence_status == "not_evaluated"
    assert evaluation.authority == "advisory"
    assert evaluation.mutation_authority is False

    requirements = {item.requirement_id: item for item in evaluation.requirements}
    assert requirements[
        "framework_review_task_owner_store_contract_resolved"
    ].status == "satisfied"
    assert requirements[
        "framework_review_task_public_protocol_contract_resolved"
    ].status == "satisfied"
    assert requirements[
        "review_task_stale_head_and_fault_counterevidence_evaluated"
    ].status == "not_evaluated"
    assert requirements["isolated_review_task_protocol_experiment_result"].status == "missing"

    facts = _facts_by_record_kind(evaluation)
    assert facts == {
        "framework_review_task_owner_store_contract": {
            "logical_owner_id": "review",
            "state_owner_id": "framework",
            "state_store_id": "sqlite:framework.sqlite3",
            "database_name": "framework.sqlite3",
            "framework_schema_version": 22,
            "review_task_contract_version": 1,
        },
        "framework_review_task_public_protocol_contract": {
            "public_adapter_module": "neocortex.api.cli.review_task",
            "public_port_module": "neocortex.workflow.review.value_review_port",
            "terminal_states": "dismissed,resolved",
            "terminal_decisions_require_human": True,
            "superseded_repository_only": True,
        },
    }
