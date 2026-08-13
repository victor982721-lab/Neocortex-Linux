"""Deterministic, read-only acceptance coverage for published code review."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

import _04_Nucleo_Operativo.code_review as code_review_module
from _02_Deduplicacion import FileSnapshot
from _04_Nucleo_Operativo.code_contracts import (
    AnalysisStatus,
    ArtifactClassification,
    ArtifactKind,
    CodeAnalysis,
    CodeFileInput,
    DiagnosticRecord,
    DiagnosticSeverity,
    ReferenceRecord,
    SourceRange,
    SymbolRecord,
)
from _04_Nucleo_Operativo.code_external_evidence import (
    EXTERNAL_EVIDENCE_SCHEMA,
    RUFF_CONFIGURATION_SIGNATURE,
    ExternalEvidencePublication,
    RuffEvidenceProvider,
    _configuration_payload,
    external_input_signature,
)
from _04_Nucleo_Operativo.code_review import review_code_state
from _04_Nucleo_Operativo.code_review_actionability import (
    CodeReviewActionabilityInput,
    assess_code_review_actionability,
    classify_source_role,
)
from _04_Nucleo_Operativo.code_schema import (
    checkpoint_code_wal,
    readonly_code_database,
    remove_checkpointed_code_sidecars,
)
from _04_Nucleo_Operativo.code_review_epistemics import (
    CodeReviewEvidenceResolutionError,
    resolve_code_review_questions,
)
from _04_Nucleo_Operativo.code_state import CodeState
from _04_Nucleo_Operativo.self_analysis_freshness import SelfAnalysisFreshness
from _04_Nucleo_Operativo.self_analysis_status import SelfAnalysisStatus
from _04_Nucleo_Operativo.semantic_models import (
    canonical_json,
    fingerprint_bytes,
    fingerprint_text,
)

PROCESSING_SIGNATURE = "code-review-fixture-v1"
ACTIONABILITY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "code_review" / "rc6_top10_actionability_v1.json"
)
REPRESENTATIVE_ACTIONABILITY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "code_review" / "rc11_top40_actionability_v2.json"
)


def _fixture_score(label: dict[str, object]) -> int:
    complexity = label["complexity"]
    function_lines = label["function_lines"]
    complexity_bp = (
        0
        if complexity is None
        else (10_000 * int(complexity["value"])) // int(complexity["threshold"])
    )
    length_bp = (
        0
        if function_lines is None
        else (10_000 * int(function_lines["value"])) // int(function_lines["threshold"])
    )
    impact_bp = 250 * min(int(label["resolved_static_callers"]), 20)
    return max(complexity_bp, length_bp) + min(complexity_bp, length_bp) // 4 + impact_bp


def test_rc6_top10_actionability_fixture_is_reproducible() -> None:
    payload = json.loads(ACTIONABILITY_FIXTURE.read_text(encoding="utf-8"))
    labels = payload["labels"]

    assert payload["schema"] == "neocortex-code-review-actionability-fixture-v1"
    assert payload["source"]["ground_truth_status"] == ("provisional_not_human_validated")
    assert [label["rank"] for label in labels] == list(range(1, 11))
    assert len({(label["path"], label["symbol"]) for label in labels}) == 10
    assert all(_fixture_score(label) == label["score_basis_points"] for label in labels)
    assert sum(label["label"] == "actionable" for label in labels) == 6
    assert sum(label["label"] == "defer" for label in labels) == 4
    assert payload["review_summary"] == {
        "actionable_ranks": [1, 4, 5, 6, 7, 9],
        "abstention_ranks": [2, 3, 8, 10],
        "duplicate_groups": [],
    }


def _representative_score(label: dict[str, object], ranking: str) -> int:
    signals = label["signals"]
    assert isinstance(signals, dict)
    complexity = signals["complexity"]
    function_lines = signals["function_lines"]
    complexity_bp = 0 if complexity is None else (10_000 * int(complexity[0])) // int(complexity[1])
    length_bp = (
        0 if function_lines is None else (10_000 * int(function_lines[0])) // int(function_lines[1])
    )
    impact_bp = 250 * min(int(signals["resolved_static_callers"]), 20)
    if ranking == "baseline":
        return max(complexity_bp, length_bp) + min(complexity_bp, length_bp) // 4 + impact_bp
    assert ranking == "candidate"
    return complexity_bp + length_bp // 4 + impact_bp


def _ranked_fixture_labels(
    labels: list[dict[str, object]],
    ranking: str,
) -> list[dict[str, object]]:
    selected = [
        label
        for label in labels
        if isinstance(label[ranking], dict) and label[ranking]["rank"] is not None
    ]
    return sorted(selected, key=lambda label: int(label[ranking]["rank"]))


def _precision_triplet(
    labels: list[dict[str, object]],
    ranking: str,
    cutoff: int,
) -> list[int]:
    selected = _ranked_fixture_labels(labels, ranking)[:cutoff]
    actionable = sum(label["label"] == "actionable" for label in selected)
    return [actionable, cutoff, (10_000 * actionable) // cutoff]


def test_rc11_top40_actionability_fixture_measures_ranking_v2() -> None:
    payload = json.loads(REPRESENTATIVE_ACTIONABILITY_FIXTURE.read_text(encoding="utf-8"))
    labels = payload["labels"]
    summary = payload["review_summary"]

    assert payload["schema"] == "neocortex-code-review-actionability-fixture-v2"
    assert payload["source"]["ground_truth_status"] == ("provisional_not_human_validated")
    assert payload["source"]["sample_strategy"] == "union_of_v1_and_v2_top40"
    assert len(labels) == summary["sample_size"] == 41
    assert len({(label["path"], label["symbol"]) for label in labels}) == 41
    assert all(":" not in label["path"] for label in labels)
    assert {label["construction"] for label in labels}.issuperset(
        {"algorithm", "builder", "orchestrator", "rule", "validator"}
    )
    assert sum(label["label"] == "actionable" for label in labels) == 24
    assert sum(label["label"] == "defer" for label in labels) == 17
    for label in labels:
        assert label["baseline"]["score_basis_points"] == _representative_score(label, "baseline")
        assert label["candidate"]["score_basis_points"] == _representative_score(label, "candidate")

    baseline = _ranked_fixture_labels(labels, "baseline")
    candidate = _ranked_fixture_labels(labels, "candidate")
    assert [label["baseline"]["rank"] for label in baseline] == list(range(1, 41))
    assert [label["candidate"]["rank"] for label in candidate] == list(range(1, 41))
    assert sum(label["baseline"]["rank"] is None for label in labels) == 1
    assert sum(label["candidate"]["rank"] is None for label in labels) == 1
    for cutoff in (10, 20, 30, 40):
        assert summary["baseline_v1"][f"top_{cutoff}"] == _precision_triplet(
            labels, "baseline", cutoff
        )
        assert summary["candidate_v2"][f"top_{cutoff}"] == _precision_triplet(
            labels, "candidate", cutoff
        )
    assert summary["precision_at_10_delta_basis_points"] == 1000
    assert summary["unchanged_precision_cutoffs"] == [20, 30, 40]
    assert summary["duplicate_groups"] == []


def _fixture_actionability_input(
    label: dict[str, object],
) -> CodeReviewActionabilityInput:
    signals = label["signals"]
    assert isinstance(signals, dict)
    complexity = signals["complexity"]
    function_lines = signals["function_lines"]
    callers = int(signals["resolved_static_callers"])
    return CodeReviewActionabilityInput(
        path=str(label["path"]),
        symbol=str(label["symbol"]),
        root=None,
        complexity_ratio_basis_points=(
            0 if complexity is None else (10_000 * int(complexity[0])) // int(complexity[1])
        ),
        length_ratio_basis_points=(
            0
            if function_lines is None
            else (10_000 * int(function_lines[0])) // int(function_lines[1])
        ),
        path_convention_production_callers=callers,
        path_convention_test_callers=0,
        path_convention_fixture_callers=0,
        path_convention_tool_callers=0,
        path_convention_compatibility_callers=0,
        resolved_static_consumer_files=callers,
    )


def test_legacy_provisional_labels_cannot_authorize_current_decisions() -> None:
    payload = json.loads(REPRESENTATIVE_ACTIONABILITY_FIXTURE.read_text(encoding="utf-8"))
    labels = payload["labels"]
    assessed = [
        (label, assess_code_review_actionability(_fixture_actionability_input(label)))
        for label in labels
    ]

    assert all(assessment.construction == "unknown" for _label, assessment in assessed)
    assert all(assessment.actionability == "characterize_first" for _label, assessment in assessed)
    assert not any(assessment.recommended_change for _label, assessment in assessed)
    assert all(
        assessment.epistemic_state.decision_readiness == "experiment_required"
        for _label, assessment in assessed
    )
    assert all(assessment.epistemic_state.decision is None for _label, assessment in assessed)


def test_actionability_gate_does_not_promote_provisional_rc14_labels() -> None:
    payload = json.loads(REPRESENTATIVE_ACTIONABILITY_FIXTURE.read_text(encoding="utf-8"))
    rc14_top_10 = sorted(
        (
            label
            for label in payload["labels"]
            if isinstance(label["candidate"], dict)
            and label["candidate"]["rank"] is not None
            and 2 <= int(label["candidate"]["rank"]) <= 11
        ),
        key=lambda label: int(label["candidate"]["rank"]),
    )
    assessed = [
        (label, assess_code_review_actionability(_fixture_actionability_input(label)))
        for label in rc14_top_10
    ]
    assert rc14_top_10[0]["symbol"] == (
        "knowledge_evaluation.GoldenCase._validate_required_feature"
    )
    assert assessed[0][1].actionability == "characterize_first"
    assert all(not assessment.recommended_change for _label, assessment in assessed)
    assert all(
        assessment.epistemic_state.decision_reason
        == "structural_observation_alone_cannot_justify_change"
        for _label, assessment in assessed
    )


@pytest.mark.parametrize(
    ("path", "root", "expected"),
    [
        (r"C:\repo\package\service.py", None, "production"),
        (r"C:\repo\tests\test_service.py", None, "test"),
        ("/repo/tests/fixtures/sample.py", None, "fixture"),
        ("/repo/tools/inspect_state.py", None, "tool"),
        ("/repo/compatibility/legacy_reader.py", None, "compatibility"),
        ("/tools/repository/package/service.py", "/tools/repository", "production"),
    ],
)
def test_source_roles_are_portable(
    path: str,
    root: str | None,
    expected: str,
) -> None:
    assert classify_source_role(path, root) == expected


def _source_range(index: int, function_lines: int) -> SourceRange:
    start_line = 1 + index * 400
    start_byte = index * 1_000
    return SourceRange(
        start_line,
        0,
        start_line + function_lines - 1,
        0,
        start_byte,
        start_byte + 100,
    )


def _analysis(
    path: Path,
    identity: int,
    *,
    symbols: tuple[SymbolRecord, ...],
    diagnostics: tuple[DiagnosticRecord, ...] = (),
    references: tuple[ReferenceRecord, ...] = (),
) -> CodeAnalysis:
    text = "x\n" * 5_000
    raw = text.encode("utf-8")
    raw_fingerprint = fingerprint_bytes(raw)
    text_fingerprint = fingerprint_text(text)
    snapshot = FileSnapshot(str(path), 1, identity, len(raw), 100 + identity, 50)
    source = CodeFileInput(
        snapshot,
        text,
        raw,
        "utf-8",
        ArtifactClassification(
            "python",
            ArtifactKind.SOURCE,
            1.0,
            ("code-review-fixture",),
        ),
        PROCESSING_SIGNATURE,
    )
    return CodeAnalysis(
        input=source,
        status=AnalysisStatus.COMPLETE,
        analyzer_id="fixture-python-analyzer",
        analyzer_version="1",
        parser_kind="fixture-parser",
        text_xxh3_128=text_fingerprint.xxh3_128,
        text_xxh3_64_guard=text_fingerprint.xxh3_64_guard,
        normalized_xxh3_128=text_fingerprint.xxh3_128,
        token_xxh3_128=None,
        structure_xxh3_128=None,
        raw_xxh3_128=raw_fingerprint.xxh3_128,
        raw_xxh3_64_guard=raw_fingerprint.xxh3_64_guard,
        symbols=symbols,
        references=references,
        diagnostics=diagnostics,
        provenance={"fixture": True},
    )


def _hotspot(
    qualified_name: str,
    index: int,
    complexity: int,
    function_lines: int,
) -> tuple[SymbolRecord, tuple[DiagnosticRecord, DiagnosticRecord]]:
    source_range = _source_range(index, function_lines)
    name = qualified_name.rsplit(".", 1)[-1]
    symbol = SymbolRecord(
        "function",
        name,
        qualified_name,
        f"{name}()",
        source_range,
        visibility="private",
        complexity=complexity,
    )
    diagnostics = (
        DiagnosticRecord(
            "neocortex-python",
            "high_complexity",
            DiagnosticSeverity.WARNING,
            "confirmed complexity hotspot",
            source_range,
            tool_name="fixture-python-analyzer",
            tool_version="1",
            metadata={"value": complexity, "threshold": 15},
        ),
        DiagnosticRecord(
            "neocortex-python",
            "long_function",
            DiagnosticSeverity.WARNING,
            "confirmed long function",
            source_range,
            tool_name="fixture-python-analyzer",
            tool_version="1",
            metadata={"value": function_lines, "threshold": 200},
        ),
    )
    return symbol, diagnostics


def _store_hotspot_file(
    state: CodeState,
    root: Path,
    *,
    filename: str,
    identity: int,
    prefix: str,
    complexities: tuple[int, ...],
) -> None:
    symbols: list[SymbolRecord] = []
    diagnostics: list[DiagnosticRecord] = []
    for index, complexity in enumerate(complexities):
        symbol, evidence = _hotspot(
            f"pkg.{prefix}_{index}",
            index,
            complexity,
            300 - index * 5,
        )
        symbols.append(symbol)
        diagnostics.extend(evidence)
    state.store_analysis(
        _analysis(
            root / filename,
            identity,
            symbols=tuple(symbols),
            diagnostics=tuple(diagnostics),
        ),
        1,
    )


def _external_publication(state: CodeState, root: Path) -> ExternalEvidencePublication:
    version = RuffEvidenceProvider().tool_version()
    assert version is not None
    files = state.external_evidence_files(root)
    result_digest = (
        "external-result-v1:xxh3_128:"
        + fingerprint_text(canonical_json({"diagnostics": []})).xxh3_128
    )
    return ExternalEvidencePublication(
        "ruff",
        version,
        RUFF_CONFIGURATION_SIGNATURE,
        "completed",
        1,
        2,
        {
            "schema": EXTERNAL_EVIDENCE_SCHEMA,
            "root": str(root),
            "execution": "full",
            "configuration": _configuration_payload(),
            "input": {
                "signature": external_input_signature(files),
                "eligible_files": len(files),
                "total_bytes": sum(item.size for item in files),
                "version_ids": [item.version_id for item in files],
            },
            "result": {
                "digest": result_digest,
                "diagnostics": 0,
                "diagnostic_ids": [],
                "records": [],
                "comparable": False,
                "baseline_tool_run_id": None,
                "added": None,
                "resolved": None,
            },
            "mutation_authority": False,
            "content_executed": False,
        },
    )


def _build_state(
    state_directory: Path,
    *,
    hotspots: bool = True,
    external_evidence: bool = False,
) -> Path:
    state_directory.mkdir(parents=True)
    database = state_directory / "code.sqlite3"
    with CodeState(database) as state:
        analysis_run_id = state.begin_run(1, 1, PROCESSING_SIGNATURE)
        if hotspots:
            _store_hotspot_file(
                state,
                state_directory,
                filename="dominant.py",
                identity=100,
                prefix="compute_dominant",
                complexities=(60, 59, 58, 57, 56),
            )
            _store_hotspot_file(
                state,
                state_directory,
                filename="secondary.py",
                identity=101,
                prefix="secondary",
                complexities=(50, 49, 48),
            )
            for offset in range(7):
                _store_hotspot_file(
                    state,
                    state_directory,
                    filename=f"single_{offset}.py",
                    identity=110 + offset,
                    prefix=f"single_{offset}",
                    complexities=(45 - offset,),
                )
            for offset in range(3):
                caller_name = f"pkg.caller_{offset}"
                caller_range = _source_range(0, 3)
                caller_paths = (
                    Path("caller_0.py"),
                    Path("tests") / "caller_1.py",
                    Path("tests") / "fixtures" / "caller_2.py",
                )
                state.store_analysis(
                    _analysis(
                        state_directory / caller_paths[offset],
                        130 + offset,
                        symbols=(
                            SymbolRecord(
                                "function",
                                f"caller_{offset}",
                                caller_name,
                                f"caller_{offset}()",
                                caller_range,
                                visibility="public",
                                complexity=1,
                            ),
                        ),
                        references=(
                            ReferenceRecord(
                                "call",
                                "compute_dominant_0",
                                caller_range,
                                source_qualified_name=caller_name,
                                target_hint="pkg.compute_dominant_0",
                                confirmed=True,
                                confidence=1.0,
                                evidence="fixture-resolved-static-call",
                            ),
                        ),
                    ),
                    1,
                )
            state.finalize_graph(1)
            candidates = 12
        else:
            symbol = SymbolRecord(
                "function",
                "small",
                "pkg.small",
                "small()",
                _source_range(0, 3),
                visibility="private",
                complexity=1,
            )
            state.store_analysis(
                _analysis(
                    state_directory / "small.py",
                    200,
                    symbols=(symbol,),
                ),
                1,
            )
            state.finalize_graph(1)
            candidates = 1
        state.complete_run(
            analysis_run_id,
            {
                "candidates": candidates,
                "processed": candidates,
                "cache_hits": 0,
                "errors": 0,
            },
            partial=False,
            graph_current=True,
            external_evidence=(
                _external_publication(state, state_directory) if external_evidence else None
            ),
        )
        checkpoint_code_wal(state.connection)
    remove_checkpointed_code_sidecars(database)
    return database


def _status(
    root: Path,
    *,
    journal_status: str = "unchanged",
    current: bool = True,
) -> SelfAnalysisStatus:
    inventory_journal = "unavailable" if journal_status == "unavailable" else "ok"
    return SelfAnalysisStatus(
        "valid",
        {
            "run": {"root": str(root)},
            "inventory": {
                "mode": "full",
                "journal": {"status": inventory_journal},
            },
        },
        SelfAnalysisFreshness(
            True,
            True,
            current,
            journal_status,  # type: ignore[arg-type]
            current,
        ),
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_review_ranks_confirmed_hotspots_deterministically_with_diversity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        code_review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )

    first = review_code_state(state_directory)
    second = review_code_state(state_directory)
    expanded = review_code_state(state_directory, limit=11)
    first_json = json.dumps(first.as_payload(), ensure_ascii=True, sort_keys=True)
    second_json = json.dumps(second.as_payload(), ensure_ascii=True, sort_keys=True)

    assert first.status == "ready"
    assert first_json == second_json
    assert first.as_payload()["schema"] == "neocortex.code-review/v15"
    assert first.as_payload()["compatible_schemas"] == []
    assert first.supply_chain is not None
    assert first.supply_chain.status == "abstained"
    assert first.state_projection is not None
    assert first.state_projection.status == "abstained"
    assert first.state_projection.reason == (
        "document_state_not_configured_for_noncanonical_code_review"
    )
    assert first.state_topology is not None
    assert first.state_topology.status == "abstained"
    assert first.change_evolution is not None
    assert first.change_evolution.code_schema.status == "ready"
    assert first.assurance is not None
    assert first.assurance.status == "abstained"
    assert first.capability_reachability is not None
    assert first.capability_reachability.status == "abstained"
    assert first.interface_surface is not None
    assert first.interface_surface.status == "ready"
    assert len(first.findings) == 10
    assert len(expanded.findings) == 11
    assert expanded.findings[:10] == first.findings
    assert expanded.work_packages == first.work_packages
    assert first.work_package_status == "abstained"
    assert first.work_packages == ()
    assert max(Counter(finding.path for finding in first.findings).values()) == 2
    assert len({finding.path for finding in first.findings}) == 9
    assert first.findings[0].symbol == "pkg.compute_dominant_0"
    assert first.findings[0].resolved_static_callers == 3
    assert first.findings[0].impact.path_convention_production_callers == 1
    assert first.findings[0].impact.path_convention_test_callers == 1
    assert first.findings[0].impact.path_convention_fixture_callers == 1
    assert first.findings[0].construction == "unknown"
    assert first.findings[0].actionability == "characterize_first"
    assert first.findings[0].recommended_change is False
    assert first.findings[0].epistemic_state.observation_status == "confirmed"
    assert first.findings[0].epistemic_state.inference_status == "abstained"
    assert first.findings[0].epistemic_state.question_readiness == "ready"
    assert first.findings[0].epistemic_state.decision_readiness == "experiment_required"
    assert first.findings[0].epistemic_state.decision is None
    epistemics = first.as_payload()["epistemics"]
    assert isinstance(epistemics, dict)
    assert epistemics["schema"] == "neocortex.code-analysis-epistemics/v1"
    assert {item["question_id"] for item in epistemics["specs"]} == {
        "maintenance.structural_hotspot_requires_change",
        "state.text_semantic_published_projection_is_aligned",
        "state.text_terminal_publication_is_relationally_closed",
        "evolution.change_surface_requires_review",
        "evolution.change_history_requires_companion_review",
        "evolution.code_owner_schema_requires_migration_review",
        "analyzer.latest_publication_is_compared_to_git_visible_worktree",
        "analyzer.effectiveness_requires_independent_outcome_calibration",
        "security.static_invariants_and_vulnerability_evidence_is_resolved",
        "dependency.declaration_installation_and_license_evidence_is_resolved",
        "structure.module_surface_concentration_requires_characterization",
        "structure.configuration_inventory_requires_complete_parsing",
        "structure.static_cli_calls_require_runtime_contract_evidence",
        "assurance.evidence_providers_are_resolved",
        "capability.text_extract_evidence_provider_is_resolved",
        "architecture.static_import_graph_is_comparably_observed",
        "architecture.declared_import_contracts_are_evaluated",
        "architecture.logical_owner_mapping_is_explicitly_declared",
    }
    assert len(epistemics["evaluations"]) == len(first.findings) + 17
    first_evaluation = epistemics["evaluations"][0]
    assert first_evaluation["observation_status"] == "confirmed"
    assert first_evaluation["inference_status"] == "abstained"
    assert first_evaluation["question_readiness"] == "ready"
    assert first_evaluation["decision_readiness"] == "experiment_required"
    assert first_evaluation["decision"] is None
    assert first_evaluation["authority"] == "advisory"
    assert first_evaluation["mutation_authority"] is False
    assert {item["source_record_id"] for item in first_evaluation["evidence"]} == {
        str(item.diagnostic_id) for item in first.findings[0].diagnostics
    }
    assert first.recommendations == ()
    assert len(first.findings[0].callers) == 3
    assert {caller.path_convention_role for caller in first.findings[0].callers} == {
        "production",
        "test",
        "fixture",
    }
    assert {diagnostic.code for diagnostic in first.findings[0].diagnostics} == {
        "high_complexity",
        "long_function",
    }
    for finding in first.findings:
        expected_score = (
            finding.complexity_ratio_basis_points
            + finding.length_ratio_basis_points // 4
            + 250 * min(finding.resolved_static_callers, 20)
        )
        assert finding.score_basis_points == expected_score
        assert "dead" not in finding.category
    assert first.coverage is not None
    assert first.coverage.probable_dead_suppressed > 0
    assert first.test_coverage is not None
    assert first.test_coverage.status == "abstained"
    coverage_payload = first.as_payload()["test_coverage"]
    assert isinstance(coverage_payload, dict)
    assert coverage_payload["schema"] == "neocortex.code-coverage-analysis/v2"
    assert "test_coverage_not_ready:" in " ".join(first.limitations)
    assert first.engineering_analytics is not None
    assert first.engineering_analytics.status == "abstained"
    engineering_payload = first.as_payload()["engineering_analytics"]
    assert isinstance(engineering_payload, dict)
    assert engineering_payload["schema"] == "neocortex.code-engineering-analytics/v2"
    assert engineering_payload["aggregate_score"] is None
    assert engineering_payload["defect_probability"] is None
    assert [gate["gate"] for gate in engineering_payload["gates"]] == [
        "mutation_test_baseline",
        "mutation_measurement_complete",
        "mutation_score_recorded",
    ]
    assert first.digest is not None

    monkeypatch.setattr(
        code_review_module,
        "CODE_REVIEW_RANKING",
        "fixture-ranking-next",
    )
    reinterpreted = review_code_state(state_directory)
    assert [finding.hotspot_id for finding in reinterpreted.findings] == [
        finding.hotspot_id for finding in first.findings
    ]
    assert [finding.finding_id for finding in reinterpreted.findings] != [
        finding.finding_id for finding in first.findings
    ]


def test_review_rejects_current_rows_not_owned_by_the_latest_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        code_review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    database = state_directory / "code.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE file_versions SET last_observed_run_id=last_observed_run_id-1 "
            "WHERE version_id=(SELECT current_version_id FROM files "
            "WHERE status='current' ORDER BY file_id LIMIT 1)"
        )
        connection.commit()
        checkpoint_code_wal(connection)
    remove_checkpointed_code_sidecars(database)

    result = review_code_state(state_directory)

    assert result.status == "abstained"
    assert result.reason == "code_review_evidence_unresolvable"
    assert result.findings == ()
    assert result.question_specs == ()
    assert result.question_evaluations == ()


def test_review_abstains_when_its_evidence_resolver_cannot_verify_a_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        code_review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    monkeypatch.setattr(
        code_review_module,
        "resolve_code_review_questions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            CodeReviewEvidenceResolutionError("source record changed")
        ),
    )

    result = review_code_state(state_directory)

    assert result.status == "abstained"
    assert result.reason == "code_review_evidence_unresolvable"
    assert result.findings == ()
    assert result.question_evaluations == ()
    assert result.digest is None


def test_evidence_resolver_rejects_a_source_record_changed_after_review_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    database = _build_state(state_directory)
    monkeypatch.setattr(
        code_review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    result = review_code_state(state_directory, limit=1)
    assert result.snapshot is not None
    diagnostic_id = result.findings[0].diagnostics[0].diagnostic_id
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE diagnostics SET source='forged-after-read' WHERE diagnostic_id=?",
            (diagnostic_id,),
        )
        connection.commit()
        checkpoint_code_wal(connection)
    remove_checkpointed_code_sidecars(database)

    with readonly_code_database(database) as connection:
        with pytest.raises(
            CodeReviewEvidenceResolutionError,
            match="disagrees with its source record",
        ):
            resolve_code_review_questions(
                connection,
                result.findings,
                result.snapshot,
                class_limit=1,
            )


def test_review_exposes_advisory_ruff_evidence_and_package_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    _build_state(state_directory, external_evidence=True)
    monkeypatch.setattr(
        code_review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(state_directory),
    )

    result = review_code_state(state_directory)
    payload = result.as_payload()

    assert result.status == "ready"
    assert result.external_evidence.status == "ready"
    assert result.external_evidence.gate == "baseline"
    assert payload["external_evidence"]["authority"] == "advisory"
    assert payload["external_evidence"]["mutation_authority"] is False
    assert "ruff_external_evidence_baseline_only" in result.limitations
    assert result.recommendations == ()
    assert result.work_packages == ()


def test_observational_findings_do_not_create_hidden_change_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        code_review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    limited = review_code_state(state_directory, limit=1)
    planning_view = review_code_state(state_directory, limit=50)

    assert limited.findings[0].actionability == "characterize_first"
    assert limited.recommendations == ()
    assert planning_view.recommendations == ()
    assert limited.work_package_status == "abstained"
    assert limited.work_packages == ()


@pytest.mark.parametrize("limit", [0, 51, True])
def test_review_rejects_an_unbounded_calibration_limit(
    tmp_path: Path,
    limit: object,
) -> None:
    with pytest.raises(
        ValueError,
        match="code review limit must be between 1 and 50",
    ):
        review_code_state(tmp_path, limit=limit)  # type: ignore[arg-type]


def test_review_freshness_is_fail_closed_with_portable_full_snapshot_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    _build_state(state_directory)

    monkeypatch.setattr(
        code_review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(
            tmp_path,
            journal_status="advanced",
            current=False,
        ),
    )
    advanced = review_code_state(state_directory)
    assert advanced.status == "abstained"
    assert advanced.reason == "self_analysis_journal_advanced"

    monkeypatch.setattr(
        code_review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(
            tmp_path,
            journal_status="unavailable",
            current=False,
        ),
    )
    portable = review_code_state(state_directory)
    assert portable.status == "ready"
    assert portable.snapshot is not None
    assert portable.snapshot.freshness == "publication_only"
    assert portable.snapshot.current is False
    assert "live_tree_freshness_not_proven_without_journal" in portable.limitations


def test_review_with_zero_hotspots_is_ready_and_does_not_mutate_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    database = _build_state(state_directory, hotspots=False)
    monkeypatch.setattr(
        code_review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    before_files = tuple(sorted(path.name for path in state_directory.iterdir()))
    before_digest = _digest(database)

    result = review_code_state(state_directory)

    assert result.status == "ready"
    assert result.findings == ()
    assert result.recommendation_status == "abstained"
    assert result.recommendation_reason == (
        "no_evidence_ready_change_decision_within_bounded_findings"
    )
    assert result.recommendations == ()
    assert result.work_package_status == "abstained"
    assert result.work_package_reason == (
        "no_evidence_ready_change_or_calibrated_characterization_candidate"
    )
    assert result.work_packages == ()
    assert result.coverage is not None
    assert result.coverage.candidate_hotspots == 0
    assert result.digest is not None
    assert tuple(sorted(path.name for path in state_directory.iterdir())) == before_files
    assert _digest(database) == before_digest
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
