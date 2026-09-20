"""Compatibility characterization for the knowledge-plan-v2 planner facade."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_planner_characterization.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from neocortex.foundation.hash_compat import HASH_ALGORITHM_128
from neocortex.api.public import KnowledgePlan as PackageKnowledgePlan, KnowledgeQuery as PackageKnowledgeQuery, RetrievalMode as PackageRetrievalMode, plan_knowledge_query as package_plan_knowledge_query
from neocortex.knowledge import knowledge_planner
from neocortex.knowledge.knowledge_planner import (
    KnowledgePlan,
    KnowledgeQuery,
    RetrievalMode,
    RetrievalStep,
    plan_knowledge_query,
)
# endregion [01]

# region [02] Implementación


_EXPECTED_ALL = (
    "MAX_KNOWLEDGE_EXACT_TERMS",
    "MAX_KNOWLEDGE_QUERY_CHARS",
    "MAX_KNOWLEDGE_RESULTS",
    "MAX_KNOWLEDGE_VECTORS",
    "KnowledgePlan",
    "KnowledgeQuery",
    "RetrievalMode",
    "RetrievalStep",
    "plan_knowledge_query",
)

_EXPECTED_PUBLIC_SIGNATURES = {
    "KnowledgePlan": (
        "(plan_id: 'str', normalized_query: 'str', "
        "retrieval_mode: 'RetrievalMode', intents: 'tuple[str, ...]', "
        "exact_terms: 'tuple[str, ...]', source_kinds: 'tuple[str, ...]', "
        "formats: 'tuple[str, ...]', project: 'str | None', "
        "date_from: 'str | None', date_to: 'str | None', "
        "include_history: 'bool', limit: 'int', max_per_resource: 'int', "
        "min_section_distance: 'int', max_vectors: 'int', "
        "steps: 'tuple[RetrievalStep, ...]', "
        "notices: 'tuple[str, ...]' = ()) -> None"
    ),
    "KnowledgeQuery": (
        "(text: 'str', retrieval_mode: 'RetrievalMode' = "
        "<RetrievalMode.EVIDENCE: 'evidence'>, include_history: 'bool' = False, "
        "source_kinds: 'tuple[str, ...]' = (), formats: 'tuple[str, ...]' = (), "
        "project: 'str | None' = None, date_from: 'str | None' = None, "
        "date_to: 'str | None' = None, limit: 'int' = 20, "
        "max_per_resource: 'int' = 3, min_section_distance: 'int' = 128, "
        "max_vectors: 'int' = 500000) -> None"
    ),
    "RetrievalMode": "(*values)",
    "RetrievalStep": (
        "(channel: 'str', ranking_name: 'str', reason: 'str', "
        "candidate_limit: 'int', required: 'bool' = False) -> None"
    ),
    "plan_knowledge_query": "(query: 'KnowledgeQuery') -> 'KnowledgePlan'",
}

_EXPECTED_PRIVATE_SIGNATURES = {
    "_canonical_retrieval_steps": (
        "(*, exact_terms: 'tuple[str, ...]', intents: 'tuple[str, ...]', "
        "source_kinds: 'tuple[str, ...]', formats: 'tuple[str, ...]', "
        "project: 'str | None', date_from: 'str | None', "
        "date_to: 'str | None', limit: 'int') -> 'tuple[RetrievalStep, ...]'"
    ),
    "_exact_terms": ("(text: 'str') -> 'tuple[tuple[str, ...], bool, bool, bool, str, str]'"),
    "_knowledge_plan_identifier": (
        "(*, normalized_query: 'str', retrieval_mode: 'RetrievalMode', "
        "intents: 'tuple[str, ...]', exact_terms: 'tuple[str, ...]', "
        "source_kinds: 'tuple[str, ...]', formats: 'tuple[str, ...]', "
        "project: 'str | None', date_from: 'str | None', "
        "date_to: 'str | None', include_history: 'bool', limit: 'int', "
        "max_per_resource: 'int', min_section_distance: 'int', "
        "max_vectors: 'int', steps: 'tuple[RetrievalStep, ...]', "
        "notices: 'tuple[str, ...]') -> 'str'"
    ),
    "_knowledge_plan_identity_payload": (
        "(*, normalized_query: 'str', retrieval_mode: 'RetrievalMode', "
        "intents: 'tuple[str, ...]', exact_terms: 'tuple[str, ...]', "
        "source_kinds: 'tuple[str, ...]', formats: 'tuple[str, ...]', "
        "project: 'str | None', date_from: 'str | None', "
        "date_to: 'str | None', include_history: 'bool', limit: 'int', "
        "max_per_resource: 'int', min_section_distance: 'int', "
        "max_vectors: 'int', steps: 'tuple[RetrievalStep, ...]', "
        "notices: 'tuple[str, ...]') -> 'dict[str, object]'"
    ),
    "_query_plan_signals": (
        "(query: 'KnowledgeQuery') -> 'tuple[tuple[str, ...], tuple[str, ...]]'"
    ),
    "_semantic_ranking_names": (
        "(source_kinds: 'tuple[str, ...]', formats: 'tuple[str, ...]') -> 'tuple[str, ...]'"
    ),
    "_validate_knowledge_plan_v2": "(plan: 'KnowledgePlan') -> 'None'",
}

_REASONS = {
    "catalog_metadata": "exact identifiers or explicit filters require owner metadata",
    "owner_fts": "exact lexical evidence is available from owner FTS indexes",
    "published_history": "query requests history, vigency or a temporal boundary",
    "semantic_image": "semantic image retrieval covers compatible visual evidence",
    "semantic_text": "semantic text retrieval covers compatible text and OCR evidence",
}

_CHANNELS = {
    "catalog_metadata": "catalog",
    "owner_fts": "lexical",
    "published_history": "temporal",
    "semantic_image": "semantic",
    "semantic_text": "semantic",
}

_PLAN_CASES = (
    pytest.param(
        KnowledgeQuery("substation condition"),
        "knowledge-plan-v2:de5c6bc13ba785c45ad95f10cef4ea65",
        ("lexical", "semantic"),
        (
            ("owner_fts", 60, True),
            ("semantic_text", 60, True),
            ("semantic_image", 60, True),
        ),
        id="broad",
    ),
    pytest.param(
        KnowledgeQuery("breaker condition", source_kinds=("image",)),
        "knowledge-plan-v2:14e562c03470d4145cbfe5905de11f27",
        ("lexical", "semantic", "filtered"),
        (
            ("owner_fts", 60, False),
            ("semantic_text", 60, True),
            ("semantic_image", 60, True),
        ),
        id="image",
    ),
    pytest.param(
        KnowledgeQuery(
            "differential protection",
            source_kinds=("pdf", "image"),
        ),
        "knowledge-plan-v2:89ca096bb0b1fbc2ff2845328164e857",
        ("lexical", "semantic", "filtered"),
        (
            ("owner_fts", 60, True),
            ("semantic_text", 60, True),
            ("semantic_image", 60, True),
            ("catalog_metadata", 60, True),
        ),
        id="mixed",
    ),
    pytest.param(
        KnowledgeQuery("validation folio", source_kinds=("image_ocr",)),
        "knowledge-plan-v2:111d7ff879631fe61b1e181e03775fbf",
        ("lexical", "semantic", "filtered"),
        (("owner_fts", 60, False), ("semantic_text", 60, True)),
        id="ocr",
    ),
    pytest.param(
        KnowledgeQuery("protection procedure", source_kinds=("pdf",)),
        "knowledge-plan-v2:085dfca26333eb66f3aa6a8b8b632286",
        ("lexical", "semantic", "filtered"),
        (
            ("owner_fts", 60, True),
            ("semantic_text", 60, True),
            ("catalog_metadata", 60, True),
        ),
        id="text",
    ),
    pytest.param(
        KnowledgeQuery(
            "inspection completed in 2025",
            include_history=True,
            date_from="2024-01-01",
            date_to="2025-12-31",
            limit=4,
        ),
        "knowledge-plan-v2:af7c331b7d908e3e1224a78b83ed4562",
        ("lexical", "semantic", "filtered", "temporal"),
        (
            ("owner_fts", 12, True),
            ("semantic_text", 12, True),
            ("semantic_image", 12, True),
            ("catalog_metadata", 12, True),
            ("published_history", 12, True),
        ),
        id="temporal",
    ),
)


def _expected_step(
    ranking_name: str,
    candidate_limit: int,
    required: bool,
) -> dict[str, object]:
    return {
        "channel": _CHANNELS[ranking_name],
        "ranking_name": ranking_name,
        "reason": _REASONS[ranking_name],
        "candidate_limit": candidate_limit,
        "required": required,
    }


def _expected_payload(
    query: KnowledgeQuery,
    plan_id: str,
    intents: tuple[str, ...],
    step_specs: tuple[tuple[str, int, bool], ...],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": "knowledge_query_plan",
        "plan_id": plan_id,
        "normalized_query": query.text,
        "retrieval_mode": query.retrieval_mode.value,
        "intents": list(intents),
        "exact_terms": [],
        "source_kinds": list(query.source_kinds),
        "formats": list(query.formats),
        "include_history": query.include_history,
        "limit": query.limit,
        "max_per_resource": query.max_per_resource,
        "min_section_distance": query.min_section_distance,
        "max_vectors": query.max_vectors,
        "steps": [_expected_step(*spec) for spec in step_specs],
    }
    if query.project is not None:
        payload["project"] = query.project
    if query.date_from is not None:
        payload["date_from"] = query.date_from
    if query.date_to is not None:
        payload["date_to"] = query.date_to
    return payload


def test_public_surface_signatures_modules_and_lazy_identities_are_exact() -> None:
    assert knowledge_planner.__all__ == _EXPECTED_ALL
    assert {
        "MAX_KNOWLEDGE_EXACT_TERMS": knowledge_planner.MAX_KNOWLEDGE_EXACT_TERMS,
        "MAX_KNOWLEDGE_QUERY_CHARS": knowledge_planner.MAX_KNOWLEDGE_QUERY_CHARS,
        "MAX_KNOWLEDGE_RESULTS": knowledge_planner.MAX_KNOWLEDGE_RESULTS,
        "MAX_KNOWLEDGE_VECTORS": knowledge_planner.MAX_KNOWLEDGE_VECTORS,
    } == {
        "MAX_KNOWLEDGE_EXACT_TERMS": 64,
        "MAX_KNOWLEDGE_QUERY_CHARS": 4_096,
        "MAX_KNOWLEDGE_RESULTS": 1_000,
        "MAX_KNOWLEDGE_VECTORS": 10_000_000,
    }
    for name, expected_signature in _EXPECTED_PUBLIC_SIGNATURES.items():
        value = getattr(knowledge_planner, name)
        assert value.__module__ == knowledge_planner.__name__
        assert value.__qualname__ == name
        assert str(inspect.signature(value)) == expected_signature

    assert PackageKnowledgePlan is KnowledgePlan
    assert PackageKnowledgeQuery is KnowledgeQuery
    assert PackageRetrievalMode is RetrievalMode
    assert package_plan_knowledge_query is plan_knowledge_query


def test_private_compatibility_seam_signatures_and_modules_are_exact() -> None:
    for name, expected_signature in _EXPECTED_PRIVATE_SIGNATURES.items():
        value = getattr(knowledge_planner, name)
        assert value.__module__ == knowledge_planner.__name__
        assert value.__qualname__ == name
        assert str(inspect.signature(value)) == expected_signature


@pytest.mark.parametrize(
    ("query", "plan_id", "intents", "step_specs"),
    _PLAN_CASES,
)
def test_plan_v2_ids_topology_payload_and_json_are_exact(
    query: KnowledgeQuery,
    plan_id: str,
    intents: tuple[str, ...],
    step_specs: tuple[tuple[str, int, bool], ...],
) -> None:
    plan = plan_knowledge_query(query)
    if HASH_ALGORITHM_128 != "xxh3-128":
        plan_id = plan.plan_id
    expected = _expected_payload(query, plan_id, intents, step_specs)
    expected_json = json.dumps(
        expected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    assert plan.plan_id == plan_id
    assert plan.to_dict() == expected
    assert plan.to_json().encode("utf-8") == expected_json.encode("utf-8")


def test_public_validation_exception_types_and_messages_are_exact() -> None:
    with pytest.raises(ValueError) as blank_query:
        KnowledgeQuery(" ")
    assert type(blank_query.value) is ValueError
    assert str(blank_query.value) == "Knowledge query cannot be blank"

    with pytest.raises(ValueError) as untyped_mode:
        KnowledgeQuery("query", retrieval_mode="evidence")  # type: ignore[arg-type]
    assert type(untyped_mode.value) is ValueError
    assert str(untyped_mode.value) == ("retrieval_mode must be a RetrievalMode instance")

    with pytest.raises(ValueError) as blank_step:
        RetrievalStep("", "semantic_text", "fixture", 1)
    assert type(blank_step.value) is ValueError
    assert str(blank_step.value) == "retrieval step channel cannot be blank"

    plan = plan_knowledge_query(KnowledgeQuery("substation condition"))
    with pytest.raises(ValueError) as malformed_identifier:
        replace(plan, plan_id="knowledge-plan-v2:fixture")
    assert type(malformed_identifier.value) is ValueError
    assert str(malformed_identifier.value) == (
        "Knowledge plan v2 plan_id must contain a lowercase XXH3-128 digest"
    )

    with pytest.raises(ValueError) as stale_identifier:
        replace(plan, max_vectors=plan.max_vectors - 1)
    assert type(stale_identifier.value) is ValueError
    assert str(stale_identifier.value) == (
        "Knowledge plan v2 plan_id does not match its canonical payload"
    )


def test_cold_import_dag_is_bounded_and_runtime_owner_free() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    script = """
import importlib
import json
import sys

sys.path.insert(0, sys.argv[1])
before = set(sys.modules)
module = importlib.import_module("neocortex.knowledge.knowledge_planner")
assert module.plan_knowledge_query(module.KnowledgeQuery("cold import")).plan_id
internal = sorted(
    name
    for name in set(sys.modules) - before
    if name.startswith("neocortex") or name.startswith("neocortex.knowledge")
)
heavy = sorted(
    name
    for name in ("fitz", "numpy", "onnxruntime", "PIL", "torch")
    if name in sys.modules
)
native = sorted(name for name in ("sqlite3",) if name in sys.modules)
print(
    json.dumps(
        {"heavy": heavy, "internal": internal, "native": native},
        sort_keys=True,
    )
)
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(repository_root)],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    payload: dict[str, Any] = json.loads(completed.stdout)
    assert payload["heavy"] == []
    assert payload["native"] == []
    loaded = set(payload["internal"])
    assert {
        "neocortex",
        "neocortex.knowledge",
        "neocortex.knowledge.knowledge_contracts",
        "neocortex.knowledge.knowledge_planner",
    } <= loaded
    assert loaded <= {
        "neocortex",
        "neocortex.foundation",
        "neocortex.foundation.hash_compat",
        "neocortex.knowledge.knowledge_planner",
        "neocortex.deduplication",
        "neocortex.deduplication.domain",
        "neocortex.deduplication.domain.errors",
        "neocortex.deduplication.domain.evidence",
        "neocortex.deduplication.domain.models",
        "neocortex.knowledge.knowledge_contract_context",
        "neocortex.knowledge.knowledge_contract_payloads",
        "neocortex.knowledge.knowledge_contract_references",
        "neocortex.knowledge.knowledge_contract_snapshot",
        "neocortex.knowledge.knowledge_contract_telemetry",
        "neocortex.knowledge.knowledge_contract_validation",
        "neocortex.knowledge.knowledge_contracts",
        "neocortex.knowledge.knowledge_planner_exact",
        "neocortex.knowledge.knowledge_planner_intents",
        "neocortex.knowledge.knowledge_planner_steps",
        "neocortex.semantic",
        "neocortex.semantic.semantic_models",
        "neocortex.knowledge",
        "neocortex.platform",
        "neocortex.platform.policy",
        "neocortex.safety",
        "neocortex.safety.route_filters",
    }


def test_cold_evidence_dependency_has_exact_stdlib_imports_and_pure_initialization() -> None:
    source_path = Path(__file__).resolve().parents[1] / "neocortex/deduplication/domain/evidence.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    assert not any(isinstance(node, ast.Import) for node in ast.walk(tree))
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert all(node.level == 0 for node in imports)
    assert [(node.module, tuple(alias.name for alias in node.names)) for node in imports] == [
        ("__future__", ("annotations",)),
        ("dataclasses", ("asdict", "dataclass")),
        ("typing", ("Literal",)),
        ("unicodedata", ("category",)),
    ]
    assert all(not node.bases and not node.keywords for node in tree.body if isinstance(node, ast.ClassDef))

    class InitializationCalls(ast.NodeVisitor):
        def __init__(self) -> None:
            self.calls: list[ast.Call] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            # Bodies run only when called; decorators and defaults run at import.
            for expression in (*node.decorator_list, *node.args.defaults, *node.args.kw_defaults):
                if expression is not None:
                    self.visit(expression)

        def visit_Call(self, node: ast.Call) -> None:
            self.calls.append(node)
            self.generic_visit(node)

    initializer = InitializationCalls()
    initializer.visit(tree)
    assert [ast.unparse(call) for call in initializer.calls] == [
        "dataclass(frozen=True, slots=True)",
        "dataclass(frozen=True, slots=True)",
        "dataclass(frozen=True, slots=True)",
    ]


# endregion [02]
