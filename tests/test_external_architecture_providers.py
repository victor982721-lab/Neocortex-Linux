"""Focused normalization and command contracts for architecture adapters."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import _04_Nucleo_Operativo.external_architecture_providers as adapters
from _04_Nucleo_Operativo.code_external_evidence import ExternalEvidenceFile


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
        "_04_Nucleo_Operativo/a.py",
        "_04_Nucleo_Operativo/b.py",
    )
    observed: list[tuple[str, ...]] = []

    def run(arguments, **kwargs):
        observed.append(tuple(arguments))
        assert kwargs["cwd"] == tmp_path
        payload = {
            "source/_04_Nucleo_Operativo/a.py": ["source/_04_Nucleo_Operativo/b.py"],
            "source/_04_Nucleo_Operativo/b.py": [],
        }
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload).encode(), b"")

    monkeypatch.setattr(adapters, "run_bounded_capture", run)
    result = adapters.execute_ruff_analyze_imports(tmp_path, staged, {})

    assert len(observed) == 1
    assert "--isolated" in observed[0]
    assert "--no-preview" in observed[0]
    assert "--no-fix" not in observed[0]
    assert tuple((item.source_key, item.target_key) for item in result.relations) == (
        ("_04_Nucleo_Operativo.a", "_04_Nucleo_Operativo.b"),
    )
    assert result.findings == result.metrics == ()
    assert result.process_invocations == 1


def test_ruff_analyze_rejects_unowned_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _staged(tmp_path, "_04_Nucleo_Operativo/a.py")
    payload = {"source/_04_Nucleo_Operativo/a.py": ["source/missing.py"]}
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
        "_04_Nucleo_Operativo/a.py",
        "_04_Nucleo_Operativo/b.py",
    )
    payload = {
        "module_metrics": [
            {
                "module": "_04_Nucleo_Operativo.a",
                "relative_path": "_04_Nucleo_Operativo/a.py",
                "fan_in": 0,
                "fan_out": 1,
                "cycle_ids": [],
            },
            {
                "module": "_04_Nucleo_Operativo.b",
                "relative_path": "_04_Nucleo_Operativo/b.py",
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
                "importer": "_04_Nucleo_Operativo.a",
                "imported": "_04_Nucleo_Operativo.b",
                "details": [{"line_number": 1, "line_contents": "from . import b"}],
            }
        ],
        "contract_evaluations": [
            {
                "contract": {"contract_id": "fixture-contract-v1", "authority": "gate"},
                "status": "failed",
                "violations": [
                    {
                        "importer": "_04_Nucleo_Operativo.a",
                        "imported": "_04_Nucleo_Operativo.b",
                        "import_chain": [
                            "_04_Nucleo_Operativo.a",
                            "_04_Nucleo_Operativo.b",
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


def test_complexipy_normalizes_module_and_symbol_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _staged(tmp_path, "_04_Nucleo_Operativo/a.py")
    payload = {
        "module_metrics": [
            {
                "module": "_04_Nucleo_Operativo.a",
                "relative_path": "_04_Nucleo_Operativo/a.py",
                "total": 7,
                "maximum": 5,
                "function_count": 2,
            }
        ],
        "function_metrics": [
            {
                "module": "_04_Nucleo_Operativo.a",
                "relative_path": "_04_Nucleo_Operativo/a.py",
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
            "schema": "neocortex.external-architecture-worker/grimp-v2",
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
