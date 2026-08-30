"""Focused contracts for bounded Deptry dependency-hygiene evidence."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

import neocortex.code.external_dependency_hygiene as hygiene
from neocortex.code.code_external_evidence import ExternalEvidenceFile
from neocortex.semantic.semantic_models import fingerprint_bytes

_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "external_dependency_hygiene"
    / "deptry_0_25_realistic.json"
)


def _stage(
    tmp_path: Path,
    files: Mapping[str, str] | None = None,
) -> tuple[Path, dict[str, ExternalEvidenceFile], Path]:
    contents = (
        {
            "pkg/a.py": "import httpx\nimport certifi\n",
            "pkg/worker.py": "import pytest\n",
        }
        if files is None
        else files
    )
    stage_root = tmp_path / "stage"
    source_root = stage_root / "source"
    source_root.mkdir(parents=True)
    staged: dict[str, ExternalEvidenceFile] = {}
    for version_id, (relative_path, content) in enumerate(contents.items(), start=1):
        path = source_root / Path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        fingerprint = fingerprint_bytes(path.read_bytes())
        staged[os.path.normcase(os.path.abspath(path))] = ExternalEvidenceFile(
            version_id,
            str(tmp_path / "owner" / Path(relative_path)),
            relative_path,
            path.stat().st_size,
            1,
            fingerprint.xxh3_128,
            fingerprint.xxh3_64_guard,
        )
    config_path = tmp_path / "owner" / "pyproject.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """[project]
name = "Fixture_Project"
version = "1.0.0"
dependencies = ["requests", "asyncio"]

[project.optional-dependencies]
dev = ["pytest"]
""",
        encoding="utf-8",
    )
    return stage_root, staged, config_path


def _issue(
    code: str,
    module: str,
    path: str,
    line: int | None,
    column: int | None,
) -> dict[str, object]:
    return {
        "error": {"code": code, "message": f"fixture {code} for {module}"},
        "module": module,
        "location": {"file": path, "line": line, "column": column},
    }


def _install_fake_run(
    monkeypatch: pytest.MonkeyPatch,
    payload: Sequence[object],
    *,
    expected_exit: int | None = None,
) -> list[tuple[str, ...]]:
    observed: list[tuple[str, ...]] = []

    def run(
        arguments: Sequence[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        command = tuple(arguments)
        observed.append(command)
        report = Path(command[command.index("--json-output") + 1])
        report.write_text(json.dumps(payload), encoding="utf-8")
        assert kwargs["timeout_seconds"] == 180.0
        assert kwargs["stdout_limit_bytes"] == 8 * 1024 * 1024
        assert kwargs["stderr_limit_bytes"] == 128 * 1024
        assert kwargs["cwd"] == report.parent.parent
        environment = kwargs["environment"]
        assert isinstance(environment, Mapping)
        assert environment["PIP_NO_INDEX"] == "1"
        assert environment["UV_OFFLINE"] == "1"
        returncode = (1 if payload else 0) if expected_exit is None else expected_exit
        return subprocess.CompletedProcess(command, returncode, b"bounded stdout", b"")

    monkeypatch.setattr(hygiene.importlib.metadata, "version", lambda _name: "0.25.1")
    monkeypatch.setattr(hygiene, "run_bounded_capture", run)
    return observed


def _individual_metrics(
    result: hygiene.DependencyHygieneExecution,
) -> tuple[object, ...]:
    return tuple(
        item for item in result.metrics if item.metric_name.startswith("dependency_issue_dep")
    )


def test_real_deptry_accepts_exact_stage_without_exclusion_panic(tmp_path: Path) -> None:
    stage_root, staged, config_path = _stage(
        tmp_path,
        {"pkg/a.py": "value = 1\n"},
    )
    config_path.write_text(
        """[project]
name = "Fixture_Project"
version = "1.0.0"
dependencies = []

[project.optional-dependencies]
dev = []
""",
        encoding="utf-8",
    )

    result = hygiene.execute_deptry_dependency_hygiene(
        stage_root,
        staged,
        config_path,
        dict(os.environ),
    )

    assert result.findings == ()
    assert result.counters["dependency_issue_count"] == 0
    assert result.process_invocations == 1


def test_realistic_json_preserves_file_and_project_issues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage_root, staged, config_path = _stage(tmp_path)
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    observed = _install_fake_run(monkeypatch, payload)

    result = hygiene.execute_deptry_dependency_hygiene(
        stage_root,
        staged,
        config_path,
        {"SYSTEMROOT": "C:\\Windows"},
    )

    assert len(observed) == 1
    command = observed[0]
    assert command[:5] == (hygiene.sys.executable, "-I", "-m", "deptry", "source")
    assert command[command.index("--config") + 1].endswith("pyproject.toml")
    assert command[command.index("--optional-dependencies-dev-groups") + 1] == "dev"
    assert command[command.index("--package-module-name-map") + 1] == (
        "Pillow=PIL,PyMuPDF=fitz,pdfminer.six=pdfminer,"
        "faster-whisper=faster_whisper,nudenet=nudenet,numpy=numpy,"
        "PySide6=PySide6,pytesseract=pytesseract"
    )
    assert command[command.index("--exclude") + 1] == r"\x00"
    assert "--json-output" in command
    assert "--no-ansi" in command
    assert "--ignore-notebooks" in command

    assert {item.code for item in result.findings} == {"DEP001", "DEP003", "DEP004"}
    assert {item.relative_path for item in result.findings} == {
        "pkg/a.py",
        "pkg/worker.py",
    }
    assert all(item.category == "dependency_hygiene" for item in result.findings)
    assert all(
        item.gate_authority == "dependency_declaration_integrity" for item in result.findings
    )
    assert all(item.mutation_authority is False for item in result.findings)

    individual = _individual_metrics(result)
    assert len(individual) == 5
    assert all(item.subject_kind == "project" for item in individual)
    assert all(item.subject_key == "project:fixture-project" for item in individual)
    metadata_by_code = {str(item.metadata["code"]): item.metadata for item in individual}
    assert metadata_by_code["DEP001"]["classification"] == "gate"
    assert metadata_by_code["DEP001"]["declared"] is False
    assert metadata_by_code["DEP003"]["transitive"] is True
    assert metadata_by_code["DEP004"]["dev"] is True
    assert metadata_by_code["DEP002"]["classification"] == "advisory"
    assert metadata_by_code["DEP002"]["path"] == "pyproject.toml"
    assert metadata_by_code["DEP002"]["line"] is None
    assert metadata_by_code["DEP005"]["classification"] == "advisory"
    assert all(item.metadata["mutation_authority"] is False for item in individual)

    assert result.counters == {
        "dependency_issue_count": 5,
        "dependency_duplicate_report_row_count": 0,
        "dependency_gate_issue_count": 3,
        "dependency_advisory_issue_count": 2,
        "dependency_python_issue_count": 3,
        "dependency_project_issue_count": 2,
        "dependency_dep001_count": 1,
        "dependency_dep002_count": 1,
        "dependency_dep003_count": 1,
        "dependency_dep004_count": 1,
        "dependency_dep005_count": 1,
    }
    aggregate = {item.metric_name: item.value for item in result.metrics}
    assert aggregate["dependency_issue_count"] == 5.0
    assert aggregate["dependency_duplicate_report_row_count"] == 0.0
    assert aggregate["dependency_gate_issue_count"] == 3.0
    assert aggregate["dependency_advisory_issue_count"] == 2.0
    assert {item.relation_kind for item in result.relations} == {"dependency_hygiene_scope"}
    assert {item.target_key for item in result.relations} == {"pkg.a", "pkg.worker"}
    assert all(item.source_key == "project:fixture-project" for item in result.relations)
    assert all(item.metadata["mutation_authority"] is False for item in result.relations)
    assert result.process_invocations == 1
    assert result.stdout_bytes == len(b"bounded stdout")
    assert result.stderr_bytes == 0
    assert result.limitations == hygiene.DEPTRY_LIMITATIONS


def test_unowned_python_issue_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage_root, staged, config_path = _stage(tmp_path)
    _install_fake_run(
        monkeypatch,
        [_issue("DEP001", "httpx", "source/pkg/missing.py", 1, 0)],
    )

    with pytest.raises(ValueError, match="unowned path"):
        hygiene.execute_deptry_dependency_hygiene(
            stage_root,
            staged,
            config_path,
            {},
        )


def test_project_issue_is_not_coerced_into_a_file_finding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage_root, staged, config_path = _stage(tmp_path)
    _install_fake_run(
        monkeypatch,
        [_issue("DEP002", "requests", "pyproject.toml", None, None)],
    )

    result = hygiene.execute_deptry_dependency_hygiene(
        stage_root,
        staged,
        config_path,
        {},
    )

    assert result.findings == ()
    individual = _individual_metrics(result)
    assert len(individual) == 1
    assert individual[0].metadata["location_kind"] == "project"
    assert individual[0].metadata["classification"] == "advisory"


def test_exact_duplicate_project_rows_are_collapsed_and_counted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage_root, staged, config_path = _stage(tmp_path)
    duplicate = _issue("DEP002", "Pillow", "pyproject.toml", None, None)
    _install_fake_run(monkeypatch, [duplicate, duplicate, duplicate])

    result = hygiene.execute_deptry_dependency_hygiene(
        stage_root,
        staged,
        config_path,
        {},
    )

    individual = _individual_metrics(result)
    assert len(individual) == 1
    assert individual[0].metadata["module"] == "Pillow"
    assert result.counters["dependency_issue_count"] == 1
    assert result.counters["dependency_duplicate_report_row_count"] == 2


def test_issue_bound_and_exit_contract_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage_root, staged, config_path = _stage(tmp_path)
    payload = [
        _issue("DEP001", "httpx", "source/pkg/a.py", 1, 0),
        _issue("DEP003", "certifi", "source/pkg/a.py", 2, 0),
    ]
    _install_fake_run(monkeypatch, payload)
    monkeypatch.setattr(hygiene, "_MAX_ISSUES", 1)

    with pytest.raises(ValueError, match="issue bound"):
        hygiene.execute_deptry_dependency_hygiene(
            stage_root,
            staged,
            config_path,
            {},
        )

    monkeypatch.setattr(hygiene, "_MAX_ISSUES", 10_000)
    _install_fake_run(monkeypatch, payload, expected_exit=0)
    with pytest.raises(ValueError, match="exit status"):
        hygiene.execute_deptry_dependency_hygiene(
            stage_root,
            staged,
            config_path,
            {},
        )


def test_stage_requires_exact_inventory_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage_root, staged, config_path = _stage(tmp_path)
    extra = stage_root / "source" / "pkg" / "extra.py"
    extra.write_text("pass\n", encoding="utf-8")
    observed = _install_fake_run(monkeypatch, [])

    with pytest.raises(ValueError, match="inventory ownership disagree"):
        hygiene.execute_deptry_dependency_hygiene(
            stage_root,
            staged,
            config_path,
            {},
        )

    assert observed == []


def test_stage_fingerprint_must_match_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage_root, staged, config_path = _stage(tmp_path)
    source = stage_root / "source" / "pkg" / "a.py"
    source.write_text("import other\nimport certifi\n", encoding="utf-8")
    observed = _install_fake_run(monkeypatch, [])

    with pytest.raises(ValueError, match="fingerprint disagrees"):
        hygiene.execute_deptry_dependency_hygiene(
            stage_root,
            staged,
            config_path,
            {},
        )

    assert observed == []


def test_config_and_version_contracts_are_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage_root, staged, config_path = _stage(tmp_path)
    config_path.write_text(
        """[project]
name = "fixture"
dependencies = []

[project.optional-dependencies]
test = ["pytest"]
""",
        encoding="utf-8",
    )
    _install_fake_run(monkeypatch, [])

    with pytest.raises(ValueError, match="optional dependency group dev"):
        hygiene.execute_deptry_dependency_hygiene(
            stage_root,
            staged,
            config_path,
            {},
        )

    second_stage, second_staged, second_config = _stage(tmp_path / "third")
    monkeypatch.setattr(hygiene.importlib.metadata, "version", lambda _name: "0.26.0")
    with pytest.raises(ValueError, match="version is unsupported"):
        hygiene.execute_deptry_dependency_hygiene(
            second_stage,
            second_staged,
            second_config,
            {},
        )
