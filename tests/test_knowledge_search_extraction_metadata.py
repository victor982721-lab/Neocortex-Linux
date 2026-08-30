"""Descriptor provenance for the Knowledge Search data contracts."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_search_extraction_metadata.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

from types import FunctionType

from neocortex.knowledge.knowledge_search import (
    KnowledgeCandidate,
    KnowledgeSearchResult,
    RankingExecution,
)
# endregion [01]

# region [02] Implementación


PUBLIC_MODULE = "neocortex.knowledge.knowledge_search"
CONTRACT_MODULE = "neocortex.knowledge.knowledge_search_contracts"


def _module_of(value: object) -> str | None:
    if isinstance(value, property):
        value = value.fget
    return value.__module__ if isinstance(value, FunctionType) else None


def test_contract_method_and_descriptor_modules_are_stable() -> None:
    public_members = {
        KnowledgeCandidate: (
            "__init__",
            "__repr__",
            "__eq__",
            "__setattr__",
            "__delattr__",
            "__hash__",
            "__post_init__",
            "evidence_key",
        ),
        RankingExecution: (
            "__init__",
            "__repr__",
            "__eq__",
            "__setattr__",
            "__delattr__",
            "__hash__",
            "__post_init__",
            "to_dict",
        ),
        KnowledgeSearchResult: (
            "__init__",
            "__repr__",
            "__eq__",
            "__setattr__",
            "__delattr__",
            "__hash__",
            "__post_init__",
            "to_dict",
            "to_json",
        ),
    }
    dataclass_members = ("__replace__", "__getstate__", "__setstate__")

    for contract, names in public_members.items():
        assert {
            name: _module_of(vars(contract)[name]) for name in names
        } == dict.fromkeys(names, CONTRACT_MODULE)
        assert {
            name: _module_of(vars(contract)[name]) for name in dataclass_members
        } == dict.fromkeys(dataclass_members, "dataclasses")
# endregion [02]
