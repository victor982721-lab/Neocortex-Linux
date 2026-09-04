"""Controlled checkpoint create/status/resume API over temporary fixtures."""

from __future__ import annotations

import hashlib
from pathlib import Path

from neocortex.api.curation_checkpoint_api import (
    curation_checkpoint_create_payload,
    curation_checkpoint_resume_payload,
    curation_checkpoint_status_payload,
)
from neocortex.api import public
import neocortex.sdk as sdk
from neocortex.curation.checkpoints import read_checkpoint
from neocortex.curation.preview import build_curation_plan_page
from tests.test_curation_verification import _build_state


def _checkpoint_path(state: Path, checkpoint_id: str) -> Path:
    return state / "curation" / "checkpoints" / f"{checkpoint_id}.json"


def test_scan_checkpoint_roundtrip_resume_is_deterministic(tmp_path: Path) -> None:
    state, corpus = _build_state(tmp_path, exact_compare=True, pair_count=8)
    before = {path.name: path.read_bytes() for path in corpus.iterdir()}

    created = curation_checkpoint_create_payload(
        "scan",
        limit=5,
        state_directory=state,
        request_id="checkpoint-create",
    )
    assert created["status"] == "complete"
    assert created["read_only"] is False
    assert created["effects"] == {
        "state": "curation_checkpoint",
        "corpus": "none",
        "external": "none",
    }
    metadata = created["result"]
    assert isinstance(metadata, dict)
    checkpoint_id = metadata["checkpoint_id"]
    assert isinstance(checkpoint_id, str)
    first_path = _checkpoint_path(state, checkpoint_id)
    assert first_path.is_file()
    first = read_checkpoint(first_path)
    assert first.state == "partial"
    assert first.cursor is not None

    status = curation_checkpoint_status_payload(
        checkpoint_id,
        state_directory=state,
        request_id="checkpoint-status",
    )
    assert status["status"] == "complete"
    assert status["read_only"] is True
    assert status["effects"] == {
        "state": "none",
        "corpus": "none",
        "external": "none",
    }
    status_result = status["result"]
    assert isinstance(status_result, dict)
    assert status_result["resume_status"] == "resume"
    assert status_result["replay_required"] is True

    resumed = curation_checkpoint_resume_payload(
        checkpoint_id,
        state_directory=state,
        request_id="checkpoint-resume",
    )
    assert resumed["status"] == "complete"
    resumed_result = resumed["result"]
    assert isinstance(resumed_result, dict)
    successor_id = resumed_result["checkpoint_id"]
    assert successor_id != checkpoint_id
    assert resumed_result["resumed_from"] == checkpoint_id
    assert resumed_result["previous_checkpoint_digest"] == (
        "sha256:" + hashlib.sha256(first.to_json().encode("utf-8")).hexdigest()
    )
    assert _checkpoint_path(state, str(successor_id)).is_file()

    replay = curation_checkpoint_resume_payload(
        checkpoint_id,
        state_directory=state,
        request_id="checkpoint-resume-replay",
    )
    assert replay["result"] == resumed["result"]
    assert len(tuple((state / "curation" / "checkpoints").glob("checkpoint-*.json"))) == 2
    assert {path.name: path.read_bytes() for path in corpus.iterdir()} == before


def test_verify_checkpoint_carries_exact_work_counters(tmp_path: Path) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=False, pair_count=3)
    page = build_curation_plan_page(state, 100)

    created = curation_checkpoint_create_payload(
        "verify",
        plan_id=page.plan_digest,
        limit=2,
        state_directory=state,
    )
    assert created["status"] == "complete"
    metadata = created["result"]
    assert isinstance(metadata, dict)
    budget = metadata["budget"]
    assert isinstance(budget, dict)
    assert budget["items_completed"] == 2
    assert budget["files_checked"] > 0
    assert budget["bytes_checked"] > 0
    assert metadata["state"] == "partial"


def test_checkpoint_writes_require_explicit_state_directory() -> None:
    result = curation_checkpoint_create_payload("scan")

    assert result["status"] == "unavailable"
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == "invalid_request"
    assert result["exit_code"] == 2

    invalid = curation_checkpoint_create_payload("scan", state_directory=123)  # type: ignore[arg-type]
    assert invalid["error"]["code"] == "invalid_request"  # type: ignore[index]


def test_checkpoint_api_is_exported_by_public_facades() -> None:
    from neocortex.api import curation_checkpoint_api

    assert public.curation_checkpoint_create_payload is curation_checkpoint_api.curation_checkpoint_create_payload
    assert sdk.curation_checkpoint_resume_payload is curation_checkpoint_api.curation_checkpoint_resume_payload


def test_missing_checkpoint_is_unavailable_without_creating_state(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"

    result = curation_checkpoint_status_payload(
        "checkpoint-00000000000000000000000000000000",
        state_directory=state,
    )

    assert result["status"] == "unavailable"
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == "unavailable"
    assert not state.exists()
