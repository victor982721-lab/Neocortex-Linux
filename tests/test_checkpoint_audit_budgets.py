"""Real API-level I/O accounting regressions for bounded checkpoint work."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys

import pytest

from neocortex.api import curation_checkpoint_api
from neocortex.api.curation_checkpoint_api import (
    curation_checkpoint_create_payload,
    curation_checkpoint_resume_payload,
    curation_checkpoint_status_payload,
)
from neocortex.curation.checkpoints import MAX_WORK_BYTES, MAX_WORK_FILES, MAX_WORK_ITEMS, read_checkpoint
from neocortex.curation.preview import build_curation_plan_page
from neocortex.curation.verification import verify_curation_page
from tests.test_curation_verification import _build_state


@contextmanager
def _observe_real_work():
    calls = {"pages": 0, "verification": []}

    def profile(frame, event, value):
        if event == "call" and frame.f_code is build_curation_plan_page.__code__:
            calls["pages"] += 1
        if event == "return" and frame.f_code is verify_curation_page.__code__:
            calls["verification"].append((frame.f_locals["budget"], value))

    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        yield calls
    finally:
        sys.setprofile(previous)


def _result(payload):
    result = payload["result"]
    assert isinstance(result, dict), payload["error"]
    return result


@pytest.mark.parametrize("field,maximum", [
    ("max_items", MAX_WORK_ITEMS), ("max_files", MAX_WORK_FILES), ("max_bytes", MAX_WORK_BYTES),
])
@pytest.mark.parametrize("invalid", [0, -1, True, "1", "over_maximum"])
def test_invalid_budget_rejected_before_plan_or_verification_io(
    tmp_path: Path, field: str, maximum: int, invalid: object,
) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=False, pair_count=2)
    plan = build_curation_plan_page(state, 100)
    value = maximum + 1 if invalid == "over_maximum" else invalid
    with _observe_real_work() as observed:
        payload = curation_checkpoint_create_payload(
            "verify", plan_id=plan.plan_digest, state_directory=state, **{field: value},
        )
    assert payload["error"]["code"] == "invalid_request"
    assert observed == {"pages": 0, "verification": []}
    assert not (state / "curation" / "checkpoints").exists()


@pytest.mark.parametrize("field,expected_files,expected_items", [
    ("max_bytes", 0, 0), ("max_files", 1, 0), ("max_items", 2, 1),
])
def test_create_obeys_budget_and_persists_interrupted_prefix(
    tmp_path: Path, field: str, expected_files: int, expected_items: int,
) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=False, pair_count=2)
    plan = build_curation_plan_page(state, 100)
    with _observe_real_work() as observed:
        payload = curation_checkpoint_create_payload(
            "verify", plan_id=plan.plan_digest, state_directory=state, **{field: 1},
        )
    metadata = _result(payload)
    assert payload["status"] == "partial"
    assert payload["error"]["code"] == "budget_exhausted"
    assert payload["exit_code"] == 2
    assert payload["coverage"] == "partial"
    assert len(observed["verification"]) == 1
    allowance, actual = observed["verification"][0]
    assert getattr(allowance, field) == 1
    assert actual.files_checked == expected_files
    budget = metadata["budget"]
    assert budget["files_checked"] == actual.files_checked
    assert budget["bytes_checked"] == actual.bytes_checked
    assert budget["items_completed"] == expected_items
    for counter, maximum in (("files_checked", "max_files"), ("bytes_checked", "max_bytes"),
                             ("items_completed", "max_items")):
        assert budget[counter] <= budget[maximum]
    assert metadata["traversal_complete"] is False
    assert metadata["resume_status"] == "budget_exhausted"
    assert metadata["replay_required"] is False
    assert metadata["cursor"] == (
        build_curation_plan_page(state, 1).next_cursor if expected_items else None
    )
    stored = read_checkpoint(state / "curation/checkpoints" / (metadata["checkpoint_id"] + ".json"))
    assert stored.budget.items_completed == expected_items
    assert stored.budget.files_checked == expected_files
    inspected = curation_checkpoint_status_payload(metadata["checkpoint_id"], state_directory=state)
    assert _result(inspected)["resume_status"] == "budget_exhausted"
    assert _result(inspected)["reason_code"] == "budget_exhausted"
    assert _result(inspected)["replay_required"] is False
    with _observe_real_work() as resumed_work:
        resumed = curation_checkpoint_resume_payload(metadata["checkpoint_id"], state_directory=state)
    assert _result(resumed)["checkpoint_id"] == metadata["checkpoint_id"]
    assert not resumed_work["verification"]
    assert sum(result.files_checked for _bound, result in resumed_work["verification"]) == 0
    assert len(list((state / "curation/checkpoints").glob("checkpoint-*.json"))) == 1


@pytest.mark.parametrize("field", ["max_files", "max_bytes"])
def test_resume_spends_only_remainder_and_does_not_skip_interrupted_item(
    tmp_path: Path, field: str,
) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=False, pair_count=2)
    page = build_curation_plan_page(state, 1)
    with _observe_real_work() as reference_work:
        reference = curation_checkpoint_create_payload(
            "verify", plan_id=page.plan_digest, limit=1, state_directory=state,
        )
    reference_result = reference_work["verification"][0][1]
    unit_bytes = reference_result.bytes_checked // reference_result.files_checked
    limit = 3 if field == "max_files" else unit_bytes * 3
    created = curation_checkpoint_create_payload(
        "verify", plan_id=page.plan_digest, limit=1, state_directory=state, **{field: limit},
    )
    first = _result(created)
    assert first["budget"]["items_completed"] == 1
    with _observe_real_work() as observed:
        resumed = curation_checkpoint_resume_payload(first["checkpoint_id"], state_directory=state)
    second = _result(resumed)
    assert second["checkpoint_id"] != first["checkpoint_id"]
    assert len(observed["verification"]) == 1
    allowance, actual = observed["verification"][0]
    assert getattr(allowance, field) == (1 if field == "max_files" else unit_bytes)
    assert actual.files_checked == 1
    assert second["budget"]["items_completed"] == 1
    assert second["budget"]["files_checked"] == 3
    assert second["budget"]["bytes_checked"] == unit_bytes * 3
    assert second["cursor"] == first["cursor"]
    assert second["traversal_complete"] is False
    assert second["replay_required"] is False
    with _observe_real_work() as stopped_work:
        stopped = curation_checkpoint_resume_payload(second["checkpoint_id"], state_directory=state)
    assert not stopped_work["verification"]
    assert _result(stopped)["checkpoint_id"] == second["checkpoint_id"]
    assert _result(reference)["limit"] == 1


def test_interrupted_batch_retains_cursor_after_completed_prefix(tmp_path: Path) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=False, pair_count=3)
    page = build_curation_plan_page(state, 100)
    with _observe_real_work() as observed:
        payload = curation_checkpoint_create_payload(
            "verify", plan_id=page.plan_digest, max_files=3, state_directory=state,
        )
    metadata = _result(payload)
    actual = observed["verification"][0][1]
    assert actual.items[0].status == "verified"
    assert actual.items[1].reason == "budget_exhausted"
    assert metadata["budget"]["items_completed"] == 1
    assert metadata["budget"]["files_checked"] == 3
    assert metadata["cursor"] == build_curation_plan_page(state, 1).next_cursor
    pending = build_curation_plan_page(state, 100, metadata["cursor"])
    assert pending.items[0].item_id == actual.items[1].item_id


def test_unspendable_positive_remainder_records_one_durable_stop(tmp_path: Path) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=False, pair_count=2)
    page = build_curation_plan_page(state, 100)
    one_group = verify_curation_page(build_curation_plan_page(state, 1)).bytes_checked
    created = curation_checkpoint_create_payload(
        "verify", plan_id=page.plan_digest, max_bytes=one_group + 1, state_directory=state,
    )
    first = _result(created)
    assert first["budget"]["items_completed"] == 1
    assert first["budget"]["bytes_remaining"] == 1
    with _observe_real_work() as observed:
        stopped = curation_checkpoint_resume_payload(first["checkpoint_id"], state_directory=state)
    final = _result(stopped)
    assert len(observed["verification"]) == 1
    assert observed["verification"][0][1].files_checked == 0
    assert final["checkpoint_id"] != first["checkpoint_id"]
    assert final["budget"] == first["budget"]
    assert final["cursor"] == first["cursor"]
    assert final["resume_status"] == "budget_exhausted"
    assert final["replay_required"] is False
    with _observe_real_work() as replay_work:
        replay = curation_checkpoint_resume_payload(final["checkpoint_id"], state_directory=state)
    assert not replay_work["verification"]
    assert _result(replay)["checkpoint_id"] == final["checkpoint_id"]
    assert len(list((state / "curation/checkpoints").glob("checkpoint-*.json"))) == 2


def test_internal_call_cap_can_resume_completed_prefix_with_fresh_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reduce only the per-call allowance, not the algorithm or any file reads,
    # so the real cap/retry boundary can be exercised with four tiny files.
    monkeypatch.setattr(curation_checkpoint_api, "MAX_VERIFICATION_FILES", 3)
    state, _corpus = _build_state(tmp_path, exact_compare=False, pair_count=2)
    page = build_curation_plan_page(state, 100)
    created = curation_checkpoint_create_payload(
        "verify", plan_id=page.plan_digest, max_files=10, state_directory=state,
    )
    first = _result(created)
    assert first["budget"]["files_checked"] == 3
    assert first["budget"]["items_completed"] == 1
    assert first["resume_status"] == "resume"
    assert first["replay_required"] is True
    assert "budget_exhausted" not in first["coverage_reasons"]
    with _observe_real_work() as observed:
        resumed = curation_checkpoint_resume_payload(first["checkpoint_id"], state_directory=state)
    final = _result(resumed)
    assert observed["verification"][0][0].max_files == 3
    assert observed["verification"][0][1].files_checked == 2
    assert final["budget"]["files_checked"] == 5
    assert final["budget"]["items_completed"] == 2
    assert final["traversal_complete"] is True
    assert final["replay_required"] is False
