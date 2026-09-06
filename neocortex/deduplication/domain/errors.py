"""Stable exception hierarchy for inventory and deduplication."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .evidence import KeeperPolicy


class DedupError(Exception):
    """Base package exception."""


class MissingDependencyError(DedupError):
    """A required optimized native dependency is unavailable."""


class FileChangedError(DedupError):
    """A file changed while it was being fingerprinted or compared."""


class InventoryError(DedupError):
    """An inventory operation could not be completed safely."""


class KeeperConflictError(InventoryError):
    """Equal content does not resolve conflicting explicit keeper decisions."""

    code = "conflicting_explicit_keepers"

    def __init__(
        self, *, policy: KeeperPolicy, identities: tuple[tuple[int, int], ...],
        full_fingerprint: str, exact_compare: bool,
    ) -> None:
        from .evidence import KEEPER_POLICY_VERSION, PROOF_VERSION, DuplicateGroupProof

        self.policy = policy
        self.identities = tuple(sorted(set(identities)))
        self.full_fingerprint = full_fingerprint
        missing = (
            "keeper_decision_conflict", "path_disposability_not_verified",
            "authorization_not_granted", "physical_reclamation_not_verified",
        )
        self.proof = DuplicateGroupProof(
            proof_version=PROOF_VERSION, requested_policy="exact" if exact_compare else "fast",
            comparison_method="byte_for_byte" if exact_compare else "full_xxh3",
            comparison_result="equal" if exact_compare else "fingerprint_match",
            missing_checks=missing if exact_compare else ("byte_for_byte_comparison", *missing),
            keeper_policy_version=KEEPER_POLICY_VERSION, keeper_reason=self.code,
        )
        super().__init__(
            f"{self.code}: {len(self.identities)} physical identities in the same content class "
            "were explicitly selected as keeper; no complete duplicate plan was published"
        )


__all__ = [
    "DedupError",
    "FileChangedError",
    "InventoryError",
    "KeeperConflictError",
    "MissingDependencyError",
]
