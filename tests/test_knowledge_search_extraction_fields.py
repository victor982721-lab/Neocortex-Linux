"""Exact dataclass field and pickle metadata captured before CL3 extraction."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_search_extraction_fields.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import hashlib
import pickle
from dataclasses import fields, is_dataclass

from neocortex.knowledge.knowledge_search import (
    KnowledgeCandidate,
    KnowledgeSearchResult,
    RankingExecution,
)
# endregion [01]

# region [02] Implementación


def test_contract_field_topology_and_class_pickle_bytes_are_stable() -> None:
    expected = {
        KnowledgeCandidate: (
            "resource",
            "revision",
            "evidence",
            "signal",
            "reason",
            "confidence",
            "warnings",
        ),
        RankingExecution: (
            "name",
            "channel",
            "executed",
            "available",
            "complete",
            "returned",
            "rows_scanned",
            "vectors_scanned",
            "reason",
            "owner",
            "elapsed_ns",
            "result_window_full",
            "next_cursor",
            "cutoff_score",
        ),
        KnowledgeSearchResult: (
            "plan",
            "snapshot",
            "hits",
            "rankings",
            "complete",
            "truncated",
            "omitted_candidates",
            "rows_scanned",
            "vectors_scanned",
            "elapsed_milliseconds",
            "warnings",
            "telemetry",
            "blocking_owners",
            "result_window_full",
            "window_omitted_candidates",
        ),
    }
    pickle_sha256 = {
        KnowledgeCandidate: ("F0183B14AD707A7902120E0C9B9F37E53FDC600D88F33E37071858937CAF8763"),
        RankingExecution: ("86E57682056EF105EDC080D32A10F622CEF001A11633E904B97DF9C62DDE143A"),
        KnowledgeSearchResult: ("B6A5381B16DCE1E5D23794C0C299ED1BA20855A6B36F7797238D7586393FF9FC"),
    }

    for contract, names in expected.items():
        assert is_dataclass(contract)
        assert tuple(field.name for field in fields(contract)) == names
        assert contract.__slots__ == names
        assert contract.__match_args__ == names
        assert (
            hashlib.sha256(pickle.dumps(contract, protocol=5)).hexdigest().upper()
            == pickle_sha256[contract]
        )

    telemetry = KnowledgeSearchResult.__dataclass_fields__["telemetry"]
    assert telemetry.compare is False
    assert telemetry.repr is False


# endregion [02]
