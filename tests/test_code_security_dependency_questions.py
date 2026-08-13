from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from _04_Nucleo_Operativo.code_analysis_epistemics import (
    validate_analysis_question_set,
)
from _04_Nucleo_Operativo.code_security_dependency_questions import (
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
    assert security.question_readiness == "ready"
    assert security.decision_readiness == "experiment_required"
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
