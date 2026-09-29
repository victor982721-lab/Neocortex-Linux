from __future__ import annotations

import os
from pathlib import Path

import pytest

from neocortex.safety.artifact_content_proof import (
    ARTIFACT_CONTENT_PROOF_MAX_BYTES,
    ARTIFACT_CONTENT_PROOF_MAX_SEGMENTS,
    ArtifactContentProofChanged,
    ArtifactContentProofError,
    capture_artifact_content_proof,
    proof_from_dict,
    revalidate_artifact_content_proof,
)
from neocortex.safety.kio_trash import (
    KioTrashUnavailable,
    _claim_source,
    _restore_claim,
)
from neocortex.deduplication import snapshot_path


def test_proof_is_bounded_versioned_and_round_trips(tmp_path: Path) -> None:
    source = tmp_path / "runtime.bin"
    source.write_bytes(bytes(range(256)) * 1_000)

    proof = capture_artifact_content_proof(source, family="artifact.strong-magic.elf")

    assert proof.schema == "neocortex.artifact-content-proof/v1"
    assert proof.bytes_observed <= ARTIFACT_CONTENT_PROOF_MAX_BYTES
    assert len(proof.segments) <= ARTIFACT_CONTENT_PROOF_MAX_SEGMENTS
    assert proof_from_dict(proof.as_dict()) == proof
    revalidate_artifact_content_proof(proof, source)


def test_in_place_change_with_restored_mtime_fails_ctime_fence(tmp_path: Path) -> None:
    source = tmp_path / "cache.bin"
    source.write_bytes(b"A" * 256_000)
    proof = capture_artifact_content_proof(source, family="artifact.cache")
    original = source.stat()

    # A live proof lease deliberately blocks a regular writer.  Closing it
    # models a caller that lost continuity; revalidation must still abstain.
    assert proof.lease is not None
    proof.lease.close()
    source.write_bytes(b"B" * 256_000)
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))

    with pytest.raises(ArtifactContentProofChanged):
        revalidate_artifact_content_proof(proof, source)


def test_identity_replacement_is_rejected_even_when_size_and_mtime_match(tmp_path: Path) -> None:
    source = tmp_path / "payload.bin"
    source.write_bytes(b"A" * 4_096)
    proof = capture_artifact_content_proof(source)
    original = source.stat()

    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"A" * 4_096)
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    source.unlink()
    replacement.rename(source)

    with pytest.raises(ArtifactContentProofChanged):
        revalidate_artifact_content_proof(proof, source)


def test_tampered_serialized_proof_and_family_mismatch_fail(tmp_path: Path) -> None:
    source = tmp_path / "payload.bin"
    source.write_bytes(b"payload")
    proof = capture_artifact_content_proof(source, family="artifact.family-a")
    tampered = proof.as_dict()
    tampered["family"] = "artifact.family-b"
    with pytest.raises(ArtifactContentProofError):
        proof_from_dict(tampered)
    with pytest.raises(ArtifactContentProofChanged):
        revalidate_artifact_content_proof(proof, source, family="artifact.family-b")


def test_symlink_is_never_followed(tmp_path: Path) -> None:
    target = tmp_path / "target.bin"
    target.write_bytes(b"secret")
    link = tmp_path / "link.bin"
    link.symlink_to(target)

    with pytest.raises(ArtifactContentProofError):
        capture_artifact_content_proof(link)


def test_existing_writer_descriptor_denies_lease_acquisition(tmp_path: Path) -> None:
    source = tmp_path / "writer-held.bin"
    source.write_bytes(b"writer")
    descriptor = os.open(source, os.O_RDWR | os.O_CLOEXEC)
    try:
        with pytest.raises(ArtifactContentProofError, match="read lease"):
            capture_artifact_content_proof(source)
    finally:
        os.close(descriptor)


def test_serialized_proof_has_no_live_effect_capability(tmp_path: Path) -> None:
    source = tmp_path / "serialized.bin"
    source.write_bytes(b"payload")
    proof = capture_artifact_content_proof(source)
    serialized = proof_from_dict(proof.as_dict())
    assert serialized.lease is None
    with pytest.raises(ArtifactContentProofChanged, match="live read lease"):
        revalidate_artifact_content_proof(serialized, source)


def test_private_kio_claim_revalidates_proof_and_restores_idempotently(tmp_path: Path) -> None:
    source = tmp_path / "claim.bin"
    source.write_bytes(b"claim payload")
    snapshot = snapshot_path(source)
    proof = capture_artifact_content_proof(snapshot, family="artifact.cache")

    claim = _claim_source(source, snapshot, content_proof=proof)
    assert not source.exists()
    assert claim.claim_path.exists()
    _restore_claim(claim)
    assert source.read_bytes() == b"claim payload"


def test_private_kio_claim_blocks_changed_content_before_rename(tmp_path: Path) -> None:
    source = tmp_path / "changed.bin"
    source.write_bytes(b"A" * 4_096)
    snapshot = snapshot_path(source)
    proof = capture_artifact_content_proof(snapshot, family="artifact.cache")
    with pytest.raises(BlockingIOError):
        os.open(source, os.O_WRONLY | os.O_NONBLOCK)

    with pytest.raises(KioTrashUnavailable, match="kio_content_proof_changed"):
        _claim_source(source, snapshot, content_proof=proof)
    assert source.exists()
