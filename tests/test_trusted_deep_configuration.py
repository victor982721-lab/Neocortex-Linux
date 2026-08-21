"""Reproducible configuration contracts for explicitly trusted deep analysis."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from neocortex.deduplication import InventoryExclusionPolicy
from _04_Nucleo_Operativo.application_config_projections import (
    code_route_config_from_application,
)
from _04_Nucleo_Operativo.code_contracts import (
    MAX_DEEP_TEST_SELECTORS,
    CodeRouteConfig,
)
from _04_Nucleo_Operativo.models import FrameworkConfig
from _04_Nucleo_Operativo.self_analysis import (
    build_self_analysis_completion_manifest,
    self_analysis_commands,
)


def _deep_framework_config(root: Path, state: Path) -> FrameworkConfig:
    return FrameworkConfig(
        root=root,
        state_directory=state,
        self_analysis=True,
        analysis_profile="trusted-deep",
        corpus_access_mode="analyze_only",
        route="code",
        document_catalog_enabled=False,
        code_include_generated=False,
        code_include_vendored=False,
        deep_test_selectors=(
            "tests/test_self_analysis_cli.py::test_self_analysis_preset_is_code_only_and_analyze_only",
        ),
        deep_max_tests=120,
        deep_time_budget_seconds=240,
        deep_shard_size=12,
        deep_mutation_target=r"_04_Nucleo_Operativo\external_deep_coverage.py",
        deep_mutation_symbol="external_deep_coverage._normalize",
        deep_mutation_max_mutants=17,
        deep_mutation_timeout_seconds=11,
        deep_mutation_time_budget_seconds=123,
    )


def test_deep_projection_declares_execution_with_separate_signature(tmp_path: Path) -> None:
    config = _deep_framework_config(tmp_path / "root", tmp_path / "state")

    projected = code_route_config_from_application(config)

    assert projected.analysis_profile == "trusted-deep"
    assert projected.deep_test_selectors == config.deep_test_selectors
    assert projected.deep_configuration_payload == {
        "schema": "neocortex.code-deep-configuration/v2",
        "analysis_profile": "trusted-deep",
        "content_executed": True,
        "suite_selection": "selected",
        "test_selectors": list(config.deep_test_selectors),
        "max_tests": 120,
        "time_budget_seconds": 240,
        "shard_size": 12,
        "mutation_target": "_04_Nucleo_Operativo/external_deep_coverage.py",
        "mutation_symbol": "external_deep_coverage._normalize",
        "mutation_max_mutants": 17,
        "mutation_timeout_seconds": 11,
        "mutation_time_budget_seconds": 123,
    }
    assert projected.deep_configuration_signature.startswith("code-deep-v2:")


def test_suite_controls_do_not_invalidate_ordinary_code_ast_signature(tmp_path: Path) -> None:
    full_suite = CodeRouteConfig(
        state_path=tmp_path / "code.sqlite3",
        dedup_path=tmp_path / "dedup.sqlite3",
        analysis_profile="trusted-deep",
    )
    selected_suite = CodeRouteConfig(
        state_path=tmp_path / "code.sqlite3",
        dedup_path=tmp_path / "dedup.sqlite3",
        analysis_profile="trusted-deep",
        deep_test_selectors=("tests/test_self_analysis_cli.py",),
        deep_max_tests=80,
        deep_time_budget_seconds=90,
        deep_shard_size=8,
    )

    assert full_suite.processing_signature == selected_suite.processing_signature
    assert full_suite.deep_configuration_signature != selected_suite.deep_configuration_signature


def test_analysis_profile_does_not_invalidate_ordinary_code_ast_signature(
    tmp_path: Path,
) -> None:
    protected = CodeRouteConfig(
        state_path=tmp_path / "code.sqlite3",
        dedup_path=tmp_path / "dedup.sqlite3",
        analysis_profile="protected",
    )
    trusted = replace(protected, analysis_profile="trusted-deep")

    assert protected.processing_signature.startswith("code-v3:")
    assert protected.processing_signature == trusted.processing_signature
    assert protected.deep_configuration_signature != trusted.deep_configuration_signature


def test_non_deep_code_config_rejects_hidden_execution_controls(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires trusted-deep"):
        CodeRouteConfig(
            state_path=tmp_path / "code.sqlite3",
            dedup_path=tmp_path / "dedup.sqlite3",
            analysis_profile="trusted-static",
            deep_test_selectors=("tests/test_self_analysis_cli.py",),
        )


def test_trusted_deep_command_and_manifest_are_exact(tmp_path: Path) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    root.mkdir()
    config = _deep_framework_config(root, state)
    commands = self_analysis_commands(config, root, state)

    analyze = commands["analyze"]
    assert analyze.count("--deep-test-selector") == 1
    assert analyze[analyze.index("--deep-max-tests") + 1] == "120"
    assert analyze[analyze.index("--deep-time-budget-seconds") + 1] == "240"
    assert analyze[analyze.index("--deep-shard-size") + 1] == "12"
    assert analyze[analyze.index("--deep-mutation-target") + 1] == (
        "_04_Nucleo_Operativo/external_deep_coverage.py"
    )
    assert analyze[analyze.index("--deep-mutation-symbol") + 1] == (
        "external_deep_coverage._normalize"
    )
    assert analyze[analyze.index("--deep-mutation-max-mutants") + 1] == "17"
    assert analyze[analyze.index("--deep-mutation-timeout-seconds") + 1] == "11"
    assert analyze[analyze.index("--deep-mutation-time-budget-seconds") + 1] == "123"

    manifest, _payload = build_self_analysis_completion_manifest(
        run={"run_id": 1},
        inventory={"scan_id": 2},
        inventory_policy=InventoryExclusionPolicy.compile((state,)),
        code_processing_signature="code-v2:fixture",
        code_summary={"processed": 1},
        safety_counts={
            "route_candidates": 0,
            "file_actions": 0,
            "run_actions": 0,
            "organization_events": 0,
        },
        commands=commands,
    )

    expected_deep = code_route_config_from_application(config)
    assert manifest["deep_analysis"] == {
        **expected_deep.deep_configuration_payload,
        "configuration_signature": expected_deep.deep_configuration_signature,
    }


def test_deep_manifest_preserves_a_full_linux_selector_inventory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    root.mkdir()
    selectors = tuple(f"tests/test_linux_boundary_{index:03d}.py" for index in range(300))
    config = replace(
        _deep_framework_config(root, state),
        deep_test_selectors=selectors,
        deep_mutation_target=None,
        deep_mutation_symbol=None,
        deep_mutation_max_mutants=20,
        deep_mutation_timeout_seconds=30,
        deep_mutation_time_budget_seconds=600,
    )
    commands = self_analysis_commands(config, root, state)

    manifest, _payload = build_self_analysis_completion_manifest(
        run={"run_id": 1},
        inventory={"scan_id": 2},
        inventory_policy=InventoryExclusionPolicy.compile((state,)),
        code_processing_signature="code-v2:fixture",
        code_summary={"processed": 1},
        safety_counts={
            "route_candidates": 0,
            "file_actions": 0,
            "run_actions": 0,
            "organization_events": 0,
        },
        commands=commands,
    )

    manifest_commands = cast(dict[str, object], manifest["commands"])
    deep_analysis = cast(dict[str, object], manifest["deep_analysis"])
    assert manifest_commands["analyze"] == commands["analyze"]
    assert deep_analysis["test_selectors"] == list(selectors)


def test_deep_selector_envelope_is_rejected_before_analysis_work(tmp_path: Path) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    root.mkdir()
    maximum = tuple(
        f"tests/test_manifest_boundary_{index:04d}.py" for index in range(MAX_DEEP_TEST_SELECTORS)
    )
    config = replace(
        _deep_framework_config(root, state),
        deep_test_selectors=maximum,
        deep_mutation_target=None,
        deep_mutation_symbol=None,
        deep_mutation_max_mutants=20,
        deep_mutation_timeout_seconds=30,
        deep_mutation_time_budget_seconds=600,
    )

    commands = self_analysis_commands(config, root, state)
    manifest, payload = build_self_analysis_completion_manifest(
        run={"run_id": 1},
        inventory={"scan_id": 2},
        inventory_policy=InventoryExclusionPolicy.compile((state,)),
        code_processing_signature="code-v2:fixture",
        code_summary={"processed": 1},
        safety_counts={
            "route_candidates": 0,
            "file_actions": 0,
            "run_actions": 0,
            "organization_events": 0,
        },
        commands=commands,
    )

    assert len(manifest["deep_analysis"]["test_selectors"]) == MAX_DEEP_TEST_SELECTORS
    assert len(payload.encode("utf-8")) <= 256 * 1024

    with pytest.raises(ValueError, match="too many deep test selectors"):
        self_analysis_commands(
            replace(config, deep_test_selectors=(*maximum, "tests/test_manifest_overflow.py")),
            root,
            state,
        )

    oversized = tuple(f"tests/test_{index:02d}_{'x' * 3900}.py" for index in range(50))
    with pytest.raises(ValueError, match="wire bound"):
        self_analysis_commands(replace(config, deep_test_selectors=oversized), root, state)


def test_deep_manifest_abstains_on_missing_or_duplicate_limits(tmp_path: Path) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    root.mkdir()
    commands = self_analysis_commands(_deep_framework_config(root, state), root, state)
    commands["analyze"].extend(("--deep-max-tests", "120"))

    with pytest.raises(ValueError, match="exactly one --deep-max-tests"):
        build_self_analysis_completion_manifest(
            run={"run_id": 1},
            inventory={"scan_id": 2},
            inventory_policy=InventoryExclusionPolicy.compile((state,)),
            code_processing_signature="code-v2:fixture",
            code_summary={"processed": 1},
            safety_counts={
                "route_candidates": 0,
                "file_actions": 0,
                "run_actions": 0,
                "organization_events": 0,
            },
            commands=commands,
        )


def test_deep_full_suite_is_declared_without_selectors(tmp_path: Path) -> None:
    config = replace(
        _deep_framework_config(tmp_path / "root", tmp_path / "state"),
        deep_test_selectors=(),
        deep_mutation_target=None,
        deep_mutation_symbol=None,
        deep_mutation_max_mutants=20,
        deep_mutation_timeout_seconds=30,
        deep_mutation_time_budget_seconds=600,
    )

    projected = code_route_config_from_application(config)

    assert projected.deep_configuration_payload["suite_selection"] == "full"
    assert projected.deep_configuration_payload["content_executed"] is True
    assert projected.deep_configuration_payload["mutation_target"] is None


def test_historical_deep_command_reconstructs_exact_v1_manifest(tmp_path: Path) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    root.mkdir()
    commands = self_analysis_commands(_deep_framework_config(root, state), root, state)
    mutation_options = {
        "--deep-mutation-target",
        "--deep-mutation-symbol",
        "--deep-mutation-max-mutants",
        "--deep-mutation-timeout-seconds",
        "--deep-mutation-time-budget-seconds",
    }
    historical: list[str] = []
    index = 0
    analyze = commands["analyze"]
    while index < len(analyze):
        if analyze[index] in mutation_options:
            index += 2
            continue
        historical.append(analyze[index])
        index += 1
    commands["analyze"] = historical

    manifest, _payload = build_self_analysis_completion_manifest(
        run={"run_id": 1},
        inventory={"scan_id": 2},
        inventory_policy=InventoryExclusionPolicy.compile((state,)),
        code_processing_signature="code-v2:fixture",
        code_summary={"processed": 1},
        safety_counts={
            "route_candidates": 0,
            "file_actions": 0,
            "run_actions": 0,
            "organization_events": 0,
        },
        commands=commands,
    )

    deep = manifest["deep_analysis"]
    assert isinstance(deep, dict)
    assert deep["schema"] == "neocortex.code-deep-configuration/v1"
    assert deep["configuration_signature"].startswith("code-deep-v1:")
    assert "mutation_target" not in deep
