"""Durable replay of EML children consumed by Framework Trash actions."""

from __future__ import annotations

import hashlib
import json
from email.message import EmailMessage
from pathlib import Path

import pytest

from neocortex.capabilities.formats.text.email_intake import (
    EmailAttachmentError,
    EmailAttachmentResolution,
    materialize_email_attachments,
)
from neocortex.deduplication import snapshot_path
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.safety.kio_trash import metadata_binding
from neocortex.workflow.actions.file_action_recovery import expected_identity_json
from tests.internal_paths_test_support import begin_signed_normal_run


def _fixture_message(root: Path, *, content: bytes = b"child payload", action_type: str = "trash_redlist") -> tuple[Path, Path, Path, dict[str, object]]:
    source = root / "message.eml"
    message = EmailMessage()
    message.set_content("curated replay fixture")
    message.add_attachment(
        content,
        maintype="application",
        subtype="octet-stream",
        filename="child.bin",
    )
    source.write_bytes(message.as_bytes())
    destination = root / "children"
    manifest = root.parent / "state" / "manifest.json"
    first = materialize_email_attachments(
        source,
        destination,
        apply=True,
        manifest_path=manifest,
    )
    child = Path(first.attachments[0].child_path or "")
    observed = snapshot_path(child)
    digest = hashlib.sha256(child.read_bytes()).hexdigest()
    receipt = {
        "operation": "trash",
        "receipt_type": "successful_return_and_observation",
        "schema_version": 1,
        "source_absent": True,
        "source_digest": metadata_binding(observed),
        "source_path": str(child),
        "target_path": None,
        "trash": {
            "digest": metadata_binding(observed),
            "file_id": f"{observed.file_id:x}",
            "info_path": str(root / "trash" / "info" / "child.bin.trashinfo"),
            "size": observed.size,
            "trash_path": str(root / "trash" / "files" / "child.bin"),
            "trash_root": str(root / "trash"),
            "volume_id": f"{observed.volume_id:x}",
        },
    }
    provenance = {
        "schema": "neocortex.file-action-consumption/v1",
        "authority": "framework.file_actions",
        "status": "applied",
        "published": True,
        "trashed": True,
        "action_id": 1,
        "run_id": 1,
        "action_type": action_type,
        "source_path": str(child),
        "source_sha256": digest,
        "source_digest": metadata_binding(observed),
        "source_identity": {
            "path": str(child),
            "device": observed.volume_id,
            "inode": observed.file_id,
            "size": observed.size,
            "mtime_ns": observed.mtime_ns,
            "birthtime_ns": observed.birthtime_ns,
            "nlink": 1,
        },
        "expected_identity": json.loads(
            expected_identity_json(
                observed,
                source_path=str(child),
                target_path=None,
            )
        ),
        "receipt": receipt,
        "effect_receipt_json": json.dumps(receipt, sort_keys=True),
        "root": str(root),
    }
    return source, destination, manifest, provenance


@pytest.mark.parametrize("content,action_type", [(b"child payload", "trash_redlist"), (b"", "trash_empty_file")])
def test_consumed_trash_replay_does_not_recreate_child_or_destination(
    tmp_path: Path, content: bytes, action_type: str,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source, destination, manifest, provenance = _fixture_message(root, content=content, action_type=action_type)
    # Rebuild an external manifest so the next run can observe the consumed
    # child without recreating the route directory.
    first = materialize_email_attachments(
        source,
        destination,
        apply=True,
        manifest_path=manifest,
    )
    child = Path(first.attachments[0].child_path or "")
    child.unlink()
    destination.rmdir()

    replay = materialize_email_attachments(
        source,
        destination,
        apply=True,
        manifest_path=manifest,
        resolver=lambda _item: EmailAttachmentResolution(
            None,
            reuse_kind="consumed_trash",
            provenance=provenance,
        ),
    )

    assert replay.status == "replayed"
    assert replay.attachments[0].child_reuse_kind == "consumed_trash"
    assert not child.exists()
    assert not destination.exists()
    assert source.exists()


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("status", "recovery_required", "consumed_trash_receipt_mismatch"),
        ("source_sha256", "0" * 64, "consumed_trash_receipt_mismatch"),
        ("root", "/other/root", "consumed_trash_root_mismatch"),
    ),
)
def test_consumed_trash_replay_rejects_untrusted_provenance(
    tmp_path: Path,
    field: str,
    value: object,
    reason: str,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source, destination, manifest, provenance = _fixture_message(root)
    first = materialize_email_attachments(
        source,
        destination,
        apply=True,
        manifest_path=manifest,
    )
    child = Path(first.attachments[0].child_path or "")
    child.unlink()
    destination.rmdir()
    forged = dict(provenance)
    forged[field] = value

    with pytest.raises(EmailAttachmentError, match=reason):
        materialize_email_attachments(
            source,
            destination,
            apply=True,
            manifest_path=manifest,
            resolver=lambda _item: EmailAttachmentResolution(
                None,
                reuse_kind="consumed_trash",
                provenance=forged,
            ),
        )
    assert not child.exists()
    assert not destination.exists()


def test_framework_trash_replay_reader_is_identity_and_root_bound(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    child = root / "child.bin"
    child.write_bytes(b"child")
    observed = snapshot_path(child)
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = begin_signed_normal_run(state, root)
        action_id = state.begin_file_action(
            run_id,
            "trash_redlist",
            str(child),
            None,
            "application/octet-stream",
            "fixture",
            True,
        )
        expected = expected_identity_json(
            observed,
            source_path=str(child),
            target_path=None,
        )
        state.mark_file_actions_applying(((action_id, expected),))
        digest = metadata_binding(observed)
        receipt = {
            "operation": "trash",
            "receipt_type": "successful_return_and_observation",
            "schema_version": 1,
            "source_absent": True,
            "source_digest": digest,
            "source_path": str(child),
            "target_path": None,
            "trash": {
                "digest": digest,
                "file_id": f"{observed.file_id:x}",
                "info_path": str(tmp_path / "trash" / "info" / "child.trashinfo"),
                "size": observed.size,
                "trash_path": str(tmp_path / "trash" / "files" / "child"),
                "trash_root": str(tmp_path / "trash"),
                "volume_id": f"{observed.volume_id:x}",
            },
        }
        state.confirm_file_actions_applied(((action_id, json.dumps(receipt)),))
        identity = {
            "device": observed.volume_id,
            "inode": observed.file_id,
            "size": observed.size,
            "mtime_ns": observed.mtime_ns,
        }
        found = state.read_historical_trash_consumption(
            root,
            source_sha256=hashlib.sha256(child.read_bytes()).hexdigest(),
            child_identity=identity,
        )
        assert len(found) == 1
        assert found[0]["action_id"] == action_id
        assert found[0]["source_identity"]["inode"] == observed.file_id
        assert state.read_historical_trash_consumption(
            tmp_path / "other-root",
            source_sha256=hashlib.sha256(child.read_bytes()).hexdigest(),
            child_identity=identity,
        ) == ()

        wrong = dict(identity)
        wrong["inode"] = observed.file_id + 1
        assert state.read_historical_file_action_consumption(
            root,
            source_sha256=hashlib.sha256(child.read_bytes()).hexdigest(),
            child_identity=wrong,
        ) == ()
