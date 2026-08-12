"""Calibration and relationship coverage for Code review work packages."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from _04_Nucleo_Operativo.code_architecture_analysis import (
    ArchitectureContract,
    ArchitectureImportEdge,
    ArchitectureModule,
    CodeArchitectureAnalysis,
)
from _04_Nucleo_Operativo.code_coverage_analysis import (
    CODE_COVERAGE_PROVIDER_ID,
    CodeCoverageAnalysis,
    CoverageGateEvaluation,
    CoverageScopeSummary,
    CoverageTestOutcomes,
    CoverageToolVersion,
    CoverageTotals,
    TestToSymbolRelation as CoverageTestToSymbolRelation,
)
from _04_Nucleo_Operativo.code_engineering_analytics import (
    CodeEngineeringAnalytics,
    EngineeringDimension,
    EngineeringGate,
    EngineeringMetric,
    EngineeringProviderSummary,
    ModuleEngineeringProfile,
)
from _04_Nucleo_Operativo.code_review_models import (
    CodeReviewDiagnostic,
    CodeReviewFinding,
    CodeReviewImpact,
    bounded_code_coverage_payload,
    bounded_code_engineering_payload,
    bounded_code_review_work_package_payload,
    bounded_code_unused_payload,
    build_code_review_recommendations,
)
from _04_Nucleo_Operativo.code_review_actionability import CodeReviewEpistemicState
from _04_Nucleo_Operativo.code_review_work_packages import (
    build_code_review_work_packages,
    plan_code_review_work_packages,
    read_code_review_planning_links,
)
from _04_Nucleo_Operativo.code_unused_analysis import (
    UnusedCalibrationSample,
    UnusedConsensusCandidate,
    UnusedEvidenceSignals,
    UnusedProviderStatus,
    analyze_code_unused,
    build_unused_analysis,
)


HISTORY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "code_review" / "rc14_rc19_work_package_outcomes_v1.json"
)


def _engineering_dimension(
    dimension: str,
    module_id: str,
    *,
    ready: bool,
    metric_count: int = 1,
) -> EngineeringDimension:
    return EngineeringDimension(
        dimension,  # type: ignore[arg-type]
        "ready" if ready else "not_recorded",
        None if ready else f"{dimension}_not_recorded",
        (
            tuple(
                EngineeringMetric(
                    f"{dimension}_{index:02d}",
                    float(index),
                    "count",
                    "fixture",
                    "module",
                    module_id,
                )
                for index in range(metric_count)
            )
            if ready
            else ()
        ),
        ("fixture",),
        (f"{dimension}_is_advisory",),
    )


def _engineering_profile(
    module_id: str,
    *,
    complete: bool,
    metric_count: int = 1,
) -> ModuleEngineeringProfile:
    return ModuleEngineeringProfile(
        module_id,
        "fixture-owner",
        _engineering_dimension("complexity", module_id, ready=True, metric_count=metric_count),
        _engineering_dimension("coverage", module_id, ready=True, metric_count=metric_count),
        _engineering_dimension("mutation", module_id, ready=complete, metric_count=metric_count),
        _engineering_dimension("history", module_id, ready=complete, metric_count=metric_count),
        _engineering_dimension("graph", module_id, ready=True, metric_count=metric_count),
    )


def _engineering_analysis(
    module_ids: tuple[str, ...],
    *,
    complete: bool,
    metric_count: int = 1,
) -> CodeEngineeringAnalytics:
    gate_status = "passed" if complete else "not_evaluated"
    gate_reason = None if complete else "mutation_provider_not_ready"
    return CodeEngineeringAnalytics(
        "fixture",
        1,
        "ready" if complete else "partial",
        None if complete else "one_or_more_engineering_dimensions_not_ready",
        (
            EngineeringProviderSummary(
                "git-history-local",
                "ready" if complete else "not_recorded",
                None if complete else "provider_missing",
                1 if complete else None,
                1 if complete else None,
                1 if complete else 0,
                0,
            ),
            EngineeringProviderSummary(
                "cosmic-ray-focal-mutation",
                "ready" if complete else "not_recorded",
                None if complete else "provider_missing",
                2 if complete else None,
                2 if complete else None,
                1 if complete else 0,
                0,
            ),
        ),
        tuple(
            _engineering_profile(
                module_id,
                complete=complete,
                metric_count=metric_count,
            )
            for module_id in module_ids
        ),
        (
            EngineeringGate("mutation_test_baseline", gate_status, gate_reason),
            EngineeringGate("mutation_measurement_complete", gate_status, gate_reason),
            EngineeringGate("mutation_score_recorded", gate_status, gate_reason),
        ),
        "fixture-scope" if complete else None,
        0.75 if complete else None,
        (
            "no_aggregate_score_is_computed",
            "no_dimension_is_a_defect_probability",
            "mutation_findings_are_advisory_and_have_zero_mutation_authority",
        ),
        "code-engineering-v1:xxh3_128:fixture",
    )


def _impact(symbol: str) -> CodeReviewImpact:
    return CodeReviewImpact(
        call_sites=2,
        path_convention_production_callers=1,
        path_convention_test_callers=1,
        path_convention_fixture_callers=0,
        path_convention_tool_callers=0,
        path_convention_compatibility_callers=0,
        resolved_static_consumer_files=2,
        path_convention_production_consumer_files=1,
        path_convention_test_consumer_files=1,
        consumer_file_examples=(f"C:\\repo\\consumer_{symbol}.py",),
    )


def _finding(
    rank: int,
    symbol: str,
    path: str,
) -> CodeReviewFinding:
    epistemic_state = CodeReviewEpistemicState(
        question_id="maintenance.structural_hotspot_requires_change",
        question_version="v1",
        observation_status="confirmed",
        observations=(
            "cyclomatic_complexity_threshold_met_or_exceeded:20000bp",
            "function_length_threshold_met_or_exceeded:11550bp",
            "path_convention_production_callers:1",
            "path_convention_test_or_fixture_callers:1",
            "resolved_static_consumer_files:2",
        ),
        inference_status="abstained",
        inferences=(),
        hypotheses=(
            "structural_hotspot_may_increase_maintenance_cost",
            "structure_may_be_intentional_or_cohesive",
        ),
        question_readiness="ready",
        question_reason=None,
        required_evidence=(
            "confirmed_structural_hotspot",
            "behavior_or_contract_problem_observed",
            "counterevidence_evaluated",
            "discriminating_experiment_result",
        ),
        satisfied_evidence=("confirmed_structural_hotspot",),
        missing_evidence=(
            "behavior_or_contract_problem_observed",
            "counterevidence_evaluated",
            "discriminating_experiment_result",
        ),
        counterevidence_status="not_evaluated",
        counterevidence_to_seek=(
            "intentional_or_cohesive_structure",
            "existing_assurance_for_observed_behavior",
            "change_cost_or_risk_exceeds_verified_benefit",
        ),
        decision_readiness="experiment_required",
        decision=None,
        decision_reason="structural_observation_alone_cannot_justify_change",
        next_actions=(
            "characterize_exact_behavior_and_contracts",
            "seek_counterevidence",
            "design_lowest_cost_discriminating_experiment",
        ),
    )
    return CodeReviewFinding(
        finding_id=f"finding:{symbol}",
        hotspot_id=f"hotspot:{symbol}",
        rank=rank,
        category="complex_and_long_hotspot",
        path=path,
        symbol=symbol,
        symbol_kind="function",
        signature=f"{symbol.rsplit('.', 1)[-1]}()",
        start_line=10,
        end_line=240,
        start_column=0,
        end_column=0,
        start_byte=100,
        end_byte=2_000,
        complexity=30,
        function_lines=231,
        complexity_ratio_basis_points=20_000,
        length_ratio_basis_points=11_550,
        score_basis_points=23_137,
        incoming_references=2,
        incoming_calls=2,
        resolved_static_callers=2,
        impact=_impact(symbol),
        path_convention_role="production",
        construction="unknown",
        actionability="characterize_first",
        change_risk="unknown",
        recommended_change=False,
        epistemic_state=epistemic_state,
        actionability_evidence=(
            "source_role_path_convention:production",
            "semantic_construction:abstained:not_observed",
            "question_readiness:ready",
            "decision_readiness:experiment_required",
            *epistemic_state.observations,
        ),
        contracts_to_preserve=(),
        recommended_validation=(),
        analyzer_id="fixture-python",
        analyzer_version="1",
        file_xxh3_128="a" * 32,
        file_xxh3_64_guard="b" * 16,
        diagnostics=(
            CodeReviewDiagnostic(
                diagnostic_id=rank * 2 - 1,
                code="high_complexity",
                value=30,
                threshold=15,
                source="fixture",
                tool_name="fixture-analyzer",
                tool_version="1",
                confirmed=True,
                confidence=1.0,
            ),
            CodeReviewDiagnostic(
                diagnostic_id=rank * 2,
                code="long_function",
                value=231,
                threshold=200,
                source="fixture",
                tool_name="fixture-analyzer",
                tool_version="1",
                confirmed=True,
                confidence=1.0,
            ),
        ),
        callers=(),
        reasons=(
            "confirmed_cyclomatic_complexity:30",
            "confirmed_function_lines:231",
            "resolved_static_callers:2",
        ),
    )


def _planning_findings() -> tuple[CodeReviewFinding, ...]:
    return (
        _finding(
            1,
            "document_taxonomy.classify_document",
            r"C:\repo\document_taxonomy.py",
        ),
        _finding(
            2,
            "document_taxonomy_kinds._normative_document_evidence",
            r"C:\repo\document_taxonomy_kinds.py",
        ),
        _finding(
            3,
            "document_taxonomy_references._plausible_authority_identifier",
            r"C:\repo\document_taxonomy_references.py",
        ),
        _finding(
            4,
            "document_taxonomy_overlay.load_overlay",
            r"C:\repo\document_taxonomy_overlay.py",
        ),
        _finding(
            5,
            "knowledge_exact.lookup_exact",
            r"C:\repo\knowledge_exact.py",
        ),
    )


def _create_graph(*, project_root: str = r"C:\repo") -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE files(
          file_id INTEGER PRIMARY KEY,current_version_id INTEGER,status TEXT,
          current_path TEXT
        );
        CREATE TABLE file_versions(
          version_id INTEGER PRIMARY KEY,invalidated_ns INTEGER,
          analysis_status TEXT,language TEXT,generated INTEGER,vendored INTEGER
        );
        CREATE TABLE symbols(
          symbol_id INTEGER PRIMARY KEY,version_id INTEGER,qualified_name TEXT,
          confirmed INTEGER
        );
        CREATE TABLE code_references(
          reference_id INTEGER PRIMARY KEY,source_symbol_id INTEGER,
          target_symbol_id INTEGER,kind TEXT,confirmed INTEGER,
          confidence REAL,evidence TEXT
        );
        CREATE TABLE projects(
          project_id INTEGER PRIMARY KEY,probable_root TEXT,status TEXT
        );
        CREATE TABLE project_memberships(
          version_id INTEGER,project_id INTEGER,selected INTEGER,confidence REAL
        );
        """
    )
    connection.execute(
        "INSERT INTO projects VALUES(1,?,'current')",
        (project_root,),
    )
    symbols = (
        (1, "document_taxonomy.classify_document", "document_taxonomy.py"),
        (2, "document_taxonomy._kind_evidence", "document_taxonomy.py"),
        (3, "document_taxonomy_kinds._normative_document_evidence", "kinds.py"),
        (4, "document_taxonomy_references._plausible_authority_identifier", "refs.py"),
        (5, "document_taxonomy_overlay.load_overlay", "overlay.py"),
        (6, "knowledge_exact.lookup_exact", "knowledge_exact.py"),
    )
    for symbol_id, qualified_name, filename in symbols:
        connection.execute(
            "INSERT INTO file_versions VALUES(?,NULL,'complete','python',0,0)",
            (symbol_id,),
        )
        connection.execute(
            "INSERT INTO files VALUES(?,?,'current',?)",
            (symbol_id, symbol_id, f"C:\\repo\\{filename}"),
        )
        connection.execute(
            "INSERT INTO symbols VALUES(?,?,?,1)",
            (symbol_id, symbol_id, qualified_name),
        )
        connection.execute(
            "INSERT INTO project_memberships VALUES(?,1,1,1.0)",
            (symbol_id,),
        )
    references = (
        (1, 1, 2, 0.9, "root-to-kind"),
        (2, 2, 3, 0.9, "kind-to-normative"),
        (3, 1, 4, 1.0, "root-to-authority-guard"),
    )
    connection.executemany(
        "INSERT INTO code_references VALUES(?,?,?,'call',1,?,?)",
        references,
    )
    return connection


def test_relationship_reader_observes_links_but_cannot_create_a_hotspot_package() -> None:
    findings = _planning_findings()
    recommendations = build_code_review_recommendations(findings, limit=3)
    with _create_graph() as connection:
        links = read_code_review_planning_links(
            connection,
            {
                1: findings[0].finding_id,
                3: findings[1].finding_id,
                4: findings[2].finding_id,
                5: findings[3].finding_id,
                6: findings[4].finding_id,
            },
        )

    packages = build_code_review_work_packages(findings, recommendations, links)
    repeated = build_code_review_work_packages(findings, recommendations, links)

    assert packages == repeated
    assert recommendations == ()
    assert packages == ()
    assert {(link.source_finding_id, link.target_finding_id) for link in links} == {
        (findings[0].finding_id, findings[1].finding_id),
        (findings[0].finding_id, findings[2].finding_id),
    }


def test_link_reader_collapses_repeated_calls_before_the_pair_bound() -> None:
    findings = _planning_findings()
    with _create_graph() as connection:
        reference_id = 10
        repeated = []
        for index in range(101):
            repeated.append((reference_id, 1, 2, 0.8, f"root-kind-{index:03d}"))
            reference_id += 1
            repeated.append((reference_id, 2, 3, 0.8, f"kind-target-{index:03d}"))
            reference_id += 1
        connection.executemany(
            "INSERT INTO code_references VALUES(?,?,?,'call',1,?,?)",
            repeated,
        )
        links = read_code_review_planning_links(
            connection,
            {
                1: findings[0].finding_id,
                3: findings[1].finding_id,
                4: findings[2].finding_id,
            },
        )

    assert {(link.source_finding_id, link.target_finding_id) for link in links} == {
        (findings[0].finding_id, findings[1].finding_id),
        (findings[0].finding_id, findings[2].finding_id),
    }


def test_bridge_role_is_relative_to_its_project_root() -> None:
    findings = _planning_findings()
    project_root = r"C:\work\tests\app"
    with _create_graph(project_root=project_root) as connection:
        connection.execute(
            "UPDATE files SET current_path=REPLACE(current_path,'C:\\repo',?)",
            (project_root,),
        )
        links = read_code_review_planning_links(
            connection,
            {
                1: findings[0].finding_id,
                3: findings[1].finding_id,
            },
        )

    assert len(links) == 1
    assert links[0].target_finding_id == findings[1].finding_id
    assert links[0].via_symbol == "document_taxonomy._kind_evidence"


def test_planner_abstains_without_an_evidence_resolved_recommendation() -> None:
    findings = _planning_findings()[:2]

    assert build_code_review_recommendations(findings, limit=2) == ()
    assert build_code_review_work_packages(findings, (), ()) == ()


def _unused_candidate(
    *,
    state: str = "probable_unused_high_consensus",
) -> UnusedConsensusCandidate:
    return UnusedConsensusCandidate(
        candidate_id="unused:document_taxonomy.classify_document",
        version_id=1,
        symbol_id=1,
        relative_path="document_taxonomy.py",
        module_id="document_taxonomy",
        symbol="document_taxonomy.classify_document",
        name="classify_document",
        kind="function",
        start_line=10,
        end_line=240,
        state=state,  # type: ignore[arg-type]
        provider_ids=("pyright-trusted-project", "vulture-unused-static"),
        signals=UnusedEvidenceSignals(
            vulture_reported=True,
            vulture_confidence=1.0,
            pyright_reported=True,
            vulture_complete=True,
            pyright_complete=True,
            providers_aligned=True,
            graph_references=0,
            graph_calls=0,
            graph_imports=0,
            in_all=False,
            reexported=False,
            entry_point=False,
            callback=False,
            registry=False,
            fixture=False,
            protocol=False,
            special=False,
            coverage_observed=False,
            coverage_status="missing",
            evidence_ids=("fixture-unused-evidence",),
        ),
        reasons=(
            "vulture_high_confidence",
            "pyright_reported_unused",
            "no_observed_usage_or_dynamic_contract",
        ),
        evidence=("fixture-unused-evidence",),
        limitations=("dynamic_usage_not_observed",),
    )


def _unused_analysis(*candidates: UnusedConsensusCandidate):
    providers = tuple(
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
    return build_unused_analysis(candidates, providers=providers)


def test_hotspot_factory_does_not_smuggle_unused_evidence_into_a_change_package() -> None:
    findings = _planning_findings()
    recommendations = build_code_review_recommendations(findings, limit=1)
    packages = build_code_review_work_packages(
        findings,
        recommendations,
        (),
        unused_analysis=_unused_analysis(_unused_candidate()),  # type: ignore[arg-type]
    )

    assert recommendations == ()
    assert packages == ()


def test_high_consensus_unused_candidate_gets_characterization_only_package() -> None:
    packages, status, reason = plan_code_review_work_packages(
        (),
        (),
        (),
        unused_analysis=_unused_analysis(_unused_candidate()),  # type: ignore[arg-type]
    )

    assert status == "ready"
    assert reason is None
    assert len(packages) == 1
    package = packages[0]
    assert package.package_kind == "unused_characterization"
    assert package.objective == "characterize_high_consensus_unused_candidate_without_mutation"
    assert all(step.phase != "change" for step in package.steps)
    assert package.requires_human_confirmation is True
    assert package.mutation_authority is False
    assert "human_confirmation_recorded" in package.acceptance_gates


def test_non_high_consensus_unused_states_never_create_characterization_packages() -> None:
    candidates = tuple(
        _unused_candidate(state=state)
        for state in (
            "explained_usage",
            "dynamic_usage_possible",
            "insufficient_evidence",
        )
    )
    packages, status, reason = plan_code_review_work_packages(
        (),
        (),
        (),
        unused_analysis=_unused_analysis(*candidates),  # type: ignore[arg-type]
    )

    assert packages == ()
    assert status == "abstained"
    assert reason == "no_evidence_ready_change_or_calibrated_characterization_candidate"


def test_unused_characterization_requires_a_passed_calibration_gate() -> None:
    candidate = _unused_candidate()
    analysis = analyze_code_unused(
        (candidate,),
        provider_signature="unused-provider-suite-fixture-v1",
        calibration_samples=(UnusedCalibrationSample("false-positive", "used", candidate.signals),),
    )

    packages, status, _reason = plan_code_review_work_packages(
        (),
        (),
        (),
        unused_analysis=analysis,
    )

    assert packages == ()
    assert status == "abstained"


def test_review_unused_payload_bounds_candidates_and_nested_evidence() -> None:
    evidence_ids = tuple(f"evidence-{index:02d}" for index in range(25))
    signals = UnusedEvidenceSignals(
        vulture_reported=True,
        vulture_confidence=1.0,
        pyright_reported=True,
        vulture_complete=True,
        pyright_complete=True,
        providers_aligned=True,
        graph_references=0,
        graph_calls=0,
        graph_imports=0,
        in_all=False,
        reexported=False,
        entry_point=False,
        callback=False,
        registry=False,
        fixture=False,
        protocol=False,
        special=False,
        coverage_observed=False,
        coverage_status="missing",
        evidence_ids=evidence_ids,
    )
    candidates = tuple(
        UnusedConsensusCandidate(
            candidate_id=f"candidate-{index:02d}",
            version_id=index + 1,
            symbol_id=index + 1,
            relative_path=f"pkg/module_{index:02d}.py",
            module_id=f"pkg.module_{index:02d}",
            symbol=f"pkg.module_{index:02d}.unused",
            name="unused",
            kind="function",
            start_line=1,
            end_line=2,
            state="probable_unused_high_consensus",
            provider_ids=("pyright-trusted-project", "vulture-unused-static"),
            signals=signals,
            reasons=(
                "vulture_high_confidence",
                "pyright_reported_unused",
                "no_observed_usage_or_dynamic_contract",
            ),
            evidence=evidence_ids,
            limitations=evidence_ids,
        )
        for index in range(21)
    )
    analysis = analyze_code_unused(candidates, provider_signature="providers-v1")

    payload = bounded_code_unused_payload(analysis)

    assert payload["candidates_total"] == 21
    assert payload["candidates_truncated"] is True
    payload_candidates = payload["candidates"]
    assert isinstance(payload_candidates, list)
    assert len(payload_candidates) == 20
    first = payload_candidates[0]
    assert isinstance(first, dict)
    assert first["evidence_total"] == 25
    assert first["evidence_truncated"] is True
    bounded_evidence = first["evidence"]
    assert isinstance(bounded_evidence, list)
    assert len(bounded_evidence) == 20
    bounded_signals = first["signals"]
    assert isinstance(bounded_signals, dict)
    assert bounded_signals["evidence_ids_total"] == 25
    assert bounded_signals["evidence_ids_truncated"] is True


def test_unused_package_adds_bounded_architecture_context_without_changing_identity() -> None:
    findings = _planning_findings()
    architecture = CodeArchitectureAnalysis(
        database="fixture",
        analysis_run_id=1,
        status="ready",
        reason=None,
        gate="observed",
        gates=(),
        providers=(),
        summary=None,
        modules=(
            ArchitectureModule(
                "consumer",
                0,
                1,
                3.0,
                3.0,
                1,
                (),
                ("layers",),
                None,
                None,
                None,
                None,
            ),
            ArchitectureModule(
                "document_taxonomy",
                1,
                0,
                20.0,
                12.0,
                2,
                (),
                ("layers",),
                None,
                None,
                None,
                None,
            ),
        ),
        symbols=(),
        imports=(
            ArchitectureImportEdge(
                "consumer",
                "document_taxonomy",
                "both",
                True,
                True,
                True,
                1.0,
            ),
        ),
        cycles=(),
        contracts=(
            ArchitectureContract(
                "layers",
                "failed",
                True,
                1,
                ("consumer",),
                ("document_taxonomy",),
                (("consumer", "document_taxonomy"),),
                "neocortex.architecture-contract/v1",
            ),
        ),
        limitations=(),
    )
    engineering = _engineering_analysis(("document_taxonomy",), complete=True)

    legacy = plan_code_review_work_packages(
        findings,
        (),
        (),
        unused_analysis=_unused_analysis(_unused_candidate()),  # type: ignore[arg-type]
    )[0][0]
    enriched = plan_code_review_work_packages(
        findings,
        (),
        (),
        architecture=architecture,
        engineering_analytics=engineering,
        unused_analysis=_unused_analysis(_unused_candidate()),  # type: ignore[arg-type]
    )[0][0]

    assert enriched.package_id == legacy.package_id
    assert enriched.primary_module == "document_taxonomy"
    assert enriched.import_chains == (("consumer", "document_taxonomy"),)
    assert enriched.affected_architecture_contracts == ("layers",)
    assert {
        "architecture_contracts_not_degraded",
        "no_new_import_cycles",
    }.issubset(enriched.acceptance_gates)
    assert "architecture:ready" in enriched.evidence
    assert enriched.engineering_profile == engineering.modules[0]
    assert [gate.gate for gate in enriched.engineering_gates] == [
        "mutation_test_baseline",
        "mutation_measurement_complete",
        "mutation_score_recorded",
    ]
    assert {gate.status for gate in enriched.engineering_gates} == {"passed"}
    assert "engineering:ready" in enriched.evidence


def test_partial_engineering_evidence_never_hides_a_work_package() -> None:
    findings = _planning_findings()
    engineering = _engineering_analysis(("document_taxonomy",), complete=False)

    packages = plan_code_review_work_packages(
        findings,
        (),
        (),
        engineering_analytics=engineering,
        unused_analysis=_unused_analysis(_unused_candidate()),  # type: ignore[arg-type]
    )[0]

    assert len(packages) == 1
    package = packages[0]
    assert package.engineering_profile is engineering.modules[0]
    assert package.engineering_profile.mutation.status == "not_recorded"
    assert package.engineering_profile.history.status == "not_recorded"
    assert {gate.status for gate in package.engineering_gates} == {"not_evaluated"}
    assert "engineering:partial" in package.evidence
    assert any(item.startswith("engineering_analytics_not_ready:") for item in package.limitations)
    assert package.mutation_authority is False


def test_work_package_projects_protecting_tests_and_missing_target_coverage() -> None:
    findings = _planning_findings()
    subject_key = "symbol:document_taxonomy.classify_document:10:240"
    totals = CoverageTotals(20, 17, 3, 8, 6, 2, 85.0, 75.0)
    coverage = CodeCoverageAnalysis(
        database="fixture",
        analysis_run_id=1,
        provider_id=CODE_COVERAGE_PROVIDER_ID,
        tool_run_id=1,
        effective_tool_run_id=1,
        status="ready",
        reason=None,
        suite_selection="selected",
        measurement_complete=True,
        content_executed=True,
        tool_versions=(CoverageToolVersion("coverage", "7.14.1"),),
        suite_signature="suite",
        configuration_signature="configuration",
        measurement_scope_signature="scope",
        outcomes=CoverageTestOutcomes(2, 2, 2, 0, 0),
        totals=totals,
        modules=(),
        symbols=(
            CoverageScopeSummary(
                "symbol",
                subject_key,
                "document_taxonomy",
                subject_key,
                "classify_document",
                10,
                240,
                "document_taxonomy.py",
                totals,
                ((30, 31), (80, 80)),
                ((25, 30), (70, 80)),
                False,
                False,
                ("tests/test_document_taxonomy.py::test_classify",),
            ),
        ),
        test_relations=(
            CoverageTestToSymbolRelation(
                "relation:classify",
                "test:test_classify",
                subject_key,
                ("tests/test_document_taxonomy.py::test_classify",),
                (10, 11, 12),
                ("tests/test_document_taxonomy.py::test_classify|run",),
                "document_taxonomy.py",
                "document_taxonomy",
                subject_key,
            ),
        ),
        failed_test_nodeids=(),
        gates=(
            CoverageGateEvaluation("tests_passed", "passed", None),
            CoverageGateEvaluation("coverage_available", "passed", None),
        ),
        limitations=("selected_suite_is_not_claimed_as_full_project_coverage",),
    )

    legacy = plan_code_review_work_packages(
        findings,
        (),
        (),
        unused_analysis=_unused_analysis(_unused_candidate()),  # type: ignore[arg-type]
    )[0][0]
    package = plan_code_review_work_packages(
        findings,
        (),
        (),
        test_coverage=coverage,
        unused_analysis=_unused_analysis(_unused_candidate()),  # type: ignore[arg-type]
    )[0][0]

    assert package.package_id == legacy.package_id
    assert package.test_coverage is not None
    assert package.test_coverage.status == "protected"
    assert package.test_coverage.primary_symbol == subject_key
    assert package.test_coverage.protecting_tests == (
        "tests/test_document_taxonomy.py::test_classify",
    )
    assert package.test_coverage.gate.status == "passed"
    assert package.test_coverage_scope is not None
    assert package.test_coverage_scope.missing_line_ranges == ((30, 31), (80, 80))
    assert package.test_coverage_scope.missing_branch_arcs == ((25, 30), (70, 80))
    assert "work_package_target_protected" not in package.acceptance_gates
    assert "coverage_gates_require_ready_trusted_deep_evidence" not in package.limitations


def test_review_json_bounds_coverage_and_work_package_examples_to_twenty() -> None:
    findings = _planning_findings()
    subject_key = "symbol:document_taxonomy.classify_document:10:240"
    tests = tuple(f"tests/test_many.py::test_{index:02d}" for index in range(25))
    ranges = tuple((index, index) for index in range(1, 26))
    arcs = tuple((index, index + 1) for index in range(1, 26))
    totals = CoverageTotals(100, 75, 25, 50, 25, 25, 75.0, 50.0)

    def scope(index: int, *, primary: bool = False) -> CoverageScopeSummary:
        key = subject_key if primary else f"symbol:module_{index}.target:1:5"
        return CoverageScopeSummary(
            "symbol",
            key,
            "document_taxonomy" if primary else f"module_{index}",
            key,
            "classify_document" if primary else "target",
            10 if primary else 1,
            240 if primary else 5,
            "document_taxonomy.py" if primary else f"module_{index}.py",
            totals,
            ranges,
            arcs,
            False,
            False,
            tests,
        )

    modules = tuple(
        CoverageScopeSummary(
            "module",
            f"module:{index}",
            f"module_{index}",
            None,
            None,
            None,
            None,
            f"module_{index}.py",
            totals,
            ranges,
            arcs,
            False,
            False,
            tests,
        )
        for index in range(25)
    )
    symbols = (scope(0, primary=True), *(scope(index) for index in range(1, 25)))
    relations = tuple(
        CoverageTestToSymbolRelation(
            f"relation:{index:02d}",
            f"test:{index:02d}",
            subject_key,
            (tests[index],),
            tuple(range(1, 26)),
            tuple(f"context:{item:02d}" for item in range(25)),
            "document_taxonomy.py",
            "document_taxonomy",
            subject_key,
        )
        for index in range(25)
    )
    coverage = CodeCoverageAnalysis(
        "fixture",
        1,
        CODE_COVERAGE_PROVIDER_ID,
        1,
        1,
        "ready",
        None,
        "selected",
        True,
        True,
        (CoverageToolVersion("coverage", "7.14.1"),),
        "suite",
        "configuration",
        "scope",
        CoverageTestOutcomes(25, 25, 25, 0, 0),
        totals,
        modules,
        symbols,
        relations,
        tests,
        (
            CoverageGateEvaluation("tests_passed", "passed", None),
            CoverageGateEvaluation("coverage_available", "passed", None),
        ),
        (),
    )
    engineering = _engineering_analysis(
        ("document_taxonomy", *(f"module_{index}" for index in range(1, 25))),
        complete=True,
        metric_count=25,
    )
    package = plan_code_review_work_packages(
        findings,
        (),
        (),
        test_coverage=coverage,
        engineering_analytics=engineering,
        unused_analysis=_unused_analysis(_unused_candidate()),  # type: ignore[arg-type]
    )[0][0]
    coverage_payload = bounded_code_coverage_payload(coverage)
    engineering_payload = bounded_code_engineering_payload(engineering)
    packages_payload = [bounded_code_review_work_package_payload(package)]
    assert isinstance(coverage_payload, dict)
    assert isinstance(packages_payload, list)
    assert len(coverage_payload["failed_test_examples"]) == 20
    assert coverage_payload["failed_test_examples_truncated"] is True
    assert len(coverage_payload["module_missing_examples"]) == 20
    assert coverage_payload["module_missing_examples_truncated"] is True
    assert len(coverage_payload["symbol_missing_examples"]) == 20
    assert coverage_payload["symbol_missing_examples_truncated"] is True
    assert len(coverage_payload["test_relation_examples"]) == 20
    assert coverage_payload["test_relation_examples_truncated"] is True
    package_payload = packages_payload[0]
    package_coverage = package_payload["test_coverage"]
    package_scope = package_payload["test_coverage_scope"]
    assert len(package_coverage["protecting_tests"]) == 20
    assert package_coverage["protecting_tests_total"] == 25
    assert package_coverage["protecting_tests_truncated"] is True
    assert len(package_coverage["relation_ids"]) == 20
    assert package_coverage["relation_ids_total"] == 25
    assert package_coverage["relation_ids_truncated"] is True
    assert len(package_scope["missing_line_ranges"]) == 20
    assert package_scope["missing_line_ranges_total"] == 25
    assert package_scope["missing_line_ranges_truncated"] is True
    assert len(package_scope["missing_branch_arcs"]) == 20
    assert package_scope["missing_branch_arcs_total"] == 25
    assert package_scope["missing_branch_arcs_truncated"] is True
    assert len(engineering_payload["modules"]) == 20
    assert engineering_payload["modules_total"] == 25
    assert engineering_payload["modules_truncated"] is True
    package_engineering = package_payload["engineering_profile"]
    assert package_engineering["module_id"] == "document_taxonomy"
    assert len(package_engineering["mutation"]["metrics"]) == 20
    assert package_engineering["mutation"]["metrics_total"] == 25
    assert package_engineering["mutation"]["metrics_truncated"] is True
    assert [gate["gate"] for gate in package_payload["engineering_gates"]] == [
        "mutation_test_baseline",
        "mutation_measurement_complete",
        "mutation_score_recorded",
    ]


def test_rc14_rc19_history_is_descriptive_not_decision_ground_truth() -> None:
    payload = json.loads(HISTORY_FIXTURE.read_text(encoding="utf-8"))
    outcomes = payload["outcomes"]

    assert payload["schema"] == "neocortex-code-review-work-package-outcomes/v1"
    assert payload["source"]["ground_truth_status"] == (
        "project_history_confirmed_not_general_human_ground_truth"
    )
    assert len(outcomes) == 7
    assert sum(bool(outcome["accepted"]) for outcome in outcomes) == 4
    assert all("target_hotspot_removed" in outcome for outcome in outcomes)
    assert all("corrected_call_resolutions" in outcome for outcome in outcomes)
