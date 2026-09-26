"""Restore lifecycle checks for pre-effect backend blocks."""

from __future__ import annotations

import json
from pathlib import Path

from neocortex.curation.recovery import (
    RestoreOutcome,
    restore_confirmation_token,
    restore_curation_action,
)
from neocortex.deduplication import FULL_ALGORITHM, full_fingerprint, snapshot_path
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.workflow.actions.file_action_recovery import (
    effect_receipt_json,
    expected_identity_json,
)
from tests.internal_paths_test_support import begin_signed_normal_run


class _BlockedRestoreBackend:
    name = "fixture-blocked-restore"

    def restore(self, candidate):  # type: ignore[no-untyped-def]
        return RestoreOutcome(
            candidate.action_id,
            "blocked",
            "restore_destination_exists",
            "fixture preflight block",
        )


class _MovedThenBlockedRestoreBackend:
    name = "fixture-moved-then-blocked-restore"

    def restore(self, candidate):  # type: ignore[no-untyped-def]
        Path(candidate.trash_path).replace(candidate.effect.source.path)
        return RestoreOutcome(
            candidate.action_id,
            "blocked",
            "restore_destination_exists",
            "adversarial post-frontier block",
        )


def test_restore_backend_block_is_terminal_skip_not_recovery(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    trash_root = tmp_path / "Trash"
    root.mkdir()
    state_directory.mkdir()
    (trash_root / "files").mkdir(parents=True)
    (trash_root / "info").mkdir()
    source = root / "payload.bin"
    source.write_bytes(b"payload")
    expected = snapshot_path(source)
    digest = f"{FULL_ALGORITHM}:" + full_fingerprint(expected).hex()
    trash_path = trash_root / "files" / source.name
    info_path = trash_root / "info" / f"{source.name}.trashinfo"
    source.rename(trash_path)
    info_path.write_text(
        f"[Trash Info]\nPath={source}\nDeletionDate=2026-09-25T00:00:00\n",
        encoding="utf-8",
    )
    evidence = {
        "digest": digest,
        "file_id": f"{expected.file_id:x}",
        "info_path": str(info_path),
        "size": expected.size,
        "trash_path": str(trash_path),
        "trash_root": str(trash_root),
        "volume_id": f"{expected.volume_id:x}",
    }
    original_receipt = json.loads(
        effect_receipt_json(operation="trash", source_path=str(source), target_path=None)
    )
    original_receipt.update({"source_digest": digest, "trash": evidence})
    original_receipt_json = json.dumps(original_receipt, sort_keys=True, separators=(",", ":"))
    database = state_directory / "framework.sqlite3"

    with FrameworkState(database) as state:
        run_id = begin_signed_normal_run(state, root)
        original_id = state.begin_file_action(
            run_id,
            "trash_duplicate",
            str(source),
            None,
            None,
            None,
            True,
        )
        state.mark_file_actions_applying(
            (
                (
                    original_id,
                    expected_identity_json(
                        expected,
                        source_path=str(source),
                        target_path=None,
                    ),
                ),
            )
        )
        state.confirm_file_actions_applied(((original_id, original_receipt_json),))
        confirmation = restore_confirmation_token(original_id, original_receipt_json)

        outcome = restore_curation_action(
            database,
            original_id,
            backend=_BlockedRestoreBackend(),
            confirmation=confirmation,
            actor="fixture-operator",
            state=state,
        )
        restore_status = state._connection.execute(
            "SELECT status FROM file_actions WHERE action_type='restore_curation'"
        ).fetchone()[0]

    assert outcome.status == "blocked"
    assert outcome.reason == "restore_destination_exists"
    assert restore_status == "skipped"
    assert not source.exists()
    assert trash_path.exists()


def test_adversarial_block_after_move_requires_recovery(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    trash_root = tmp_path / "Trash"
    root.mkdir()
    state_directory.mkdir()
    (trash_root / "files").mkdir(parents=True)
    (trash_root / "info").mkdir()
    source = root / "payload.bin"
    source.write_bytes(b"payload")
    expected = snapshot_path(source)
    digest = f"{FULL_ALGORITHM}:" + full_fingerprint(expected).hex()
    trash_path = trash_root / "files" / source.name
    info_path = trash_root / "info" / f"{source.name}.trashinfo"
    source.rename(trash_path)
    info_path.write_text(
        f"[Trash Info]\nPath={source}\nDeletionDate=2026-09-25T00:00:00\n",
        encoding="utf-8",
    )
    evidence = {
        "digest": digest,
        "file_id": f"{expected.file_id:x}",
        "info_path": str(info_path),
        "size": expected.size,
        "trash_path": str(trash_path),
        "trash_root": str(trash_root),
        "volume_id": f"{expected.volume_id:x}",
    }
    original_receipt = json.loads(
        effect_receipt_json(operation="trash", source_path=str(source), target_path=None)
    )
    original_receipt.update({"source_digest": digest, "trash": evidence})
    original_receipt_json = json.dumps(original_receipt, sort_keys=True, separators=(",", ":"))
    database = state_directory / "framework.sqlite3"

    with FrameworkState(database) as state:
        run_id = begin_signed_normal_run(state, root)
        original_id = state.begin_file_action(
            run_id, "trash_duplicate", str(source), None, None, None, True
        )
        state.mark_file_actions_applying(
            (
                (
                    original_id,
                    expected_identity_json(expected, source_path=str(source), target_path=None),
                ),
            )
        )
        state.confirm_file_actions_applied(((original_id, original_receipt_json),))
        confirmation = restore_confirmation_token(original_id, original_receipt_json)
        outcome = restore_curation_action(
            database,
            original_id,
            backend=_MovedThenBlockedRestoreBackend(),
            confirmation=confirmation,
            actor="fixture-operator",
            state=state,
        )
        restore_status = state._connection.execute(
            "SELECT status FROM file_actions WHERE action_type='restore_curation'"
        ).fetchone()[0]

    assert outcome.status == "recovery_required"
    assert outcome.reason == "blocked_restore_evidence_changed"
    assert restore_status == "recovery_required"
    assert source.exists()
    assert not trash_path.exists()
