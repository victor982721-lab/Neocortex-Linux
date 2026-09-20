"""Pure file observations, detached from the inventory SQLite owner."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypedDict

from .domain.errors import FileChangedError
from .domain.fingerprint_observation import FingerprintObservation, FingerprintReadFailure
from .domain.models import FileSnapshot
from .fingerprinting import FULL_ALGORITHM


class _CheckpointOptions(TypedDict, total=False):
    checkpoint: Callable[[], None]


def observe_content_fingerprint(
    snapshot: FileSnapshot,
    algorithm: str,
    *,
    cached_evidence: tuple[bytes, bytes] | None = None,
    expected_ctime_ns: int | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> FingerprintObservation:
    """Observe one complete SHA-256 digest with an identity fence.

    Callers look up ``cached_evidence`` on the owner thread before dispatch.
    This function never opens SQLite and is safe to run in bounded workers.
    """

    from .fingerprinting import (
        fingerprint_change_version,
        full_fingerprint,
        require_fingerprint_change_version,
    )

    if algorithm != FULL_ALGORITHM:
        raise ValueError("deduplication requires the canonical full SHA-256 algorithm")
    full_reads = full_bytes = 0

    def observe_full(count: int) -> None:
        nonlocal full_bytes
        full_bytes += count

    try:
        if checkpoint is not None:
            checkpoint()
        ctime_ns = fingerprint_change_version(snapshot)
        if expected_ctime_ns is not None and ctime_ns != expected_ctime_ns:
            raise FileChangedError(f"file changed after candidate sampling: {snapshot.path}")
        options: _CheckpointOptions = {}
        if checkpoint is not None:
            options["checkpoint"] = checkpoint
        full_reads = 1
        digest = full_fingerprint(snapshot, read_observer=observe_full, **options)
        complete = digest
        cache_hit = cached_evidence is not None and cached_evidence == (digest, digest)
        require_fingerprint_change_version(snapshot, ctime_ns)
        if checkpoint is not None:
            checkpoint()
    except (OSError, FileChangedError) as exc:
        raise FingerprintReadFailure(
            str(exc), full_reads=full_reads, partial_reads=0,
            full_read_bytes=full_bytes, partial_read_bytes=0,
            validation_read_bytes=full_bytes if cached_evidence is not None else 0,
        ) from exc
    return FingerprintObservation(
        snapshot=snapshot, algorithm=algorithm, digest=digest, full_digest=complete,
        ctime_ns=ctime_ns, computed=True, cache_hit=cache_hit,
        full_reads=full_reads, partial_reads=0,
        full_read_bytes=full_bytes, partial_read_bytes=0,
        validation_read_bytes=full_bytes if cached_evidence is not None else 0,
    )
