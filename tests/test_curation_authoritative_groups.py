"""Authoritative duplicate-group verification beyond the preview sample cap."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.curation import lifecycle
from neocortex.curation.authorization import (
    CurationAuthorizationSnapshotChanged,
    _effect_manifest,
    authorize_curation_items,
)
from neocortex.curation.preview import build_curation_plan_page
from neocortex.curation.verification import CurationWorkBudget, verify_curation_page
from neocortex.deduplication import DedupIndex, DedupPlanner, InventoryCheckpoint
from neocortex.documents.document_catalog import initialize_document_catalog
from neocortex.persistence.framework_schema import initialize_framework_schema


def _large_group(tmp_path: Path, *, count: int = 65):
    state = tmp_path / "state"
    corpus = tmp_path / "corpus"
    state.mkdir()
    corpus.mkdir()
    for index in range(count):
        (corpus / f"member-{index:04d}.bin").write_bytes(b"same")
    with DedupIndex(state / "dedup.sqlite3") as owner:
        summary = owner.scan(corpus, excluded_paths=())
        owner.bind_inventory_checkpoint(
            InventoryCheckpoint(str(corpus), summary.scan_id, None, None, None, True)
        )
        DedupPlanner(owner, partial_threshold=0).plan(
            summary.scan_id,
            exact_compare=True,
            preview_limit=None,
        )
    initialize_document_catalog(state / "document_catalog.sqlite3")
    page = build_curation_plan_page(state, 100)
    assert page.coverage == "complete"
    assert page.duplicate_groups == 1
    item = next(item for item in page.items if item.kind == "duplicate_group")
    assert item.evidence["member_count"] == count
    return state, corpus, page, item


def test_65_member_group_uses_authoritative_batched_membership(tmp_path: Path) -> None:
    state, _corpus, page, item = _large_group(tmp_path)

    presentation_only = verify_curation_page(page)
    assert presentation_only.status == "partial"
    assert presentation_only.items[0].reason == "evidence_truncated"

    result = verify_curation_page(page, state_directory=state)

    assert result.status == "complete"
    assert result.coverage == "complete"
    assert result.items_verified == 1
    assert result.files_checked == 65
    assert result.items[0].status == "verified"
    assert result.items[0].checked_files == 65
    assert len(item.evidence["members"]) == 64
    assert item.evidence["members_truncated"] is True


def test_authoritative_verification_scales_to_hundreds_without_materializing_list(
    tmp_path: Path,
) -> None:
    state, _corpus, page, _item = _large_group(tmp_path, count=300)

    result = verify_curation_page(
        page,
        state_directory=state,
        max_files=300,
        max_bytes=1_200,
    )

    assert result.status == "complete"
    assert result.items_verified == 1
    assert result.files_checked == 300
    assert result.bytes_checked == 1_200


def test_keeper_after_preview_sample_is_recovered_from_owner(tmp_path: Path) -> None:
    state, _corpus, _page, item = _large_group(tmp_path)
    group_id = int(item.evidence["group_id"])
    database = state / "dedup.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE planned_duplicate_members SET member_order=999 "
            "WHERE group_id=? AND member_order=0",
            (group_id,),
        )
        connection.execute(
            "UPDATE planned_duplicate_members SET member_order=0,role='redundant' "
            "WHERE group_id=? AND member_order=64",
            (group_id,),
        )
        connection.execute(
            "UPDATE planned_duplicate_members SET member_order=64,role='keep' "
            "WHERE group_id=? AND member_order=999",
            (group_id,),
        )
        connection.commit()

    page = build_curation_plan_page(state, 100)
    item = next(item for item in page.items if item.kind == "duplicate_group")
    assert item.evidence["members_truncated"] is True
    assert all(member["role"] != "keep" for member in item.evidence["members"])

    result = verify_curation_page(page, state_directory=state)

    assert result.status == "complete"
    assert result.items_verified == 1
    assert result.files_checked == 65


def test_member_after_preview_sample_cannot_be_verified(tmp_path: Path) -> None:
    state, corpus, page, item = _large_group(tmp_path)
    group_id = int(item.evidence["group_id"])
    with closing(sqlite3.connect(state / "dedup.sqlite3")) as connection:
        row = connection.execute(
            "SELECT path FROM planned_duplicate_members "
            "WHERE group_id=? AND member_order=64",
            (group_id,),
        ).fetchone()
    assert row is not None
    posterior = Path(str(row[0]))
    original = posterior.read_bytes()
    posterior.write_bytes(b"changed")
    try:
        result = verify_curation_page(page, state_directory=state)
    finally:
        posterior.write_bytes(original)

    assert result.status == "snapshot_changed"
    assert result.items_failed == 1
    assert result.items[0].status == "source_changed"
    assert result.items[0].reason == "source_changed"
    assert result.items_verified == 0
    assert posterior.is_relative_to(corpus)


@pytest.mark.parametrize(
    ("max_files", "max_bytes", "expected_checked"),
    ((64, 10_000, 64), (512, 64, 16)),
)
def test_authoritative_group_preserves_partial_budget_state(
    tmp_path: Path,
    max_files: int,
    max_bytes: int,
    expected_checked: int,
) -> None:
    state, _corpus, page, _item = _large_group(tmp_path)

    result = verify_curation_page(
        page,
        state_directory=state,
        max_files=max_files,
        max_bytes=max_bytes,
        budget=CurationWorkBudget(max_files=max_files, max_bytes=max_bytes),
    )

    assert result.status == "partial"
    assert result.coverage == "partial"
    assert result.files_checked == expected_checked
    assert result.items[0].reason == "budget_exhausted"
    assert result.items[0].checked_files == expected_checked
    assert result.items[0].verified_files == expected_checked
    replay = verify_curation_page(page, state_directory=state)
    assert replay.status == "complete"
    assert replay.files_checked == 65


def test_authorization_expands_a_truncated_group_from_published_owner(tmp_path: Path) -> None:
    state, _corpus, page, _item = _large_group(tmp_path)
    framework = state / "framework.sqlite3"
    with closing(sqlite3.connect(framework)) as connection:
        initialize_framework_schema(connection, lambda: None)
    reviewed = lifecycle.review_curation_page(
        state,
        framework,
        plan_digest=page.plan_digest,
        limit=100,
        clock_ns=lambda: 1_000,
    )
    duplicate = next(item for item in reviewed.items if item.item.kind == "duplicate_group")
    assert duplicate.current_event_id is not None
    lifecycle.decide_curation_item(
        state,
        framework,
        plan_digest=page.plan_digest,
        item_id=duplicate.item.item_id,
        expected_event_id=duplicate.current_event_id,
        decision="resolved",
        decision_scope="permanent",
        actor="victor",
        clock_ns=lambda: 2_000,
    )

    outcome = authorize_curation_items(
        state,
        framework,
        plan_digest=page.plan_digest,
        item_ids=(duplicate.item.item_id,),
        action="trash",
        actor="victor",
        expires_ns=10_000,
        max_bytes=10_000,
        clock_ns=lambda: 3_000,
    )

    assert len(outcome.grant.authorized_effects or ()) == 64
    assert outcome.grant.authorized_effects is not None
    assert outcome.grant.authorized_effects[-1].source.path.endswith("member-0064.bin")
    assert all(effect.source.path != duplicate.item.evidence["keep_path"] for effect in outcome.grant.authorized_effects)


def test_authorization_does_not_use_a_tampered_preview_sample(tmp_path: Path) -> None:
    state, _corpus, page, item = _large_group(tmp_path)
    tampered_evidence = dict(item.evidence)
    tampered_evidence["members"] = [{"path": "/tmp/forged", "role": "keep"}]
    tampered = replace(item, evidence=tampered_evidence)

    effects, total_bytes = _effect_manifest(
        (tampered,),
        ("fixture-review-task",),
        "trash",
        page=page,
        state_directory=state,
    )

    assert len(effects) == 64
    assert total_bytes == 64 * 4


def test_authorization_rejects_member_change_after_preview_sample(tmp_path: Path) -> None:
    state, _corpus, page, _item = _large_group(tmp_path)
    framework = state / "framework.sqlite3"
    with closing(sqlite3.connect(framework)) as connection:
        initialize_framework_schema(connection, lambda: None)
    reviewed = lifecycle.review_curation_page(
        state,
        framework,
        plan_digest=page.plan_digest,
        limit=100,
        clock_ns=lambda: 1_000,
    )
    duplicate = next(item for item in reviewed.items if item.item.kind == "duplicate_group")
    assert duplicate.current_event_id is not None
    lifecycle.decide_curation_item(
        state,
        framework,
        plan_digest=page.plan_digest,
        item_id=duplicate.item.item_id,
        expected_event_id=duplicate.current_event_id,
        decision="resolved",
        decision_scope="permanent",
        actor="victor",
        clock_ns=lambda: 2_000,
    )
    with closing(sqlite3.connect(state / "dedup.sqlite3")) as connection:
        path = str(
            connection.execute(
                "SELECT path FROM planned_duplicate_members "
                "WHERE group_id=? AND member_order=64",
                (int(duplicate.item.evidence["group_id"]),),
            ).fetchone()[0]
        )
    posterior = Path(path)
    original = posterior.read_bytes()
    posterior.write_bytes(b"changed")
    try:
        with pytest.raises(CurationAuthorizationSnapshotChanged) as error:
            authorize_curation_items(
                state,
                framework,
                plan_digest=page.plan_digest,
                item_ids=(duplicate.item.item_id,),
                action="trash",
                actor="victor",
                expires_ns=10_000,
                max_bytes=10_000,
                clock_ns=lambda: 3_000,
            )
    finally:
        posterior.write_bytes(original)
    assert "changed" in str(error.value).casefold() or "snapshot" in str(error.value).casefold()
