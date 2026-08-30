"""Contracts for the deterministic local/CI quality gate."""

from __future__ import annotations

import hashlib
import json
import sysconfig
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

import neocortex.code.external_architecture_worker as architecture_worker
from neocortex.code.code_architecture_contracts import ModuleImport
from neocortex import semgrep_tool_contract
from tools import quality_gate
from tools.quality_gate import (
    BASELINE_SCHEMA,
    COVERAGE_BASELINE_SCHEMA,
    EXPECTED_ARCHITECTURE_BASELINE_ID,
    EXPECTED_ARCHITECTURE_CONTRACTS,
    EXPECTED_CAPABILITY_CANONICAL_FAMILY,
    PRODUCTION_COVERAGE_SOURCES,
    WHEEL_PACKAGE_ROOTS,
    GateError,
    StaticObservation,
    _pre_push_receipt_payload,
    baseline_payload,
    build_production_source_inventory_payload,
    build_test_inventory_payload,
    compare_coverage_report,
    compare_static_observations,
    coverage_baseline_payload,
    discover_test_files,
    discover_production_sources,
    evaluate_architecture_payload,
    evaluate_audit_payload,
    evaluate_tool_audit_payload,
    evaluate_tool_runtime_receipt,
    main,
    partition_test_files,
    run_coverage_gate,
    run_installed_wheel_gate,
    write_gate_receipt,
)


def _repository_fixture(root: Path) -> Path:
    (root / "tests" / "nested").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[project]\nname='fixture'\n", encoding="utf-8", newline="\n"
    )
    (root / "tests" / "test_alpha.py").write_text(
        "def test_alpha(): pass\n", encoding="utf-8", newline="\n"
    )
    (root / "tests" / "nested" / "beta_test.py").write_text(
        "def test_beta(): pass\n" * 3, encoding="utf-8", newline="\n"
    )
    (root / "tests" / "helpers.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    return root


def _observation(
    tool: str,
    *entries: tuple[str, str, str, int],
    version: str = "1.0.0",
) -> StaticObservation:
    counts: Counter[tuple[str, str, str]] = Counter()
    for path, rule, severity, count in entries:
        counts[(path, rule, severity)] = count
    return StaticObservation(tool, version, counts)


def _static_set(ruff_count: int = 2) -> tuple[StaticObservation, ...]:
    return (
        _observation("ruff", ("source.py", "F401", "error", ruff_count)),
        _observation("mypy", ("source.py", "attr-defined", "error", 1)),
        _observation("pyright", ("source.py", "reportUnknownMemberType", "warning", 1)),
    )


def _coverage_report(
    *,
    covered_lines: int = 80,
    statements: int = 100,
    covered_branches: int = 30,
    branches: int = 50,
    version: str = "7.14.1",
) -> dict[str, object]:
    return {
        "meta": {"version": version, "branch_coverage": True},
        "files": {"neocortex/__init__.py": {}},
        "totals": {
            "covered_lines": covered_lines,
            "num_statements": statements,
            "covered_branches": covered_branches,
            "num_branches": branches,
        },
    }


def _coverage_inventory(*paths: str) -> dict[str, object]:
    return {
        "file_count": len(paths),
        "hash_algorithm": "sha256(path,lf-size,sha256(lf-content))-v1",
        "sha256": hashlib.sha256("\n".join(paths).encode()).hexdigest(),
        "files": sorted(paths),
    }


def _architecture_payload(
    imports: tuple[ModuleImport, ...] = (),
) -> dict[str, object]:
    canonical, _, _ = architecture_worker._registered_capability_labels()
    target_modules = architecture_worker._target_registry.registered_core_modules()
    modules = tuple(sorted({*canonical, *target_modules}))
    cycles = architecture_worker._cycle_payloads(modules, imports)
    return {
        "schema": architecture_worker.GRIMP_WORKER_SCHEMA,
        "status": "ready",
        "counters": {
            "modules": len(modules),
            "production_relations": len(imports),
            "contract_violations": 0,
            "cyclic_components": len(cycles),
        },
        "contract_evaluations": [
            {"contract": {"contract_id": contract_id}, "status": "passed"}
            for contract_id in sorted(EXPECTED_ARCHITECTURE_CONTRACTS)
        ],
        "module_metrics": [{"module": module} for module in modules],
        "relations": [item.as_payload() for item in imports],
        "cycles": list(cycles),
        "projections": architecture_worker._capability_projection_payload(modules, imports),
        "tool": {"name": "grimp", "version": "3.15"},
        "architecture": {"baseline_id": EXPECTED_ARCHITECTURE_BASELINE_ID},
        "inputs": {"content_manifest_sha256": hashlib.sha256(b"[]").hexdigest()},
    }


def test_inventory_discovers_both_pytest_patterns_and_partitions_exactly(tmp_path: Path) -> None:
    root = _repository_fixture(tmp_path)

    files = discover_test_files(root)
    first = partition_test_files(files, 2)
    second = partition_test_files(tuple(reversed(files)), 2)
    payload = build_test_inventory_payload(root, 2)

    assert [item.path for item in files] == [
        "tests/nested/beta_test.py",
        "tests/test_alpha.py",
    ]
    assert first == second
    assert {item.path for shard in first for item in shard} == {item.path for item in files}
    assert sum(len(shard) for shard in first) == len(files)
    assert payload["file_count"] == 2
    assert len(str(payload["sha256"])) == 64


def test_inventory_cli_emits_a_reproducible_json_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository_fixture(tmp_path)

    assert main(("--root", str(root), "inventory", "--shard-count", "2", "--json")) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "neocortex.quality-gate-test-inventory/v1"
    assert [item["index"] for item in payload["shards"]] == [0, 1]


def test_inventory_and_shards_are_reproducible_across_lf_and_crlf(tmp_path: Path) -> None:
    lf_root = _repository_fixture(tmp_path / "lf")
    crlf_root = _repository_fixture(tmp_path / "crlf")
    for candidate in (crlf_root / "tests").rglob("*.py"):
        candidate.write_bytes(candidate.read_bytes().replace(b"\n", b"\r\n"))

    assert discover_test_files(lf_root) == discover_test_files(crlf_root)
    lf_inventory = build_test_inventory_payload(lf_root, 2)
    crlf_inventory = build_test_inventory_payload(crlf_root, 2)
    assert lf_inventory["sha256"] == crlf_inventory["sha256"]
    assert lf_inventory["shards"] == crlf_inventory["shards"]


def test_pytest_lab_rejects_codex_home_before_creating_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    basetemp = codex_home / "vault" / "work" / "pytest"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    with pytest.raises(
        GateError,
        match="must remain outside protected Codex home and vault",
    ):
        quality_gate._pytest_command((), basetemp=basetemp)

    assert not codex_home.exists()


def test_repository_does_not_configure_github_actions() -> None:
    workflow_root = Path(__file__).parents[1] / ".github" / "workflows"
    workflows = () if not workflow_root.exists() else tuple(workflow_root.glob("*.y*ml"))

    assert workflows == ()
    assert WHEEL_PACKAGE_ROOTS == ("neocortex",)


def test_installed_wheel_gate_refuses_a_source_backed_probe(tmp_path: Path) -> None:
    root = _repository_fixture(tmp_path)

    with pytest.raises(GateError, match="outside the repository"):
        run_installed_wheel_gate(root, root / "probe")


def test_static_baseline_allows_only_same_or_reduced_bucket_counts() -> None:
    baseline = baseline_payload(_static_set(ruff_count=2))

    assert compare_static_observations(_static_set(ruff_count=1), baseline) == {
        "ruff": 1,
        "mypy": 1,
        "pyright": 1,
    }

    with pytest.raises(GateError, match="F401/error 3 > 2"):
        compare_static_observations(_static_set(ruff_count=3), baseline)
    with pytest.raises(GateError, match=r"version 2\.0\.0 != 1\.0\.0"):
        compare_static_observations(
            (
                _observation("ruff", ("source.py", "F401", "error", 1), version="2.0.0"),
                *_static_set()[1:],
            ),
            baseline,
        )


def test_static_baseline_rejects_new_rule_even_when_total_is_lower() -> None:
    baseline = baseline_payload(_static_set(ruff_count=2))
    current = (
        _observation("ruff", ("source.py", "B904", "error", 1)),
        *_static_set()[1:],
    )

    with pytest.raises(GateError, match="B904/error 1 > 0"):
        compare_static_observations(current, baseline)

    assert baseline["schema"] == BASELINE_SCHEMA


def test_static_diagnostic_fingerprint_normalizes_message_and_binds_identity_fields(
    tmp_path: Path,
) -> None:
    root = _repository_fixture(tmp_path)

    def evidence(
        *,
        tool: str = "ruff",
        version: str = "0.15.17",
        path: str = "source.py",
        rule: str = "F401",
        severity: str = "error",
        message: object = None,
        anchor: str | None = "4:1-4:5",
        symbol: str | None = "name",
    ) -> quality_gate.StaticDiagnosticEvidence:
        effective_message = (
            f"{root}/source.py   imported\nname is unused" if message is None else message
        )
        return quality_gate._static_diagnostic_evidence(
            tool=tool,
            version=version,
            path=path,
            rule=rule,
            severity=severity,
            message=effective_message,
            root=root,
            anchor=anchor,
            symbol=symbol,
        )

    first = evidence()
    normalized_equivalent = evidence(message="<root>/source.py imported name is unused")

    assert first.normalized_message == "<root>/source.py imported name is unused"
    assert first.fingerprint == normalized_equivalent.fingerprint
    assert (
        len(
            {
                first.fingerprint,
                evidence(tool="mypy").fingerprint,
                evidence(version="0.15.18").fingerprint,
                evidence(path="other.py").fingerprint,
                evidence(rule="F821").fingerprint,
                evidence(severity="warning").fingerprint,
                evidence(message="a different diagnostic").fingerprint,
                evidence(anchor="8:2-8:6").fingerprint,
                evidence(symbol="other_name").fingerprint,
            }
        )
        == 9
    )


def test_static_shadow_report_distinguishes_diagnostics_with_equal_gate_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository_fixture(tmp_path)

    def observations(message: str, anchor: str) -> tuple[StaticObservation, ...]:
        diagnostic = quality_gate._static_diagnostic_evidence(
            tool="ruff",
            version="1.0.0",
            path="source.py",
            rule="F401",
            severity="error",
            message=message,
            root=root,
            anchor=anchor,
            symbol="imported_name",
        )
        return (
            StaticObservation(
                "ruff",
                "1.0.0",
                Counter({("source.py", "F401", "error"): 1}),
                (diagnostic,),
            ),
            _observation("mypy"),
            _observation("pyright"),
        )

    first = observations("first unused import", "4:1-4:5")
    second = observations("second unused import", "9:1-9:6")
    baseline = baseline_payload(first)
    baseline_path = root / "static-baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    monkeypatch.setattr(quality_gate, "collect_static_observations", lambda _root: first)
    first_report = quality_gate.run_static_gate(root, baseline_path)
    monkeypatch.setattr(quality_gate, "collect_static_observations", lambda _root: second)
    second_report = quality_gate.run_static_gate(root, baseline_path)

    assert (
        first_report["tools"]
        == second_report["tools"]
        == {
            "ruff": {"version": "1.0.0", "total": 1},
            "mypy": {"version": "1.0.0", "total": 0},
            "pyright": {"version": "1.0.0", "total": 0},
        }
    )
    assert baseline_payload(first) == baseline_payload(second) == baseline
    persisted_baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert "diagnostic_shadow" not in persisted_baseline

    first_shadow = first_report["diagnostic_shadow"]
    second_shadow = second_report["diagnostic_shadow"]
    assert isinstance(first_shadow, dict)
    assert isinstance(second_shadow, dict)
    assert first_shadow["enforced"] is second_shadow["enforced"] is False
    first_tool = first_shadow["tools"]["ruff"]
    second_tool = second_shadow["tools"]["ruff"]
    assert first_tool["count_baseline_total"] == second_tool["count_baseline_total"] == 1
    assert first_tool["coverage"] == second_tool["coverage"] == "complete"
    first_diagnostic = first_tool["diagnostics"][0]
    second_diagnostic = second_tool["diagnostics"][0]
    assert first_diagnostic["fingerprint"] != second_diagnostic["fingerprint"]
    assert first_tool["manifest_sha256"] != second_tool["manifest_sha256"]


def test_pyright_policy_uses_the_live_interpreter_packages_not_missing_import_debt() -> None:
    root = Path(__file__).parents[1]

    payload = quality_gate._pyright_config_payload(root)

    extra_paths = payload["extraPaths"]
    assert isinstance(extra_paths, list)
    assert extra_paths[0] == str(root)
    assert str(Path(sysconfig.get_path("purelib")).resolve()) in extra_paths
    assert payload["pythonVersion"] == "3.13"
    assert payload["typeCheckingMode"] == "basic"


def test_pyright_command_enforces_the_bounded_node_heap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "pyright"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("NEOCORTEX_PYRIGHT", str(executable))
    monkeypatch.setenv("NODE_OPTIONS", "--max-old-space-size=999999")
    monkeypatch.setattr(quality_gate.shutil, "which", lambda _name: None)

    selected, environment = quality_gate._pyright_command()

    assert selected == executable.resolve()
    assert environment["NODE_OPTIONS"] == "--max-old-space-size=1792"


def test_coverage_baseline_is_branch_aware_versioned_and_uses_production_scope() -> None:
    report = _coverage_report()
    baseline = coverage_baseline_payload(
        report,
        test_inventory=_coverage_inventory("tests/test_alpha.py", "tests/test_beta.py"),
        source_inventory=_coverage_inventory(
            "neocortex/__init__.py", "neocortex/cli.py"
        ),
    )

    assert baseline["schema"] == COVERAGE_BASELINE_SCHEMA
    scope = baseline["scope"]
    assert isinstance(scope, dict)
    assert scope["branch"] is True
    assert scope["sources"] == list(PRODUCTION_COVERAGE_SOURCES)
    assert baseline["tool"] == {"name": "coverage", "version": "7.14.1"}
    assert baseline["approved"] == {
        "lines": {"covered": 80, "total": 100},
        "branches": {"covered": 30, "total": 50},
    }
    assert (
        compare_coverage_report(
            report,
            baseline,
            test_inventory=_coverage_inventory("tests/test_alpha.py", "tests/test_beta.py"),
            source_inventory=_coverage_inventory(
                "neocortex/__init__.py", "neocortex/cli.py"
            ),
        )["metrics"]
        == baseline["approved"]
    )


@pytest.mark.parametrize(
    ("report", "message"),
    (
        (_coverage_report(covered_lines=79), "lines 79/100 < 80/100"),
        (_coverage_report(covered_branches=29), "branches 29/50 < 30/50"),
    ),
)
def test_coverage_gate_rejects_line_or_branch_rate_regression(
    report: dict[str, object], message: str
) -> None:
    baseline = coverage_baseline_payload(
        _coverage_report(),
        test_inventory=_coverage_inventory("tests/test_alpha.py", "tests/test_beta.py"),
        source_inventory=_coverage_inventory(
            "neocortex/__init__.py", "neocortex/cli.py"
        ),
    )

    with pytest.raises(GateError, match=message):
        compare_coverage_report(
            report,
            baseline,
            test_inventory=_coverage_inventory("tests/test_alpha.py", "tests/test_beta.py"),
            source_inventory=_coverage_inventory(
                "neocortex/__init__.py", "neocortex/cli.py"
            ),
        )


def test_coverage_gate_rejects_non_branch_report_and_tool_version_drift() -> None:
    baseline = coverage_baseline_payload(
        _coverage_report(),
        test_inventory=_coverage_inventory("tests/test_alpha.py", "tests/test_beta.py"),
        source_inventory=_coverage_inventory(
            "neocortex/__init__.py", "neocortex/cli.py"
        ),
    )
    not_branch_aware = _coverage_report()
    meta = not_branch_aware["meta"]
    assert isinstance(meta, dict)
    meta["branch_coverage"] = False

    with pytest.raises(GateError, match="not branch-aware"):
        compare_coverage_report(
            not_branch_aware,
            baseline,
            test_inventory=_coverage_inventory("tests/test_alpha.py", "tests/test_beta.py"),
            source_inventory=_coverage_inventory(
                "neocortex/__init__.py", "neocortex/cli.py"
            ),
        )
    with pytest.raises(GateError, match="does not match baseline"):
        compare_coverage_report(
            _coverage_report(version="7.15.0"),
            baseline,
            test_inventory=_coverage_inventory("tests/test_alpha.py", "tests/test_beta.py"),
            source_inventory=_coverage_inventory(
                "neocortex/__init__.py", "neocortex/cli.py"
            ),
        )


@pytest.mark.parametrize(
    ("test_inventory", "source_inventory", "message"),
    (
        (
            _coverage_inventory("tests/test_alpha.py"),
            _coverage_inventory("neocortex/__init__.py", "neocortex/cli.py"),
            "test paths removed",
        ),
        (
            _coverage_inventory("tests/test_alpha.py", "tests/test_beta.py"),
            _coverage_inventory("neocortex/__init__.py"),
            "production source paths removed",
        ),
    ),
)
def test_coverage_inventory_ratchet_rejects_deleted_test_or_source_path(
    test_inventory: dict[str, object],
    source_inventory: dict[str, object],
    message: str,
) -> None:
    baseline = coverage_baseline_payload(
        _coverage_report(),
        test_inventory=_coverage_inventory("tests/test_alpha.py", "tests/test_beta.py"),
        source_inventory=_coverage_inventory(
            "neocortex/__init__.py", "neocortex/cli.py"
        ),
    )

    with pytest.raises(GateError, match=message):
        compare_coverage_report(
            _coverage_report(),
            baseline,
            test_inventory=test_inventory,
            source_inventory=source_inventory,
        )


def test_coverage_inventory_ratchet_allows_additions_without_rewriting_baseline() -> None:
    baseline = coverage_baseline_payload(
        _coverage_report(),
        test_inventory=_coverage_inventory("tests/test_alpha.py"),
        source_inventory=_coverage_inventory("neocortex/__init__.py"),
    )

    summary = compare_coverage_report(
        _coverage_report(),
        baseline,
        test_inventory=_coverage_inventory("tests/test_alpha.py", "tests/test_new.py"),
        source_inventory=_coverage_inventory(
            "neocortex/__init__.py", "neocortex/new.py"
        ),
    )

    assert summary["metrics"] == baseline["approved"]


def test_coverage_inventory_requires_every_production_root(tmp_path: Path) -> None:
    root = _repository_fixture(tmp_path)
    for package in PRODUCTION_COVERAGE_SOURCES[:-1]:
        directory = root / package
        directory.mkdir()
        (directory / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")

    with pytest.raises(GateError, match="production coverage root is missing"):
        discover_production_sources(root)


def test_coverage_runner_uses_branch_mode_exact_scope_and_dynamic_total_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository_fixture(tmp_path)
    for package in PRODUCTION_COVERAGE_SOURCES:
        directory = root / package
        directory.mkdir()
        (directory / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    source_inventory = build_production_source_inventory_payload(root)
    source_files = source_inventory["files"]
    assert isinstance(source_files, list)
    files: dict[str, dict[str, object]] = {str(path): {} for path in source_files}
    report = _coverage_report()
    report["files"] = files
    test_inventory = build_test_inventory_payload(root, 1)
    baseline = coverage_baseline_payload(
        report,
        test_inventory=test_inventory,
        source_inventory=source_inventory,
    )
    baseline_path = root / "coverage-baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    calls: list[list[str]] = []
    environments: list[dict[str, str] | None] = []

    def fake_call(
        command: list[str] | tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> int:
        assert cwd == root
        observed = list(command)
        calls.append(observed)
        environments.append(env)
        if "json" in observed:
            output = Path(observed[observed.index("-o") + 1])
            output.write_text(json.dumps(report), encoding="utf-8")
        return 0

    monkeypatch.setattr(quality_gate.subprocess, "call", fake_call)

    summary = run_coverage_gate(
        root,
        baseline_path,
        data_file=tmp_path / "evidence" / ".coverage",
        report_path=tmp_path / "evidence" / "coverage.json",
        basetemp=tmp_path / "pytest",
    )

    run_command = calls[0]
    assert "--branch" in run_command
    assert f"--source={','.join(PRODUCTION_COVERAGE_SOURCES)}" in run_command
    assert str(root / "tests" / "test_alpha.py") in run_command
    assert str(root / "tests" / "nested" / "beta_test.py") in run_command
    pytest_environment = environments[0]
    assert pytest_environment is not None
    temporary_root = str(tmp_path.resolve())
    assert pytest_environment["TMPDIR"] == temporary_root
    assert pytest_environment["TEMP"] == temporary_root
    assert pytest_environment["TMP"] == temporary_root
    summary_inventory = summary["test_inventory"]
    assert isinstance(summary_inventory, dict)
    assert summary_inventory["file_count"] == 2


def test_live_architecture_gate_requires_the_exact_acyclic_v2_contract() -> None:
    payload = _architecture_payload()
    projections = payload["projections"]
    assert isinstance(projections, dict)
    registry = projections["capability_registry"]
    scope = projections["scope"]
    core_target = projections["core_target"]
    assert isinstance(registry, dict)
    assert isinstance(scope, dict)
    assert isinstance(core_target, dict)
    core_registry = core_target["registry"]
    core_scope = core_target["scope"]
    assert isinstance(core_registry, dict)
    assert isinstance(core_scope, dict)

    assert evaluate_architecture_payload(payload) == {
        "modules": len(core_scope["registered_modules"]),
        "production_relations": 0,
        "contract_violations": 0,
        "cyclic_components": 0,
        "contract_ids": sorted(EXPECTED_ARCHITECTURE_CONTRACTS),
        "grimp_version": "3.15",
        "architecture_baseline_id": EXPECTED_ARCHITECTURE_BASELINE_ID,
        "input_manifest_sha256": hashlib.sha256(b"[]").hexdigest(),
        "projection_policy_id": architecture_worker.CAPABILITY_PROJECTION_POLICY_ID,
        "capability_registry_fingerprint": registry["fingerprint"],
        "registered_capability_modules": len(scope["registered_modules"]),
        "missing_registered_capability_modules": 0,
        "owner_unmapped_modules": 0,
        "owner_overlapping_modules": 0,
        "family_unmapped_modules": 0,
        "family_overlapping_modules": 0,
        "family_forbidden_edges": 0,
        "owner_aggregate_quotient_sccs": 0,
        "family_aggregate_quotient_sccs": 0,
        "core_target_registry_fingerprint": core_registry["fingerprint"],
        "core_target_registered_modules": len(core_scope["registered_modules"]),
        "core_target_forbidden_direct_module_edges": 0,
        "core_target_family_regression_direct_module_edges": 0,
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("empty", "contract inventory drifted"),
        ("negative", "counter is malformed"),
        ("stale", "architecture baseline identity"),
        ("baseline_status", "live architecture contracts failed"),
        ("cycle", "module SCC inventories disagree"),
    ),
)
def test_live_architecture_gate_rejects_empty_negative_stale_or_cyclic_payloads(
    mutation: str, message: str
) -> None:
    payload = _architecture_payload()
    if mutation == "empty":
        payload["contract_evaluations"] = []
    elif mutation == "negative":
        counters = payload["counters"]
        assert isinstance(counters, dict)
        counters["modules"] = -1
    elif mutation == "stale":
        architecture = payload["architecture"]
        assert isinstance(architecture, dict)
        architecture["baseline_id"] = "neocortex-production-imports-2026-08-10/v1"
    elif mutation == "baseline_status":
        evaluations = payload["contract_evaluations"]
        assert isinstance(evaluations, list)
        evaluations[0]["status"] = "baseline"
    else:
        counters = payload["counters"]
        assert isinstance(counters, dict)
        counters["cyclic_components"] = 1
        payload["cycles"] = [{"cycle_id": "forbidden"}]

    with pytest.raises(GateError, match=message):
        evaluate_architecture_payload(payload)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("projection_schema", "projection schema drifted"),
        ("projection_policy", "projection policy drifted"),
        ("registry_fingerprint", "registry fingerprint drifted"),
        ("dag_fingerprint", "DAG policy drifted"),
        ("family_resolution_policy", "resolution policy drifted"),
        ("missing", "projection scope drifted"),
        ("owner_unmapped", "registered module mapping is incomplete"),
        ("family_overlap", "registered module mapping is incomplete"),
    ),
)
def test_live_architecture_gate_rejects_stale_or_incomplete_projection_evidence(
    mutation: str, message: str
) -> None:
    payload = _architecture_payload()
    projections = payload["projections"]
    assert isinstance(projections, dict)
    if mutation == "projection_schema":
        projections["schema"] = "neocortex.architecture-projection/v0"
    elif mutation == "projection_policy":
        projections["policy_id"] = "stale"
    elif mutation == "registry_fingerprint":
        registry = projections["capability_registry"]
        assert isinstance(registry, dict)
        registry["fingerprint"] = "capability-registry-v1:sha256:" + "0" * 64
    elif mutation == "dag_fingerprint":
        family = projections["target_family"]
        assert isinstance(family, dict)
        dag = family["dag"]
        assert isinstance(dag, dict)
        dag["fingerprint"] = "architecture-family-dag-v1:sha256:" + "0" * 64
    elif mutation == "family_resolution_policy":
        family = projections["target_family"]
        assert isinstance(family, dict)
        family["resolution_policy"] = "stale"
    elif mutation == "missing":
        scope = projections["scope"]
        assert isinstance(scope, dict)
        registered = scope["registered_modules"]
        assert isinstance(registered, list)
        scope["missing_registered_modules"] = [registered[0]]
    else:
        projection_name = "logical_owner" if mutation == "owner_unmapped" else "target_family"
        projection = projections[projection_name]
        assert isinstance(projection, dict)
        resolutions = projection["mapping_resolutions"]
        assert isinstance(resolutions, list)
        resolution = next(
            item
            for item in resolutions
            if isinstance(item, dict) and item.get("status") == "resolved"
        )
        assert isinstance(resolution, dict)
        if mutation == "owner_unmapped":
            resolution.update({"status": "unmapped", "labels": []})
        else:
            resolution.update(
                {
                    "status": "overlap",
                    "labels": [
                        EXPECTED_CAPABILITY_CANONICAL_FAMILY,
                        "neocortex.other.family",
                    ],
                }
            )

    with pytest.raises(GateError, match=message):
        evaluate_architecture_payload(payload)


def test_live_architecture_gate_rejects_core_family_baseline_regression() -> None:
    modules = architecture_worker._target_registry.registered_core_modules()
    foundation = [
        module
        for module in modules
        if architecture_worker._target_registry.matching_target_families(module) == ("foundation",)
    ]
    runtime = [
        module
        for module in modules
        if architecture_worker._target_registry.matching_target_families(module) == ("runtime",)
    ]
    imports = (
        ModuleImport(foundation[0], runtime[0]),
        ModuleImport(foundation[1], runtime[1]),
    )

    with pytest.raises(GateError, match="dependencies regressed"):
        evaluate_architecture_payload(_architecture_payload(imports))


def test_aggregate_owner_quotient_scc_is_diagnostic_not_a_gate() -> None:
    _, owners, families = architecture_worker._registered_capability_labels()
    archive = [
        module
        for module, labels in owners.items()
        if labels == ("archive",)
        and families[module] == (architecture_worker.CAPABILITY_CANONICAL_FAMILY,)
    ]
    docx = [
        module
        for module, labels in owners.items()
        if labels == ("docx",)
        and families[module] == (architecture_worker.CAPABILITY_CANONICAL_FAMILY,)
    ]
    payload = _architecture_payload(
        (
            ModuleImport(archive[0], docx[0]),
            ModuleImport(docx[1], archive[1]),
        )
    )

    summary = evaluate_architecture_payload(payload)

    assert summary["cyclic_components"] == 0
    assert summary["owner_aggregate_quotient_sccs"] == 1


def test_live_architecture_gate_rejects_an_empty_payload() -> None:
    with pytest.raises(GateError, match="unsupported schema"):
        evaluate_architecture_payload({})


def test_supply_chain_gate_allows_only_the_local_project_skip() -> None:
    clean = {
        "dependencies": [
            {"name": "neocortex-framework", "skip_reason": "not on PyPI", "vulns": []},
            {"name": "ruff", "version": "1", "vulns": []},
        ]
    }

    assert evaluate_audit_payload(
        clean,
        allowed_skips=frozenset({"neocortex-framework"}),
        allowed_vulnerabilities=frozenset(),
    ) == {
        "dependencies": 2,
        "skipped_local_projects": 1,
        "vulnerabilities": 0,
        "accepted_vulnerabilities": 0,
    }

    vulnerable = {
        "dependencies": [
            {"name": "neocortex-framework", "skip_reason": "not on PyPI", "vulns": []},
            {"name": "ruff", "version": "1", "vulns": [{"id": "GHSA-example"}]},
        ]
    }
    with pytest.raises(GateError, match="GHSA-example"):
        evaluate_audit_payload(
            vulnerable,
            allowed_skips=frozenset({"neocortex-framework"}),
            allowed_vulnerabilities=frozenset(),
        )


def test_supply_chain_gate_fails_closed_on_an_unexpected_skip() -> None:
    payload = {
        "dependencies": [
            {"name": "unknown-private", "skip_reason": "unresolved", "vulns": []},
        ]
    }

    with pytest.raises(GateError, match="unknown-private"):
        evaluate_audit_payload(
            payload,
            allowed_skips=frozenset({"neocortex-framework"}),
            allowed_vulnerabilities=frozenset(),
        )


def test_supply_chain_exceptions_are_policy_driven_not_globally_hardcoded() -> None:
    payload = {
        "dependencies": [
            {
                "name": "example_package",
                "version": "1.2.3",
                "vulns": [{"id": "GHSA-example"}],
            }
        ]
    }

    assert (
        evaluate_audit_payload(
            payload,
            allowed_skips=frozenset(),
            allowed_vulnerabilities=frozenset({("example-package", "1.2.3", "GHSA-example")}),
        )["accepted_vulnerabilities"]
        == 1
    )


def test_semgrep_receipt_must_match_exact_unexpired_isolation_policy() -> None:
    policy_path = Path(__file__).parents[1] / "tools" / "quality_gate_supply_policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    semgrep = policy["tool_runtimes"]["semgrep"]
    receipt = {
        key: semgrep[key]
        for key in (
            "schema_version",
            "kind",
            "tool",
            "version",
            "scan_wrapper",
            "scan_wrapper_sha256",
            "constraints_filename",
            "constraints_sha256",
            "pip_bootstrap_version",
            "pip_bootstrap_filename",
            "pip_bootstrap_sha256",
            "vulnerability_exceptions",
        )
    }
    receipt["python_executable"] = "bin/python"
    receipt["allowed_surfaces"] = semgrep["allowed_surfaces"]
    receipt["denied_console_entrypoints"] = semgrep["denied_console_entrypoints"]
    receipt["installed_packages"] = [
        {"name": name, "version": version} for name, version in semgrep["required_packages"].items()
    ]

    summary = evaluate_tool_runtime_receipt(receipt, policy, today=date.fromisoformat("2026-08-10"))

    assert summary["accepted_vulnerability_exceptions"] == 3
    assert summary["earliest_expiry"] == "2026-09-30"

    expired = json.loads(json.dumps(receipt))
    with pytest.raises(GateError, match="expired"):
        evaluate_tool_runtime_receipt(expired, policy, today=date.fromisoformat("2026-10-01"))
    altered = json.loads(json.dumps(receipt))
    altered["vulnerability_exceptions"][0]["reachable"] = True
    with pytest.raises(GateError, match="differ from policy"):
        evaluate_tool_runtime_receipt(altered, policy, today=date.fromisoformat("2026-08-10"))


def test_tool_runtime_audit_matches_pypi_primary_ids_through_exact_ghsa_aliases() -> None:
    policy_path = Path(__file__).parents[1] / "tools" / "quality_gate_supply_policy.json"
    exceptions = json.loads(policy_path.read_text(encoding="utf-8"))["tool_runtimes"]["semgrep"][
        "vulnerability_exceptions"
    ]
    payload = {
        "dependencies": [
            {
                "name": "mcp",
                "version": "1.23.3",
                "vulns": [
                    {"id": f"PYSEC-2026-{index}", "aliases": [item["id"]]}
                    for index, item in enumerate(exceptions, 3481)
                ],
            },
            {"name": "semgrep", "version": "1.172.0", "vulns": []},
        ]
    }

    summary = evaluate_tool_audit_payload(payload, exceptions)

    assert summary["vulnerabilities"] == 3
    assert summary["matched_policy_exceptions"] == 3

    unmatched = json.loads(json.dumps(payload))
    unmatched["dependencies"][0]["vulns"][0]["aliases"] = []
    with pytest.raises(GateError, match="unaccepted vulnerabilities"):
        evaluate_tool_audit_payload(unmatched, exceptions)


def test_supply_policy_matches_the_independent_semgrep_runtime_contract() -> None:
    policy_path = Path(__file__).parents[1] / "tools" / "quality_gate_supply_policy.json"
    semgrep = json.loads(policy_path.read_text(encoding="utf-8"))["tool_runtimes"]["semgrep"]

    assert semgrep["schema_version"] == semgrep_tool_contract.SEMGREP_TOOL_SCHEMA_VERSION
    assert semgrep["kind"] == semgrep_tool_contract.SEMGREP_TOOL_RECEIPT_KIND
    assert semgrep["version"] == semgrep_tool_contract.SEMGREP_TOOL_VERSION
    assert semgrep["scan_wrapper"] == semgrep_tool_contract.SEMGREP_SCAN_WRAPPER_NAME
    assert semgrep["scan_wrapper_sha256"] == semgrep_tool_contract.SEMGREP_SCAN_WRAPPER_SHA256
    assert semgrep["constraints_sha256"] == semgrep_tool_contract.SEMGREP_TOOL_CONSTRAINTS_SHA256
    assert semgrep["pip_bootstrap_sha256"] == semgrep_tool_contract.PIP_BOOTSTRAP_SHA256
    assert semgrep["allowed_surfaces"] == list(semgrep_tool_contract.SEMGREP_TOOL_ALLOWED_SURFACES)
    assert semgrep["denied_console_entrypoints"] == list(
        semgrep_tool_contract.SEMGREP_TOOL_DENIED_ENTRYPOINTS
    )
    assert semgrep["vulnerability_exceptions"] == [
        dict(item) for item in semgrep_tool_contract.SEMGREP_TOOL_VULNERABILITY_EXCEPTIONS
    ]


def test_pre_push_receipt_is_written_atomically_only_to_the_explicit_path(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "evidence" / "pre-push.json"
    payload = {
        "schema": "neocortex.quality-gate-receipt/v1",
        "repository": {"sha": "a" * 40},
        "test_inventory": {"file_count": 2, "sha256": "b" * 64},
        "result": "passed",
    }

    write_gate_receipt(destination, payload)

    assert json.loads(destination.read_text(encoding="utf-8")) == payload
    assert not destination.with_name(".pre-push.json.tmp").exists()


def test_pre_push_receipt_binds_sha_inventory_versions_commands_and_results(
    tmp_path: Path,
) -> None:
    payload = _pre_push_receipt_payload(
        root=tmp_path,
        sha="a" * 40,
        inventory={
            "file_count": 260,
            "total_bytes": 1234,
            "hash_algorithm": "sha256(path,lf-size,sha256(lf-content))-v1",
            "sha256": "b" * 64,
        },
        architecture={"grimp_version": "3.15", "contract_violations": 0},
        static_summary={"tools": {"ruff": {"version": "0.15.17", "total": 77}}},
        supply_chain={"main_runtime": {"pip_audit_version": "2.10.1", "vulnerabilities": 0}},
        coverage_summary={
            "coverage_version": "7.14.1",
            "metrics": {
                "lines": {"covered": 80, "total": 100},
                "branches": {"covered": 30, "total": 50},
            },
        },
        baseline_path=tmp_path / "baseline.json",
        coverage_baseline_path=tmp_path / "coverage-baseline.json",
        supply_policy=tmp_path / "policy.json",
        tool_receipt=tmp_path / "tools" / "semgrep" / "neocortex-tool-runtime.json",
        basetemp=tmp_path / "pytest",
        coverage_data_file=tmp_path / "coverage" / ".coverage",
        coverage_report=tmp_path / "coverage" / "coverage.json",
        post_coverage_snapshot={"sha": "a" * 40, "worktree": "clean"},
        final_snapshot={"sha": "a" * 40, "worktree": "clean"},
    )
    document = json.loads(json.dumps(payload))

    assert document["repository"] == {
        "root": str(tmp_path),
        "sha": "a" * 40,
        "branch": "main",
    }
    assert document["test_inventory"]["sha256"] == "b" * 64
    assert document["gates"]["architecture"]["evidence"]["grimp_version"] == "3.15"
    assert document["gates"]["static"]["evidence"]["tools"]["ruff"]["total"] == 77
    assert document["gates"]["supply_chain"]["result"] == "passed"
    assert "--tool-receipt" in document["gates"]["supply_chain"]["command"]
    assert document["gates"]["coverage"]["evidence"]["coverage_version"] == "7.14.1"
    assert "--basetemp" in document["gates"]["coverage"]["command"]
    assert "--data-file" in document["gates"]["coverage"]["command"]
    assert document["gates"]["final_git_snapshot"]["evidence"]["worktree"] == "clean"
    assert document["result"] == "passed"
