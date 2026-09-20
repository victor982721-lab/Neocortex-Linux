"""Typed outcomes and claims for the fail-closed KIO Trash adapter.

The effect implementation lives in :mod:`neocortex.safety.kio_trash`; this
module deliberately contains only immutable records and status values.  Keeping
these records separate makes the public result contract easy to inspect without
moving any filesystem or subprocess boundary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from neocortex.deduplication import FileSnapshot


KIO_CLAIM_SCHEMA = "neocortex.kio-claim/v1"


class KioTrashStatus(StrEnum):
    """Terminal classification returned by the prepared primitive."""

    BLOCKED = "blocked"
    RECOVERY_REQUIRED = "recovery_required"
    APPLIED = "applied"


class KioTrashUnavailable(RuntimeError):
    """The local environment cannot safely start the KIO client."""

    def __init__(self, reason: str, detail: str):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True, slots=True)
class KioTrashClaim:
    """One same-filesystem, no-replace claim made before invoking KIO.

    KIO itself accepts a path, not an open descriptor.  Native operation mode
    therefore moves the already validated source to a private sibling path by
    ``renameat2(RENAME_NOREPLACE)`` first.  The claim is never a copy and is
    never removed with ``unlink``; an uncertain claim is retained for recovery.
    """

    source_path: Path
    claim_path: Path
    claim_directory: Path
    snapshot: FileSnapshot

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": KIO_CLAIM_SCHEMA,
            "source_path": os.fspath(self.source_path),
            "claim_path": os.fspath(self.claim_path),
            "claim_directory": os.fspath(self.claim_directory),
            "volume_id": f"{self.snapshot.volume_id:x}",
            "file_id": f"{self.snapshot.file_id:x}",
            "size": self.snapshot.size,
            "mtime_ns": self.snapshot.mtime_ns,
            "birthtime_ns": self.snapshot.birthtime_ns,
        }


class KioTrashClaimUnavailable(KioTrashUnavailable):
    """A claim crossed the rename frontier but needs caller-owned recovery."""

    def __init__(self, reason: str, detail: str, claim: KioTrashClaim):
        self.claim = claim
        super().__init__(reason, detail)


@dataclass(frozen=True, slots=True)
class KioTrashPreflight:
    """Read-only environment evidence collected before starting KIO."""

    client: Path
    config_home: Path
    client_snapshot: FileSnapshot | None = None


@dataclass(frozen=True, slots=True)
class KioTrashVerification:
    """Post-effect evidence supplied by a caller-owned verifier."""

    source_absent: bool
    trash_evidence: str | None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class KioTrashReceipt:
    """Evidence for one verified, reversible path-bound KIO move."""

    source_path: str
    client_path: str
    trash_evidence: str
    volume_id: int
    file_id: int
    size: int
    mtime_ns: int
    birthtime_ns: int
    verified_ns: int
    backend: str = "kio"
    guarantee: str = "reversible_path_bound"
    operation: str = "trash"
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class KioTrashResult:
    """One explicit outcome; only ``APPLIED`` carries a receipt."""

    status: KioTrashStatus
    reason: str
    source_path: str
    detail: str | None = None
    client_path: str | None = None
    command: tuple[str, ...] = ()
    returncode: int | None = None
    receipt: KioTrashReceipt | None = None


@dataclass(frozen=True, slots=True)
class KioTrashBatchItem:
    """One source admitted to :func:`move_many_to_trash`.

    ``source_digest`` is optional for compatibility with small fixture
    callers.  The batch primitive computes it once when omitted, while the
    curation backend supplies its already-computed digest to avoid hashing a
    source twice.
    """

    source: str | os.PathLike[str]
    expected: FileSnapshot
    source_digest: str | None = None


@dataclass(frozen=True, slots=True)
class KioTrashBatchResult:
    """Per-element outcomes plus the shared process evidence for one batch."""

    outcomes: tuple[KioTrashResult, ...]
    command: tuple[str, ...] = ()
    returncode: int | None = None
    cancelled: bool = False

    @property
    def applied(self) -> int:
        """Number of elements with a verified reversible effect."""

        return sum(item.status is KioTrashStatus.APPLIED for item in self.outcomes)

    @property
    def recovery_required(self) -> int:
        """Number of elements whose physical result remains ambiguous."""

        return sum(item.status is KioTrashStatus.RECOVERY_REQUIRED for item in self.outcomes)


__all__ = [
    "KIO_CLAIM_SCHEMA",
    "KioTrashBatchItem",
    "KioTrashBatchResult",
    "KioTrashClaim",
    "KioTrashClaimUnavailable",
    "KioTrashPreflight",
    "KioTrashReceipt",
    "KioTrashResult",
    "KioTrashStatus",
    "KioTrashUnavailable",
    "KioTrashVerification",
]
