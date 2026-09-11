"""Legacy recovery preserves a complete epoch rather than pretending rollback."""

from pathlib import Path
from dataclasses import replace
import json

import pytest

from neocortex.persistence.state_publication import (
    StateOwnerHead,
    StatePublicationConflictError,
    StatePublicationError,
    begin_state_publication,
    read_state_publication_state,
    read_state_publications,
    reconcile_unbound_state_publication,
    record_state_publication,
    resume_state_publication,
)


def _legacy(state: Path):
    head = StateOwnerHead("semantic", 16, "a" * 64)
    publication = None
    for epoch in range(3):
        publication = record_state_publication(
            state, operation="framework-all-semantic", owners=("semantic",),
            status="complete", idempotency_key=f"complete:{epoch}",
            owner_heads=(head,), manifest_sha256="b" * 64,
        )
    pending = begin_state_publication(
        state, operation="framework-all-semantic", owners=("semantic",),
        idempotency_key="legacy:pending", manifest_sha256="c" * 64,
    ).prepared
    assert publication is not None
    return head, publication, pending


def test_legacy_epoch_three_reconciliation_preserves_complete_publication(tmp_path: Path):
    head, complete, pending = _legacy(tmp_path)
    pointer = (tmp_path / "state-epoch.json").read_bytes()
    result = reconcile_unbound_state_publication(
        tmp_path, event_id=pending.event_id, expected_epoch=3,
        expected_publication_event_id=complete.event_id,
        verify_owner_heads=lambda: (head,),
    )
    assert result.status == "failed"
    assert "rollback_claimed=false" in (result.detail or "")
    assert result.owner_heads == ()
    assert (tmp_path / "state-epoch.json").read_bytes() == pointer
    view = read_state_publication_state(tmp_path)
    assert view.status == "complete"
    assert view.epoch.epoch == 3 and view.publication == complete
    count = len(read_state_publications(tmp_path))
    with pytest.raises(StatePublicationConflictError, match="one exact pending"):
        reconcile_unbound_state_publication(
            tmp_path, event_id=pending.event_id, expected_epoch=3,
            expected_publication_event_id=complete.event_id,
            verify_owner_heads=lambda: (head,),
        )
    assert len(read_state_publications(tmp_path)) == count


@pytest.mark.parametrize("observation", [(), (StateOwnerHead("semantic", 17, "d" * 64),)])
def test_legacy_reconciliation_rejects_missing_or_changed_owner_heads(tmp_path: Path, observation):
    _head, complete, pending = _legacy(tmp_path)
    before = (tmp_path / "state-publication-journal.jsonl").read_bytes()
    with pytest.raises(StatePublicationConflictError, match="fresh owner heads"):
        reconcile_unbound_state_publication(
            tmp_path, event_id=pending.event_id, expected_epoch=3,
            expected_publication_event_id=complete.event_id,
            verify_owner_heads=lambda: observation,
        )
    assert (tmp_path / "state-publication-journal.jsonl").read_bytes() == before
    assert read_state_publication_state(tmp_path).status == "blocked"


def test_legacy_reconciliation_authenticates_complete_manifest(tmp_path: Path):
    head, complete, pending = _legacy(tmp_path)
    assert complete.content_manifest_name is not None
    (tmp_path / complete.content_manifest_name).write_text("{}")
    with pytest.raises(StatePublicationError):
        reconcile_unbound_state_publication(
            tmp_path, event_id=pending.event_id, expected_epoch=3,
            expected_publication_event_id=complete.event_id,
            verify_owner_heads=lambda: (head,),
        )
    assert read_state_publication_state(tmp_path).status == "blocked"


def test_legacy_reconciliation_does_not_resolve_other_pending_events(tmp_path: Path):
    head, complete, pending = _legacy(tmp_path)
    # A second independently prepared operation makes the scope ambiguous.
    # Simulate an old/foreign malformed writer; the current writer correctly
    # rejects this shape before appending it.
    other = replace(pending, event_id="fixture:other", idempotency_key="other")
    with (tmp_path / "state-publication-journal.jsonl").open("a") as stream:
        stream.write(json.dumps(other.as_payload()) + "\n")
    with pytest.raises(StatePublicationConflictError, match="one exact pending"):
        reconcile_unbound_state_publication(
            tmp_path, event_id=pending.event_id, expected_epoch=3,
            expected_publication_event_id=complete.event_id,
            verify_owner_heads=lambda: (head,),
        )


def test_legacy_roll_forward_reuses_pending_key_without_claiming_rollback(tmp_path: Path):
    _head, _complete, pending = _legacy(tmp_path)
    before = (tmp_path / "state-publication-journal.jsonl").read_bytes()
    transaction = resume_state_publication(
        tmp_path, event_id=pending.event_id, operation="framework-all-semantic",
        owners=("semantic",), idempotency_key="legacy:pending",
        manifest_sha256="c" * 64, expected_epoch=3,
    )
    assert transaction.prepared == pending
    assert (tmp_path / "state-publication-journal.jsonl").read_bytes() == before
    assert read_state_publication_state(tmp_path).status == "blocked"
    complete = transaction.commit((StateOwnerHead("semantic", 17, "d" * 64),))
    assert complete.idempotency_key == pending.idempotency_key
    assert complete.event_id != pending.event_id
    view = read_state_publication_state(tmp_path)
    assert view.status == "complete" and view.epoch.epoch == 4


@pytest.mark.parametrize("change", ["key", "manifest", "epoch"])
def test_legacy_roll_forward_requires_exact_original_contract(tmp_path: Path, change: str):
    _head, _complete, pending = _legacy(tmp_path)
    before = (tmp_path / "state-publication-journal.jsonl").read_bytes()
    with pytest.raises(StatePublicationConflictError):
        resume_state_publication(
            tmp_path, event_id=pending.event_id, operation="framework-all-semantic",
            owners=("semantic",), idempotency_key="wrong" if change == "key" else "legacy:pending",
            manifest_sha256="d" * 64 if change == "manifest" else "c" * 64,
            expected_epoch=4 if change == "epoch" else 3,
        )
    assert (tmp_path / "state-publication-journal.jsonl").read_bytes() == before


def test_commit_revalidates_owner_heads_inside_publication_lock(tmp_path: Path):
    _head, _complete, pending = _legacy(tmp_path)
    transaction = resume_state_publication(
        tmp_path, event_id=pending.event_id, operation="framework-all-semantic",
        owners=("semantic",), idempotency_key="legacy:pending",
        manifest_sha256="c" * 64, expected_epoch=3,
    )
    expected = (StateOwnerHead("semantic", 17, "d" * 64),)
    changed = (StateOwnerHead("semantic", 18, "e" * 64),)
    before = (tmp_path / "state-publication-journal.jsonl").read_bytes()
    with pytest.raises(StatePublicationConflictError, match="changed before publication"):
        transaction.commit(expected, verify_owner_heads=lambda: changed)
    assert (tmp_path / "state-publication-journal.jsonl").read_bytes() == before
    assert read_state_publication_state(tmp_path).status == "blocked"
    result = transaction.commit(expected, verify_owner_heads=lambda: expected)
    assert result.epoch == 4


@pytest.mark.parametrize("prepare_again", (False, True))
def test_stale_transaction_cannot_commit_after_reconciliation(tmp_path: Path, prepare_again: bool):
    head, complete, pending = _legacy(tmp_path)
    transaction = resume_state_publication(
        tmp_path, event_id=pending.event_id, operation="framework-all-semantic",
        owners=("semantic",), idempotency_key="legacy:pending",
        manifest_sha256="c" * 64, expected_epoch=3,
    )
    reconcile_unbound_state_publication(
        tmp_path, event_id=pending.event_id, expected_epoch=3,
        expected_publication_event_id=complete.event_id, verify_owner_heads=lambda: (head,),
    )
    if prepare_again:
        begin_state_publication(
            tmp_path, operation="framework-all-semantic", owners=("semantic",),
            idempotency_key="legacy:pending", manifest_sha256="c" * 64,
        )
    before = (tmp_path / "state-publication-journal.jsonl").read_bytes()
    with pytest.raises(StatePublicationConflictError, match="prepared event changed"):
        transaction.commit((StateOwnerHead("semantic", 17, "d" * 64),))
    assert (tmp_path / "state-publication-journal.jsonl").read_bytes() == before
    assert read_state_publication_state(tmp_path).epoch.epoch == 3
