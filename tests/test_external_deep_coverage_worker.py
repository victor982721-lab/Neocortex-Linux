"""Synthetic subprocess contracts for the trusted deep Coverage.py worker."""

from __future__ import annotations

import json
import io
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

import neocortex.code.external_deep_coverage_worker as worker
import neocortex.code.external_deep_coverage as deep
import neocortex.code.external_evidence_providers as providers
from neocortex.code.code_external_evidence import ExternalEvidenceFile
from neocortex.semantic.semantic_models import fingerprint_bytes


def test_worker_cleanup_failure_does_not_replace_the_primary_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = worker.WorkerContractError("primary_failure", "primary failure")

    def fail_cleanup() -> None:
        raise worker.WorkerContractError("cleanup_failure", "cleanup failure")

    monkeypatch.setattr(worker, "_cleanup_runtime_roots", fail_cleanup)

    observed = worker._cleanup_error_or(primary)

    assert observed is primary
    assert observed.code == "primary_failure"
    assert any("cleanup_failure" in note for note in getattr(observed, "__notes__", ()))


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


def _source_file(path: Path, relative_path: str, module: str) -> worker.SourceFile:
    raw = path.read_bytes()
    return worker.SourceFile(
        path=path,
        relative_path=relative_path,
        module=module,
        size=len(raw),
        content_digest=worker._content_digest(raw),
        production=True,
        raw=raw,
    )


def _worker_limits(**overrides: int | float) -> worker.WorkerLimits:
    values: dict[str, int | float] = _limits()
    values.update(overrides)
    return worker.WorkerLimits(
        max_tests=cast(int, values["max_tests"]),
        time_budget_seconds=float(values["time_budget_seconds"]),
        shard_size=cast(int, values["shard_size"]),
        max_output_bytes=cast(int, values["max_output_bytes"]),
        max_failures=cast(int, values["max_failures"]),
        max_contexts=cast(int, values["max_contexts"]),
    )


def test_in_process_scalar_and_limit_validation_is_fail_closed() -> None:
    assert worker._required_string({"value": "ok"}, "value", maximum=2) == "ok"
    for value in (None, "", "long", "bad\nvalue"):
        with pytest.raises(worker.WorkerContractError) as caught:
            worker._required_string({"value": value}, "value", maximum=2)
        assert caught.value.code == "invalid_request"

    assert worker._required_int({"value": 3}, "value") == 3
    assert worker._required_number({"value": 3.5}, "value") == 3.5
    for function, value in ((worker._required_int, True), (worker._required_number, False)):
        with pytest.raises(worker.WorkerContractError, match="value") as caught:
            function({"value": value}, "value")
        assert caught.value.code == "invalid_request"

    worker._exact_keys({"only": 1}, frozenset({"only"}), label="fixture")
    with pytest.raises(worker.WorkerContractError, match="fields"):
        worker._exact_keys({"extra": 1}, frozenset({"only"}), label="fixture")

    limits = worker._validate_limits(_limits())
    assert limits.as_payload() == _limits()
    with pytest.raises(worker.WorkerContractError, match="limits must be an object"):
        worker._validate_limits([])
    with pytest.raises(worker.WorkerContractError, match="fields"):
        worker._validate_limits({**_limits(), "extra": 1})
    for overrides in (
        {"max_tests": 0},
        {"time_budget_seconds": 0},
        {"max_tests": 1, "shard_size": 2},
    ):
        with pytest.raises(worker.WorkerContractError) as caught:
            worker._validate_limits({**_limits(), **overrides})
        assert caught.value.code == "invalid_limit"


def test_in_process_root_and_relative_path_guards(tmp_path: Path) -> None:
    project, tests = _project(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    assert worker._inside(tests, project) is True
    assert worker._inside(tmp_path, project) is False
    worker._require_plain_tree_path(tests / "test_logic.py", project, label="fixture")
    with pytest.raises(worker.WorkerContractError, match="escapes"):
        worker._require_plain_tree_path(tmp_path, project, label="fixture")

    link = project / "linked-test.py"
    link.symlink_to(tests / "test_logic.py")
    with pytest.raises(worker.WorkerContractError, match="reparse"):
        worker._require_plain_tree_path(link, project, label="fixture")

    assert worker._absolute_directory(os.fspath(project), label="root") == project.resolve()
    for raw in (
        None,
        "relative",
        os.fspath(project / "missing"),
        os.fspath(tests / "test_logic.py"),
    ):
        with pytest.raises(worker.WorkerContractError) as caught:
            worker._absolute_directory(raw, label="root")
        assert caught.value.code == "invalid_root"
    linked_directory = tmp_path / "linked-directory"
    linked_directory.symlink_to(project, target_is_directory=True)
    with pytest.raises(worker.WorkerContractError, match="reparse"):
        worker._absolute_directory(os.fspath(linked_directory), label="root")

    assert worker._validate_roots(
        {"project_root": os.fspath(project), "scratch_root": os.fspath(scratch)}
    ) == (project.resolve(), tests.resolve(), scratch.resolve())
    bad_project = tmp_path / "bad-project"
    bad_project.mkdir()
    (bad_project / "tests").write_text("not a directory", encoding="utf-8")
    with pytest.raises(worker.WorkerContractError, match="tests root"):
        worker._validate_roots(
            {"project_root": os.fspath(bad_project), "scratch_root": os.fspath(scratch)}
        )
    inner_scratch = project / "scratch"
    inner_scratch.mkdir()
    with pytest.raises(worker.WorkerContractError, match="must not overlap"):
        worker._validate_roots(
            {"project_root": os.fspath(project), "scratch_root": os.fspath(inner_scratch)}
        )

    assert worker._relative_path("tests/test_logic.py", label="path").as_posix() == (
        "tests/test_logic.py"
    )
    for raw, code in (
        (None, "invalid_request"),
        ("bad\npath.py", "invalid_request"),
        (os.fspath(project / "demo" / "logic.py"), "unsafe_path"),
        ("tests/../demo/logic.py", "unsafe_path"),
    ):
        with pytest.raises(worker.WorkerContractError) as caught:
            worker._relative_path(raw, label="path")
        assert caught.value.code == code


def test_in_process_stable_bytes_detects_type_size_and_races(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "input.py"
    path.write_bytes(b"value = 1\n")
    assert worker._stable_bytes(path) == b"value = 1\n"
    assert worker._stable_bytes(path, expected_size=10) == b"value = 1\n"
    with pytest.raises(worker.WorkerContractError, match="size differs"):
        worker._stable_bytes(path, expected_size=1)
    with pytest.raises(worker.WorkerContractError, match="regular file"):
        worker._stable_bytes(tmp_path)

    link = tmp_path / "link.py"
    link.symlink_to(path)
    with pytest.raises(worker.WorkerContractError, match="regular file"):
        worker._stable_bytes(link)

    original_read_bytes = Path.read_bytes

    def mutate_after_read(selected: Path) -> bytes:
        raw = original_read_bytes(selected)
        selected.write_bytes(raw + b"# changed\n")
        return raw

    monkeypatch.setattr(Path, "read_bytes", mutate_after_read)
    with pytest.raises(worker.WorkerContractError, match="while it was being read"):
        worker._stable_bytes(path)


def test_in_process_selector_and_nodeid_validation_covers_adversarial_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, tests = _project(tmp_path)
    limits = _worker_limits()
    assert worker._validate_selectors([], project_root=project, test_root=tests, limits=limits) == (
        "tests",
    )
    assert worker._validate_selectors(
        ["tests/test_logic.py::test_positive"],
        project_root=project,
        test_root=tests,
        limits=limits,
    ) == ("tests/test_logic.py::test_positive",)

    monkeypatch.setattr(worker, "HARD_MAX_SELECTORS", 1)
    invalid_selectors: tuple[tuple[object, str], ...] = (
        (None, "invalid_request"),
        (["tests/test_logic.py", "tests/test_logic.py::test_positive"], "test_bound_exceeded"),
        (["-q"], "invalid_request"),
        (["tests/test_logic.py::"], "invalid_request"),
        (["tests/missing.py"], "missing_test_path"),
        (["demo/logic.py"], "unsafe_path"),
        (["tests::suite"], "invalid_request"),
    )
    for raw, code in invalid_selectors:
        with pytest.raises(worker.WorkerContractError) as caught:
            worker._validate_selectors(raw, project_root=project, test_root=tests, limits=limits)
        assert caught.value.code == code
    monkeypatch.setattr(worker, "HARD_MAX_SELECTORS", 2)
    for raw in (
        ["tests/test_logic.py", "tests/test_logic.py"],
        ["tests/test_logic.py::test_zero_failure", "tests/test_logic.py::test_positive"],
    ):
        with pytest.raises(worker.WorkerContractError, match="sorted and unique"):
            worker._validate_selectors(raw, project_root=project, test_root=tests, limits=limits)

    exact = "tests/test_logic.py::test_positive"
    assert worker._validate_nodeids(
        [exact], project_root=project, test_root=tests, limits=limits
    ) == (exact,)
    invalid_nodeids: tuple[tuple[object, str], ...] = (
        ([], "invalid_request"),
        ([exact, "tests/test_logic.py::test_zero_failure"], "test_bound_exceeded"),
        (["bad\nnode::test"], "invalid_request"),
        (["tests/test_logic.py"], "invalid_request"),
        (["demo/logic.py::test"], "unsafe_path"),
        (["tests::test"], "unsafe_path"),
        ([exact, exact], "test_bound_exceeded"),
    )
    one_test = _worker_limits(max_tests=1, shard_size=1)
    for raw, code in invalid_nodeids:
        selected_limits = one_test if isinstance(raw, list) and len(raw) > 1 else limits
        with pytest.raises(worker.WorkerContractError) as caught:
            worker._validate_nodeids(
                raw, project_root=project, test_root=tests, limits=selected_limits
            )
        assert caught.value.code == code
    with pytest.raises(worker.WorkerContractError, match="unique"):
        worker._validate_nodeids(
            [exact, exact], project_root=project, test_root=tests, limits=limits
        )


def test_in_process_source_manifest_validation_covers_integrity_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _ = _project(tmp_path)
    extra = project / "demo" / "extra.py"
    extra.write_text("VALUE = 2\n", encoding="utf-8")
    logic_item = _manifest_item(project, "demo/logic.py", "demo.logic")
    extra_item = _manifest_item(project, "demo/extra.py", "demo.extra")
    limits = _worker_limits()

    sources = worker._validate_source_manifest(
        [logic_item, extra_item], project_root=project, limits=limits
    )
    assert [source.relative_path for source in sources] == ["demo/extra.py", "demo/logic.py"]
    worker._validate_sources_unchanged(sources)
    worker._validate_sources_unchanged(())

    with pytest.raises(worker.WorkerContractError, match="non-empty array"):
        worker._validate_source_manifest([], project_root=project, limits=limits)
    monkeypatch.setattr(worker, "HARD_MAX_SOURCE_FILES", 1)
    with pytest.raises(worker.WorkerContractError, match="file bound"):
        worker._validate_source_manifest(
            [logic_item, extra_item], project_root=project, limits=limits
        )
    monkeypatch.setattr(worker, "HARD_MAX_SOURCE_FILES", 20_000)

    invalid_items: list[tuple[object, str]] = [
        ("not-an-object", "invalid_request"),
        ({**logic_item, "relative_path": "demo/logic.txt"}, "invalid_source"),
        ({**logic_item, "module": "not-a-module!"}, "invalid_source"),
        ({**logic_item, "size": -1}, "invalid_source"),
        ({**logic_item, "production": 1}, "invalid_source"),
        ({**logic_item, "content_digest": "xxh3_128:nope"}, "invalid_source"),
    ]
    for item, code in invalid_items:
        with pytest.raises(worker.WorkerContractError) as caught:
            worker._validate_source_manifest([item], project_root=project, limits=limits)
        assert caught.value.code == code

    with pytest.raises(worker.WorkerContractError, match="unique"):
        worker._validate_source_manifest(
            [logic_item, {**extra_item, "module": "demo.logic"}],
            project_root=project,
            limits=limits,
        )
    missing_item = {
        **logic_item,
        "relative_path": "demo/missing.py",
        "module": "demo.missing",
        "size": 0,
        "content_digest": worker._content_digest(b""),
    }
    with pytest.raises(worker.WorkerContractError, match="unavailable"):
        worker._validate_source_manifest([missing_item], project_root=project, limits=limits)

    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 3\n", encoding="utf-8")
    escaped = project / "escaped.py"
    escaped.symlink_to(outside)
    escaped_item = _manifest_item(project, "escaped.py", "escaped")
    with pytest.raises(worker.WorkerContractError, match="escapes"):
        worker._validate_source_manifest([escaped_item], project_root=project, limits=limits)

    with pytest.raises(worker.WorkerContractError, match="size differs"):
        worker._validate_source_manifest(
            [{**logic_item, "size": cast(int, logic_item["size"]) + 1}],
            project_root=project,
            limits=limits,
        )
    with pytest.raises(worker.WorkerContractError, match="digest differs"):
        worker._validate_source_manifest(
            [{**logic_item, "content_digest": worker._content_digest(b"different")}],
            project_root=project,
            limits=limits,
        )
    monkeypatch.setattr(worker, "HARD_MAX_SOURCE_BYTES", 1)
    with pytest.raises(worker.WorkerContractError, match="byte bound"):
        worker._validate_source_manifest([logic_item], project_root=project, limits=limits)

    extra.write_text("VALUE = 9\n", encoding="utf-8")
    with pytest.raises(worker.WorkerContractError, match="source changed"):
        worker._validate_sources_unchanged(sources)


def test_in_process_runtime_lifecycle_versions_and_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _ = _project(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    paths = worker._runtime_paths(scratch, "fixture-request")
    assert paths.scratch_root.parent == scratch / worker._WORKER_RUNS_DIRECTORY
    assert paths.temp.is_dir()

    monkeypatch.setenv("NO_COLOR", "original")
    monkeypatch.setenv("COVERAGE_PROCESS_START", "original-start")
    original_directory = Path.cwd()
    with worker._execution_environment(project, paths):
        assert Path.cwd() == project
        assert os.environ["TMPDIR"] == os.fspath(paths.temp)
        assert "COVERAGE_PROCESS_START" not in os.environ
        assert sys.path[0] == os.fspath(project)
    assert Path.cwd() == original_directory
    assert os.environ["NO_COLOR"] == "original"
    assert os.environ["COVERAGE_PROCESS_START"] == "original-start"

    with worker._execution_environment(project, paths):
        sys.path.remove(os.fspath(project))
    worker._cleanup_runtime_roots()
    assert not paths.scratch_root.exists()
    worker._cleanup_runtime_roots()

    unsafe = tmp_path / "unsafe-cleanup"
    unsafe.mkdir()
    worker._RUNTIME_ROOTS.append(unsafe)
    with pytest.raises(worker.WorkerContractError, match="target is unsafe"):
        worker._cleanup_runtime_roots()

    failed_root = scratch / "w" / "failed"
    failed_root.mkdir()
    worker._RUNTIME_ROOTS.append(failed_root)
    monkeypatch.setattr(worker.shutil, "rmtree", lambda _path: (_ for _ in ()).throw(OSError()))
    with pytest.raises(worker.WorkerContractError, match="could not be removed"):
        worker._cleanup_runtime_roots()

    assert worker._version_tuple("7.14.1.dev1") == (7, 14, 1)
    with pytest.raises(worker.WorkerContractError, match="malformed"):
        worker._version_tuple("version-seven")
    observed = {"coverage": "7.14.1", "pytest": "9.1.0", "python": "3.14.4"}
    worker._validate_tool_versions(observed, observed)
    for raw in ([], {**observed, "pytest": "8.0"}):
        with pytest.raises(worker.WorkerContractError) as caught:
            worker._validate_tool_versions(raw, observed)
        assert caught.value.code in {"invalid_request", "tool_version_incompatible"}

    monkeypatch.setattr(worker, "_version_tuple", lambda _value: (8, 0, 0))
    with pytest.raises(worker.WorkerContractError, match="requires Coverage"):
        worker._load_tools()


def test_in_process_collection_plugin_and_projection_guards(tmp_path: Path) -> None:
    project, tests = _project(tmp_path)
    limits = _worker_limits()
    assert worker._pytest_arguments(
        project,
        worker.RuntimePaths(
            tmp_path,
            tmp_path / "b",
            tmp_path / "c",
            tmp_path / "v",
            tmp_path / "v" / ".coverage",
            tmp_path / "v" / "coverage.json",
            tmp_path / "t",
            tmp_path / "p",
        ),
        ["tests"],
        collect_only=True,
    )[-2:] == ["--collect-only", "tests"]

    coverage_switches: list[str] = []
    plugin = worker._PytestEvidencePlugin(
        pytest,
        SimpleNamespace(switch_context=coverage_switches.append),
    )
    item = SimpleNamespace(nodeid=r"tests\test_logic.py::test_positive")
    plugin._switch(item, "call")
    assert coverage_switches == ["tests/test_logic.py::test_positive|call"]
    worker._PytestEvidencePlugin(pytest, None)._switch(item, "call")
    session = SimpleNamespace(
        items=[
            SimpleNamespace(
                nodeid="tests/test_logic.py::test_positive", path=tests / "test_logic.py"
            )
        ]
    )
    plugin.pytest_collection_finish(session)
    plugin.pytest_runtest_logreport(SimpleNamespace(outcome="passed"))
    assert plugin.collected[0][0].endswith("test_positive")
    assert len(plugin.reports) == 1

    collected = [("tests/test_logic.py::test_positive", tests / "test_logic.py")]
    assert worker._validate_collected(collected, test_root=tests, maximum=2) == (
        "tests/test_logic.py::test_positive",
    )
    invalid_collections: tuple[tuple[list[tuple[str, Path]], int, str], ...] = (
        ([], 2, "empty_selection"),
        (collected * 2, 1, "test_bound_exceeded"),
        ([("bad\nnode", tests / "test_logic.py")], 2, "invalid_collection"),
        ([("demo/logic.py::test", project / "demo" / "logic.py")], 2, "unsafe_collection"),
        (collected * 2, 2, "invalid_collection"),
    )
    for raw, maximum, code in invalid_collections:
        with pytest.raises(worker.WorkerContractError) as caught:
            worker._validate_collected(raw, test_root=tests, maximum=maximum)
        assert caught.value.code == code
    assert worker._analysis_contract(executes_tests=False)["executes_tests"] is False
    assert limits.max_tests == 20


def test_in_process_symbol_projection_handles_all_symbol_kinds(tmp_path: Path) -> None:
    source_path = tmp_path / "symbols.py"
    source_path.write_text(
        "@decorator\n"
        "class Example:\n"
        "    def method(self):\n"
        "        return 1\n"
        "    async def async_method(self):\n"
        "        return 2\n"
        "\n"
        "async def async_function():\n"
        "    return 3\n"
        "\n"
        "if True:\n"
        "    def nested():\n"
        "        return 4\n",
        encoding="utf-8",
    )
    source = _source_file(source_path, "symbols.py", "symbols")
    symbols = worker._symbol_payloads(source)
    assert {item["kind"] for item in symbols} == {
        "module",
        "class",
        "method",
        "async_method",
        "async_function",
        "function",
    }
    assert next(item for item in symbols if item["kind"] == "class")["start_line"] == 1

    invalid = worker.SourceFile(
        source.path,
        source.relative_path,
        source.module,
        1,
        worker._content_digest(b"("),
        True,
        b"(",
    )
    with pytest.raises(worker.WorkerContractError, match="not valid Python"):
        worker._symbol_payloads(invalid)


class _FixtureCoverageReport:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def json_report(self, **arguments: object) -> None:
        Path(cast(str, arguments["outfile"])).write_text(json.dumps(self.payload), encoding="utf-8")


def _coverage_file_payload() -> dict[str, object]:
    return {
        "contexts": {"1": ["ctx-b", "ctx-a", "ctx-a"], "2": []},
        "excluded_lines": [],
        "executed_branches": [[1, 2], [1, 2]],
        "executed_lines": [2, 1, 1],
        "missing_branches": [[2, -1]],
        "missing_lines": [3],
    }


def test_in_process_coverage_projection_validates_every_nested_shape(tmp_path: Path) -> None:
    source_path = tmp_path / "covered.py"
    source_path.write_text("VALUE = 1\n", encoding="utf-8")
    source = _source_file(source_path, "covered.py", "covered")
    report_path = tmp_path / "coverage.json"
    relative_name = os.path.relpath(source_path, Path.cwd())
    payload = {"files": {relative_name: _coverage_file_payload()}}
    projected = worker._coverage_payloads(
        _FixtureCoverageReport(payload), [source], report_path, max_contexts=10
    )
    assert projected[0]["executed_lines"] == [1, 2]
    assert projected[0]["executed_branches"] == [[1, 2]]
    assert projected[0]["contexts"] == {"1": ["ctx-a", "ctx-b"], "2": []}

    absolute_payload = {"files": {os.fspath(source_path): _coverage_file_payload()}}
    assert (
        worker._coverage_payloads(
            _FixtureCoverageReport(absolute_payload), [source], report_path, max_contexts=10
        )[0]["relative_path"]
        == "covered.py"
    )

    malformed_payloads: list[object] = [
        {"files": []},
        {"files": {os.fspath(tmp_path / "missing.py"): _coverage_file_payload()}},
        {"files": {os.fspath(source_path): {**_coverage_file_payload(), "executed_lines": "1"}}},
        {"files": {os.fspath(source_path): {**_coverage_file_payload(), "executed_branches": "1"}}},
        {
            "files": {
                os.fspath(source_path): {**_coverage_file_payload(), "executed_branches": [[0, 1]]}
            }
        },
        {"files": {os.fspath(source_path): {**_coverage_file_payload(), "contexts": []}}},
        {"files": {os.fspath(source_path): {**_coverage_file_payload(), "contexts": {"bad": []}}}},
        {"files": {os.fspath(source_path): {**_coverage_file_payload(), "contexts": {"1": [1]}}}},
    ]
    for malformed in malformed_payloads:
        with pytest.raises(worker.WorkerContractError) as caught:
            worker._coverage_payloads(
                _FixtureCoverageReport(malformed), [source], report_path, max_contexts=10
            )
        assert caught.value.code == "coverage_projection_failed"

    with pytest.raises(worker.WorkerContractError) as caught:
        worker._coverage_payloads(
            _FixtureCoverageReport(absolute_payload), [source], report_path, max_contexts=1
        )
    assert caught.value.code == "context_bound_exceeded"


def test_in_process_request_json_signature_and_output_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text('{"value":1}', encoding="utf-8")
    assert worker._read_request(request_path) == {"value": 1}
    for path in (Path("relative.json"), tmp_path, tmp_path / "missing.json"):
        with pytest.raises(worker.WorkerContractError) as caught:
            worker._read_request(path)
        assert caught.value.code == "invalid_request_path"

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"value":1,"value":2}', encoding="utf-8")
    with pytest.raises(worker.WorkerContractError) as caught:
        worker._read_request(duplicate)
    assert caught.value.code == "invalid_json"
    malformed = tmp_path / "malformed.json"
    malformed.write_bytes(b"\xff")
    with pytest.raises(worker.WorkerContractError, match="canonical UTF-8"):
        worker._read_request(malformed)
    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(worker.WorkerContractError, match="JSON object"):
        worker._read_request(array)
    constant = tmp_path / "constant.json"
    constant.write_text('{"value":NaN}', encoding="utf-8")
    with pytest.raises(worker.WorkerContractError) as caught:
        worker._read_request(constant)
    assert caught.value.code == "invalid_json"

    monkeypatch.setattr(worker, "HARD_MAX_REQUEST_BYTES", 1)
    with pytest.raises(worker.WorkerContractError, match="hard byte bound"):
        worker._read_request(request_path)
    monkeypatch.setattr(worker, "HARD_MAX_REQUEST_BYTES", 16 * 1024 * 1024)

    unsigned: dict[str, object] = {"value": 1}
    signature = "deep-coverage-request-v1:xxh3_128:" + xxhash.xxh3_128_hexdigest(
        worker._canonical_json(unsigned)
    )
    signed = {**unsigned, "request_signature": signature}
    assert worker._validate_request_signature(signed) == signature
    with pytest.raises(worker.WorkerContractError, match="disagrees"):
        worker._validate_request_signature({**signed, "request_signature": signature + "0"})

    stream = io.BytesIO()
    monkeypatch.setattr(worker.sys, "stdout", SimpleNamespace(buffer=stream))
    worker._emit({"ready": True}, 1_000)
    assert json.loads(stream.getvalue()) == {"ready": True}
    with pytest.raises(worker.WorkerContractError, match="exceeds"):
        worker._emit({"value": "x" * 100}, 10)

    stream.seek(0)
    stream.truncate()
    with pytest.raises(SystemExit) as exited:
        worker._fail(worker.WorkerContractError("fixture", "failure"), 2_048)
    assert exited.value.code == 2
    assert json.loads(stream.getvalue())["error"]["code"] == "fixture"
    stream.seek(0)
    stream.truncate()
    with pytest.raises(SystemExit):
        worker._fail(worker.WorkerContractError("fixture", "x" * 4_096), 2_048)
    assert stream.getvalue() == b""


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
    scratch = tmp_path / "s"
    package = project / "neocortex"
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
        "import os\n"
        "from pathlib import Path\n\n"
        "from neocortex.logic import choose\n\n"
        "def test_true(tmp_path):\n"
        "    assert choose(True) == 1\n"
        "    runtime = Path(os.environ['NEOCORTEX_AUDIT_LAB_ROOT']).resolve()\n"
        f"    durable = Path({os.fspath(scratch)!r}).resolve()\n"
        f"    outer = Path({os.fspath(tmp_path)!r}).resolve()\n"
        "    assert tmp_path.resolve().is_relative_to(runtime)\n"
        "    assert not tmp_path.resolve().is_relative_to(durable)\n"
        "    assert not tmp_path.resolve().is_relative_to(outer)\n"
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
            "neocortex/__init__.py",
            "neocortex/logic.py",
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
    scratch.mkdir()
    monkeypatch.setattr(deep, "_canonical_repository_root", lambda: project)
    config = deep.DeepCoverageConfig((), 3, 60.0, 1, "real-worker-fixture-v1")
    for name in ("RUNTIME_DIRECTORY", "RUNNER_TEMP", "XDG_RUNTIME_DIR"):
        monkeypatch.setenv(name, os.fspath(tmp_path))

    with providers._deep_coverage_runtime(
        root=project,
        audit_lab_root=tmp_path,
    ) as first_stage:
        first = deep.execute_pytest_coverage(
            first_stage,
            owners,
            {},
            trusted_root=project,
            scratch_root=scratch,
            config=config,
        )

        assert first.measurement_complete is True
        assert first.counters["tests_passed"] == 3, tuple(
            finding.message for finding in first.findings
        )
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
        first_worker_runs = first_stage / "r" / "w"
        assert not first_worker_runs.resolve().is_relative_to(scratch.resolve())
        assert first_worker_runs.is_dir()
        assert not tuple(first_worker_runs.iterdir())
    assert not first_stage.exists()

    with providers._deep_coverage_runtime(
        root=project,
        audit_lab_root=tmp_path,
    ) as replay_stage:
        assert replay_stage != first_stage
        replay = deep.execute_pytest_coverage(
            replay_stage,
            owners,
            {},
            trusted_root=project,
            scratch_root=scratch,
            config=config,
        )

        assert replay.counters["shards_reused"] == 3
        assert replay.process_invocations == 2
        replay_worker_runs = replay_stage / "r" / "w"
        assert not replay_worker_runs.resolve().is_relative_to(scratch.resolve())
        assert replay_worker_runs.is_dir()
        assert not tuple(replay_worker_runs.iterdir())
    assert not replay_stage.exists()
    assert {path.name for path in scratch.iterdir()} == {"checkpoints"}
