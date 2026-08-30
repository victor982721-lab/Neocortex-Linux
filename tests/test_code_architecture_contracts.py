"""Focused fixtures for the versioned production architecture contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import neocortex.code.external_architecture_worker as architecture_worker
from neocortex.code.code_architecture_contracts import (
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


def test_declared_boundary_entry_points_pass_with_acyclic_v5_baseline() -> None:
    modules = {
        "neocortex",
        "neocortex.cli",
        "neocortex.sdk",
        "neocortex.enumeration",
        "neocortex.enumeration.path_index.schema",
        "neocortex.deduplication",
        "neocortex.deduplication.__main__",
        "neocortex.deduplication.inventory.scanner",
        "neocortex.platform_policy",
        "neocortex.sqlite_schema_contract",
            "neocortex.api",
            "neocortex.api.cli.cli_app",
            "neocortex.api.cli.cli_config",
        "neocortex.interface",
        "neocortex.interface.application.app",
        "neocortex.interface.protocol.worker",
        "neocortex.progress",
        "neocortex.progress.events",
    }
    imports = (
            ModuleImport("neocortex.deduplication.__main__", "neocortex.runtime.config.app_paths"),
            ModuleImport("neocortex.deduplication.__main__", "neocortex.api.cli.cli_app"),
        ModuleImport("neocortex.deduplication.__main__", "neocortex.platform_policy"),
        ModuleImport("neocortex.deduplication.inventory.scanner", "neocortex.progress"),
        ModuleImport(
            "neocortex.enumeration.path_index.schema",
            "neocortex.sqlite_schema_contract",
        ),
            ModuleImport("neocortex.cli", "neocortex.runtime.config.app_paths"),
            ModuleImport("neocortex.cli", "neocortex.api.cli.cli_app"),
        ModuleImport("neocortex.cli", "neocortex.interface.application.app"),
        ModuleImport("neocortex.cli", "neocortex.interface.protocol.worker"),
            ModuleImport("neocortex.interface.application.app", "neocortex.runtime.config.app_paths"),
            ModuleImport("neocortex.interface.protocol.worker", "neocortex.api.cli.cli_config"),
        ModuleImport("neocortex.interface.application.app", "neocortex.platform_policy"),
            ModuleImport("neocortex.sdk", "neocortex.api.public"),
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
        "neocortex.enumeration",
        "neocortex.enumeration.source",
        "neocortex.deduplication",
        "neocortex.deduplication.worker",
        "neocortex.alpha",
        "neocortex.beta",
        "neocortex.api.target",
        "neocortex.interface",
        "neocortex.interface.view",
        "neocortex.progress",
        "neocortex.progress.events",
    }
    imports = (
        ModuleImport("neocortex.enumeration.source", "neocortex.bridge"),
        ModuleImport(
            "neocortex.bridge",
            "neocortex.api.target",
            (ImportLineDetail(7, "from neocortex import target"),),
        ),
        ModuleImport("neocortex.deduplication.worker", "neocortex.api.target"),
        ModuleImport("neocortex.deduplication.worker", "neocortex.bridge"),
        ModuleImport("neocortex.interface.view", "neocortex.api.target"),
        ModuleImport("neocortex.interface.view", "neocortex.bridge"),
        ModuleImport("neocortex.alpha", "neocortex.beta"),
        ModuleImport("neocortex.beta", "neocortex.alpha"),
        ModuleImport("neocortex.api.target", "tests.helpers"),
        ModuleImport("neocortex.progress.events", "neocortex.api.target"),
    )

    evaluations = _evaluations(modules, imports)

    foundation = evaluations["foundation-does-not-depend-on-core-or-ui-v1"]
    assert foundation.status == "failed"
    assert foundation.violations[0].import_chain == (
        "neocortex.enumeration.source",
        "neocortex.bridge",
        "neocortex.api.target",
    )
    assert foundation.violations[0].details[0].line_number == 7
    assert evaluations["dedup-core-boundary-v1"].status == "failed"
    assert evaluations["dedup-product-boundary-v1"].status == "failed"
    assert evaluations["enumeration-product-boundary-v1"].status == "failed"
    assert evaluations["interface-core-boundary-v1"].status == "failed"
    assert evaluations["interface-product-boundary-v1"].status == "failed"
    assert evaluations["neocortex-core-ui-boundary-v1"].status == "failed"
    assert evaluations["progress-does-not-depend-on-other-production-v1"].status == "failed"
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
    projections = payload["projections"]
    assert projections["scope"]["missing_registered_modules"] == []
    assert projections["logical_owner"]["counters"]["unmapped_modules"] == 0
    assert projections["logical_owner"]["counters"]["overlapping_modules"] == 0
    assert projections["target_family"]["counters"]["unmapped_modules"] == 0
    assert projections["target_family"]["counters"]["overlapping_modules"] == 0
    assert projections["target_family"]["counters"]["forbidden_edges"] == 0

    public_facades = {
        "neocortex.read_api",
        "neocortex.review_task_cli_adapter",
        "neocortex.value_cli_adapter",
    }
    crossings = {
        (item["importer"], item["imported"])
        for item in payload["relations"]
        if item["importer"] in public_facades
        and (
            item["imported"].partition(".")[0] == "neocortex"
            or item["imported"] == "neocortex.interface"
            or item["imported"].startswith("neocortex.interface.")
        )
    }
    assert crossings
    assert all("_04_Nucleo_Operativo" not in item for pair in crossings for item in pair)

    central_component = next(
        (
            item["modules"]
            for item in payload["cycles"]
            if "neocortex.workflow.actions.actions" in item["modules"]
        ),
        (),
    )
    assert "neocortex.capabilities.formats.archive.route" not in central_component
    assert "neocortex.capabilities.formats.archive.route" not in central_component
    assert "neocortex.capabilities.formats.video.route" not in central_component


def test_canonical_capability_projection_is_exact_and_preserves_witnesses() -> None:
    canonical, _, _ = architecture_worker._registered_capability_labels()
    modules = tuple(sorted(canonical))
    relation = ModuleImport(canonical[0], canonical[1])

    payload = architecture_worker._capability_projection_payload(modules, (relation,))

    assert payload["schema"] == "neocortex.architecture-projection/v1"
    scope = payload["scope"]
    assert isinstance(scope, dict)
    assert scope["registered_modules"] == list(modules)
    assert scope["present_registered_modules"] == list(modules)
    assert scope["missing_registered_modules"] == []
    owner = payload["logical_owner"]
    family = payload["target_family"]
    assert isinstance(owner, dict)
    assert isinstance(family, dict)
    assert isinstance(owner["counters"], dict)
    assert isinstance(family["counters"], dict)
    assert isinstance(family["projected_edges"], list)
    assert owner["counters"]["resolved_modules"] == len(modules)
    assert family["counters"]["resolved_modules"] == len(modules)
    assert family["counters"]["unmapped_modules"] == 0
    assert family["counters"]["overlapping_modules"] == 0
    assert family["counters"]["forbidden_edges"] == 0
    assert family["edge_decisions"] == [
        {
            "edge_id": family["projected_edges"][0]["edge_id"],
            "source_family": architecture_worker.CAPABILITY_CANONICAL_FAMILY,
            "target_family": architecture_worker.CAPABILITY_CANONICAL_FAMILY,
            "allowed": True,
            "reason": "allowed_same_family",
            "witness_ids": [relation.relation_id],
        }
    ]
    assert family["projected_edges"][0]["module_relations"] == [
        {
            "source_module": relation.importer,
            "target_module": relation.imported,
            "witness_ids": [relation.relation_id],
        }
    ]


def test_disconnected_owner_quotient_cycle_remains_typed_diagnostic_evidence() -> None:
    canonical, owners, _ = architecture_worker._registered_capability_labels()
    modules = tuple(sorted(canonical))
    archive = [module for module, labels in owners.items() if labels == ("archive",)]
    docx = [module for module, labels in owners.items() if labels == ("docx",)]
    imports = (
        ModuleImport(archive[0], docx[0]),
        ModuleImport(docx[1], archive[1]),
    )

    payload = architecture_worker._capability_projection_payload(modules, imports)

    module_graph = payload["module_graph"]
    assert isinstance(module_graph, dict)
    assert module_graph["cyclic_sccs"] == []
    owner = payload["logical_owner"]
    assert isinstance(owner, dict)
    assert isinstance(owner["counters"], dict)
    assert isinstance(owner["aggregate_quotient_sccs"], list)
    assert owner["realizable_sccs"] == []
    assert owner["unresolved_sccs"] == []
    assert owner["counters"]["aggregate_quotient_sccs"] == 1
    aggregate = owner["aggregate_quotient_sccs"][0]
    assert aggregate["labels"] == ["archive", "docx"]
    assert aggregate["authority"] == "diagnostic"
    assert aggregate["semantics"] == ("aggregate_quotient_dependency_cycle_noncomposable-v1")
    assert aggregate["realizable_module_components"] == []
