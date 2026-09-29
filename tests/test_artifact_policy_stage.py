from __future__ import annotations

from pathlib import Path
import sqlite3
import struct

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.workflow.actions.action_artifact_stage import ArtifactPrepassError, ArtifactStageMixin


class _Stage(ArtifactStageMixin):
    _apply = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[tuple[str, str], ...]]] = []

    def _checkpoint(self) -> None:
        return None

    def _apply_trash_batch(
        self,
        action_type,
        batch,
        *,
        expected_snapshots,
        expected_content_proofs=None,
        defer_reconciliation,
    ):
        self.calls.append((action_type, batch))
        assert action_type == "trash_artifact"
        assert len(expected_snapshots) == len(batch)
        assert expected_content_proofs is not None
        assert all(proof is not None for proof in expected_content_proofs)
        assert defer_reconciliation is True
        return 0, 0, 0


def _snapshot(path: Path) -> FileSnapshot:
    stat = path.stat()
    return FileSnapshot(str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, -1)


def _valid_elf() -> bytes:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4:7] = b"\x02\x01\x01"
    struct.pack_into("<HHI", header, 16, 2, 0x3E, 1)
    struct.pack_into("<QQ", header, 32, 0, 0)
    struct.pack_into("<HHHHH", header, 52, 64, 56, 0, 64, 0)
    return bytes(header)


def test_artifact_stage_preview_excludes_candidates_by_path_and_identity(tmp_path: Path) -> None:
    generated = tmp_path / ".pytest_cache" / "v" / "cache" / "nodeids"
    generated.parent.mkdir(parents=True)
    generated.write_text("generated", encoding="utf-8")
    retained = tmp_path / "History"
    retained.write_text("valuable", encoding="utf-8")
    stage = _Stage()

    result = stage.run_artifact_prepass([_snapshot(generated), _snapshot(retained)])

    assert result["status"] == "completed"
    assert result["matched"] == 1
    assert result["planned"] == 1
    assert result["applied"] == 0
    assert result["excluded_paths"] == (str(generated),)
    assert stage._artifact_is_excluded(str(generated)) is True
    assert stage._artifact_is_excluded(_snapshot(generated)) is True
    assert generated.exists()
    assert retained.exists()
    assert stage.calls and stage.calls[0][0] == "trash_artifact"
    assert result["previews"][0]["rule_id"] == "artifact.generated-file.v1"


def test_preidentify_phase_is_conservative(tmp_path: Path) -> None:
    source = tmp_path / "program.dll"
    source.write_bytes(_valid_elf())
    generated = tmp_path / "package-1.0.dist-info" / "METADATA"
    generated.parent.mkdir()
    generated.write_text("Metadata-Version: 2.1\nName: package\n", encoding="utf-8")
    stage = _Stage()

    result = stage.run_artifact_prepass(
        [_snapshot(source), _snapshot(generated)],
        preidentify=True,
    )

    assert result["matched"] == 1
    assert result["planned"] == 1
    assert str(source) not in result["excluded_paths"]
    assert str(generated) in result["excluded_paths"]


def test_apply_recovery_status_is_not_reported_as_protected_success(tmp_path: Path) -> None:
    candidate = tmp_path / "binary"
    candidate.write_bytes(_valid_elf())

    class RecoveryStage(_Stage):
        _apply = True

        def __init__(self) -> None:
            super().__init__()
            self._run_id = 7
            self._state = type("State", (), {})()
            self._state._connection = sqlite3.connect(":memory:")
            self._state._connection.execute(
                "CREATE TABLE file_actions(run_id INTEGER, action_type TEXT, "
                "status TEXT, source_path TEXT)"
            )

        def _apply_trash_batch(
            self,
            action_type,
            batch,
            *,
            expected_snapshots,
            expected_content_proofs=None,
            defer_reconciliation,
        ):
            self._state._connection.execute(
                "INSERT INTO file_actions VALUES(?,?,?,?)",
                (self._run_id, action_type, "recovery_required", batch[0][0]),
            )
            self._state._connection.commit()
            return 0, 1, 0

    stage = RecoveryStage()
    with pytest.raises(ArtifactPrepassError):
        stage.run_artifact_prepass([_snapshot(candidate)])
