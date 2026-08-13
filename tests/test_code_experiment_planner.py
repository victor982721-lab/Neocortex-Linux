from __future__ import annotations

import json
from dataclasses import replace

import pytest

from _04_Nucleo_Operativo.code_experiment_planner import (
    CODE_EXPERIMENT_MAX_PROPOSALS,
    CODE_EXPERIMENT_TEMPLATES,
    experiment_template,
    experiment_template_registry_fingerprint,
    parse_code_experiment_plan_payload,
    plan_code_experiments,
)
from _04_Nucleo_Operativo.code_invariant_assurance_analysis import (
    analyze_code_invariant_assurance,
    invariant_assurance_questions,
)
from _04_Nucleo_Operativo.code_invariant_contracts import INVARIANT_SPECS, RUNTIME_SCENARIOS
from _04_Nucleo_Operativo.external_deep_coverage import PYTEST_COVERAGE_PROVIDER_ID
from _04_Nucleo_Operativo.external_evidence_models import (
    ExternalProviderEvidence,
    ExternalProviderRelation,
    external_relation_identity,
)


def _provider() -> ExternalProviderEvidence:
    relations = tuple(
        ExternalProviderRelation(
            external_relation_identity(
                PYTEST_COVERAGE_PROVIDER_ID,
                relation_kind="declared_test_outcome",
                source_kind="contract",
                source_key=f"pytest-nodeid:{scenario.test_nodeid}",
                target_kind="run",
                target_key="coverage-run:fixture",
            ),
            "declared_test_outcome",
            "contract",
            f"pytest-nodeid:{scenario.test_nodeid}",
            "run",
            "coverage-run:fixture",
            confidence=1.0,
            metadata={
                "nodeid": scenario.test_nodeid,
                "outcome": "passed",
                "claim_scope": "exact_selected_test_execution_outcome",
                "assertion_or_invariant_proof": False,
                "measurement_scope_signature": "fixture",
            },
        )
        for scenario in RUNTIME_SCENARIOS
    )
    return ExternalProviderEvidence(
        PYTEST_COVERAGE_PROVIDER_ID,
        11,
        11,
        "ready",
        None,
        relations=relations,
    )


def _invariant_questions():
    analysis = analyze_code_invariant_assurance(
        {PYTEST_COVERAGE_PROVIDER_ID: _provider()},
        snapshot_id="snapshot-fixture",
        snapshot_freshness="current",
    )
    return analysis.question_specs, analysis.question_evaluations


def test_registry_is_canonical_non_mutating_and_bounded() -> None:
    assert tuple(item.template_id for item in CODE_EXPERIMENT_TEMPLATES) == tuple(
        sorted(item.template_id for item in CODE_EXPERIMENT_TEMPLATES)
    )
    action_ids = [action for item in CODE_EXPERIMENT_TEMPLATES for action in item.action_ids]
    assert len(action_ids) == len(set(action_ids))
    assert all(item.authority == "advisory" for item in CODE_EXPERIMENT_TEMPLATES)
    assert all(item.mutation_authority is False for item in CODE_EXPERIMENT_TEMPLATES)
    assert all(1 <= item.timeout_seconds <= 900 for item in CODE_EXPERIMENT_TEMPLATES)
    assert experiment_template("structure.static_characterization").cost_tier == "metadata"
    assert experiment_template_registry_fingerprint().startswith(
        "code-experiment-template-registry-v1:xxh3_128:"
    )


def test_cheapest_registered_experiment_is_selected_without_change_authority() -> None:
    specs, evaluations = _invariant_questions()

    result = plan_code_experiments(specs, evaluations)

    assert result.status == "ready"
    assert result.experiment_required_count == len(evaluations)
    assert result.planned_count == len(evaluations)
    assert result.executable_count == len(evaluations)
    assert result.registry_gap_count == 0
    assert {item.template_id for item in result.proposals} == {
        "analyzer.registered_invariant_scenarios"
    }
    assert all(item.runner_kind == "trusted_deep_declared_scenarios" for item in result.proposals)
    assert all(item.mutation_authority is False for item in result.proposals)
    assert all(
        "result_digest_and_environment_receipt_are_recorded" in item.acceptance_gates
        for item in result.proposals
    )
    assert parse_code_experiment_plan_payload(json.loads(json.dumps(result.as_payload()))) == result


def test_missing_runtime_provider_plans_the_allowlisted_experiment_instead_of_deadlocking() -> None:
    analysis = analyze_code_invariant_assurance(
        {},
        snapshot_id="snapshot-fixture",
        snapshot_freshness="publication_only",
    )
    specs, evaluations = invariant_assurance_questions(analysis, rank_offset=0)

    result = plan_code_experiments(specs, evaluations)

    assert result.status == "ready"
    assert result.experiment_required_count == len(INVARIANT_SPECS)
    assert result.executable_count == len(INVARIANT_SPECS)
    assert result.registry_gap_count == 0
    assert all(
        proposal.template_id == "analyzer.registered_invariant_scenarios"
        and proposal.runner_kind == "trusted_deep_declared_scenarios"
        for proposal in result.proposals
    )


def test_unregistered_action_is_preserved_as_registry_gap_not_shell_text() -> None:
    specs, evaluations = _invariant_questions()
    evaluation = replace(
        evaluations[0],
        next_action_ids=("unregistered_delete_everything_now",),
    )
    # This modified object no longer validates against the original spec and
    # must be rejected rather than treated as an executable string.
    with pytest.raises(ValueError, match="readiness"):
        plan_code_experiments(specs, (evaluation,))


def test_question_owned_unknown_experiment_becomes_explicit_registry_gap() -> None:
    specs, evaluations = _invariant_questions()
    spec = replace(
        specs[0],
        next_actions=(
            replace(
                specs[0].next_actions[-1],
                action_id="unknown_but_question_owned_experiment",
            ),
        ),
    )
    evaluation = replace(
        evaluations[0],
        question_spec_fingerprint=__import__(
            "_04_Nucleo_Operativo.code_analysis_epistemics",
            fromlist=["analysis_question_spec_fingerprint"],
        ).analysis_question_spec_fingerprint(spec),
        next_action_ids=("unknown_but_question_owned_experiment",),
    )

    result = plan_code_experiments((spec,), (evaluation,))

    assert result.status == "partial"
    assert result.registry_gap_count == 1
    proposal = result.proposals[0]
    assert proposal.planning_status == "registry_gap"
    assert proposal.template_id is None
    assert proposal.runner_kind is None
    assert proposal.acceptance_gates == ()


def test_no_experiment_required_produces_explicit_empty_plan() -> None:
    specs, evaluations = _invariant_questions()
    # An empty, valid evaluation set means no unresolved question requested an
    # experiment in this bounded projection.
    result = plan_code_experiments((), ())

    assert result.status == "not_required"
    assert result.reason == "no_evaluation_requires_an_experiment"
    assert result.proposals == ()
    assert result.source_evaluation_count == 0
    del specs, evaluations


def test_evaluation_bound_abstains_without_partial_proposals() -> None:
    specs, evaluations = _invariant_questions()
    repeated = tuple(
        replace(
            evaluations[0],
            evaluation_id=f"evaluation:{index}",
            rank=index + 1,
        )
        for index in range(CODE_EXPERIMENT_MAX_PROPOSALS + 1)
    )

    result = plan_code_experiments(specs, repeated)

    assert result.status == "abstained"
    assert result.reason == "evaluation_bound_exceeded"
    assert result.proposals == ()


def test_wire_and_public_constructor_reject_execution_smuggling() -> None:
    specs, evaluations = _invariant_questions()
    result = plan_code_experiments(specs, evaluations)
    proposal = result.proposals[0]

    with pytest.raises(ValueError, match="derived from its template"):
        replace(proposal, acceptance_gates=("delete_and_commit_production",))

    payload = json.loads(json.dumps(result.as_payload()))
    payload["mutation_authority"] = True
    with pytest.raises(ValueError, match="advisory and non-mutating"):
        parse_code_experiment_plan_payload(payload)

    payload = json.loads(json.dumps(result.as_payload()))
    payload["proposals"][0]["runner_kind"] = "shell"
    with pytest.raises(ValueError, match="derived from its template"):
        parse_code_experiment_plan_payload(payload)
