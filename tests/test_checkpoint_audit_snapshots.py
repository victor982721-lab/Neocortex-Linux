"""Regression coverage for checkpoint snapshot and traversal invariants."""

from __future__ import annotations

import sys
import sqlite3
import zlib
from collections.abc import Callable
from pathlib import Path
from types import FrameType

import pytest

from neocortex.api import curation_checkpoint_api
from neocortex.api.curation_checkpoint_api import (
    curation_checkpoint_create_payload,
    curation_checkpoint_resume_payload,
    curation_checkpoint_status_payload,
)
from neocortex.curation.preview import build_curation_plan_page
from neocortex.curation.verification import verify_curation_page
from neocortex.capabilities.formats.docx.state import initialize_docx_state
from neocortex.deduplication import DedupIndex, DedupPlanner, InventoryCheckpoint
from neocortex.deduplication import snapshot_path
from neocortex.documents.document_catalog import update_document_catalog_source
from neocortex.documents.document_organization_planning import plan_document_organization
from neocortex.documents.document_organization_scope import capture_organization_input_scope
from tests.test_curation_verification import _build_state


def _result(payload: dict[str, object]) -> dict[str, object]:
    result = payload["result"]
    assert isinstance(result, dict)
    return result


def _publish_new_scan(state: Path, corpus: Path) -> None:
    """Publish a genuinely new inventory and duplicate plan for the fixture."""

    (corpus / "new-after-checkpoint.txt").write_text("new snapshot", encoding="utf-8")
    with DedupIndex(state / "dedup.sqlite3") as index:
        summary = index.scan(corpus)
        index.bind_inventory_checkpoint(
            InventoryCheckpoint(str(corpus), summary.scan_id, None, None, None, True)
        )
        DedupPlanner(index, partial_threshold=0).plan(summary.scan_id, exact_compare=True)


def _publish_complete_catalog_scope(state: Path, corpus: Path, tmp_path: Path) -> None:
    """Publish one real catalog source and compatible organization-plan run."""

    source = corpus / "catalog-source.docx"
    source.write_bytes(b"bounded catalog fixture")
    _publish_new_scan(state, corpus)
    source_state = state / "docx.sqlite3"
    initialize_docx_state(source_state)
    snapshot = snapshot_path(source)
    with sqlite3.connect(source_state) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            integrity_status,text_zlib,text_chars,text_xxh3_128,last_seen_run_id,
            updated_ns,title,author)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"{snapshot.volume_id}:{snapshot.file_id}", snapshot.path, snapshot.size,
                snapshot.mtime_ns, snapshot.birthtime_ns, "checkpoint-test-v1", "complete",
                "valid", zlib.compress(b"technical report"), 16, "fixture-text", 1, 1,
                "Technical report", "",
            ),
        )
    update_document_catalog_source(
        state / "document_catalog.sqlite3", source_state, "docx", verify_source_paths=True,
        source_root=corpus,
    )
    source_scope = capture_organization_input_scope(state / "document_catalog.sqlite3", corpus)
    planned = plan_document_organization(
        state / "document_catalog.sqlite3", tmp_path / "organized", source_scope=source_scope,
    )
    assert planned.planned + planned.review_required > 0


@pytest.mark.parametrize("operation", ["scan", "verify"])
@pytest.mark.parametrize("limit", [100, 1], ids=["terminal", "partial"])
def test_status_and_resume_reject_checkpoint_after_new_published_snapshot(
    tmp_path: Path,
    limit: int,
    operation: str,
) -> None:
    state, corpus = _build_state(tmp_path, exact_compare=True, pair_count=3)
    _publish_complete_catalog_scope(state, corpus, tmp_path)
    plan_id = build_curation_plan_page(state, 100).plan_digest
    created = curation_checkpoint_create_payload(
        operation,  # type: ignore[arg-type]
        plan_id=plan_id if operation == "verify" else None,
        limit=limit,
        state_directory=state,
        request_id=f"create-{operation}-{limit}",
    )
    assert created["status"] == "complete"
    checkpoint = _result(created)
    checkpoint_id = checkpoint["checkpoint_id"]
    assert isinstance(checkpoint_id, str)
    if limit == 100:
        assert checkpoint["state"] == "complete"
    else:
        assert checkpoint["state"] == "partial"

    _publish_new_scan(state, corpus)

    verify_calls = 0
    verify_code = curation_checkpoint_api.verify_curation_page.__code__

    def profile(frame: FrameType, event: str, arg: object) -> Callable[..., object] | None:
        nonlocal verify_calls
        if event == "call" and frame.f_code is verify_code:
            verify_calls += 1
        return profile

    for operation_call in (
        curation_checkpoint_status_payload,
        curation_checkpoint_resume_payload,
    ):
        sys.setprofile(profile)
        try:
            payload = operation_call(checkpoint_id, state_directory=state)
        finally:
            sys.setprofile(None)
        assert payload["status"] == "snapshot_changed"
        error = payload["error"]
        assert isinstance(error, dict)
        assert error["code"] == "snapshot_changed"
        result = payload.get("result")
        if isinstance(result, dict):
            assert result.get("resume_status") != "complete"
    assert verify_calls == 0


def test_missing_catalog_terminal_partial_is_stable_and_not_reprocessed(
    tmp_path: Path,
) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=True, pair_count=0)
    (state / "document_catalog.sqlite3").unlink()

    created = curation_checkpoint_create_payload("scan", limit=1, state_directory=state)
    assert created["status"] == "complete"
    assert created["coverage"] == "partial"
    checkpoint = _result(created)
    checkpoint_id = checkpoint["checkpoint_id"]
    assert isinstance(checkpoint_id, str)
    assert checkpoint["state"] == "partial"
    assert checkpoint["cursor"] is None
    assert checkpoint["traversal_complete"] is True
    assert checkpoint["limit"] == 1

    status = curation_checkpoint_status_payload(checkpoint_id, state_directory=state)
    assert status["status"] == "complete"
    assert status["coverage"] == "partial"
    status_result = _result(status)
    assert status_result["checkpoint_id"] == checkpoint_id
    assert status_result["resume_status"] == "partial"
    assert status_result["replay_required"] is False
    assert status_result["traversal_complete"] is True

    calls: list[tuple[object, object]] = []
    target_code = curation_checkpoint_api.build_curation_plan_page.__code__
    verify_code = curation_checkpoint_api.verify_curation_page.__code__
    verify_calls = 0

    def profile(frame: FrameType, event: str, arg: object) -> Callable[..., object] | None:
        nonlocal verify_calls
        if event == "call" and frame.f_code is target_code:
            calls.append((frame.f_locals.get("cursor"), frame.f_locals.get("limit")))
        if event == "call" and frame.f_code is verify_code:
            verify_calls += 1
        return profile

    sys.setprofile(profile)
    try:
        resumed = curation_checkpoint_resume_payload(checkpoint_id, state_directory=state)
    finally:
        sys.setprofile(None)

    assert calls == [(None, 1)]
    assert verify_calls == 0
    assert resumed["status"] == "complete"
    assert resumed["coverage"] == "partial"
    resumed_result = _result(resumed)
    assert resumed_result["checkpoint_id"] == checkpoint_id
    assert resumed_result["replay_required"] is False
    assert resumed_result["traversal_complete"] is True
    assert resumed_result["limit"] == 1
    assert len(tuple((state / "curation" / "checkpoints").glob("checkpoint-*.json"))) == 1


def test_limit_one_is_preserved_by_successor(tmp_path: Path) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=True, pair_count=3)
    created = curation_checkpoint_create_payload("scan", limit=1, state_directory=state)
    checkpoint_id = _result(created)["checkpoint_id"]
    assert isinstance(checkpoint_id, str)

    resumed = curation_checkpoint_resume_payload(checkpoint_id, state_directory=state)

    successor = _result(resumed)
    assert successor["checkpoint_id"] != checkpoint_id
    assert successor["limit"] == 1
    assert successor["budget"]["items_completed"] == 2  # type: ignore[index]


@pytest.mark.parametrize("operation", ["scan", "verify"])
def test_missing_catalog_partial_coverage_persists_through_final_page(
    tmp_path: Path,
    operation: str,
) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=True, pair_count=3)
    (state / "document_catalog.sqlite3").unlink()
    plan_id = build_curation_plan_page(state, 100).plan_digest
    created = curation_checkpoint_create_payload(
        operation,  # type: ignore[arg-type]
        plan_id=plan_id if operation == "verify" else None,
        limit=1,
        state_directory=state,
    )
    current = _result(created)
    seen_ids = {current["checkpoint_id"]}
    assert created["coverage"] == "partial"
    assert current["coverage"] == "partial"
    assert current["limit"] == 1

    while not current["traversal_complete"]:
        checkpoint_id = current["checkpoint_id"]
        assert isinstance(checkpoint_id, str)
        resumed = curation_checkpoint_resume_payload(checkpoint_id, state_directory=state)
        assert resumed["status"] == "complete"
        assert resumed["coverage"] == "partial"
        current = _result(resumed)
        assert current["coverage"] == "partial"
        assert current["limit"] == 1
        assert current["checkpoint_id"] not in seen_ids
        seen_ids.add(current["checkpoint_id"])

    assert current["state"] == "partial"
    assert current["cursor"] is None
    assert current["replay_required"] is False
    terminal_id = current["checkpoint_id"]
    assert isinstance(terminal_id, str)
    status = curation_checkpoint_status_payload(terminal_id, state_directory=state)
    assert status["coverage"] == "partial"
    assert _result(status)["resume_status"] == "partial"


def test_invalid_create_cursor_is_invalid_request_without_verification(
    tmp_path: Path,
) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=True, pair_count=2)
    plan_id = build_curation_plan_page(state, 100).plan_digest
    calls = 0
    target_code = verify_curation_page.__code__

    def profile(frame: FrameType, event: str, arg: object) -> Callable[..., object] | None:
        nonlocal calls
        if event == "call" and frame.f_code is target_code:
            calls += 1
        return profile

    sys.setprofile(profile)
    try:
        payload = curation_checkpoint_create_payload(
            "verify",
            plan_id=plan_id,
            cursor="not-a-curation-cursor",
            state_directory=state,
        )
    finally:
        sys.setprofile(None)

    assert payload["status"] == "unavailable"
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["code"] == "invalid_request"
    assert payload["exit_code"] == 2
    assert calls == 0
    assert not (state / "curation" / "checkpoints").exists()
