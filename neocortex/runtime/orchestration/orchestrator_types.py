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
    from _thread import LockType
    from collections.abc import Callable
    from pathlib import Path

    from neocortex.progress import ProgressCallback
    from neocortex.runtime.control.cancellation import CancellationToken
    from neocortex.runtime.control.global_resources import GlobalResourceCoordinator
    from neocortex.runtime.models import FrameworkConfig
    from neocortex.runtime.orchestration.route_registry import RouteAdapter
    from neocortex.capabilities.formats.image.route import ImageRouteSummary
    from neocortex.documents.document_organization import (
        OrganizationApplySummary,
        OrganizationPlanSummary,
    )


class _FrameworkOrchestratorOwner:
    """Type-only shared state surface for extracted orchestration mixins."""

    if TYPE_CHECKING:
        config: FrameworkConfig
        selected_routes: tuple[str, ...]
        route_registry: Mapping[str, RouteAdapter]
        progress: ProgressCallback
        _cancellation: CancellationToken
        _organization_resume_pending: bool
        _lifecycle_stage_runner: Callable[[int], object] | None
        _lifecycle_stage_details: Mapping[str, object]
        _active_coordinator: GlobalResourceCoordinator | None
        _coordinator_lock: LockType
        _active_run: tuple[Path, int] | None
        _unavailable_routes: dict[str, str]
        _resource_deadline: tuple[int, Mapping[str, object]] | None
        _SCRATCH_OWNER: str
        _SCRATCH_SCOPE: str
        _SCRATCH_REPORT_COUNT_LIMIT: int
        _SCRATCH_REPORT_BYTES_LIMIT: int


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
    # Aggregate, run-scoped admission evidence.  It deliberately remains a
    # mapping rather than a new persistence table or per-file status.
    size_admission: Mapping[str, object] = field(default_factory=dict)
    zip_intake: Mapping[str, object] = field(default_factory=dict)


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

    device_id = policy.root_device_id
    file_id = policy.root_file_id
    birthtime_ns = policy.root_birthtime_ns
    if (
        type(device_id) is not int
        or type(file_id) is not int
        or type(birthtime_ns) is not int
    ):
        raise ValueError("corpus root identity is incomplete")
    return device_id, file_id, birthtime_ns


__all__ = [
    "InitialExecution",
    "InitialWork",
    "RouteExecutionError",
    "RouteOnlyExecution",
    "RouteOnlySource",
    "_complete_root_identity",
]
