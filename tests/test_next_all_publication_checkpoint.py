"""A new run checkpoints published heads, not unfinished producer work."""

from pathlib import Path
from dataclasses import replace
import json

import pytest

from neocortex.persistence import state_publication as publication
from neocortex.persistence.state_publication import (
    StateOwnerHead,
    StatePublicationCommitError,
    StatePublicationConflictError,
    StatePublicationError,
    begin_state_publication,
    read_state_publication_state,
    read_state_publications,
    record_state_publication,
    restart_state_publication_checkpoint,
    resume_state_publication,
)


def _legacy(state: Path):
    old = StateOwnerHead("semantic", 16, "a" * 64)
    for number in range(3):
        record_state_publication(
            state,
            operation="framework-all-semantic",
            owners=("semantic",),
            status="complete",
            idempotency_key=f"previous:{number}",
            owner_heads=(old,),
        )
    pending = begin_state_publication(
        state,
        operation="framework-all-semantic",
        owners=("semantic",),
        idempotency_key="unfinished",
        manifest_sha256="b" * 64,
    ).prepared
    # This is a fresh aggregate, not a comparison against the legacy max id.
    heads = (
        StateOwnerHead("semantic", 17, "c" * 64, 7),
        StateOwnerHead("code", 4, "d" * 64, 7),
    )
    return pending, heads


def _restart(state: Path, pending, heads, *, callback=None):
    return restart_state_publication_checkpoint(
        state,
        event_id=pending.event_id,
        expected_epoch=3,
        owner_heads=heads,
        verify_owner_heads=(lambda: heads) if callback is None else callback,
    )


def test_restart_has_no_unfenced_old_epoch_window(tmp_path: Path, monkeypatch):
    pending, heads = _legacy(tmp_path)
    journal = tmp_path / publication.STATE_PUBLICATION_JOURNAL_FILENAME
    original = journal.read_bytes()
    actual_replace = publication.os.replace
    observed = []

    def replacing(source, target):
        if Path(target) == journal:
            before = read_state_publication_state(tmp_path)
            assert before.status == "blocked" and before.epoch.epoch == 3
            assert journal.read_bytes() == original
            actual_replace(source, target)
            after = read_state_publication_state(tmp_path)
            assert after.status == "complete" and after.epoch.epoch == 4
            assert set(after.epoch.owner_heads) == set(heads)
            observed.append(after.epoch.event_id)
        else:
            actual_replace(source, target)

    monkeypatch.setattr(publication.os, "replace", replacing)
    result = _restart(tmp_path, pending, heads)
    assert observed == [result.event_id]
    assert journal.read_bytes().startswith(original)
    assert result.operation == "framework-all-restart-checkpoint"
    assert result.manifest_sha256 is None
    assert "interrupted_work_completed=false" in result.detail
    terminal = [
        event for event in read_state_publications(tmp_path)
        if event.idempotency_key == pending.idempotency_key and event.status == "failed"
    ]
    assert len(terminal) == 1 and "rollback_claimed=false" in terminal[0].detail
    before_replay = journal.read_bytes()
    assert _restart(tmp_path, pending, heads) == result
    assert journal.read_bytes() == before_replay
    assert not list(tmp_path.glob("*.sqlite3")), "the kernel never opens or builds an owner"


def test_restart_revalidates_heads_after_manifest_and_journal_staging(tmp_path: Path):
    pending, heads = _legacy(tmp_path)
    path = tmp_path / publication.STATE_PUBLICATION_JOURNAL_FILENAME
    original = path.read_bytes()
    calls = 0

    def observer():
        nonlocal calls
        calls += 1
        return heads if calls == 1 else (replace(heads[0], revision=18), heads[1])

    with pytest.raises(StatePublicationConflictError, match="heads changed"):
        _restart(tmp_path, pending, heads, callback=observer)
    assert calls == 2
    assert path.read_bytes() == original
    assert read_state_publication_state(tmp_path).status == "blocked"


def test_restart_rejects_journal_drift_at_effect_boundary(tmp_path: Path):
    pending, heads = _legacy(tmp_path)
    journal = tmp_path / publication.STATE_PUBLICATION_JOURNAL_FILENAME
    original = journal.read_bytes()
    calls = 0

    def observer():
        nonlocal calls
        calls += 1
        if calls == 2:
            with journal.open("ab") as stream:
                stream.write(b"\n")
        return heads

    with pytest.raises(StatePublicationConflictError, match="journal changed"):
        _restart(tmp_path, pending, heads, callback=observer)
    assert journal.read_bytes() == original + b"\n"
    assert read_state_publication_state(tmp_path).status == "blocked"


def test_restart_pointer_failure_falls_forward_without_duplicate_events(tmp_path: Path, monkeypatch):
    pending, heads = _legacy(tmp_path)
    pointer = tmp_path / publication.STATE_EPOCH_FILENAME
    old_pointer = pointer.read_bytes()
    actual_write = publication._atomic_write_json

    def write(path, value):
        if path == pointer:
            raise OSError("injected pointer write failure")
        actual_write(path, value)

    monkeypatch.setattr(publication, "_atomic_write_json", write)
    with pytest.raises(StatePublicationCommitError) as error:
        _restart(tmp_path, pending, heads)
    assert error.value.durable is True
    assert pointer.read_bytes() == old_pointer
    view = read_state_publication_state(tmp_path)
    assert view.status == "complete" and view.epoch.epoch == 4
    assert view.epoch.source == "journal"
    count = len(read_state_publications(tmp_path))
    assert _restart(tmp_path, pending, heads) == error.value.publication
    assert len(read_state_publications(tmp_path)) == count


def test_restart_rejects_tampered_previous_publication(tmp_path: Path):
    pending, heads = _legacy(tmp_path)
    previous = read_state_publication_state(tmp_path).publication
    assert previous is not None and previous.content_manifest_name is not None
    (tmp_path / previous.content_manifest_name).write_text("{}")
    with pytest.raises(StatePublicationError):
        _restart(tmp_path, pending, heads)
    assert read_state_publication_state(tmp_path).pending == (pending,)


def test_restart_refuses_owner_scope_reduction(tmp_path: Path):
    semantic = StateOwnerHead("semantic", 1, "a" * 64)
    code = StateOwnerHead("code", 1, "b" * 64)
    record_state_publication(
        tmp_path, operation="framework-all-semantic", owners=("semantic", "code"),
        status="complete", idempotency_key="previous", owner_heads=(semantic, code),
    )
    pending = begin_state_publication(
        tmp_path, operation="framework-all-semantic", owners=("semantic",),
        idempotency_key="new-work",
    ).prepared
    with pytest.raises(StatePublicationConflictError, match="owner scope"):
        restart_state_publication_checkpoint(
            tmp_path, event_id=pending.event_id, expected_epoch=1,
            owner_heads=(semantic,), verify_owner_heads=lambda: (semantic,),
        )


def test_superseded_producer_cannot_commit_late(tmp_path: Path):
    pending, heads = _legacy(tmp_path)
    old = resume_state_publication(
        tmp_path, event_id=pending.event_id, operation=pending.operation,
        owners=pending.owners, idempotency_key="unfinished",
        manifest_sha256="b" * 64, expected_epoch=3,
    )
    _restart(tmp_path, pending, heads)
    count = len(read_state_publications(tmp_path))
    with pytest.raises(StatePublicationConflictError):
        old.commit((heads[0],))
    assert len(read_state_publications(tmp_path)) == count


@pytest.mark.parametrize("tampered", [False, True])
def test_restart_replay_cannot_hide_a_later_publication(tmp_path: Path, tampered: bool):
    pending, heads = _legacy(tmp_path)
    _restart(tmp_path, pending, heads)
    later = record_state_publication(
        tmp_path, operation="framework-all-semantic", owners=("semantic", "code"),
        status="complete", idempotency_key="later", owner_heads=heads,
    )
    if tampered:
        assert later.content_manifest_name is not None
        (tmp_path / later.content_manifest_name).unlink()
    with pytest.raises(StatePublicationConflictError, match=r"no longer.*current"):
        _restart(tmp_path, pending, heads)
    assert read_state_publication_state(tmp_path).epoch.epoch == 5


@pytest.mark.parametrize("field", (
    "operation", "owners", "owner_heads", "manifest_sha256",
    "content_manifest_sha256", "content_manifest_name",
))
def test_restart_replay_rejects_same_event_pointer_contract_drift(tmp_path: Path, field: str):
    pending, heads = _legacy(tmp_path)
    _restart(tmp_path, pending, heads)
    pointer = tmp_path / publication.STATE_EPOCH_FILENAME
    payload = json.loads(pointer.read_text())
    drift = {
        "operation": "different-operation",
        "owners": ["semantic"],
        "owner_heads": [heads[0].as_payload()],
        "manifest_sha256": "f" * 64,
        "content_manifest_sha256": "f" * 64,
        "content_manifest_name": "content-publication-manifest.different.json",
    }
    payload[field] = drift[field]
    pointer.write_text(json.dumps(payload))
    journal = tmp_path / publication.STATE_PUBLICATION_JOURNAL_FILENAME
    before = journal.read_bytes()
    with pytest.raises(StatePublicationError, match="pointer disagrees"):
        _restart(tmp_path, pending, heads)
    assert journal.read_bytes() == before


@pytest.mark.parametrize("field", ["epoch", "owner_heads"])
def test_restart_rejects_malformed_pending_baseline(tmp_path: Path, field: str):
    pending, heads = _legacy(tmp_path)
    journal = tmp_path / publication.STATE_PUBLICATION_JOURNAL_FILENAME
    lines = journal.read_text().splitlines()
    last = json.loads(lines[-1])
    last[field] = 2 if field == "epoch" else [head.as_payload() for head in heads]
    lines[-1] = json.dumps(last)
    journal.write_text("\n".join(lines) + "\n")
    original = journal.read_bytes()
    with pytest.raises(StatePublicationConflictError):
        _restart(tmp_path, pending, heads)
    assert journal.read_bytes() == original


def test_restart_keeps_verified_empty_owner_explicit(tmp_path: Path):
    pending, _ = _legacy(tmp_path)
    empty = StateOwnerHead("semantic", 0, "e" * 64, 7)
    result = _restart(tmp_path, pending, (empty,))
    assert result.owners == ("semantic",)
    assert result.owner_heads == (empty,)
