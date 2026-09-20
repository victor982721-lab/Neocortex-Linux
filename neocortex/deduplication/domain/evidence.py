"""Typed content evidence, separate from requested policy and action authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal
from unicodedata import category


DedupPolicy = Literal["legacy_unknown", "fast", "exact"]
PlanCoverage = Literal["legacy_unknown", "complete", "partial"]
ComparisonMethod = Literal["not_recorded", "sha256_full", "byte_for_byte"]
ComparisonResult = Literal["not_recorded", "reference", "fingerprint_match", "equal"]
PROOF_VERSION = "neocortex.duplicate-content-proof/v1"
KEEPER_POLICY_VERSION = "neocortex.duplicate-keeper/v1"


@dataclass(frozen=True, slots=True)
class KeeperPolicy:
    """Caller-supplied preferences, never permission to discard another path.

    References must have been verified by the supplying owner.  An empty tuple
    means no verified preference was supplied, not that references do not exist.
    Identity selectors describe physical inventory objects, not virtual members.
    """

    explicit_keep_identities: tuple[tuple[int, int], ...] = ()
    preferred_roots: tuple[str, ...] = ()
    verified_reference_identities: tuple[tuple[int, int], ...] = ()
    verified_reference_evidence: tuple[tuple[tuple[int, int], tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        for identities in (self.explicit_keep_identities, self.verified_reference_identities):
            if not isinstance(identities, tuple):
                raise TypeError("keeper identity preferences must be immutable tuples")
            for identity in identities:
                if (
                    not isinstance(identity, tuple)
                    or len(identity) != 2
                    or any(type(value) is not int or value < 0 for value in identity)
                ):
                    raise ValueError("keeper preferences require physical (volume_id, file_id)")
        if not isinstance(self.preferred_roots, tuple) or any(
            not isinstance(root, str) or not root or "\0" in root
            for root in self.preferred_roots
        ):
            raise ValueError("preferred roots must be an immutable tuple of nonempty paths")
        if not isinstance(self.verified_reference_evidence, tuple):
            raise TypeError("verified reference evidence must be an immutable tuple")
        verified = frozenset(self.verified_reference_identities)
        for entry in self.verified_reference_evidence:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError("verified reference evidence requires (identity, evidence_ids)")
            identity, evidence_ids = entry
            if (
                not isinstance(identity, tuple) or len(identity) != 2
                or any(type(value) is not int or value < 0 for value in identity)
                or identity not in verified
            ):
                raise ValueError("reference evidence identity must be a verified physical reference")
            if not isinstance(evidence_ids, tuple) or not evidence_ids:
                raise ValueError("verified reference evidence IDs must be a nonempty immutable tuple")
            for evidence_id in evidence_ids:
                if (
                    not isinstance(evidence_id, str) or not evidence_id.strip() or len(evidence_id) > 512
                    or any(category(character) in {"Cc", "Cf", "Cs"} for character in evidence_id)
                ):
                    raise ValueError("reference evidence IDs must contain 1-512 characters without controls")


@dataclass(frozen=True, slots=True)
class DuplicateMemberProof:
    proof_version: str = "legacy_unknown"
    comparison_method: ComparisonMethod = "not_recorded"
    comparison_result: ComparisonResult = "not_recorded"
    compared_to_identity: tuple[int, int] | None = None
    comparison_bytes: int | None = None
    fingerprint_algorithm: str | None = None
    fingerprint_source: Literal["not_recorded", "computed", "cached"] = "not_recorded"
    missing_checks: tuple[str, ...] = ("verification_not_recorded",)
    aliases: tuple[str, ...] = ()
    alias_count: int | None = None
    aliases_truncated: bool = False
    observed_link_count: int | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DuplicateGroupProof:
    proof_version: str = "legacy_unknown"
    requested_policy: DedupPolicy = "legacy_unknown"
    comparison_method: ComparisonMethod = "not_recorded"
    comparison_result: ComparisonResult = "not_recorded"
    missing_checks: tuple[str, ...] = ("verification_not_recorded",)
    keeper_policy_version: str = "legacy_unknown"
    keeper_reason: str = "not_recorded"
    keeper_factors: tuple[str, ...] = ()
    scope: Literal["physical_files"] = "physical_files"
    actionability: Literal["review_required"] = "review_required"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


__all__ = [
    "KEEPER_POLICY_VERSION",
    "PROOF_VERSION",
    "ComparisonMethod",
    "ComparisonResult",
    "DedupPolicy",
    "DuplicateGroupProof",
    "DuplicateMemberProof",
    "KeeperPolicy",
    "PlanCoverage",
]
