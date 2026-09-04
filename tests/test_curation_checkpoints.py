from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.curation.checkpoints import (
    MAX_BATCH_PAYLOAD_BYTES,
    MAX_CHECKPOINT_BYTES,
    MAX_SOURCE_HEADS,
    CurationCheckpointBudget,
    CurationCheckpointConflictError,
    CurationCheckpointCorruptError,
    CurationCheckpointStorageError,
    CurationCheckpointRoot,
    CurationCheckpointSourceHead,
    CurationSnapshotObservation,
    compute_batch_digest,
    create_checkpoint,
    parse_checkpoint_json,
    read_checkpoint,
    resume_checkpoint,
    serialize_checkpoint,
    validate_checkpoint,
    write_checkpoint,
)


_PLAN_DIGEST = "sha256:" + "a" * 64
_SNAPSHOT_ID = "sha256:" + "b" * 64
_BATCH_DIGEST = "sha256:" + "c" * 64


def _root(*, inode: int = 22) -> CurationCheckpointRoot:
    return CurationCheckpointRoot(
        path="/tmp/curation-fixture",
        dev=11,
        inode=inode,
        birthtime_ns=-1,
    )


def _head(index: int, *, digest_character: str = "d") -> CurationCheckpointSourceHead:
    return CurationCheckpointSourceHead(
        owner=f"owner-{index}",
        kind="plan",
        head_id=f"head-{index}",
        digest="sha256:" + digest_character * 64,
        root="/tmp/curation-fixture",
        revision=index,
        item_count=index,
        coverage="complete",
        metadata=(("index", index),),
    )


def _budget(*, completed: int = 2) -> CurationCheckpointBudget:
    return CurationCheckpointBudget(
        max_items=10,
        max_files=20,
        max_bytes=1_000,
        items_completed=completed,
        files_checked=3,
        bytes_checked=40,
    )


def _checkpoint(
    *,
    state: str = "partial",
    root: CurationCheckpointRoot | None = None,
    heads: tuple[CurationCheckpointSourceHead, ...] = (_head(1), _head(2)),
    cursor: str | None = "cursor-after",
    event_id: str = "event-1",
    batch: object = ("item-1", "item-2"),
):
    budget = _budget()
    batch_digest = compute_batch_digest(
        operation="verify",
        plan_digest=_PLAN_DIGEST,
        snapshot_id=_SNAPSHOT_ID,
        cursor_before="cursor-before",
        cursor_after=cursor,
        batch=batch,
        budget=budget,
    )
    return create_checkpoint(
        operation="verify",
        state=state,  # type: ignore[arg-type]
        root=root or _root(),
        source_heads=heads,
        plan_digest=_PLAN_DIGEST,
        snapshot_id=_SNAPSHOT_ID,
        cursor=cursor,
        batch_digest=batch_digest,
        budget=budget,
        event_id=event_id,
    )


def _observation(
    *,
    root: CurationCheckpointRoot | None = None,
    heads: tuple[CurationCheckpointSourceHead, ...] = (_head(1), _head(2)),
    plan_digest: str = _PLAN_DIGEST,
    snapshot_id: str = _SNAPSHOT_ID,
) -> CurationSnapshotObservation:
    return CurationSnapshotObservation(
        root=root or _root(),
        source_heads=heads,
        plan_digest=plan_digest,
        snapshot_id=snapshot_id,
    )


def test_checkpoint_is_canonical_and_source_heads_are_sorted() -> None:
    checkpoint = _checkpoint(heads=(_head(2), _head(1)))

    raw = serialize_checkpoint(checkpoint)
    assert raw == checkpoint.to_json()
    assert checkpoint.source_heads[0].owner == "owner-1"
    assert parse_checkpoint_json(raw) == checkpoint
    assert len(raw.encode("utf-8")) <= MAX_CHECKPOINT_BYTES

    payload = json.loads(raw)
    payload["future_field"] = True
    future = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with pytest.raises(CurationCheckpointCorruptError, match="invalid"):
        parse_checkpoint_json(future)
    with pytest.raises(CurationCheckpointCorruptError, match="canonical"):
        parse_checkpoint_json(raw + "\n")


def test_checkpoint_rejects_future_schema_duplicate_keys_and_unbounded_values() -> None:
    checkpoint = _checkpoint()
    payload = json.loads(checkpoint.to_json())
    payload["schema_version"] = 2
    future = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with pytest.raises(CurationCheckpointCorruptError):
        parse_checkpoint_json(future)

    duplicate = checkpoint.to_json().replace('"batch_digest":', '"batch_digest":"' + "c" * 71 + '","batch_digest":', 1)
    with pytest.raises(CurationCheckpointCorruptError, match="invalid"):
        parse_checkpoint_json(duplicate)

    with pytest.raises(ValueError):
        _checkpoint(cursor="x" * 2_049)
    with pytest.raises(ValueError):
        _checkpoint(event_id="x" * 257)
    with pytest.raises(ValueError):
        create_checkpoint(
            operation="verify",
            state="partial",
            root=_root(),
            source_heads=tuple(_head(index) for index in range(MAX_SOURCE_HEADS + 1)),
            plan_digest=_PLAN_DIGEST,
            snapshot_id=_SNAPSHOT_ID,
            cursor=None,
            batch_digest=_BATCH_DIGEST,
            budget=_budget(),
        )
    with pytest.raises(ValueError, match="batch"):
        compute_batch_digest(
            operation="verify",
            plan_digest=_PLAN_DIGEST,
            snapshot_id=_SNAPSHOT_ID,
            cursor_before=None,
            cursor_after=None,
            batch="x" * (MAX_BATCH_PAYLOAD_BYTES + 1),
            budget=_budget(),
        )


def test_validate_and_resume_fail_closed_on_root_or_head_drift() -> None:
    checkpoint = _checkpoint()
    same = validate_checkpoint(checkpoint, lambda: _observation())
    assert same.status == "valid"
    assert same.resumable is True

    root_changed = validate_checkpoint(checkpoint, lambda: _observation(root=_root(inode=23)))
    assert root_changed.status == "snapshot_changed"
    assert root_changed.reason_code == "root_identity_changed"
    assert root_changed.resumable is False

    head_changed = validate_checkpoint(
        checkpoint,
        lambda: _observation(heads=(_head(1, digest_character="e"), _head(2))),
    )
    assert head_changed.status == "snapshot_changed"
    assert head_changed.reason_code == "source_heads_changed"

    resumed = resume_checkpoint(checkpoint, lambda: _observation())
    assert resumed.status == "resume"
    assert resumed.cursor == "cursor-after"
    assert resumed.replay_required is True

    changed = resume_checkpoint(checkpoint, lambda: _observation(plan_digest="sha256:" + "f" * 64))
    assert changed.status == "snapshot_changed"
    assert changed.reason_code == "plan_digest_changed"


def test_replay_requires_the_exact_batch_digest_and_complete_is_terminal() -> None:
    checkpoint = _checkpoint()
    correct = resume_checkpoint(
        checkpoint,
        lambda: _observation(),
        cursor_before="cursor-before",
        batch=("item-1", "item-2"),
    )
    assert correct.status == "resume"

    wrong = resume_checkpoint(
        checkpoint,
        lambda: _observation(),
        cursor_before="cursor-before",
        batch=("item-1", "tampered"),
    )
    assert wrong.status == "invalid"
    assert wrong.reason_code == "batch_digest_mismatch"

    terminal = _checkpoint(state="complete", cursor=None)
    result = resume_checkpoint(terminal, lambda: _observation())
    assert result.status == "complete"
    assert result.cursor is None
    assert result.replay_required is False


@pytest.mark.parametrize(
    ("state", "expected_status"),
    (
        ("partial", "resume"),
        ("complete", "complete"),
        ("cancelled", "resume"),
        ("snapshot_changed", "snapshot_changed"),
        ("invalid", "invalid"),
    ),
)
def test_checkpoint_states_have_explicit_resume_semantics(
    state: str,
    expected_status: str,
) -> None:
    checkpoint = _checkpoint(state=state, cursor=None if state == "complete" else "cursor-after")
    assert parse_checkpoint_json(checkpoint.to_json()) == checkpoint
    result = resume_checkpoint(checkpoint, lambda: _observation())
    assert result.status == expected_status


def test_write_is_crash_tolerant_and_never_replaces_an_event(tmp_path: Path) -> None:
    target = tmp_path / "checkpoints" / "event-1.json"
    checkpoint = _checkpoint()
    assert write_checkpoint(target, checkpoint) == checkpoint
    inode = target.stat().st_ino
    metadata = target.stat()
    assert metadata.st_nlink == 1
    assert metadata.st_mode & 0o777 == 0o600
    assert read_checkpoint(target) == checkpoint
    assert target.stat().st_ino == inode
    assert not list(target.parent.glob("*.tmp"))

    crash_link = target.parent / ".event-1.json.crashed.tmp"
    os.link(target, crash_link)
    assert target.stat().st_nlink == 2
    assert read_checkpoint(target) == checkpoint
    crash_link.unlink()
    assert target.stat().st_nlink == 1

    # A partial temporary from a crashed writer is not a visible checkpoint and
    # must not prevent a later no-replace publication.
    stale = target.parent / ".event-2.json.crashed.tmp"
    stale.write_bytes(b'{"truncated":')
    second = target.parent / "event-2.json"
    checkpoint_two = _checkpoint(event_id="event-2")
    write_checkpoint(second, checkpoint_two)
    assert read_checkpoint(second) == checkpoint_two
    assert stale.exists()

    conflicting = _checkpoint(event_id="event-1", batch=("different",))
    before = target.read_bytes()
    with pytest.raises(CurationCheckpointConflictError, match="different evidence"):
        write_checkpoint(target, conflicting)
    assert target.read_bytes() == before
    assert target.stat().st_ino == inode


def test_read_does_not_create_a_missing_parent(tmp_path: Path) -> None:
    missing = tmp_path / "not-created" / "checkpoint.json"
    with pytest.raises(CurationCheckpointStorageError, match="parent"):
        read_checkpoint(missing)
    assert not missing.parent.exists()


def test_checkpoint_parent_must_be_private(tmp_path: Path) -> None:
    parent = tmp_path / "unsafe"
    parent.mkdir(mode=0o755)
    target = parent / "checkpoint.json"

    with pytest.raises(ValueError, match="unsafe permissions"):
        write_checkpoint(target, _checkpoint())
    assert not target.exists()


def test_stat_identity_factory_uses_caller_supplied_result() -> None:
    result = os.stat_result((0o40755, 2, 1, 1, 1000, 1000, 0, 0, 0, 0))
    root = CurationCheckpointRoot.from_stat_result("/tmp/curation-fixture", result)
    assert root.path == "/tmp/curation-fixture"
    assert root.dev == 1
    assert root.inode == 2
    assert root.birthtime_ns == -1

    legacy = SimpleNamespace(
        path="/tmp/curation-fixture",
        volume_id=3,
        file_id=4,
        birthtime_ns=5,
    )
    adapted = CurationCheckpointRoot.from_root_identity(legacy)
    assert (adapted.dev, adapted.inode, adapted.birthtime_ns) == (3, 4, 5)
