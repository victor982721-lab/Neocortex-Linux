"""Replaceable vector ranking contracts, without repository or citation authority.

The request embeds the complete existing query and its scan constraints. A
backend may decline only *before* scanning. Once started, drift/cancellation
must raise; returning an unavailable outcome after scanning violates this
contract. The semantic owner retains source fencing and citation hydration.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from .semantic_models import ExactSearchPage, ExactSearchQuery, SearchHit
from .semantic_search_order import exact_search_order


class VectorSearchContractError(RuntimeError):
    """A backend cannot substantiate a result for the requested snapshot."""


@dataclass(frozen=True, slots=True)
class VectorSearchRequest:
    query: ExactSearchQuery
    owner_path: Path
    snapshot_id: str
    after_ref_id: int = 0
    evidence_mode: bool = False
    text_scope: Literal["all", "content", "title"] = "all"
    diagnostic_item_ids: tuple[str, ...] = ()
    metric: Literal["cosine"] = "cosine"

    def __post_init__(self) -> None:
        if not isinstance(self.query, ExactSearchQuery) or not self.snapshot_id:
            raise ValueError("vector request requires an exact query and owner snapshot")
        if type(self.after_ref_id) is not int or self.after_ref_id < 0:
            raise ValueError("after_ref_id must be a nonnegative integer")
        if self.metric != "cosine" or self.text_scope not in {"all", "content", "title"}:
            raise ValueError("unsupported vector metric or text scope")
        if type(self.evidence_mode) is not bool:
            raise ValueError("evidence_mode must be boolean")


@dataclass(frozen=True, slots=True)
class VectorSearchBudget:
    limit: int = 20
    max_vectors: int = 50_000
    batch_size: int = 512

    def __post_init__(self) -> None:
        for name, maximum in (("limit", 10_000), ("max_vectors", 10_000_000), ("batch_size", 10_000)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")


@dataclass(frozen=True, slots=True)
class VectorSearchUnavailable:
    """Explicit pre-scan decline; no candidates, budget use, or retry authority."""

    backend_id: str
    reason: str
    phase: Literal["prescan"] = "prescan"


@dataclass(frozen=True, slots=True)
class VectorSearchPage:
    page: ExactSearchPage
    backend_id: str
    snapshot_id: str
    coverage: Literal["complete", "partial"]
    fallback_reason: str | None = None
    diagnostics: Mapping[str, object] = field(default_factory=dict)


class SemanticVectorSearch(Protocol):
    def search_page(
        self, request: VectorSearchRequest, budget: VectorSearchBudget,
        cancelled: Callable[[], None] | None = None,
    ) -> VectorSearchPage | VectorSearchUnavailable: ...

    def close(self) -> None: ...


def validate_vector_page(
    result: VectorSearchPage, request: VectorSearchRequest, budget: VectorSearchBudget,
) -> None:
    """Reject malformed/stale pages instead of silently falling back or sorting."""
    if not isinstance(result, VectorSearchPage) or not isinstance(result.page, ExactSearchPage):
        raise VectorSearchContractError("backend returned an invalid vector page")
    if not isinstance(result.backend_id, str) or not result.backend_id:
        raise VectorSearchContractError("backend identity is missing")
    if result.snapshot_id != request.snapshot_id:
        raise VectorSearchContractError("backend returned a different owner snapshot")
    page = result.page
    if (
        type(page.scanned) is not int or not 0 <= page.scanned <= budget.max_vectors
        or type(page.complete) is not bool or not isinstance(page.hits, tuple)
        or len(page.hits) > min(budget.limit, page.scanned)
        or result.coverage != ("complete" if page.complete else "partial")
        or (page.complete and page.next_cursor is not None)
        or (not page.complete and (
            type(page.next_cursor) is not int or page.next_cursor <= request.after_ref_id
        ))
    ):
        raise VectorSearchContractError("backend coverage or scan budget is inconsistent")
    seen: set[tuple[str, str]] = set()
    orders = []
    query = request.query
    for hit in page.hits:
        if not isinstance(hit, SearchHit) or (
            type(hit.ref_id) is not int or hit.ref_id <= request.after_ref_id
            or type(hit.generation_id) is not int or hit.generation_id < 1
            or not isinstance(hit.score, (int, float)) or not math.isfinite(hit.score)
            or not -1.0 <= hit.score <= 1.0
            or hit.vector_space != query.vector_space or hit.modality is not query.target_modality
            or hit.query_model_signature != query.query_model_signature
            or (query.indexed_model_signatures and hit.indexed_model_signature not in query.indexed_model_signatures)
            or any(not isinstance(value, str) or not value for value in (
                hit.item_id, hit.entity_id, hit.indexed_model_signature,
            ))
            or (page.next_cursor is not None and hit.ref_id > page.next_cursor)
        ):
            raise VectorSearchContractError("backend candidate violates the query boundary")
        key = (hit.item_id, hit.entity_id if request.evidence_mode else "")
        if key in seen:
            raise VectorSearchContractError("backend returned duplicate ranked groups")
        seen.add(key)
        orders.append(exact_search_order(
            hit.score, hit.item_id, hit.entity_id, hit.indexed_model_signature, hit.ref_id,
        ))
    if orders != sorted(orders):
        raise VectorSearchContractError("backend candidates violate the exact total order")
