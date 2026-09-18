from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from neocortex.capabilities.formats.archive import materialization
from neocortex.capabilities.formats.archive.materialization import (
    REGISTERED_SCRATCH_OWNER,
    materialize_archive,
)
from neocortex.runtime.scratch import ScratchManager, ScratchState


def _zip_bytes(name: str, payload: bytes) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, payload)
    return stream.getvalue()


def _scratch_manager(root: Path) -> ScratchManager:
    root.mkdir(mode=0o700)
    return ScratchManager(root, owner=REGISTERED_SCRATCH_OWNER, create_root=False)


def test_archive_materialization_retires_registered_workspace_on_success(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.zip"
    source.write_bytes(_zip_bytes("evidence.txt", b"archive evidence"))
    destination = tmp_path / "materialized"
    scratch = tmp_path / "scratch"
    manager = _scratch_manager(scratch)

    result = materialize_archive(
        source,
        destination,
        apply=True,
        scratch_directory=scratch,
    )

    assert result.status == "complete"
    assert result.manifest_digest
    assert (destination / "evidence.txt").read_bytes() == b"archive evidence"
    assert manager.records() == ()
    # Scratch keeps its durable retirement control after all workspaces retire.
    assert {path.name for path in scratch.iterdir()} == {".scratch-control"}


def test_archive_materialization_replay_reuses_outputs_and_retire_scratch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.zip"
    source.write_bytes(_zip_bytes("replay.txt", b"stable bytes"))
    destination = tmp_path / "materialized"
    scratch = tmp_path / "scratch"
    manager = _scratch_manager(scratch)

    first = materialize_archive(
        source,
        destination,
        apply=True,
        scratch_directory=scratch,
    )
    before = (destination / "replay.txt").read_bytes()
    second = materialize_archive(
        source,
        destination,
        apply=True,
        scratch_directory=scratch,
    )

    assert first.status == second.status == "complete"
    assert before == b"stable bytes"
    assert (destination / "replay.txt").read_bytes() == before
    assert {output.status for output in second.outputs} == {"reused"}
    assert manager.records() == ()
    # Scratch keeps its durable retirement control after all workspaces retire.
    assert {path.name for path in scratch.iterdir()} == {".scratch-control"}


def test_archive_materialization_retains_registered_workspace_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.zip"
    source.write_bytes(_zip_bytes("retained.txt", b"retain this stage"))
    destination = tmp_path / "materialized"
    scratch = tmp_path / "scratch"
    manager = _scratch_manager(scratch)

    def fail_after_scan(*_args, **_kwargs):
        raise RuntimeError("fixture archive publication failure")

    monkeypatch.setattr(materialization, "_apply_manifest", fail_after_scan)

    with pytest.raises(RuntimeError, match="fixture archive publication failure"):
        materialize_archive(
            source,
            destination,
            apply=True,
            scratch_directory=scratch,
        )

    records = manager.records()
    assert len(records) == 1
    record = records[0]
    assert record.state is ScratchState.FAILED_RETAINED
    assert record.owner == REGISTERED_SCRATCH_OWNER
    assert record.metadata["component"] == REGISTERED_SCRATCH_OWNER
    assert record.metadata["operation"] == "materialize_archive"
    assert record.path.parent == scratch
    assert record.path.is_dir()
    assert (record.path / "manifest.json").is_file()
    assert record.size_bytes > 0
    assert not destination.exists()
