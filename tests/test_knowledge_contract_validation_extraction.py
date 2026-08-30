"""Tests-first extraction contract for Knowledge validation primitives."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_contract_validation_extraction.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import pickle
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from neocortex.knowledge import knowledge_contracts as contracts
# endregion [01]

# region [02] Implementación


CONTRACT_MODULE = "neocortex.knowledge.knowledge_contracts"
VALIDATION_MODULE = "neocortex.knowledge.knowledge_contract_validation"
DELEGATES = {
    "_required_text": "_contract_required_text_impl",
    "_optional_text": "_contract_optional_text_impl",
}
SIGNATURES = {
    "_required_text": "(name: 'str', value: 'str') -> 'str'",
    "_optional_text": "(name: 'str', value: 'str | None') -> 'str | None'",
}


@pytest.mark.parametrize("name", DELEGATES)
def test_validation_facade_seams_are_stable_thin_delegates(name: str) -> None:
    seam = getattr(contracts, name)
    assert str(inspect.signature(seam)) == SIGNATURES[name]
    assert seam.__module__ == CONTRACT_MODULE
    assert seam.__qualname__ == name
    assert pickle.loads(pickle.dumps(seam, protocol=5)) is seam

    tree = ast.parse(textwrap.dedent(inspect.getsource(seam)))
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    assert len(function.body) == 1
    statement = function.body[0]
    assert isinstance(statement, ast.Return)
    assert isinstance(statement.value, ast.Call)
    assert isinstance(statement.value.func, ast.Name)
    assert statement.value.func.id == DELEGATES[name]


@pytest.mark.parametrize("name", DELEGATES)
def test_validation_facade_forwards_exact_runtime_objects(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = object()
    first = object()
    second = object()
    captured: tuple[object, object] | None = None

    def implementation(arg1: object, arg2: object) -> object:
        nonlocal captured
        captured = (arg1, arg2)
        return marker

    monkeypatch.setattr(contracts, DELEGATES[name], implementation, raising=False)

    assert getattr(contracts, name)(first, second) is marker
    assert captured is not None
    assert captured[0] is first
    assert captured[1] is second


def test_validation_module_is_pure_and_has_no_facade_cycle() -> None:
    spec = importlib.util.find_spec(VALIDATION_MODULE)
    assert spec is not None
    assert spec.origin is not None
    source = Path(spec.origin).read_text(encoding="utf-8")
    assert len(source.splitlines()) <= 140
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert CONTRACT_MODULE not in imported_modules
    assert not any(
        module.endswith(".knowledge_contracts") for module in imported_modules
    )

    helper = importlib.import_module(VALIDATION_MODULE)
    assert callable(helper.required_text)
    assert callable(helper.optional_text)


def test_validation_modules_form_one_way_relative_import_dag() -> None:
    modules = (CONTRACT_MODULE, VALIDATION_MODULE)
    edges: set[tuple[str, str]] = set()
    for module_name in modules:
        spec = importlib.util.find_spec(module_name)
        assert spec is not None
        assert spec.origin is not None
        tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
        package = module_name.rpartition(".")[0]
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                imported = f"{package}.{node.module}" if node.level else node.module
                if imported in modules:
                    edges.add((module_name, imported))
            elif isinstance(node, ast.Import):
                edges.update(
                    (module_name, alias.name)
                    for alias in node.names
                    if alias.name in modules
                )
    assert edges == {(CONTRACT_MODULE, VALIDATION_MODULE)}


@pytest.mark.parametrize(
    "module_order",
    (
        (CONTRACT_MODULE, VALIDATION_MODULE),
        (VALIDATION_MODULE, CONTRACT_MODULE),
    ),
)
def test_validation_modules_support_both_cold_import_orders(
    module_order: tuple[str, str],
) -> None:
    repository = Path(contracts.__file__).resolve().parents[2]
    script = textwrap.dedent(
        f"""
        import importlib
        import inspect
        import pickle
        import sys

        sys.path.insert(0, {str(repository)!r})
        for module_name in {module_order!r}:
            importlib.import_module(module_name)
        facade = importlib.import_module({CONTRACT_MODULE!r})
        helper = importlib.import_module({VALIDATION_MODULE!r})
        signatures = {SIGNATURES!r}
        for name in {tuple(DELEGATES)!r}:
            seam = getattr(facade, name)
            assert str(inspect.signature(seam)) == signatures[name]
            assert seam.__module__ == {CONTRACT_MODULE!r}
            assert pickle.loads(pickle.dumps(seam, protocol=5)) is seam
        assert callable(helper.required_text)
        assert callable(helper.optional_text)
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
