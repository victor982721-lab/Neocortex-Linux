"""Immutable domain values shared by inventory and duplicate planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .evidence import DedupPolicy, DuplicateGroupProof, DuplicateMemberProof, PlanCoverage


VerificationMode = Literal["legacy_unknown", "fast", "partial", "full_hash"]
VALID_VERIFICATION_MODES = frozenset(
    {"legacy_unknown", "fast", "partial", "full_hash"}
)


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    path: str
    volume_id: int
    file_id: int
    size: int
    mtime_ns: int
    birthtime_ns: int

    @property
    def identity(self) -> tuple[int, int]:
        return self.volume_id, self.file_id


@dataclass(frozen=True, slots=True)
class ScanSummary:
    scan_id: int
    root: str
    files_seen: int
    directories_seen: int
    bytes_seen: int
    skipped_links: int
    excluded_directories: int
    errors: int


@dataclass(frozen=True, slots=True)
class InventoryCheckpoint:
    """Policy-bound publication for one complete portable inventory scan."""

    root: str
    scan_id: int
    valid: bool = True
    inventory_policy_signature: str | None = None


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    """One content-equality candidate set; evidence does not authorize effects."""

    size: int
    keep: FileSnapshot
    redundant: tuple[FileSnapshot, ...]
    full_fingerprint: str
    verification_mode: VerificationMode = "legacy_unknown"
    proof: DuplicateGroupProof | None = None
    member_proofs: tuple[DuplicateMemberProof, ...] = ()

    @property
    def reclaimable_bytes(self) -> int:
        """Compatibility alias for nominal redundant content, not physical savings."""

        return self.nominal_redundant_bytes

    @property
    def nominal_redundant_bytes(self) -> int:
        return self.size * len(self.redundant)

    @property
    def physical_reclaimable_bytes(self) -> None:
        """Link topology, allocated extents and an authorized effect are not proven."""

        return None


@dataclass(frozen=True, slots=True)
class PlanStatistics:
    inventory_files: int
    size_candidate_files: int
    partial_hash_files: int
    full_hash_files: int
    exact_compare_files: int
    changed_or_unreadable_files: int
    hash_read_bytes: int = 0
    cache_validation_reads: int = 0
    cache_validation_bytes: int = 0
    fingerprint_cache_hits: int = 0
    full_digest_reuses: int = 0
    exact_comparison_bytes: int = 0


@dataclass(frozen=True, slots=True)
class DedupPlan:
    """Non-destructive physical-content plan with explicit verification coverage."""

    scan_id: int
    groups: tuple[DuplicateGroup, ...]
    statistics: PlanStatistics
    total_groups: int | None = None
    total_redundant_files: int | None = None
    total_reclaimable_bytes: int | None = None
    verification_mode: VerificationMode = "legacy_unknown"
    requested_policy: DedupPolicy = "legacy_unknown"
    coverage: PlanCoverage = "legacy_unknown"
    keeper_reference_status: str = "unverified"
    keeper_reference_reason: str | None = None
    keeper_reference_count: int = 0

    @property
    def group_count(self) -> int:
        return len(self.groups) if self.total_groups is None else self.total_groups

    @property
    def redundant_files(self) -> int:
        if self.total_redundant_files is not None:
            return self.total_redundant_files
        return sum(len(group.redundant) for group in self.groups)

    @property
    def reclaimable_bytes(self) -> int:
        if self.total_reclaimable_bytes is not None:
            return self.total_reclaimable_bytes
        return sum(group.reclaimable_bytes for group in self.groups)

    @property
    def nominal_redundant_bytes(self) -> int:
        return self.reclaimable_bytes

    @property
    def physical_reclaimable_bytes(self) -> None:
        return None


__all__ = [
    "VALID_VERIFICATION_MODES",
    "DedupPlan",
    "DuplicateGroup",
    "FileSnapshot",
    "InventoryCheckpoint",
    "PlanStatistics",
    "ScanSummary",
    "VerificationMode",
]
