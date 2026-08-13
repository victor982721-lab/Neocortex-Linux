from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from _04_Nucleo_Operativo.code_analyzer_calibration import (
    analyze_code_analyzer_calibration,
    analyzer_calibration_questions,
    parse_code_analyzer_calibration_payload,
)
from _04_Nucleo_Operativo.code_invariant_assurance_analysis import (
    analyze_code_invariant_assurance,
)
from _04_Nucleo_Operativo.code_invariant_contracts import RUNTIME_SCENARIOS
from _04_Nucleo_Operativo.external_deep_coverage import PYTEST_COVERAGE_PROVIDER_ID
from _04_Nucleo_Operativo.external_evidence_models import (
    ExternalProviderEvidence,
    ExternalProviderRelation,
    external_relation_identity,
)


ROOT = Path(__file__).resolve().parents[1]


def _assurance(outcome: str):
    relations = tuple(
        ExternalProviderRelation(
            external_relation_identity(
                PYTEST_COVERAGE_PROVIDER_ID,
                relation_kind="declared_test_outcome",
                source_kind="contract",
                source_key=f"pytest-nodeid:{nodeid}",
                target_kind="run",
                target_key="coverage-run:calibration",
            ),
            "declared_test_outcome",
            "contract",
            f"pytest-nodeid:{nodeid}",
            "run",
            "coverage-run:calibration",
            confidence=1.0,
            metadata={
                "nodeid": nodeid,
                "outcome": outcome,
                "claim_scope": "exact_selected_test_execution_outcome",
                "assertion_or_invariant_proof": False,
                "measurement_scope_signature": "calibration",
            },
        )
        for scenario in RUNTIME_SCENARIOS
        for nodeid in scenario.test_nodeids
    )
    provider = ExternalProviderEvidence(
        PYTEST_COVERAGE_PROVIDER_ID,
        33,
        33,
        "ready",
        None,
        relations=relations,
    )
    return analyze_code_invariant_assurance(
        {PYTEST_COVERAGE_PROVIDER_ID: provider},
        snapshot_id="snapshot-calibration",
        snapshot_freshness="current",
    )


def test_provisional_fixture_is_observed_but_never_claims_effectiveness() -> None:
    result = analyze_code_analyzer_calibration(ROOT, source_version="source-fixture")

    assert result.status == "provisional"
    assert result.labels_total == result.provisional_labels == 40
    assert result.independent_labels == result.holdout_labels == 0
    assert result.calibration_labels == 40
    assert result.precision_at_k is result.recall is None
    assert result.finding_to_decision_rate is None
    assert result.decisions_per_attention_minute is None
    assert result.authority == "advisory"
    assert result.mutation_authority is False
    assert all(not label.detector_independent for label in result.corpora[0].labels)

    specs, evaluations = analyzer_calibration_questions(result, rank_offset=7)
    assert len(specs) == len(evaluations) == 1
    evaluation = evaluations[0]
    assert evaluation.rank == 8
    assert evaluation.question_readiness == "ready"
    assert evaluation.decision_readiness == "experiment_required"
    assert evaluation.decision is None
    requirements = {item.requirement_id: item.status for item in evaluation.requirements}
    assert requirements["versioned_calibration_corpus_observed"] == "satisfied"
    assert requirements["independent_holdout_labels_linked"] == "missing"
    assert requirements["antigoodhart_controls_evaluated"] == "not_evaluated"


def test_antigoodhart_receipt_is_linked_without_overstating_general_invariance() -> None:
    result = analyze_code_analyzer_calibration(
        ROOT,
        source_version="source-fixture",
        invariant_assurance=_assurance("passed"),
    )

    by_kind = {item.transformation: item for item in result.anti_goodhart_controls}
    assert by_kind["rename"].status == "passed"
    assert by_kind["move"].status == "passed"
    assert by_kind["wrapper"].status == "passed"
    assert by_kind["call_spelling"].status == "passed"
    assert len(by_kind["call_spelling"].test_nodeids) == 7
    assert by_kind["call_spelling"].test_nodeids == tuple(
        sorted(by_kind["call_spelling"].test_nodeids, key=lambda item: (item.casefold(), item))
    )
    assert by_kind["metric_dilution"].status == "not_observed"
    assert result.anti_goodhart_passed == 4
    assert result.anti_goodhart_not_observed == 1
    assert result.status == "provisional"


def test_failed_antigoodhart_scenario_is_preserved_as_counterevidence() -> None:
    result = analyze_code_analyzer_calibration(
        ROOT,
        source_version="source-fixture",
        invariant_assurance=_assurance("failed"),
    )

    assert result.anti_goodhart_failed == 4
    assert result.anti_goodhart_not_observed == 1
    assert result.status == "provisional"


def test_missing_fixture_fails_closed_without_labels_or_metrics(tmp_path: Path) -> None:
    result = analyze_code_analyzer_calibration(tmp_path, source_version="source-fixture")

    assert result.status == "not_established"
    assert result.reason == "calibration_corpus_unavailable"
    assert result.corpora == ()
    assert result.labels_total == 0
    assert result.precision_at_k is result.recall is None
    _specs, evaluations = analyzer_calibration_questions(result, rank_offset=0)
    assert evaluations[0].observation_status == "abstained"
    assert evaluations[0].next_action_ids == ()


def test_wire_round_trip_and_identity_reject_forgery() -> None:
    result = analyze_code_analyzer_calibration(ROOT, source_version="source-fixture")
    payload = json.loads(json.dumps(result.as_payload()))

    assert parse_code_analyzer_calibration_payload(payload) == result

    payload["precision_at_k"] = 1.0
    with pytest.raises(ValueError, match="cannot publish effectiveness metrics"):
        parse_code_analyzer_calibration_payload(payload)

    with pytest.raises(ValueError, match="counts are not derived"):
        replace(result, provisional_labels=0)


def test_metric_dilution_control_remains_explicit_until_executed() -> None:
    result = analyze_code_analyzer_calibration(ROOT, source_version="source-fixture")
    control = next(
        item for item in result.anti_goodhart_controls if item.transformation == "metric_dilution"
    )

    assert control.status == "not_observed"
    assert control.provider_run_id is None
    assert "not_yet_linked" in control.limitation


def test_provisional_label_cannot_be_relabelled_independent_by_wire_edit() -> None:
    result = analyze_code_analyzer_calibration(ROOT, source_version="source-fixture")
    payload = json.loads(json.dumps(result.as_payload()))
    label = payload["corpora"][0]["labels"][0]
    label["label_status"] = "independent_human_validated"
    label["detector_independent"] = True
    with pytest.raises(ValueError, match="identity"):
        parse_code_analyzer_calibration_payload(payload)
