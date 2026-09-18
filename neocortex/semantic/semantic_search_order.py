"""One total order for every exact-search representative, top K and rank.

Ascending keys mean better hits: score descending; item, entity and model
ascending; ref_id descending only when the complete semantic identity ties.
The final ref_id preserves the existing preference for a newer identical
representative.  Scores have already been validated by the exact scorer.
This policy changes query ordering only, not persisted vectors or formats.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

ExactSearchOrder: TypeAlias = tuple[float, str, str, str, int]


def exact_search_order(
    score: float, item_id: str, entity_id: str, model_signature: str, ref_id: int,
) -> ExactSearchOrder:
    return -score, item_id, entity_id, model_signature, -ref_id


@dataclass(frozen=True, slots=True)
class ExactSearchHeapKey:
    """Reverse only the shared key so heap[0] is the worst retained hit."""

    order: ExactSearchOrder

    def __lt__(self, other: ExactSearchHeapKey) -> bool:
        return self.order > other.order
