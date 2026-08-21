"""Functional contracts for the normalized external-provider platform."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import cast

import pytest

import _04_Nucleo_Operativo.code_review as code_review_module
import _04_Nucleo_Operativo.external_evidence_providers as providers_module
from _02_Deduplicacion import FileSnapshot
from _04_Nucleo_Operativo.cli_code import _read_code_status_snapshot
from _04_Nucleo_Operativo.code_contracts import CodeRouteConfig
from _04_Nucleo_Operativo.code_external_evidence import ExternalEvidenceFile
from _04_Nucleo_Operativo.code_publication_diff import compare_code_publications
from _04_Nucleo_Operativo.code_review import review_code_state
from _04_Nucleo_Operativo.code_route import CodeRoute
from _04_Nucleo_Operativo.code_schema import connect_code_state, readonly_code_database
from _04_Nucleo_Operativo.external_evidence_providers import (
    MypyTrustedProjectProvider,
    PyrightTrustedProjectProvider,
)
from _04_Nucleo_Operativo.external_evidence_store import (
    read_external_evidence_suite,
)
from _04_Nucleo_Operativo.self_analysis_status import (
    SelfAnalysisFreshness,
    SelfAnalysisStatus,
)
from _04_Nucleo_Operativo.semantic_models import fingerprint_bytes


class _Inventory:
    def __init__(self, paths: tuple[Path, ...]) -> None:
        self.paths = paths

    def snapshots(self, scan_id: int):
        del scan_id
        for path in self.paths:
            observed = path.stat()
            yield FileSnapshot(
                str(path),
                observed.st_dev,
                observed.st_ino,
                observed.st_size,
                observed.st_mtime_ns,
                getattr(observed, "st_birthtime_ns", observed.st_ctime_ns),
            )


class _FrameworkState:
    def begin_route_phase(self, *_args, **_kwargs) -> None:
        return None

    def complete_route_phase(self, *_args, **_kwargs) -> None:
        return None

    def fail_route_phase(self, *_args, **_kwargs) -> None:
        return None


def _tree(root: Path) -> tuple[Path, ...]:
    root.mkdir(parents=True)
    first = root / "a.py"
    second = root / "b.py"
    first.write_text("def f(x: int) -> str:\n    return x\n", encoding="utf-8")
    second.write_text("import os\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        """[tool.ruff]
target-version = "py313"
[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "B"]
[tool.mypy]
python_version = "3.13"
check_untyped_defs = true
[tool.pyright]
pythonVersion = "3.13"
typeCheckingMode = "basic"
""",
        encoding="utf-8",
    )
    return first, second


def _run(root: Path, state: Path, paths: tuple[Path, ...], run_id: int, profile: str):
    return CodeRoute(
        CodeRouteConfig(
            state_path=state / "code.sqlite3",
            dedup_path=state / "dedup.sqlite3",
            max_file_bytes=1024 * 1024,
            max_text_chars=100_000,
            chunk_chars=1024,
            include_generated=False,
            include_vendored=False,
            external_evidence_root=root,
            analysis_profile=profile,  # type: ignore[arg-type]
        ),
        _Inventory(paths),
        _FrameworkState(),
        run_id,
        run_id,
    ).run()


def _external_file(path: Path, root: Path, version_id: int = 1) -> ExternalEvidenceFile:
    observed = path.stat()
    fingerprint = fingerprint_bytes(path.read_bytes())
    return ExternalEvidenceFile(
        version_id,
        str(path),
        path.relative_to(root).as_posix(),
        observed.st_size,
        observed.st_mtime_ns,
        fingerprint.xxh3_128,
        fingerprint.xxh3_64_guard,
    )


def test_protected_provider_is_normalized_and_replays_exactly(tmp_path: Path) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    paths = _tree(root)

    first = _run(root, state, paths, 1, "protected")
    second = _run(root, state, paths, 2, "protected")

    assert first.external_tool_runs == second.external_tool_runs == 1
    assert second.external_cache_hits == 1
    with readonly_code_database(state / "code.sqlite3") as connection:
        suite = read_external_evidence_suite(connection, 2, enforce_current_runtime=True)
        rows = connection.execute("SELECT COUNT(*) FROM external_run_contracts").fetchone()[0]
    assert rows == 2
    assert suite.status == "ready"
    assert suite.profile == "protected"
    assert suite.providers[0].provider_id == "ruff-protected-basic"
    assert suite.providers[0].execution == "cache_replay"
    assert suite.providers[0].gate == "passed"
    assert suite.providers[0].counters["process_invocations"] == 0
    assert suite.providers[0].counters["cache_hits"] == 1
    assert suite.providers[0].counters["files_verified"] == len(paths)
    assert suite.providers[0].counters["bytes_verified"] > 0
    gates = {item.gate: item.status for item in suite.gates}
    assert gates["no_added_ruff_basic_diagnostics"] == "passed"
    assert gates["no_added_mypy_errors"] == "not_evaluated"
    assert gates["no_added_pyright_errors"] == "not_evaluated"


def test_trusted_static_runs_independent_provider_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    paths = _tree(root)
    package_version = providers_module._package_version
    unavailable = {
        "deptry",
        "neocortex-framework",
        "pip-audit",
        "semgrep",
    }
    monkeypatch.setattr(
        providers_module,
        "_package_version",
        lambda name: None if name in unavailable else package_version(name),
    )
    monkeypatch.setattr(providers_module, "managed_semgrep_version", lambda: None)

    summary = _run(root, state, paths, 1, "trusted-static")
    replay_summary = _run(root, state, paths, 2, "trusted-static")

    with readonly_code_database(state / "code.sqlite3") as connection:
        suite = read_external_evidence_suite(connection, 2, enforce_current_runtime=True)
        provider_rows = connection.execute(
            """SELECT c.provider_id,COUNT(*) FROM external_run_contracts c
            GROUP BY c.provider_id ORDER BY c.provider_id"""
        ).fetchall()
        sources = {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT source FROM diagnostics WHERE source LIKE 'external:%'"
            )
        }
    providers = {item.provider_id: item for item in suite.providers}
    assert summary.external_tool_runs == 13
    assert replay_summary.external_tool_runs == 13
    assert tuple(row[0] for row in provider_rows) == (
        "complexipy-cognitive",
        "deptry-project-dependencies",
        "git-history-local",
        "grimp-architecture",
        "installed-package-inventory",
        "mypy-trusted-project",
        "pip-audit-known-vulnerabilities",
        "pyright-trusted-project",
        "ruff-analyze-imports",
        "ruff-protected-basic",
        "ruff-trusted-project",
        "semgrep-neocortex-invariants",
        "vulture-unused-static",
    )
    assert providers["ruff-protected-basic"].status == "ready"
    assert providers["ruff-trusted-project"].status == "ready"
    assert providers["mypy-trusted-project"].status == "ready"
    assert providers["pyright-trusted-project"].status in {"ready", "abstained"}
    assert providers["vulture-unused-static"].status == "ready"
    for provider_id in (
        "deptry-project-dependencies",
        "installed-package-inventory",
        "pip-audit-known-vulnerabilities",
        "semgrep-neocortex-invariants",
    ):
        assert providers[provider_id].status == "abstained"
        assert providers[provider_id].reason == "provider_unavailable"
    for provider_id in (
        "complexipy-cognitive",
        "grimp-architecture",
        "ruff-analyze-imports",
    ):
        assert providers[provider_id].status == "abstained"
        assert providers[provider_id].eligible_files == 0
        assert providers[provider_id].covered_files == 0
    assert "external:ruff-protected-basic" in sources
    assert "external:ruff-trusted-project" in sources
    assert "external:mypy" in sources
    assert "external:vulture-unused-static" in sources
    assert suite.type_consensus.status in {"both_report", "not_comparable"}
    for provider_id in (
        "ruff-protected-basic",
        "ruff-trusted-project",
        "mypy-trusted-project",
        "vulture-unused-static",
    ):
        assert providers[provider_id].execution == "cache_replay"
        assert providers[provider_id].counters["process_invocations"] == 0
        assert providers[provider_id].counters["cache_hits"] == 1
    assert providers["mypy-trusted-project"].limitations == (
        "unresolved_third_party_imports_are_treated_as_any",
    )
    if providers["pyright-trusted-project"].status == "abstained":
        assert providers["pyright-trusted-project"].reason == "provider_unavailable"
        assert providers["pyright-trusted-project"].eligible_files == len(paths)
        assert providers["pyright-trusted-project"].covered_files == 0
        assert providers["pyright-trusted-project"].counters["unavailable"] == 1
        assert suite.status == "partial"


def test_mypy_parser_preserves_structured_location_and_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    scratch = tmp_path / "scratch"
    source = _tree(root)[0]
    scratch.mkdir()
    monkeypatch.setattr(providers_module, "_package_version", lambda _name: "2.1.0")
    provider = MypyTrustedProjectProvider(root)

    def fake_run(arguments, **_kwargs):
        argument_file = Path(next(str(item)[1:] for item in arguments if str(item).startswith("@")))
        staged_path = argument_file.read_text(encoding="utf-8").splitlines()[0]
        output = json.dumps(
            {
                "file": staged_path,
                "line": 2,
                "column": 11,
                "end_line": 2,
                "end_column": 12,
                "severity": "error",
                "message": "Incompatible return value type",
                "code": "return-value",
            }
        ).encode("utf-8")
        return subprocess.CompletedProcess(tuple(arguments), 1, output + b"\n", b"")

    monkeypatch.setattr(providers_module, "run_bounded_capture", fake_run)
    publication = provider.run(
        root,
        (_external_file(source, root),),
        baseline=None,
        scratch_root=scratch,
    )

    assert publication.status == "completed", publication.publication.provenance.get("error")
    assert publication.coverage_complete is True
    assert publication.counters["process_invocations"] == 1
    assert len(publication.findings) == 1
    finding = publication.findings[0]
    assert finding.category == "typing"
    assert finding.code == "return-value"
    assert finding.severity == "error"
    assert (finding.start_line, finding.start_column) == (2, 11)
    assert (finding.end_line, finding.end_column) == (2, 12)


def test_mypy_parser_normalizes_line_level_negative_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    scratch = tmp_path / "scratch"
    source = _tree(root)[0]
    scratch.mkdir()
    monkeypatch.setattr(providers_module, "_package_version", lambda _name: "2.1.0")
    provider = MypyTrustedProjectProvider(root)

    def fake_run(arguments, **_kwargs):
        argument_file = Path(next(str(item)[1:] for item in arguments if str(item).startswith("@")))
        staged_path = argument_file.read_text(encoding="utf-8").splitlines()[0]
        output = json.dumps(
            {
                "file": staged_path,
                "line": 180,
                "column": -1,
                "end_line": 180,
                "end_column": -1,
                "severity": "error",
                "message": 'Unused "type: ignore" comment',
                "code": "unused-ignore",
            }
        ).encode("utf-8")
        return subprocess.CompletedProcess(tuple(arguments), 1, output + b"\n", b"")

    monkeypatch.setattr(providers_module, "run_bounded_capture", fake_run)
    publication = provider.run(
        root,
        (_external_file(source, root),),
        baseline=None,
        scratch_root=scratch,
    )

    assert publication.status == "completed", publication.publication.provenance.get("error")
    assert len(publication.findings) == 1
    finding = publication.findings[0]
    assert finding.code == "unused-ignore"
    assert (finding.start_line, finding.start_column) == (180, 0)
    assert (finding.end_line, finding.end_column) == (180, 0)
    assert finding.metadata["location_precision"] == "line"


def test_mypy_parser_collapses_identical_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    scratch = tmp_path / "scratch"
    source = _tree(root)[0]
    scratch.mkdir()
    monkeypatch.setattr(providers_module, "_package_version", lambda _name: "2.1.0")
    provider = MypyTrustedProjectProvider(root)

    def fake_run(arguments, **_kwargs):
        argument_file = Path(next(str(item)[1:] for item in arguments if str(item).startswith("@")))
        staged_path = argument_file.read_text(encoding="utf-8").splitlines()[0]
        output = json.dumps(
            {
                "file": staged_path,
                "line": 2,
                "column": 11,
                "end_line": 2,
                "end_column": 12,
                "severity": "error",
                "message": "Incompatible return value type",
                "code": "return-value",
            }
        ).encode("utf-8")
        return subprocess.CompletedProcess(
            tuple(arguments),
            1,
            output + b"\n" + output + b"\n",
            b"",
        )

    monkeypatch.setattr(providers_module, "run_bounded_capture", fake_run)
    publication = provider.run(
        root,
        (_external_file(source, root),),
        baseline=None,
        scratch_root=scratch,
    )

    assert publication.status == "completed", publication.publication.provenance.get("error")
    assert publication.counters["process_invocations"] == 1
    assert len(publication.findings) == 1
    finding = publication.findings[0]
    assert finding.code == "return-value"
    assert finding.metadata["duplicate_observations"] == 2


def test_mypy_staged_project_resolves_first_party_imports(tmp_path: Path) -> None:
    root = tmp_path / "root with spaces"
    package = root / "pkg"
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[tool.mypy]\npython_version = "3.13"\nexplicit_package_bases = true\n',
        encoding="utf-8",
    )
    init = package / "__init__.py"
    dependency = package / "dependency.py"
    consumer = package / "consumer.py"
    init.write_text("\n", encoding="utf-8")
    dependency.write_text("VALUE: int = 1\n", encoding="utf-8")
    consumer.write_text("from pkg.dependency import VALUE\nresult: int = VALUE\n", encoding="utf-8")
    files = tuple(
        _external_file(path, root, version_id=index)
        for index, path in enumerate((init, dependency, consumer), start=1)
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    publication = MypyTrustedProjectProvider(root).run(
        root,
        files,
        baseline=None,
        scratch_root=scratch,
    )

    assert publication.status == "completed", publication.publication.provenance.get("error")
    assert not publication.findings


def test_mypy_staged_project_preserves_namespace_module_identity(tmp_path: Path) -> None:
    root = tmp_path / "root"
    tests_root = root / "tests"
    tests_root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[tool.mypy]\npython_version = "3.13"\nexplicit_package_bases = true\n',
        encoding="utf-8",
    )
    audit_guard = tests_root / "audit_lab_guard.py"
    conftest = tests_root / "conftest.py"
    audit_guard.write_text("LAB_ROOT: str = 'isolated'\n", encoding="utf-8")
    conftest.write_text(
        "from tests.audit_lab_guard import LAB_ROOT\nconfigured_root: str = LAB_ROOT\n",
        encoding="utf-8",
    )
    files = tuple(
        _external_file(path, root, version_id=index)
        for index, path in enumerate((audit_guard, conftest), start=1)
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    publication = MypyTrustedProjectProvider(root).run(
        root,
        files,
        baseline=None,
        scratch_root=scratch,
    )

    assert publication.status == "completed", publication.publication.provenance.get("error")
    assert publication.coverage_complete is True
    assert len(publication.inputs) == len(files)
    assert publication.counters["files_verified"] == len(files)
    assert publication.counters["process_invocations"] == 1
    assert not publication.findings


def test_pyright_parser_preserves_structured_range_and_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    scratch = tmp_path / "scratch"
    source = _tree(root)[0]
    scratch.mkdir()
    monkeypatch.setattr(
        providers_module,
        "_pyright_locations",
        lambda: (Path("C:/fixture/node.exe"), Path("C:/fixture/pyright/index.js"), "1.1.411"),
    )
    provider = PyrightTrustedProjectProvider(root)
    assert provider.descriptor.execution_strategy.endswith("node-memory-v4")
    assert provider.descriptor.limits.memory_bound_bytes == 3 * 1024 * 1024 * 1024

    def fake_run(arguments, **kwargs):
        assert arguments[1] == "--max-old-space-size=2304"
        expected_process_limit = 3 * 1024 * 1024 * 1024 if os.name == "nt" else None
        assert kwargs["memory_limit_bytes"] == expected_process_limit
        config_path = Path(arguments[arguments.index("--project") + 1])
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert config["executionEnvironments"][0]["root"] == "source"
        assert config["extraPaths"] == [str(provider._runtime_search_path())]
        staged_path = os.path.abspath(Path(kwargs["cwd"]) / config["include"][0])
        output = {
            "generalDiagnostics": [
                {
                    "file": staged_path,
                    "severity": "information",
                    "message": "Type int is not assignable to str",
                    "rule": "reportReturnType",
                    "range": {
                        "start": {"line": 1, "character": 11},
                        "end": {"line": 1, "character": 12},
                    },
                },
                {
                    "file": staged_path,
                    "severity": "warning",
                    "message": f"Import cycle detected\n  {staged_path}",
                    "rule": "reportImportCycles",
                },
            ],
            "summary": {"filesAnalyzed": len(config["include"])},
        }
        return subprocess.CompletedProcess(
            tuple(arguments), 1, json.dumps(output).encode("utf-8"), b""
        )

    monkeypatch.setattr(providers_module, "run_bounded_capture", fake_run)
    publication = provider.run(
        root,
        (_external_file(source, root),),
        baseline=None,
        scratch_root=scratch,
    )

    assert publication.status == "completed"
    assert len(publication.findings) == 2
    finding = next(item for item in publication.findings if item.code == "reportReturnType")
    assert finding.code == "reportReturnType"
    assert finding.severity == "info"
    assert (finding.start_line, finding.start_column) == (2, 11)
    assert (finding.end_line, finding.end_column) == (2, 12)
    file_level = next(item for item in publication.findings if item.code == "reportImportCycles")
    assert (file_level.start_line, file_level.start_column) == (1, 0)
    assert file_level.metadata["location_precision"] == "file"
    assert file_level.message.startswith("Import cycle detected\n  <project>")
    assert str(scratch).casefold() not in file_level.message.casefold()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
def test_pyright_prefers_the_owned_release_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    owned_node = runtime / "tools" / "node" / "bin" / "node"
    owned_node.parent.mkdir(parents=True)
    owned_node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    owned_node.chmod(0o755)
    pyright = runtime / "tools" / "pyright" / "node_modules" / "pyright"
    pyright.mkdir(parents=True)
    (pyright / "index.js").write_text("", encoding="utf-8")
    (pyright / "package.json").write_text('{"version":"1.1.411"}', encoding="utf-8")
    system_bin = tmp_path / "system-bin"
    system_bin.mkdir()
    system_node = system_bin / "node"
    system_node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    system_node.chmod(0o755)
    monkeypatch.setattr(providers_module.sys, "prefix", str(runtime))
    monkeypatch.setenv("PATH", str(system_bin))

    node, index, version = providers_module._pyright_locations()

    assert node == owned_node
    assert index == pyright / "index.js"
    assert version == "1.1.411"


def test_malformed_provider_output_abstains_without_partial_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    scratch = tmp_path / "scratch"
    source = _tree(root)[0]
    scratch.mkdir()
    monkeypatch.setattr(providers_module, "_package_version", lambda _name: "2.1.0")
    provider = MypyTrustedProjectProvider(root)
    monkeypatch.setattr(
        providers_module,
        "run_bounded_capture",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(
            tuple(arguments), 1, b"not-json\n", b""
        ),
    )

    publication = provider.run(
        root,
        (_external_file(source, root),),
        baseline=None,
        scratch_root=scratch,
    )

    assert publication.status == "failed"
    assert publication.coverage_complete is False
    assert publication.findings == ()
    assert publication.counters["errors"] == 1
    error = cast(dict[str, object], publication.publication.provenance["error"])
    assert error["reason"] == ("provider_failure:ValueError:mypy JSON Lines output is malformed")
    assert publication.limitations[0].startswith("provider_failure:ValueError:")


def test_unexpected_provider_exit_retains_bounded_single_line_detail() -> None:
    completed = subprocess.CompletedProcess(
        ("tool",),
        134,
        b"",
        b"fatal allocation\nwith detail\n",
    )

    assert providers_module._unexpected_exit_message("pyright", completed) == (
        "pyright_unexpected_exit:134:fatal allocation with detail"
    )


def test_runtime_staleness_and_projection_corruption_fail_closed_per_provider(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    paths = _tree(root)
    _run(root, state, paths, 1, "protected")
    database = state / "code.sqlite3"

    connection = connect_code_state(database, create=False)
    try:
        connection.execute(
            """UPDATE external_tool_runs SET configuration_signature='stale'
            WHERE tool_run_id=(SELECT tool_run_id FROM external_run_contracts
            WHERE provider_id='ruff-protected-basic' ORDER BY tool_run_id DESC LIMIT 1)"""
        )
        connection.commit()
    finally:
        connection.close()
    with readonly_code_database(database) as connection:
        stale = read_external_evidence_suite(connection, 1, enforce_current_runtime=True)
    assert stale.providers[0].status == "abstained"
    assert stale.providers[0].reason == "external_provider_runtime_stale"

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """UPDATE diagnostics SET message='tampered'
            WHERE diagnostic_id=(SELECT projected_diagnostic_id
            FROM external_findings WHERE tool_run_id=(SELECT tool_run_id
            FROM external_run_contracts WHERE provider_id='ruff-protected-basic'
            ORDER BY tool_run_id DESC LIMIT 1) LIMIT 1)"""
        )
        connection.commit()
    finally:
        connection.close()
    with readonly_code_database(database) as connection:
        corrupted = read_external_evidence_suite(connection, 1, enforce_current_runtime=False)
    assert corrupted.providers[0].status == "abstained"
    assert corrupted.providers[0].reason == "external_provider_projection_invalid"


def test_provider_environment_identity_ignores_only_equivalent_release_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers_module.sys, "prefix", "/release-a")
    monkeypatch.setattr(providers_module.sys, "executable", "/release-a/bin/python3.14")
    first = providers_module._environment_signature(
        tool_name="fixture-tool",
        tool_version="1.2.3",
        node_path="/release-a/tools/node/bin/node",
        path_value="/release-a/tools/bin:/usr/bin",
    )

    monkeypatch.setattr(providers_module.sys, "prefix", "/release-b")
    monkeypatch.setattr(providers_module.sys, "executable", "/release-b/bin/python3.14")
    relocated = providers_module._environment_signature(
        tool_name="fixture-tool",
        tool_version="1.2.3",
        node_path="/release-b/tools/node/bin/node",
        path_value="/release-b/tools/bin:/usr/bin",
    )
    changed_tool = providers_module._environment_signature(
        tool_name="fixture-tool",
        tool_version="1.2.4",
        node_path="/release-b/tools/node/bin/node",
        path_value="/release-b/tools/bin:/usr/bin",
    )

    assert relocated == first
    assert changed_tool != first


def test_status_review_and_diff_consume_the_normalized_provider_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    baseline_state = tmp_path / "baseline"
    current_state = tmp_path / "current"
    paths = _tree(root)
    _run(root, baseline_state, paths, 1, "protected")
    _run(root, current_state, paths, 1, "protected")

    snapshot = _read_code_status_snapshot(current_state / "code.sqlite3")
    suite_payload = snapshot.external_evidence_suite
    assert suite_payload["schema"] == "neocortex.external-evidence-suite/v1"
    assert suite_payload["profile"] == "protected"
    provider_payloads = cast(list[dict[str, object]], suite_payload["providers"])
    assert provider_payloads[0]["provider_id"] == "ruff-protected-basic"

    status = SelfAnalysisStatus(
        "valid",
        {
            "run": {"root": str(root)},
            "inventory": {"mode": "full", "journal": {"status": "unavailable"}},
        },
        SelfAnalysisFreshness(True, True, True, "unavailable", True),
    )
    monkeypatch.setattr(
        code_review_module,
        "read_self_analysis_status",
        lambda _state, _run: status,
    )
    review = review_code_state(current_state)
    assert review.status == "ready"
    assert review.external_evidence_suite is not None
    assert review.external_evidence_suite.profile == "protected"
    assert review.external_evidence_suite.providers[0].provider_id == ("ruff-protected-basic")
    review_suite = cast(dict[str, object], review.as_payload()["external_evidence_suite"])
    assert review_suite["schema"] == ("neocortex.external-evidence-suite/v1")

    first_diff = compare_code_publications(baseline_state, current_state)
    second_diff = compare_code_publications(baseline_state, current_state)
    assert first_diff == second_diff
    assert first_diff.status == "ready"
    diff_payload = first_diff.as_payload()
    assert diff_payload["schema"] == "neocortex.code-publication-diff/v10"
    compatible_schemas = diff_payload["compatible_schemas"]
    assert isinstance(compatible_schemas, list)
    assert compatible_schemas == []
    assert first_diff.analysis_profile == "protected"
    assert len(first_diff.providers) == 1
    assert first_diff.providers[0].provider_id == "ruff-protected-basic"
    assert first_diff.providers[0].gate == "passed"
    assert first_diff.verdict == "equivalent_under_observed_metrics"


def test_diff_retains_comparable_provider_verdict_when_suite_expands(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    baseline_state = tmp_path / "baseline"
    current_state = tmp_path / "current"
    paths = _tree(root)
    _run(root, baseline_state, paths, 1, "protected")
    _run(root, current_state, paths, 1, "trusted-static")

    result = compare_code_publications(baseline_state, current_state)

    providers = {item.provider_id: item for item in result.providers}
    assert result.status == "ready"
    assert providers["ruff-protected-basic"].status == "ready"
    assert providers["mypy-trusted-project"].status == "not_evaluated"
    assert result.verdict == "equivalent_under_observed_metrics"
    assert "provider_verdict_uses_only_comparable_providers" in result.limitations
