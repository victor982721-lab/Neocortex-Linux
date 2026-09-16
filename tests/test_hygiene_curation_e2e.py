"""Contained end-to-end fixture for the read-only hygiene/curation gates.

The scenario intentionally exercises the complete *preparation* chain without
giving the hygiene surface a mutation capability.  Curation effects are
allowed only through fake POSIX Trash/restore backends rooted below
``tmp_path``; no production state, corpus, KIO service, or SQLite database is
selected.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from neocortex.curation.application import (
    ApplyCandidate,
    BackendOutcome,
    apply_authorization_grant,
    reconcile_curation_actions,
)
from neocortex.curation.authorization import authorize_curation_items
from neocortex.curation.lifecycle import decide_curation_item, review_curation_page
from neocortex.curation.preview import build_curation_plan_page
from neocortex.curation.recovery import (
    PosixRestoreBackend,
    restore_curation_action,
    restore_curation_preview,
)
from neocortex.deduplication import DedupIndex, DedupPlanner, InventoryCheckpoint
from neocortex.documents.document_catalog import initialize_document_catalog
from neocortex.persistence.framework_schema import initialize_framework_schema
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.artifact_registry import ArtifactRegistry
from neocortex.runtime.hygiene import HygieneManager
from neocortex.runtime.scratch import ScratchManager
from neocortex.workflow.actions.file_action_recovery import effect_receipt_json
from tests.internal_paths_test_support import begin_signed_normal_run


class _FakePosixTrashBackend:
    """A contained Trash backend with the same evidence shape as the POSIX path."""

    name = "fixture-posix-trash-v1"

    def __init__(self, trash_root: Path) -> None:
        self.trash_root = trash_root
        self.calls = 0

    def apply(self, candidate: ApplyCandidate) -> BackendOutcome:
        self.calls += 1
        source = Path(candidate.effect.source.path)
        files_root = self.trash_root / "files"
        info_root = self.trash_root / "info"
        files_root.mkdir(mode=0o700, exist_ok=True)
        info_root.mkdir(mode=0o700, exist_ok=True)
        target = files_root / source.name
        os.rename(source, target)
        info = info_root / f"{target.name}.trashinfo"
        info.write_text(f"[Trash Info]\nPath={source}\n", encoding="utf-8")
        receipt = json.loads(
            effect_receipt_json(
                operation="trash",
                source_path=candidate.effect.source.path,
                target_path=None,
            )
        )
        target_stat = target.stat()
        receipt["trash"] = {
            "trash_root": str(self.trash_root),
            "trash_path": str(target),
            "info_path": str(info),
            "volume_id": f"{target_stat.st_dev:x}",
            "file_id": f"{target_stat.st_ino:x}",
            "size": candidate.effect.source.size,
            "digest": candidate.effect.source_digest,
        }
        return BackendOutcome(
            "applied",
            "fixture_trash_verified",
            receipt_json=json.dumps(receipt, sort_keys=True, separators=(",", ":")),
        )


class _FakeRecoveryBackend:
    name = "fixture-recovery-v1"

    def __init__(self) -> None:
        self.calls = 0

    def apply(self, _candidate: ApplyCandidate) -> BackendOutcome:
        self.calls += 1
        return BackendOutcome("recovery_required", "fixture_effect_ambiguous")


class _EmptyRetentionOwner:
    """A non-persistent empty owner keeps hygiene coverage complete in-fixture."""

    def plan(self) -> dict[str, object]:
        return {
            "status": "ready",
            "observed": 0,
            "protected": 0,
            "eligible": 0,
            "blocked": 0,
            "unknown": 0,
            "stores": [],
        }


def _prepare_curation_fixture(base: Path) -> tuple[Path, Path, Path, Path]:
    """Create one complete duplicate plan, entirely below ``base``."""

    state = base / "state"
    corpus = base / "corpus"
    trash = base / "trash"
    state.mkdir(parents=True)
    corpus.mkdir()
    trash.mkdir(mode=0o700)
    (corpus / "keep.txt").write_bytes(b"same fixture bytes")
    (corpus / "duplicate.txt").write_bytes(b"same fixture bytes")

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
    return state, corpus, trash, framework


def _grant_for_fixture(
    state: Path,
    corpus: Path,
    framework: Path,
    *,
    actor: str = "victor",
) -> tuple[object, str]:
    """Run the explicit plan/review/decision/authorization preparation."""

    del corpus  # The signed run is created at apply time, after authorization.
    page = build_curation_plan_page(state, 100)
    reviewed = review_curation_page(
        state,
        framework,
        plan_digest=page.plan_digest,
        limit=100,
        clock_ns=lambda: 1_000,
    )
    assert reviewed.status == "complete"
    assert reviewed.items
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
            actor=actor,
            clock_ns=lambda offset=offset: 2_000 + offset,
        )
    duplicate_ids = tuple(
        item.item.item_id
        for item in reviewed.items
        if item.item.kind == "duplicate_group"
    )
    assert duplicate_ids
    outcome = authorize_curation_items(
        state,
        framework,
        plan_digest=page.plan_digest,
        item_ids=duplicate_ids,
        action="trash",
        actor=actor,
        expires_ns=10_000,
        max_bytes=1024,
        clock_ns=lambda: 3_000,
    )
    return outcome.grant, page.plan_digest


def test_hygiene_preview_and_curation_lifecycle_are_contained_and_replay_safe(
    tmp_path: Path,
) -> None:
    """Exercise preview, grant, fake Trash, restore, and recovery in one test."""

    # First establish two registered scratch states: one completed/eligible
    # and one active/protected.  The real ArtifactRegistry is used only below
    # tmp_path, so the subsequent hygiene read cannot touch production state.
    registry_root = tmp_path / "artifact-registry"
    scratch_root = tmp_path / "owned-temp"
    audit_root = tmp_path / "audit-work"
    registry = ArtifactRegistry(registry_root, owner="fixture-owner", create_root=True)
    scratch = ScratchManager(
        scratch_root,
        owner="fixture-owner",
        create_root=True,
        artifact_registry=registry,
    )
    audit_scratch = ScratchManager(audit_root, owner="fixture-owner", create_root=True)

    eligible_workspace = scratch.create(
        run_id="hygiene-eligible",
        retain_on_success=True,
        metadata={"purpose": "fixture eligible"},
    )
    eligible_output = eligible_workspace.path / "result.txt"
    eligible_output.write_text("registered fixture", encoding="utf-8")
    eligible_output.chmod(0o600)
    eligible_workspace.complete(retain=True)
    protected_workspace = scratch.create(
        run_id="hygiene-protected",
        retain_on_success=True,
        metadata={"purpose": "fixture active"},
    )
    protected_output = protected_workspace.path / "partial.txt"
    protected_output.write_text("active fixture", encoding="utf-8")
    protected_output.chmod(0o600)

    registry_before = {
        path: path.read_bytes() for path in registry_root.glob("*.json")
    }
    scratch_before = {
        path: path.read_bytes()
        for path in scratch_root.glob("workspace-*/manifest.json")
    }
    hygiene = HygieneManager(
        artifact_registry=registry,
        scratch_managers={
            "owned-temp": scratch,
            "audit-work": audit_scratch,
        },
        retention=_EmptyRetentionOwner(),
        now_ns=10**30,
    )
    hygiene_plan = hygiene.plan()
    assert hygiene_plan.status == "planned"
    assert hygiene_plan.eligible >= 2  # registry + owned-temp projections
    assert hygiene_plan.protected >= 2
    assert hygiene_plan.read_only is True
    assert hygiene_plan.preview_only is True
    assert hygiene_plan.effects_enabled is False
    assert hygiene_plan.deletion_performed == 0
    assert hygiene_plan.applied == 0
    assert hygiene_plan.file_actions == 0
    assert hygiene_plan.to_dict()["deletion_performed"] == 0
    assert {path: path.read_bytes() for path in registry_root.glob("*.json")} == registry_before
    assert {
        path: path.read_bytes() for path in scratch_root.glob("workspace-*/manifest.json")
    } == scratch_before
    assert eligible_workspace.path.exists()
    assert protected_workspace.path.exists()

    # Prepare and consume one curation grant against an entirely temporary
    # corpus.  The fake Trash backend is the only mutation implementation in
    # this test; applying the same grant twice must not call it twice.
    success_base = tmp_path / "curation-success"
    success_base.mkdir()
    state, corpus, trash, framework = _prepare_curation_fixture(success_base)
    grant, plan_digest = _grant_for_fixture(state, corpus, framework)
    assert grant.plan_digest == plan_digest
    corpus_before = {
        path.name: path.read_bytes() for path in corpus.iterdir() if path.is_file()
    }
    with FrameworkState(framework) as framework_state:
        run_id = begin_signed_normal_run(framework_state, corpus)
        backend = _FakePosixTrashBackend(trash)
        applied = apply_authorization_grant(
            state,
            framework,
            grant.grant_id,
            run_id=run_id,
            backend=backend,
            state=framework_state,
            clock_ns=lambda: 4_000,
        )
        replay = apply_authorization_grant(
            state,
            framework,
            grant.grant_id,
            run_id=run_id,
            backend=backend,
            state=framework_state,
            clock_ns=lambda: 5_000,
        )
        assert applied.status == "complete"
        assert applied.applied == 1
        assert replay.status == "complete"
        assert replay.effects[0].idempotent is True
        assert backend.calls == 1
        action_id = applied.effects[0].action_id
        assert action_id is not None
        restore_preview = restore_curation_preview(framework, action_id)
        assert restore_preview["read_only"] is True
        assert restore_preview["restorable"] is True
        restored = restore_curation_action(
            framework,
            action_id,
            backend=PosixRestoreBackend(trash),
            confirmation=str(restore_preview["confirmation"]),
            actor="victor",
            state=framework_state,
        )
    assert restored.status == "restored"
    assert restored.reason == "restore_verified"
    assert {
        path.name: path.read_bytes() for path in corpus.iterdir() if path.is_file()
    } == corpus_before
    assert not tuple((trash / "files").glob("*.txt"))
    restored_preview = restore_curation_preview(framework, action_id)
    assert restored_preview["restorable"] is False
    with closing(sqlite3.connect(framework)) as connection:
        action_rows = connection.execute(
            "SELECT action_type,status FROM file_actions ORDER BY action_id"
        ).fetchall()
    assert action_rows == [
        ("trash_curation", "applied"),
        ("restore_curation", "applied"),
    ]

    # A second independent grant takes the recovery_required branch without
    # touching its source.  Reconciliation is read-only evidence and replaying
    # it returns the same durable event rather than retrying the backend.
    recovery_base = tmp_path / "curation-recovery"
    recovery_base.mkdir()
    recovery_state, recovery_corpus, _recovery_trash, recovery_framework = (
        _prepare_curation_fixture(recovery_base)
    )
    recovery_grant, _ = _grant_for_fixture(
        recovery_state,
        recovery_corpus,
        recovery_framework,
    )
    recovery_before = {
        path.name: path.read_bytes()
        for path in recovery_corpus.iterdir()
        if path.is_file()
    }
    with FrameworkState(recovery_framework) as recovery_framework_state:
        recovery_run_id = begin_signed_normal_run(
            recovery_framework_state,
            recovery_corpus,
        )
        recovery_backend = _FakeRecoveryBackend()
        recovery_result = apply_authorization_grant(
            recovery_state,
            recovery_framework,
            recovery_grant.grant_id,
            run_id=recovery_run_id,
            backend=recovery_backend,
            state=recovery_framework_state,
            clock_ns=lambda: 4_000,
        )
    assert recovery_result.status == "recovery_required"
    assert recovery_result.effects[0].status == "recovery_required"
    assert recovery_backend.calls == 1
    assert {
        path.name: path.read_bytes()
        for path in recovery_corpus.iterdir()
        if path.is_file()
    } == recovery_before
    first_reconcile = reconcile_curation_actions(recovery_framework, actor="victor")
    second_reconcile = reconcile_curation_actions(recovery_framework, actor="victor")
    assert first_reconcile
    assert first_reconcile[0].classification == "not_performed"
    assert second_reconcile[0].event_id == first_reconcile[0].event_id
    with closing(sqlite3.connect(recovery_framework)) as connection:
        assert connection.execute(
            "SELECT status FROM file_actions"
        ).fetchone() == ("recovery_required",)

    # All physical fixture roots and databases used in the scenario are
    # descendants of tmp_path; no production SQLite path is ever selected.
    for candidate in (
        registry_root,
        scratch_root,
        audit_root,
        state,
        corpus,
        trash,
        framework,
        recovery_state,
        recovery_corpus,
        recovery_framework,
    ):
        assert candidate.resolve().is_relative_to(tmp_path.resolve())
