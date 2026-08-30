"""Regressions for fail-closed Code assurance and invariant evidence."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from neocortex.code.code_assurance_analysis import (
    ASSURANCE_AVAILABILITY_QUESTION,
    CODE_ASSURANCE_SCHEMA,
    analyze_code_assurance,
    assurance_questions,
    parse_code_assurance_payload,
)
from neocortex.code.code_coverage_analysis import (
    CodeCoverageAnalysis,
    CoverageTotals,
    analyze_code_coverage,
)
from neocortex.code.external_evidence_models import (
    ExternalProviderEvidence,
    ExternalProviderMetric,
    ExternalProviderRelation,
)
from neocortex.code.external_mutation_cosmic_ray import (
    COSMIC_RAY_MUTATION_PROVIDER_ID,
    COSMIC_RAY_MUTATION_PROVIDER_SCHEMA,
)
from tests.test_code_coverage_analysis import SYMBOL, TEST_NODEID, _context, _evidence

SNAPSHOT_ID = "code-snapshot:v1:fixture"


def _coverage() -> CodeCoverageAnalysis:
    return analyze_code_coverage(_evidence(), database="fixture", analysis_run_id=7)


def _mutation_provider(
    target_symbol: str,
    *,
    generated: int = 2,
    selected: int = 2,
    completed: int = 2,
    killed: int = 1,
    survived: int = 1,
    timed_out: int = 0,
    incompetent: int = 0,
    measurement_complete: bool = True,
    selection_truncated: bool = False,
    extra_metadata: dict[str, object] | None = None,
) -> ExternalProviderEvidence:
    scope = "mutation-scope:v1"
    selectors = ("tests/test_mod.py::test_target",)
    metadata: dict[str, object] = {
        "provider_schema": COSMIC_RAY_MUTATION_PROVIDER_SCHEMA,
        "target_relative_path": "pkg/mod.py",
        "target_symbol": target_symbol,
        "test_selectors": list(selectors),
        "selection_truncated": selection_truncated,
        "measurement_complete": measurement_complete,
        "baseline_passed": True,
        "measurement_scope_signature": scope,
        "mutation_authority": False,
        **(extra_metadata or {}),
    }
    values: tuple[tuple[str, float, str], ...] = (
        ("mutants_generated", float(generated), "count"),
        ("mutants_selected", float(selected), "count"),
        ("mutants_completed", float(completed), "count"),
        ("mutants_killed", float(killed), "count"),
        ("mutants_survived", float(survived), "count"),
        ("mutants_timed_out", float(timed_out), "count"),
        ("mutants_incompetent", float(incompetent), "count"),
        ("mutants_reused", 0.0, "count"),
        ("baseline_passed", 1.0, "boolean"),
        ("measurement_complete", float(measurement_complete), "boolean"),
        ("mutation_score", killed / (killed + survived), "ratio"),
    )
    metrics = tuple(
        ExternalProviderMetric(
            f"mutation-metric:{name}",
            "symbol",
            target_symbol,
            "mutation_testing",
            name,
            value,
            unit,
            metadata=metadata,
        )
        for name, value, unit in values
    )
    common_relation_metadata = {
        "provider_schema": COSMIC_RAY_MUTATION_PROVIDER_SCHEMA,
        "target_relative_path": "pkg/mod.py",
        "target_symbol": target_symbol,
        "test_selectors": list(selectors),
        "measurement_scope_signature": scope,
        "mutation_authority": False,
    }
    relations = (
        ExternalProviderRelation(
            "mutation-relation:target",
            "mutation_targets_symbol",
            "run",
            scope,
            "symbol",
            target_symbol,
            metadata=common_relation_metadata,
        ),
        ExternalProviderRelation(
            "mutation-relation:test",
            "mutation_tested_by",
            "symbol",
            target_symbol,
            "file",
            "tests/test_mod.py",
            metadata={**common_relation_metadata, "selectors": list(selectors)},
        ),
    )
    return ExternalProviderEvidence(
        COSMIC_RAY_MUTATION_PROVIDER_ID,
        11,
        11,
        "ready",
        None,
        (),
        metrics,
        relations,
    )


def _requirement_statuses(analysis) -> dict[str, str]:
    return {
        item.requirement_id: item.status for item in analysis.question_evaluations[0].requirements
    }


def _all_mapping_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for nested in value.values() for key in _all_mapping_keys(nested)}
    if isinstance(value, list):
        return {key for nested in value for key in _all_mapping_keys(nested)}
    return set()


def test_coverage_contexts_publish_executing_tests_without_protection_claim() -> None:
    analysis = analyze_code_assurance(
        _coverage(),
        {},
        snapshot_id=SNAPSHOT_ID,
        snapshot_freshness="current",
    )

    observation = analysis.observations[0]
    evaluation = analysis.question_evaluations[0]
    assert analysis.status == "ready"
    assert observation.subject_key == SYMBOL
    assert observation.test_execution_status == "observed"
    assert observation.coverage_observed is True
    assert observation.executing_tests == (TEST_NODEID,)
    assert observation.assertion_invariant_evidence_status == "not_recorded"
    assert observation.runtime_scenario_evidence_status == "not_recorded"
    assert observation.mutation.status == "not_recorded"
    assert observation.behavioral_assurance_status == "not_established"
    assert evaluation.question_readiness == "ready"
    assert evaluation.decision_readiness == "experiment_required"
    assert evaluation.decision is None
    assert _requirement_statuses(analysis) == {
        "declared_test_execution_observed": "satisfied",
        "symbol_line_execution_context_observed": "satisfied",
        "explicit_assertion_or_invariant_link_observed": "not_evaluated",
        "discriminating_mutation_or_runtime_scenario_result": "missing",
        "negative_control_or_counterevidence_evaluated": "not_evaluated",
    }
    assert analysis.calibration.status == "not_established"
    assert analysis.calibration.coverage_only_subjects == 1
    assert analysis.calibration.behavioral_assurance_claims == 0
    payload = analysis.as_payload()
    assert payload["schema"] == CODE_ASSURANCE_SCHEMA
    assert "protecting_tests" not in _all_mapping_keys(payload)
    assert '"protected"' not in json.dumps(payload, sort_keys=True)


def test_full_coverage_and_assertive_test_name_do_not_invent_asserts_relation() -> None:
    coverage = _coverage()
    nodeid = "tests/test_mod.py::test_asserts_publication_invariant"
    relation = replace(
        coverage.test_relations[0],
        relation_id="relation:assertive-name-negative-control",
        test_key=f"pytest-nodeid:{nodeid}",
        test_nodeids=(nodeid,),
        contexts=(nodeid,),
    )
    full_totals = CoverageTotals(5, 5, 0, 2, 2, 0, 100.0, 100.0)
    symbol = replace(
        coverage.symbols[0],
        totals=full_totals,
        missing_line_ranges=(),
        missing_branch_arcs=(),
        executing_tests=(nodeid,),
    )
    coverage = replace(coverage, symbols=(symbol,), test_relations=(relation,))

    analysis = analyze_code_assurance(
        coverage,
        {},
        snapshot_id=SNAPSHOT_ID,
        snapshot_freshness="current",
    )

    observation = analysis.observations[0]
    assert observation.line_coverage_percent == 100.0
    assert observation.branch_coverage_percent == 100.0
    assert observation.executing_tests == (nodeid,)
    assert observation.assertion_invariant_evidence_status == "not_recorded"
    assert observation.behavioral_assurance_status == "not_established"
    assert (
        _requirement_statuses(analysis)["explicit_assertion_or_invariant_link_observed"]
        == "not_evaluated"
    )


def test_scope_executing_tests_without_exact_relation_are_ignored() -> None:
    coverage = _coverage()
    assert coverage.symbols[0].executing_tests == (TEST_NODEID,)
    coverage = replace(coverage, test_relations=())

    analysis = analyze_code_assurance(
        coverage,
        {},
        snapshot_id=SNAPSHOT_ID,
        snapshot_freshness="current",
    )

    observation = analysis.observations[0]
    evaluation = analysis.question_evaluations[0]
    assert observation.test_execution_status == "observed"
    assert observation.coverage_observed is False
    assert observation.executing_tests == ()
    assert evaluation.question_readiness == "abstained"
    assert evaluation.decision_readiness == "abstained"
    assert _requirement_statuses(analysis)["symbol_line_execution_context_observed"] == "missing"


def test_partial_run_keeps_coverage_observation_separate_from_execution_gate() -> None:
    coverage = analyze_code_coverage(
        _evidence(context=_context(measurement_complete=False)),
        database="fixture",
        analysis_run_id=7,
    )
    assert coverage.status == "ready"
    assert coverage.test_relations

    analysis = analyze_code_assurance(
        coverage,
        {},
        snapshot_id=SNAPSHOT_ID,
        snapshot_freshness="current",
    )

    observation = analysis.observations[0]
    evaluation = analysis.question_evaluations[0]
    assert observation.coverage_observed is True
    assert observation.executing_tests == (TEST_NODEID,)
    assert observation.test_execution_status == "not_evaluated"
    assert evaluation.question_readiness == "abstained"
    assert evaluation.decision_readiness == "abstained"
    statuses = _requirement_statuses(analysis)
    assert statuses["declared_test_execution_observed"] == "not_evaluated"
    assert statuses["symbol_line_execution_context_observed"] == "satisfied"


def test_complete_mutation_is_separate_experiment_and_counterevidence() -> None:
    coverage = _coverage()
    target = coverage.symbols[0].qualified_name
    assert target is not None
    provider = _mutation_provider(
        target,
        extra_metadata={
            # Untyped metadata is data, not an ASSERTS relation.
            "asserts_invariant": True,
            "executing_tests": [TEST_NODEID],
        },
    )

    analysis = analyze_code_assurance(
        coverage,
        {COSMIC_RAY_MUTATION_PROVIDER_ID: provider},
        snapshot_id=SNAPSHOT_ID,
        snapshot_freshness="current",
    )

    observation = analysis.observations[0]
    evaluation = analysis.question_evaluations[0]
    assert observation.mutation.status == "complete"
    assert observation.mutation.selected == 2
    assert observation.mutation.killed == 1
    assert observation.mutation.survived == 1
    assert observation.mutation.discriminating_result_observed is True
    assert observation.mutation.counterevidence_evaluated is True
    assert observation.assertion_invariant_evidence_status == "not_recorded"
    assert observation.runtime_scenario_evidence_status == "not_recorded"
    assert observation.behavioral_assurance_status == "not_established"
    assert evaluation.counterevidence_status == "evaluated"
    assert evaluation.decision_readiness == "experiment_required"
    statuses = _requirement_statuses(analysis)
    assert statuses["discriminating_mutation_or_runtime_scenario_result"] == "satisfied"
    assert statuses["negative_control_or_counterevidence_evaluated"] == "satisfied"
    assert statuses["explicit_assertion_or_invariant_link_observed"] == "not_evaluated"
    assert analysis.calibration.discriminating_mutation_results == 1
    assert analysis.calibration.coverage_only_subjects == 0
    assert analysis.calibration.human_review_ready == 0


def test_truncated_mutation_cannot_satisfy_experiment_or_counterevidence() -> None:
    coverage = _coverage()
    target = coverage.symbols[0].qualified_name
    assert target is not None
    provider = _mutation_provider(
        target,
        generated=3,
        selected=2,
        completed=2,
        selection_truncated=True,
    )

    analysis = analyze_code_assurance(
        coverage,
        {COSMIC_RAY_MUTATION_PROVIDER_ID: provider},
        snapshot_id=SNAPSHOT_ID,
        snapshot_freshness="current",
    )

    observation = analysis.observations[0]
    evaluation = analysis.question_evaluations[0]
    assert observation.mutation.status == "incomplete"
    assert observation.mutation.discriminating_result_observed is False
    assert observation.mutation.counterevidence_evaluated is False
    assert evaluation.counterevidence_status == "not_evaluated"
    statuses = _requirement_statuses(analysis)
    assert statuses["discriminating_mutation_or_runtime_scenario_result"] == "missing"
    assert statuses["negative_control_or_counterevidence_evaluated"] == "not_evaluated"


def test_malformed_mutation_partition_fails_closed_without_losing_coverage() -> None:
    coverage = _coverage()
    target = coverage.symbols[0].qualified_name
    assert target is not None
    provider = _mutation_provider(
        target,
        completed=2,
        killed=2,
        survived=1,
    )

    analysis = analyze_code_assurance(
        coverage,
        {COSMIC_RAY_MUTATION_PROVIDER_ID: provider},
        snapshot_id=SNAPSHOT_ID,
        snapshot_freshness="current",
    )

    observation = analysis.observations[0]
    assert observation.coverage_observed is True
    assert observation.mutation.status == "incompatible"
    assert observation.mutation.reason.startswith("mutation_evidence_incompatible:")
    assert analysis.question_evaluations[0].decision_readiness == "experiment_required"
    statuses = _requirement_statuses(analysis)
    assert statuses["discriminating_mutation_or_runtime_scenario_result"] == "missing"
    assert statuses["negative_control_or_counterevidence_evaluated"] == "not_evaluated"


def test_assurance_v1_rejects_forged_assertion_or_runtime_status() -> None:
    analysis = analyze_code_assurance(
        _coverage(),
        {},
        snapshot_id=SNAPSHOT_ID,
        snapshot_freshness="current",
    )
    observation = analysis.observations[0]

    with pytest.raises(ValueError, match="cannot invent assertion"):
        replace(observation, assertion_invariant_evidence_status="observed")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="cannot invent runtime scenario"):
        replace(observation, runtime_scenario_evidence_status="observed")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="cannot claim behavioral protection"):
        replace(observation, behavioral_assurance_status="established")  # type: ignore[arg-type]


def test_missing_coverage_abstains_without_question_evidence() -> None:
    analysis = analyze_code_assurance(
        None,
        {},
        snapshot_id=SNAPSHOT_ID,
        snapshot_freshness="unknown",
    )

    assert analysis.status == "abstained"
    assert analysis.reason == "coverage_analysis_missing"
    assert analysis.observations == ()
    assert analysis.question_specs == ()
    assert analysis.question_evaluations == ()
    assert analysis.calibration.evaluated_subjects == 0
    assert analysis.calibration.precision_at_k is None

    specs, evaluations = assurance_questions(analysis, rank_offset=7)
    assert specs == (ASSURANCE_AVAILABILITY_QUESTION,)
    assert len(evaluations) == 1
    evaluation = evaluations[0]
    assert evaluation.rank == 8
    assert evaluation.subject.subject_kind == "run"
    assert evaluation.observation_status == "abstained"
    assert evaluation.question_readiness == "abstained"
    assert evaluation.decision_readiness == "abstained"
    assert evaluation.decision is None
    assert {item.requirement_id: item.status for item in evaluation.requirements} == {
        "coverage_provider_run_resolved": "missing",
        "assertion_or_invariant_provider_resolved": "missing",
        "mutation_or_runtime_scenario_provider_resolved": "missing",
        "negative_control_provider_resolved": "not_evaluated",
    }


def test_assurance_wire_roundtrip_revalidates_nested_epistemic_evidence() -> None:
    analysis = analyze_code_assurance(
        _coverage(),
        {},
        snapshot_id=SNAPSHOT_ID,
        snapshot_freshness="current",
    )
    payload = json.loads(json.dumps(analysis.as_payload()))

    assert parse_code_assurance_payload(payload) == analysis

    payload["question_evaluations"][0]["decision_readiness"] = "human_review_required"
    with pytest.raises(ValueError, match="readiness is not derived"):
        parse_code_assurance_payload(payload)


def test_assurance_wire_rejects_unknown_fields_and_forged_behavior_claims() -> None:
    analysis = analyze_code_assurance(
        _coverage(),
        {},
        snapshot_id=SNAPSHOT_ID,
        snapshot_freshness="current",
    )
    payload = json.loads(json.dumps(analysis.as_payload()))
    payload["observations"][0]["behavioral_assurance_status"] = "established"
    with pytest.raises(ValueError, match="cannot claim behavioral protection"):
        parse_code_assurance_payload(payload)

    payload = json.loads(json.dumps(analysis.as_payload()))
    payload["unknown"] = True
    with pytest.raises(ValueError, match="fields are invalid"):
        parse_code_assurance_payload(payload)
