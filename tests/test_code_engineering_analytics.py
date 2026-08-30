"""Focused contracts for the multidimensional engineering projection."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection

import pytest

from neocortex.code import code_engineering_analytics as engineering_module
from neocortex.code.code_architecture_analysis import (
    ArchitectureModule,
    ArchitectureSummary,
    CodeArchitectureAnalysis,
)
from neocortex.code.code_coverage_analysis import (
    CodeCoverageAnalysis,
    CoverageScopeSummary,
    CoverageTotals,
)
from neocortex.code.code_engineering_analytics import (
    GIT_HISTORY_PROVIDER_ID,
    MUTATION_PROVIDER_ID,
    analyze_code_engineering,
    engineering_profile_for_module,
    read_code_engineering_analysis,
)
from neocortex.code.external_evidence_models import (
    ExternalProviderEvidence,
    ExternalProviderMetric,
    ExternalProviderRelation,
)


MODULE = "neocortex.code.external_deep_coverage"
TARGET_PATH = "neocortex/code/external_deep_coverage.py"


def _architecture() -> CodeArchitectureAnalysis:
    module = ArchitectureModule(
        MODULE,
        4,
        3,
        18.0,
        7.0,
        4,
        (),
        (),
        4,
        3,
        0,
        False,
        "neocortex",
        12,
        False,
        ("neocortex",),
        9,
        False,
        ("tests",),
        0.35,
        1,
        2,
    )
    return CodeArchitectureAnalysis(
        "fixture.sqlite3",
        7,
        "ready",
        None,
        "observed",
        (),
        (),
        ArchitectureSummary(1, 0, 0, 0, 0, None, None, None, None),
        (module,),
        (),
        (),
        (),
        (),
        (),
    )


def _coverage() -> CodeCoverageAnalysis:
    totals = CoverageTotals(20, 17, 3, 8, 6, 2, 85.0, 75.0)
    scope = CoverageScopeSummary(
        "module",
        MODULE,
        MODULE,
        None,
        None,
        None,
        None,
        TARGET_PATH,
        totals,
        ((31, 33),),
        ((42, 45),),
        False,
        False,
        ("tests/test_external_deep_coverage.py::test_normalize",),
    )
    return CodeCoverageAnalysis(
        "fixture.sqlite3",
        7,
        "pytest-coverage-trusted-deep",
        10,
        10,
        "ready",
        None,
        "selected",
        True,
        True,
        (),
        "suite-v1",
        "coverage-config-v1",
        "coverage-scope-v1",
        None,
        totals,
        (scope,),
        (),
        (),
        (),
        (),
        (),
    )


def _metric(
    identity: str,
    provider: str,
    name: str,
    value: float,
    unit: str,
    *,
    subject_kind: str = "module",
    subject_key: str = MODULE,
    metadata: dict[str, object] | None = None,
) -> ExternalProviderMetric:
    return ExternalProviderMetric(
        identity,
        subject_kind,  # type: ignore[arg-type]
        subject_key,
        "history" if provider == GIT_HISTORY_PROVIDER_ID else "mutation_testing",
        name,
        value,
        unit,
        metadata={} if metadata is None else metadata,
    )


def _providers() -> dict[str, ExternalProviderEvidence]:
    history_metrics = (
        _metric("h1", GIT_HISTORY_PROVIDER_ID, "observed_commit_count", 21, "count"),
        _metric("h2", GIT_HISTORY_PROVIDER_ID, "observed_churn_lines", 540, "lines"),
        _metric(
            "h3",
            GIT_HISTORY_PROVIDER_ID,
            "observed_change_frequency_per_100_commits",
            8.2,
            "changes_per_100_commits",
        ),
    )
    mutation_metadata = {
        "target_relative_path": TARGET_PATH,
        "target_symbol": "_normalize",
        "measurement_scope_signature": "mutation-scope-v1",
        "measurement_complete": True,
    }
    mutation_metrics = (
        _metric(
            "m1",
            MUTATION_PROVIDER_ID,
            "baseline_passed",
            1,
            "boolean",
            subject_kind="symbol",
            subject_key="external_deep_coverage._normalize",
            metadata=mutation_metadata,
        ),
        _metric(
            "m2",
            MUTATION_PROVIDER_ID,
            "baseline_duration_milliseconds",
            1200,
            "milliseconds",
            subject_kind="symbol",
            subject_key="external_deep_coverage._normalize",
            metadata=mutation_metadata,
        ),
        _metric(
            "m3",
            MUTATION_PROVIDER_ID,
            "measurement_complete",
            1,
            "flag",
            subject_kind="symbol",
            subject_key="external_deep_coverage._normalize",
            metadata=mutation_metadata,
        ),
        _metric(
            "m4",
            MUTATION_PROVIDER_ID,
            "mutation_score",
            0.75,
            "ratio",
            subject_kind="symbol",
            subject_key="external_deep_coverage._normalize",
            metadata=mutation_metadata,
        ),
        _metric(
            "m5",
            MUTATION_PROVIDER_ID,
            "mutants_survived",
            1,
            "count",
            subject_kind="symbol",
            subject_key="external_deep_coverage._normalize",
            metadata=mutation_metadata,
        ),
    )
    cochange = ExternalProviderRelation(
        "r1",
        "file_cochange",
        "file",
        TARGET_PATH,
        "file",
        "tests/test_external_deep_coverage.py",
        directed=False,
    )
    return {
        GIT_HISTORY_PROVIDER_ID: ExternalProviderEvidence(
            GIT_HISTORY_PROVIDER_ID,
            20,
            20,
            "ready",
            None,
            metrics=history_metrics,
            relations=(cochange,),
        ),
        MUTATION_PROVIDER_ID: ExternalProviderEvidence(
            MUTATION_PROVIDER_ID, 21, 21, "ready", None, metrics=mutation_metrics
        ),
    }


def test_engineering_analysis_is_partial_without_deep_evidence() -> None:
    result = analyze_code_engineering(_architecture(), None, {}, analysis_run_id=7)

    assert result.status == "partial"
    assert result.aggregate_score is None
    assert result.defect_probability is None
    assert [item.status for item in result.providers] == ["not_recorded", "not_recorded"]
    profile = engineering_profile_for_module(result, MODULE)
    assert profile is not None
    assert profile.graph.status == "ready"
    assert profile.complexity.status == "ready"
    assert profile.coverage.status == "not_recorded"
    assert profile.mutation.status == "not_recorded"
    assert {gate.status for gate in result.gates} == {"not_evaluated"}


def test_engineering_analysis_correlates_dimensions_without_score() -> None:
    first = analyze_code_engineering(
        _architecture(), _coverage(), _providers(), database="fixture.sqlite3", analysis_run_id=7
    )
    second = analyze_code_engineering(
        _architecture(), _coverage(), _providers(), database="different.sqlite3", analysis_run_id=99
    )

    assert first.status == "ready"
    assert first.mutation_scope_signature == "mutation-scope-v1"
    assert first.mutation_score == 0.75
    assert [gate.status for gate in first.gates] == ["passed", "passed", "passed"]
    profile = engineering_profile_for_module(first, MODULE)
    assert profile is not None
    assert profile.path_namespace_id == "neocortex"
    assert {item.name for item in profile.graph.metrics} >= {
        "blast_radius",
        "directed_degree_centrality",
        "cross_path_namespace_fan_out",
    }
    assert {item.name for item in profile.history.metrics} >= {
        "observed_commit_count",
        "observed_churn_lines",
        "observed_cochange_relation_count",
    }
    assert {item.name for item in profile.coverage.metrics} >= {
        "line_coverage_percent",
        "branch_coverage_percent",
    }
    assert {item.name for item in profile.mutation.metrics} >= {
        "mutation_score",
        "mutants_survived",
    }
    assert first.digest == second.digest
    assert first.as_payload()["aggregate_score"] is None
    assert first.as_payload()["defect_probability"] is None


def test_engineering_reader_reuses_projections_and_filters_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    architecture = _architecture()
    coverage = _coverage()
    providers = _providers()
    observed_provider_ids: tuple[str, ...] | None = None

    def fail_duplicate_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an already-read projection was loaded again")

    def read_selected(
        _connection: sqlite3.Connection,
        _analysis_run_id: int,
        *,
        provider_ids: Collection[str] | None = None,
    ) -> dict[str, ExternalProviderEvidence]:
        nonlocal observed_provider_ids
        assert provider_ids is not None
        observed_provider_ids = tuple(provider_ids)
        return providers

    monkeypatch.setattr(engineering_module, "read_code_architecture_analysis", fail_duplicate_read)
    monkeypatch.setattr(engineering_module, "read_code_coverage_analysis", fail_duplicate_read)
    monkeypatch.setattr(engineering_module, "read_external_provider_evidence", read_selected)

    with sqlite3.connect(":memory:") as connection:
        result = read_code_engineering_analysis(
            connection,
            7,
            database="fixture.sqlite3",
            architecture=architecture,
            coverage=coverage,
        )

    assert observed_provider_ids == (GIT_HISTORY_PROVIDER_ID, MUTATION_PROVIDER_ID)
    assert (
        result.digest
        == analyze_code_engineering(
            architecture,
            coverage,
            providers,
            database="fixture.sqlite3",
            analysis_run_id=7,
        ).digest
    )


def test_engineering_analyzer_signature_status_order_and_digests_are_frozen() -> None:
    from inspect import signature

    assert str(signature(analyze_code_engineering)) == (
        "(architecture: 'CodeArchitectureAnalysis | None', "
        "coverage: 'CodeCoverageAnalysis | None', "
        "providers: 'Mapping[str, ExternalProviderEvidence]', *, "
        "database: 'str' = '', analysis_run_id: 'int | None' = None) "
        "-> 'CodeEngineeringAnalytics'"
    )
    ready = analyze_code_engineering(_architecture(), _coverage(), _providers())
    partial = analyze_code_engineering(_architecture(), None, {})
    abstained = analyze_code_engineering(None, None, {})

    assert [item.provider_id for item in ready.providers] == [
        GIT_HISTORY_PROVIDER_ID,
        MUTATION_PROVIDER_ID,
    ]
    assert [item.module_id for item in ready.modules] == [MODULE]
    assert [item.gate for item in ready.gates] == [
        "mutation_test_baseline",
        "mutation_measurement_complete",
        "mutation_score_recorded",
    ]
    assert (ready.status, ready.reason, ready.digest) == (
        "ready",
        None,
        "code-engineering-v2:xxh3_128:3e5f3f352ab664b37400a8e5c5367d4a",
    )
    assert (partial.status, partial.reason, partial.digest) == (
        "partial",
        "one_or_more_engineering_dimensions_not_ready",
        "code-engineering-v2:xxh3_128:7b06f5b21f2f3f8fef50727d29e8ff0a",
    )
    assert (abstained.status, abstained.reason, abstained.digest) == (
        "abstained",
        "architecture_not_ready",
        "code-engineering-v2:xxh3_128:ea7a72397d258bf053904222e3b717d9",
    )
    assert abstained.modules == ()
    assert {gate.status for gate in abstained.gates} == {"not_evaluated"}


def test_engineering_analyzer_enforces_module_bound_before_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engineering_module, "CODE_ENGINEERING_MODULE_LIMIT", 0)

    with pytest.raises(ValueError, match="engineering module bound exceeded"):
        analyze_code_engineering(_architecture(), _coverage(), _providers())
