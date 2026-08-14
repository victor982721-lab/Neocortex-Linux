"""Calibrated, advisory consensus for potentially unused code."""

from __future__ import annotations

import inspect
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from _04_Nucleo_Operativo.code_unused_analysis import (
    CODE_UNUSED_REFERENCE_LIMIT,
    DEFAULT_CALIBRATION_SAMPLES,
    DEFAULT_HOLDOUT_SAMPLES,
    CodeUnusedAnalysis,
    UnusedConsensusCandidate,
    UnusedEvidenceSignals,
    UnusedProviderStatus,
    _CurrentSymbol,
    _classify,
    _graph_observations,
    analyze_code_unused,
    build_unused_analysis,
    classify_unused_candidate,
    evaluate_unused_calibration,
    read_code_unused_analysis,
)


FIXTURES = Path(__file__).parent / "fixtures" / "unused_consensus"


def _graph_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE files(
            file_id INTEGER PRIMARY KEY,
            current_version_id INTEGER,
            status TEXT NOT NULL
        );
        CREATE TABLE file_versions(
            version_id INTEGER PRIMARY KEY,
            file_id INTEGER NOT NULL,
            invalidated_ns INTEGER
        );
        CREATE TABLE code_references(
            reference_id INTEGER PRIMARY KEY,
            version_id INTEGER NOT NULL,
            source_symbol_id INTEGER,
            target_symbol_id INTEGER,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            target_hint TEXT
        );
        """
    )
    return connection


def _current_symbol(
    symbol_id: int,
    *,
    version_id: int = 2,
    parent_symbol_id: int | None = None,
    name: str = "candidate",
) -> _CurrentSymbol:
    return _CurrentSymbol(
        symbol_id=symbol_id,
        version_id=version_id,
        parent_symbol_id=parent_symbol_id,
        kind="function",
        name=name,
        qualified_name=f"pkg.{name}",
        start_line=1,
        end_line=2,
        path_observed="pkg/current.py",
    )


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
        (
            "vulture_high_confidence",
            "pyright_reported_unused",
            "no_observed_usage_or_dynamic_contract",
        ),
        signals.evidence_ids,
        ("advisory_only",),
    )


def _ready_providers() -> tuple[UnusedProviderStatus, ...]:
    return tuple(
        UnusedProviderStatus(
            provider_id=provider_id,
            status="ready",
            reason=None,
            tool_run_id=index,
            effective_tool_run_id=index,
            findings=1,
            eligible_candidates=1,
            covered_candidates=1,
            comparability="comparable",
            source_provider_schema="neocortex.external-provider/v1",
            source_tool_name=provider_id,
            source_tool_version="fixture-1",
            source_comparability_signature=f"comparability:{provider_id}",
        )
        for index, provider_id in enumerate(
            ("pyright-trusted-project", "vulture-unused-static"), start=1
        )
    )


def test_private_classification_signature_and_reason_precedence() -> None:
    assert str(inspect.signature(_classify)) == (
        "(signals: 'UnusedEvidenceSignals') -> 'tuple[UnusedState, tuple[str, ...]]'"
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
    "changes",
    (
        {"vulture_confidence": True},
        {"vulture_confidence": float("nan")},
        {"vulture_confidence": 1.01},
        {"graph_references": True},
        {"graph_calls": -1},
        {"graph_imports": 0.5},
        {"evidence_ids": ("duplicate", "duplicate")},
    ),
)
def test_classification_rejects_invalid_or_ambiguous_signal_domains(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _signals(**changes)


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
    analysis = build_unused_analysis((candidate,), providers=_ready_providers())

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


def test_graph_bound_excludes_more_than_half_a_million_historical_references() -> None:
    connection = _graph_connection()
    historical_version = 1
    current_version = 2
    candidate_id = 10
    caller_id = 20
    connection.executemany(
        "INSERT INTO file_versions(version_id,file_id,invalidated_ns) VALUES(?,?,?)",
        (
            (historical_version, 1, 100),
            (current_version, 1, None),
        ),
    )
    connection.execute(
        "INSERT INTO files(file_id,current_version_id,status) VALUES(1,?,'current')",
        (current_version,),
    )
    connection.execute(
        """WITH RECURSIVE sequence(reference_id) AS (
        SELECT 1 UNION ALL SELECT reference_id + 1 FROM sequence WHERE reference_id < ?
        )
        INSERT INTO code_references(
            reference_id,version_id,source_symbol_id,target_symbol_id,kind,name,target_hint
        )
        SELECT reference_id,?,?,?,'call','candidate',NULL FROM sequence""",
        (CODE_UNUSED_REFERENCE_LIMIT + 1, historical_version, caller_id, candidate_id),
    )
    connection.execute(
        """INSERT INTO code_references(
        reference_id,version_id,source_symbol_id,target_symbol_id,kind,name,target_hint
        ) VALUES(?,?,?,?,?,?,NULL)""",
        (
            CODE_UNUSED_REFERENCE_LIMIT + 2,
            current_version,
            caller_id,
            candidate_id,
            "call",
            "candidate",
        ),
    )

    graph = _graph_observations(
        connection,
        (_current_symbol(candidate_id), _current_symbol(caller_id, name="caller")),
        frozenset({candidate_id}),
    )

    assert connection.execute("SELECT COUNT(*) FROM code_references").fetchone()[0] == (
        CODE_UNUSED_REFERENCE_LIMIT + 2
    )
    assert graph[candidate_id].calls == 1
    assert graph[candidate_id].references == 0
    assert graph[candidate_id].imports == 0


def test_graph_ignores_invalidated_reference_to_current_candidate() -> None:
    connection = _graph_connection()
    connection.executemany(
        "INSERT INTO file_versions(version_id,file_id,invalidated_ns) VALUES(?,?,?)",
        (
            (1, 1, 100),
            (2, 1, None),
        ),
    )
    connection.execute("INSERT INTO files(file_id,current_version_id,status) VALUES(1,2,'current')")
    connection.executemany(
        """INSERT INTO code_references(
        reference_id,version_id,source_symbol_id,target_symbol_id,kind,name,target_hint
        ) VALUES(?,?,?,?,?,?,NULL)""",
        (
            (1, 1, 20, 10, "call", "candidate"),
            (2, 2, 20, 10, "import", "candidate"),
        ),
    )

    graph = _graph_observations(
        connection,
        (_current_symbol(10), _current_symbol(20, name="caller")),
        frozenset({10}),
    )

    assert graph[10].calls == 0
    assert graph[10].imports == 1


def test_graph_bound_still_applies_to_the_current_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _graph_connection()
    connection.execute(
        "INSERT INTO file_versions(version_id,file_id,invalidated_ns) VALUES(2,1,NULL)"
    )
    connection.execute("INSERT INTO files(file_id,current_version_id,status) VALUES(1,2,'current')")
    connection.executemany(
        """INSERT INTO code_references(
        reference_id,version_id,source_symbol_id,target_symbol_id,kind,name,target_hint
        ) VALUES(?,2,20,10,'call','candidate',NULL)""",
        ((1,), (2,)),
    )
    monkeypatch.setattr(
        "_04_Nucleo_Operativo.code_unused_analysis.CODE_UNUSED_REFERENCE_LIMIT",
        1,
    )

    with pytest.raises(ValueError, match="unused code reference bound exceeded"):
        _graph_observations(
            connection,
            (_current_symbol(10), _current_symbol(20, name="caller")),
            frozenset({10}),
        )
