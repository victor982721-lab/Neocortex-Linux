"""Bounded, deterministic stages for non-destructive duplicate planning."""

from __future__ import annotations

import time
import os
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from itertools import islice
from typing import Protocol

from ..domain.errors import FileChangedError, KeeperConflictError
from ..domain.evidence import (
    KEEPER_POLICY_VERSION,
    PROOF_VERSION,
    DedupPolicy,
    DuplicateGroupProof,
    DuplicateMemberProof,
    KeeperPolicy,
    PlanCoverage,
)
from ..domain.models import (
    DedupPlan,
    DuplicateGroup,
    FileSnapshot,
    PlanStatistics,
    VerificationMode,
)
from ..domain.fingerprint_observation import ExactComparisonObservation, FingerprintObservation, FingerprintReadFailure
from ..fingerprinting import (
    FULL_ALGORITHM,
    PARTIAL_ALGORITHM,
    fingerprint_change_version,
    require_fingerprint_change_version,
    stat_matches_snapshot,
)
from ..inventory.index import DedupIndex
from ..inventory.repository_plans import PlanningMemberMetadata
from .keeper import KeeperRank, keeper_factors, keeper_rank, keeper_reason
from neocortex.progress import ProgressCallback, ProgressEvent, emit_progress


# region [01] Planning contracts and bounded accumulators

DEFAULT_PARTIAL_THRESHOLD = 8 * 1024 * 1024
PLAN_GROUP_BATCH_SIZE = 256
PLAN_MEMBER_BATCH_SIZE = 2048
PLAN_ALIAS_BATCH_SIZE = 4096
FINGERPRINT_WRITE_BATCH_SIZE = 512
MAX_REDUNDANT_MEMBERS_PER_GROUP = 1024
MAX_EXACT_HASH_COLLISION_SETS = 128

type FingerprintRow = tuple[FileSnapshot, bytes, bool, bytes | None, bool]
type SnapshotCapture = Callable[[str], FileSnapshot]
type ExactMatcher = Callable[[FileSnapshot, FileSnapshot], bool | ExactComparisonObservation]


class FingerprintProvider(Protocol):
    def __call__(self, snapshot: FileSnapshot, *, partial: bool) -> FingerprintObservation | tuple[bytes, bool]: ...


type FingerprintResult = tuple[FileSnapshot, FingerprintObservation | tuple[bytes, bool] | Exception]


class FingerprintBatchProvider(Protocol):
    def __call__(
        self, snapshots: Iterable[FileSnapshot], *, partial: bool, after_partial: bool = False,
    ) -> AbstractContextManager[Iterator[FingerprintResult]]: ...


@dataclass(slots=True)
class _PlanCounters:
    size_candidates: int = 0
    partial_count: int = 0
    full_count: int = 0
    comparisons: int = 0
    failures: int = 0
    hash_read_bytes: int = 0
    cache_validation_reads: int = 0
    cache_validation_bytes: int = 0
    cache_hits: int = 0
    full_digest_reuses: int = 0
    exact_comparison_bytes: int = 0


def _record_failed_reads(counters: _PlanCounters, exc: BaseException) -> None:
    if isinstance(exc, FingerprintReadFailure):
        counters.full_count += exc.full_reads
        counters.partial_count += exc.partial_reads
        counters.hash_read_bytes += exc.full_read_bytes + exc.partial_read_bytes
        counters.cache_validation_reads += int(exc.validation_read_bytes > 0)
        counters.cache_validation_bytes += exc.validation_read_bytes
        counters.exact_comparison_bytes += exc.exact_comparison_bytes


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

    def checkpoint(self, description: str) -> None:
        # Metadata does not increment completed. A completed multiple of 32
        # must not make every check publish another identical UI event.
        if self._callback is not None and time.monotonic() - self._last_progress_at >= 0.1:
            self.report(description, force=True)


class _PlanAccumulator:
    """Persist plan groups in bounded batches while retaining exact totals."""

    def __init__(
        self,
        index: DedupIndex,
        scan_id: int,
        *,
        exact_compare: bool,
        keeper_policy: KeeperPolicy,
        progress: _PlanningProgress,
    ) -> None:
        self._index = index
        self._scan_id = scan_id
        self._batch: list[DuplicateGroup] = []
        self._batch_member_count = 0
        self._batch_alias_count = 0
        self.group_count = 0
        self.redundant_files = 0
        self.reclaimable_bytes = 0
        self._exact_compare = exact_compare
        self._keeper_policy = keeper_policy
        self._work = progress

    def _member_proof(
        self, member: FileSnapshot, keep: FileSnapshot, metadata: PlanningMemberMetadata,
    ) -> DuplicateMemberProof:
        aliases, alias_count, links, computed = metadata
        reference = member.identity == keep.identity
        missing = []
        if not reference and not self._exact_compare:
            missing.append("byte_for_byte_comparison")
        if links > alias_count:
            missing.append("aliases_outside_inventory")
        return DuplicateMemberProof(
            proof_version=PROOF_VERSION,
            comparison_method="full_xxh3"
            if reference or not self._exact_compare
            else "byte_for_byte",
            comparison_result="reference"
            if reference
            else "equal"
            if self._exact_compare
            else "fingerprint_match",
            compared_to_identity=None if reference else keep.identity,
            comparison_bytes=member.size if self._exact_compare and not reference else None,
            fingerprint_algorithm=FULL_ALGORITHM,
            fingerprint_source="computed" if computed else "cached",
            missing_checks=tuple(missing),
            aliases=aliases,
            alias_count=alias_count,
            aliases_truncated=alias_count > len(aliases),
            observed_link_count=links,
        )

    def _metadata_checkpoint(self) -> None:
        self._work.checkpoint("Documentando evidencia de duplicados")

    def store(self, digest: bytes, keep: FileSnapshot, redundant: list[FileSnapshot]) -> None:
        if not redundant:
            return
        members = (keep, *redundant)
        explicit = frozenset(self._keeper_policy.explicit_keep_identities)
        selected = tuple(
            member.identity for member in members if member.identity in explicit
        )
        if len(set(selected)) > 1:
            raise KeeperConflictError(
                policy=self._keeper_policy,
                identities=selected,
                full_fingerprint=digest.hex(),
                exact_compare=self._exact_compare,
            )
        if self._batch_member_count + len(members) > PLAN_MEMBER_BATCH_SIZE:
            self.flush()
        ranks = tuple(keeper_rank(member, self._keeper_policy) for member in members)
        missing: tuple[str, ...] = (
            "path_disposability_not_verified",
            "authorization_not_granted",
            "physical_reclamation_not_verified",
        )
        if not self._exact_compare:
            missing = ("byte_for_byte_comparison", *missing)
        group = DuplicateGroup(
            size=keep.size,
            keep=keep,
            redundant=tuple(redundant),
            full_fingerprint=digest.hex(),
            verification_mode="full_hash" if self._exact_compare else "fast",
            proof=DuplicateGroupProof(
                proof_version=PROOF_VERSION,
                requested_policy="exact" if self._exact_compare else "fast",
                comparison_method="byte_for_byte" if self._exact_compare else "full_xxh3",
                comparison_result="equal" if self._exact_compare else "fingerprint_match",
                missing_checks=missing,
                keeper_policy_version=KEEPER_POLICY_VERSION,
                keeper_reason=keeper_reason(ranks),
                keeper_factors=keeper_factors(keep, self._keeper_policy),
            ),
            member_proofs=tuple(
                self._member_proof(member, keep, metadata)
                for member, metadata in zip(
                    members,
                    self._index.iter_planning_member_metadata(
                        members, checkpoint=self._metadata_checkpoint,
                    ),
                    strict=True,
                )
            ),
        )
        alias_count = sum(len(proof.aliases) for proof in group.member_proofs)
        if self._batch_alias_count + alias_count > PLAN_ALIAS_BATCH_SIZE:
            self.flush()
        self._batch.append(group)
        self._batch_member_count += len(members)
        self._batch_alias_count += alias_count
        self.group_count += 1
        self.redundant_files += len(redundant)
        self.reclaimable_bytes += group.reclaimable_bytes
        # A group keeps its complete proof even when its alias sample alone
        # exceeds the batch budget; persist that group immediately.
        if (
            len(self._batch) >= PLAN_GROUP_BATCH_SIZE
            or self._batch_member_count >= PLAN_MEMBER_BATCH_SIZE
            or self._batch_alias_count >= PLAN_ALIAS_BATCH_SIZE
        ):
            self.flush()

    def flush(self) -> None:
        if not self._batch:
            return
        self._index.store_duplicate_groups(self._scan_id, self._batch)
        self._batch.clear()
        self._batch_member_count = 0
        self._batch_alias_count = 0


def _store_fingerprints(index: DedupIndex, stage: str, batch: list[FingerprintRow]) -> None:
    if not batch:
        return
    index.store_planning_fingerprints(
        stage,
        ((snapshot, digest) for snapshot, digest, _computed, _content, _cache_hit in batch),
        computed_identities=frozenset(
            snapshot.identity
            for snapshot, _digest, computed, _content, _cache_hit in batch
            if computed
        ),
    )
    cache_updates = [
        (snapshot, digest)
        for snapshot, digest, computed, _content, cache_hit in batch
        if computed and not cache_hit and _content is not None
    ]
    if cache_updates:
        algorithm = PARTIAL_ALGORITHM if stage == "partial" else FULL_ALGORITHM
        content_digests = {
            snapshot.identity: content
            for snapshot, _digest, computed, content, cache_hit in batch
            if computed and not cache_hit and content is not None
        }
        index.store_fingerprints(
            algorithm,
            cache_updates,
            content_digests=content_digests,
        )
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

        placement = self._place_with_representative(snapshot)
        if placement is None:
            # A mutation makes the candidate ineligible for both roles.  Do
            # not turn a failed exact comparison into a new representative:
            # a later candidate must not be able to form a group around an
            # unverified snapshot.
            return
        if not placement:
            self._add_representative(snapshot)
            return
        self._flush_complete_chunks()

    def _place_with_representative(self, snapshot: FileSnapshot) -> bool | None:
        if not self._exact_compare and self._representatives:
            self._redundant_chunks[0].append(snapshot)
            return True
        for position, representative in enumerate(self._representatives):
            self._work.extend(1)
            self._work.report("Comparando contenido exacto", force=True)
            try:
                self._counters.comparisons += 1
                comparison = self._exact_matcher(representative, snapshot)
                if isinstance(comparison, ExactComparisonObservation):
                    self._counters.exact_comparison_bytes += comparison.read_bytes
                    equal = comparison.equal
                else:
                    equal = comparison
                if equal:
                    self._redundant_chunks[position].append(snapshot)
                    return True
            except FileChangedError as exc:
                _record_failed_reads(self._counters, exc)
                self._counters.failures += 1
                return None
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
        keeper_policy: KeeperPolicy | None = None,
        keeper_validation: Callable[[], None] | None = None,
        fingerprint_batch: FingerprintBatchProvider | None = None,
        checkpoint: Callable[[], None] | None = None,
        metadata_scope: Callable[[], AbstractContextManager[object]] | None = None,
    ) -> None:
        self._index = index
        self._scan_id = scan_id
        self._partial_threshold = partial_threshold
        self._preview_limit = preview_limit
        self._exact_compare = exact_compare
        self._fingerprint = fingerprint
        self._fingerprint_batch = fingerprint_batch
        self._checkpoint = checkpoint
        self._metadata_scope = metadata_scope
        self._capture_snapshot = capture_snapshot
        self._exact_matcher = exact_matcher
        self._keeper_policy = keeper_policy or KeeperPolicy()
        if keeper_validation is not None and not callable(keeper_validation):
            raise TypeError("keeper_validation must be callable")
        self._keeper_validation = keeper_validation
        self._counters = _PlanCounters()
        self._work = _PlanningProgress(progress, index.size_candidate_file_count(scan_id))
        self._groups = _PlanAccumulator(
            index,
            scan_id,
            exact_compare=exact_compare,
            keeper_policy=self._keeper_policy,
            progress=self._work,
        )

    def run(self) -> DedupPlan:
        if self._checkpoint is not None:
            self._checkpoint()
        self._work.start()
        self._index.begin_planning_fingerprints()
        for size, _raw_count in self._index.size_collision_sizes(self._scan_id):
            if self._checkpoint is not None:
                self._checkpoint()
            self._plan_size(size)
        self._groups.flush()
        if self._keeper_validation is not None:
            self._keeper_validation()
        self._index.complete_duplicate_plan(
            self._scan_id,
            group_count=self._groups.group_count,
            redundant_files=self._groups.redundant_files,
            reclaimable_bytes=self._groups.reclaimable_bytes,
            verification_mode=self._verification_mode(),
            requested_policy=self._requested_policy(),
            coverage=self._coverage(),
            exact_comparisons=self._counters.comparisons,
            changed_or_unreadable_files=self._counters.failures,
        )
        groups = self._materialize_groups()
        self._work.finish()
        return self._build_result(groups)

    def _verification_mode(self) -> VerificationMode:
        if self._counters.failures:
            return "partial"
        return "full_hash" if self._exact_compare else "fast"

    def _requested_policy(self) -> DedupPolicy:
        return "exact" if self._exact_compare else "fast"

    def _coverage(self) -> PlanCoverage:
        return "partial" if self._counters.failures else "complete"

    def _plan_size(self, size: int) -> None:
        self._index.clear_planning_fingerprints()
        used_partial = size >= self._partial_threshold
        if not self._fingerprint_size_members(size, partial=used_partial):
            return
        if used_partial:
            self._fingerprint_partial_collisions()
        self._group_full_collisions()

    def _fingerprint_size_members(self, size: int, *, partial: bool) -> bool:
        stage = "partial" if partial else "full"
        batch: list[FingerprintRow] = []
        full_observations: list[FingerprintObservation] = []
        with self._metadata_scope() if self._metadata_scope is not None else nullcontext():
            self._capture_size_members(size)
            if not self._index.planning_has_multiple_identities():
                return False
        with self._fingerprint_results(self._index.iter_planning_identities(), partial=partial) as results:
            for snapshot, result in results:
                self._work.extend(1)
                try:
                    self._counters.size_candidates += 1
                    if isinstance(result, Exception):
                        raise result
                    observation = self._accept_fingerprint(snapshot, result, partial=partial)
                    batch.append((snapshot, observation.digest, observation.computed, observation.full_digest, observation.cache_hit))
                    if partial:
                        full_observations.append(observation)
                    if len(batch) >= FINGERPRINT_WRITE_BATCH_SIZE:
                        _store_fingerprints(self._index, stage, batch)
                        self._index.store_planning_full_observations(full_observations)
                        full_observations.clear()
                except (OSError, FileChangedError) as exc:
                    _record_failed_reads(self._counters, exc)
                    self._counters.failures += 1
                self._work.complete(
                    "Calculando firmas parciales" if partial else "Calculando hashes completos"
                )
        _store_fingerprints(self._index, stage, batch)
        self._index.store_planning_full_observations(full_observations)
        return True

    def _capture_size_members(self, size: int) -> None:
        observations: list[tuple[FileSnapshot, KeeperRank, int]] = []
        for recorded in self._index.snapshots_by_size(self._scan_id, size):
            if self._checkpoint is not None:
                self._checkpoint()
            try:
                snapshot = self._capture_snapshot(recorded.path)
                if not self._matches_recorded(snapshot, recorded):
                    self._counters.failures += 1
                    continue
                current_stat = os.stat(snapshot.path, follow_symlinks=False)
                if not stat_matches_snapshot(snapshot, current_stat):
                    raise FileChangedError("file changed while capturing duplicate aliases")
                observations.append(
                    (snapshot, keeper_rank(snapshot, self._keeper_policy), current_stat.st_nlink)
                )
                if len(observations) >= FINGERPRINT_WRITE_BATCH_SIZE:
                    self._index.store_planning_observations(observations)
                    observations.clear()
            except (OSError, FileChangedError) as exc:
                _record_failed_reads(self._counters, exc)
                self._counters.failures += 1
            finally:
                self._work.complete("Validando identidades y alias físicos")
        self._index.store_planning_observations(observations)

    @staticmethod
    def _matches_recorded(snapshot: FileSnapshot, recorded: FileSnapshot) -> bool:
        return (
            snapshot.identity == recorded.identity
            and snapshot.size == recorded.size
            and snapshot.mtime_ns == recorded.mtime_ns
            and snapshot.birthtime_ns == recorded.birthtime_ns
        )

    @contextmanager
    def _fingerprint_results(
        self, snapshots: Iterable[FileSnapshot], *, partial: bool, after_partial: bool = False,
    ) -> Iterator[Iterator[FingerprintResult]]:
        if self._fingerprint_batch is not None:
            with self._fingerprint_batch(snapshots, partial=partial, after_partial=after_partial) as results:
                yield results
            return

        def serial() -> Iterator[FingerprintResult]:
            for snapshot in snapshots:
                if self._checkpoint is not None:
                    self._checkpoint()
                try:
                    observation: FingerprintObservation | tuple[bytes, bool] | None = (
                        self._index.planning_full_observation(snapshot) if after_partial else None
                    )
                    if observation is None:
                        observation = self._fingerprint(snapshot, partial=partial)
                        if after_partial:
                            version = self._index.planning_observed_change_version(snapshot)
                            if version is not None:
                                try:
                                    require_fingerprint_change_version(snapshot, version)
                                except FileChangedError as exc:
                                    if isinstance(observation, FingerprintObservation):
                                        raise FingerprintReadFailure(
                                            str(exc), full_reads=observation.full_reads,
                                            partial_reads=observation.partial_reads,
                                            full_read_bytes=observation.full_read_bytes,
                                            partial_read_bytes=observation.partial_read_bytes,
                                            validation_read_bytes=observation.validation_read_bytes,
                                        ) from exc
                                    raise
                    yield snapshot, observation
                except (OSError, FileChangedError) as exc:
                    yield snapshot, exc
        yield serial()

    def _accept_fingerprint(
        self, snapshot: FileSnapshot, result: FingerprintObservation | tuple[bytes, bool], *, partial: bool,
    ) -> FingerprintObservation:
        if not isinstance(result, FingerprintObservation):
            # Preserve the established injected-provider seam used for
            # adversarial hash-collision tests. Production returns evidence.
            digest, computed = result
            full = None if partial else digest
            result = FingerprintObservation(
                snapshot=snapshot, algorithm=PARTIAL_ALGORITHM if partial else FULL_ALGORITHM,
                digest=digest, full_digest=full, ctime_ns=fingerprint_change_version(snapshot),
                computed=computed, cache_hit=not computed and not partial,
                full_reads=int(not partial and computed),
                partial_reads=int(partial and computed), full_read_bytes=snapshot.size if not partial and computed else 0,
            )
        self._count_observation(result)
        return result

    def _count_observation(self, result: FingerprintObservation) -> None:
        self._counters.partial_count += result.partial_reads
        self._counters.full_count += result.full_reads
        self._counters.hash_read_bytes += result.full_read_bytes + result.partial_read_bytes
        self._counters.cache_validation_reads += int(result.validation_read_bytes > 0)
        self._counters.cache_validation_bytes += result.validation_read_bytes
        self._counters.cache_hits += result.cache_hit
        self._counters.full_digest_reuses += result.reused_full_digest

    def _fingerprint_partial_collisions(self) -> None:
        full_candidates = self._index.planning_collision_member_count("partial")
        self._work.extend(full_candidates)
        self._work.report("Preparando hashes completos", force=True)
        batch: list[FingerprintRow] = []
        snapshots = (snapshot for _digest, snapshot in self._index.iter_planning_collision_members("partial"))
        with self._fingerprint_results(snapshots, partial=False, after_partial=True) as results:
            for snapshot, result in results:
                try:
                    if isinstance(result, Exception):
                        raise result
                    observation = self._accept_fingerprint(snapshot, result, partial=False)
                    batch.append((snapshot, observation.digest, observation.computed, observation.full_digest, observation.cache_hit))
                    if len(batch) >= FINGERPRINT_WRITE_BATCH_SIZE:
                        _store_fingerprints(self._index, "full", batch)
                except (OSError, FileChangedError) as exc:
                    _record_failed_reads(self._counters, exc)
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
                hash_read_bytes=self._counters.hash_read_bytes,
                cache_validation_reads=self._counters.cache_validation_reads,
                cache_validation_bytes=self._counters.cache_validation_bytes,
                fingerprint_cache_hits=self._counters.cache_hits,
                full_digest_reuses=self._counters.full_digest_reuses,
                exact_comparison_bytes=self._counters.exact_comparison_bytes,
            ),
            total_groups=self._groups.group_count,
            total_redundant_files=self._groups.redundant_files,
            total_reclaimable_bytes=self._groups.reclaimable_bytes,
            verification_mode=self._verification_mode(),
            requested_policy=self._requested_policy(),
            coverage=self._coverage(),
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
