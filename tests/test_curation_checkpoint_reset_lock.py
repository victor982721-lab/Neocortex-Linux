"""Factory-reset exclusion for curation checkpoint publication."""

from __future__ import annotations

from pathlib import Path

from neocortex.api.curation_checkpoint_api import (
    curation_checkpoint_create_payload,
    curation_checkpoint_resume_payload,
)
from neocortex.runtime.control.locking import FrameworkRunLock
from tests.test_curation_verification import _build_state


def _checkpoint_files(state: Path) -> tuple[Path, ...]:
    return tuple(sorted((state / "curation" / "checkpoints").glob("checkpoint-*.json")))


def test_busy_framework_writer_blocks_create_before_checkpoint_publication(
    tmp_path: Path,
) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=True, pair_count=4)

    with FrameworkRunLock(state / "framework.lock"):
        result = curation_checkpoint_create_payload(
            "scan",
            limit=1,
            state_directory=state,
            request_id="busy-create",
        )

    assert result["status"] == "unavailable"
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == "unavailable"
    assert _checkpoint_files(state) == ()


def test_busy_framework_writer_blocks_resume_without_successor_publication(
    tmp_path: Path,
) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=True, pair_count=4)
    created = curation_checkpoint_create_payload(
        "scan",
        limit=1,
        state_directory=state,
        request_id="before-resume",
    )
    metadata = created["result"]
    assert isinstance(metadata, dict)
    checkpoint_id = metadata["checkpoint_id"]
    assert isinstance(checkpoint_id, str)
    before = _checkpoint_files(state)
    assert len(before) == 1

    with FrameworkRunLock(state / "framework.lock"):
        result = curation_checkpoint_resume_payload(
            checkpoint_id,
            state_directory=state,
            request_id="busy-resume",
        )

    assert result["status"] == "unavailable"
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == "unavailable"
    assert _checkpoint_files(state) == before
