"""Contracts for bounded focal Cosmic Ray evidence normalization."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import _04_Nucleo_Operativo.external_mutation_cosmic_ray as mutation
from _04_Nucleo_Operativo.code_external_evidence import ExternalEvidenceFile
from _04_Nucleo_Operativo.semantic_models import fingerprint_bytes


def _stage(
    tmp_path: Path,
) -> tuple[Path, Path, Path, dict[str, ExternalEvidenceFile]]:
    trusted = tmp_path / "trusted"
    stage = tmp_path / "stage"
    source = stage / "source"
    scratch = tmp_path / "scratch"
    trusted.mkdir()
    source.mkdir(parents=True)
    scratch.mkdir()
    files = {
        "pkg/logic.py": "def choose(value):\n    return 1 if value else 2\n",
        "tests/test_logic.py": "from pkg.logic import choose\n\ndef test_choose():\n    assert choose(True) == 1\n",
    }
    staged: dict[str, ExternalEvidenceFile] = {}
    for version_id, (relative, content) in enumerate(files.items(), start=1):
        original = trusted / relative
        copy = source / relative
        original.parent.mkdir(parents=True, exist_ok=True)
        copy.parent.mkdir(parents=True, exist_ok=True)
        original.write_text(content, encoding="utf-8")
        copy.write_text(content, encoding="utf-8")
        raw = original.read_bytes()
        fingerprint = fingerprint_bytes(raw)
        metadata = original.stat()
        staged[os.path.normcase(os.path.abspath(copy))] = ExternalEvidenceFile(
            version_id,
            str(original),
            relative,
            metadata.st_size,
            metadata.st_mtime_ns,
            fingerprint.xxh3_128,
            fingerprint.xxh3_64_guard,
        )
    return trusted, stage, scratch, staged


def _config() -> mutation.FocalMutationConfig:
    return mutation.FocalMutationConfig(
        "pkg/logic.py",
        "pkg.logic.choose",
        ("tests/test_logic.py::test_choose",),
        4,
        10,
        60,
        "deep-configuration-v2:fixture",
    )


def test_adapter_normalizes_counts_findings_relations_and_timeout_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted, stage, scratch, staged = _stage(tmp_path)
    target = trusted / "pkg/logic.py"
    original_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    events: list[str] = []
    monkeypatch.setattr(mutation, "_canonical_repository_root", lambda: trusted)
    monkeypatch.setattr(mutation, "cosmic_ray_tool_version", lambda: "8.4.6")

    original_validate_root = mutation._validate_trusted_root
    original_validate_inputs = mutation.validate_external_inputs
    original_sha256 = mutation._sha256

    def validate_root(root: Path) -> Path:
        events.append("trusted-root")
        return original_validate_root(root)

    def validate_inputs(files: tuple[ExternalEvidenceFile, ...]) -> None:
        events.append("validate-inputs")
        original_validate_inputs(files)

    def sha256(path: Path) -> str:
        events.append(f"sha256:{path.relative_to(stage / 'source').as_posix()}")
        return original_sha256(path)

    def run(arguments, **_kwargs):
        events.append("worker")
        assert Path(arguments[-1]).is_file()
        assert not Path(arguments[-1]).with_suffix(".tmp").exists()
        request = json.loads(Path(arguments[-1]).read_text(encoding="utf-8"))
        raw = {
            "operator": "core/NumberReplacer",
            "occurrence": 0,
            "definition_name": "choose",
            "start_line": 2,
            "start_column": 11,
            "end_line": 2,
            "end_column": 12,
            "duration_milliseconds": 20,
            "output_sha256": "a" * 64,
        }
        payload = {
            "schema": mutation.COSMIC_RAY_MUTATION_WORKER_SCHEMA,
            "status": "ready",
            "request_signature": request["request_signature"],
            "measurement_scope_signature": request["measurement_scope_signature"],
            "canonical_symbol": "pkg.logic.choose",
            "baseline_duration_milliseconds": 15,
            "duration_milliseconds": 80,
            "measurement_complete": True,
            "selection_truncated": False,
            "counts": {
                "generated": 3,
                "selected": 3,
                "completed": 3,
                "killed": 1,
                "survived": 1,
                "timed_out": 1,
                "incompetent": 0,
                "reused": 0,
                "process_invocations": 4,
            },
            "mutations": [
                {**raw, "mutation_id": "mutant-survived", "outcome": "survived"},
                {
                    **raw,
                    "mutation_id": "mutant-timeout",
                    "occurrence": 1,
                    "outcome": "timeout",
                },
                {
                    **raw,
                    "mutation_id": "mutant-killed",
                    "occurrence": 2,
                    "outcome": "killed",
                },
            ],
            "limitations": ["advisory_only_no_mutation_authority"],
        }
        encoded = json.dumps(payload).encode("utf-8")
        return subprocess.CompletedProcess(arguments, 0, encoded, b"")

    monkeypatch.setattr(mutation, "_validate_trusted_root", validate_root)
    monkeypatch.setattr(mutation, "validate_external_inputs", validate_inputs)
    monkeypatch.setattr(mutation, "_sha256", sha256)
    monkeypatch.setattr(mutation, "run_bounded_capture", run)
    result = mutation.execute_cosmic_ray_mutation(
        stage,
        staged,
        {},
        trusted_root=trusted,
        scratch_root=scratch,
        config=_config(),
    )

    metrics = {item.metric_name: item for item in result.metrics}
    findings = {item.code: item for item in result.findings}
    assert metrics["mutation_score"].value == 0.5
    assert metrics["mutants_timed_out"].value == 1
    assert metrics["baseline_passed"].value == 1
    assert metrics["mutation_score"].subject_kind == "symbol"
    assert metrics["mutation_score"].subject_key == "pkg.logic.choose"
    assert set(findings) == {"MUTATION_SURVIVED", "MUTATION_TIMEOUT"}
    assert findings["MUTATION_TIMEOUT"].mutation_authority is False
    assert {item.relation_kind for item in result.relations} == {
        "mutation_targets_file",
        "mutation_targets_symbol",
        "mutation_tested_by",
    }
    assert result.process_invocations == 5
    assert result.measurement_complete is True
    assert hashlib.sha256(target.read_bytes()).hexdigest() == original_digest
    assert events == [
        "trusted-root",
        "validate-inputs",
        "sha256:pkg/logic.py",
        "sha256:tests/test_logic.py",
        "worker",
        "validate-inputs",
        "sha256:pkg/logic.py",
    ]


def test_adapter_abstains_without_declared_tests_or_indexed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(mutation.MutationAbstentionError, match="tests_not_declared"):
        mutation.FocalMutationConfig(
            "pkg/logic.py", None, (), 4, 10, 60, "deep-configuration-v2:fixture"
        )

    trusted, stage, scratch, staged = _stage(tmp_path)
    config = mutation.FocalMutationConfig(
        "pkg/missing.py",
        None,
        ("tests/test_logic.py::test_choose",),
        4,
        10,
        60,
        "deep-configuration-v2:fixture",
    )
    monkeypatch.setattr(mutation, "_canonical_repository_root", lambda: trusted)
    with pytest.raises(mutation.MutationAbstentionError, match="target_not_indexed"):
        mutation.execute_cosmic_ray_mutation(
            stage,
            staged,
            {},
            trusted_root=trusted,
            scratch_root=scratch,
            config=config,
        )


def test_adapter_preserves_bounded_worker_failure_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted, stage, scratch, staged = _stage(tmp_path)
    monkeypatch.setattr(mutation, "_canonical_repository_root", lambda: trusted)
    monkeypatch.setattr(mutation, "cosmic_ray_tool_version", lambda: "8.4.6")

    def run(arguments, **_kwargs):
        payload = {
            "schema": "neocortex.external-mutation-cosmic-ray-worker/error-v1",
            "status": "error",
            "error": {
                "code": "worker_failure",
                "message": "FileNotFoundError:checkpoint.tmp",
            },
        }
        return subprocess.CompletedProcess(arguments, 2, json.dumps(payload).encode(), b"")

    monkeypatch.setattr(mutation, "run_bounded_capture", run)
    with pytest.raises(
        ValueError,
        match=r"worker_failure:FileNotFoundError:checkpoint\.tmp",
    ):
        mutation.execute_cosmic_ray_mutation(
            stage,
            staged,
            {},
            trusted_root=trusted,
            scratch_root=scratch,
            config=_config(),
        )


def test_input_signature_covers_target_suite_and_limits(tmp_path: Path) -> None:
    _trusted, _stage_root, _scratch, staged = _stage(tmp_path)
    files = tuple(staged.values())
    first = mutation.mutation_input_signature(files, _config())
    changed = mutation.FocalMutationConfig(
        "pkg/logic.py",
        "pkg.logic.choose",
        ("tests/test_logic.py::test_choose",),
        5,
        10,
        60,
        "deep-configuration-v2:fixture",
    )
    assert first != mutation.mutation_input_signature(files, changed)
