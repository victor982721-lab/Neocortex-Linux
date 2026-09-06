"""Deterministic keeper preference, independent from content equality."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..domain.evidence import KeeperPolicy
from ..domain.models import FileSnapshot


_COPY_SUFFIX = re.compile(r"(?:\s*\(\d+\)|[ _-](?:copy|copia)(?:[ _-]?\d+)?)$", re.IGNORECASE)
type KeeperRank = tuple[int, int, int, int, str, str]


def keeper_rank(snapshot: FileSnapshot, policy: KeeperPolicy) -> KeeperRank:
    """Rank lower first; mtime and birthtime are not document revisions."""

    path = os.path.abspath(snapshot.path)
    location = len(policy.preferred_roots)
    for ordinal, root in enumerate(policy.preferred_roots):
        preferred = os.path.abspath(root)
        if os.path.commonpath((path, preferred)) == preferred:
            location = ordinal
            break
    return (
        0 if snapshot.identity in policy.explicit_keep_identities else 1,
        location,
        0 if snapshot.identity in policy.verified_reference_identities else 1,
        int(_COPY_SUFFIX.search(Path(snapshot.path).stem) is not None),
        f"{snapshot.volume_id:032x}:{snapshot.file_id:032x}",
        snapshot.path,
    )


def keeper_reason(ranks: tuple[KeeperRank, ...]) -> str:
    labels = (
        "explicit_user_decision", "preferred_location", "verified_reference",
        "clean_name_tiebreak", "stable_identity", "stable_path",
    )
    for position, label in enumerate(labels):
        if len({rank[position] for rank in ranks}) > 1:
            return label
    return "stable_identity"


def keeper_factors(snapshot: FileSnapshot, policy: KeeperPolicy) -> tuple[str, ...]:
    rank = keeper_rank(snapshot, policy)
    factors = tuple(
        label for enabled, label in (
            (rank[0] == 0, "explicit_user_decision"),
            (rank[1] < len(policy.preferred_roots), "preferred_location"),
            (rank[2] == 0, "verified_reference"),
            (rank[3] == 0, "clean_name"),
        ) if enabled
    )
    if snapshot.identity not in policy.verified_reference_identities:
        return factors
    evidence_ids = tuple(dict.fromkeys(
        evidence_id
        for identity, values in policy.verified_reference_evidence if identity == snapshot.identity
        for evidence_id in values
    ))
    return factors + tuple(f"verified_reference_evidence:{evidence_id}" for evidence_id in evidence_ids)


__all__ = ["KeeperRank", "keeper_factors", "keeper_rank", "keeper_reason"]
