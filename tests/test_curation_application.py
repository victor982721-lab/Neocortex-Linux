"""Contained-fixture acceptance tests for the 0.11 grant-bound effect slice."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.curation.application import (
    ApplyCandidate,
    BackendOutcome,
    KioTrashBackend,
    PosixRenameBackend,
    apply_authorization_grant,
    reconcile_curation_actions,
)
from neocortex.curation.recovery import (
    PosixRestoreBackend,
    restore_curation_action,
    restore_curation_preview,
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
from neocortex.safety.kio_trash import KioTrashVerification
from neocortex.workflow.authorization.contracts import AuthorizationEffect
from neocortex.workflow.authorization.repository import read_authorization_grant
from tests.internal_paths_test_support import begin_signed_normal_run


class FixtureTrashBackend:
    name = "fixture-trash-v1"

    def __init__(self, trash_root: Path, *, structured: bool = False) -> None:
        self.trash_root = trash_root
        self.structured = structured
        self.calls = 0

    def apply(self, candidate: ApplyCandidate) -> BackendOutcome:
        self.calls += 1
        effect = candidate.effect
        files_root = self.trash_root / "files" if self.structured else self.trash_root
        info_root = self.trash_root / "info" if self.structured else self.trash_root
        files_root.mkdir(exist_ok=True)
        info_root.mkdir(exist_ok=True)
        target = files_root / Path(effect.source.path).name
        os.rename(effect.source.path, target)
        info = info_root / (target.name + ".trashinfo")
        info.write_text(f"[Trash Info]\nPath={effect.source.path}\n", encoding="utf-8")
        payload = json.loads(
            effect_receipt_json(
                operation="trash",
                source_path=effect.source.path,
                target_path=None,
            )
        )
        payload["trash"] = {
            "trash_root": str(self.trash_root),
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


class CrashRestoreBackend:
    name = "fixture-restore-crash-v1"

    def restore(self, candidate) -> object:
        os.rename(candidate.trash_path, candidate.effect.source.path)
        candidate.info_path.unlink()
        raise RuntimeError("receipt persistence crash")


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


def test_restore_uses_no_replace_and_verifies_fixture_bytes(tmp_path: Path) -> None:
    state, corpus, trash, framework, grant_id = _fixture(tmp_path)
    # Re-run the small fixture with a KDE-shaped files/info layout so restore
    # can prove the receipt is bound to its configured Trash root.
    with FrameworkState(framework) as framework_state:
        run_id = begin_signed_normal_run(framework_state, corpus)
        backend = FixtureTrashBackend(trash, structured=True)
        applied = apply_authorization_grant(
            state,
            framework,
            grant_id,
            run_id=run_id,
            backend=backend,
            state=framework_state,
            clock_ns=lambda: 4_000,
        )
        assert applied.status == "complete"
        action_id = applied.effects[0].action_id
        assert action_id is not None
        preview = restore_curation_preview(framework, action_id)
        restored = restore_curation_action(
            framework,
            action_id,
            backend=PosixRestoreBackend(trash),
            confirmation=str(preview["confirmation"]),
            actor="victor",
            state=framework_state,
        )
    assert restored.status == "restored"
    assert restored.receipt_json is not None
    assert len(list(corpus.iterdir())) == 2
    assert not list((trash / "files").glob("*.txt"))
    # A second restore cannot replace the now-present source.
    with FrameworkState(framework) as framework_state:
        replay = restore_curation_action(
            framework,
            action_id,
            backend=PosixRestoreBackend(trash),
            confirmation=str(preview["confirmation"]),
            actor="victor",
            state=framework_state,
        )
    assert replay.status == "already_restored"
    assert replay.idempotent is True
    with closing(sqlite3.connect(framework)) as connection:
        action_rows = connection.execute(
            "SELECT action_type,status FROM file_actions ORDER BY action_id"
        ).fetchall()
    assert action_rows == [("trash_curation", "applied"), ("restore_curation", "applied")]


def test_restore_crash_after_effect_is_recovery_and_reconcilable(tmp_path: Path) -> None:
    state, corpus, trash, framework, grant_id = _fixture(tmp_path)
    with FrameworkState(framework) as framework_state:
        run_id = begin_signed_normal_run(framework_state, corpus)
        applied = apply_authorization_grant(
            state,
            framework,
            grant_id,
            run_id=run_id,
            backend=FixtureTrashBackend(trash, structured=True),
            state=framework_state,
            clock_ns=lambda: 4_000,
        )
        action_id = applied.effects[0].action_id
        assert action_id is not None
        preview = restore_curation_preview(framework, action_id)
        crashed = restore_curation_action(
            framework,
            action_id,
            backend=CrashRestoreBackend(),
            confirmation=str(preview["confirmation"]),
            actor="victor",
            state=framework_state,
        )
    assert crashed.status == "recovery_required"
    assert crashed.reason == "restore_backend_exception"
    events = reconcile_curation_actions(framework, actor="victor")
    assert events[0].classification == "confirmed"
    with closing(sqlite3.connect(framework)) as connection:
        assert connection.execute(
            "SELECT action_type,status FROM file_actions ORDER BY action_id"
        ).fetchall() == [("trash_curation", "applied"), ("restore_curation", "recovery_required")]


def test_kio_backend_requires_structured_fixture_trash_evidence(tmp_path: Path) -> None:
    _state, _corpus, trash, framework, grant_id = _fixture(tmp_path)
    grant = read_authorization_grant(framework, grant_id=grant_id)
    assert grant is not None and grant.authorized_effects is not None
    effect = grant.authorized_effects[0]
    root = Path(grant.root)
    client = tmp_path / "kioclient5"
    client.write_text("fixture", encoding="utf-8")
    client.chmod(0o700)
    config = tmp_path / "config"
    config.mkdir()
    files = trash / "files"
    info = trash / "info"
    files.mkdir()
    info.mkdir()

    def runner(command, **kwargs):
        assert command == [str(client), "move", effect.source.path, "trash:/"]
        assert kwargs["shell"] is False
        target = files / Path(effect.source.path).name
        os.rename(effect.source.path, target)
        (info / (target.name + ".trashinfo")).write_text(
            f"[Trash Info]\nPath={effect.source.path}\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    def verifier(_source, _expected, _client):
        target = files / Path(effect.source.path).name
        return KioTrashVerification(
            True,
            json.dumps(
                {
                    "trash_root": str(trash),
                    "trash_path": str(target),
                    "info_path": str(info / (target.name + ".trashinfo")),
                    "volume_id": f"{target.stat().st_dev:x}",
                    "file_id": f"{target.stat().st_ino:x}",
                    "size": effect.source.size,
                    "digest": effect.source_digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    result = KioTrashBackend(
        verifier=verifier,
        runner=runner,
        which=lambda name: str(client) if name == "kioclient5" else None,
        environment={"XDG_CONFIG_HOME": str(config)},
    ).apply(ApplyCandidate(grant_id, "sha256:" + "0" * 64, root, effect))
    assert result.status == "applied"
    assert result.receipt_json is not None


def test_replay_rejects_a_tampered_applied_receipt(tmp_path: Path) -> None:
    state, corpus, trash, framework, grant_id = _fixture(tmp_path)
    with FrameworkState(framework) as framework_state:
        run_id = begin_signed_normal_run(framework_state, corpus)
        backend = FixtureTrashBackend(trash)
        apply_authorization_grant(
            state,
            framework,
            grant_id,
            run_id=run_id,
            backend=backend,
            state=framework_state,
            clock_ns=lambda: 4_000,
        )
    with closing(sqlite3.connect(framework)) as connection:
        connection.execute(
            "UPDATE file_actions SET effect_receipt_json=?",
            ("{}",),
        )
        connection.commit()
    with FrameworkState(framework) as framework_state:
        with pytest.raises(RuntimeError, match="receipt"):
            apply_authorization_grant(
                state,
                framework,
                grant_id,
                run_id=run_id,
                backend=backend,
                state=framework_state,
                clock_ns=lambda: 5_000,
            )
    assert backend.calls == 1
