"""Bounded content proofs for destructive artifact effects.

This module is deliberately separate from the full-content dedupe digest and
from the redlist metadata binding.  It observes at most eight deterministic
segments (128 KiB total) through an ``O_NOFOLLOW`` descriptor and binds those
bytes to the physical identity *and* ctime observed around the read.  A
metadata-only replacement that restores mtime therefore still fails the
proof's ctime fence.

The proof is evidence, not a policy decision.  Callers choose the family/rule
that authorized the bounded probe; KIO revalidates the same proof before and
after its private claim and immediately before the path-bound effect.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field as dataclass_field, replace
from typing import Mapping

from neocortex.deduplication import FileSnapshot
from neocortex.platform.policy import stat_birthtime_ns
from .artifact_read_lease import (
    ArtifactReadLease,
    ArtifactReadLeaseError,
    acquire_artifact_read_lease,
)


ARTIFACT_CONTENT_PROOF_SCHEMA = "neocortex.artifact-content-proof/v1"
ARTIFACT_CONTENT_PROOF_PREFIX = "artifact-content-v1:"
ARTIFACT_CONTENT_PROOF_MAX_BYTES = 128 * 1024
ARTIFACT_CONTENT_PROOF_MAX_SEGMENTS = 8
ARTIFACT_CONTENT_PROOF_SEGMENT_BYTES = ARTIFACT_CONTENT_PROOF_MAX_BYTES // ARTIFACT_CONTENT_PROOF_MAX_SEGMENTS


class ArtifactContentProofError(ValueError):
    """A bounded proof is malformed, unavailable, or no longer valid."""


class ArtifactContentProofChanged(ArtifactContentProofError):
    """The object or one of its bounded observations changed."""


@dataclass(frozen=True, slots=True)
class ArtifactContentSegment:
    offset: int
    length: int
    digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "offset": self.offset,
            "length": self.length,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class ArtifactContentProof:
    """Versioned bounded evidence for one regular corpus object."""

    family: str
    volume_id: int
    file_id: int
    birthtime_ns: int
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    nlink: int
    segments: tuple[ArtifactContentSegment, ...]
    binding: str
    # Live process capability; intentionally omitted from serialized evidence.
    lease: ArtifactReadLease | None = dataclass_field(default=None, repr=False, compare=False)

    @property
    def schema(self) -> str:
        return ARTIFACT_CONTENT_PROOF_SCHEMA

    @property
    def bytes_observed(self) -> int:
        return sum(segment.length for segment in self.segments)

    def _unsigned_payload(self) -> dict[str, object]:
        return {
            "schema": ARTIFACT_CONTENT_PROOF_SCHEMA,
            "family": self.family,
            "identity": {
                "volume_id": self.volume_id,
                "file_id": self.file_id,
                "birthtime_ns": self.birthtime_ns,
                "size": self.size,
                "mtime_ns": self.mtime_ns,
                "ctime_ns": self.ctime_ns,
                "mode": self.mode,
                "nlink": self.nlink,
            },
            "segments": tuple(segment.as_dict() for segment in self.segments),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._unsigned_payload(), "binding": self.binding}

    def as_json(self) -> str:
        return json.dumps(
            self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


def _canonical(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _binding(payload: Mapping[str, object]) -> str:
    return ARTIFACT_CONTENT_PROOF_PREFIX + hashlib.sha256(_canonical(payload)).hexdigest()


def _validate_family(family: object) -> str:
    if not isinstance(family, str) or not family or len(family) > 128:
        raise ArtifactContentProofError("artifact proof family is invalid")
    if "\x00" in family or any(not character.isprintable() for character in family):
        raise ArtifactContentProofError("artifact proof family contains unsafe characters")
    return family


def _segment_offsets(size: int) -> tuple[tuple[int, int], ...]:
    if type(size) is not int or size < 0:
        raise ArtifactContentProofError("artifact proof size is invalid")
    if size == 0:
        return ()
    length = min(ARTIFACT_CONTENT_PROOF_SEGMENT_BYTES, size)
    if size <= ARTIFACT_CONTENT_PROOF_MAX_BYTES:
        return tuple(
            (offset, min(length, size - offset))
            for offset in range(0, size, length)
        )[:ARTIFACT_CONTENT_PROOF_MAX_SEGMENTS]
    last_offset = size - length
    offsets: list[int] = []
    for index in range(ARTIFACT_CONTENT_PROOF_MAX_SEGMENTS):
        offset = (last_offset * index) // (ARTIFACT_CONTENT_PROOF_MAX_SEGMENTS - 1)
        if offset not in offsets:
            offsets.append(offset)
    return tuple((offset, length) for offset in offsets)


def _stat_payload(metadata: os.stat_result) -> dict[str, int]:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ArtifactContentProofError("artifact proof source is not a unique regular file")
    ctime_ns = getattr(metadata, "st_ctime_ns", None)
    if type(ctime_ns) is not int or ctime_ns < 0:
        raise ArtifactContentProofError("artifact proof source lacks ctime")
    return {
        "volume_id": int(metadata.st_dev),
        "file_id": int(metadata.st_ino),
        "birthtime_ns": int(stat_birthtime_ns(metadata)),
        "size": int(metadata.st_size),
        "mtime_ns": int(metadata.st_mtime_ns),
        "ctime_ns": ctime_ns,
        "mode": int(stat.S_IMODE(metadata.st_mode)),
        "nlink": int(metadata.st_nlink),
    }


def _same_stat(expected: Mapping[str, int], current: Mapping[str, int]) -> bool:
    return all(current.get(key) == expected.get(key) for key in expected)


def _same_stat_except_ctime(expected: Mapping[str, int], current: Mapping[str, int]) -> bool:
    return all(
        current.get(key) == expected.get(key)
        for key in expected
        if key != "ctime_ns"
    )


def _make_proof(
    family: str,
    metadata: Mapping[str, int],
    segments: tuple[ArtifactContentSegment, ...],
    *,
    lease: ArtifactReadLease | None = None,
) -> ArtifactContentProof:
    unsigned = {
        "schema": ARTIFACT_CONTENT_PROOF_SCHEMA,
        "family": family,
        "identity": dict(metadata),
        "segments": tuple(segment.as_dict() for segment in segments),
    }
    return ArtifactContentProof(
        family=family,
        **dict(metadata),
        segments=segments,
        binding=_binding(unsigned),
        lease=lease,
    )


def _read_segments(fd: int, segments: tuple[tuple[int, int], ...]) -> tuple[ArtifactContentSegment, ...]:
    observed: list[ArtifactContentSegment] = []
    for offset, length in segments:
        try:
            payload = os.pread(fd, length, offset)
        except OSError as exc:
            raise ArtifactContentProofError("artifact proof segment cannot be read") from exc
        if len(payload) != length:
            raise ArtifactContentProofChanged("artifact proof source truncated during read")
        observed.append(
            ArtifactContentSegment(offset, length, hashlib.sha256(payload).hexdigest())
        )
    return tuple(observed)


def _expected_snapshot_matches(snapshot: FileSnapshot, metadata: Mapping[str, int]) -> bool:
    return (
        snapshot.volume_id == metadata["volume_id"]
        and snapshot.file_id == metadata["file_id"]
        and snapshot.size == metadata["size"]
        and snapshot.mtime_ns == metadata["mtime_ns"]
        and snapshot.birthtime_ns == metadata["birthtime_ns"]
    )


def capture_artifact_content_proof(
    source: str | os.PathLike[str] | FileSnapshot,
    *,
    family: str = "artifact",
) -> ArtifactContentProof:
    """Capture bounded bytes and ctime under one no-follow descriptor."""

    family = _validate_family(family)
    expected_snapshot = source if isinstance(source, FileSnapshot) else None
    path = source.path if expected_snapshot is not None else os.fspath(source)
    try:
        lease = acquire_artifact_read_lease(path)
    except (ArtifactReadLeaseError, OSError) as exc:
        raise ArtifactContentProofError("artifact proof read lease is unavailable") from exc
    try:
        lease.check()
        before = _stat_payload(os.fstat(lease.fd))
        if expected_snapshot is not None and not _expected_snapshot_matches(expected_snapshot, before):
            raise ArtifactContentProofChanged("artifact proof source differs from inventory")
        segments = _read_segments(lease.fd, _segment_offsets(before["size"]))
        lease.check()
        after = _stat_payload(os.fstat(lease.fd))
        if not _same_stat(before, after):
            raise ArtifactContentProofChanged("artifact proof source changed during capture")
    except BaseException:
        lease.close()
        raise
    return _make_proof(family, before, segments, lease=lease)


def proof_from_dict(value: object) -> ArtifactContentProof:
    if not isinstance(value, Mapping):
        raise ArtifactContentProofError("artifact content proof is not an object")
    if value.get("schema") != ARTIFACT_CONTENT_PROOF_SCHEMA:
        raise ArtifactContentProofError("artifact content proof schema is unsupported")
    family = _validate_family(value.get("family"))
    identity = value.get("identity")
    raw_segments = value.get("segments")
    binding = value.get("binding")
    if not isinstance(identity, Mapping) or not isinstance(raw_segments, (list, tuple)):
        raise ArtifactContentProofError("artifact content proof fields are invalid")
    if not isinstance(binding, str) or not binding.startswith(ARTIFACT_CONTENT_PROOF_PREFIX):
        raise ArtifactContentProofError("artifact content proof binding is invalid")
    fields = ("volume_id", "file_id", "birthtime_ns", "size", "mtime_ns", "ctime_ns", "mode", "nlink")
    parsed: dict[str, int] = {}
    for field in fields:
        value_field = identity.get(field)
        if type(value_field) is not int or (
            value_field < 0 and not (field == "birthtime_ns" and value_field == -1)
        ):
            raise ArtifactContentProofError("artifact content proof identity is invalid")
        parsed[field] = value_field
    if parsed["size"] == 0 and raw_segments:
        raise ArtifactContentProofError("empty proof cannot contain segments")
    if len(raw_segments) > ARTIFACT_CONTENT_PROOF_MAX_SEGMENTS:
        raise ArtifactContentProofError("artifact content proof has too many segments")
    segments: list[ArtifactContentSegment] = []
    total = 0
    previous = -1
    for raw in raw_segments:
        if not isinstance(raw, Mapping):
            raise ArtifactContentProofError("artifact content proof segment is invalid")
        offset, length, digest = raw.get("offset"), raw.get("length"), raw.get("digest")
        if (
            type(offset) is not int
            or type(length) is not int
            or offset < 0
            or length <= 0
            or length > ARTIFACT_CONTENT_PROOF_SEGMENT_BYTES
            or offset <= previous
            or offset + length > parsed["size"]
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ArtifactContentProofError("artifact content proof segment bounds are invalid")
        previous = offset
        total += length
        segments.append(ArtifactContentSegment(offset, length, digest))
    if total > ARTIFACT_CONTENT_PROOF_MAX_BYTES:
        raise ArtifactContentProofError("artifact content proof exceeds its byte bound")
    unsigned = {
        "schema": ARTIFACT_CONTENT_PROOF_SCHEMA,
        "family": family,
        "identity": parsed,
        "segments": tuple(segment.as_dict() for segment in segments),
    }
    if _binding(unsigned) != binding:
        raise ArtifactContentProofError("artifact content proof binding does not verify")
    return ArtifactContentProof(family=family, **parsed, segments=tuple(segments), binding=binding)


def coerce_artifact_content_proof(value: object) -> ArtifactContentProof:
    if isinstance(value, ArtifactContentProof):
        # Validate the immutable object as well, so callers cannot construct a
        # forged proof by bypassing ``proof_from_dict``.
        checked = proof_from_dict(value.as_dict())
        return replace(checked, lease=value.lease)
    return proof_from_dict(value)


def _path_metadata(path: str | os.PathLike[str]) -> Mapping[str, int]:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ArtifactContentProofChanged("artifact proof path is unavailable") from exc
    return _stat_payload(metadata)


def _lease_matches_metadata(
    lease: ArtifactReadLease,
    metadata: Mapping[str, int],
) -> None:
    if (
        metadata.get("volume_id"),
        metadata.get("file_id"),
        metadata.get("birthtime_ns"),
    ) != lease.identity:
        raise ArtifactContentProofChanged("artifact proof path is not the leased object")


def _live_lease(proof: ArtifactContentProof) -> ArtifactReadLease:
    lease = proof.lease
    if lease is None:
        raise ArtifactContentProofChanged("artifact content proof lacks a live read lease")
    try:
        lease.check()
    except (ArtifactReadLeaseError, OSError) as exc:
        raise ArtifactContentProofChanged("artifact content proof read lease is not live") from exc
    return lease


def revalidate_artifact_content_proof(
    proof: ArtifactContentProof | Mapping[str, object],
    source: str | os.PathLike[str],
    *,
    expected: FileSnapshot | None = None,
    family: str | None = None,
) -> None:
    """Re-read the same bounded segments and identity fields from ``source``."""

    checked = coerce_artifact_content_proof(proof)
    lease = _live_lease(checked)
    if family is not None and checked.family != _validate_family(family):
        raise ArtifactContentProofChanged("artifact proof family changed")
    lease.check()
    before = _path_metadata(source)
    _lease_matches_metadata(lease, before)
    if expected is not None and not _expected_snapshot_matches(expected, before):
        raise ArtifactContentProofChanged("artifact proof source identity changed")
    expected_identity = {
        "volume_id": checked.volume_id,
        "file_id": checked.file_id,
        "birthtime_ns": checked.birthtime_ns,
        "size": checked.size,
        "mtime_ns": checked.mtime_ns,
        "ctime_ns": checked.ctime_ns,
        "mode": checked.mode,
        "nlink": checked.nlink,
    }
    leased_before = _stat_payload(lease.check())
    if not _same_stat(expected_identity, leased_before) or not _same_stat(expected_identity, before):
        raise ArtifactContentProofChanged("artifact proof metadata changed")
    observed = _read_segments(lease.fd, tuple((item.offset, item.length) for item in checked.segments))
    if observed != checked.segments:
        raise ArtifactContentProofChanged("artifact proof bytes changed")
    leased_after = _stat_payload(lease.check())
    after = _path_metadata(source)
    _lease_matches_metadata(lease, after)
    if not _same_stat(expected_identity, leased_after) or not _same_stat(expected_identity, after):
        raise ArtifactContentProofChanged("artifact proof source changed during revalidation")


def rebind_artifact_content_proof(
    proof: ArtifactContentProof | Mapping[str, object],
    source: str | os.PathLike[str],
    *,
    expected: FileSnapshot | None = None,
) -> ArtifactContentProof:
    """Rebind ctime after an identity-preserving claim/Trash rename.

    A rename legitimately changes inode ctime even though it does not change
    payload bytes.  This seam keeps the original identity/segment proof while
    recording the new ctime; any in-place content or metadata mutation still
    fails the non-ctime identity and segment checks.
    """

    checked = coerce_artifact_content_proof(proof)
    lease = _live_lease(checked)
    lease.check()
    before = _path_metadata(source)
    _lease_matches_metadata(lease, before)
    if expected is not None and not _expected_snapshot_matches(expected, before):
        raise ArtifactContentProofChanged("artifact proof source identity changed")
    expected_identity = {
        "volume_id": checked.volume_id,
        "file_id": checked.file_id,
        "birthtime_ns": checked.birthtime_ns,
        "size": checked.size,
        "mtime_ns": checked.mtime_ns,
        "ctime_ns": checked.ctime_ns,
        "mode": checked.mode,
        "nlink": checked.nlink,
    }
    leased_before = _stat_payload(lease.check())
    if not _same_stat_except_ctime(expected_identity, leased_before) or not _same_stat_except_ctime(expected_identity, before):
        raise ArtifactContentProofChanged("artifact proof identity changed during rebind")
    observed = _read_segments(lease.fd, tuple((item.offset, item.length) for item in checked.segments))
    if observed != checked.segments:
        raise ArtifactContentProofChanged("artifact proof bytes changed during rebind")
    leased_after = _stat_payload(lease.check())
    after = _path_metadata(source)
    _lease_matches_metadata(lease, after)
    if not _same_stat_except_ctime(leased_before, leased_after) or not _same_stat_except_ctime(before, after):
        raise ArtifactContentProofChanged("artifact proof source changed during rebind")
    return _make_proof(checked.family, leased_after, observed, lease=lease)


__all__ = [
    "ARTIFACT_CONTENT_PROOF_MAX_BYTES",
    "ARTIFACT_CONTENT_PROOF_MAX_SEGMENTS",
    "ARTIFACT_CONTENT_PROOF_SCHEMA",
    "ArtifactContentProof",
    "ArtifactContentProofChanged",
    "ArtifactContentProofError",
    "ArtifactContentSegment",
    "capture_artifact_content_proof",
    "coerce_artifact_content_proof",
    "proof_from_dict",
    "rebind_artifact_content_proof",
    "revalidate_artifact_content_proof",
]
