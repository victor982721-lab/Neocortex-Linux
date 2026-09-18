"""Candidate reduction and exact, non-destructive duplicate planning."""
# region [00] Contexto del módulo
# Module: canonical deduplication planner
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from collections.abc import Callable

from ..domain.models import DedupPlan, FileSnapshot
from ..domain.fingerprint_observation import ExactComparisonObservation, FingerprintObservation, FingerprintReadFailure
from ..domain.errors import FileChangedError
from ..domain.evidence import KeeperPolicy
from ..fingerprinting import (
    FULL_ALGORITHM,
    PARTIAL_ALGORITHM,
    files_equal_exact,
    snapshot_path,
)
from ..inventory.index import DedupIndex
from .pipeline import (
    DEFAULT_PARTIAL_THRESHOLD as DEFAULT_PARTIAL_THRESHOLD,
    FINGERPRINT_WRITE_BATCH_SIZE as FINGERPRINT_WRITE_BATCH_SIZE,
    MAX_EXACT_HASH_COLLISION_SETS as MAX_EXACT_HASH_COLLISION_SETS,
    MAX_REDUNDANT_MEMBERS_PER_GROUP as MAX_REDUNDANT_MEMBERS_PER_GROUP,
    PLAN_GROUP_BATCH_SIZE as PLAN_GROUP_BATCH_SIZE,
    PlanningSession,
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
        partial_threshold: int = DEFAULT_PARTIAL_THRESHOLD,
        keeper_policy: KeeperPolicy | None = None,
        keeper_validation: Callable[[], None] | None = None,
    ):
        if partial_threshold < 0:
            raise ValueError("partial_threshold cannot be negative")
        self._index = index
        self._partial_threshold = partial_threshold
        self._keeper_policy = keeper_policy or KeeperPolicy()
        if keeper_validation is not None and not callable(keeper_validation):
            raise TypeError("keeper_validation must be callable")
        self._keeper_validation = keeper_validation

    def _fingerprint(self, snapshot: FileSnapshot, *, partial: bool) -> FingerprintObservation:
        algorithm = PARTIAL_ALGORITHM if partial else FULL_ALGORITHM
        observation = self._index.observe_fingerprint(snapshot, algorithm)
        assert observation is not None  # cached_only=False always observes content.
        return observation

    @staticmethod
    def _compare_exact(left: FileSnapshot, right: FileSnapshot) -> ExactComparisonObservation:
        read_bytes = 0

        def observe(count: int) -> None:
            nonlocal read_bytes
            read_bytes += count

        try:
            equal = files_equal_exact(left, right, read_observer=observe)
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
        scan_id = self._index.current_scan_id(scan_id)
        self._index.begin_duplicate_plan(scan_id)
        return PlanningSession(
            self._index,
            scan_id,
            partial_threshold=self._partial_threshold,
            progress=progress,
            preview_limit=preview_limit,
            exact_compare=exact_compare,
            fingerprint=self._fingerprint,
            capture_snapshot=snapshot_path,
            exact_matcher=self._compare_exact,
            keeper_policy=self._keeper_policy,
            keeper_validation=self._keeper_validation,
        ).run()


__all__ = ["DedupPlanner"]
