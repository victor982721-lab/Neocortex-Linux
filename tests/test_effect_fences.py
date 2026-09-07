"""Focused regressions for executable, directory and root effect fences."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import neocortex.curation.application as application
import neocortex.safety.kio_trash as kio_trash
from neocortex.curation.application import ApplyCandidate, PosixRenameBackend
from neocortex.deduplication import full_fingerprint, snapshot_path
from neocortex.deduplication.domain.errors import InventoryError
from neocortex.deduplication.inventory.traversal import RootIdentity, validate_inventory_root
from neocortex.safety.kio_trash import KioTrashStatus, KioTrashVerification, move_to_trash
from neocortex.workflow.authorization.contracts import AuthorizationEffect


def _client(path: Path) -> Path:
    path.write_text("fixture executable", encoding="utf-8")
    path.chmod(0o700)
    return path


def _rename_candidate(root: Path) -> ApplyCandidate:
    source = root / "source" / "payload.bin"
    target = root / "target" / "renamed.bin"
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_bytes(b"rename fence fixture")
    snapshot = snapshot_path(source)
    digest = "xxh3_128_full_v1:" + full_fingerprint(snapshot).hex()
    effect = AuthorizationEffect(
        effect_id="effect:fence",
        item_id="item:fence",
        task_id="task:fence",
        ordinal=1,
        action="rename",
        kind="organization_plan",
        source=snapshot,
        source_digest=digest,
        target_path=str(target),
    )
    return ApplyCandidate("grant:fence", "sha256:" + "0" * 64, root, effect)


def test_kio_rejects_replaced_executable_before_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    expected = snapshot_path(source)
    client = _client(tmp_path / "kioclient5")
    config = tmp_path / "config"
    config.mkdir()
    original_preflight = kio_trash.preflight_kio_trash

    def raced_preflight(**kwargs: object):
        preflight = original_preflight(**kwargs)
        replacement = tmp_path / "replacement-client"
        _client(replacement)
        replacement.replace(client)
        return preflight

    monkeypatch.setattr(kio_trash, "preflight_kio_trash", raced_preflight)
    calls: list[object] = []
    result = move_to_trash(
        source,
        expected,
        verifier=lambda *_: pytest.fail("verification must remain unreachable"),
        runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        which=lambda name: str(client) if name == "kioclient5" else None,
        environment={"XDG_CONFIG_HOME": str(config)},
    )

    assert result.status is KioTrashStatus.BLOCKED
    assert result.reason == "kio_client_changed"
    assert calls == []
    assert source.exists()


def test_real_kio_backend_remains_fail_closed_without_injected_runner() -> None:
    outcome = application.KioTrashBackend(
        verifier=lambda *_: pytest.fail("real KIO must not be reached")
    ).apply(SimpleNamespace(effect=SimpleNamespace(action="trash")))
    assert outcome.status == "blocked"
    assert outcome.reason == "kio_runner_not_injected"


def test_kio_directory_fsync_failure_is_not_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    expected = snapshot_path(source)
    client = _client(tmp_path / "kioclient5")
    config = tmp_path / "config"
    config.mkdir()

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        source.unlink()
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(
        kio_trash,
        "_fsync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("fixture fsync failure")),
    )
    result = move_to_trash(
        source,
        expected,
        verifier=lambda *_: KioTrashVerification(True, "fixture evidence"),
        runner=runner,
        which=lambda name: str(client) if name == "kioclient5" else None,
        environment={"XDG_CONFIG_HOME": str(config)},
    )

    assert result.status is KioTrashStatus.RECOVERY_REQUIRED
    assert result.reason == "kio_directory_fsync_failed"


def test_posix_rename_flushes_both_parent_directories_before_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    candidate = _rename_candidate(root)
    real_fsync = application.os.fsync
    flushed: list[str] = []

    def traced_fsync(descriptor: int) -> None:
        flushed.append(os.readlink(f"/proc/self/fd/{descriptor}"))
        real_fsync(descriptor)

    monkeypatch.setattr(application.os, "fsync", traced_fsync)
    outcome = PosixRenameBackend().apply(candidate)
    if outcome.reason == "renameat2_unavailable":
        pytest.skip("host does not expose renameat2")

    assert outcome.status == "applied"
    assert str(root / "source") in flushed
    assert str(root / "target") in flushed


def test_posix_rename_fsync_failure_after_syscall_requires_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    candidate = _rename_candidate(root)
    monkeypatch.setattr(application.os, "fsync", lambda _descriptor: (_ for _ in ()).throw(OSError("fixture fsync failure")))
    outcome = PosixRenameBackend().apply(candidate)
    if outcome.reason == "renameat2_unavailable":
        pytest.skip("host does not expose renameat2")

    assert outcome.status == "recovery_required"
    assert not Path(candidate.effect.source.path).exists()
    assert Path(candidate.effect.target_path or "").exists()


@pytest.mark.parametrize("root", (None, b"/tmp", 42, object()))
def test_inventory_root_rejects_non_text_path_types(root: object) -> None:
    with pytest.raises(InventoryError, match="inventory root"):
        validate_inventory_root(root)  # type: ignore[arg-type]


def test_inventory_root_and_identity_reject_non_directory(tmp_path: Path) -> None:
    file_root = tmp_path / "not-a-root"
    file_root.write_bytes(b"file")
    with pytest.raises(InventoryError, match="not a directory"):
        validate_inventory_root(file_root)

    metadata = os.lstat(file_root)
    identity = RootIdentity(
        str(file_root),
        metadata.st_dev,
        metadata.st_ino,
        -1,
    )
    with pytest.raises(InventoryError, match="real directory"):
        identity.verify_unchanged()
