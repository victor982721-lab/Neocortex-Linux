from __future__ import annotations

import json
from dataclasses import replace

import pytest

from _04_Nucleo_Operativo.code_invariant_assurance_analysis import (
    analyze_code_invariant_assurance,
    parse_code_invariant_assurance_payload,
)
from _04_Nucleo_Operativo.code_invariant_contracts import (
    INVARIANT_SPECS,
    RUNTIME_SCENARIOS,
    invariant_registry_fingerprint,
    runtime_scenario,
)
from _04_Nucleo_Operativo.external_deep_coverage import PYTEST_COVERAGE_PROVIDER_ID
from _04_Nucleo_Operativo.external_evidence_models import (
    ExternalProviderEvidence,
    ExternalProviderRelation,
    external_relation_identity,
)


def _relation(nodeid: str, outcome: str, index: int) -> ExternalProviderRelation:
    run_key = "coverage-run:fixture-scope"
    return ExternalProviderRelation(
        external_relation_identity(
            PYTEST_COVERAGE_PROVIDER_ID,
            relation_kind="declared_test_outcome",
            source_kind="contract",
            source_key=f"pytest-nodeid:{nodeid}",
            target_kind="run",
            target_key=run_key,
        ),
        "declared_test_outcome",
        "contract",
        f"pytest-nodeid:{nodeid}",
        "run",
        run_key,
        confidence=1.0,
        metadata={
            "nodeid": nodeid,
            "outcome": outcome,
            "claim_scope": "exact_selected_test_execution_outcome",
            "assertion_or_invariant_proof": False,
            "measurement_scope_signature": "fixture-scope",
            "ordinal": index,
        },
    )


def _provider(outcomes: dict[str, str]) -> ExternalProviderEvidence:
    relations = tuple(
        _relation(nodeid, outcome, index)
        for index, (nodeid, outcome) in enumerate(sorted(outcomes.items()), start=1)
    )
    return ExternalProviderEvidence(
        PYTEST_COVERAGE_PROVIDER_ID,
        17,
        17,
        "ready",
        None,
        (),
        (),
        relations,
    )


def test_registry_is_canonical_versioned_and_every_scenario_is_linked() -> None:
    assert tuple(item.invariant_id for item in INVARIANT_SPECS) == tuple(
        sorted(item.invariant_id for item in INVARIANT_SPECS)
    )
    assert tuple(item.scenario_id for item in RUNTIME_SCENARIOS) == tuple(
        sorted(item.scenario_id for item in RUNTIME_SCENARIOS)
    )
    assert {scenario for invariant in INVARIANT_SPECS for scenario in invariant.scenario_ids} == {
        item.scenario_id for item in RUNTIME_SCENARIOS
    }
    assert invariant_registry_fingerprint().startswith("code-invariant-registry-v1:xxh3_128:")
    assert runtime_scenario("semantic.staging_process_death_resume").scenario_kind == (
        "process_death"
    )
    with pytest.raises(ValueError, match="unknown runtime scenario"):
        runtime_scenario("delete.production.now")


def test_exact_passing_scenario_receipts_are_observed_but_never_become_a_decision() -> None:
    provider = _provider({item.test_nodeid: "passed" for item in RUNTIME_SCENARIOS})

    result = analyze_code_invariant_assurance(
        {PYTEST_COVERAGE_PROVIDER_ID: provider},
        snapshot_id="snapshot-fixture",
        snapshot_freshness="current",
    )

    assert result.status == "ready"
    assert result.resolved_scenarios == result.passed_scenarios == len(RUNTIME_SCENARIOS)
    assert result.counterevidence_scenarios == 0
    assert all(item.status == "all_declared_scenarios_passed" for item in result.observations)
    assert all(item.observation_status == "confirmed" for item in result.question_evaluations)
    assert all(
        item.decision_readiness == "experiment_required" for item in result.question_evaluations
    )
    assert all(item.decision is None for item in result.question_evaluations)
    assert result.authority == "advisory"
    assert result.mutation_authority is False


def test_failed_scenario_is_preserved_as_counterevidence_not_change_authority() -> None:
    outcomes = {item.test_nodeid: "passed" for item in RUNTIME_SCENARIOS}
    failed_nodeid = RUNTIME_SCENARIOS[0].test_nodeid
    outcomes[failed_nodeid] = "failed"

    result = analyze_code_invariant_assurance(
        {PYTEST_COVERAGE_PROVIDER_ID: _provider(outcomes)},
        snapshot_id="snapshot-fixture",
        snapshot_freshness="current",
    )

    assert result.counterevidence_scenarios == 1
    failed = next(
        item for item in result.observations if any(s.status == "failed" for s in item.scenarios)
    )
    assert failed.status == "counterevidence_observed"
    evaluation = next(
        item
        for item in result.question_evaluations
        if item.subject.subject_key == f"invariant:{failed.invariant_id}"
    )
    assert evaluation.counterevidence_status == "evaluated"
    assert evaluation.decision_readiness == "experiment_required"
    assert evaluation.decision is None


def test_partial_selection_remains_partial_and_unselected_is_not_a_failure() -> None:
    selected = RUNTIME_SCENARIOS[:2]
    result = analyze_code_invariant_assurance(
        {PYTEST_COVERAGE_PROVIDER_ID: _provider({item.test_nodeid: "passed" for item in selected})},
        snapshot_id="snapshot-fixture",
        snapshot_freshness="current",
    )

    assert result.status == "partial"
    assert result.resolved_scenarios == 2
    assert result.counterevidence_scenarios == 0
    assert sum(item.missing for item in result.observations) == 2
    assert any(item.status == "scenario_evidence_incomplete" for item in result.observations)


def test_missing_or_abstained_provider_fails_closed_without_nominal_evidence() -> None:
    missing = analyze_code_invariant_assurance(
        {}, snapshot_id="snapshot-fixture", snapshot_freshness="publication_only"
    )
    abstained = analyze_code_invariant_assurance(
        {
            PYTEST_COVERAGE_PROVIDER_ID: ExternalProviderEvidence(
                PYTEST_COVERAGE_PROVIDER_ID,
                9,
                None,
                "abstained",
                "provider_failed",
            )
        },
        snapshot_id="snapshot-fixture",
        snapshot_freshness="publication_only",
    )

    assert missing.status == abstained.status == "abstained"
    assert missing.observations == abstained.observations == ()
    assert missing.question_evaluations == abstained.question_evaluations == ()


def test_forged_test_outcome_relation_and_authority_are_rejected() -> None:
    scenario = RUNTIME_SCENARIOS[0]
    valid = _relation(scenario.test_nodeid, "passed", 1)
    forged = replace(
        valid,
        metadata={**valid.metadata, "assertion_or_invariant_proof": True},
    )
    provider = ExternalProviderEvidence(
        PYTEST_COVERAGE_PROVIDER_ID, 1, 1, "ready", None, (), (), (forged,)
    )

    with pytest.raises(ValueError, match="incompatible"):
        analyze_code_invariant_assurance(
            {PYTEST_COVERAGE_PROVIDER_ID: provider},
            snapshot_id="snapshot-fixture",
            snapshot_freshness="current",
        )


def test_wire_round_trip_and_digest_bound_counts_reject_forgery() -> None:
    provider = _provider({item.test_nodeid: "passed" for item in RUNTIME_SCENARIOS})
    result = analyze_code_invariant_assurance(
        {PYTEST_COVERAGE_PROVIDER_ID: provider},
        snapshot_id="snapshot-fixture",
        snapshot_freshness="current",
    )
    payload = json.loads(json.dumps(result.as_payload()))

    assert parse_code_invariant_assurance_payload(payload) == result

    payload["passed_scenarios"] = 0
    with pytest.raises(ValueError, match="pass count"):
        parse_code_invariant_assurance_payload(payload)
