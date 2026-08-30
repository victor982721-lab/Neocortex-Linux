from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from neocortex.code.code_analysis_epistemics import (
    validate_analysis_question_set,
)
from neocortex.code.code_experiment_planner import plan_code_experiments
from neocortex.code.code_security_dependency_questions import (
    DEPENDENCY_EVIDENCE_QUESTION,
    SECURITY_EVIDENCE_QUESTION,
    security_dependency_questions,
)
from tests.test_code_supply_chain_analysis import (
    _database,
    _read,
)


def _requirements(evaluation) -> dict[str, str]:
    return {item.requirement_id: item.status for item in evaluation.requirements}


def test_security_and_dependency_questions_link_complete_provider_receipts(
    tmp_path: Path,
) -> None:
    analysis = _read(_database(tmp_path, findings=False), 1)

    specs, evaluations = security_dependency_questions(
        analysis,
        snapshot_id="snapshot:fixture",
        snapshot_freshness="current",
        rank_offset=5,
    )

    assert specs == (SECURITY_EVIDENCE_QUESTION, DEPENDENCY_EVIDENCE_QUESTION)
    assert tuple(item.rank for item in evaluations) == (6, 7)
    assert all(item.observation_status == "confirmed" for item in evaluations)
    assert all(item.inference_status == "abstained" for item in evaluations)
    assert all(item.decision_readiness == "experiment_required" for item in evaluations)
    assert all(item.decision is None for item in evaluations)
    assert (
        _requirements(evaluations[0])["semgrep_invariant_provider_and_gate_evaluated"]
        == "satisfied"
    )
    assert (
        _requirements(evaluations[0])["current_vulnerability_provider_and_gates_evaluated"]
        == "satisfied"
    )
    assert (
        _requirements(evaluations[1])["dependency_declaration_provider_and_gate_evaluated"]
        == "satisfied"
    )
    assert (
        _requirements(evaluations[1])["installed_package_and_license_gates_evaluated"]
        == "satisfied"
    )
    validate_analysis_question_set(
        specs,
        tuple(replace(item, rank=index) for index, item in enumerate(evaluations, start=1)),
    )


def test_missing_security_provider_is_observed_but_cannot_satisfy_decision_evidence(
    tmp_path: Path,
) -> None:
    providers = (
        "semgrep-neocortex-invariants",
        "deptry-project-dependencies",
        "installed-package-inventory",
    )
    analysis = _read(_database(tmp_path, findings=False, providers=providers), 1)

    _, evaluations = security_dependency_questions(
        analysis,
        snapshot_id="snapshot:fixture",
        snapshot_freshness="publication_only",
        rank_offset=0,
    )

    security = evaluations[0]
    assert security.observation_status == "abstained"
    assert security.question_readiness == "abstained"
    assert security.decision_readiness == "abstained"
    assert (
        _requirements(security)["current_vulnerability_provider_and_gates_evaluated"] == "missing"
    )
    coverage = security.evidence[0]
    assert next(item.value for item in coverage.facts if item.name == "ready_provider_count") == 1
    assert security.authority == "advisory"
    assert security.mutation_authority is False


def test_failed_advisory_gates_remain_evidence_not_a_change_decision(tmp_path: Path) -> None:
    analysis = _read(_database(tmp_path, findings=True), 1)

    _, evaluations = security_dependency_questions(
        analysis,
        snapshot_id="snapshot:fixture",
        snapshot_freshness="current",
        rank_offset=0,
    )

    for evaluation in evaluations:
        failed_counts = tuple(
            fact.value
            for evidence in evaluation.evidence
            for fact in evidence.facts
            if fact.name == "failed_gate_count"
        )
        assert any(isinstance(value, int) and value > 0 for value in failed_counts)
        assert evaluation.decision is None
        assert evaluation.decision_readiness == "experiment_required"


def test_capture_local_supply_runs_keep_portable_experiment_identity(tmp_path: Path) -> None:
    analysis = _read(_database(tmp_path, findings=False), 1)
    specs, first = security_dependency_questions(
        analysis,
        snapshot_id="snapshot:first-capture",
        snapshot_freshness="current",
        rank_offset=0,
    )
    replay = replace(
        analysis,
        analysis_run_id=2,
        providers=tuple(
            replace(
                provider,
                execution="cache_replay" if index % 2 else "full",
                tool_run_id=100 + index,
                source_tool_run_id=50 + index,
                source=f"capture:{index}",
                observed_date="2026-08-15",
            )
            for index, provider in enumerate(analysis.providers)
        ),
        digest=replace(
            analysis.digest,
            xxh3_128="f" * 32,
            xxh3_64_guard="e" * 16,
        ),
    )
    _, second = security_dependency_questions(
        replay,
        snapshot_id="snapshot:replay-capture",
        snapshot_freshness="current",
        rank_offset=0,
    )

    first_plan = plan_code_experiments(specs, first)
    second_plan = plan_code_experiments(specs, second)

    assert tuple(item.evaluation_id for item in first) != tuple(
        item.evaluation_id for item in second
    )
    assert tuple(item.proposal_id for item in first_plan.proposals) == tuple(
        item.proposal_id for item in second_plan.proposals
    )

    changed = replace(
        replay,
        providers=tuple(
            replace(provider, findings=provider.findings + 1)
            if provider.provider_id == "semgrep-neocortex-invariants"
            else provider
            for provider in replay.providers
        ),
    )
    _, changed_evaluations = security_dependency_questions(
        changed,
        snapshot_id="snapshot:semantic-change",
        snapshot_freshness="current",
        rank_offset=0,
    )
    changed_plan = plan_code_experiments(specs, changed_evaluations)
    first_by_question = {item.question_id: item.proposal_id for item in first_plan.proposals}
    changed_by_question = {item.question_id: item.proposal_id for item in changed_plan.proposals}
    assert (
        changed_by_question[SECURITY_EVIDENCE_QUESTION.question_id]
        != first_by_question[SECURITY_EVIDENCE_QUESTION.question_id]
    )
    assert (
        changed_by_question[DEPENDENCY_EVIDENCE_QUESTION.question_id]
        == first_by_question[DEPENDENCY_EVIDENCE_QUESTION.question_id]
    )
