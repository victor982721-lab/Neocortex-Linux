"""Calibrated, advisory consensus for potentially unused code."""

from __future__ import annotations

import inspect
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from _04_Nucleo_Operativo.code_unused_analysis import (
    DEFAULT_CALIBRATION_SAMPLES,
    DEFAULT_HOLDOUT_SAMPLES,
    CodeUnusedAnalysis,
    UnusedConsensusCandidate,
    UnusedEvidenceSignals,
    _classify,
    analyze_code_unused,
    classify_unused_candidate,
    evaluate_unused_calibration,
    read_code_unused_analysis,
)


FIXTURES = Path(__file__).parent / "fixtures" / "unused_consensus"


def _signals(**changes: object) -> UnusedEvidenceSignals:
    values: dict[str, object] = {
        "vulture_reported": True,
        "vulture_confidence": 1.0,
        "pyright_reported": True,
        "vulture_complete": True,
        "pyright_complete": True,
        "providers_aligned": True,
        "graph_references": 0,
        "graph_calls": 0,
        "graph_imports": 0,
        "in_all": False,
        "reexported": False,
        "entry_point": False,
        "callback": False,
        "registry": False,
        "fixture": False,
        "protocol": False,
        "special": False,
        "coverage_observed": False,
        "coverage_status": "missing",
        "evidence_ids": ("finding-vulture", "finding-pyright"),
    }
    values.update(changes)
    return UnusedEvidenceSignals(**values)  # type: ignore[arg-type]


def _candidate(index: int) -> UnusedConsensusCandidate:
    signals = _signals()
    return UnusedConsensusCandidate(
        f"candidate-{index:03d}",
        index + 1,
        index + 1,
        f"pkg/module_{index:03d}.py",
        f"pkg.module_{index:03d}",
        f"pkg.module_{index:03d}.unused_{index:03d}",
        f"unused_{index:03d}",
        "function",
        10,
        12,
        classify_unused_candidate(signals),
        ("pyright-trusted-project", "vulture-unused-static"),
        signals,
        ("static_consensus",),
        signals.evidence_ids,
        ("advisory_only",),
    )


def test_private_classification_signature_and_reason_precedence() -> None:
    assert str(inspect.signature(_classify)) == (
        "(signals: 'UnusedEvidenceSignals') -> "
        "'tuple[UnusedState, tuple[str, ...]]'"
    )
    state, reasons = _classify(
        _signals(
            graph_references=1,
            graph_calls=2,
            graph_imports=3,
            in_all=True,
            reexported=True,
            entry_point=True,
            coverage_observed=True,
            coverage_status="complete",
            callback=True,
            registry=True,
            fixture=True,
            protocol=True,
            special=True,
        )
    )

    assert state == "explained_usage"
    assert reasons == (
        "indexed_reference",
        "indexed_call",
        "indexed_import",
        "declared_in___all__",
        "reexported",
        "declared_entry_point",
        "observed_by_declared_coverage_scope",
    )


def test_dynamic_reasons_are_ordered_and_precede_static_consensus() -> None:
    state, reasons = _classify(
        _signals(
            callback=True,
            registry=True,
            fixture=True,
            protocol=True,
            special=True,
        )
    )

    assert state == "dynamic_usage_possible"
    assert reasons == (
        "callback_binding",
        "registry_binding",
        "fixture_binding",
        "protocol_contract",
        "special_runtime_protocol",
    )


def test_high_consensus_and_abstention_reasons_are_exact() -> None:
    assert _classify(_signals()) == (
        "probable_unused_high_consensus",
        (
            "vulture_high_confidence",
            "pyright_reported_unused",
            "no_observed_usage_or_dynamic_contract",
        ),
    )

    state, reasons = _classify(
        _signals(
            vulture_reported=False,
            vulture_confidence=None,
            pyright_reported=False,
            vulture_complete=False,
            pyright_complete=False,
            providers_aligned=False,
            coverage_status="partial",
        )
    )

    assert state == "insufficient_evidence"
    assert reasons == (
        "coverage_partial_does_not_support_usage",
        "provider_domains_not_aligned",
        "provider_measurement_incomplete",
        "pyright_did_not_report",
        "vulture_did_not_report",
    )


@pytest.mark.parametrize(
    "signals",
    (
        _signals(vulture_confidence=True),
        _signals(vulture_confidence=float("nan")),
        _signals(vulture_confidence=1.01),
        _signals(graph_references=True),
        _signals(graph_calls=-1),
        _signals(graph_imports=0.5),
        _signals(evidence_ids=("duplicate", "duplicate")),
    ),
)
def test_classification_rejects_invalid_or_ambiguous_signal_domains(
    signals: UnusedEvidenceSignals,
) -> None:
    with pytest.raises(ValueError):
        _classify(signals)


@pytest.mark.parametrize(
    ("signals", "expected"),
    (
        (_signals(graph_references=1), "explained_usage"),
        (_signals(in_all=True), "explained_usage"),
        (_signals(entry_point=True), "explained_usage"),
        (
            _signals(coverage_observed=True, coverage_status="partial"),
            "explained_usage",
        ),
        (_signals(registry=True), "dynamic_usage_possible"),
        (_signals(protocol=True), "dynamic_usage_possible"),
        (_signals(coverage_status="partial"), "probable_unused_high_consensus"),
        (_signals(pyright_reported=False), "insufficient_evidence"),
        (_signals(vulture_confidence=0.60), "insufficient_evidence"),
    ),
)
def test_classification_precedence_is_explainable_and_conservative(
    signals: UnusedEvidenceSignals,
    expected: str,
) -> None:
    assert classify_unused_candidate(signals) == expected


def test_calibration_reports_precision_recall_and_abstention_separately() -> None:
    calibration = evaluate_unused_calibration(
        DEFAULT_CALIBRATION_SAMPLES,
        dataset_id="neocortex-unused-calibration/v1",
    )
    holdout = evaluate_unused_calibration(
        DEFAULT_HOLDOUT_SAMPLES,
        dataset_id="neocortex-unused-holdout/v1",
    )

    assert calibration.precision == 1.0
    assert calibration.recall == pytest.approx(2 / 3)
    assert calibration.abstention_rate == 0.25
    assert holdout.precision == 1.0
    assert holdout.recall == pytest.approx(1 / 3)
    assert holdout.abstention_rate == 0.5


@pytest.mark.parametrize(
    ("fixture_name", "samples"),
    (
        ("calibration-v1.json", DEFAULT_CALIBRATION_SAMPLES),
        ("holdout-v1.json", DEFAULT_HOLDOUT_SAMPLES),
    ),
)
def test_labeled_fixture_matches_the_embedded_installed_dataset(
    fixture_name: str,
    samples: tuple,
) -> None:
    payload = json.loads((FIXTURES / fixture_name).read_text(encoding="utf-8"))
    actual = {
        item.sample_id: (item.label, classify_unused_candidate(item.signals)) for item in samples
    }
    expected = {
        item["sample_id"]: (item["label"], item["expected_state"]) for item in payload["samples"]
    }

    assert actual == expected


def test_public_payload_is_bounded_but_digest_retains_all_candidate_evidence() -> None:
    analysis = analyze_code_unused(
        tuple(_candidate(index) for index in range(25)),
        provider_signature="providers-v1",
    )

    payload = analysis.as_payload()
    digest = analysis.digest_payload()
    assert payload["candidates_total"] == 25
    assert payload["candidates_truncated"] is True
    assert len(payload["candidates"]) == 20  # type: ignore[arg-type]
    assert len(digest["candidates"]) == 25  # type: ignore[arg-type]
    assert analysis.mutation_authority is False
    assert all(candidate.mutation_authority is False for candidate in analysis.candidates)


def test_high_consensus_candidate_has_no_delete_or_mutation_authority() -> None:
    candidate = _candidate(1)
    analysis = analyze_code_unused((candidate,), provider_signature="providers-v1")

    assert analysis.status == "ready"
    assert analysis.counts["probable_unused_high_consensus"] == 1
    assert analysis.authority == "advisory"
    assert analysis.mutation_authority is False
    assert {gate.status for gate in analysis.gates} == {"passed"}


def test_unresolved_candidate_cannot_be_promoted_to_high_consensus() -> None:
    candidate = replace(
        _candidate(1),
        symbol_id=None,
        symbol=None,
        signals=_signals(providers_aligned=False),
        state="insufficient_evidence",
    )
    analysis = analyze_code_unused((candidate,), provider_signature="providers-v1")

    assert analysis.candidates[0].state == "insufficient_evidence"


def test_reader_abstains_cleanly_when_normalized_provider_state_is_missing() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row

    analysis: CodeUnusedAnalysis = read_code_unused_analysis(connection, 1)

    assert analysis.status == "abstained"
    assert analysis.reason is not None
    assert analysis.candidates == ()
    assert analysis.mutation_authority is False
