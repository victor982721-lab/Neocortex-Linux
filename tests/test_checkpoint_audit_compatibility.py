"""Exact v1 manifest compatibility through the real checkpoint API."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from neocortex.api.curation_checkpoint_api import (
    curation_checkpoint_create_payload,
    curation_checkpoint_resume_payload,
    curation_checkpoint_status_payload,
)
from neocortex.curation.checkpoints import read_checkpoint
from neocortex.curation.preview import build_curation_plan_page
from tests.test_checkpoint_audit_budgets import _observe_real_work, _result
from tests.test_curation_verification import _build_state


def _as_v1(state: Path, metadata: dict, *, state_value: str | None = None) -> tuple[Path, bytes]:
    path = state / "curation/checkpoints" / (metadata["checkpoint_id"] + ".json")
    value = read_checkpoint(path).to_dict()
    for key in ("page_limit", "traversal_complete", "coverage", "coverage_reasons"):
        del value[key]
    value.update(schema_version=1, contract="neocortex.curation-checkpoint/v1")
    if state_value is not None:
        value["state"] = state_value
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    path.write_bytes(raw)
    assert read_checkpoint(path).to_json().encode() == raw
    return path, raw


@pytest.mark.parametrize("operation", ["scan", "verify"])
def test_legacy_terminal_is_revalidated_without_upgrading_unrecorded_evidence(
    tmp_path: Path, operation: str,
) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=False, pair_count=2)
    plan = build_curation_plan_page(state, 100)
    created = curation_checkpoint_create_payload(
        operation, plan_id=plan.plan_digest if operation == "verify" else None, state_directory=state,
    )
    metadata = _result(created)
    assert metadata["traversal_complete"] is True
    path, raw = _as_v1(state, metadata, state_value="complete")
    with _observe_real_work() as observed:
        status = curation_checkpoint_status_payload(metadata["checkpoint_id"], state_directory=state)
        resumed = curation_checkpoint_resume_payload(metadata["checkpoint_id"], state_directory=state)
    assert observed["pages"] == 2
    assert not observed["verification"]
    for result in (status, resumed):
        assert result["coverage"] == "partial"
        current = _result(result)
        assert current["checkpoint_id"] == metadata["checkpoint_id"]
        assert current["contract"] == "neocortex.curation-checkpoint/v1"
        assert current["coverage_reasons"] == ["legacy_coverage_unproven"]
        assert current["resume_status"] == "partial"
        assert current["replay_required"] is False
        assert current["limit_source"] == "legacy_default"
    assert path.read_bytes() == raw
    assert len(list(path.parent.glob("checkpoint-*.json"))) == 1


def test_legacy_nonterminal_cursor_resumes_to_v2_without_rewriting_ancestor(tmp_path: Path) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=False, pair_count=2)
    created = curation_checkpoint_create_payload("scan", limit=1, state_directory=state)
    first = _result(created)
    path, raw = _as_v1(state, first)
    resumed = curation_checkpoint_resume_payload(first["checkpoint_id"], state_directory=state)
    successor = _result(resumed)
    assert successor["checkpoint_id"] != first["checkpoint_id"]
    assert successor["contract"] == "neocortex.curation-checkpoint/v2"
    assert successor["previous_checkpoint_digest"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert successor["budget"]["items_completed"] == 2
    assert successor["coverage"] == "partial"
    assert "legacy_coverage_unproven" in successor["coverage_reasons"]
    assert successor["traversal_complete"] is True
    assert successor["replay_required"] is False
    assert path.read_bytes() == raw


def test_legacy_null_nonterminal_cursor_is_not_replayed_from_start(tmp_path: Path) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=False, pair_count=2)
    plan = build_curation_plan_page(state, 100)
    created = curation_checkpoint_create_payload(
        "verify", plan_id=plan.plan_digest, max_bytes=1, state_directory=state,
    )
    first = _result(created)
    assert first["cursor"] is None
    path, raw = _as_v1(state, first)
    status = curation_checkpoint_status_payload(first["checkpoint_id"], state_directory=state)
    assert _result(status)["resume_status"] == "invalid"
    assert _result(status)["reason_code"] == "legacy_continuation_unproven"
    with _observe_real_work() as observed:
        resumed = curation_checkpoint_resume_payload(first["checkpoint_id"], state_directory=state)
    assert not observed["verification"]
    assert resumed["error"]["code"] == "schema_incompatible"
    assert resumed["read_only"] is True
    assert _result(resumed)["checkpoint_id"] == first["checkpoint_id"]
    assert _result(resumed)["replay_required"] is False
    assert path.read_bytes() == raw
