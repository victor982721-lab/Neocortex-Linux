"""Replay labels frozen independently of the lexical implementation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.knowledge_evidence_evaluation import evaluate_case, load_frozen_cases, summarize

ROOT = Path(__file__).parent / "fixtures" / "evidence_evaluation"
CASES, MANIFEST = load_frozen_cases(ROOT / "cases.jsonl", ROOT / "manifest.json")
FAMILY_CASES, FAMILY_MANIFEST = load_frozen_cases(ROOT / "documented_families.jsonl", ROOT / "documented_families_manifest.json")
ACCEPTANCE_CASES, ACCEPTANCE_MANIFEST = load_frozen_cases(ROOT / "epistemic_acceptance.jsonl", ROOT / "epistemic_acceptance_manifest.json")
ALL_CASES = CASES + FAMILY_CASES + ACCEPTANCE_CASES


@pytest.mark.parametrize("case", ALL_CASES, ids=[case["case_id"] for case in ALL_CASES])
def test_independent_case_preserves_literal_and_final_excerpt_limits(case: dict) -> None:
    row = evaluate_case(case)
    assert row["contract_errors"] == []
    assert row["independent_literal_validator_errors"] == []
    assert row["answer_sufficiency"] == "not_assessed"
    assert row["emitted_extent"]["document_completeness"] == "not_asserted"
    # Unknown cases count as abstentions in the report, never correct answers.
    # A necessary-marker claim on a independently labeled negative is unsafe.
    if case["expected_requirement"] != "support":
        assert row["observed_requirement"] != "support", row
    if case["expected_requirement"] == "not_assessed":
        assert row["observed_requirement"] != "contradicted", row


def test_independent_dataset_hash_and_scenario_split_are_enforced(tmp_path: Path) -> None:
    changed = tmp_path / "cases.jsonl"
    changed.write_bytes((ROOT / "cases.jsonl").read_bytes() + b"\n")
    with pytest.raises(ValueError, match="digest mismatch"):
        load_frozen_cases(changed, ROOT / "manifest.json")
    by_scenario = {}
    for case in CASES:
        assert by_scenario.setdefault(case["paraphrase_group"], case["split"]) == case["split"]
    assert {case["split"] for case in CASES} == {"development", "heldout"}
    # A distinct evaluator authored the labels; the public fixture never
    # rewrites its expected fields from observed implementation predictions.
    assert MANIFEST["labels_are_semantic_expectations_not_current_engine_predictions"] is True


def test_evaluation_keeps_abstentions_and_unlabeled_claims_out_of_precision() -> None:
    pairs = [
        ("support", "support"), ("not_seen", "support"), ("support", "not_seen"),
        ("contradicted", "contradicted"), ("support", "not_assessed"),
        ("not_seen", "not_assessed"), ("not_assessed", "support"),
    ]
    result = summarize([
        {"expected_requirement": expected, "observed_requirement": observed, "contract_errors": []}
        for expected, observed in pairs
    ])
    assert (result["tp"], result["fp"], result["fn"], result["tn"]) == (1, 1, 1, 1)
    assert result["abstentions"] == 2 and result["abstained_positive"] == 1
    assert result["unsafe_assessment"] == 1
    assert result["support_precision_assessed"] == .5
    assert result["support_precision_denominator"] == 2
    assert result["assessment_coverage"] == 4 / 6


def test_expected_labels_are_serializable_without_product_predictions() -> None:
    encoded = json.dumps(CASES, ensure_ascii=False)
    assert "observed_requirement" not in encoded
    assert "necessary_checks_not_failed" not in encoded
