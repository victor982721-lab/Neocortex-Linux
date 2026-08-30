"""Focused normalization and command contracts for architecture adapters."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import neocortex.code.external_architecture_providers as adapters
from neocortex.code.code_external_evidence import ExternalEvidenceFile


def _staged(
    stage_root: Path,
    *relative_paths: str,
) -> dict[str, ExternalEvidenceFile]:
    result = {}
    for version_id, relative_path in enumerate(relative_paths, start=1):
        path = stage_root / "source" / Path(relative_path)
        result[os.path.normcase(os.path.abspath(path))] = ExternalEvidenceFile(
            version_id,
            str(path),
            relative_path,
            1,
            1,
            "a" * 32,
            "b" * 16,
        )
    return result


def test_ruff_analyze_normalizes_owned_production_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _staged(
        tmp_path,
        "neocortex/a.py",
        "neocortex/b.py",
    )
    observed: list[tuple[str, ...]] = []

    def run(arguments, **kwargs):
        observed.append(tuple(arguments))
        assert kwargs["cwd"] == tmp_path
        payload = {
            "source/neocortex/a.py": ["source/neocortex/b.py"],
            "source/neocortex/b.py": [],
        }
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload).encode(), b"")

    monkeypatch.setattr(adapters, "run_bounded_capture", run)
    result = adapters.execute_ruff_analyze_imports(tmp_path, staged, {})

    assert len(observed) == 1
    assert "--isolated" in observed[0]
    assert "--no-preview" in observed[0]
    assert "--no-fix" not in observed[0]
    assert tuple((item.source_key, item.target_key) for item in result.relations) == (
        ("neocortex.a", "neocortex.b"),
    )
    assert result.findings == result.metrics == ()
    assert result.process_invocations == 1


def test_ruff_analyze_rejects_unowned_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _staged(tmp_path, "neocortex/a.py")
    payload = {"source/neocortex/a.py": ["source/missing.py"]}
    monkeypatch.setattr(
        adapters,
        "run_bounded_capture",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(
            arguments, 0, json.dumps(payload).encode(), b""
        ),
    )

    with pytest.raises(ValueError, match="unowned"):
        adapters.execute_ruff_analyze_imports(tmp_path, staged, {})


def test_grimp_normalizes_graph_contracts_and_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _staged(
        tmp_path,
        "neocortex/a.py",
        "neocortex/b.py",
    )
    payload = {
        "module_metrics": [
            {
                "module": "neocortex.a",
                "relative_path": "neocortex/a.py",
                "fan_in": 0,
                "fan_out": 1,
                "cycle_ids": [],
            },
            {
                "module": "neocortex.b",
                "relative_path": "neocortex/b.py",
                "fan_in": 1,
                "fan_out": 0,
                "cycle_ids": [],
            },
        ],
        "cycles": [],
        "counters": {
            "modules": 2,
            "production_relations": 1,
            "cyclic_components": 0,
        },
        "relations": [
            {
                "relation": "module_import",
                "importer": "neocortex.a",
                "imported": "neocortex.b",
                "details": [{"line_number": 1, "line_contents": "from . import b"}],
            }
        ],
        "contract_evaluations": [
            {
                "contract": {"contract_id": "fixture-contract-v1", "authority": "gate"},
                "status": "failed",
                "violations": [
                    {
                        "importer": "neocortex.a",
                        "imported": "neocortex.b",
                        "import_chain": [
                            "neocortex.a",
                            "neocortex.b",
                        ],
                        "message": "fixture violation",
                        "details": [{"line_number": 1, "line_contents": "from . import b"}],
                        "metadata": {},
                    }
                ],
            }
        ],
    }
    monkeypatch.setattr(
        adapters,
        "_execute_worker",
        lambda *_args, **_kwargs: (payload, 100, 0),
    )
    monkeypatch.setattr(adapters, "_core_target_evidence", lambda *_args: ([], []))

    result = adapters.execute_grimp_architecture(tmp_path, staged, {})
    names = {(item.subject_kind, item.metric_name) for item in result.metrics}

    assert ("module", "module_fan_in") in names
    assert ("module", "module_fan_out") in names
    assert ("module", "module_scc_size") in names
    assert ("module", "module_cycle_membership") in names
    assert ("run", "internal_module_count") in names
    assert ("run", "internal_import_edge_count") in names
    assert ("run", "cyclic_scc_count") in names
    assert ("contract", "architecture_contract_evaluated") in names
    assert ("contract", "architecture_contract_violations") in names
    assert len(result.relations) == 1
    assert result.findings[0].category == "architecture"
    assert result.findings[0].metadata["contract_id"] == "fixture-contract-v1"
    assert result.findings[0].mutation_authority is False


def test_core_target_projection_emits_regression_findings(
    tmp_path: Path,
) -> None:
    module = "neocortex.a"
    relative = "neocortex/a.py"
    staged = _staged(tmp_path, relative)
    relation = {
        "source_module": module,
        "target_module": "neocortex.b",
        "witness_ids": ["module-import-v1:fixture"],
    }
    payload = {
        "projections": {
            "core_target": {
                "registry": {
                    "schema": adapters.CORE_RESPONSIBILITY_REGISTRY_SCHEMA,
                    "fingerprint": "core-architecture-target-v1:sha256:fixture",
                },
                "scope": {
                    "registered_modules": [module],
                    "missing_registered_modules": [],
                    "unregistered_core_modules": [module],
                },
                "target_responsibility": {
                    "counters": {"unmapped_modules": 1, "overlapping_modules": 0},
                    "mapping_resolutions": [
                        {"module_id": module, "status": "unmapped", "labels": []}
                    ],
                },
                "target_family": {
                    "counters": {
                        "unmapped_modules": 0,
                        "overlapping_modules": 0,
                        "forbidden_direct_module_edges": 187,
                        "baseline_forbidden_direct_module_edges": 186,
                        "regression_direct_module_edges": 1,
                        "resolved_direct_module_edges": 0,
                    },
                    "mapping_resolutions": [
                        {"module_id": module, "status": "resolved", "labels": ["code"]}
                    ],
                    "projected_edges": [
                        {
                            "source_label": "code",
                            "target_label": "knowledge",
                            "module_relations": [relation],
                        },
                    ],
                    "transition_baseline": [
                        {
                            "source_family": "code",
                            "target_family": "knowledge",
                            "regression_direct_module_edges": 1,
                        }
                    ],
                    "edge_decisions": [],
                },
            }
        }
    }

    findings, metrics = adapters._core_target_evidence(
        payload,
        staged,
        {module: relative},
    )

    assert {item.code for item in findings} == {
        "core_target_family_regression",
        "core_target_responsibility_unmapped",
    }
    values = {item.metric_name: item.value for item in metrics}
    assert values["family_regression_direct_edge_count"] == 1


def test_complexipy_normalizes_module_and_symbol_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _staged(tmp_path, "neocortex/a.py")
    payload = {
        "module_metrics": [
            {
                "module": "neocortex.a",
                "relative_path": "neocortex/a.py",
                "total": 7,
                "maximum": 5,
                "function_count": 2,
            }
        ],
        "function_metrics": [
            {
                "module": "neocortex.a",
                "relative_path": "neocortex/a.py",
                "symbol": "f",
                "start_line": 1,
                "end_line": 4,
                "value": 5,
                "scope": "symbol",
                "lines": [{"line": 2, "complexity": 1}],
            }
        ],
    }
    monkeypatch.setattr(
        adapters,
        "_execute_worker",
        lambda *_args, **_kwargs: (payload, 90, 0),
    )

    result = adapters.execute_complexipy_cognitive(tmp_path, staged, {})
    observed = {(item.subject_kind, item.metric_name, item.value) for item in result.metrics}

    assert ("module", "module_cognitive_complexity_total", 7.0) in observed
    assert ("module", "module_cognitive_complexity_max", 5.0) in observed
    assert ("symbol", "cognitive_complexity", 5.0) in observed
    assert result.findings == result.relations == ()


def test_worker_command_is_direct_isolated_and_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, ...]] = []

    def run(arguments, **kwargs):
        observed.append(tuple(arguments))
        assert kwargs["timeout_seconds"] == 180.0
        payload = {
            "schema": "neocortex.external-architecture-worker/grimp-v3",
            "status": "ready",
            "inputs": {"file_count": 1},
        }
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload).encode(), b"")

    monkeypatch.setattr(adapters, "run_bounded_capture", run)
    payload, _, _ = adapters._execute_worker("grimp", tmp_path, {})

    assert payload["status"] == "ready"
    assert observed[0][1] == "-I"
    assert observed[0][2].endswith("external_architecture_worker.py")
    assert "-m" not in observed[0]
    assert "--max-files" in observed[0]
    assert "--max-input-bytes" in observed[0]
    assert "--max-output-bytes" in observed[0]
