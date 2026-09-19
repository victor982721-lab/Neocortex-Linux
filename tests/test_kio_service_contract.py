"""Public KIO ownership and lifecycle regressions, using contained fixtures.

Subprocesses are always explicit doubles.  Native-mode tests replace
subprocess.run to exercise real claims without invoking a desktop client.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import neocortex
from neocortex.curation.application import KioTrashBackend
from neocortex.deduplication import FULL_ALGORITHM, FileSnapshot, full_fingerprint, snapshot_path
from neocortex.safety import kio_trash as kio


TEST_CAPABILITIES = ("base", "platform")
pytestmark = pytest.mark.capability("base", "platform")


class TrashFixture:
    def __init__(self, root: Path) -> None:
        self.root = root / "corpus"
        self.root.mkdir()
        self.home = root / "home"
        self.home.mkdir()
        self.config = root / "config"
        self.config.mkdir()
        self.data = root / "data"
        self.trash = self.data / "Trash"
        (self.trash / "files").mkdir(parents=True)
        (self.trash / "info").mkdir()
        self.client = root / "kioclient5"
        self.bus = root / "dbus-run-session"
        for executable in (self.client, self.bus):
            executable.write_text("fixture", encoding="utf-8")
            executable.chmod(0o700)
        self.environment = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_DATA_HOME": str(self.data),
        }
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def item(self, name: str = "source.txt") -> kio.KioTrashBatchItem:
        source = self.root / name
        source.write_text(name, encoding="utf-8")
        snapshot = snapshot_path(source)
        digest = FULL_ALGORITHM + ":" + full_fingerprint(snapshot).hex()
        return kio.KioTrashBatchItem(source, snapshot, digest)

    def which(self, name: str) -> str | None:
        return {"kioclient5": str(self.client), "dbus-run-session": str(self.bus)}.get(name)

    def runner(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(command), kwargs))
        assert kwargs["shell"] is False
        for raw in command[command.index("move") + 1:-1]:
            source = Path(raw)
            target = self.trash / "files" / source.name
            os.rename(source, target)
            (self.trash / "info" / (source.name + ".trashinfo")).write_text(
                f"[Trash Info]\nPath={source}\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    def service(self, **kwargs: object) -> kio.KioTrashService:
        return kio.KioTrashService(
            which=self.which,
            environment=self.environment,
            home_directory=self.home,
            **kwargs,
        )

    def backend(self, **kwargs: object) -> KioTrashBackend:
        return KioTrashBackend(
            which=self.which,
            environment=self.environment,
            home_directory=self.home,
            **kwargs,
        )


def test_production_kio_consumers_import_only_public_contracts() -> None:
    package = Path(neocortex.__file__).resolve().parent
    violations = []
    for source in package.rglob("*.py"):
        if source.is_relative_to(package / "safety"):
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "neocortex.safety.kio_trash":
                violations.extend(
                    f"{source.relative_to(package)}:{node.lineno}:{alias.name}"
                    for alias in node.names if alias.name.startswith("_")
                )
    assert violations == []


def test_single_claim_failure_after_rename_restores_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = TrashFixture(tmp_path)
    item = fixture.item()
    original = kio._renameat2_noreplace
    claims = 0

    def fail_after_claim(source: Path, target: Path, *, expected: FileSnapshot) -> None:
        nonlocal claims
        if claims == 0:
            claims += 1
            original(source, target, expected=expected)
            raise kio.KioTrashUnavailable("kio_claim_unverified", "fixture post-rename failure")
        original(source, target, expected=expected)

    monkeypatch.setattr(kio, "_renameat2_noreplace", fail_after_claim)
    monkeypatch.setattr(kio.subprocess, "run", lambda *_a, **_k: pytest.fail("must not start KIO"))
    result = fixture.backend(private_config=False, private_bus=False).apply_snapshot(
        item.expected, root=fixture.root, source_digest=item.source_digest
    )
    assert result.status == "blocked"
    assert Path(item.source).exists()
    assert snapshot_path(item.source) == item.expected
    assert not list(fixture.root.glob(".neocortex-kio-claim-*"))


def test_single_cleanup_flushes_parent_after_rmdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = TrashFixture(tmp_path)
    item = fixture.item()
    events: list[tuple[str, str]] = []
    original_fsync = os.fsync
    original_rmdir = os.rmdir

    def fsync(descriptor: int) -> None:
        events.append(("fsync", os.readlink(f"/proc/self/fd/{descriptor}")))
        original_fsync(descriptor)

    def rmdir(path: str | os.PathLike[str], *args: object, **kwargs: object) -> None:
        original_rmdir(path, *args, **kwargs)
        if ".neocortex-kio-claim-" in os.fspath(path):
            events.append(("claim_removed", str(fixture.root)))

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "rmdir", rmdir)
    monkeypatch.setattr(kio.subprocess, "run", fixture.runner)
    result = fixture.backend(private_config=False, private_bus=False).apply_snapshot(
        item.expected, root=fixture.root, source_digest=item.source_digest
    )
    assert result.status == "applied"
    removed = events.index(("claim_removed", str(fixture.root)))
    assert ("fsync", str(fixture.root)) in events[removed + 1:]


def test_service_native_context_bus_and_receipt_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = TrashFixture(tmp_path)
    item = fixture.item("source with spaces.txt")
    monkeypatch.setattr(kio.subprocess, "run", fixture.runner)
    result = fixture.service().move(item.expected, source_digest=item.source_digest)
    assert result.status is kio.KioTrashStatus.APPLIED
    assert result.receipt is not None
    command, kwargs = fixture.calls[0]
    assert command[:3] == [str(fixture.bus), "--", str(fixture.client)]
    claimed = Path(command[command.index("move") + 1])
    assert claimed.parent.parent == fixture.root
    assert claimed.parent.name.startswith(".neocortex-kio-claim-")
    assert not claimed.parent.exists()
    environment = kwargs["env"]
    assert environment["XDG_CONFIG_HOME"] != str(fixture.config)
    assert environment["XDG_DATA_HOME"] == str(fixture.data)
    assert not Path(environment["XDG_CONFIG_HOME"]).exists()
    assert not list(fixture.config.iterdir())
    receipt = json.loads(result.receipt.trash_evidence)
    root, target, info = kio.trash_receipt_paths(receipt, item.expected, item.source_digest)
    assert root == fixture.trash
    observed = kio.verify_trash_receipt_evidence(receipt, item.expected, item.source_digest)
    assert observed.path == str(target)
    assert f"Path={item.source}\n" in info.read_text(encoding="utf-8")
    stat_before = info.stat()
    info.write_text("[Trash Info]\nPath=/another/source\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source"):
        kio.verify_trash_receipt_evidence(receipt, item.expected, item.source_digest)
    assert not Path(item.source).exists()
    assert len(fixture.calls) == 1
    assert info.stat().st_ino == stat_before.st_ino


def test_metadata_binding_roundtrip_does_not_read_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = TrashFixture(tmp_path)
    item = fixture.item("generated.BAK")
    metadata = kio.metadata_binding(item.expected)
    monkeypatch.setattr(kio, "full_fingerprint", lambda *_a, **_k: pytest.fail("content hash"))
    monkeypatch.setattr(kio.subprocess, "run", fixture.runner)
    result = fixture.service(private_config=False, private_bus=False).move(
        item.expected,
        source_digest=metadata,
    )
    assert result.status is kio.KioTrashStatus.APPLIED
    assert result.receipt is not None
    receipt = json.loads(result.receipt.trash_evidence)
    observed = kio.verify_trash_receipt_evidence(receipt, item.expected, metadata)
    assert observed.path.endswith("generated.BAK")


def test_metadata_binding_receipt_restores_without_content_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = TrashFixture(tmp_path)
    item = fixture.item("restore.BAK")
    metadata = kio.metadata_binding(item.expected)
    monkeypatch.setattr(kio.subprocess, "run", fixture.runner)
    outcome = fixture.backend(private_config=False, private_bus=False).apply_snapshot(
        item.expected,
        root=fixture.root,
        source_digest=metadata,
    )
    assert outcome.status == "applied"
    assert outcome.receipt_json is not None
    monkeypatch.setattr(kio, "full_fingerprint", lambda *_a, **_k: pytest.fail("content hash"))

    restored = kio.restore_trash_receipt(outcome.receipt_json, root=fixture.root)

    assert restored["status"] == "restored"
    assert Path(item.source).exists()


def test_service_injected_runner_preserves_environment_and_no_claims(tmp_path: Path) -> None:
    fixture = TrashFixture(tmp_path)
    items = (fixture.item("first"), fixture.item("second"))
    result = fixture.service(runner=fixture.runner).move_many(items)
    assert [outcome.status for outcome in result] == [kio.KioTrashStatus.APPLIED] * 2
    assert len(fixture.calls) == 1
    command, kwargs = fixture.calls[0]
    assert command == [str(fixture.client), "--noninteractive", "move", *(str(x.source) for x in items), "trash:/"]
    assert kwargs["env"] == fixture.environment
    assert not list(fixture.root.glob(".neocortex-kio-claim-*"))


def test_batch_verifier_finds_new_item_after_large_existing_trash_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = TrashFixture(tmp_path)
    for index in range(5_000):
        (fixture.trash / "info" / f"noise-{index}.txt.trashinfo").write_text(
            "[Trash Info]\nPath=/unrelated\n", encoding="utf-8"
        )
    item = fixture.item("late.BAK")
    monkeypatch.setattr(kio.subprocess, "run", fixture.runner)

    result = fixture.service(private_config=False, private_bus=False).move_many((item,))

    assert [outcome.status for outcome in result] == [kio.KioTrashStatus.APPLIED]


def test_service_preserves_earlier_receipt_when_later_chunk_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = TrashFixture(tmp_path)
    items = (fixture.item("first"), fixture.item("second"))
    original = kio.move_many_to_trash
    calls = 0

    def second_chunk_fails(batch_items, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("fixture later-chunk failure")
        return original(batch_items, **kwargs)

    monkeypatch.setattr(kio, "MAX_KIO_BATCH_ITEMS", 1)
    monkeypatch.setattr(kio, "move_many_to_trash", second_chunk_fails)
    results = fixture.service(runner=fixture.runner).move_many(items)
    assert results[0].status is kio.KioTrashStatus.APPLIED
    assert results[0].receipt is not None
    assert results[0].receipt.source_path == str(items[0].source)
    assert results[1].status is kio.KioTrashStatus.RECOVERY_REQUIRED
    assert results[1].source_path == str(items[1].source)
    assert len(fixture.calls) == 1
    assert Path(items[1].source).exists()


def test_service_splits_only_pre_effect_argument_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = TrashFixture(tmp_path)
    items = (fixture.item("first"), fixture.item("second"))
    one_command = [str(fixture.client), "--noninteractive", "move", str(items[0].source), "trash:/"]
    limit = sum(len(arg.encode("utf-8")) + 1 for arg in one_command) + 3
    monkeypatch.setattr(kio, "MAX_KIO_BATCH_ARGUMENT_BYTES", limit)
    results = fixture.service(runner=fixture.runner).move_many(items)
    assert [result.status for result in results] == [kio.KioTrashStatus.APPLIED] * 2
    assert len(fixture.calls) == 2


@pytest.mark.parametrize("interrupted", [False, True])
def test_service_does_not_retry_timeout_or_interrupt(tmp_path: Path, interrupted: bool) -> None:
    fixture = TrashFixture(tmp_path)
    items = (fixture.item("first"), fixture.item("second"))
    calls = 0

    def timeout(command, **kwargs):
        nonlocal calls
        calls += 1
        if interrupted:
            raise KeyboardInterrupt
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    results = fixture.service(runner=timeout).move_many(items)
    assert all(result.status is kio.KioTrashStatus.RECOVERY_REQUIRED for result in results)
    assert calls == 1


def test_batch_preserves_structured_claim_recovery_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deep = tmp_path / ("a" * 120) / ("b" * 120)
    deep.mkdir(parents=True)
    fixture = TrashFixture(deep)
    item = fixture.item()

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(kio.subprocess, "run", timeout)
    result = kio.move_many_to_trash(
        (item,), which=fixture.which, environment=fixture.environment,
        home_directory=fixture.home,
    ).outcomes[0]
    assert result.status is kio.KioTrashStatus.RECOVERY_REQUIRED
    assert len(result.detail) > kio.MAX_DIAGNOSTIC_CHARS
    detail = json.loads(result.detail)
    assert detail["schema"] == "neocortex.kio-claim-recovery/v1"
    assert detail["claim"]["source_path"] == str(item.source)
    assert Path(detail["claim"]["claim_path"]).exists()
    assert not Path(item.source).exists()
    claim = kio.read_claim_recovery_detail(result.detail, source_path=item.source)
    assert claim.snapshot == item.expected
    assert claim.claim_path == Path(detail["claim"]["claim_path"])


def test_service_interrupt_stops_later_chunks_and_keeps_crossed_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = TrashFixture(tmp_path)
    items = (fixture.item("first"), fixture.item("second"))

    def move_then_interrupt(command, **kwargs):
        fixture.runner(command, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(kio, "MAX_KIO_BATCH_ITEMS", 1)
    monkeypatch.setattr(kio.subprocess, "run", move_then_interrupt)
    results = fixture.service(private_config=False, private_bus=False).move_many(items)
    assert results[0].status is kio.KioTrashStatus.RECOVERY_REQUIRED
    assert results[0].reason == "kio_process_interrupted"
    assert results[1].status is kio.KioTrashStatus.BLOCKED
    assert results[1].reason == "kio_cancelled_before_effect"
    assert not Path(items[0].source).exists()
    assert (fixture.trash / "files" / "first").exists()
    assert Path(items[1].source).exists()
    assert len(fixture.calls) == 1


@pytest.mark.parametrize("after_rename", [False, True])
def test_claim_preparation_interrupt_restores_all_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_rename: bool
) -> None:
    fixture = TrashFixture(tmp_path)
    items = tuple(fixture.item(name) for name in ("first", "second", "third", "fourth"))
    original = kio._renameat2_noreplace

    def interrupt_second(source, target, *, expected):
        if source == items[1].source:
            if after_rename:
                original(source, target, expected=expected)
            raise KeyboardInterrupt("fixture interrupted second claim")
        original(source, target, expected=expected)

    monkeypatch.setattr(kio, "_renameat2_noreplace", interrupt_second)
    monkeypatch.setattr(kio, "MAX_KIO_BATCH_ITEMS", 2)
    monkeypatch.setattr(kio.subprocess, "run", lambda *_a, **_k: pytest.fail("KIO must not start"))
    results = fixture.service(private_config=False, private_bus=False).move_many(items)
    assert all(result.status is kio.KioTrashStatus.BLOCKED for result in results)
    assert all(snapshot_path(item.source) == item.expected for item in items)
    assert not list(fixture.root.glob(".neocortex-kio-claim-*"))


def test_client_open_interrupt_restores_prepared_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = TrashFixture(tmp_path)
    items = (fixture.item("first"), fixture.item("second"))

    def interrupt_open(*_args):
        raise KeyboardInterrupt("fixture client open interrupted")

    monkeypatch.setattr(kio, "_open_kio_client", interrupt_open)
    monkeypatch.setattr(kio.subprocess, "run", lambda *_a, **_k: pytest.fail("KIO must not start"))
    results = fixture.service(private_config=False, private_bus=False).move_many(items)
    assert all(result.status is kio.KioTrashStatus.BLOCKED for result in results)
    assert all(snapshot_path(item.source) == item.expected for item in items)
    assert not list(fixture.root.glob(".neocortex-kio-claim-*"))


def test_claim_restore_cleanup_flush_failure_keeps_recovery_locator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = TrashFixture(tmp_path)
    items = (fixture.item("first"), fixture.item("second"))
    original_rename, original_rmdir, original_fsync = kio._renameat2_noreplace, os.rmdir, kio._fsync_directory
    first_directory = None
    removed = False

    def reject_second(source, target, *, expected):
        nonlocal first_directory
        if source == items[1].source:
            raise kio.KioTrashUnavailable("fixture_claim_rejected", "fixture second claim rejection")
        if source == items[0].source:
            first_directory = target.parent
        original_rename(source, target, expected=expected)

    def rmdir(path, *args, **kwargs):
        nonlocal removed
        original_rmdir(path, *args, **kwargs)
        if Path(path) == first_directory:
            removed = True

    def fsync(path):
        if removed and path == fixture.root:
            raise OSError("fixture rollback parent flush failure")
        original_fsync(path)

    monkeypatch.setattr(kio, "_renameat2_noreplace", reject_second)
    monkeypatch.setattr(os, "rmdir", rmdir)
    monkeypatch.setattr(kio, "_fsync_directory", fsync)
    monkeypatch.setattr(kio.subprocess, "run", lambda *_a, **_k: pytest.fail("KIO must not start"))
    results = fixture.service(private_config=False, private_bus=False).move_many(items)
    assert results[0].status is kio.KioTrashStatus.RECOVERY_REQUIRED
    assert results[0].reason == "kio_claim_restore_failed"
    assert snapshot_path(items[0].source) == items[0].expected
    claim = kio.read_claim_recovery_detail(results[0].detail, source_path=items[0].source)
    assert claim.snapshot == items[0].expected
    assert results[1].status is kio.KioTrashStatus.BLOCKED


def test_interrupt_between_verifications_preserves_completed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = TrashFixture(tmp_path)
    items = (fixture.item("first"), fixture.item("second"))
    original = kio._batch_verified_result
    calls = 0

    def interrupt_second(work, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("fixture between verification results")
        return original(work, **kwargs)

    monkeypatch.setattr(kio, "_batch_verified_result", interrupt_second)
    monkeypatch.setattr(kio.subprocess, "run", fixture.runner)
    results = fixture.service(private_config=False, private_bus=False).move_many(items)
    assert results[0].status is kio.KioTrashStatus.APPLIED
    assert results[0].receipt is not None
    assert results[1].status is kio.KioTrashStatus.RECOVERY_REQUIRED
    claim = kio.read_claim_recovery_detail(results[1].detail, source_path=items[1].source)
    assert claim.source_path == Path(items[1].source)
    assert len(fixture.calls) == 1


@pytest.mark.parametrize("phase", ["default_verifier", "custom_verifier", "evidence_finalize", "claim_rollback"])
def test_internal_interrupt_stops_unstarted_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    fixture = TrashFixture(tmp_path)
    items = (fixture.item("first"), fixture.item("second"))

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt("fixture internal cancellation")

    service_kwargs = {}
    if phase == "default_verifier":
        monkeypatch.setattr(kio, "_default_kio_verifier_batch", interrupt)
    elif phase == "custom_verifier":
        service_kwargs["verifier"] = interrupt
    elif phase == "evidence_finalize":
        monkeypatch.setattr(kio, "_batch_curation_evidence", interrupt)
    else:
        def reject_client(*_args):
            raise kio.KioTrashUnavailable("fixture_client_rejected", "fixture client rejected")
        monkeypatch.setattr(kio, "_open_kio_client", reject_client)
        monkeypatch.setattr(kio, "_restore_claim", interrupt)
    monkeypatch.setattr(kio, "MAX_KIO_BATCH_ITEMS", 1)
    monkeypatch.setattr(kio.subprocess, "run", fixture.runner)
    results = fixture.service(private_config=False, private_bus=False, **service_kwargs).move_many(items)
    assert results[0].status is kio.KioTrashStatus.RECOVERY_REQUIRED
    assert results[1].status is kio.KioTrashStatus.BLOCKED
    assert results[1].reason == "kio_cancelled_before_effect"
    assert Path(items[1].source).exists()
    assert len(fixture.calls) == (0 if phase == "claim_rollback" else 1)
    claim = kio.read_claim_recovery_detail(results[0].detail, source_path=items[0].source)
    assert claim.snapshot == items[0].expected


def test_process_result_interrupt_stops_unstarted_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = TrashFixture(tmp_path)
    items = (fixture.item("first"), fixture.item("second"))

    class InterruptedResult:
        @property
        def returncode(self) -> int:
            raise KeyboardInterrupt("fixture process-result cancellation")

    def interrupted_runner(command: list[str], **kwargs: object) -> InterruptedResult:
        fixture.runner(command, **kwargs)
        return InterruptedResult()

    monkeypatch.setattr(kio, "MAX_KIO_BATCH_ITEMS", 1)
    monkeypatch.setattr(kio.subprocess, "run", interrupted_runner)
    results = fixture.service(private_config=False, private_bus=False).move_many(items)
    assert results[0].status is kio.KioTrashStatus.RECOVERY_REQUIRED
    assert results[1].status is kio.KioTrashStatus.BLOCKED
    assert results[1].reason == "kio_cancelled_before_effect"
    assert Path(items[1].source).exists()
    assert len(fixture.calls) == 1
    claim = kio.read_claim_recovery_detail(results[0].detail, source_path=items[0].source)
    assert claim.snapshot == items[0].expected


def test_service_context_cleanup_error_keeps_verified_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = TrashFixture(tmp_path)
    item = fixture.item()

    @contextmanager
    def context(*_args, **_kwargs) -> Iterator[tuple[dict[str, str], Path]]:
        yield fixture.environment, fixture.home
        raise OSError("fixture derivative-context cleanup failure")

    monkeypatch.setattr(kio, "private_kio_context", context)
    monkeypatch.setattr(kio.subprocess, "run", fixture.runner)
    result = fixture.service(private_bus=False).move(item.expected, source_digest=item.source_digest)
    assert result.status is kio.KioTrashStatus.APPLIED
    assert result.receipt is not None
    assert "kio_private_context_cleanup_failed" in result.detail
    assert not Path(item.source).exists()


def test_backend_long_path_recovery_keeps_locator_inside_detail_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deep = tmp_path
    for number in range(14):
        deep /= f"{number:02d}" + "x" * 118
    deep.mkdir(parents=True)
    fixture = TrashFixture(deep)
    item = fixture.item()
    calls = 0

    def timeout(command, **kwargs):
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(kio.subprocess, "run", timeout)
    result = fixture.backend(private_config=False, private_bus=False).apply_snapshot(
        item.expected, root=fixture.root, source_digest=item.source_digest
    )
    assert result.status == "recovery_required"
    assert len(result.detail.encode("utf-8")) <= 4096
    assert json.loads(result.detail)["schema"] == "neocortex.kio-claim-recovery/v2"
    claim = kio.read_claim_recovery_detail(result.detail, source_path=item.source)
    assert claim.source_path == Path(item.source)
    assert claim.snapshot == item.expected
    assert claim.claim_path.exists()
    assert snapshot_path(claim.claim_path).file_id == item.expected.file_id
    assert not Path(item.source).exists()
    with pytest.raises(ValueError, match="source"):
        kio.read_claim_recovery_detail(result.detail, source_path=fixture.root / "wrong")
    assert calls == 1


def test_claim_recovery_serialization_preserves_surrogateescape_path(tmp_path: Path) -> None:
    source = tmp_path / "source\udcff.txt"
    source.write_bytes(b"fixture")
    snapshot = snapshot_path(source)
    directory = tmp_path / ".neocortex-kio-claim-fixture"
    claim = kio.KioTrashClaim(source, directory / source.name, directory, snapshot)
    detail = kio._claim_recovery_detail(claim, reason="fixture", detail="fixture")
    assert len(detail.encode("utf-8")) <= 4096
    assert kio.read_claim_recovery_detail(detail, source_path=source) == claim


def test_service_rejects_misaligned_batch_result_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = TrashFixture(tmp_path)
    item = fixture.item()
    calls = 0

    def incorrect_vector(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return kio.KioTrashBatchResult((kio.KioTrashResult(
            kio.KioTrashStatus.BLOCKED, "kio_batch_arguments_too_large", "/another/source"
        ),))

    monkeypatch.setattr(kio, "move_many_to_trash", incorrect_vector)
    result = fixture.service(runner=fixture.runner).move(item.expected, source_digest=item.source_digest)
    assert result.status is kio.KioTrashStatus.RECOVERY_REQUIRED
    assert result.reason == "kio_batch_result_invalid"
    assert result.source_path == item.expected.path
    assert calls == 1


def test_backend_keeps_public_aliases_and_schema(tmp_path: Path) -> None:
    fixture = TrashFixture(tmp_path)
    item = fixture.item()
    backend = fixture.backend(runner=fixture.runner)
    results = backend.apply_snapshots(((item.expected, item.source_digest),), root=fixture.root)
    assert results[0].status == "applied"
    receipt = json.loads(results[0].receipt_json)
    assert receipt["schema_version"] == 1
    assert receipt["backend"] == "kio-trash-path-bound-v1"
    assert receipt["source_path"] == str(item.source)
    assert receipt["source_digest"] == item.source_digest
    assert receipt["operation"] == "trash"
    assert receipt["receipt_type"] == "successful_return_and_observation"
    assert callable(backend.apply_snapshot_batch)
    assert callable(kio.move_to_trash)
    assert callable(kio.move_many_to_trash)
    assert callable(kio.restore_trash_receipt)
