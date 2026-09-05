"""Crash-safe, fail-closed cross-owner publication protocol tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

import neocortex.persistence.state_publication as publication
from neocortex.persistence.state_publication import (
    StateOwnerHead,
    StatePublicationConflictError,
    StatePublicationError,
    abort_state_publication,
    begin_state_publication,
    publication_idempotency_key,
    read_state_publication_state,
    read_state_publications,
    require_complete_state_epoch,
    record_state_publication,
)


def _head(owner: str, revision: int, digit: str) -> StateOwnerHead:
    return StateOwnerHead(
        owner=owner,
        revision=revision,
        digest_sha256=digit * 64,
        schema_version=1,
    )


def test_two_phase_publication_publishes_authenticated_owner_heads(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    baseline = (_head("catalog", 4, "a"), _head("framework", 8, "b"))
    final = (_head("catalog", 5, "c"), _head("framework", 9, "d"))

    transaction = begin_state_publication(
        state,
        operation="catalog-framework-publish",
        owners=("catalog", "framework"),
        idempotency_key="publish-1",
        owner_heads=baseline,
    )
    blocked = read_state_publication_state(state)
    assert blocked.status == "blocked"
    assert blocked.pending == (transaction.prepared,)
    with pytest.raises(StatePublicationError, match="pending recovery"):
        require_complete_state_epoch(state)

    complete = transaction.commit(final)
    view = read_state_publication_state(state)
    assert view.status == "complete"
    assert view.publication == complete
    assert require_complete_state_epoch(state, owner_heads=final).epoch == 1
    with pytest.raises(StatePublicationConflictError, match="owner heads"):
        require_complete_state_epoch(state, owner_heads=baseline)

    assert complete.content_manifest_name is not None
    content_manifest = state / complete.content_manifest_name
    assert content_manifest.is_file()
    assert (state / publication.STATE_CONTENT_PUBLICATION_MANIFEST_FILENAME).is_file()
    assert json.loads(content_manifest.read_text(encoding="utf-8"))["owner_heads"]


def test_replay_is_idempotent_and_does_not_create_a_second_epoch(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    heads = (_head("image", 1, "1"),)
    key = publication_idempotency_key("image", "generation", 1)
    first = record_state_publication(
        state,
        operation="image-publish",
        owners=("image",),
        status="complete",
        idempotency_key=key,
        owner_heads=heads,
    )
    replay = record_state_publication(
        state,
        operation="image-publish",
        owners=("image",),
        status="complete",
        idempotency_key=key,
        expected_epoch=1,
        owner_heads=heads,
    )
    assert replay == first
    assert len(read_state_publications(state)) == 1
    assert require_complete_state_epoch(state, expected_epoch=1, owner_heads=heads).epoch == 1


def test_interrupted_commit_leaves_pending_gate_and_orphan_manifest_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    transaction = begin_state_publication(
        state,
        operation="interrupted",
        owners=("catalog",),
        idempotency_key="interrupted-1",
        owner_heads=(_head("catalog", 1, "a"),),
    )
    real_append = publication._append_journal

    def fail_complete(path: Path, event: publication.StatePublication) -> None:
        if event.status == "complete":
            raise OSError("simulated crash before complete journal append")
        real_append(path, event)

    monkeypatch.setattr(publication, "_append_journal", fail_complete)
    with pytest.raises(OSError, match="simulated crash"):
        transaction.commit((_head("catalog", 2, "b"),))

    view = read_state_publication_state(state)
    assert view.status == "blocked"
    assert view.epoch.epoch == 0
    assert any(path.name.startswith(publication.STATE_CONTENT_PUBLICATION_MANIFEST_PREFIX)
               for path in state.iterdir())


def test_abort_requires_verified_rollback_to_prepare_heads(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    baseline = (_head("catalog", 2, "a"), _head("framework", 3, "b"))
    transaction = begin_state_publication(
        state,
        operation="rollback-test",
        owners=("catalog", "framework"),
        idempotency_key="rollback-1",
        owner_heads=baseline,
    )
    with pytest.raises(StatePublicationConflictError, match="do not prove rollback"):
        transaction.abort((_head("catalog", 3, "c"), _head("framework", 3, "b")))
    assert read_state_publication_state(state).status == "blocked"

    failed = abort_state_publication(
        state,
        event_id=transaction.prepared.event_id,
        observed_owner_heads=baseline,
        expected_epoch=0,
    )
    assert failed.status == "failed"
    assert read_state_publication_state(state).status == "absent"
    assert len(read_state_publications(state)) == 2


def test_concurrent_different_publications_use_epoch_cas(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()

    def publish(number: int) -> str:
        try:
            record_state_publication(
                state,
                operation=f"operation-{number}",
                owners=("catalog",),
                status="complete",
                idempotency_key=f"concurrent-{number}",
                expected_epoch=0,
                owner_heads=(_head("catalog", number + 1, str(number % 10)),),
            )
        except StatePublicationConflictError:
            return "conflict"
        return "complete"

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = tuple(executor.map(publish, range(8)))
    assert outcomes.count("complete") == 1
    assert outcomes.count("conflict") == 7
    assert read_state_publication_state(state).status == "complete"
    assert require_complete_state_epoch(state).epoch == 1


def test_unresolved_prepare_blocks_a_different_publication(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    prepared = begin_state_publication(
        state, operation="restore", owners=("image",), idempotency_key="restore-1",
        owner_heads=(_head("image", 0, "a"),),
    )
    with pytest.raises(StatePublicationConflictError, match="pending recovery"):
        record_state_publication(
            state, operation="other", owners=("catalog",), status="complete",
            idempotency_key="other-1", expected_epoch=0,
        )
    assert read_state_publications(state) == (prepared.prepared,)
    assert read_state_publication_state(state).status == "blocked"


def test_prepared_baseline_cannot_be_replaced_or_dropped(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    baseline = (_head("image", 0, "a"),)
    transaction = begin_state_publication(
        state, operation="restore", owners=("image",), idempotency_key="restore-1",
        owner_heads=baseline,
    )
    attempts = (
        ("partial", (_head("image", 0, "b"),)),
        ("complete", ()),
        ("failed", baseline),
    )
    for status, heads in attempts:
        with pytest.raises(StatePublicationConflictError):
            record_state_publication(
                state, operation="restore", owners=("image",), status=status,
                idempotency_key="restore-1", owner_heads=heads,
            )
    assert read_state_publications(state) == (transaction.prepared,)


def test_inconsistent_complete_manifest_cannot_be_hidden_by_new_publication(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    complete = record_state_publication(
        state, operation="first", owners=("image",), status="complete",
        idempotency_key="first", owner_heads=(_head("image", 1, "a"),),
    )
    assert complete.content_manifest_name is not None
    (state / complete.content_manifest_name).unlink()
    with pytest.raises(publication.StatePublicationError, match="manifest is missing"):
        record_state_publication(
            state, operation="second", owners=("image",), status="complete",
            idempotency_key="second", owner_heads=(_head("image", 2, "b"),),
        )
    assert read_state_publications(state) == (complete,)
