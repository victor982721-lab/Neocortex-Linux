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

from _04_Nucleo_Operativo.knowledge_search import (
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
        KnowledgeCandidate: (
            "6E6E55E406EBFFBA58E2F888A1D1CD907F1B8DA7B6D394F1484C3DE7210AF873"
        ),
        RankingExecution: (
            "5382CB89ABDF9E9E1E043B4A6E418D1A1E5BE3DC2443E8000C15730FC1292368"
        ),
        KnowledgeSearchResult: (
            "E577900298AD8FDAAB3E0E67D71E9BF39C275298C409E4A23F969385F02A9C54"
        ),
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
