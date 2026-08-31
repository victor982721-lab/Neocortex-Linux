"""Focused Linux mutation and KIO backend regressions."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from neocortex.deduplication import snapshot_path
from neocortex.safety import kio_trash
from neocortex.safety.kio_trash import KioTrashEffectUncertain, KioTrashReceipt
from neocortex.safety.posix_mutation import (
    IdentityBoundMutationError,
    UnsupportedIdentityBoundMutation,
    rename_no_replace_by_identity,
)
from neocortex.documents import document_organization_application as organization
from neocortex.safety.corpus_access import CorpusAccessPolicy, CorpusMutationGuard
from tests.internal_paths_test_support import disjoint_internal_paths_policy


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux POSIX backend")


def test_posix_rename_no_replace_moves_and_preserves_identity(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("payload", encoding="utf-8")
    expected = snapshot_path(source)

    receipt = rename_no_replace_by_identity(
        source,
        destination,
        expected,
        before_native_call=lambda: None,
    )

    assert receipt.file_system == "POSIX"
    assert receipt.guarantee == "verified_no_replace"
    assert receipt.volume_id == expected.volume_id
    assert receipt.file_id == expected.file_id
    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "payload"


def test_posix_rename_rejects_destination_collision(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source", encoding="utf-8")
    destination.write_text("keeper", encoding="utf-8")

    with pytest.raises(FileExistsError):
        rename_no_replace_by_identity(
            source,
            destination,
            snapshot_path(source),
            before_native_call=lambda: None,
        )

    assert source.read_text(encoding="utf-8") == "source"
    assert destination.read_text(encoding="utf-8") == "keeper"


def test_posix_rename_rejects_source_change_before_frontier(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("before", encoding="utf-8")
    expected = snapshot_path(source)
    source.write_text("after", encoding="utf-8")

    with pytest.raises(IdentityBoundMutationError):
        rename_no_replace_by_identity(
            source,
            destination,
            expected,
            before_native_call=lambda: None,
        )

    assert source.exists()
    assert not destination.exists()


def test_posix_rename_abstains_when_syscall_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("payload", encoding="utf-8")
    monkeypatch.setattr(
        "neocortex.safety.posix_mutation._renameat2_function",
        lambda: None,
    )

    with pytest.raises(UnsupportedIdentityBoundMutation, match="renameat2"):
        rename_no_replace_by_identity(
            source,
            destination,
            snapshot_path(source),
            before_native_call=lambda: None,
        )


def test_organization_consumer_uses_posix_no_replace_move(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("payload", encoding="utf-8")
    organization_root = tmp_path / "organized"
    organization_root.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    destination = organization_root / "target.txt"
    guard = CorpusMutationGuard(
        CorpusAccessPolicy.capture("normal", organization_root),
        disjoint_internal_paths_policy(organization_root),
    )

    status, detail = organization._move_organization_source(
        source,
        destination,
        snapshot_path(source),
        state_directory,
        organization_root,
        os.stat(organization_root, follow_symlinks=False),
        guard,
    )

    assert status == "moved"
    assert "POSIX no-replace" in detail
    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "payload"


def test_kio_move_uses_observed_trash_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "candidate.txt"
    source.write_text("payload", encoding="utf-8")
    expected = snapshot_path(source)
    listings = [("existing.txt",), ("existing.txt", "0-candidate.txt")]

    monkeypatch.setattr(kio_trash, "list_trash_entries", lambda _client: listings.pop(0))

    def fake_run(
        _client: str,
        *arguments: str,
        timeout: float = 120.0,
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        assert arguments[:2] == ("move", str(source))
        source.unlink()
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(kio_trash, "_run_kio", fake_run)
    receipt = kio_trash.move_to_trash(
        source,
        expected,
        before_native_call=lambda: None,
        client="/usr/bin/kioclient5",
        client_version="kioclient5 test",
    )

    assert isinstance(receipt, KioTrashReceipt)
    assert receipt.trash_entry == "0-candidate.txt"
    assert receipt.guarantee == "reversible_path_bound"


def test_kio_move_rejects_ambiguous_listing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate.txt"
    source.write_text("payload", encoding="utf-8")
    expected = snapshot_path(source)
    listings = [("existing.txt",), ("existing.txt", "0-candidate.txt", "1-candidate.txt")]
    monkeypatch.setattr(kio_trash, "list_trash_entries", lambda _client: listings.pop(0))

    def fake_run(_client: str, *arguments: str, timeout: float = 120.0):
        del timeout
        source.unlink()
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(kio_trash, "_run_kio", fake_run)
    with pytest.raises(KioTrashEffectUncertain):
        kio_trash.move_to_trash(
            source,
            expected,
            before_native_call=lambda: None,
            client="/usr/bin/kioclient5",
        )


def test_real_kio_self_test_is_opt_in(tmp_path: Path) -> None:
    if os.environ.get("NEOCORTEX_KIO_REAL_TEST") != "1":
        pytest.skip("set NEOCORTEX_KIO_REAL_TEST=1 for the local KIO integration")
    receipt = kio_trash.run_self_test(parent=tmp_path, receipt_path=tmp_path / "receipt.json")
    assert receipt.result == "passed"
    assert receipt.fixture_sha256
