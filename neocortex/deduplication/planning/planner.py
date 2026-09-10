"""Candidate reduction and exact, non-destructive duplicate planning."""
# region [00] Contexto del módulo
# Module: canonical deduplication planner
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from collections.abc import Callable
from typing import cast

from ..domain.models import DedupPlan, FileSnapshot
from ..domain.evidence import KeeperPolicy
from ..fingerprinting import (
    FULL_ALGORITHM,
    PARTIAL_ALGORITHM,
    files_equal_exact,
    full_fingerprint,
    partial_fingerprint,
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

    def _fingerprint(self, snapshot: FileSnapshot, *, partial: bool) -> tuple[bytes, bool]:
        algorithm = PARTIAL_ALGORITHM if partial else FULL_ALGORITHM
        validated_cache = getattr(self._index, "validated_cached_fingerprint", None)
        if callable(validated_cache):
            cached = cast(
                Callable[[FileSnapshot, str], bytes | None], validated_cache
            )(snapshot, algorithm)
        else:
            cached = self._index.cached_fingerprint(snapshot, algorithm)
        if cached is not None:
            return cached, False
        digest = partial_fingerprint(snapshot) if partial else full_fingerprint(snapshot)
        return digest, True

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
            exact_matcher=files_equal_exact,
            keeper_policy=self._keeper_policy,
            keeper_validation=self._keeper_validation,
        ).run()


__all__ = ["DedupPlanner"]
