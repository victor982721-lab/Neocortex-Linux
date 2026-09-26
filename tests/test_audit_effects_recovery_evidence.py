"""Adversarial evidence checks for uncertain Trash actions."""

from __future__ import annotations

import json
from pathlib import Path

from neocortex.deduplication import FULL_ALGORITHM, FileSnapshot, full_fingerprint, snapshot_path
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.workflow.actions.file_action_recovery import (
    effect_receipt_json,
    expected_identity_json,
    list_file_action_reconciliations,
)
from tests.internal_paths_test_support import begin_signed_normal_run


def _uncertain_trash_action(
    state: FrameworkState,
    root: Path,
    source: Path,
    expected: FileSnapshot | None = None,
) -> int:
    run_id = begin_signed_normal_run(state, root)
    action_id = state.begin_file_action(
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
                action_id,
                expected_identity_json(
                    snapshot_path(source) if expected is None else expected,
                    source_path=str(source),
                    target_path=None,
                ),
            ),
        )
    )
    state.require_file_action_recovery((action_id,), "fixture uncertainty")
    return action_id


def test_trash_recovery_does_not_confirm_generic_success_receipt(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    source = root / "payload.bin"
    source.write_bytes(b"payload")
    database = state_directory / "framework.sqlite3"

    with FrameworkState(database) as state:
        action_id = _uncertain_trash_action(state, root, source)
        source.unlink()
        # This receipt intentionally has the right generic source fields but no
        # KIO destination evidence.  Absence of the source alone is ambiguous.
        receipt = effect_receipt_json(
            operation="trash",
            source_path=str(source),
            target_path=None,
        )
        state._connection.execute(
            "UPDATE file_actions SET effect_receipt_json=? WHERE action_id=?",
            (receipt, action_id),
        )
        state._connection.commit()

    result = list_file_action_reconciliations(database, limit=1)[0]

    assert result.action_id == action_id
    assert result.classification == "ambiguous"
    assert "receipt" in result.detail.casefold()


def test_trash_recovery_confirms_source_bound_physical_evidence(tmp_path: Path) -> None:
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
    database = state_directory / "framework.sqlite3"

    with FrameworkState(database) as state:
        action_id = _uncertain_trash_action(state, root, source, expected)
        receipt = json.loads(
            effect_receipt_json(
                operation="trash",
                source_path=str(source),
                target_path=None,
            )
        )
        receipt.update({"source_digest": digest, "trash": evidence})
        state._connection.execute(
            "UPDATE file_actions SET effect_receipt_json=? WHERE action_id=?",
            (json.dumps(receipt, sort_keys=True, separators=(",", ":")), action_id),
        )
        state._connection.commit()

    result = list_file_action_reconciliations(database, limit=1)[0]

    assert result.action_id == action_id
    assert result.classification == "confirmed"
