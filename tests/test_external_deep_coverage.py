"""Focused contracts for trusted-deep orchestration and normalization."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import _04_Nucleo_Operativo.external_deep_coverage as deep
from _04_Nucleo_Operativo.code_external_evidence import ExternalEvidenceFile
from _04_Nucleo_Operativo.semantic_models import fingerprint_bytes


_VERSIONS = {"coverage": "7.14.0", "pytest": "9.1.0", "python": "3.13.5"}


def _owner(root: Path, relative_path: str, version_id: int) -> ExternalEvidenceFile:
    path = root / Path(relative_path)
    metadata = path.stat()
    digest = fingerprint_bytes(path.read_bytes())
    return ExternalEvidenceFile(
        version_id,
        str(path),
        relative_path,
        metadata.st_size,
        metadata.st_mtime_ns,
        digest.xxh3_128,
        digest.xxh3_64_guard,
    )


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, dict[str, ExternalEvidenceFile]]:
    trusted = tmp_path / "trusted"
    stage = tmp_path / "stage"
    scratch = tmp_path / "scratch"
    (trusted / "_04_Nucleo_Operativo").mkdir(parents=True)
    (trusted / "tests").mkdir()
    stage.mkdir()
    scratch.mkdir()
    (trusted / "_04_Nucleo_Operativo" / "logic.py").write_text(
        "def choose(value: bool) -> int:\n    if value:\n        return 1\n    return 2\n",
        encoding="utf-8",
    )
    (trusted / "tests" / "test_logic.py").write_text(
        "from _04_Nucleo_Operativo.logic import choose\n\n"
        "def test_true():\n"
        "    assert choose(True) == 1\n\n"
        "def test_false():\n"
        "    assert choose(False) == 2\n\n"
        "def test_again():\n"
        "    assert choose(True) == 1\n",
        encoding="utf-8",
    )
    (trusted / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    (trusted / "tests" / "cases.json").write_text('{"case":1}\n', encoding="utf-8")
    git = shutil.which("git")
    assert git is not None
    subprocess.run(
        (git, "init", "--quiet", str(trusted)),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    owners = (
        _owner(trusted, "_04_Nucleo_Operativo/logic.py", 1),
        _owner(trusted, "tests/test_logic.py", 2),
    )
    staged = {
        os.path.normcase(os.path.abspath(stage / Path(item.relative_path))): item for item in owners
    }
    monkeypatch.setattr(deep, "_canonical_repository_root", lambda: trusted)
    monkeypatch.setattr(deep, "_tool_versions", lambda: dict(_VERSIONS))
    return trusted, stage, scratch, staged


def _config(
    *,
    max_tests: int = 10,
    shard_size: int = 2,
    selectors: tuple[str, ...] = (),
    budget: float = 30.0,
) -> deep.DeepCoverageConfig:
    return deep.DeepCoverageConfig(selectors, max_tests, budget, shard_size, "fixture-config-v1")


def _worker(
    nodeids: tuple[str, ...],
    *,
    failed: frozenset[str] = frozenset(),
):
    calls: list[tuple[str, tuple[str, ...], float]] = []

    def run(request, *, scratch_root, environment, timeout_seconds):
        del scratch_root, environment
        mode = str(request["mode"])
        selected = tuple(str(value) for value in request["nodeids"])
        calls.append((mode, selected, timeout_seconds))
        signature = deep._request_digest(request)
        if mode == "collect":
            return (
                {
                    "schema": deep.DEEP_COVERAGE_COLLECT_SCHEMA,
                    "status": "ready",
                    "mode": "collect",
                    "request_signature": signature,
                    "tool_versions": dict(_VERSIONS),
                    "nodeids": list(nodeids),
                    "symbols": [
                        {
                            "relative_path": "_04_Nucleo_Operativo/logic.py",
                            "module": "_04_Nucleo_Operativo.logic",
                            "qualified_name": "_04_Nucleo_Operativo.logic.choose",
                            "kind": "function",
                            "start_line": 1,
                            "end_line": 4,
                        }
                    ],
                },
                31,
                0,
            )
        tests = [
            {"nodeid": nodeid, "outcome": "failed" if nodeid in failed else "passed"}
            for nodeid in selected
        ]
        failures = [
            {
                "nodeid": nodeid,
                "phase": "call",
                "message": "assertion failed",
                "relative_path": "tests/test_logic.py",
                "line": 4,
            }
            for nodeid in selected
            if nodeid in failed
        ]
        contexts = {
            "1": [f"{nodeid}|call" for nodeid in selected],
            "2": [f"{nodeid}|call" for nodeid in selected],
            "3": [f"{nodeid}|call" for nodeid in selected],
        }
        return (
            {
                "schema": deep.DEEP_COVERAGE_SHARD_SCHEMA,
                "status": "ready",
                "mode": "shard",
                "request_signature": signature,
                "tool_versions": dict(_VERSIONS),
                "nodeids": list(selected),
                "tests": tests,
                "failures": failures,
                "files": [
                    {
                        "relative_path": "_04_Nucleo_Operativo/logic.py",
                        "module": "_04_Nucleo_Operativo.logic",
                        "statements": [1, 2, 3, 4],
                        "executed_lines": [1, 2, 3],
                        "missing_lines": [4],
                        "excluded_lines": [],
                        "executed_branches": [[2, 3]],
                        "missing_branches": [[2, 4]],
                        "contexts": contexts,
                    }
                ],
                "analysis_contract": {
                    "main_process_only": True,
                    "subprocess_coverage": False,
                },
            },
            101,
            3,
        )

    return calls, run


def _execute(
    trusted: Path,
    stage: Path,
    scratch: Path,
    staged: dict[str, ExternalEvidenceFile],
    config: deep.DeepCoverageConfig,
) -> deep.DeepCoverageExecution:
    return deep.execute_pytest_coverage(
        stage,
        staged,
        {},
        trusted_root=trusted,
        scratch_root=scratch,
        config=config,
    )


def test_normalizes_canonical_metrics_context_relations_and_missing_ranges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted, stage, scratch, staged = _fixture(tmp_path, monkeypatch)
    nodeids = (
        "tests/test_logic.py::test_again",
        "tests/test_logic.py::test_false",
        "tests/test_logic.py::test_true",
    )
    calls, run = _worker(nodeids)
    monkeypatch.setattr(deep, "_run_worker", run)

    result = _execute(trusted, stage, scratch, staged, _config())

    assert result.measurement_complete is True
    assert result.suite_selection == "full"
    assert result.process_invocations == 4  # git, collect, two shards
    assert result.counters["tests_passed"] == 3
    assert result.counters["support_files_verified"] == 4
    assert [item[0] for item in calls] == ["collect", "shard", "shard"]
    canonical = {
        "executable_lines",
        "covered_lines",
        "missing_lines",
        "line_coverage_percent",
        "branch_exits",
        "covered_branch_exits",
        "missing_branch_exits",
        "branch_coverage_percent",
    }
    observed = {
        metric.metric_name
        for metric in result.metrics
        if metric.subject_kind in {"file", "module", "symbol"}
    }
    assert observed == canonical
    expected_coverage = {
        "executable_lines": 4.0,
        "covered_lines": 3.0,
        "missing_lines": 1.0,
        "line_coverage_percent": 75.0,
        "branch_exits": 2.0,
        "covered_branch_exits": 1.0,
        "missing_branch_exits": 1.0,
        "branch_coverage_percent": 50.0,
    }
    for subject_kind in ("file", "module", "symbol"):
        assert {
            metric.metric_name: metric.value
            for metric in result.metrics
            if metric.subject_kind == subject_kind
        } == expected_coverage
    run_metrics = {
        metric.metric_name: metric.value
        for metric in result.metrics
        if metric.subject_kind == "run"
    }
    assert run_metrics == {
        **expected_coverage,
        "tests_collected": 3.0,
        "tests_selected": 3.0,
        "tests_passed": 3.0,
        "tests_failed": 0.0,
        "tests_skipped": 0.0,
        "shards_total": 2.0,
        "shards_reused": 0.0,
    }
    assert len(result.metrics) == 39
    file_metric = next(
        item
        for item in result.metrics
        if item.subject_kind == "file" and item.metric_name == "missing_lines"
    )
    assert file_metric.subject_key == file_metric.metadata["relative_path"]
    assert file_metric.metadata["missing_line_ranges"] == [[4, 4]]
    assert file_metric.metadata["missing_branch_arcs"] == [[2, 4]]
    symbol_metric = next(
        item
        for item in result.metrics
        if item.subject_kind == "symbol" and item.metric_name == "covered_lines"
    )
    assert symbol_metric.subject_key == (
        f"{symbol_metric.metadata['module_key']}:{symbol_metric.metadata['qualified_name']}:1:4"
    )
    assert len(result.relations) == 3
    relation = result.relations[0]
    assert relation.relation_kind == "test_covers_symbol"
    assert relation.source_key.startswith("pytest-nodeid:tests/test_logic.py::")
    assert relation.metadata["qualified_name"] == "_04_Nucleo_Operativo.logic.choose"
    assert relation.metadata["start_line"] == 1
    assert relation.metadata["end_line"] == 4
    for relation in result.relations:
        test_nodeids = relation.metadata["test_nodeids"]
        assert isinstance(test_nodeids, list)
        assert len(test_nodeids) == 1
        nodeid = test_nodeids[0]
        assert isinstance(nodeid, str)
        assert relation.metadata["lines"] == [1, 2, 3]
        assert relation.metadata["contexts"] == [f"{nodeid}|call"]
    assert result.findings == ()


def test_reuses_only_validated_passing_shards_and_reruns_malformed_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted, stage, scratch, staged = _fixture(tmp_path, monkeypatch)
    nodeids = (
        "tests/test_logic.py::test_again",
        "tests/test_logic.py::test_false",
        "tests/test_logic.py::test_true",
    )
    calls, run = _worker(nodeids)
    monkeypatch.setattr(deep, "_run_worker", run)
    config = _config()

    first = _execute(trusted, stage, scratch, staged, config)
    second = _execute(trusted, stage, scratch, staged, config)

    assert first.counters["shards_reused"] == 0
    assert second.counters["shards_reused"] == 2
    assert second.process_invocations == 2  # git and collect only
    checkpoints = sorted((scratch / "checkpoints").glob("*.json"))
    assert len(checkpoints) == 2
    checkpoints[0].write_text("{}", encoding="utf-8")

    third = _execute(trusted, stage, scratch, staged, config)

    assert third.counters["shards_reused"] == 1
    assert third.process_invocations == 3
    assert [item[0] for item in calls].count("shard") == 3


def test_interrupted_shards_publish_no_partial_result_and_resume_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted, stage, scratch, staged = _fixture(tmp_path, monkeypatch)
    nodeids = (
        "tests/test_logic.py::test_false",
        "tests/test_logic.py::test_true",
    )
    calls, stable_worker = _worker(nodeids)
    interrupted_modes: list[str] = []

    def interrupted_worker(request, *, scratch_root, environment, timeout_seconds):
        mode = str(request["mode"])
        interrupted_modes.append(mode)
        if mode == "shard" and tuple(request["nodeids"]) == (nodeids[1],):
            raise subprocess.TimeoutExpired(("pytest",), timeout_seconds)
        return stable_worker(
            request,
            scratch_root=scratch_root,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )

    monkeypatch.setattr(deep, "_run_worker", interrupted_worker)
    config = _config(shard_size=1)

    with pytest.raises(subprocess.TimeoutExpired):
        _execute(trusted, stage, scratch, staged, config)

    assert interrupted_modes == ["collect", "shard", "shard"]
    assert len(tuple((scratch / "checkpoints").glob("*.json"))) == 1

    monkeypatch.setattr(deep, "_run_worker", stable_worker)
    resumed = _execute(trusted, stage, scratch, staged, config)

    assert resumed.measurement_complete is True
    assert resumed.counters["tests_passed"] == 2
    assert resumed.counters["shards_reused"] == 1
    assert resumed.process_invocations == 3  # git, collect and the unfinished shard
    assert [item[0] for item in calls] == ["collect", "shard", "collect", "shard"]


def test_failed_shard_is_advisory_and_never_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted, stage, scratch, staged = _fixture(tmp_path, monkeypatch)
    nodeids = ("tests/test_logic.py::test_true",)
    calls, run = _worker(nodeids, failed=frozenset(nodeids))
    monkeypatch.setattr(deep, "_run_worker", run)

    first = _execute(trusted, stage, scratch, staged, _config(shard_size=1))
    second = _execute(trusted, stage, scratch, staged, _config(shard_size=1))

    assert first.findings[0].category == "test_failure"
    assert first.findings[0].mutation_authority is False
    assert second.counters["shards_reused"] == 0
    assert [item[0] for item in calls].count("shard") == 2
    assert not tuple((scratch / "checkpoints").glob("*.json"))


def test_support_change_invalidates_checkpoint_but_preserves_comparability_signatures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted, stage, scratch, staged = _fixture(tmp_path, monkeypatch)
    nodeids = ("tests/test_logic.py::test_true",)
    calls, run = _worker(nodeids)
    monkeypatch.setattr(deep, "_run_worker", run)
    config = _config(shard_size=1)

    first = _execute(trusted, stage, scratch, staged, config)
    (trusted / "tests" / "cases.json").write_text('{"case":2}\n', encoding="utf-8")
    second = _execute(trusted, stage, scratch, staged, config)

    assert first.suite_signature == second.suite_signature
    assert first.measurement_scope_signature == second.measurement_scope_signature
    first_run = next(metric for metric in first.metrics if metric.subject_kind == "run")
    second_run = next(metric for metric in second.metrics if metric.subject_kind == "run")
    assert first_run.metadata["support_signature"] != second_run.metadata["support_signature"]
    assert (
        first_run.metadata["publication_input_signature"]
        != second_run.metadata["publication_input_signature"]
    )
    assert second.counters["shards_reused"] == 0
    assert [item[0] for item in calls].count("shard") == 2


def test_codex_control_changes_do_not_invalidate_test_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted, stage, scratch, staged = _fixture(tmp_path, monkeypatch)
    codex = trusted / ".codex"
    codex.mkdir()
    config_path = codex / "config.toml"
    config_path.write_text("model = 'one'\n", encoding="utf-8")
    nodeids = ("tests/test_logic.py::test_true",)
    _calls, run = _worker(nodeids)
    monkeypatch.setattr(deep, "_run_worker", run)
    config = _config(shard_size=1)

    first = _execute(trusted, stage, scratch, staged, config)
    config_path.write_text("model = 'two'\n", encoding="utf-8")
    second = _execute(trusted, stage, scratch, staged, config)

    first_run = next(metric for metric in first.metrics if metric.subject_kind == "run")
    second_run = next(metric for metric in second.metrics if metric.subject_kind == "run")
    assert first_run.metadata["support_signature"] == second_run.metadata["support_signature"]
    assert (
        first_run.metadata["publication_input_signature"]
        == second_run.metadata["publication_input_signature"]
    )
    assert second.counters["shards_reused"] == 1
    assert "codex_control_files_excluded_from_support_signature" in second.limitations


def test_truncation_is_honest_and_selected_scope_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted, stage, scratch, staged = _fixture(tmp_path, monkeypatch)
    nodeids = (
        "tests/test_logic.py::test_again",
        "tests/test_logic.py::test_false",
        "tests/test_logic.py::test_true",
    )
    _calls, run = _worker(nodeids)
    monkeypatch.setattr(deep, "_run_worker", run)

    result = _execute(trusted, stage, scratch, staged, _config(max_tests=2))

    assert result.suite_selection == "full"
    assert result.measurement_complete is False
    assert result.counters["tests_collected"] == 3
    assert result.counters["tests_selected"] == 2
    assert "suite_truncated_by_max_tests" in result.limitations


def test_trusted_root_and_public_bounds_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted, stage, scratch, staged = _fixture(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()

    with pytest.raises(ValueError, match="canonical Neocortex"):
        _execute(other, stage, scratch, staged, _config())
    with pytest.raises(ValueError, match="shard_size"):
        _config(max_tests=100, shard_size=51)
    assert _config(max_tests=100, shard_size=50).shard_size == 50
    with pytest.raises(ValueError, match="max_tests"):
        _config(max_tests=5_001, shard_size=50)
    with pytest.raises(ValueError, match=r"0\.\.900"):
        _config(budget=901.0)
    with pytest.raises(ValueError, match="deterministically sorted"):
        _config(selectors=("tests/test_z.py", "tests/test_a.py"))

    assert deep._validate_arc([12, -5], label="coverage branch") == (12, -5)
    with pytest.raises(ValueError, match="source is invalid"):
        deep._validate_arc([0, 5], label="coverage branch")


def test_prepared_input_reports_real_support_cost_and_is_reusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted, stage, scratch, staged = _fixture(tmp_path, monkeypatch)
    config = _config(shard_size=1)
    prepared = deep.prepare_deep_coverage_input(
        trusted,
        tuple(staged.values()),
        config,
    )
    nodeids = ("tests/test_logic.py::test_true",)
    _calls, run = _worker(nodeids)
    monkeypatch.setattr(deep, "_run_worker", run)

    result = deep.execute_pytest_coverage(
        stage,
        staged,
        {},
        trusted_root=trusted,
        scratch_root=scratch,
        config=config,
        prepared_input=prepared,
    )

    assert prepared.support_files_verified == 4
    assert prepared.support_bytes_verified > 0
    assert prepared.process_invocations == 1
    assert deep.deep_coverage_input_signature(
        trusted,
        tuple(staged.values()),
        config,
    ).startswith("deep-coverage-publication-input-v1:")
    assert result.counters["support_bytes_verified"] == prepared.support_bytes_verified
