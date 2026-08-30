"""Real Cosmic Ray worker regression on an isolated focal project."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import neocortex.code.external_mutation_cosmic_ray as mutation


def _python_with_cosmic_ray() -> str:
    override = os.environ.get("NEOCORTEX_COSMIC_RAY_TEST_PYTHON")
    if override:
        return override
    try:
        if importlib.metadata.version("cosmic-ray") == "8.4.6":
            return sys.executable
    except importlib.metadata.PackageNotFoundError:
        pass
    pytest.skip("Cosmic Ray 8.4.6 is not installed in the active test runtime")


def _tool_versions(python: str) -> dict[str, str]:
    script = (
        "import importlib.metadata,json,sys;"
        "print(json.dumps({'cosmic-ray':importlib.metadata.version('cosmic-ray'),"
        "'pytest':importlib.metadata.version('pytest'),'python':sys.version.split()[0]}))"
    )
    completed = subprocess.run(
        [python, "-I", "-c", script], check=True, capture_output=True, text=True, timeout=20
    )
    return json.loads(completed.stdout)


def _request(tmp_path: Path, python: str) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    tests = project / "tests"
    tests.mkdir(parents=True)
    scratch.mkdir()
    target = project / "logic.py"
    target.write_text(
        "def choose(value: int) -> str:\n"
        "    if value > 0:\n"
        "        return 'positive'\n"
        "    return 'other'\n",
        encoding="utf-8",
    )
    test = tests / "test_logic.py"
    test.write_text(
        "from logic import choose\n\n"
        "def test_positive():\n"
        "    assert choose(1) == 'positive'\n\n"
        "def test_other():\n"
        "    assert choose(0) == 'other'\n",
        encoding="utf-8",
    )
    manifest = []
    for relative, path in (("logic.py", target), ("tests/test_logic.py", test)):
        raw = path.read_bytes()
        manifest.append(
            {"relative_path": relative, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        )
    request: dict[str, object] = {
        "schema": mutation.COSMIC_RAY_MUTATION_REQUEST_SCHEMA,
        "project_root": str(project.resolve()),
        "scratch_root": str(scratch.resolve()),
        "target": "logic.py",
        "symbol": "logic.choose",
        "test_selectors": [
            "tests/test_logic.py::test_other",
            "tests/test_logic.py::test_positive",
        ],
        "configuration_signature": "deep-configuration-v2:fixture",
        "measurement_scope_signature": "cosmic-ray-mutation-input-v1:fixture",
        "source_manifest": manifest,
        "tool_versions": _tool_versions(python),
        "limits": {
            "max_mutants": 4,
            "mutant_timeout_seconds": 10,
            "time_budget_seconds": 60,
            "max_output_bytes": 262_144,
        },
    }
    request["request_signature"] = mutation._request_signature(request)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    return path, target, scratch


def _run(python: str, request: Path, *, timeout: float = 90) -> dict[str, object]:
    worker = Path(mutation.__file__).with_name("external_mutation_cosmic_ray_worker.py")
    completed = subprocess.run(
        [python, "-I", str(worker), "--request", str(request.resolve())],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


def test_real_worker_is_reproducible_resumable_and_preserves_source(tmp_path: Path) -> None:
    python = _python_with_cosmic_ray()
    request, target, _scratch = _request(tmp_path, python)
    before = hashlib.sha256(target.read_bytes()).hexdigest()

    first = _run(python, request)
    second = _run(python, request)

    assert first["schema"] == mutation.COSMIC_RAY_MUTATION_WORKER_SCHEMA
    assert first["canonical_symbol"] == "logic.choose"
    assert first["baseline_passed"] is True
    assert first["counts"]["generated"] >= first["counts"]["selected"] == 4
    assert first["counts"]["completed"] == 4
    assert {item["outcome"] for item in first["mutations"]} <= {
        "killed",
        "survived",
        "timeout",
        "incompetent",
    }
    assert second["counts"]["reused"] == 4
    assert second["counts"]["process_invocations"] == 0
    assert [item["mutation_id"] for item in first["mutations"]] == [
        item["mutation_id"] for item in second["mutations"]
    ]
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before


@pytest.mark.skipif(
    os.environ.get("NEOCORTEX_RUN_REAL_MUTATION_ACCEPTANCE") != "1",
    reason="explicit bounded current-work-package acceptance only",
)
def test_real_current_work_package_external_deep_coverage_normalize(tmp_path: Path) -> None:
    python = _python_with_cosmic_ray()
    repository = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    selected_sources = [
        *sorted((repository / "neocortex").rglob("*.py")),
        repository / "tests" / "test_external_deep_coverage.py",
    ]
    manifest = []
    for source in selected_sources:
        relative = source.relative_to(repository).as_posix()
        destination = project / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        raw = destination.read_bytes()
        manifest.append(
            {
                "relative_path": relative,
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    target_relative = "neocortex/external_deep_coverage.py"
    target = project / target_relative
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    request: dict[str, object] = {
        "schema": mutation.COSMIC_RAY_MUTATION_REQUEST_SCHEMA,
        "project_root": str(project.resolve()),
        "scratch_root": str(scratch.resolve()),
        "target": target_relative,
        "symbol": "external_deep_coverage._normalize",
        "test_selectors": ["tests/test_external_deep_coverage.py"],
        "configuration_signature": "deep-configuration-v2:current-work-package",
        "measurement_scope_signature": "cosmic-ray-mutation-input-v1:current-work-package",
        "source_manifest": manifest,
        "tool_versions": _tool_versions(python),
        "limits": {
            "max_mutants": 2,
            "mutant_timeout_seconds": 30,
            "time_budget_seconds": 180,
            "max_output_bytes": 262_144,
        },
    }
    request["request_signature"] = mutation._request_signature(request)
    request_path = tmp_path / "work-package-request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")

    payload = _run(python, request_path, timeout=180)

    assert payload["canonical_symbol"] == ("neocortex.code.external_deep_coverage._normalize")
    assert payload["counts"]["selected"] == payload["counts"]["completed"] == 2
    assert payload["baseline_passed"] is True
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before
