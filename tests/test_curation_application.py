"""Contained-fixture acceptance tests for the 0.11 grant-bound effect slice."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.curation.application import (
    ApplyCandidate,
    BackendOutcome,
    PosixRenameBackend,
    apply_authorization_grant,
    reconcile_curation_actions,
)
from neocortex.curation.authorization import authorize_curation_items
from neocortex.curation.lifecycle import decide_curation_item, review_curation_page
from neocortex.curation.preview import build_curation_plan_page
from neocortex.deduplication import (
    DedupIndex,
    DedupPlanner,
    InventoryCheckpoint,
    full_fingerprint,
    snapshot_path,
)
from neocortex.documents.document_catalog import initialize_document_catalog
from neocortex.persistence.framework_schema import initialize_framework_schema
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.workflow.actions.file_action_recovery import effect_receipt_json
from neocortex.workflow.authorization.contracts import AuthorizationEffect
from neocortex.workflow.authorization.repository import read_authorization_grant
from tests.internal_paths_test_support import begin_signed_normal_run


class FixtureTrashBackend:
    name = "fixture-trash-v1"

    def __init__(self, trash_root: Path) -> None:
        self.trash_root = trash_root
        self.calls = 0

    def apply(self, candidate: ApplyCandidate) -> BackendOutcome:
        self.calls += 1
        effect = candidate.effect
        target = self.trash_root / Path(effect.source.path).name
        os.rename(effect.source.path, target)
        info = target.with_suffix(target.suffix + ".trashinfo")
        info.write_text("[Trash Info]\n", encoding="utf-8")
        payload = json.loads(
            effect_receipt_json(
                operation="trash",
                source_path=effect.source.path,
                target_path=None,
            )
        )
        payload["trash"] = {
            "trash_path": str(target),
            "info_path": str(info),
            "volume_id": f"{target.stat().st_dev:x}",
            "file_id": f"{target.stat().st_ino:x}",
            "size": effect.source.size,
            "digest": effect.source_digest,
        }
        return BackendOutcome(
            "applied",
            "fixture_trash_verified",
            receipt_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )


class RecoveryBackend:
    name = "fixture-recovery-v1"

    def __init__(self) -> None:
        self.calls = 0

    def apply(self, _candidate: ApplyCandidate) -> BackendOutcome:
        self.calls += 1
        return BackendOutcome("recovery_required", "fixture_effect_ambiguous")


def _fixture(tmp_path: Path, *, group_count: int = 1) -> tuple[Path, Path, Path, Path, str]:
    state = tmp_path / "state"
    corpus = tmp_path / "corpus"
    trash = tmp_path / "trash"
    state.mkdir()
    corpus.mkdir()
    trash.mkdir()
    for number in range(group_count):
        suffix = "" if number == 0 else f"-{number}"
        payload = b"same" if number == 0 else f"same-{number}".encode()
        (corpus / f"keep{suffix}.txt").write_bytes(payload)
        (corpus / f"duplicate{suffix}.txt").write_bytes(payload)
    with DedupIndex(state / "dedup.sqlite3") as index:
        summary = index.scan(corpus)
        index.bind_inventory_checkpoint(
            InventoryCheckpoint(str(corpus), summary.scan_id, None, None, None, True)
        )
        DedupPlanner(index).plan(summary.scan_id, exact_compare=True)
    initialize_document_catalog(state / "document_catalog.sqlite3")
    framework = state / "framework.sqlite3"
    with closing(sqlite3.connect(framework)) as connection:
        initialize_framework_schema(connection, lambda: None)
    page = build_curation_plan_page(state, 100, None)
    reviewed = review_curation_page(
        state,
        framework,
        plan_digest=page.plan_digest,
        limit=100,
        clock_ns=lambda: 1_000,
    )
    for offset, item in enumerate(reviewed.items, start=1):
        assert item.current_event_id is not None
        decide_curation_item(
            state,
            framework,
            plan_digest=page.plan_digest,
            item_id=item.item.item_id,
            expected_event_id=item.current_event_id,
            decision="resolved",
            decision_scope="permanent",
            actor="victor",
            clock_ns=lambda offset=offset: 2_000 + offset,
        )
    duplicates = [item for item in reviewed.items if item.item.kind == "duplicate_group"]
    grant = authorize_curation_items(
        state,
        framework,
        plan_digest=page.plan_digest,
        item_ids=tuple(item.item.item_id for item in duplicates),
        action="trash",
        actor="victor",
        expires_ns=10_000,
        max_bytes=100,
        clock_ns=lambda: 3_000,
    ).grant
    return state, corpus, trash, framework, grant.grant_id


def test_apply_trash_is_grant_bound_and_replay_is_idempotent(tmp_path: Path) -> None:
    state, corpus, trash, framework, grant_id = _fixture(tmp_path)
    with FrameworkState(framework) as framework_state:
        run_id = begin_signed_normal_run(framework_state, corpus)
        backend = FixtureTrashBackend(trash)
        first = apply_authorization_grant(
            state,
            framework,
            grant_id,
            run_id=run_id,
            backend=backend,
            state=framework_state,
            clock_ns=lambda: 4_000,
        )
        second = apply_authorization_grant(
            state,
            framework,
            grant_id,
            run_id=run_id,
            backend=backend,
            state=framework_state,
            clock_ns=lambda: 5_000,
        )
    assert first.status == "complete"
    assert first.applied == 1
    assert second.status == "complete"
    assert second.effects[0].idempotent is True
    assert backend.calls == 1
    assert len(list(corpus.iterdir())) == 1
    assert len(list(trash.glob("*.trashinfo"))) == 1
    with closing(sqlite3.connect(framework)) as connection:
        row = connection.execute(
            "SELECT status,effect_receipt_json,evidence FROM file_actions"
        ).fetchone()
    assert row[0] == "applied"
    assert json.loads(str(row[1]))["grant_id"] == grant_id
    assert json.loads(str(row[2]))["effect"]["effect_id"]


def test_apply_replay_across_operational_runs_reuses_grant_intent(tmp_path: Path) -> None:
    state, corpus, trash, framework, grant_id = _fixture(tmp_path)
    with FrameworkState(framework) as framework_state:
        first_run = begin_signed_normal_run(framework_state, corpus)
        backend = FixtureTrashBackend(trash)
        first = apply_authorization_grant(
            state,
            framework,
            grant_id,
            run_id=first_run,
            backend=backend,
            state=framework_state,
            clock_ns=lambda: 4_000,
        )
        second_run = begin_signed_normal_run(framework_state, corpus)
        second = apply_authorization_grant(
            state,
            framework,
            grant_id,
            run_id=second_run,
            backend=backend,
            state=framework_state,
            clock_ns=lambda: 5_000,
        )
    assert first.status == second.status == "complete"
    assert second.effects[0].idempotent is True
    assert backend.calls == 1
    with closing(sqlite3.connect(framework)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (1,)


def test_apply_rechecks_expiry_between_effects(tmp_path: Path) -> None:
    state, corpus, trash, framework, grant_id = _fixture(tmp_path, group_count=2)
    clock_values = iter((3_500, 4_000, 10_000))
    backend = FixtureTrashBackend(trash)
    with FrameworkState(framework) as framework_state:
        run_id = begin_signed_normal_run(framework_state, corpus)
        result = apply_authorization_grant(
            state,
            framework,
            grant_id,
            run_id=run_id,
            backend=backend,
            state=framework_state,
            clock_ns=lambda: next(clock_values),
        )
    assert backend.calls == 1
    assert result.status == "blocked"
    assert sum(effect.status == "applied" for effect in result.effects) == 1
    with closing(sqlite3.connect(framework)) as connection:
        statuses = [row[0] for row in connection.execute("SELECT status FROM file_actions ORDER BY action_id")]
    assert statuses == ["applied", "failed"]


def test_apply_rejects_expired_grant_before_creating_actions(tmp_path: Path) -> None:
    state, corpus, trash, framework, grant_id = _fixture(tmp_path)
    backend = FixtureTrashBackend(trash)
    with FrameworkState(framework) as framework_state:
        run_id = begin_signed_normal_run(framework_state, corpus)
        with pytest.raises(RuntimeError, match="expired"):
            apply_authorization_grant(
                state,
                framework,
                grant_id,
                run_id=run_id,
                backend=backend,
                state=framework_state,
                clock_ns=lambda: 10_000,
            )
    assert backend.calls == 0
    with closing(sqlite3.connect(framework)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (0,)


def test_legacy_grant_is_readable_but_not_consumable(tmp_path: Path) -> None:
    state, corpus, trash, framework, grant_id = _fixture(tmp_path)
    grant = read_authorization_grant(framework, grant_id=grant_id)
    assert grant is not None
    legacy = replace(
        grant,
        grant_id=grant.grant_id + ":legacy",
        authorization_key=grant.authorization_key + ":legacy",
        root_snapshot=None,
        source_heads=None,
        source_heads_digest=None,
        authorized_effects=None,
        authorized_effects_digest=None,
        max_actions=1,
    )
    from neocortex.workflow.authorization.repository import issue_authorization_grant

    issue_authorization_grant(framework, legacy)
    backend = FixtureTrashBackend(trash)
    with FrameworkState(framework) as framework_state:
        run_id = begin_signed_normal_run(framework_state, corpus)
        with pytest.raises(RuntimeError, match="manifest"):
            apply_authorization_grant(
                state,
                framework,
                legacy.grant_id,
                run_id=run_id,
                backend=backend,
                state=framework_state,
                clock_ns=lambda: 4_000,
            )
    assert backend.calls == 0
    with closing(sqlite3.connect(framework)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM file_actions"
        ).fetchone() == (0,)


def test_apply_detects_same_metadata_content_mutation_from_effect_digest(tmp_path: Path) -> None:
    state, corpus, trash, framework, grant_id = _fixture(tmp_path)
    grant = read_authorization_grant(framework, grant_id=grant_id)
    assert grant is not None and grant.authorized_effects is not None
    duplicate = Path(grant.authorized_effects[0].source.path)
    original = snapshot_path(duplicate)
    duplicate.write_bytes(b"diff")
    os.utime(duplicate, ns=(original.mtime_ns, original.mtime_ns))
    backend = FixtureTrashBackend(trash)
    with FrameworkState(framework) as framework_state:
        run_id = begin_signed_normal_run(framework_state, corpus)
        result = apply_authorization_grant(
            state,
            framework,
            grant_id,
            run_id=run_id,
            backend=backend,
            state=framework_state,
            clock_ns=lambda: 4_000,
        )
    assert result.status == "blocked"
    assert result.effects[0].reason == "preflight_failed"
    assert backend.calls == 0
    assert duplicate.exists()
    with closing(sqlite3.connect(framework)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (0,)


def test_posix_rename_backend_never_overwrites_existing_target(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "source.txt"
    target = root / "target.txt"
    source.write_bytes(b"source")
    target.write_bytes(b"target")
    snapshot = snapshot_path(source)
    digest = "xxh3_128_full_v1:" + full_fingerprint(snapshot).hex()
    effect = AuthorizationEffect(
        effect_id="effect:1",
        item_id="item:1",
        task_id="task:1",
        ordinal=1,
        action="rename",
        kind="organization_plan",
        source=snapshot,
        source_digest=digest,
        target_path=str(target),
    )
    result = PosixRenameBackend().apply(
        ApplyCandidate("grant:1", "sha256:" + "0" * 64, root, effect)
    )
    assert result.status == "blocked"
    assert result.reason == "destination_exists"
    assert source.read_bytes() == b"source"
    assert target.read_bytes() == b"target"


def test_apply_recovery_is_not_retried(tmp_path: Path) -> None:
    state, corpus, _trash, framework, grant_id = _fixture(tmp_path)
    backend = RecoveryBackend()
    with FrameworkState(framework) as framework_state:
        run_id = begin_signed_normal_run(framework_state, corpus)
        first = apply_authorization_grant(
            state,
            framework,
            grant_id,
            run_id=run_id,
            backend=backend,
            state=framework_state,
            clock_ns=lambda: 4_000,
        )
        second = apply_authorization_grant(
            state,
            framework,
            grant_id,
            run_id=run_id,
            backend=backend,
            state=framework_state,
            clock_ns=lambda: 5_000,
        )
    assert first.status == "recovery_required"
    assert second.status == "recovery_required"
    assert backend.calls == 1
    with closing(sqlite3.connect(framework)) as connection:
        assert connection.execute(
            "SELECT status FROM file_actions"
        ).fetchone() == ("recovery_required",)
    first = reconcile_curation_actions(framework, actor="victor")
    second = reconcile_curation_actions(framework, actor="victor")
    assert first[0].classification == "not_performed"
    assert second[0].event_id == first[0].event_id
    with closing(sqlite3.connect(framework)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM file_action_reconciliation_events"
        ).fetchone() == (1,)
