"""Authoritative duplicate-group verification beyond the preview sample cap."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from neocortex.api import curation_authorization_api
from neocortex.curation import authorization, lifecycle
from neocortex.curation.authorization import (
    CurationAuthorizationError,
    CurationAuthorizationSnapshotChanged,
    _effect_manifest,
    authorize_curation_items,
)
from neocortex.curation.preview import build_curation_plan_page
from neocortex.curation.verification import CurationWorkBudget, verify_curation_page
from neocortex.deduplication import DedupIndex, DedupPlanner, InventoryCheckpoint
from neocortex.documents.document_catalog import initialize_document_catalog
from neocortex.persistence.framework_schema import initialize_framework_schema
from neocortex.persistence.sqlite_immutable import SQLiteReadSession
from neocortex.workflow.authorization.contracts import MAX_AUTHORIZATION_JSON_BYTES


def _large_group(tmp_path: Path, *, count: int = 65, compact_paths: bool = False):
    state = tmp_path / ("s" if compact_paths else "state")
    corpus = tmp_path / ("c" if compact_paths else "corpus")
    state.mkdir()
    corpus.mkdir()
    for index in range(count):
        name = f"{index:04d}" if compact_paths else f"member-{index:04d}.bin"
        (corpus / name).write_bytes(b"same")
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


def _resolved_group(tmp_path: Path, *, count: int = 65, compact_paths: bool = False):
    state, corpus, page, _item = _large_group(
        tmp_path, count=count, compact_paths=compact_paths
    )
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

    return state, corpus, page, duplicate, framework


def _originals(corpus: Path) -> dict[str, tuple[bytes, int, int, int, int]]:
    result = {}
    for path in corpus.iterdir():
        observed = path.stat()
        result[path.name] = (
            path.read_bytes(),
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            observed.st_mtime_ns,
        )
    return result


def _assert_no_authorization_or_actions(framework: Path) -> None:
    with SQLiteReadSession(framework) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='table' AND name='curation_authorization_grants'"
        ).fetchone()
        if table is not None:
            assert connection.execute(
                "SELECT COUNT(*) FROM curation_authorization_grants"
            ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM file_actions").fetchone()[0] == 0


def test_authorization_expands_a_truncated_group_from_published_owner() -> None:
    # Keep the positive grant within its independent wire budget. The private
    # root still honors TMPDIR, and all 65 physical members remain authoritative.
    with TemporaryDirectory(prefix="g") as temporary:
        state, corpus, page, duplicate, framework = _resolved_group(
            Path(temporary), compact_paths=True
        )
        before = _originals(corpus)
        assert len(duplicate.item.evidence["members"]) == 64
        assert duplicate.item.evidence["members_truncated"] is True

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

        effects = outcome.grant.authorized_effects
        assert effects is not None
        assert len(effects) == 64
        assert {effect.source.path for effect in effects} == {
            str(path)
            for path in corpus.iterdir()
            if str(path) != duplicate.item.evidence["keep_path"]
        }
        grant_json = outcome.grant.to_json()
        assert len(grant_json.encode("utf-8")) <= MAX_AUTHORIZATION_JSON_BYTES
        with SQLiteReadSession(framework) as connection:
            receipts = connection.execute(
                "SELECT receipt_json FROM curation_authorization_grants"
            ).fetchall()
            assert [row[0] for row in receipts] == [grant_json]
            assert connection.execute("SELECT COUNT(*) FROM file_actions").fetchone()[0] == 0
        assert _originals(corpus) == before


def test_authorization_denies_oversized_complete_grant_before_publication(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The preview remains limited to 64 members, so its ReviewTask fits while
    # the complete grant reaches 100 effects and exceeds the separate wire
    # budget. Keep the root compact instead of overflowing the earlier row.
    state, corpus, page, duplicate, framework = _resolved_group(
        tmp_path_factory.mktemp("grant"), count=101, compact_paths=True
    )
    before = _originals(corpus)
    assert len(before) == 101
    assert duplicate.item.evidence["members_truncated"] is True

    with pytest.raises(CurationAuthorizationError, match="byte limit") as rejected:
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

    assert isinstance(rejected.value.__cause__, ValueError)
    _assert_no_authorization_or_actions(framework)
    assert _originals(corpus) == before

    monkeypatch.setattr(curation_authorization_api, "default_state_directory", lambda: state)
    payload = curation_authorization_api.curation_authorize_payload(
        page.plan_digest,
        [duplicate.item.item_id],
        action="trash",
        actor="victor",
        expires_ns=10_000,
        max_bytes=10_000,
        clock_ns=lambda: 3_000,
    )
    assert payload["status"] == "unavailable"
    assert payload["error"]["code"] == "authorization_denied"
    assert "byte limit" in payload["error"]["message"]
    assert payload["exit_code"] == 1
    assert payload["grant"] is None
    assert payload["effects"] == {"state": "none", "corpus": "none", "external": "none"}
    assert payload["trust"]["actions_authorized"] is False
    assert payload["trust"]["physical_effect_applied"] is False
    _assert_no_authorization_or_actions(framework)
    assert _originals(corpus) == before


def test_authorization_wraps_invalid_root_metadata_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, corpus, page, duplicate, framework = _resolved_group(tmp_path, count=2)
    before = _originals(corpus)
    snapshot_path = authorization.snapshot_path

    def invalid_root(path):
        observed = snapshot_path(path)
        if Path(path) == corpus:
            return replace(observed, birthtime_ns=-2)
        return observed

    monkeypatch.setattr(authorization, "snapshot_path", invalid_root)
    with pytest.raises(CurationAuthorizationError, match="birthtime_ns") as rejected:
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

    assert isinstance(rejected.value.__cause__, ValueError)
    assert "root_snapshot.birthtime_ns" in str(rejected.value.__cause__)
    _assert_no_authorization_or_actions(framework)
    assert _originals(corpus) == before


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
