"""Focused tests for the prepared KIO trash primitive.

No test invokes a real KIO client.  All process effects and trash evidence are
provided by explicit fixture doubles.
"""

from __future__ import annotations

import os
import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest

from neocortex.deduplication import FULL_ALGORITHM, FileSnapshot, full_fingerprint, snapshot_path
from neocortex.safety import kio_trash
from neocortex.safety.kio_trash import (
    KIO_TRASH_URL,
    KioTrashBatchItem,
    KioTrashStatus,
    KioTrashBatchResult,
    KioTrashVerification,
    discover_kio_client,
    move_many_to_trash,
    move_to_trash,
)


TEST_CAPABILITIES = ("base", 'platform')
pytestmark = pytest.mark.capability("base", 'platform')


class RunnerSpy:
    def __init__(
        self,
        effect: Callable[[Sequence[str], Mapping[str, object]], subprocess.CompletedProcess[str]],
    ) -> None:
        self.effect = effect
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(
        self,
        command: Sequence[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        captured = (list(command), dict(kwargs))
        self.calls.append(captured)
        return self.effect(*captured)


def _executable(path: Path) -> Path:
    path.write_text("fixture executable; never invoked", encoding="utf-8")
    path.chmod(0o700)
    return path


def _source(path: Path, payload: bytes = b"fixture payload") -> tuple[Path, FileSnapshot]:
    path.write_bytes(payload)
    return path, snapshot_path(path)


def _environment(config_home: Path) -> dict[str, str]:
    return {"XDG_CONFIG_HOME": str(config_home)}


def _which_for(client: Path) -> Callable[[str], str | None]:
    return lambda name: str(client) if name == "kioclient5" else None


def _never_verify(
    _source: Path,
    _expected: FileSnapshot,
    _client: Path,
) -> KioTrashVerification:
    raise AssertionError("verifier must remain unreachable")


def _batch_digest(expected: FileSnapshot) -> str:
    return FULL_ALGORITHM + ":" + full_fingerprint(expected).hex()


def _batch_evidence(
    trash: Path,
    source: Path,
    expected: FileSnapshot,
    digest: str,
) -> str:
    target = trash / "files" / source.name
    info = trash / "info" / (target.name + ".trashinfo")
    return json.dumps(
        {
            "trash_root": str(trash),
            "trash_path": str(target),
            "info_path": str(info),
            "volume_id": f"{expected.volume_id:x}",
            "file_id": f"{target.stat().st_ino:x}",
            "size": expected.size,
            "digest": digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def test_client_resolution_uses_required_priority_order(tmp_path: Path) -> None:
    client5 = _executable(tmp_path / "kioclient5")
    generic = _executable(tmp_path / "kioclient")
    calls: list[str] = []

    def which(name: str) -> str | None:
        calls.append(name)
        return {
            "kioclient5": str(client5),
            "kioclient": str(generic),
        }.get(name)

    assert discover_kio_client(which=which) == client5
    assert calls == ["kioclient6", "kioclient5"]


def test_absolute_kioclient6_wins_without_probing_lower_priority(tmp_path: Path) -> None:
    client6 = _executable(tmp_path / "kioclient6")
    calls: list[str] = []

    def which(name: str) -> str | None:
        calls.append(name)
        return str(client6) if name == "kioclient6" else None

    assert discover_kio_client(which=which) == client6
    assert calls == ["kioclient6"]


def test_unwritable_config_home_abstains_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    client = _executable(tmp_path / "kioclient5")
    source, expected = _source(tmp_path / "source.bin")
    runner = RunnerSpy(lambda *_args: pytest.fail("subprocess must not start"))
    real_access = os.access

    def access(path: str | os.PathLike[str], mode: int) -> bool:
        if Path(path) == config_home and mode == os.W_OK | os.X_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(kio_trash.os, "access", access)
    result = move_to_trash(
        source,
        expected,
        verifier=_never_verify,
        runner=runner,
        which=_which_for(client),
        environment=_environment(config_home),
    )

    assert result.status is KioTrashStatus.BLOCKED
    assert result.reason == "kio_config_home_unwritable"
    assert runner.calls == []
    assert source.exists()


def test_existing_unwritable_kio_config_file_abstains_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    config_file = config_home / "kioclient5rc"
    config_file.write_text("[KIO]\n", encoding="utf-8")
    client = _executable(tmp_path / "kioclient5")
    source, expected = _source(tmp_path / "source.bin")
    runner = RunnerSpy(lambda *_args: pytest.fail("subprocess must not start"))
    real_access = os.access

    def access(path: str | os.PathLike[str], mode: int) -> bool:
        if Path(path) == config_file and mode == os.W_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(kio_trash.os, "access", access)
    result = move_to_trash(
        source,
        expected,
        verifier=_never_verify,
        runner=runner,
        which=_which_for(client),
        environment=_environment(config_home),
    )

    assert result.status is KioTrashStatus.BLOCKED
    assert result.reason == "kio_config_file_unwritable"
    assert runner.calls == []
    assert config_file.read_text(encoding="utf-8") == "[KIO]\n"


def test_relative_config_home_abstains_before_subprocess(tmp_path: Path) -> None:
    client = _executable(tmp_path / "kioclient5")
    source, expected = _source(tmp_path / "source.bin")
    runner = RunnerSpy(lambda *_args: pytest.fail("subprocess must not start"))

    result = move_to_trash(
        source,
        expected,
        verifier=_never_verify,
        runner=runner,
        which=_which_for(client),
        environment={"XDG_CONFIG_HOME": "relative-config"},
    )

    assert result.status is KioTrashStatus.BLOCKED
    assert result.reason == "kio_config_home_not_absolute"
    assert runner.calls == []


def test_unwritable_parent_of_missing_config_home_abstains_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_parent = tmp_path / "config-parent"
    config_parent.mkdir()
    config_home = config_parent / "config"
    client = _executable(tmp_path / "kioclient5")
    source, expected = _source(tmp_path / "source.bin")
    runner = RunnerSpy(lambda *_args: pytest.fail("subprocess must not start"))
    real_access = os.access

    def access(path: str | os.PathLike[str], mode: int) -> bool:
        if Path(path) == config_parent and mode == os.W_OK | os.X_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(kio_trash.os, "access", access)
    result = move_to_trash(
        source,
        expected,
        verifier=_never_verify,
        runner=runner,
        which=_which_for(client),
        environment=_environment(config_home),
    )

    assert result.status is KioTrashStatus.BLOCKED
    assert result.reason == "kio_config_parent_unwritable"
    assert runner.calls == []
    assert not config_home.exists()


def test_writable_parent_allows_missing_config_without_preflight_writes(tmp_path: Path) -> None:
    config_parent = tmp_path / "config-parent"
    config_parent.mkdir()
    config_home = config_parent / "config"
    client = _executable(tmp_path / "kioclient5")
    source, expected = _source(tmp_path / "source.bin")

    def effect(
        command: Sequence[str],
        _kwargs: Mapping[str, object],
    ) -> subprocess.CompletedProcess[str]:
        assert not config_home.exists()
        source.unlink()
        return subprocess.CompletedProcess(command, 0, "", "")

    result = move_to_trash(
        source,
        expected,
        verifier=lambda *_args: KioTrashVerification(True, "trash-entry=fixture"),
        runner=RunnerSpy(effect),
        which=_which_for(client),
        environment=_environment(config_home),
    )

    assert result.status is KioTrashStatus.APPLIED
    assert not config_home.exists()


def test_relative_source_abstains_before_subprocess(tmp_path: Path) -> None:
    client = _executable(tmp_path / "kioclient5")
    config_home = tmp_path / "config"
    config_home.mkdir()
    expected = FileSnapshot("relative.bin", 1, 2, 3, 4, -1)
    runner = RunnerSpy(lambda *_args: pytest.fail("subprocess must not start"))

    result = move_to_trash(
        "relative.bin",
        expected,
        verifier=_never_verify,
        runner=runner,
        which=_which_for(client),
        environment=_environment(config_home),
    )

    assert result.status is KioTrashStatus.BLOCKED
    assert result.reason == "kio_source_not_absolute"
    assert runner.calls == []


def test_symlink_source_abstains_before_subprocess(tmp_path: Path) -> None:
    target, _target_snapshot = _source(tmp_path / "target.bin")
    source = tmp_path / "source.bin"
    source.symlink_to(target)
    expected = snapshot_path(source)
    config_home = tmp_path / "config"
    config_home.mkdir()
    client = _executable(tmp_path / "kioclient5")
    runner = RunnerSpy(lambda *_args: pytest.fail("subprocess must not start"))

    result = move_to_trash(
        source,
        expected,
        verifier=_never_verify,
        runner=runner,
        which=_which_for(client),
        environment=_environment(config_home),
    )

    assert result.status is KioTrashStatus.BLOCKED
    assert result.reason == "kio_source_symlink"
    assert runner.calls == []


def test_special_source_abstains_before_subprocess(tmp_path: Path) -> None:
    source = tmp_path / "pipe"
    os.mkfifo(source)
    expected = snapshot_path(source)
    config_home = tmp_path / "config"
    config_home.mkdir()
    client = _executable(tmp_path / "kioclient5")
    runner = RunnerSpy(lambda *_args: pytest.fail("subprocess must not start"))

    result = move_to_trash(
        source,
        expected,
        verifier=_never_verify,
        runner=runner,
        which=_which_for(client),
        environment=_environment(config_home),
    )

    assert result.status is KioTrashStatus.BLOCKED
    assert result.reason == "kio_source_not_regular"
    assert runner.calls == []


def test_hardlinked_source_abstains_before_subprocess(tmp_path: Path) -> None:
    source, _ = _source(tmp_path / "source.bin")
    alias = tmp_path / "alias.bin"
    os.link(source, alias)
    expected = snapshot_path(source)
    config_home = tmp_path / "config"
    config_home.mkdir()
    client = _executable(tmp_path / "kioclient5")
    runner = RunnerSpy(lambda *_args: pytest.fail("subprocess must not start"))

    result = move_to_trash(
        source,
        expected,
        verifier=_never_verify,
        runner=runner,
        which=_which_for(client),
        environment=_environment(config_home),
    )

    assert result.status is KioTrashStatus.BLOCKED
    assert result.reason == "kio_source_hardlink"
    assert runner.calls == []
    assert source.exists() and alias.exists()


def test_source_is_revalidated_immediately_before_effect(tmp_path: Path) -> None:
    source, expected = _source(tmp_path / "source.bin")
    config_home = tmp_path / "config"
    config_home.mkdir()
    client = _executable(tmp_path / "kioclient5")
    runner = RunnerSpy(lambda *_args: pytest.fail("subprocess must not start"))
    mutated = False

    def which(name: str) -> str | None:
        nonlocal mutated
        if name != "kioclient5":
            return None
        if not mutated:
            source.write_bytes(b"changed after initial source admission")
            mutated = True
        return str(client)

    result = move_to_trash(
        source,
        expected,
        verifier=_never_verify,
        runner=runner,
        which=which,
        environment=_environment(config_home),
    )

    assert mutated
    assert result.status is KioTrashStatus.BLOCKED
    assert result.reason == "kio_source_changed"
    assert runner.calls == []


def test_exact_command_has_no_shell_and_success_receipt_keeps_snapshot(tmp_path: Path) -> None:
    source, expected = _source(tmp_path / "source with spaces.bin")
    config_home = tmp_path / "config"
    config_home.mkdir()
    client = _executable(tmp_path / "kioclient5")

    def effect(
        command: Sequence[str],
        _kwargs: Mapping[str, object],
    ) -> subprocess.CompletedProcess[str]:
        source.unlink()
        return subprocess.CompletedProcess(command, 0, "", "")

    runner = RunnerSpy(effect)

    def verifier(
        verified_source: Path,
        verified_snapshot: FileSnapshot,
        verified_client: Path,
    ) -> KioTrashVerification:
        assert verified_source == source
        assert verified_snapshot == expected
        assert verified_client == client
        return KioTrashVerification(True, "trash-entry=fixture-token")

    result = move_to_trash(
        source,
        expected,
        verifier=verifier,
        runner=runner,
        which=_which_for(client),
        environment=_environment(config_home),
        timeout_seconds=15,
    )

    command, kwargs = runner.calls[0]
    assert command == [str(client), "--noninteractive", "move", str(source), KIO_TRASH_URL]
    assert kwargs == {
        "check": False,
        "shell": False,
        "capture_output": True,
        "text": True,
        "timeout": 15.0,
        "env": _environment(config_home),
    }
    assert result.status is KioTrashStatus.APPLIED
    assert result.command == tuple(command)
    assert result.receipt is not None
    assert result.receipt.guarantee == "reversible_path_bound"
    assert result.receipt.volume_id == expected.volume_id
    assert result.receipt.file_id == expected.file_id
    assert result.receipt.size == expected.size
    assert result.receipt.mtime_ns == expected.mtime_ns
    assert result.receipt.birthtime_ns == expected.birthtime_ns


def test_timeout_is_always_recovery_required_and_sanitizes_diagnostic(tmp_path: Path) -> None:
    source, expected = _source(tmp_path / "source.bin")
    config_home = tmp_path / "config"
    config_home.mkdir()
    client = _executable(tmp_path / "kioclient5")

    def effect(command: Sequence[str], kwargs: Mapping[str, object]):
        raise subprocess.TimeoutExpired(
            command,
            cast(float, kwargs["timeout"]),
            stderr=("\x1b[31m" + "x" * 1_200 + "\x1b[0m\n").encode(),
        )

    runner = RunnerSpy(effect)
    result = move_to_trash(
        source,
        expected,
        verifier=_never_verify,
        runner=runner,
        which=_which_for(client),
        environment=_environment(config_home),
    )

    assert result.status is KioTrashStatus.RECOVERY_REQUIRED
    assert result.reason == "kio_timeout_effect_ambiguous"
    assert result.returncode is None
    assert result.detail is not None
    assert "\x1b" not in result.detail
    assert len(result.detail) <= kio_trash.MAX_DIAGNOSTIC_CHARS


def test_nonzero_return_is_recovery_required_without_verification(tmp_path: Path) -> None:
    source, expected = _source(tmp_path / "source.bin")
    config_home = tmp_path / "config"
    config_home.mkdir()
    client = _executable(tmp_path / "kioclient5")
    runner = RunnerSpy(
        lambda command, _kwargs: subprocess.CompletedProcess(
            command,
            17,
            "",
            "\x1b[31mfailed\x1b[0m\nsecond line",
        )
    )

    result = move_to_trash(
        source,
        expected,
        verifier=_never_verify,
        runner=runner,
        which=_which_for(client),
        environment=_environment(config_home),
    )

    assert result.status is KioTrashStatus.RECOVERY_REQUIRED
    assert result.reason == "kio_nonzero_effect_ambiguous"
    assert result.returncode == 17
    assert result.detail == "failed second line"


def test_runner_interrupt_is_recovery_required(tmp_path: Path) -> None:
    source, expected = _source(tmp_path / "source.bin")
    config_home = tmp_path / "config"
    config_home.mkdir()
    client = _executable(tmp_path / "kioclient5")

    def effect(_command: Sequence[str], _kwargs: Mapping[str, object]):
        raise KeyboardInterrupt

    result = move_to_trash(
        source,
        expected,
        verifier=_never_verify,
        runner=RunnerSpy(effect),
        which=_which_for(client),
        environment=_environment(config_home),
    )

    assert result.status is KioTrashStatus.RECOVERY_REQUIRED
    assert result.reason == "kio_process_interrupted"


def test_verifier_interrupt_after_zero_return_is_recovery_required(tmp_path: Path) -> None:
    source, expected = _source(tmp_path / "source.bin")
    config_home = tmp_path / "config"
    config_home.mkdir()
    client = _executable(tmp_path / "kioclient5")

    def effect(
        command: Sequence[str],
        _kwargs: Mapping[str, object],
    ) -> subprocess.CompletedProcess[str]:
        source.unlink()
        return subprocess.CompletedProcess(command, 0, "", "")

    def interrupted_verifier(*_args: object) -> KioTrashVerification:
        raise KeyboardInterrupt

    result = move_to_trash(
        source,
        expected,
        verifier=interrupted_verifier,
        runner=RunnerSpy(effect),
        which=_which_for(client),
        environment=_environment(config_home),
    )

    assert result.status is KioTrashStatus.RECOVERY_REQUIRED
    assert result.reason == "kio_verification_failed"


@pytest.mark.parametrize(
    ("source_absent", "trash_evidence", "remove_source"),
    (
        (False, None, False),
        (True, None, True),
        (True, "trash-entry=claimed", False),
    ),
)
def test_zero_return_without_both_observations_requires_recovery(
    tmp_path: Path,
    source_absent: bool,
    trash_evidence: str | None,
    remove_source: bool,
) -> None:
    source, expected = _source(tmp_path / "source.bin")
    config_home = tmp_path / "config"
    config_home.mkdir()
    client = _executable(tmp_path / "kioclient5")

    def effect(
        command: Sequence[str],
        _kwargs: Mapping[str, object],
    ) -> subprocess.CompletedProcess[str]:
        if remove_source:
            source.unlink()
        return subprocess.CompletedProcess(command, 0, "", "")

    runner = RunnerSpy(effect)
    result = move_to_trash(
        source,
        expected,
        verifier=lambda *_args: KioTrashVerification(source_absent, trash_evidence),
        runner=runner,
        which=_which_for(client),
        environment=_environment(config_home),
    )

    assert result.status is KioTrashStatus.RECOVERY_REQUIRED
    assert result.reason == "kio_effect_unverified"
    assert result.receipt is None


@pytest.mark.parametrize("timeout", (True, 0, -1, 0.5, 301, float("inf"), float("nan")))
def test_timeout_limits_reject_invalid_values_before_runner(
    tmp_path: Path,
    timeout: object,
) -> None:
    source, expected = _source(tmp_path / "source.bin")
    runner = RunnerSpy(lambda *_args: pytest.fail("subprocess must not start"))

    with pytest.raises((TypeError, ValueError)):
        move_to_trash(
            source,
            expected,
            verifier=_never_verify,
            runner=runner,
            timeout_seconds=timeout,  # type: ignore[arg-type]
        )

    assert runner.calls == []


def test_batch_uses_one_noninteractive_kio_process_and_returns_item_receipts(
    tmp_path: Path,
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    trash = tmp_path / "trash"
    (trash / "files").mkdir(parents=True)
    (trash / "info").mkdir()
    client = _executable(tmp_path / "kioclient5")
    sources: list[Path] = []
    items: list[KioTrashBatchItem] = []
    digests: dict[str, str] = {}
    for name in ("first.txt", "second.txt"):
        source, expected = _source(tmp_path / name, name.encode("utf-8"))
        digest = _batch_digest(expected)
        sources.append(source)
        digests[source.name] = digest
        items.append(KioTrashBatchItem(source, expected, digest))

    def effect(
        command: Sequence[str],
        _kwargs: Mapping[str, object],
    ) -> subprocess.CompletedProcess[str]:
        assert command[:4] == [str(client), "--noninteractive", "move", command[3]]
        assert command[-1] == KIO_TRASH_URL
        for argument in command[3:-1]:
            source = Path(argument)
            target = trash / "files" / source.name
            os.rename(source, target)
            (trash / "info" / (target.name + ".trashinfo")).write_text(
                f"[Trash Info]\nPath={source}\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    def verifier(
        source: Path,
        expected: FileSnapshot,
        _client: Path,
    ) -> KioTrashVerification:
        return KioTrashVerification(
            True,
            _batch_evidence(trash, source, expected, digests[source.name]),
        )

    runner = RunnerSpy(effect)
    result = move_many_to_trash(
        items,
        verifier=verifier,
        runner=runner,
        which=_which_for(client),
        environment=_environment(config_home),
        timeout_seconds=15,
    )

    assert isinstance(result, KioTrashBatchResult)
    assert len(runner.calls) == 1
    command, kwargs = runner.calls[0]
    assert command == [
        str(client),
        "--noninteractive",
        "move",
        str(sources[0]),
        str(sources[1]),
        KIO_TRASH_URL,
    ]
    assert kwargs["timeout"] == 15.0
    assert [item.status for item in result.outcomes] == [
        KioTrashStatus.APPLIED,
        KioTrashStatus.APPLIED,
    ]
    assert result.applied == 2
    assert all(item.receipt is not None for item in result.outcomes)
    assert all(not source.exists() for source in sources)
    assert all(
        f"Path={source}\n"
        in (trash / "info" / (source.name + ".trashinfo")).read_text(encoding="utf-8")
        for source in sources
    )


def test_batch_native_claims_are_shared_process_and_cleanup_is_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    trash = tmp_path / "trash"
    (trash / "files").mkdir(parents=True)
    (trash / "info").mkdir()
    client = _executable(tmp_path / "kioclient5")
    items: list[KioTrashBatchItem] = []
    digests: dict[str, str] = {}
    for name in ("first.txt", "second.txt"):
        source, expected = _source(tmp_path / name, name.encode("utf-8"))
        digest = _batch_digest(expected)
        items.append(KioTrashBatchItem(source, expected, digest))
        digests[name] = digest

    def native_runner(
        command: Sequence[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert command[0] == str(client)
        assert command[1:4] == ["--noninteractive", "move", command[3]]
        assert all(".neocortex-kio-claim-" in argument for argument in command[3:-1])
        for argument in command[3:-1]:
            claim = Path(argument)
            target = trash / "files" / claim.name
            os.rename(claim, target)
            (trash / "info" / (target.name + ".trashinfo")).write_text(
                f"[Trash Info]\nPath={claim}\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    def verifier(
        claim: Path,
        expected: FileSnapshot,
        _client: Path,
    ) -> KioTrashVerification:
        original_name = claim.name
        # The source basename survives the private claim, so the fixture can
        # bind each verifier result without observing the original path.
        return KioTrashVerification(
            True,
            _batch_evidence(trash, claim, expected, digests[original_name]),
        )

    monkeypatch.setattr(kio_trash.subprocess, "run", native_runner)
    result = move_many_to_trash(
        items,
        verifier=verifier,
        which=_which_for(client),
        environment=_environment(config_home),
    )
    assert [item.status for item in result.outcomes] == [
        KioTrashStatus.APPLIED,
        KioTrashStatus.APPLIED,
    ]
    assert not list(tmp_path.glob(".neocortex-kio-claim-*"))
    for item in items:
        info = trash / "info" / (Path(item.source).name + ".trashinfo")
        assert f"Path={item.source}\n" in info.read_text(encoding="utf-8")


def test_claim_failure_after_rename_is_restored_and_not_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    client = _executable(tmp_path / "kioclient5")
    source, expected = _source(tmp_path / "source.txt")
    item = KioTrashBatchItem(source, expected, _batch_digest(expected))
    original = kio_trash._renameat2_noreplace
    calls = 0

    def fail_after_claim(
        claim_source: Path,
        claim_destination: Path,
        *,
        expected: FileSnapshot,
    ) -> None:
        nonlocal calls
        if calls == 0:
            calls += 1
            os.rename(claim_source, claim_destination)
            raise kio_trash.KioTrashUnavailable(
                "kio_claim_unverified",
                "injected failure after claim rename",
            )
        original(claim_source, claim_destination, expected=expected)

    monkeypatch.setattr(kio_trash, "_renameat2_noreplace", fail_after_claim)
    result = move_many_to_trash(
        (item,),
        verifier=_never_verify,
        which=_which_for(client),
        environment=_environment(config_home),
    )

    assert result.outcomes[0].status is KioTrashStatus.BLOCKED
    assert result.outcomes[0].reason == "kio_claim_unverified"
    assert source.exists()
    assert not list(tmp_path.glob(".neocortex-kio-claim-*"))


def test_batch_verification_failure_does_not_hide_other_item(
    tmp_path: Path,
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    trash = tmp_path / "trash"
    (trash / "files").mkdir(parents=True)
    (trash / "info").mkdir()
    client = _executable(tmp_path / "kioclient5")
    items: list[KioTrashBatchItem] = []
    for name in ("first.txt", "second.txt"):
        source, expected = _source(tmp_path / name, name.encode("utf-8"))
        items.append(KioTrashBatchItem(source, expected, _batch_digest(expected)))

    def effect(
        command: Sequence[str],
        _kwargs: Mapping[str, object],
    ) -> subprocess.CompletedProcess[str]:
        for argument in command[3:-1]:
            source = Path(argument)
            target = trash / "files" / source.name
            os.rename(source, target)
            (trash / "info" / (target.name + ".trashinfo")).write_text(
                f"[Trash Info]\nPath={source}\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    def verifier(
        source: Path,
        expected: FileSnapshot,
        _client: Path,
    ) -> KioTrashVerification:
        if source.name == "first.txt":
            return KioTrashVerification(True, "not-json")
        return KioTrashVerification(
            True,
            _batch_evidence(
                trash,
                source,
                expected,
                cast(str, items[1].source_digest),
            ),
        )

    result = move_many_to_trash(
        items,
        verifier=verifier,
        runner=RunnerSpy(effect),
        which=_which_for(client),
        environment=_environment(config_home),
    )

    assert result.outcomes[0].status is KioTrashStatus.RECOVERY_REQUIRED
    assert result.outcomes[0].reason == "kio_effect_unverified"
    assert result.outcomes[1].status is KioTrashStatus.APPLIED


def test_batch_timeout_is_recovery_required_for_each_item(tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    client = _executable(tmp_path / "kioclient5")
    items = []
    for name in ("first.txt", "second.txt"):
        source, expected = _source(tmp_path / name)
        items.append(KioTrashBatchItem(source, expected, _batch_digest(expected)))

    def effect(command: Sequence[str], kwargs: Mapping[str, object]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, cast(float, kwargs["timeout"]), stderr="timed out")

    result = move_many_to_trash(
        items,
        verifier=_never_verify,
        runner=RunnerSpy(effect),
        which=_which_for(client),
        environment=_environment(config_home),
    )

    assert [item.status for item in result.outcomes] == [
        KioTrashStatus.RECOVERY_REQUIRED,
        KioTrashStatus.RECOVERY_REQUIRED,
    ]
    assert all(item.reason == "kio_timeout_effect_ambiguous" for item in result.outcomes)
