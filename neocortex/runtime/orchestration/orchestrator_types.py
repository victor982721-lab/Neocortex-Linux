"""Small data contracts shared by the orchestration slices.

The public orchestration surface remains :class:`FrameworkOrchestrator`; these
types live separately so the lifecycle, pipeline, and route modules can share
the same hand-off records without importing the coordinator back into a
partially initialized module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from neocortex.deduplication import DedupPlan
from neocortex.integrations.inventory.inventory_coordinator import PreparedInventory
from neocortex.runtime.control.global_resources import GlobalResourceSummary
from neocortex.runtime.models import ActionSummary
from neocortex.safety.corpus_access import CorpusAccessPolicy

if TYPE_CHECKING:
    from neocortex.capabilities.formats.image.route import ImageRouteSummary
    from neocortex.documents.document_organization import (
        OrganizationApplySummary,
        OrganizationPlanSummary,
    )


@dataclass(frozen=True, slots=True)
class InitialWork:
    """Outputs produced by the inventory, curation, and route pipeline."""

    inventory: PreparedInventory
    dedup_plan: DedupPlan
    actions: ActionSummary
    route_results: dict[str, object]
    image: ImageRouteSummary | None
    global_resources: GlobalResourceSummary | None
    organization_plan: OrganizationPlanSummary | None
    organization_apply: OrganizationApplySummary | None
    route_failures: dict[str, str] = field(default_factory=dict)
    maintenance: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InitialExecution:
    """Initial pipeline output plus its durable journal successor."""

    work: InitialWork
    journal_after: None


@dataclass(frozen=True, slots=True)
class RouteOnlySource:
    """Durable source information used by an isolated route continuation."""

    run_id: int
    scan_id: int
    route_input_sources: Mapping[str, str]
    candidate_backed_routes: tuple[str, ...]
    candidate_rows: int


@dataclass(frozen=True, slots=True)
class RouteOnlyExecution:
    """Results of a route-only continuation before result projection."""

    run_id: int
    source_run_id: int
    route_results: dict[str, object]
    global_resources: GlobalResourceSummary | None
    route_failures: dict[str, str] = field(default_factory=dict)


class RouteExecutionError(RuntimeError):
    """Aggregate failures from content routes after durable per-route writes."""

    def __init__(self, failures: Mapping[str, BaseException]):
        self.failures = dict(failures)
        detail = "; ".join(
            f"{name}={type(exc).__name__}: {exc}" for name, exc in self.failures.items()
        )
        super().__init__(f"one or more content routes failed: {detail}")


def _complete_root_identity(policy: CorpusAccessPolicy) -> tuple[int, int, int]:
    """Return a manifest identity only when all root fields are captured."""

    identity = (
        policy.root_device_id,
        policy.root_file_id,
        policy.root_birthtime_ns,
    )
    if any(type(value) is not int for value in identity):
        raise ValueError("corpus root identity is incomplete")
    return identity


__all__ = [
    "InitialExecution",
    "InitialWork",
    "RouteExecutionError",
    "RouteOnlyExecution",
    "RouteOnlySource",
    "_complete_root_identity",
]
