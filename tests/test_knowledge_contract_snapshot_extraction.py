"""Tests-first extraction contract for logical Knowledge snapshots."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_contract_snapshot_extraction.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from neocortex.knowledge import knowledge_contracts as contracts
# endregion [01]

# region [02] Implementación


CONTRACT_MODULE = "neocortex.knowledge.knowledge_contracts"
SNAPSHOT_MODULE = "neocortex.knowledge.knowledge_contract_snapshot"
DELEGATES = {
    ("PublicationHead", "__post_init__"): "validate_publication_head",
    ("LogicalWatermark", "__post_init__"): "validate_logical_watermark",
    ("ActiveModel", "__post_init__"): "validate_active_model",
    ("OwnerSnapshot", "__post_init__"): "validate_owner_snapshot",
    ("OwnerSnapshot", "changed"): "owner_snapshot_changed",
    ("KnowledgeSnapshot", "__post_init__"): "validate_knowledge_snapshot",
    ("KnowledgeSnapshot", "create"): "create_knowledge_snapshot",
    ("KnowledgeSnapshot", "changed_owners"): "knowledge_snapshot_changed_owners",
}


def _function_for(class_name: str, method_name: str) -> object:
    value = inspect.getattr_static(getattr(contracts, class_name), method_name)
    if isinstance(value, classmethod):
        return value.__func__
    if isinstance(value, property):
        assert value.fget is not None
        return value.fget
    return value


@pytest.mark.parametrize(
    ("class_name", "method_name", "helper_name"),
    [(*key, helper_name) for key, helper_name in DELEGATES.items()],
)
def test_snapshot_methods_are_thin_delegates(
    class_name: str,
    method_name: str,
    helper_name: str,
) -> None:
    function = _function_for(class_name, method_name)
    assert function.__module__ == CONTRACT_MODULE
    assert function.__qualname__ == f"{class_name}.{method_name}"

    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    definition = tree.body[0]
    assert isinstance(definition, ast.FunctionDef)
    assert len(definition.body) == 1
    calls = [node for node in ast.walk(definition) if isinstance(node, ast.Call)]
    helper_calls = [
        call
        for call in calls
        if isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "_contract_snapshot"
        and call.func.attr == helper_name
    ]
    assert len(helper_calls) == 1


def test_snapshot_module_is_bounded_and_has_no_runtime_facade_cycle() -> None:
    spec = importlib.util.find_spec(SNAPSHOT_MODULE)
    assert spec is not None
    assert spec.origin is not None
    source = Path(spec.origin).read_text(encoding="utf-8")
    assert len(source.splitlines()) <= 450
    tree = ast.parse(source)
    runtime_nodes = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        )
    ]
    runtime_tree = ast.Module(body=runtime_nodes, type_ignores=[])
    imported_modules = {
        node.module
        for node in ast.walk(runtime_tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(runtime_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert CONTRACT_MODULE not in imported_modules
    assert not any(
        module.endswith(".knowledge_contracts") for module in imported_modules
    )

    helper = importlib.import_module(SNAPSHOT_MODULE)
    for helper_name in DELEGATES.values():
        assert callable(getattr(helper, helper_name))


def test_snapshot_modules_form_one_way_runtime_import_dag() -> None:
    modules = (CONTRACT_MODULE, SNAPSHOT_MODULE)
    edges: set[tuple[str, str]] = set()
    for module_name in modules:
        spec = importlib.util.find_spec(module_name)
        assert spec is not None
        assert spec.origin is not None
        tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
        package = module_name.rpartition(".")[0]
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                imported_names = (
                    tuple(f"{package}.{alias.name}" for alias in node.names)
                    if node.level and node.module is None
                    else ((f"{package}.{node.module}" if node.level else node.module),)
                    if node.module is not None
                    else ()
                )
                edges.update(
                    (module_name, imported)
                    for imported in imported_names
                    if imported in modules
                )
            elif isinstance(node, ast.Import):
                edges.update(
                    (module_name, alias.name)
                    for alias in node.names
                    if alias.name in modules
                )
    assert edges == {(CONTRACT_MODULE, SNAPSHOT_MODULE)}


@pytest.mark.parametrize(
    "module_order",
    (
        (CONTRACT_MODULE, SNAPSHOT_MODULE),
        (SNAPSHOT_MODULE, CONTRACT_MODULE),
    ),
)
def test_snapshot_modules_support_both_cold_import_orders(
    module_order: tuple[str, str],
) -> None:
    # Isolated Python does not retain the cwd on ``sys.path``; pass the
    # project root rather than the ``neocortex`` package directory.
    repository = Path(contracts.__file__).resolve().parents[2]
    script = textwrap.dedent(
        f"""
        import importlib
        import sys

        sys.path.insert(0, {str(repository)!r})
        for module_name in {module_order!r}:
            importlib.import_module(module_name)
        facade = importlib.import_module({CONTRACT_MODULE!r})
        helper = importlib.import_module({SNAPSHOT_MODULE!r})
        delegates = {DELEGATES!r}
        for (class_name, method_name), helper_name in delegates.items():
            assert getattr(getattr(facade, class_name), method_name) is not None
            assert callable(getattr(helper, helper_name))
        print("ok")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"
# endregion [02]
