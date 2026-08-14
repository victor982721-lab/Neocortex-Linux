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
from _04_Nucleo_Operativo.code_invariant_contracts import (
    EXPERIMENT_SCENARIO_IDS,
    INVARIANT_SPECS,
    INVARIANT_RUNTIME_SCENARIOS,
)
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
                source_key=f"pytest-nodeid:{nodeid}",
                target_kind="run",
                target_key="coverage-run:fixture",
            ),
            "declared_test_outcome",
            "contract",
            f"pytest-nodeid:{nodeid}",
            "run",
            "coverage-run:fixture",
            confidence=1.0,
            metadata={
                "nodeid": nodeid,
                "outcome": "passed",
                "claim_scope": "exact_selected_test_execution_outcome",
                "assertion_or_invariant_proof": False,
                "measurement_scope_signature": "fixture",
            },
        )
        for scenario in INVARIANT_RUNTIME_SCENARIOS
        for nodeid in scenario.test_nodeids
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
        "code-experiment-template-registry-v4:xxh3_128:"
    )
    executable = tuple(item for item in CODE_EXPERIMENT_TEMPLATES if item.executable)
    assert {scenario for item in executable for scenario in item.scenario_ids} == set(
        EXPERIMENT_SCENARIO_IDS
    )
    assert all(len(item.scenario_ids) == 1 for item in executable)
    assert experiment_template("analyzer.registered_invariant_scenarios").executable is False
    semantic = experiment_template("state.semantic_process_death_recovery")
    assert semantic.executable is True
    assert semantic.applies_to(
        question_id="state.text_semantic_published_projection_is_aligned",
        subject_key="workflow:text-to-semantic-published-projection",
    )
    capability = experiment_template("capability.public_route_acceptance")
    assert capability.applies_to(
        question_id="capability.route_reaches_user_visible_outcome",
        subject_key="capability:route:text",
    )
    assert not capability.applies_to(
        question_id="capability.route_reaches_user_visible_outcome",
        subject_key="capability:route:pdf",
    )
    assert not capability.applies_to(
        question_id="state.declared_workflow_sql_matches_implementation",
        subject_key="capability:route:text",
    )
    schema = experiment_template("evolution.code_schema_upgrade_matrix")
    assert schema.executable is True
    assert schema.scenario_ids == ("evolution.code_schema_upgrade_matrix",)
    assert schema.applies_to(
        question_id="evolution.code_owner_schema_requires_migration_review",
        subject_key="code-owner-schema-subject-v1:fixture",
    )
    assert not schema.applies_to(
        question_id="evolution.change_surface_requires_review",
        subject_key="code-owner-schema-subject-v1:fixture",
    )
    architecture = experiment_template("architecture.declared_import_contract_acceptance")
    assert architecture.executable is True
    assert architecture.scenario_ids == ("architecture.declared_import_contract_acceptance",)
    assert architecture.applies_to(
        question_id="architecture.declared_import_contracts_are_evaluated",
        subject_key="architecture:contract:fixture",
    )
    assert not architecture.applies_to(
        question_id="architecture.static_import_graph_is_comparably_observed",
        subject_key="architecture:run:fixture",
    )
    assert all(item.acceptance_gates for item in executable)


def test_unimplemented_independent_invariant_experiment_remains_manual() -> None:
    specs, evaluations = _invariant_questions()

    result = plan_code_experiments(specs, evaluations)

    assert result.status == "ready"
    assert result.experiment_required_count == len(evaluations)
    assert result.planned_count == len(evaluations)
    assert result.executable_count == 0
    assert result.registry_gap_count == 0
    assert {item.template_id for item in result.proposals} == {
        "analyzer.registered_invariant_scenarios"
    }
    assert all(item.runner_kind == "none" for item in result.proposals)
    assert all(item.mutation_authority is False for item in result.proposals)
    assert all(
        "result_digest_and_environment_receipt_are_recorded" in item.acceptance_gates
        for item in result.proposals
    )
    assert parse_code_experiment_plan_payload(json.loads(json.dumps(result.as_payload()))) == result


def test_missing_runtime_provider_plans_without_claiming_an_independent_runner() -> None:
    analysis = analyze_code_invariant_assurance(
        {},
        snapshot_id="snapshot-fixture",
        snapshot_freshness="publication_only",
    )
    specs, evaluations = invariant_assurance_questions(analysis, rank_offset=0)

    result = plan_code_experiments(specs, evaluations)

    assert result.status == "ready"
    assert result.experiment_required_count == len(INVARIANT_SPECS)
    assert result.executable_count == 0
    assert result.registry_gap_count == 0
    assert all(
        proposal.template_id == "analyzer.registered_invariant_scenarios"
        and proposal.runner_kind == "none"
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


def test_proposal_identity_survives_an_exact_replay_evaluation_identity() -> None:
    specs, evaluations = _invariant_questions()
    original = plan_code_experiments(specs, evaluations)
    replayed = plan_code_experiments(
        specs,
        (replace(evaluations[0], evaluation_id="evaluation:exact-replay"),),
    )
    original_proposal = next(
        item for item in original.proposals if item.evaluation_id == evaluations[0].evaluation_id
    )

    assert replayed.proposals[0].evaluation_id == "evaluation:exact-replay"
    assert replayed.proposals[0].proposal_id == original_proposal.proposal_id


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
