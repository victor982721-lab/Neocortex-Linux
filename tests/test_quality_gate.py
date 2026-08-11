"""Contracts for the deterministic local/CI quality gate."""

from __future__ import annotations

import hashlib
import json
import sysconfig
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from neocortex import semgrep_tool_contract
from tools import quality_gate
from tools.quality_gate import (
    BASELINE_SCHEMA,
    COVERAGE_BASELINE_SCHEMA,
    EXPECTED_ARCHITECTURE_BASELINE_ID,
    EXPECTED_ARCHITECTURE_CONTRACTS,
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
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (root / "tests" / "test_alpha.py").write_text("def test_alpha(): pass\n", encoding="utf-8")
    (root / "tests" / "nested" / "beta_test.py").write_text(
        "def test_beta(): pass\n" * 3, encoding="utf-8"
    )
    (root / "tests" / "helpers.py").write_text("VALUE = 1\n", encoding="utf-8")
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
        "files": {"Orquestador.py": {}},
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


def _architecture_payload() -> dict[str, object]:
    return {
        "schema": "neocortex.external-architecture-worker/grimp-v1",
        "status": "ready",
        "counters": {
            "modules": 10,
            "production_relations": 20,
            "contract_violations": 0,
            "cyclic_components": 0,
        },
        "contract_evaluations": [
            {"contract": {"contract_id": contract_id}, "status": "passed"}
            for contract_id in sorted(EXPECTED_ARCHITECTURE_CONTRACTS)
        ],
        "cycles": [],
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


def test_push_ci_uses_dynamic_total_shards_instead_of_manual_test_lists() -> None:
    workflow_path = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    fast_and_quality, remaining = workflow.split("  standard:\n", 1)
    fast, quality = fast_and_quality.split("  quality:\n", 1)
    standard, _deep = remaining.split("  deep-windows:\n", 1)

    assert "tests/test_" not in fast_and_quality
    assert "tests/test_" not in standard
    assert "quality_gate.py inventory --shard-count 2" in fast_and_quality
    assert "quality_gate.py coverage" in fast_and_quality
    assert "actions/upload-artifact@v7" in fast_and_quality
    assert "--no-install-recommends ffmpeg libegl1" not in fast
    assert "--no-install-recommends ffmpeg libegl1" in quality
    assert "semgrep_tool_runtime.py install" in fast_and_quality
    assert "--tool-receipt" in fast_and_quality
    assert "quality_gate.py tests" in standard
    assert "--no-install-recommends ffmpeg libegl1" in standard
    assert "os: [ubuntu-latest, windows-latest]" in standard
    assert 'python: ["3.13", "3.14"]' in standard
    assert "shard: [0, 1]" in standard
    assert workflow.count("python -I tools/bootstrap_pip.py") == 5
    assert workflow.count("python -I tools/pyright_runtime.py install --target") == 2
    assert "npm install --prefix" not in workflow
    assert "NEOCORTEX_BOOTSTRAP_PIP_VERSION" not in workflow
    assert '--constraint constraints.txt ".[analysis]"' in fast_and_quality
    assert "quality_gate.py wheel-smoke" in standard
    assert workflow.count("git-snapshot --sha") == 6
    assert WHEEL_PACKAGE_ROOTS == (
        "_01_Enumeracion",
        "_02_Deduplicacion",
        "_03_Progreso",
        "_04_Nucleo_Operativo",
        "_05_Interfaz",
        "neocortex",
    )


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


def test_pyright_policy_uses_the_live_interpreter_packages_not_missing_import_debt() -> None:
    root = Path(__file__).parents[1]

    payload = quality_gate._pyright_config_payload(root)

    extra_paths = payload["extraPaths"]
    assert isinstance(extra_paths, list)
    assert extra_paths[0] == str(root)
    assert str(Path(sysconfig.get_path("purelib")).resolve()) in extra_paths
    assert payload["pythonVersion"] == "3.13"
    assert payload["typeCheckingMode"] == "basic"


def test_coverage_baseline_is_branch_aware_versioned_and_uses_production_scope() -> None:
    report = _coverage_report()
    baseline = coverage_baseline_payload(
        report,
        test_inventory=_coverage_inventory("tests/test_alpha.py", "tests/test_beta.py"),
        source_inventory=_coverage_inventory("Orquestador.py", "neocortex/__init__.py"),
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
            source_inventory=_coverage_inventory("Orquestador.py", "neocortex/__init__.py"),
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
        source_inventory=_coverage_inventory("Orquestador.py", "neocortex/__init__.py"),
    )

    with pytest.raises(GateError, match=message):
        compare_coverage_report(
            report,
            baseline,
            test_inventory=_coverage_inventory("tests/test_alpha.py", "tests/test_beta.py"),
            source_inventory=_coverage_inventory("Orquestador.py", "neocortex/__init__.py"),
        )


def test_coverage_gate_rejects_non_branch_report_and_tool_version_drift() -> None:
    baseline = coverage_baseline_payload(
        _coverage_report(),
        test_inventory=_coverage_inventory("tests/test_alpha.py", "tests/test_beta.py"),
        source_inventory=_coverage_inventory("Orquestador.py", "neocortex/__init__.py"),
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
            source_inventory=_coverage_inventory("Orquestador.py", "neocortex/__init__.py"),
        )
    with pytest.raises(GateError, match="does not match baseline"):
        compare_coverage_report(
            _coverage_report(version="7.15.0"),
            baseline,
            test_inventory=_coverage_inventory("tests/test_alpha.py", "tests/test_beta.py"),
            source_inventory=_coverage_inventory("Orquestador.py", "neocortex/__init__.py"),
        )


@pytest.mark.parametrize(
    ("test_inventory", "source_inventory", "message"),
    (
        (
            _coverage_inventory("tests/test_alpha.py"),
            _coverage_inventory("Orquestador.py", "neocortex/__init__.py"),
            "test paths removed",
        ),
        (
            _coverage_inventory("tests/test_alpha.py", "tests/test_beta.py"),
            _coverage_inventory("Orquestador.py"),
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
        source_inventory=_coverage_inventory("Orquestador.py", "neocortex/__init__.py"),
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
        source_inventory=_coverage_inventory("Orquestador.py"),
    )

    summary = compare_coverage_report(
        _coverage_report(),
        baseline,
        test_inventory=_coverage_inventory("tests/test_alpha.py", "tests/test_new.py"),
        source_inventory=_coverage_inventory("Orquestador.py", "neocortex/new.py"),
    )

    assert summary["metrics"] == baseline["approved"]


def test_coverage_inventory_requires_every_production_root(tmp_path: Path) -> None:
    root = _repository_fixture(tmp_path)
    (root / "Orquestador.py").write_text("VALUE = 1\n", encoding="utf-8")
    for package in PRODUCTION_COVERAGE_SOURCES[1:-1]:
        directory = root / package
        directory.mkdir()
        (directory / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")

    with pytest.raises(GateError, match="production coverage root is missing"):
        discover_production_sources(root)


def test_coverage_runner_uses_branch_mode_exact_scope_and_dynamic_total_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository_fixture(tmp_path)
    (root / "Orquestador.py").write_text("VALUE = 1\n", encoding="utf-8")
    for package in PRODUCTION_COVERAGE_SOURCES[1:]:
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

    assert evaluate_architecture_payload(payload) == {
        "modules": 10,
        "production_relations": 20,
        "contract_violations": 0,
        "cyclic_components": 0,
        "contract_ids": sorted(EXPECTED_ARCHITECTURE_CONTRACTS),
        "grimp_version": "3.15",
        "architecture_baseline_id": EXPECTED_ARCHITECTURE_BASELINE_ID,
        "input_manifest_sha256": hashlib.sha256(b"[]").hexdigest(),
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("empty", "contract inventory drifted"),
        ("negative", "module or relation counters are invalid"),
        ("stale", "architecture baseline identity"),
        ("baseline_status", "live architecture contracts failed"),
        ("cycle", "live architecture contracts failed"),
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
