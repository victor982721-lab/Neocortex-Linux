"""Regressions for the bounded trusted-deep coverage consumer."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import _04_Nucleo_Operativo.code_coverage_analysis as coverage_module
from _04_Nucleo_Operativo.code_coverage_analysis import (
    CODE_COVERAGE_PROVIDER_ID,
    CODE_COVERAGE_SCHEMA,
    analyze_code_coverage,
    compare_code_coverage,
    project_work_package_coverage,
    project_work_package_coverage_scope,
    read_code_coverage_analysis,
)
from _04_Nucleo_Operativo.external_evidence_models import (
    ExternalProviderEvidence,
    ExternalProviderMetric,
    ExternalProviderRelation,
)

SYMBOL = "pkg.mod:target:10:20"
SECOND_SYMBOL = "pkg.mod:target:30:40"
TEST_NODEID = "tests/test_mod.py::test_target"
PACKAGED_MODULE = "_04_Nucleo_Operativo.external_deep_coverage"
PACKAGED_QUALIFIED_NAME = f"{PACKAGED_MODULE}._normalize"
PACKAGED_SYMBOL = f"{PACKAGED_MODULE}:{PACKAGED_QUALIFIED_NAME}:984:1200"
PACKAGED_REVIEW_SYMBOL = "external_deep_coverage._normalize"


def _context(
    *,
    suite_selection: str = "full",
    measurement_complete: bool = True,
    suite_signature: str = "suite:v1",
    configuration_signature: str = "config:v1",
    measurement_scope_signature: str = "scope:v1",
    tool_versions: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "suite_selection": suite_selection,
        "measurement_complete": measurement_complete,
        "content_executed": True,
        "tool_versions": tool_versions or {"coverage": "7.14.0", "pytest": "9.1.0"},
        "suite_signature": suite_signature,
        "configuration_signature": configuration_signature,
        "measurement_scope_signature": measurement_scope_signature,
        "limitations": ["fixture_scope"],
    }


def _metric(
    kind: str,
    key: str,
    name: str,
    value: int | float,
    metadata: dict[str, object],
    *,
    unit: str = "count",
) -> ExternalProviderMetric:
    return ExternalProviderMetric(
        f"metric:{kind}:{key}:{name}",
        kind,  # type: ignore[arg-type]
        key,
        "coverage",
        name,
        value,
        unit,
        metadata=metadata,
    )


def _coverage_metrics(
    kind: str,
    key: str,
    metadata: dict[str, object],
    *,
    executable: int,
    covered: int,
    branches: int,
    covered_branches: int,
) -> list[ExternalProviderMetric]:
    missing = executable - covered
    missing_branches = branches - covered_branches
    metrics = [
        _metric(kind, key, "line_total", executable, metadata),
        _metric(kind, key, "executable_lines", executable, metadata),
        _metric(kind, key, "line_covered", covered, metadata),
        _metric(kind, key, "covered_lines", covered, metadata),
        _metric(kind, key, "line_missing", missing, metadata),
        _metric(kind, key, "missing_lines", missing, metadata),
        _metric(kind, key, "branch_total", branches, metadata),
        _metric(kind, key, "branches", branches, metadata),
        _metric(kind, key, "branch_covered", covered_branches, metadata),
        _metric(kind, key, "covered_branches", covered_branches, metadata),
        _metric(kind, key, "branch_missing", missing_branches, metadata),
        _metric(kind, key, "missing_branches", missing_branches, metadata),
    ]
    if executable:
        line_percent = covered * 100.0 / executable
        metrics.extend(
            (
                _metric(kind, key, "line_rate", line_percent / 100.0, metadata, unit="ratio"),
                _metric(
                    kind,
                    key,
                    "line_coverage_percent",
                    line_percent,
                    metadata,
                    unit="percent",
                ),
            )
        )
    if branches:
        branch_percent = covered_branches * 100.0 / branches
        metrics.extend(
            (
                _metric(
                    kind,
                    key,
                    "branch_rate",
                    branch_percent / 100.0,
                    metadata,
                    unit="ratio",
                ),
                _metric(
                    kind,
                    key,
                    "branch_coverage_percent",
                    branch_percent,
                    metadata,
                    unit="percent",
                ),
            )
        )
    return metrics


def _run_metrics(context: dict[str, object]) -> list[ExternalProviderMetric]:
    metrics = _coverage_metrics(
        "run",
        "coverage-run",
        context,
        executable=10,
        covered=8,
        branches=4,
        covered_branches=3,
    )
    for name, value in (
        ("tests_collected", 3),
        ("tests_selected", 3),
        ("tests_passed", 2),
        ("tests_failed", 0),
        ("tests_skipped", 1),
        ("shards_total", 2),
        ("shards_reused", 1),
    ):
        metrics.append(_metric("run", "coverage-run", name, value, context))
    return metrics


def _module_metrics(context: dict[str, object]) -> list[ExternalProviderMetric]:
    metadata = {
        **context,
        "module_key": "pkg.mod",
        "relative_path": "pkg/mod.py",
        "missing_line_ranges": [[13, 14]],
        "missing_branch_arcs": [[12, 15]],
        "missing_line_ranges_truncated": False,
        "missing_branch_arcs_truncated": False,
    }
    return _coverage_metrics(
        "module",
        "pkg.mod",
        metadata,
        executable=10,
        covered=8,
        branches=4,
        covered_branches=3,
    )


def _symbol_metrics(
    context: dict[str, object],
    symbol: str = SYMBOL,
    *,
    start_line: int = 10,
    end_line: int = 20,
    module_key: str = "pkg.mod",
    qualified_name: str = "target",
    relative_path: str = "pkg/mod.py",
    executable: int = 5,
    covered: int = 4,
    branches: int = 2,
    covered_branches: int = 1,
) -> list[ExternalProviderMetric]:
    metadata = {
        **context,
        "module_key": module_key,
        "symbol_key": symbol,
        "qualified_name": qualified_name,
        "start_line": start_line,
        "end_line": end_line,
        "relative_path": relative_path,
        "missing_line_ranges": [[13, 13]],
        "missing_branch_arcs": [[12, 15]],
        "missing_line_ranges_truncated": False,
        "missing_branch_arcs_truncated": False,
    }
    return _coverage_metrics(
        "symbol",
        symbol,
        metadata,
        executable=executable,
        covered=covered,
        branches=branches,
        covered_branches=covered_branches,
    )


def _relation(symbol: str = SYMBOL) -> ExternalProviderRelation:
    return ExternalProviderRelation(
        f"relation:{symbol}",
        "test_covers_symbol",
        "symbol",
        f"pytest-nodeid:{TEST_NODEID}",
        "symbol",
        symbol,
        confidence=1.0,
        metadata={
            "test_nodeids": [TEST_NODEID],
            "lines": [10, 11, 12],
            "contexts": [TEST_NODEID],
            "relative_path": "pkg/mod.py",
            "module_key": "pkg.mod",
            "symbol_key": symbol,
        },
    )


def _packaged_symbol_evidence(
    *,
    module_key: str = PACKAGED_MODULE,
    qualified_name: str = PACKAGED_QUALIFIED_NAME,
    symbol: str = PACKAGED_SYMBOL,
) -> ExternalProviderEvidence:
    context = _context()
    metrics = [
        *_run_metrics(context),
        *_symbol_metrics(
            context,
            symbol,
            start_line=984,
            end_line=1200,
            module_key=module_key,
            qualified_name=qualified_name,
            relative_path="_04_Nucleo_Operativo/external_deep_coverage.py",
            executable=44,
            covered=38,
            branches=22,
            covered_branches=17,
        ),
    ]
    relation = ExternalProviderRelation(
        f"relation:{symbol}",
        "test_covers_symbol",
        "symbol",
        f"pytest-nodeid:{TEST_NODEID}",
        "symbol",
        symbol,
        confidence=1.0,
        metadata={
            "test_nodeids": [TEST_NODEID],
            "lines": [984, 990, 1000],
            "contexts": [TEST_NODEID],
            "relative_path": "_04_Nucleo_Operativo/external_deep_coverage.py",
            "module_key": module_key,
            "symbol_key": symbol,
        },
    )
    return ExternalProviderEvidence(
        CODE_COVERAGE_PROVIDER_ID,
        7,
        7,
        "ready",
        None,
        (),
        tuple(metrics),
        (relation,),
    )


def _evidence(
    *,
    context: dict[str, object] | None = None,
    extra_symbols: tuple[tuple[str, int, int], ...] = (),
    relations: tuple[ExternalProviderRelation, ...] | None = None,
) -> ExternalProviderEvidence:
    selected_context = context or _context()
    metrics = [
        *_run_metrics(selected_context),
        *_module_metrics(selected_context),
        *_symbol_metrics(selected_context),
    ]
    for symbol, start, end in extra_symbols:
        metrics.extend(_symbol_metrics(selected_context, symbol, start_line=start, end_line=end))
    return ExternalProviderEvidence(
        CODE_COVERAGE_PROVIDER_ID,
        7,
        7,
        "ready",
        None,
        (),
        tuple(metrics),
        (_relation(),) if relations is None else relations,
    )


def test_ready_evidence_preserves_dimensions_and_exact_test_relations() -> None:
    analysis = analyze_code_coverage(_evidence(), database="code.sqlite3", analysis_run_id=5)

    assert analysis.status == "ready"
    assert analysis.suite_selection == "full"
    assert analysis.measurement_complete is True
    assert analysis.content_executed is True
    assert [(item.name, item.version) for item in analysis.tool_versions] == [
        ("coverage", "7.14.0"),
        ("pytest", "9.1.0"),
    ]
    assert analysis.outcomes is not None
    assert (analysis.outcomes.collected, analysis.outcomes.passed, analysis.outcomes.skipped) == (
        3,
        2,
        1,
    )
    assert analysis.totals is not None
    assert analysis.totals.line_coverage_percent == 80.0
    assert analysis.totals.branch_coverage_percent == 75.0
    assert len(analysis.modules) == 1
    assert len(analysis.symbols) == 1
    assert analysis.symbols[0].missing_line_ranges == ((13, 13),)
    assert analysis.symbols[0].qualified_name == "target"
    assert analysis.symbols[0].executing_tests == (TEST_NODEID,)
    assert analysis.test_relations[0].production_symbol == SYMBOL
    assert analysis.test_relations[0].lines == (10, 11, 12)
    assert {gate.gate: gate.status for gate in analysis.gates} == {
        "tests_passed": "passed",
        "coverage_available": "passed",
    }
    payload = analysis.as_payload()
    assert payload["schema"] == CODE_COVERAGE_SCHEMA
    json.dumps(payload, sort_keys=True)
    assert "database" not in analysis.digest_payload()
    assert "tool_run_id" not in analysis.digest_payload()


def test_work_package_projection_resolves_stable_and_review_identities() -> None:
    analysis = analyze_code_coverage(_evidence())

    for target in (SYMBOL, "pkg.mod.target"):
        scope = project_work_package_coverage_scope(analysis, target)
        projection = project_work_package_coverage(analysis, target)
        assert scope is not None and scope.subject_key == SYMBOL
        assert projection.primary_symbol == SYMBOL
        assert projection.status == "executed"
        assert projection.executing_tests == (TEST_NODEID,)
        assert projection.gate.status == "passed"


def test_work_package_projection_resolves_unique_packaged_qualified_name_suffix() -> None:
    analysis = analyze_code_coverage(_packaged_symbol_evidence())

    scope = project_work_package_coverage_scope(analysis, PACKAGED_REVIEW_SYMBOL)
    projection = project_work_package_coverage(analysis, PACKAGED_REVIEW_SYMBOL)

    assert scope is not None
    assert scope.subject_key == PACKAGED_SYMBOL
    assert scope.qualified_name == PACKAGED_QUALIFIED_NAME
    assert round(scope.totals.branch_coverage_percent or 0.0, 2) == 77.27
    assert scope.executing_tests == (TEST_NODEID,)
    assert projection.primary_symbol == PACKAGED_SYMBOL
    assert projection.status == "executed"
    assert projection.executing_tests == (TEST_NODEID,)
    assert projection.gate.status == "passed"


def test_work_package_packaged_qualified_name_suffix_ambiguity_abstains() -> None:
    evidence = _packaged_symbol_evidence()
    second_module = "vendor.external_deep_coverage"
    second_qualified_name = f"{second_module}._normalize"
    second_symbol = f"{second_module}:{second_qualified_name}:984:1200"
    evidence = replace(
        evidence,
        metrics=(
            *evidence.metrics,
            *_symbol_metrics(
                _context(),
                second_symbol,
                start_line=984,
                end_line=1200,
                module_key=second_module,
                qualified_name=second_qualified_name,
                relative_path="vendor/external_deep_coverage.py",
            ),
        ),
    )
    analysis = analyze_code_coverage(evidence)

    projection = project_work_package_coverage(analysis, PACKAGED_REVIEW_SYMBOL)

    assert project_work_package_coverage_scope(analysis, PACKAGED_REVIEW_SYMBOL) is None
    assert projection.status == "not_evaluated"
    assert projection.gate.reason == "work_package_target_ambiguous"


def test_work_package_alias_ambiguity_and_missing_target_abstain() -> None:
    analysis = analyze_code_coverage(_evidence(extra_symbols=((SECOND_SYMBOL, 30, 40),)))

    ambiguous = project_work_package_coverage(analysis, "pkg.mod.target")
    missing = project_work_package_coverage(analysis, "pkg.mod.missing")
    exact = project_work_package_coverage(analysis, SYMBOL)

    assert project_work_package_coverage_scope(analysis, "pkg.mod.target") is None
    assert ambiguous.status == "not_evaluated"
    assert ambiguous.gate.reason == "work_package_target_ambiguous"
    assert missing.status == "not_evaluated"
    assert missing.gate.reason == "work_package_target_not_measured"
    assert exact.primary_symbol == SYMBOL
    assert exact.status == "executed"


def test_failed_or_absent_provider_never_passes_gates() -> None:
    failed = ExternalProviderEvidence(
        CODE_COVERAGE_PROVIDER_ID,
        8,
        None,
        "abstained",
        "pytest_timed_out",
    )

    absent = analyze_code_coverage(None)
    failed_analysis = analyze_code_coverage(failed)

    assert absent.status == "abstained"
    assert failed_analysis.status == "abstained"
    assert failed_analysis.reason == "pytest_timed_out"
    assert all(item.status == "not_evaluated" for item in absent.gates)
    assert all(item.status == "not_evaluated" for item in failed_analysis.gates)


def test_selected_incomplete_measurement_is_preserved_but_not_comparable() -> None:
    analysis = analyze_code_coverage(
        _evidence(
            context=_context(
                suite_selection="selected",
                measurement_complete=False,
            )
        )
    )

    assert analysis.status == "ready"
    assert analysis.suite_selection == "selected"
    assert analysis.measurement_complete is False
    assert all(item.status == "not_evaluated" for item in analysis.gates)
    comparison = compare_code_coverage(analysis, analysis)
    assert comparison.status == "not_comparable"
    assert comparison.reason == "coverage_measurement_incomplete"
    projection = project_work_package_coverage(analysis, SYMBOL)
    assert projection.status == "not_evaluated"


def test_comparison_requires_exact_suite_config_scope_and_tool_versions() -> None:
    baseline = analyze_code_coverage(_evidence())
    changed_config = analyze_code_coverage(
        _evidence(context=_context(configuration_signature="config:v2"))
    )
    changed_tools = analyze_code_coverage(
        _evidence(context=_context(tool_versions={"coverage": "7.15.0", "pytest": "9.1.0"}))
    )

    config_diff = compare_code_coverage(baseline, changed_config)
    tool_diff = compare_code_coverage(baseline, changed_tools)

    assert config_diff.status == "not_comparable"
    assert config_diff.reason == "coverage_configuration_changed"
    assert all(item.status == "not_evaluated" for item in config_diff.gates)
    assert tool_diff.reason == "coverage_tool_versions_changed"


def test_comparison_keeps_changed_measurement_universe_comparable() -> None:
    baseline = analyze_code_coverage(_evidence())
    assert baseline.totals is not None
    current_totals = replace(
        baseline.totals,
        executable_lines=12,
        covered_lines=9,
        missing_lines=3,
        branch_exits=6,
        covered_branch_exits=4,
        missing_branch_exits=2,
        line_coverage_percent=75.0,
        branch_coverage_percent=4 * 100.0 / 6,
    )
    current = replace(baseline, totals=current_totals)

    comparison = compare_code_coverage(baseline, current)

    assert comparison.status == "comparable"
    assert comparison.executable_lines_delta == 2
    assert comparison.covered_lines_delta == 1
    assert comparison.missing_lines_delta == 1
    assert comparison.branch_exits_delta == 2
    assert comparison.covered_branch_exits_delta == 1
    assert comparison.missing_branch_exits_delta == 1
    assert comparison.line_coverage_percent_delta == -5.0
    assert comparison.branch_coverage_percent_delta is not None
    assert {item.gate: item.status for item in comparison.gates} == {
        "line_coverage_not_degraded": "failed",
        "branch_coverage_not_degraded": "failed",
    }


def test_corrupt_alias_or_relation_abstains_without_partial_projection() -> None:
    evidence = _evidence()
    conflicting = replace(evidence.metrics[0], value=11)
    corrupt_metrics = replace(evidence, metrics=(conflicting, *evidence.metrics[1:]))
    corrupt_relation = replace(
        evidence.relations[0],
        metadata={**evidence.relations[0].metadata, "test_nodeids": ["another::test"]},
    )

    bad_metric = analyze_code_coverage(corrupt_metrics)
    bad_relation = analyze_code_coverage(replace(evidence, relations=(corrupt_relation,)))

    assert bad_metric.status == "abstained"
    assert "conflicting_metric_alias" in (bad_metric.reason or "")
    assert not bad_metric.symbols and not bad_metric.test_relations
    assert bad_relation.status == "abstained"
    assert "coverage_relation_test_identity_mismatch" in (bad_relation.reason or "")


def test_consumer_bounds_persisted_evidence(monkeypatch: object) -> None:
    evidence = _evidence()
    monkeypatch.setattr(coverage_module, "CODE_COVERAGE_METRIC_LIMIT", 1)  # type: ignore[attr-defined]

    analysis = analyze_code_coverage(evidence)

    assert analysis.status == "abstained"
    assert "provider_metric_bound_exceeded" in (analysis.reason or "")
    assert all(item.status == "not_evaluated" for item in analysis.gates)


def test_database_reader_rejects_stale_provider_before_evidence_read(monkeypatch: object) -> None:
    calls = {"evidence": 0}

    def suite(*args: object, **kwargs: object) -> object:
        assert kwargs["enforce_current_runtime"] is True
        return SimpleNamespace(
            providers=(
                SimpleNamespace(
                    provider_id=CODE_COVERAGE_PROVIDER_ID,
                    status="abstained",
                    reason="external_provider_runtime_stale",
                ),
            )
        )

    def evidence(*args: object, **kwargs: object) -> dict[str, object]:
        calls["evidence"] += 1
        return {}

    monkeypatch.setattr(coverage_module, "read_external_evidence_suite", suite)  # type: ignore[attr-defined]
    monkeypatch.setattr(coverage_module, "read_external_provider_evidence", evidence)  # type: ignore[attr-defined]

    analysis = read_code_coverage_analysis(SimpleNamespace(), 9)  # type: ignore[arg-type]

    assert analysis.status == "abstained"
    assert analysis.reason == "external_provider_runtime_stale"
    assert calls["evidence"] == 0
