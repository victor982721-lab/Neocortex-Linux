"""Characterization and dependency gates for corpus mutation policies."""
# region [00] Contexto del módulo
# Módulo: tests/test_corpus_policy_architecture.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

from neocortex.safety import corpus_access, internal_paths, protected_content
# endregion [01]

# region [02] Implementación

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORPUS_MODULE = "neocortex.safety.corpus_access"
INTERNAL_MODULE = "neocortex.safety.internal_paths"
PROTECTED_MODULE = "neocortex.safety.protected_content"
LEGACY_POLICY_MODULES = {
    "neocortex.safety.corpus_access",
    "neocortex.safety.internal_paths",
    "neocortex.safety.protected_content",
}
POLICY_MODULES = (CORPUS_MODULE, INTERNAL_MODULE, PROTECTED_MODULE)

EXPECTED_ALL = {
    CORPUS_MODULE: (
        "PROTECTED_ANALYSIS_REASON",
        "CorpusAccessMode",
        "CorpusAccessPolicy",
        "CorpusMutationGuard",
        "ProtectedAnalysisRootError",
        "path_trees_intersect",
    ),
    INTERNAL_MODULE: (
        "EFFECTIVE_INVENTORY_POLICY_VERSION",
        "EFFECTIVE_INVENTORY_POLICY_VERSION_V2",
        "INTERNAL_PATHS_POLICY_VERSION",
        "INTERNAL_PATH_PROTECTION_REASON",
        "InternalPathIdentity",
        "InternalPathKind",
        "InternalPathProtectionError",
        "InternalPathRole",
        "InternalPathSpec",
        "InternalPathsPolicy",
        "canonical_internal_paths_policy",
        "effective_inventory_policy_signature",
    ),
    PROTECTED_MODULE: (
        "MAX_PROTECTED_PATH_ENTRIES",
        "PROTECTED_CONTENT_POLICY_VERSION",
        "PROTECTED_CONTENT_REASON",
        "ProtectedContentError",
        "ProtectedContentPolicy",
        "ProtectedDisposition",
        "ProtectedPathIdentity",
        "ProtectedPathKind",
        "ProtectedPathSpec",
        "canonical_protected_content_policy",
    ),
}

EXPECTED_DATACLASS_FIELDS = {
    corpus_access.CorpusAccessPolicy: (
        "mode",
        "root",
        "root_device_id",
        "root_file_id",
        "root_birthtime_ns",
    ),
    corpus_access.CorpusMutationGuard: (
        "policy",
        "internal_paths_policy",
        "protected_content_policy",
    ),
    internal_paths.InternalPathSpec: ("role", "kind", "path"),
    internal_paths.InternalPathIdentity: (
        "role",
        "kind",
        "configured_path",
        "canonical_path",
        "exists",
        "device_id",
        "file_id",
        "birthtime_ns",
    ),
    internal_paths.InternalPathsPolicy: ("entries", "signature"),
    protected_content.ProtectedPathSpec: ("role", "kind", "disposition", "path"),
    protected_content.ProtectedPathIdentity: (
        "role",
        "kind",
        "disposition",
        "configured_path",
        "canonical_path",
        "exists",
        "device_id",
        "file_id",
        "birthtime_ns",
    ),
    protected_content.ProtectedContentPolicy: ("entries", "signature"),
}


def _source_tree(module_name: str) -> ast.Module:
    path = PROJECT_ROOT.joinpath(*module_name.split(".")).with_suffix(".py")
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_imports(module_name: str) -> set[str]:
    package = module_name.rpartition(".")[0]
    imported: set[str] = set()
    for node in ast.walk(_source_tree(module_name)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.module is None:
                imported.update(f"{package}.{alias.name}" for alias in node.names)
            elif node.module is not None:
                imported.add(f"{package}.{node.module}" if node.level else node.module)
    return imported


def test_corpus_policy_public_surface_and_identity_are_stable() -> None:
    modules = {
        CORPUS_MODULE: corpus_access,
        INTERNAL_MODULE: internal_paths,
        PROTECTED_MODULE: protected_content,
    }
    for module_name, module in modules.items():
        assert tuple(module.__all__) == EXPECTED_ALL[module_name]
    for contract, expected_fields in EXPECTED_DATACLASS_FIELDS.items():
        assert contract.__module__ in LEGACY_POLICY_MODULES
        assert tuple(field.name for field in fields(contract)) == expected_fields
    assert issubclass(
        protected_content.ProtectedContentError,
        corpus_access.ProtectedAnalysisRootError,
    )
    assert protected_content.ProtectedContentError.reason_code == "protected_content_root"


def test_corpus_policy_static_dependencies_follow_one_direction() -> None:
    policy_modules = set(POLICY_MODULES)
    observed_edges = {
        (importer, imported)
        for importer in POLICY_MODULES
        for imported in _module_imports(importer)
        if imported in policy_modules
    }
    assert observed_edges == {
        (INTERNAL_MODULE, CORPUS_MODULE),
        (PROTECTED_MODULE, CORPUS_MODULE),
    }

    type_checking_block = next(
        node
        for node in _source_tree(CORPUS_MODULE).body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
    )
    ports = {node.name: node for node in type_checking_block.body if isinstance(node, ast.ClassDef)}
    assert set(ports) == {
        "InternalPathsPolicy",
        "ProtectedContentPolicy",
        "_ProtectedPathIdentity",
    }
    assert all(
        any(isinstance(base, ast.Name) and base.id == "Protocol" for base in port.bases)
        for port in ports.values()
    )


def test_live_grimp_graph_has_no_corpus_policy_cycle() -> None:
    worker = PROJECT_ROOT / "neocortex" / "code" / "external_architecture_worker.py"
    executable = os.environ.get("NEOCORTEX_ARCHITECTURE_TEST_PYTHON", sys.executable)
    completed = subprocess.run(
        [
            executable,
            "-I",
            os.fspath(worker),
            "grimp",
            "--root",
            os.fspath(PROJECT_ROOT),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["status"] == "ready"
    assert payload["counters"]["contract_violations"] == 0
    assert payload["counters"]["cyclic_components"] <= 1
    policy_modules = set(POLICY_MODULES)
    assert all(policy_modules.isdisjoint(cycle["modules"]) for cycle in payload["cycles"])


# endregion [02]
