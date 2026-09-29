from __future__ import annotations

import os
from pathlib import Path

import pytest

from neocortex.deduplication import snapshot_path
from neocortex.safety.artifact_content_proof import capture_artifact_content_proof
from neocortex.safety.kio_trash import (
    KioTrashClaimUnavailable,
    _claim_source,
    _restore_claim,
)
from neocortex.workflow.actions.artifact_policy import ArtifactPolicy


def test_archive_policy_cannot_trash_from_a_signature_for_a_non_member(
    tmp_path: Path,
) -> None:
    """Caller-supplied archive context must not invent a runtime member."""

    archive = tmp_path / "docs.zip"
    decision = ArtifactPolicy().evaluate_archive_members(
        archive,
        ("sdk/readme.txt",),
        # This path is absent from ``members``.  It must not authorize a hold.
        member_signatures={"sdk/lib/libcore.so": "elf"},
    )

    assert decision.disposition == "keep"
    assert decision.rule_id == "keep.archive-not-demonstrable-runtime"


def test_private_claim_rejects_lease_break_before_claim_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer request between proof check and rename must not pass rebind."""

    source = tmp_path / "runtime.bin"
    source.write_bytes(b"A" * 256_000)
    snapshot = snapshot_path(source)
    proof = capture_artifact_content_proof(snapshot, family="artifact.strong-magic.elf")
    real_rename = __import__(
        "neocortex.safety.kio_trash",
        fromlist=["_renameat2_noreplace"],
    )._renameat2_noreplace

    def mutate_before_rename(*args, **kwargs):
        # A blocking writer would correctly wait for the lease-break response;
        # use O_NONBLOCK so this bounded fixture observes the break without
        # changing global signal handling or hanging the test process.
        with pytest.raises(BlockingIOError):
            os.open(source, os.O_WRONLY | os.O_NONBLOCK)
        return real_rename(*args, **kwargs)

    monkeypatch.setattr(
        "neocortex.safety.kio_trash._renameat2_noreplace",
        mutate_before_rename,
    )
    claim = None
    caught = None
    try:
        with pytest.raises(KioTrashClaimUnavailable) as raised:
            claim = _claim_source(source, snapshot, content_proof=proof)
        caught = raised.value
    finally:
        monkeypatch.undo()
        if claim is not None:
            _restore_claim(claim)
        elif caught is not None and caught.claim.lease is not None:
            # The lease-break request is itself recovery evidence.  Do not
            # force a restore after continuity was lost.
            caught.claim.lease.close()
