"""Candidate reduction and exact, non-destructive duplicate planning."""
# region [00] Contexto del módulo
# Module: canonical deduplication planner
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager

from ..domain.models import DedupPlan, FileSnapshot
from ..domain.fingerprint_observation import ExactComparisonObservation, FingerprintObservation, FingerprintReadFailure
from ..domain.errors import FileChangedError
from ..domain.evidence import KeeperPolicy
from ..fingerprinting import (
    FULL_ALGORITHM,
    files_equal_exact,
    snapshot_path,
    DEFAULT_IO_CHUNK_SIZE,
)
from ..inventory.index import DedupIndex
from .pipeline import (
    FINGERPRINT_WRITE_BATCH_SIZE as FINGERPRINT_WRITE_BATCH_SIZE,
    MAX_EXACT_HASH_COLLISION_SETS as MAX_EXACT_HASH_COLLISION_SETS,
    MAX_REDUNDANT_MEMBERS_PER_GROUP as MAX_REDUNDANT_MEMBERS_PER_GROUP,
    PLAN_GROUP_BATCH_SIZE as PLAN_GROUP_BATCH_SIZE,
    PlanningSession,
    FingerprintResult,
)
from neocortex.progress import ProgressCallback
# endregion [01]

# region [02] Implementación


class DedupPlanner:
    """Build content evidence while hashing only physical size-collision candidates."""

    def __init__(
        self,
        index: DedupIndex,
        *,
        keeper_policy: KeeperPolicy | None = None,
        keeper_validation: Callable[[], None] | None = None,
        resource_gate=None,
        cancellation=None,
        max_workers: int | None = None,
    ):
        self._index = index
        self._keeper_policy = keeper_policy or KeeperPolicy()
        if keeper_validation is not None and not callable(keeper_validation):
            raise TypeError("keeper_validation must be callable")
        self._keeper_validation = keeper_validation
        if max_workers is not None and (type(max_workers) is not int or max_workers < 1):
            raise ValueError("max_workers must be positive")
        if resource_gate is None:
            from neocortex.runtime.control.global_resources import resource_gate as current_gate
            resource_gate = current_gate("dedup")
        self._resource_gate = resource_gate
        self._cancellation = cancellation
        self._max_workers = max_workers
        self._scan_device: str | None = None

    def _checkpoint(self) -> None:
        if self._cancellation is not None:
            self._cancellation.checkpoint()
        if self._resource_gate is not None:
            from neocortex.runtime.control.global_resources import current_resource_grant
            grant = current_resource_grant()
            if grant is not None:
                grant.checkpoint()

    @contextmanager
    def _metadata_scope(self):
        if self._resource_gate is None:
            yield
            return
        from neocortex.runtime.control.global_resources import resource_grant_scope
        with self._resource_gate.admit(
            8 * 1024 * 1024, io_slots=1, io_device=self._scan_device, phase="dedup_metadata",
        ) as grant, resource_grant_scope(grant):
            yield grant

    def _fingerprint(self, snapshot: FileSnapshot) -> FingerprintObservation:
        algorithm = FULL_ALGORITHM
        if self._cancellation is not None:
            from ..content_observation import observe_content_fingerprint
            return observe_content_fingerprint(
                snapshot, algorithm,
                cached_evidence=self._index.fingerprint_cache_evidence(snapshot, algorithm),
                checkpoint=self._checkpoint,
            )
        observation = self._index.observe_fingerprint(snapshot, algorithm)
        assert observation is not None  # cached_only=False always observes content.
        return observation

    @contextmanager
    def _fingerprint_batch(
        self, snapshots: Iterable[FileSnapshot],
    ) -> Iterator[Iterator[FingerprintResult]]:
        from neocortex.runtime.control.elastic_workers import ImmediateResult, elastic_map
        from ..content_observation import observe_content_fingerprint

        algorithm = FULL_ALGORITHM

        def prepare(snapshot: FileSnapshot):
            # elastic_map invokes preparation and consumes results on this
            # owner thread. No SQLite connection crosses into a worker.
            if self._cancellation is not None:
                self._cancellation.checkpoint()
            try:
                evidence = self._index.fingerprint_cache_evidence(snapshot, algorithm)
                return snapshot, evidence, None
            except (OSError, FileChangedError) as exc:
                return ImmediateResult((snapshot, exc))

        def observe(task):
            snapshot, evidence, _version = task
            try:
                result = observe_content_fingerprint(
                    snapshot, algorithm, cached_evidence=evidence,
                    checkpoint=self._checkpoint,
                )
                return snapshot, result
            except (OSError, FileChangedError) as exc:
                return snapshot, exc

        buffer_size = DEFAULT_IO_CHUNK_SIZE
        with elastic_map(
            observe, snapshots, gate=self._resource_gate, prepare=prepare,
            estimated_bytes=lambda snapshot: min(snapshot.size, buffer_size) + 64 * 1024,
            max_workers=self._max_workers, native_threads=1,
            cancellation=self._cancellation, io_slots=1,
            io_device=lambda snapshot: str(snapshot.volume_id),
            phase="dedup_full",
        ) as results:
            yield results

    def _compare_exact(self, left: FileSnapshot, right: FileSnapshot) -> ExactComparisonObservation:
        read_bytes = 0

        def observe(count: int) -> None:
            nonlocal read_bytes
            read_bytes += count

        try:
            if self._resource_gate is None:
                if self._cancellation is None:
                    equal = files_equal_exact(left, right, read_observer=observe)
                else:
                    equal = files_equal_exact(left, right, read_observer=observe, checkpoint=self._checkpoint)
            else:
                from neocortex.runtime.control.global_resources import resource_grant_scope
                with self._resource_gate.admit(
                    2 * min(left.size, DEFAULT_IO_CHUNK_SIZE) + 64 * 1024,
                    io_slots=1, io_device=str(left.volume_id), phase="dedup_exact",
                ) as grant, resource_grant_scope(grant):
                    equal = files_equal_exact(left, right, read_observer=observe, checkpoint=self._checkpoint)
        except FileChangedError as exc:
            raise FingerprintReadFailure(str(exc), exact_comparison_bytes=read_bytes) from exc
        return ExactComparisonObservation(equal, read_bytes)

    def plan(
        self,
        scan_id: int,
        *,
        progress: ProgressCallback | None = None,
        preview_limit: int | None = 0,
        exact_compare: bool = True,
    ) -> DedupPlan:
        if preview_limit is not None and preview_limit < 0:
            raise ValueError("preview_limit cannot be negative")
        if self._resource_gate is not None:
            return self._plan_admitted(scan_id, progress=progress, preview_limit=preview_limit, exact_compare=exact_compare)
        from neocortex.runtime.control.global_resources import (
            CoordinatedMemoryGate, GlobalResourceCoordinator, GlobalResourceLimits,
            resource_gate, resource_scope,
        )
        shared = resource_gate("dedup")
        if shared is not None:
            self._resource_gate = shared
            try:
                return self._plan_admitted(scan_id, progress=progress, preview_limit=preview_limit, exact_compare=exact_compare)
            finally:
                self._resource_gate = None
        coordinator = GlobalResourceCoordinator(("dedup",), GlobalResourceLimits(), cancellation=self._cancellation)
        with resource_scope(coordinator):
            self._resource_gate = CoordinatedMemoryGate(coordinator, "dedup", cancellation=self._cancellation)
            try:
                return self._plan_admitted(scan_id, progress=progress, preview_limit=preview_limit, exact_compare=exact_compare)
            finally:
                self._resource_gate = None

    def _plan_admitted(
        self, scan_id: int, *, progress: ProgressCallback | None,
        preview_limit: int | None, exact_compare: bool,
    ) -> DedupPlan:
        self._checkpoint()
        scan_id = self._index.current_scan_id(scan_id)
        if self._resource_gate is not None:
            identity = self._index.scan_root_identity(scan_id)
            self._scan_device = None if identity is None else str(identity[0])
        self._index.begin_duplicate_plan(scan_id)
        return PlanningSession(
            self._index,
            scan_id,
            progress=progress,
            preview_limit=preview_limit,
            exact_compare=exact_compare,
            fingerprint=self._fingerprint,
            capture_snapshot=snapshot_path,
            exact_matcher=self._compare_exact,
            keeper_policy=self._keeper_policy,
            keeper_validation=self._keeper_validation,
            fingerprint_batch=(
                self._fingerprint_batch
                if self._resource_gate is not None
                and getattr(self._fingerprint, "__func__", None) is _DEFAULT_FINGERPRINT_PROVIDER
                else None
            ),
            checkpoint=self._checkpoint,
            metadata_scope=self._metadata_scope,
        ).run()


_DEFAULT_FINGERPRINT_PROVIDER = DedupPlanner._fingerprint

__all__ = ["DedupPlanner"]
