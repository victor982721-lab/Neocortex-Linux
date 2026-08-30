"""Static dependency-direction regression for the Knowledge contracts."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_contract_dependency_direction.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import ast
from pathlib import Path
# endregion [01]

# region [02] Implementación

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "neocortex.knowledge"
FACADE = f"{PACKAGE}.knowledge_contracts"
PROTOCOLS = f"{PACKAGE}.knowledge_contract_protocols"
HELPERS = tuple(
    f"{PACKAGE}.knowledge_contract_{suffix}"
    for suffix in ("context", "payloads", "references", "snapshot", "telemetry")
)
CONTRACT_MODULES = (FACADE, *HELPERS, PROTOCOLS)


def _module_imports(module_name: str) -> set[str]:
    path = PROJECT_ROOT.joinpath(*module_name.split(".")).with_suffix(".py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = module_name.rpartition(".")[0]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.module is None:
                imported.update(f"{package}.{alias.name}" for alias in node.names)
            elif node.module is not None:
                imported.add(f"{package}.{node.module}" if node.level else node.module)
    return imported


def test_knowledge_contract_static_graph_points_to_protocol_leaf() -> None:
    """TYPE_CHECKING imports must follow the same DAG as runtime imports."""

    contract_modules = set(CONTRACT_MODULES)
    observed_edges = {
        (importer, imported)
        for importer in CONTRACT_MODULES
        for imported in _module_imports(importer)
        if imported in contract_modules
    }
    expected_edges = {
        *((FACADE, helper) for helper in HELPERS),
        *((helper, PROTOCOLS) for helper in HELPERS),
    }
    assert observed_edges == expected_edges


# endregion [02]
