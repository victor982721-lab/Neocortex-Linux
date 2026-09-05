"""Fail-closed curation regressions; all effects stay in injected tmp fixtures."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.curation.application import (
    BackendOutcome,
    CurationApplicationError,
    apply_authorization_grant,
)
from neocortex.curation.authorization import CurationAuthorizationError, authorize_curation_items
from neocortex.curation.recovery import (
    PosixRestoreBackend,
    RestoreOutcome,
    restore_curation_action,
    restore_curation_preview,
)
from neocortex.deduplication import FileChangedError, full_fingerprint, snapshot_path
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.safety import kio_trash
from neocortex.safety.kio_trash import KioTrashStatus, move_to_trash
from neocortex.workflow.actions import file_action_recovery as reconciliation
from neocortex.workflow.authorization.repository import read_authorization_grant
from tests.internal_paths_test_support import begin_signed_normal_run
from tests.test_curation_application import FixtureTrashBackend, _fixture


def _apply_fixture(tmp_path: Path):
    state, corpus, trash, database, grant_id = _fixture(tmp_path)
    grant = read_authorization_grant(database, grant_id=grant_id)
    assert grant is not None and grant.authorized_effects
    with FrameworkState(database) as owner:
        run_id = begin_signed_normal_run(owner, corpus)
        result = apply_authorization_grant(
            state, database, grant_id, run_id=run_id,
            backend=FixtureTrashBackend(trash, structured=True), state=owner,
            clock_ns=lambda: 4_000,
        )
        assert result.status == "complete"
    action_id = result.effects[0].action_id
    assert action_id is not None
    return state, corpus, trash, database, grant, run_id, action_id


@pytest.mark.parametrize("malformed", [None, object(), SimpleNamespace(status="applied")])
def test_apply_invalid_backend_result_is_durable_recovery_without_retry(tmp_path, malformed):
    state, corpus, _trash, database, grant_id = _fixture(tmp_path)

    class Backend:
        name = "invalid-fixture"
        calls = 0

        def apply(self, _candidate):
            self.calls += 1
            return malformed

    backend = Backend()
    with FrameworkState(database) as owner:
        run_id = begin_signed_normal_run(owner, corpus)
        for _ in range(2):
            result = apply_authorization_grant(
                state, database, grant_id, run_id=run_id, backend=backend,
                state=owner, clock_ns=lambda: 4_000,
            )
            assert result.status == "recovery_required"
            assert owner._connection.execute("SELECT status FROM file_actions").fetchall() == [
                ("recovery_required",)
            ]
    assert backend.calls == 1
    assert len(list(corpus.iterdir())) == 2


@pytest.mark.parametrize("malformed", [None, object(), SimpleNamespace(status="restored")])
def test_restore_invalid_backend_result_is_durable_recovery_without_retry(tmp_path, malformed):
    _state, _corpus, _trash, database, _grant, _run_id, action_id = _apply_fixture(tmp_path)
    preview = restore_curation_preview(database, action_id)

    class Backend:
        name = "invalid-restore-fixture"
        calls = 0

        def restore(self, _candidate):
            self.calls += 1
            return malformed

    backend = Backend()
    with FrameworkState(database) as owner:
        for _ in range(2):
            result = restore_curation_action(
                database, action_id, backend=backend,
                confirmation=preview["confirmation"], actor="victor", state=owner,
            )
            assert result.status == "recovery_required"
            assert owner._connection.execute(
                "SELECT status FROM file_actions WHERE action_type='restore_curation'"
            ).fetchall() == [("recovery_required",)]
    assert backend.calls == 1


@pytest.mark.parametrize("change", ["content", "same_bytes_new_inode", "symlink", "missing", "trash", "info"])
def test_restore_replay_reobserves_identity_bytes_and_cleanup(tmp_path, change):
    _state, _corpus, trash, database, grant, _run_id, action_id = _apply_fixture(tmp_path)
    preview = restore_curation_preview(database, action_id)

    class Backend(PosixRestoreBackend):
        calls = 0

        def restore(self, candidate):
            self.calls += 1
            return super().restore(candidate)

    backend = Backend(trash)
    first = restore_curation_action(
        database, action_id, backend=backend,
        confirmation=preview["confirmation"], actor="victor",
    )
    assert first.status == "restored"
    source = Path(grant.authorized_effects[0].source.path)
    if change == "content":
        old_stat = source.stat()
        source.write_bytes(b"fake")
        os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    elif change in {"same_bytes_new_inode", "symlink"}:
        preserved = source.with_suffix(".preserved")
        source.rename(preserved)
        if change == "symlink":
            source.symlink_to(preserved)
        else:
            shutil.copy2(preserved, source)
    elif change == "missing":
        source.unlink()
    elif change == "trash":
        Path(preview["trash_path"]).write_bytes(b"same")
    else:
        Path(preview["info_path"]).write_text("stale", encoding="utf-8")
    for _ in range(2):
        replay = restore_curation_action(
            database, action_id, backend=backend,
            confirmation=preview["confirmation"], actor="victor",
        )
        assert replay.status == "recovery_required"
    assert backend.calls == 1


@pytest.mark.parametrize("change", ["missing_trash", "missing_info", "wrong_info", "replacement", "hardlink", "symlink_parent", "fifo_info"])
def test_preview_requires_complete_live_trash_evidence(tmp_path, change):
    _state, _corpus, trash, database, _grant, _run_id, action_id = _apply_fixture(tmp_path)
    preview = restore_curation_preview(database, action_id)
    target, info = Path(preview["trash_path"]), Path(preview["info_path"])
    if change == "missing_trash":
        target.unlink()
    elif change == "missing_info":
        info.unlink()
    elif change == "wrong_info":
        info.write_text("[Trash Info]\nPath=/not-the-source\n", encoding="utf-8")
    elif change == "replacement":
        preserved = target.with_suffix(".preserved")
        target.rename(preserved)
        shutil.copy2(preserved, target)
    elif change == "hardlink":
        os.link(target, target.with_suffix(".linked"))
    elif change == "fifo_info":
        info.unlink()
        os.mkfifo(info)
    else:
        (trash / "files").rename(trash / "preserved-files")
        (trash / "files").symlink_to(trash / "preserved-files", target_is_directory=True)
    before = database.read_bytes()
    assert restore_curation_preview(database, action_id)["restorable"] is False
    assert database.read_bytes() == before


@pytest.mark.parametrize("change", ["receipt_inode", "wrong_info", "replacement", "layout", "birthtime"])
def test_apply_success_receipt_must_prove_exact_trashed_source(tmp_path, change):
    state, corpus, trash, database, grant_id = _fixture(tmp_path)

    class Backend(FixtureTrashBackend):
        def apply(self, candidate):
            outcome = super().apply(candidate)
            receipt = json.loads(outcome.receipt_json)
            target = Path(receipt["trash"]["trash_path"])
            if change == "receipt_inode":
                receipt["trash"]["file_id"] = "0"
            elif change == "wrong_info":
                Path(receipt["trash"]["info_path"]).write_text(
                    "[Trash Info]\nPath=/another-source\n", encoding="utf-8"
                )
            elif change == "replacement":
                preserved = target.with_suffix(".preserved")
                target.rename(preserved)
                shutil.copy2(preserved, target)
                receipt["trash"]["file_id"] = f"{target.stat().st_ino:x}"
            elif change == "layout":
                receipt["trash"]["trash_root"] = str(trash.parent)
            else:
                receipt["trash"]["birthtime_ns"] = candidate.effect.source.birthtime_ns + 1
            return replace(outcome, receipt_json=json.dumps(receipt))

    backend = Backend(trash, structured=True)
    with FrameworkState(database) as owner:
        run_id = begin_signed_normal_run(owner, corpus)
        result = apply_authorization_grant(
            state, database, grant_id, run_id=run_id, backend=backend,
            state=owner, clock_ns=lambda: 4_000,
        )
        assert result.status == "recovery_required"
        replay = apply_authorization_grant(
            state, database, grant_id, run_id=run_id, backend=backend,
            state=owner, clock_ns=lambda: 4_000,
        )
        assert replay.status == "recovery_required"
    assert backend.calls == 1


def test_apply_replay_rejects_same_bytes_at_new_trash_identity(tmp_path):
    state, _corpus, _trash, database, grant, run_id, action_id = _apply_fixture(tmp_path)
    preview = restore_curation_preview(database, action_id)
    target = Path(preview["trash_path"])
    preserved = target.with_suffix(".preserved")
    target.rename(preserved)
    shutil.copy2(preserved, target)
    with FrameworkState(database) as owner, pytest.raises(CurationApplicationError):
        apply_authorization_grant(
            state, database, grant.grant_id, run_id=run_id,
            backend=SimpleNamespace(apply=lambda *_: pytest.fail("replay must not apply")),
            state=owner, clock_ns=lambda: 4_000,
        )


def test_reconciliation_reports_hash_race_without_raising(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"same")
    snapshot = snapshot_path(source)
    expected = reconciliation._ExpectedIdentity(
        snapshot.volume_id, snapshot.file_id, snapshot.size, snapshot.mtime_ns,
        snapshot.birthtime_ns, "xxh3_128_full_v1:" + full_fingerprint(snapshot).hex(),
    )

    def raced(_snapshot):
        raise FileChangedError("fixture concurrent change")

    monkeypatch.setattr(reconciliation, "full_fingerprint", raced)
    assert reconciliation._observe_path(str(source), expected)[0] == "error"


@pytest.mark.parametrize("malformed", [object(), SimpleNamespace(returncode=1), SimpleNamespace(returncode=True)])
def test_kio_malformed_runner_outcome_is_ambiguous(tmp_path, malformed):
    source = tmp_path / "source"
    source.write_bytes(b"same")
    client = tmp_path / "kioclient5"
    client.write_text("fixture only", encoding="utf-8")
    client.chmod(0o700)
    config = tmp_path / "config"
    config.mkdir()
    result = move_to_trash(
        source, snapshot_path(source), verifier=lambda *_: pytest.fail("must not verify"),
        runner=lambda *_args, **_kwargs: malformed,
        which=lambda name: str(client) if name == "kioclient5" else None,
        environment={"XDG_CONFIG_HOME": str(config)},
    )
    assert result.status is KioTrashStatus.RECOVERY_REQUIRED


def test_authorization_reissue_does_not_bypass_changed_physical_evidence(tmp_path):
    state, _corpus, _trash, database, grant, _run_id, _action_id = _apply_fixture(tmp_path)
    before = database.read_bytes()
    with pytest.raises(CurationAuthorizationError):
        authorize_curation_items(
            state, database, plan_digest=grant.plan_digest, item_ids=grant.item_ids,
            action=grant.action, actor=grant.actor, expires_ns=grant.expires_ns,
            max_bytes=grant.max_bytes, authorization_key=grant.authorization_key,
            clock_ns=lambda: 4_000,
        )
    assert database.read_bytes() == before
    assert read_authorization_grant(database, authorization_key=grant.authorization_key) == grant


@pytest.mark.parametrize("field,value", [("status", []), ("reason", 1), ("receipt_json", {})])
def test_apply_revalidates_nominal_outcome_values(tmp_path, field, value):
    state, corpus, _trash, database, grant_id = _fixture(tmp_path)
    malformed = BackendOutcome("blocked", "fixture")
    object.__setattr__(malformed, field, value)
    with FrameworkState(database) as owner:
        result = apply_authorization_grant(
            state, database, grant_id, run_id=begin_signed_normal_run(owner, corpus),
            backend=SimpleNamespace(apply=lambda *_: malformed), state=owner,
            clock_ns=lambda: 4_000,
        )
        assert result.status == "recovery_required"
        assert owner._connection.execute("SELECT status FROM file_actions").fetchall() == [
            ("recovery_required",)
        ]


@pytest.mark.parametrize("field,value", [("status", []), ("reason", 1), ("action_id", 999), ("idempotent", True)])
def test_restore_revalidates_nominal_outcome_values_and_action_binding(tmp_path, field, value):
    _state, _corpus, _trash, database, _grant, _run_id, action_id = _apply_fixture(tmp_path)
    preview = restore_curation_preview(database, action_id)

    def restore(candidate):
        malformed = RestoreOutcome(candidate.action_id, "blocked", "fixture")
        object.__setattr__(malformed, field, value)
        return malformed

    with FrameworkState(database) as owner:
        result = restore_curation_action(
            database, action_id, backend=SimpleNamespace(restore=restore),
            confirmation=preview["confirmation"], actor="victor", state=owner,
        )
        assert result.status == "recovery_required"
        assert owner._connection.execute(
            "SELECT status FROM file_actions WHERE action_type='restore_curation'"
        ).fetchall() == [("recovery_required",)]


@pytest.mark.parametrize("change", ["new_inode", "retained_info", "retained_trash"])
def test_restore_nominal_success_requires_original_identity_and_cleanup(tmp_path, change):
    _state, _corpus, trash, database, _grant, _run_id, action_id = _apply_fixture(tmp_path)
    preview = restore_curation_preview(database, action_id)

    class Backend(PosixRestoreBackend):
        def restore(self, candidate):
            outcome = super().restore(candidate)
            assert outcome.status == "restored"
            source = Path(candidate.effect.source.path)
            if change == "new_inode":
                preserved = source.with_suffix(".preserved")
                source.rename(preserved)
                shutil.copy2(preserved, source)
            elif change == "retained_info":
                candidate.info_path.write_text("retained", encoding="utf-8")
            else:
                candidate.trash_path.write_bytes(b"same")
            return outcome

    with FrameworkState(database) as owner:
        result = restore_curation_action(
            database, action_id, backend=Backend(trash),
            confirmation=preview["confirmation"], actor="victor", state=owner,
        )
        assert result.status == "recovery_required"
        assert result.reason == "restore_postcondition_failed"
        assert owner._connection.execute(
            "SELECT status FROM file_actions WHERE action_type='restore_curation'"
        ).fetchall() == [("recovery_required",)]


def test_restore_receipt_persistence_exception_is_recovery_without_retry(tmp_path, monkeypatch):
    _state, _corpus, trash, database, grant, _run_id, action_id = _apply_fixture(tmp_path)
    preview = restore_curation_preview(database, action_id)

    def persist_failed(*_args):
        raise RuntimeError("fixture persistence interruption")

    with FrameworkState(database) as owner:
        monkeypatch.setattr(type(owner), "confirm_file_actions_applied", persist_failed)
        first = restore_curation_action(
            database, action_id, backend=PosixRestoreBackend(trash),
            confirmation=preview["confirmation"], actor="victor", state=owner,
        )
        assert first.reason == "restore_receipt_persistence_failed"
        replay = restore_curation_action(
            database, action_id,
            backend=SimpleNamespace(restore=lambda *_: pytest.fail("must not retry")),
            confirmation=preview["confirmation"], actor="victor", state=owner,
        )
        assert replay.status == "recovery_required"
        expected = json.loads(owner._connection.execute(
            "SELECT expected_identity_json FROM file_actions WHERE action_type='restore_curation'"
        ).fetchone()[0])
        assert expected["source_digest"] == grant.authorized_effects[0].source_digest
    source = Path(grant.authorized_effects[0].source.path)
    old_stat = source.stat()
    source.write_bytes(b"fake")
    os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    observations = reconciliation.list_file_action_reconciliations(database, limit=10)
    assert len(observations) == 1
    assert observations[0].classification == "ambiguous"


@pytest.mark.parametrize("change", ["replacement", "wrong_info", "layout", "hardlink", "birthtime", "hash_race"])
def test_reconciler_never_confirms_unbound_trash_evidence(tmp_path, change, monkeypatch):
    _state, _corpus, trash, database, grant, run_id, action_id = _apply_fixture(tmp_path)
    with FrameworkState(database) as owner:
        raw = owner._connection.execute(
            "SELECT effect_receipt_json FROM file_actions WHERE action_id=?", (action_id,)
        ).fetchone()[0]
    receipt = json.loads(raw)
    target = Path(receipt["trash"]["trash_path"])
    if change == "replacement":
        preserved = target.with_suffix(".preserved")
        target.rename(preserved)
        shutil.copy2(preserved, target)
        receipt["trash"]["file_id"] = f"{target.stat().st_ino:x}"
    elif change == "wrong_info":
        Path(receipt["trash"]["info_path"]).write_text("[Trash Info]\nPath=/wrong\n", encoding="utf-8")
    elif change == "layout":
        receipt["trash"]["trash_root"] = str(trash.parent)
    elif change == "hardlink":
        os.link(target, target.with_suffix(".linked"))
    elif change == "birthtime":
        real_snapshot = kio_trash.snapshot_path
        monkeypatch.setattr(kio_trash, "snapshot_path", lambda path: replace(
            real_snapshot(path), birthtime_ns=real_snapshot(path).birthtime_ns + 1,
        ))
    else:
        def raced(_snapshot):
            raise FileChangedError("fixture concurrent change")

        monkeypatch.setattr(kio_trash, "full_fingerprint", raced)
    effect = grant.authorized_effects[0]
    source = effect.source
    expected = reconciliation._ExpectedIdentity(
        source.volume_id, source.file_id, source.size, source.mtime_ns,
        source.birthtime_ns, effect.source_digest,
    )
    action = reconciliation._RecordedAction(
        action_id, run_id, None, "trash_curation", source.path, None, "recovery_required",
    )
    assert reconciliation._valid_success_receipt(
        json.dumps(receipt), operation="trash", action=action, expected=expected,
    ) is False


def test_apply_nonfinite_receipt_is_recovery_not_an_unhandled_canonicalization_error(tmp_path):
    state, corpus, trash, database, grant_id = _fixture(tmp_path)

    class Backend(FixtureTrashBackend):
        def apply(self, candidate):
            outcome = super().apply(candidate)
            receipt = json.loads(outcome.receipt_json)
            receipt["untrusted_extra"] = float("nan")
            return replace(outcome, receipt_json=json.dumps(receipt))

    backend = Backend(trash)
    with FrameworkState(database) as owner:
        run_id = begin_signed_normal_run(owner, corpus)
        for _ in range(2):
            result = apply_authorization_grant(
                state, database, grant_id, run_id=run_id, backend=backend,
                state=owner, clock_ns=lambda: 4_000,
            )
            assert result.status == "recovery_required"
            assert owner._connection.execute("SELECT status FROM file_actions").fetchall() == [
                ("recovery_required",)
            ]
    assert backend.calls == 1


@pytest.mark.parametrize("field,value", [("source_absent", "yes"), ("trash_evidence", object()), ("unset_source", None)])
def test_kio_nominal_verification_requires_typed_evidence(tmp_path, field, value):
    source = tmp_path / "source"
    source.write_bytes(b"same")
    expected = snapshot_path(source)
    client = tmp_path / "kioclient5"
    client.write_text("fixture only", encoding="utf-8")
    client.chmod(0o700)
    config = tmp_path / "config"
    config.mkdir()
    verification = kio_trash.KioTrashVerification(True, "fixture receipt")
    if field == "unset_source":
        object.__delattr__(verification, "source_absent")
    else:
        object.__setattr__(verification, field, value)

    def runner(command, **_kwargs):
        source.rename(tmp_path / "moved-source")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = move_to_trash(
        source, expected, verifier=lambda *_: verification, runner=runner,
        which=lambda name: str(client) if name == "kioclient5" else None,
        environment={"XDG_CONFIG_HOME": str(config)},
    )
    assert result.status is KioTrashStatus.RECOVERY_REQUIRED
