"""Focused fixtures for the versioned production architecture contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import _04_Nucleo_Operativo.external_architecture_worker as architecture_worker
from _04_Nucleo_Operativo.code_architecture_contracts import (
    ARCHITECTURE_BASELINE_ID,
    ARCHITECTURE_CONTRACT_SCHEMA,
    ImportLineDetail,
    ModuleImport,
    architecture_contract_manifest,
    evaluate_architecture_contracts,
)


def _evaluations(modules: set[str], imports: tuple[ModuleImport, ...]):
    return {
        item.definition.contract_id: item
        for item in evaluate_architecture_contracts(modules, imports)
    }


def test_declared_boundary_entry_points_pass_with_acyclic_v2_baseline() -> None:
    modules = {
        "neocortex",
        "neocortex.cli",
        "neocortex.sdk",
        "_01_Enumeracion",
        "_02_Deduplicacion",
        "_02_Deduplicacion.__main__",
        "_03_Progreso",
        "_04_Nucleo_Operativo",
        "_04_Nucleo_Operativo.app_paths",
        "_04_Nucleo_Operativo.cli_app",
        "_05_Interfaz",
        "_05_Interfaz.app",
        "_05_Interfaz.worker",
    }
    imports = (
        ModuleImport("_02_Deduplicacion.__main__", "_04_Nucleo_Operativo.app_paths"),
        ModuleImport("_02_Deduplicacion.__main__", "_04_Nucleo_Operativo.cli_app"),
        ModuleImport("neocortex.cli", "_04_Nucleo_Operativo.cli_app"),
        ModuleImport("neocortex.cli", "_05_Interfaz.app"),
        ModuleImport("neocortex.cli", "_05_Interfaz.worker"),
        ModuleImport("neocortex.sdk", "_04_Nucleo_Operativo"),
    )

    evaluations = _evaluations(modules, imports)
    manifest = architecture_contract_manifest()

    assert manifest["schema"] == ARCHITECTURE_CONTRACT_SCHEMA
    assert manifest["baseline_id"] == ARCHITECTURE_BASELINE_ID
    assert manifest["known_cycle_components"] == []
    assert all(item.status == "passed" for item in evaluations.values())


def test_violations_expose_shortest_chains_lines_and_new_cycle() -> None:
    modules = {
        "neocortex",
        "neocortex.bridge",
        "_01_Enumeracion",
        "_01_Enumeracion.source",
        "_02_Deduplicacion",
        "_02_Deduplicacion.worker",
        "_03_Progreso",
        "_04_Nucleo_Operativo",
        "_04_Nucleo_Operativo.alpha",
        "_04_Nucleo_Operativo.beta",
        "_04_Nucleo_Operativo.target",
        "_05_Interfaz",
        "_05_Interfaz.view",
    }
    imports = (
        ModuleImport("_01_Enumeracion.source", "neocortex.bridge"),
        ModuleImport(
            "neocortex.bridge",
            "_04_Nucleo_Operativo.target",
            (ImportLineDetail(7, "from _04_Nucleo_Operativo import target"),),
        ),
        ModuleImport("_02_Deduplicacion.worker", "_04_Nucleo_Operativo.target"),
        ModuleImport("neocortex.bridge", "_05_Interfaz.view"),
        ModuleImport("_04_Nucleo_Operativo.alpha", "_04_Nucleo_Operativo.beta"),
        ModuleImport("_04_Nucleo_Operativo.beta", "_04_Nucleo_Operativo.alpha"),
        ModuleImport("_04_Nucleo_Operativo.target", "tests.helpers"),
    )

    evaluations = _evaluations(modules, imports)

    foundation = evaluations["foundation-does-not-depend-on-core-or-ui-v1"]
    assert foundation.status == "failed"
    assert foundation.violations[0].import_chain == (
        "_01_Enumeracion.source",
        "neocortex.bridge",
        "_04_Nucleo_Operativo.target",
    )
    assert foundation.violations[0].details[0].line_number == 7
    assert evaluations["dedup-core-boundary-v1"].status == "failed"
    assert evaluations["neocortex-core-ui-boundary-v1"].status == "failed"
    assert evaluations["production-does-not-import-nonproduction-namespaces-v1"].status == "failed"
    cycles = evaluations["no-new-production-import-cycles-v1"]
    assert cycles.status == "failed"
    assert cycles.violations[0].import_chain[0] == cycles.violations[0].import_chain[-1]


def test_live_repository_graph_satisfies_published_architecture_contracts() -> None:
    """Keep the versioned policy connected to the production graph it gates."""

    assert architecture_worker.__file__ is not None
    root = Path(__file__).resolve().parents[1]
    executable = os.environ.get("NEOCORTEX_ARCHITECTURE_TEST_PYTHON", sys.executable)
    completed = subprocess.run(
        [
            executable,
            "-I",
            architecture_worker.__file__,
            "grimp",
            "--root",
            os.fspath(root),
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    failures = [item for item in payload["contract_evaluations"] if item["status"] == "failed"]
    assert failures == []

    public_facades = {"neocortex.read_api", "neocortex.value_cli_adapter"}
    crossings = {
        (item["importer"], item["imported"])
        for item in payload["relations"]
        if item["importer"] in public_facades
        and item["imported"].partition(".")[0] in {"_04_Nucleo_Operativo", "_05_Interfaz"}
    }
    assert crossings == {
        ("neocortex.read_api", "_04_Nucleo_Operativo.read_api_port"),
        (
            "neocortex.value_cli_adapter",
            "_04_Nucleo_Operativo.value_review_port",
        ),
    }

    central_component = next(
        (
            item["modules"]
            for item in payload["cycles"]
            if "_04_Nucleo_Operativo.actions" in item["modules"]
        ),
        (),
    )
    assert "_04_Nucleo_Operativo.archive_route" not in central_component
    assert "_04_Nucleo_Operativo.video_route" not in central_component
