"""Tests-first extraction contract for Knowledge payload builders."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_contract_payload_extraction.py
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
PAYLOAD_MODULE = "neocortex.knowledge.knowledge_contract_payloads"
PAYLOAD_METHODS = {
    ("KnowledgePhaseTiming", "to_dict"): "knowledge_phase_timing_payload",
    ("KnowledgeQueryTelemetry", "to_dict"): "knowledge_query_telemetry_payload",
    ("PhysicalIdentityRef", "to_dict"): "physical_identity_ref_payload",
    ("ResourceRef", "to_dict"): "resource_ref_payload",
    ("RevisionRef", "to_dict"): "revision_ref_payload",
    ("EvidenceRef", "to_dict"): "evidence_ref_payload",
    ("RankingSignal", "to_dict"): "ranking_signal_payload",
    ("KnowledgeHit", "to_dict"): "knowledge_hit_payload",
    ("PublicationHead", "to_dict"): "publication_head_payload",
    ("LogicalWatermark", "to_dict"): "logical_watermark_payload",
    ("ActiveModel", "to_dict"): "active_model_payload",
    ("OwnerSnapshot", "identity_dict"): "owner_snapshot_identity_payload",
    ("OwnerSnapshot", "to_dict"): "owner_snapshot_payload",
    ("KnowledgeSnapshot", "to_dict"): "knowledge_snapshot_payload",
    ("ContextPlanStepRef", "to_dict"): "context_plan_step_ref_payload",
    ("ContextPlanRef", "to_dict"): "context_plan_ref_payload",
    ("ContextGraphBudget", "to_dict"): "context_graph_budget_payload",
    ("ContextBudget", "to_dict"): "context_budget_payload",
    ("ContextEntityRef", "to_dict"): "context_entity_ref_payload",
    ("ContextContradictionRef", "to_dict"): "context_contradiction_ref_payload",
    ("ContextRelationRef", "to_dict"): "context_relation_ref_payload",
    ("ContextBundle", "to_dict"): "context_bundle_payload",
}


@pytest.mark.parametrize(
    ("name", "expected_signature"),
    (
        ("_base_payload", "(kind: 'str') -> 'dict[str, object]'"),
        (
            "_canonical_output",
            "(payload: 'Mapping[str, object]') -> 'str'",
        ),
    ),
)
def test_payload_facade_seams_remain_stable_thin_delegates(
    name: str,
    expected_signature: str,
) -> None:
    seam = getattr(contracts, name)
    assert str(inspect.signature(seam)) == expected_signature
    assert seam.__module__ == CONTRACT_MODULE
    assert seam.__qualname__ == name
    assert pickle.loads(pickle.dumps(seam, protocol=5)) is seam
    tree = ast.parse(textwrap.dedent(inspect.getsource(seam)))
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    assert len(function.body) == 1
    assert isinstance(function.body[0], ast.Return)


@pytest.mark.parametrize(
    ("class_name", "method_name", "helper_name"),
    tuple((*key, value) for key, value in PAYLOAD_METHODS.items()),
)
def test_payload_methods_are_thin_local_wrappers(
    class_name: str,
    method_name: str,
    helper_name: str,
) -> None:
    method = getattr(getattr(contracts, class_name), method_name)
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    assert len(function.body) == 1
    statement = function.body[0]
    assert isinstance(statement, ast.Return)
    assert isinstance(statement.value, ast.Call)
    assert isinstance(statement.value.func, ast.Attribute)
    assert statement.value.func.attr == helper_name
    assert isinstance(statement.value.func.value, ast.Name)
    assert statement.value.func.value.id == "_contract_payloads"


def test_payload_module_is_bounded_and_has_no_facade_cycle() -> None:
    spec = importlib.util.find_spec(PAYLOAD_MODULE)
    assert spec is not None
    assert spec.origin is not None
    source = Path(spec.origin).read_text(encoding="utf-8")
    assert len(source.splitlines()) <= 520
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert CONTRACT_MODULE not in imported_modules
    assert not any(
        module.endswith(".knowledge_contracts") for module in imported_modules
    )
    helper = importlib.import_module(PAYLOAD_MODULE)
    assert callable(helper.base_payload)
    assert callable(helper.canonical_output)
    for helper_name in PAYLOAD_METHODS.values():
        assert callable(getattr(helper, helper_name))


def test_payload_modules_form_one_way_relative_import_dag() -> None:
    modules = (CONTRACT_MODULE, PAYLOAD_MODULE)
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
    assert edges == {(CONTRACT_MODULE, PAYLOAD_MODULE)}


@pytest.mark.parametrize(
    "module_order",
    (
        (CONTRACT_MODULE, PAYLOAD_MODULE),
        (PAYLOAD_MODULE, CONTRACT_MODULE),
    ),
)
def test_payload_modules_support_both_cold_import_orders(
    module_order: tuple[str, str],
) -> None:
    repository = Path(contracts.__file__).resolve().parents[1]
    script = textwrap.dedent(
        f"""
        import importlib
        import pickle
        import sys

        sys.path.insert(0, {str(repository)!r})
        for module_name in {module_order!r}:
            importlib.import_module(module_name)
        facade = importlib.import_module({CONTRACT_MODULE!r})
        helper = importlib.import_module({PAYLOAD_MODULE!r})
        for name in ('_base_payload', '_canonical_output'):
            seam = getattr(facade, name)
            assert pickle.loads(pickle.dumps(seam, protocol=5)) is seam
        for helper_name in {tuple(PAYLOAD_METHODS.values())!r}:
            assert callable(getattr(helper, helper_name))
        print('ok')
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
