"""Synthetic subprocess contracts for the trusted deep Coverage.py worker."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from typing import Any

import coverage
import pytest
import xxhash

import _04_Nucleo_Operativo.external_deep_coverage_worker as worker
import _04_Nucleo_Operativo.external_deep_coverage as deep
from _04_Nucleo_Operativo.code_external_evidence import ExternalEvidenceFile
from _04_Nucleo_Operativo.semantic_models import fingerprint_bytes


def test_bounded_diagnostic_preserves_the_root_cause_tail(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    diagnostic = f"{project}:" + ("setup context\n" * 100) + f"{scratch}: filename too long"

    bounded = worker._bounded_diagnostic(diagnostic, project, scratch, 256)

    assert len(bounded) == 256
    assert bounded.startswith("$PROJECT:")
    assert "...[truncated]..." in bounded
    assert bounded.endswith("$SCRATCH: filename too long")


def test_bounded_diagnostic_enforces_its_utf8_byte_limit(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"

    bounded = worker._bounded_diagnostic("causa → " * 1000, project, scratch, 256)

    assert len(bounded.encode("utf-8")) <= 256
    assert "...[truncated]..." in bounded


@pytest.mark.parametrize(
    ("nodeid", "expected"),
    [
        ("tests/test_logic.py", "tests/test_logic.py"),
        (r"tests\test_logic.py", "tests/test_logic.py"),
        (r"tests\nested\test_logic.py", "tests/nested/test_logic.py"),
        ("tests/test_logic.py::test_plain", "tests/test_logic.py::test_plain"),
        (r"tests\test_logic.py::test_plain", "tests/test_logic.py::test_plain"),
        (
            r"tests\test_logic.py::TestLogic::test_method",
            "tests/test_logic.py::TestLogic::test_method",
        ),
        (
            r"tests\test_logic.py::test_value[line\nbreak]",
            r"tests/test_logic.py::test_value[line\nbreak]",
        ),
        (
            r"tests\test_logic.py::test_value[tab\tbreak]",
            r"tests/test_logic.py::test_value[tab\tbreak]",
        ),
        (
            r"tests\test_logic.py::test_value[return\rbreak]",
            r"tests/test_logic.py::test_value[return\rbreak]",
        ),
        (
            r"tests\test_logic.py::test_value[form\fbreak]",
            r"tests/test_logic.py::test_value[form\fbreak]",
        ),
        (
            r"tests\test_logic.py::test_value[vertical\vbreak]",
            r"tests/test_logic.py::test_value[vertical\vbreak]",
        ),
        (
            r"tests\test_logic.py::test_value[null\0byte]",
            r"tests/test_logic.py::test_value[null\0byte]",
        ),
        (
            r"tests\test_logic.py::test_value[hex\x5cvalue]",
            r"tests/test_logic.py::test_value[hex\x5cvalue]",
        ),
        (
            r"tests\test_logic.py::test_value[unicode\u005cvalue]",
            r"tests/test_logic.py::test_value[unicode\u005cvalue]",
        ),
        (
            r"tests\test_logic.py::test_value[wide\U0000005cvalue]",
            r"tests/test_logic.py::test_value[wide\U0000005cvalue]",
        ),
        (
            r"tests\test_logic.py::test_value[double\\slash]",
            r"tests/test_logic.py::test_value[double\\slash]",
        ),
        (
            r"tests\test_logic.py::test_value[path\looking\id]",
            r"tests/test_logic.py::test_value[path\looking\id]",
        ),
        (
            r"tests\test_logic.py::test_value[quoted\'id]",
            r"tests/test_logic.py::test_value[quoted\'id]",
        ),
        (
            r"tests\test_logic.py::test_value[quoted\"id]",
            r"tests/test_logic.py::test_value[quoted\"id]",
        ),
        (
            r"tests\test_logic.py::test_value[bracket\[id\]]",
            r"tests/test_logic.py::test_value[bracket\[id\]]",
        ),
        (
            r"tests\test_logic.py::test_value[regex\d+\s]",
            r"tests/test_logic.py::test_value[regex\d+\s]",
        ),
        (
            r"tests\test_logic.py::test_value[namespace::member]",
            r"tests/test_logic.py::test_value[namespace::member]",
        ),
        (
            r"tests\test_logic.py::TestLogic::test_value[line\nbreak]",
            r"tests/test_logic.py::TestLogic::test_value[line\nbreak]",
        ),
        (
            "tests\\test_logic.py::test_value[actual\nnewline]",
            "tests/test_logic.py::test_value[actual\nnewline]",
        ),
    ],
)
def test_portable_nodeid_normalizes_only_the_path(nodeid: str, expected: str) -> None:
    assert worker._portable_nodeid(nodeid) == expected


def test_test_evidence_uses_the_canonical_casefold_nodeid_order(tmp_path: Path) -> None:
    nodeids = (
        "tests/test_logic.py::test_case[KeyboardInterrupt]",
        "tests/test_logic.py::test_case[RuntimeError]",
        "tests/test_logic.py::test_case[_FatalRequestConstruction]",
    )
    reports = [
        SimpleNamespace(nodeid=nodeid, when="call", outcome="passed", failed=False)
        for nodeid in reversed(nodeids)
    ]

    tests, failures = worker._test_evidence(
        reports,
        project_root=tmp_path,
        scratch_root=tmp_path / "scratch",
        limits=worker.WorkerLimits(
            max_tests=20,
            time_budget_seconds=30,
            shard_size=20,
            max_output_bytes=2 * 1024 * 1024,
            max_failures=20,
            max_contexts=10_000,
        ),
    )

    assert failures == []
    assert [item["nodeid"] for item in tests] == [
        "tests/test_logic.py::test_case[_FatalRequestConstruction]",
        "tests/test_logic.py::test_case[KeyboardInterrupt]",
        "tests/test_logic.py::test_case[RuntimeError]",
    ]


def _project(root: Path) -> tuple[Path, Path]:
    project = root / "project"
    tests = project / "tests"
    package = project / "demo"
    tests.mkdir(parents=True)
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "logic.py").write_text(
        """def choose(value):
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "zero"
""",
        encoding="utf-8",
    )
    (tests / "test_logic.py").write_text(
        """import pytest

from demo.logic import choose


@pytest.mark.parametrize("value", ["line\\nbreak"])
def test_escaped_parameter(value):
    assert value == "line\\nbreak"


def test_positive(tmp_path):
    assert tmp_path.is_dir()
    assert choose(1) == "positive"


def test_zero_failure():
    assert choose(0) == "positive"
""",
        encoding="utf-8",
    )
    return project, tests


def _limits(**overrides: int) -> dict[str, int]:
    values = {
        "max_contexts": 10_000,
        "max_failures": 20,
        "max_output_bytes": 2 * 1024 * 1024,
        "max_tests": 20,
        "shard_size": 20,
        "time_budget_seconds": 30,
    }
    values.update(overrides)
    return values


def _manifest_item(project: Path, relative_path: str, module: str) -> dict[str, object]:
    raw = (project / relative_path).read_bytes()
    return {
        "content_digest": (
            f"xxh3_128:{xxhash.xxh3_128_hexdigest(raw)}:"
            f"xxh3_64:{xxhash.xxh3_64_hexdigest(raw, seed=worker._FINGERPRINT_GUARD_SEED)}"
        ),
        "module": module,
        "production": True,
        "relative_path": relative_path,
        "size": len(raw),
    }


def _request(
    *,
    mode: str,
    project: Path,
    scratch: Path,
    **fields: object,
) -> dict[str, object]:
    scratch.mkdir(exist_ok=True)
    request: dict[str, object] = {
        "configuration_signature": "fixture-configuration-v1",
        "input_signature": "fixture-input-v1",
        "limits": _limits(),
        "mode": mode,
        "project_root": os.fspath(project),
        "schema": worker.REQUEST_SCHEMA,
        "scratch_root": os.fspath(scratch),
        "support_signature": "fixture-support-v1",
        "tool_versions": {
            "coverage": coverage.__version__,
            "pytest": pytest.__version__,
            "python": sys.version.split()[0],
        },
    }
    if mode == "collect":
        request.update({"nodeids": [], "selectors": []})
    else:
        request.update(
            {
                "measurement_scope_signature": "fixture-scope-v1",
                "selectors": [],
                "shard_index": 0,
                "shard_signature": "fixture-shard-v1",
                "suite_signature": "fixture-suite-v1",
            }
        )
    request.update(fields)
    return request


def _run(request: dict[str, object], path: Path) -> subprocess.CompletedProcess[str]:
    unsigned = {key: value for key, value in request.items() if key != "request_signature"}
    encoded = json.dumps(
        unsigned,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    request["request_signature"] = "deep-coverage-request-v1:xxh3_128:" + xxhash.xxh3_128_hexdigest(
        encoded
    )
    path.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
    assert worker.__file__ is not None
    return subprocess.run(
        [sys.executable, "-I", worker.__file__, "--request", os.fspath(path.resolve())],
        cwd=path.parent,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


def _payload(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert completed.stdout.count("\n") == 1, completed.stdout
    value = json.loads(completed.stdout)
    assert isinstance(value, dict)
    return value


def test_collect_returns_sorted_deterministic_nodeids(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)
    first_request = _request(
        mode="collect",
        project=project,
        scratch=tmp_path / "scratch-first",
        source_manifest=[_manifest_item(project, "demo/logic.py", "demo.logic")],
    )
    second_request = _request(
        mode="collect",
        project=project,
        scratch=tmp_path / "scratch-first",
        source_manifest=[_manifest_item(project, "demo/logic.py", "demo.logic")],
    )

    first = _run(first_request, tmp_path / "collect-first.json")
    second = _run(second_request, tmp_path / "collect-second.json")

    assert first.returncode == second.returncode == 0, first.stderr or first.stdout
    assert first.stdout == second.stdout
    payload = _payload(first)
    assert payload["schema"] == worker.COLLECT_SCHEMA
    assert payload["nodeids"] == [
        "tests/test_logic.py::test_escaped_parameter[line\\nbreak]",
        "tests/test_logic.py::test_positive",
        "tests/test_logic.py::test_zero_failure",
    ]
    assert payload["tool_versions"]["coverage"].startswith("7.14.")
    assert payload["tool_versions"]["pytest"].startswith("9.")
    symbol = next(
        item for item in payload["symbols"] if item["qualified_name"] == "demo.logic.choose"
    )
    assert symbol == {
        "end_line": 6,
        "kind": "function",
        "module": "demo.logic",
        "qualified_name": "demo.logic.choose",
        "relative_path": "demo/logic.py",
        "start_line": 1,
    }
    assert payload["analysis_contract"] == {
        "branch": True,
        "coverage_config_file": False,
        "executes_project_content": True,
        "executes_tests": False,
        "loads_project_conftest": True,
        "main_process_only": True,
        "pytest_programmatic": True,
        "subprocess_coverage": False,
        "uses_network": False,
    }
    worker_runs = tmp_path / "scratch-first" / "w"
    assert worker_runs.is_dir()
    assert not tuple(worker_runs.iterdir())


def test_shard_maps_outcomes_contexts_and_branches(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)
    request = _request(
        mode="shard",
        project=project,
        scratch=tmp_path / "scratch",
        nodeids=[
            "tests/test_logic.py::test_positive",
            "tests/test_logic.py::test_zero_failure",
        ],
        source_manifest=[_manifest_item(project, "demo/logic.py", "demo.logic")],
    )
    replay_request = _request(
        mode="shard",
        project=project,
        scratch=tmp_path / "scratch",
        nodeids=[
            "tests/test_logic.py::test_positive",
            "tests/test_logic.py::test_zero_failure",
        ],
        source_manifest=[_manifest_item(project, "demo/logic.py", "demo.logic")],
    )

    completed = _run(request, tmp_path / "shard.json")
    replay = _run(replay_request, tmp_path / "shard-replay.json")

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert replay.returncode == 0, replay.stderr or replay.stdout
    assert completed.stdout == replay.stdout
    payload = _payload(completed)
    assert payload["schema"] == worker.SHARD_SCHEMA
    assert payload["suite_status"] == "failed"
    assert [(item["nodeid"], item["outcome"]) for item in payload["tests"]] == [
        ("tests/test_logic.py::test_positive", "passed"),
        ("tests/test_logic.py::test_zero_failure", "failed"),
    ]
    assert payload["failures"][0]["nodeid"] == "tests/test_logic.py::test_zero_failure"
    assert str(tmp_path) not in payload["failures"][0]["message"]
    assert payload["nodeids"] == [
        "tests/test_logic.py::test_positive",
        "tests/test_logic.py::test_zero_failure",
    ]
    coverage_file = payload["files"][0]
    assert coverage_file["relative_path"] == "demo/logic.py"
    assert coverage_file["module"] == "demo.logic"
    assert coverage_file["statements"] == sorted(coverage_file["statements"])
    assert coverage_file["executed_lines"]
    assert coverage_file["missing_lines"]
    assert coverage_file["executed_branches"]
    assert coverage_file["missing_branches"]
    assert any(
        "tests/test_logic.py::test_positive|call" in contexts
        for contexts in coverage_file["contexts"].values()
    )
    assert payload["analysis_contract"]["main_process_only"] is True
    assert payload["analysis_contract"]["subprocess_coverage"] is False
    assert "symbols" not in payload
    assert not (project / ".coverage").exists()
    assert not (project / ".pytest_cache").exists()
    assert not any(project.rglob("__pycache__"))
    worker_runs = tmp_path / "scratch" / "w"
    assert worker_runs.is_dir()
    assert not tuple(worker_runs.iterdir())


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        (
            lambda request: request.update(schema="neocortex.external-deep-coverage-request/v2"),
            "unsupported_schema",
        ),
        (
            lambda request: request["limits"].update(max_tests=worker.HARD_MAX_TESTS + 1),
            "invalid_limit",
        ),
    ],
)
def test_worker_rejects_incompatible_schema_and_bounds(
    tmp_path: Path,
    mutation: Any,
    error_code: str,
) -> None:
    project, _ = _project(tmp_path)
    request = _request(
        mode="collect",
        project=project,
        scratch=tmp_path / "scratch",
        source_manifest=[_manifest_item(project, "demo/logic.py", "demo.logic")],
    )
    mutation(request)

    completed = _run(request, tmp_path / "request.json")

    assert completed.returncode == 2
    assert _payload(completed)["error"]["code"] == error_code


def test_worker_rejects_source_escape_before_execution(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    raw = outside.read_bytes()
    request = _request(
        mode="shard",
        project=project,
        scratch=tmp_path / "scratch",
        nodeids=["tests/test_logic.py::test_positive"],
        source_manifest=[
            {
                "content_digest": (
                    f"xxh3_128:{xxhash.xxh3_128_hexdigest(raw)}:"
                    f"xxh3_64:{xxhash.xxh3_64_hexdigest(raw, seed=worker._FINGERPRINT_GUARD_SEED)}"
                ),
                "module": "outside",
                "production": False,
                "relative_path": "../outside.py",
                "size": len(raw),
            }
        ],
    )

    completed = _run(request, tmp_path / "request.json")

    assert completed.returncode == 2
    assert _payload(completed)["error"]["code"] == "unsafe_path"
    assert not (tmp_path / "scratch" / "b").exists()


def test_shard_rejects_more_than_fifty_exact_nodeids(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)
    request = _request(
        mode="shard",
        project=project,
        scratch=tmp_path / "scratch",
        nodeids=[f"tests/test_logic.py::test_positive[case-{index}]" for index in range(51)],
        source_manifest=[_manifest_item(project, "demo/logic.py", "demo.logic")],
    )
    request["limits"] = _limits(max_tests=5_000, shard_size=50)

    completed = _run(request, tmp_path / "request.json")

    assert completed.returncode == 2
    assert _payload(completed)["error"]["code"] == "test_bound_exceeded"
    assert not (tmp_path / "scratch" / "b").exists()


def test_real_adapter_runs_collect_shards_and_checkpoint_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "trusted"
    package = project / "_04_Nucleo_Operativo"
    tests = project / "tests"
    package.mkdir(parents=True)
    tests.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "logic.py").write_text(
        "def choose(value):\n"
        "    if value:\n"
        "        return 1\n"
        "    return 2\n\n"
        "def uncovered_exit(value):\n"
        "    if value:\n"
        "        return 3\n",
        encoding="utf-8",
    )
    (tests / "test_logic.py").write_text(
        "import sqlite3\n"
        "import shutil\n"
        "import subprocess\n\n"
        "from _04_Nucleo_Operativo.logic import choose\n\n"
        "def test_true(tmp_path):\n"
        "    assert choose(True) == 1\n"
        "    git = shutil.which('git')\n"
        "    assert git is not None\n"
        "    completed = subprocess.run(\n"
        "        [git, 'init', '--quiet', str(tmp_path / 'r')],\n"
        "        capture_output=True, text=True, timeout=20,\n"
        "    )\n"
        "    assert completed.returncode == 0, completed.stderr\n\n"
        "def test_false():\n"
        "    assert choose(False) == 2\n\n"
        "def test_coverage_sqlite_is_isolated(monkeypatch):\n"
        "    class Connection:\n"
        "        pass\n\n"
        "    monkeypatch.setattr(sqlite3, 'connect', lambda *_a, **_kw: Connection())\n"
        "    assert isinstance(sqlite3.connect('fixture'), Connection)\n",
        encoding="utf-8",
    )
    (tests / "conftest.py").write_text(
        "import os\n"
        "import shutil\n"
        "from pathlib import Path\n\n"
        "def pytest_configure(config):\n"
        "    del config\n"
        "    root = Path(os.environ['NEOCORTEX_AUDIT_LAB_ROOT']).resolve()\n"
        "    for name in ('TEMP', 'TMP', 'TMPDIR', 'PYTHONPYCACHEPREFIX'):\n"
        "        assert Path(os.environ[name]).resolve().is_relative_to(root)\n"
        "    if os.name == 'nt':\n"
        "        for name in ('SYSTEMROOT', 'WINDIR'):\n"
        "            assert Path(os.environ[name]).is_dir()\n"
        "    assert os.environ['GIT_CONFIG_COUNT'] == '1'\n"
        "    assert os.environ['GIT_CONFIG_KEY_0'] == 'core.longpaths'\n"
        "    assert os.environ['GIT_CONFIG_VALUE_0'] == 'true'\n"
        "    assert Path.home().is_dir()\n"
        "    assert shutil.which('git') is not None\n",
        encoding="utf-8",
    )
    (project / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    git = shutil.which("git")
    assert git is not None
    subprocess.run(
        [git, "init", "--quiet", os.fspath(project)],
        check=True,
        capture_output=True,
        timeout=20,
    )
    owners: dict[str, ExternalEvidenceFile] = {}
    for version_id, relative_path in enumerate(
        (
            "_04_Nucleo_Operativo/__init__.py",
            "_04_Nucleo_Operativo/logic.py",
            "tests/conftest.py",
            "tests/test_logic.py",
        ),
        start=1,
    ):
        path = project / relative_path
        metadata = path.stat()
        digest = fingerprint_bytes(path.read_bytes())
        owner = ExternalEvidenceFile(
            version_id,
            os.fspath(path),
            relative_path,
            metadata.st_size,
            metadata.st_mtime_ns,
            digest.xxh3_128,
            digest.xxh3_64_guard,
        )
        owners[os.path.normcase(os.path.abspath(path))] = owner
    stage = tmp_path / "g"
    scratch = tmp_path / "s"
    stage.mkdir()
    scratch.mkdir()
    monkeypatch.setattr(deep, "_canonical_repository_root", lambda: project)
    config = deep.DeepCoverageConfig((), 3, 60.0, 1, "real-worker-fixture-v1")

    first = deep.execute_pytest_coverage(
        stage,
        owners,
        {},
        trusted_root=project,
        scratch_root=scratch,
        config=config,
    )
    replay = deep.execute_pytest_coverage(
        stage,
        owners,
        {},
        trusted_root=project,
        scratch_root=scratch,
        config=config,
    )

    assert first.measurement_complete is True
    assert first.counters["tests_passed"] == 3, tuple(finding.message for finding in first.findings)
    assert first.counters["shards_reused"] == 0
    assert any(
        endpoint < 0
        for metric in first.metrics
        for arc in cast(list[list[int]], metric.metadata.get("missing_branch_arcs", []))
        for endpoint in arc
    )
    assert any(
        metric.subject_kind == "symbol"
        and cast(str, metric.metadata["qualified_name"]).endswith(".choose")
        for metric in first.metrics
    )
    assert replay.counters["shards_reused"] == 3
    assert replay.process_invocations == 2
    worker_runs = scratch / "r" / "w"
    assert worker_runs.is_dir()
    assert not tuple(worker_runs.iterdir())
