"""Bounded, deterministic stages for non-destructive duplicate planning."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from itertools import islice
from typing import Protocol

from ..domain.errors import FileChangedError
from ..domain.models import DedupPlan, DuplicateGroup, FileSnapshot, PlanStatistics
from ..fingerprinting import FULL_ALGORITHM, PARTIAL_ALGORITHM
from ..inventory.index import DedupIndex
from neocortex.progress import ProgressCallback, ProgressEvent, emit_progress


# region [01] Planning contracts and bounded accumulators

DEFAULT_PARTIAL_THRESHOLD = 8 * 1024 * 1024
PLAN_GROUP_BATCH_SIZE = 256
FINGERPRINT_WRITE_BATCH_SIZE = 512
MAX_REDUNDANT_MEMBERS_PER_GROUP = 1024
MAX_EXACT_HASH_COLLISION_SETS = 128

type FingerprintRow = tuple[FileSnapshot, bytes, bool]
type SnapshotCapture = Callable[[str], FileSnapshot]
type ExactMatcher = Callable[[FileSnapshot, FileSnapshot], bool]


class FingerprintProvider(Protocol):
    def __call__(self, snapshot: FileSnapshot, *, partial: bool) -> tuple[bytes, bool]: ...


@dataclass(slots=True)
class _PlanCounters:
    size_candidates: int = 0
    partial_count: int = 0
    full_count: int = 0
    comparisons: int = 0
    failures: int = 0


class _PlanningProgress:
    def __init__(self, callback: ProgressCallback | None, initial_total: int) -> None:
        self._callback = callback
        self.completed = 0
        self.total = initial_total
        self._last_progress_at = 0.0

    def start(self) -> None:
        emit_progress(
            self._callback,
            ProgressEvent(
                "dedup",
                "verify",
                "Validando candidatos por contenido",
                0,
                self.total,
                "operaciones",
            ),
        )

    def extend(self, amount: int) -> None:
        self.total += amount

    def complete(self, description: str) -> None:
        self.completed += 1
        self.report(description)

    def report(self, description: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self.completed % 32 != 0 and now - self._last_progress_at < 0.1:
            return
        emit_progress(
            self._callback,
            ProgressEvent(
                "dedup",
                "verify",
                description,
                self.completed,
                self.total,
                "operaciones",
            ),
        )
        self._last_progress_at = now

    def finish(self) -> None:
        emit_progress(
            self._callback,
            ProgressEvent(
                "dedup",
                "verify",
                "Validación de duplicados completada",
                self.total,
                self.total,
                "operaciones",
                True,
            ),
        )


class _PlanAccumulator:
    """Persist plan groups in bounded batches while retaining exact totals."""

    def __init__(self, index: DedupIndex, scan_id: int) -> None:
        self._index = index
        self._scan_id = scan_id
        self._batch: list[DuplicateGroup] = []
        self.group_count = 0
        self.redundant_files = 0
        self.reclaimable_bytes = 0

    def store(self, digest: bytes, keep: FileSnapshot, redundant: list[FileSnapshot]) -> None:
        if not redundant:
            return
        group = DuplicateGroup(
            size=keep.size,
            keep=keep,
            redundant=tuple(redundant),
            full_fingerprint=digest.hex(),
        )
        self._batch.append(group)
        self.group_count += 1
        self.redundant_files += len(redundant)
        self.reclaimable_bytes += group.reclaimable_bytes
        if len(self._batch) >= PLAN_GROUP_BATCH_SIZE:
            self.flush()

    def flush(self) -> None:
        if not self._batch:
            return
        self._index.store_duplicate_groups(self._scan_id, self._batch)
        self._batch.clear()


def _store_fingerprints(index: DedupIndex, stage: str, batch: list[FingerprintRow]) -> None:
    if not batch:
        return
    index.store_planning_fingerprints(
        stage, ((snapshot, digest) for snapshot, digest, _computed in batch)
    )
    computed_rows = [(snapshot, digest) for snapshot, digest, computed in batch if computed]
    if computed_rows:
        algorithm = PARTIAL_ALGORITHM if stage == "partial" else FULL_ALGORITHM
        index.store_fingerprints(algorithm, computed_rows)
    batch.clear()


# region [02] Full-digest grouping and exact verification


class _CollisionGroupBuilder:
    """Separate hash collisions into bounded byte-identical member groups."""

    def __init__(
        self,
        accumulator: _PlanAccumulator,
        counters: _PlanCounters,
        work: _PlanningProgress,
        *,
        exact_compare: bool,
        exact_matcher: ExactMatcher,
    ) -> None:
        self._accumulator = accumulator
        self._counters = counters
        self._work = work
        self._exact_compare = exact_compare
        self._exact_matcher = exact_matcher
        self._active_digest: bytes | None = None
        self._representatives: list[FileSnapshot] = []
        self._redundant_chunks: list[list[FileSnapshot]] = []

    def add(self, digest: bytes, snapshot: FileSnapshot) -> None:
        if self._active_digest != digest:
            self.flush()
            self._active_digest = digest
            self._representatives = []
            self._redundant_chunks = []

        if not self._place_with_representative(snapshot):
            self._add_representative(snapshot)
            return
        self._flush_complete_chunks()

    def _place_with_representative(self, snapshot: FileSnapshot) -> bool:
        if not self._exact_compare and self._representatives:
            self._redundant_chunks[0].append(snapshot)
            return True
        for position, representative in enumerate(self._representatives):
            self._work.extend(1)
            self._work.report("Comparando contenido exacto", force=True)
            try:
                self._counters.comparisons += 1
                if self._exact_matcher(representative, snapshot):
                    self._redundant_chunks[position].append(snapshot)
                    return True
            except FileChangedError:
                self._counters.failures += 1
                return True
            finally:
                self._work.complete("Comparando contenido exacto")
        return False

    def _add_representative(self, snapshot: FileSnapshot) -> None:
        if self._exact_compare and len(self._representatives) >= MAX_EXACT_HASH_COLLISION_SETS:
            self._counters.failures += 1
            return
        self._representatives.append(snapshot)
        self._redundant_chunks.append([])

    def _flush_complete_chunks(self) -> None:
        assert self._active_digest is not None
        for position, redundant in enumerate(self._redundant_chunks):
            if len(redundant) < MAX_REDUNDANT_MEMBERS_PER_GROUP:
                continue
            self._accumulator.store(self._active_digest, self._representatives[position], redundant)
            self._redundant_chunks[position] = []

    def flush(self) -> None:
        if self._active_digest is None:
            return
        for keep, redundant in zip(self._representatives, self._redundant_chunks, strict=True):
            self._accumulator.store(self._active_digest, keep, redundant)
            redundant.clear()


# endregion


# region [03] Candidate, fingerprint, and plan lifecycle


class PlanningSession:
    """Execute the planner stages without changing duplicate policy."""

    def __init__(
        self,
        index: DedupIndex,
        scan_id: int,
        *,
        partial_threshold: int,
        progress: ProgressCallback | None,
        preview_limit: int | None,
        exact_compare: bool,
        fingerprint: FingerprintProvider,
        capture_snapshot: SnapshotCapture,
        exact_matcher: ExactMatcher,
    ) -> None:
        self._index = index
        self._scan_id = scan_id
        self._partial_threshold = partial_threshold
        self._preview_limit = preview_limit
        self._exact_compare = exact_compare
        self._fingerprint = fingerprint
        self._capture_snapshot = capture_snapshot
        self._exact_matcher = exact_matcher
        self._counters = _PlanCounters()
        self._work = _PlanningProgress(progress, index.size_candidate_file_count(scan_id))
        self._groups = _PlanAccumulator(index, scan_id)

    def run(self) -> DedupPlan:
        self._work.start()
        self._index.begin_planning_fingerprints()
        for size, _raw_count in self._index.size_collision_sizes(self._scan_id):
            self._plan_size(size)
        self._groups.flush()
        self._index.complete_duplicate_plan(
            self._scan_id,
            group_count=self._groups.group_count,
            redundant_files=self._groups.redundant_files,
            reclaimable_bytes=self._groups.reclaimable_bytes,
        )
        groups = self._materialize_groups()
        self._work.finish()
        return self._build_result(groups)

    def _plan_size(self, size: int) -> None:
        self._index.clear_planning_fingerprints()
        used_partial = size >= self._partial_threshold
        self._fingerprint_size_members(size, partial=used_partial)
        if used_partial:
            self._fingerprint_partial_collisions()
        self._group_full_collisions()

    def _fingerprint_size_members(self, size: int, *, partial: bool) -> None:
        stage = "partial" if partial else "full"
        batch: list[FingerprintRow] = []
        for recorded in self._index.snapshots_by_size(self._scan_id, size):
            try:
                snapshot = self._capture_snapshot(recorded.path)
                if not self._matches_recorded(snapshot, recorded):
                    self._counters.failures += 1
                    continue
                if not self._index.claim_planning_identity(snapshot):
                    continue
                self._counters.size_candidates += 1
                digest, computed = self._fingerprint(snapshot, partial=partial)
                self._count_fingerprint(partial=partial, computed=computed)
                batch.append((snapshot, digest, computed))
                if len(batch) >= FINGERPRINT_WRITE_BATCH_SIZE:
                    _store_fingerprints(self._index, stage, batch)
            except (OSError, FileChangedError):
                self._counters.failures += 1
            self._work.complete(
                "Calculando firmas parciales" if partial else "Calculando hashes completos"
            )
        _store_fingerprints(self._index, stage, batch)

    @staticmethod
    def _matches_recorded(snapshot: FileSnapshot, recorded: FileSnapshot) -> bool:
        return (
            snapshot.identity == recorded.identity
            and snapshot.size == recorded.size
            and snapshot.mtime_ns == recorded.mtime_ns
            and snapshot.birthtime_ns == recorded.birthtime_ns
        )

    def _count_fingerprint(self, *, partial: bool, computed: bool) -> None:
        if not computed:
            return
        if partial:
            self._counters.partial_count += 1
        else:
            self._counters.full_count += 1

    def _fingerprint_partial_collisions(self) -> None:
        full_candidates = self._index.planning_collision_member_count("partial")
        self._work.extend(full_candidates)
        self._work.report("Preparando hashes completos", force=True)
        batch: list[FingerprintRow] = []
        for _partial_digest, snapshot in self._index.iter_planning_collision_members("partial"):
            try:
                digest, computed = self._fingerprint(snapshot, partial=False)
                self._counters.full_count += computed
                batch.append((snapshot, digest, computed))
                if len(batch) >= FINGERPRINT_WRITE_BATCH_SIZE:
                    _store_fingerprints(self._index, "full", batch)
            except FileChangedError:
                self._counters.failures += 1
            self._work.complete("Calculando hashes completos")
        _store_fingerprints(self._index, "full", batch)

    def _group_full_collisions(self) -> None:
        builder = _CollisionGroupBuilder(
            self._groups,
            self._counters,
            self._work,
            exact_compare=self._exact_compare,
            exact_matcher=self._exact_matcher,
        )
        for digest, snapshot in self._index.iter_planning_collision_members("full"):
            builder.add(digest, snapshot)
        builder.flush()

    def _materialize_groups(self) -> tuple[DuplicateGroup, ...]:
        stored_groups = self._index.iter_duplicate_groups(self._scan_id)
        return (
            tuple(stored_groups)
            if self._preview_limit is None
            else tuple(islice(stored_groups, self._preview_limit))
        )

    def _build_result(self, groups: tuple[DuplicateGroup, ...]) -> DedupPlan:
        return DedupPlan(
            scan_id=self._scan_id,
            groups=groups,
            statistics=PlanStatistics(
                inventory_files=self._index.file_count(self._scan_id),
                size_candidate_files=self._counters.size_candidates,
                partial_hash_files=self._counters.partial_count,
                full_hash_files=self._counters.full_count,
                exact_compare_files=self._counters.comparisons,
                changed_or_unreadable_files=self._counters.failures,
            ),
            total_groups=self._groups.group_count,
            total_redundant_files=self._groups.redundant_files,
            total_reclaimable_bytes=self._groups.reclaimable_bytes,
        )


# endregion


__all__ = [
    "DEFAULT_PARTIAL_THRESHOLD",
    "FINGERPRINT_WRITE_BATCH_SIZE",
    "MAX_EXACT_HASH_COLLISION_SETS",
    "MAX_REDUNDANT_MEMBERS_PER_GROUP",
    "PLAN_GROUP_BATCH_SIZE",
    "FingerprintProvider",
    "PlanningSession",
]
