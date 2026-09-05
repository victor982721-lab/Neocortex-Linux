# region [00] Contexto del módulo
# Módulo: tests/test_release_windows.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations


import hashlib
import inspect
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from tools import release_windows
from tools.release_windows import (
    REPLACEFILE_IGNORE_ACL_ERRORS,
    REPLACEFILE_IGNORE_MERGE_ERRORS,
    REPLACEFILE_WRITE_THROUGH,
    FileSnapshot,
    LauncherTransitionRequest,
    ReceiptChain,
    ReceiptValidationError,
    ReleaseLayout,
    ReleaseTransitionError,
    TransitionEffectUncertainError,
    TransitionResult,
    WindowsReplaceFileNative,
    recover_pending_transition,
    transition_launcher,
)


TEST_CAPABILITIES = ("platform",)
TEST_PLATFORMS = ("win32",)
# endregion [01]

# region [02] Implementación


_OLD_DESCRIPTOR = b"owner=victor;dacl=launcher-v1"
_NEW_DESCRIPTOR = b"owner=victor;dacl=runtime-v2"


class _FakeLock:
    def __init__(self, owner: _FakeOps, path: Path) -> None:
        self._owner = owner
        self._path = path
        self._lock = owner._locks.setdefault(path, threading.Lock())

    def acquire(self) -> None:
        self._lock.acquire()
        with self._owner._counter_lock:
            self._owner.active_locks += 1
            self._owner.max_active_locks = max(
                self._owner.max_active_locks, self._owner.active_locks
            )

    def release(self) -> None:
        try:
            if self._owner.lock_release_error is not None:
                raise self._owner.lock_release_error
        finally:
            with self._owner._counter_lock:
                self._owner.active_locks -= 1
            self._lock.release()


class _FakeOps:
    def __init__(self) -> None:
        self.descriptors: dict[Path, bytes] = {}
        self.file_systems: dict[Path, str] = {}
        self.volume_overrides: dict[Path, int] = {}
        self.snapshot_calls: list[Path] = []
        self.snapshot_counts: dict[Path, int] = {}
        self.snapshot_hooks: dict[Path, Any] = {}
        self.read_counts: dict[Path, int] = {}
        self.read_hooks: dict[Path, Any] = {}
        self.copy_calls: list[tuple[Path, Path]] = []
        self.receipt_writes: list[Path] = []
        self.removed: list[Path] = []
        self.remove_errors: dict[Path, BaseException] = {}
        self.remove_hooks: dict[Path, Any] = {}
        self.write_error_suffix: str | None = None
        self.lock_release_error: BaseException | None = None
        self._locks: dict[Path, threading.Lock] = {}
        self._counter_lock = threading.Lock()
        self.active_locks = 0
        self.max_active_locks = 0
        self.opened_locks: list[Path] = []

    @staticmethod
    def _key(path: Path) -> Path:
        return Path(os.path.abspath(path))

    def register(self, path: Path, descriptor: bytes) -> None:
        self.descriptors[self._key(path)] = descriptor

    def open_external_lock(self, path: Path) -> _FakeLock:
        key = self._key(path)
        assert not key.parent.samefile(key.parents[2] / "install")
        self.opened_locks.append(key)
        return _FakeLock(self, key)

    def snapshot_by_handle(self, path: Path) -> FileSnapshot:
        key = self._key(path)
        self.snapshot_calls.append(key)
        count = self.snapshot_counts.get(key, 0) + 1
        self.snapshot_counts[key] = count
        hook = self.snapshot_hooks.get(key)
        if hook is not None:
            hook(count)
        with key.open("rb") as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            size = 0
            while chunk := stream.read(64 * 1024):
                size += len(chunk)
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        assert (before.st_dev, before.st_ino, before.st_size) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
        )
        descriptor = self.descriptors[key]
        return FileSnapshot(
            path=str(key),
            size=size,
            sha256=digest.hexdigest(),
            volume_id=self.volume_overrides.get(key, int(after.st_dev)),
            file_id=int(after.st_ino),
            file_system=self.file_systems.get(key, "NTFS"),
            security_descriptor_sha256=hashlib.sha256(descriptor).hexdigest(),
            security_descriptor=descriptor,
        )

    def copy_create_new_and_flush(self, source: Path, destination: Path) -> FileSnapshot:
        source_key = self._key(source)
        destination_key = self._key(destination)
        self.copy_calls.append((source_key, destination_key))
        digest = hashlib.sha256()
        size = 0
        with source_key.open("rb") as reader, destination_key.open("xb") as writer:
            while chunk := reader.read(64 * 1024):
                writer.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
            created = os.fstat(writer.fileno())
        descriptor = self.descriptors[source_key]
        self.descriptors[destination_key] = descriptor
        return FileSnapshot(
            path=str(destination_key),
            size=size,
            sha256=digest.hexdigest(),
            volume_id=self.volume_overrides.get(destination_key, int(created.st_dev)),
            file_id=int(created.st_ino),
            file_system=self.file_systems.get(destination_key, "NTFS"),
            security_descriptor_sha256=hashlib.sha256(descriptor).hexdigest(),
            security_descriptor=descriptor,
        )

    def write_create_new_and_flush(self, path: Path, payload: bytes) -> None:
        key = self._key(path)
        if self.write_error_suffix is not None and key.name.endswith(self.write_error_suffix):
            raise OSError("synthetic receipt write failure")
        with key.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        self.receipt_writes.append(key)

    def read_bytes(self, path: Path, *, max_bytes: int) -> bytes:
        key = self._key(path)
        count = self.read_counts.get(key, 0) + 1
        self.read_counts[key] = count
        hook = self.read_hooks.get(key)
        if hook is not None:
            hook(count)
        if key.stat().st_size > max_bytes:
            raise ReceiptValidationError("receipt exceeds its size limit")
        return key.read_bytes()

    def path_exists(self, path: Path) -> bool:
        return os.path.lexists(self._key(path))

    def remove_file(self, path: Path) -> None:
        key = self._key(path)
        hook = self.remove_hooks.get(key)
        if hook is not None:
            hook()
        error = self.remove_errors.get(key)
        if error is not None:
            raise error
        key.unlink()
        self.descriptors.pop(key, None)
        self.removed.append(key)

    def remove_file_if_snapshot(self, path: Path, expected: FileSnapshot) -> None:
        key = self._key(path)
        hook = self.remove_hooks.get(key)
        if hook is not None:
            hook()
        error = self.remove_errors.get(key)
        if error is not None:
            raise error
        try:
            actual = self.snapshot_by_handle(key)
        except FileNotFoundError:
            return
        if actual != expected:
            raise ReleaseTransitionError("atomic cleanup identity mismatch")
        key.unlink()
        self.descriptors.pop(key, None)
        self.removed.append(key)


class _FakeNative:
    def __init__(self, ops: _FakeOps) -> None:
        self.ops = ops
        self.calls: list[tuple[Path, Path, Path, int]] = []
        self.fail: str | None = None
        self.corrupt_final_acl = False
        self.delay_seconds = 0.0

    def replace_file(
        self,
        replaced: Path,
        replacement: Path,
        backup: Path,
        *,
        flags: int,
    ) -> None:
        replaced = self.ops._key(replaced)
        replacement = self.ops._key(replacement)
        backup = self.ops._key(backup)
        self.calls.append((replaced, replacement, backup, flags))
        assert flags == REPLACEFILE_WRITE_THROUGH
        assert not flags & REPLACEFILE_IGNORE_MERGE_ERRORS
        assert not flags & REPLACEFILE_IGNORE_ACL_ERRORS
        assert replacement != replaced
        assert not backup.exists()
        if self.fail == "before":
            raise OSError("synthetic ReplaceFileW pre-effect failure")
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        old_descriptor = self.ops.descriptors.pop(replaced)
        self.ops.descriptors.pop(replacement)
        os.replace(replaced, backup)
        os.replace(replacement, replaced)
        self.ops.descriptors[backup] = old_descriptor
        self.ops.descriptors[replaced] = (
            b"corrupt-acl" if self.corrupt_final_acl else old_descriptor
        )
        if self.fail == "after":
            raise OSError("synthetic ReplaceFileW post-effect failure")


@dataclass(frozen=True, slots=True)
class _Fixture:
    layout: ReleaseLayout
    desired: Path
    ops: _FakeOps
    native: _FakeNative
    request: LauncherTransitionRequest


def _fixture(tmp_path: Path, *, operation: str = "promote") -> _Fixture:
    launcher = (tmp_path / "install" / "bin" / "Neocortex.exe").resolve()
    desired = (tmp_path / "runtime" / "bin" / "Neocortex.exe").resolve()
    receipts = (tmp_path / "control" / "receipts").resolve()
    backups = (tmp_path / "control" / "backups").resolve()
    lock = (tmp_path / "control" / "locks" / "launcher.lock").resolve()
    launcher.parent.mkdir(parents=True)
    desired.parent.mkdir(parents=True)
    receipts.mkdir(parents=True)
    backups.mkdir(parents=True)
    lock.parent.mkdir(parents=True)
    launcher.write_bytes(b"old-launcher-arbitrary-baseline\n")
    desired.write_bytes(b"new-launcher-0.7.2\n")
    ops = _FakeOps()
    ops.register(launcher, _OLD_DESCRIPTOR)
    ops.register(desired, _NEW_DESCRIPTOR)
    native = _FakeNative(ops)
    layout = ReleaseLayout(
        launcher_path=launcher,
        receipts_directory=receipts,
        backup_directory=backups,
        lock_path=lock,
    )
    request = LauncherTransitionRequest(
        layout=layout,
        desired_launcher=desired,
        expected_current=ops.snapshot_by_handle(launcher),
        expected_desired=ops.snapshot_by_handle(desired),
        operation=operation,
        receipt_chain=ReceiptChain(),
    )
    ops.snapshot_calls.clear()
    ops.snapshot_counts.clear()
    return _Fixture(layout, desired, ops, native, request)


def _canonical_payload(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    decoded = json.loads(payload)
    assert (
        payload
        == (
            json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
    )
    return decoded


def _result(
    fixture: _Fixture,
    *,
    checkpoint: Any = None,
) -> TransitionResult:
    return transition_launcher(
        fixture.request,
        ops=fixture.ops,
        native=fixture.native,
        checkpoint=checkpoint,
    )


def test_snapshot_contract_validates_descriptor_bytes_and_hash(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    snapshot = fixture.request.expected_current
    assert snapshot.size == len(b"old-launcher-arbitrary-baseline\n")
    assert snapshot.file_system == "NTFS"
    assert snapshot.volume_id > 0
    assert snapshot.file_id > 0
    assert snapshot.security_descriptor == _OLD_DESCRIPTOR
    with pytest.raises(ValueError, match="security descriptor hash"):
        replace(snapshot, security_descriptor_sha256="0" * 64)


@pytest.mark.parametrize("kind", ["backup", "receipts", "lock"])
def test_layout_requires_external_control_paths(tmp_path: Path, kind: str) -> None:
    launcher = (tmp_path / "install" / "bin" / "Neocortex.exe").resolve()
    values = {
        "launcher_path": launcher,
        "receipts_directory": (tmp_path / "control" / "receipts").resolve(),
        "backup_directory": (tmp_path / "control" / "backups").resolve(),
        "lock_path": (tmp_path / "control" / "launcher.lock").resolve(),
    }
    if kind == "backup":
        values["backup_directory"] = launcher.parent / "backups"
    elif kind == "receipts":
        values["receipts_directory"] = launcher.parent / "receipts"
    else:
        values["lock_path"] = launcher.parent / "launcher.lock"
    with pytest.raises(ValueError, match="external"):
        ReleaseLayout(**values)


def test_successful_transition_enforces_complete_atomic_contract(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    result = _result(fixture)

    assert result.status == "success"
    assert result.operation == "promote"
    assert result.performed is True
    assert fixture.layout.launcher_path.read_bytes() == b"new-launcher-0.7.2\n"
    assert fixture.ops.descriptors[fixture.layout.launcher_path] == _OLD_DESCRIPTOR
    assert len(fixture.native.calls) == 1
    replaced, staged, native_backup, flags = fixture.native.calls[0]
    assert replaced == fixture.layout.launcher_path
    assert staged.parent == fixture.layout.launcher_path.parent
    assert staged != fixture.desired
    assert flags == REPLACEFILE_WRITE_THROUGH
    assert not native_backup.exists()

    external_backup = fixture.layout.backup_directory / (
        f"{fixture.request.expected_current.sha256}.launcher"
    )
    assert external_backup.read_bytes() == b"old-launcher-arbitrary-baseline\n"
    assert fixture.ops.descriptors[external_backup] == _OLD_DESCRIPTOR
    assert (fixture.layout.launcher_path, external_backup) in fixture.ops.copy_calls
    assert fixture.ops.snapshot_counts[fixture.layout.launcher_path] >= 3
    assert fixture.ops.snapshot_counts[fixture.desired] >= 3
    assert fixture.desired.read_bytes() == b"new-launcher-0.7.2\n"

    intent = _canonical_payload(result.intent_path)
    receipt = _canonical_payload(result.result_path)
    assert intent["previous_receipt_sha256"] is None
    assert receipt["intent_sha256"] == result.intent_sha256
    assert intent["stage_evidence_path"] == str(result.stage_evidence_path)
    stage_evidence = _canonical_payload(result.stage_evidence_path)
    assert stage_evidence["intent_sha256"] == result.intent_sha256
    assert hashlib.sha256(result.intent_path.read_bytes()).hexdigest() == (result.intent_sha256)
    assert hashlib.sha256(result.stage_evidence_path.read_bytes()).hexdigest() == (
        result.stage_evidence_sha256
    )
    assert hashlib.sha256(result.result_path.read_bytes()).hexdigest() == (result.receipt_sha256)
    assert fixture.ops.receipt_writes == [
        result.intent_path,
        result.stage_evidence_path,
        result.result_path,
    ]


def test_known_launcher_hash_is_input_not_hardcoded_authorization(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert "1d4fc0" not in inspect.getsource(release_windows).casefold()
    assert _result(fixture).status == "success"


@pytest.mark.parametrize("target", ["current", "desired"])
def test_initial_exact_cas_failure_precedes_all_writes(tmp_path: Path, target: str) -> None:
    fixture = _fixture(tmp_path)
    field = "expected_current" if target == "current" else "expected_desired"
    bad = replace(getattr(fixture.request, field), sha256="0" * 64)
    request = replace(fixture.request, **{field: bad})
    with pytest.raises(ReleaseTransitionError, match="exact CAS"):
        transition_launcher(request, ops=fixture.ops, native=fixture.native)
    assert fixture.native.calls == []
    assert fixture.ops.receipt_writes == []
    assert fixture.ops.copy_calls == []


def test_second_exact_cas_drift_preserves_committed_stage(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    def mutate_on_second_snapshot(count: int) -> None:
        if count == 2:
            fixture.layout.launcher_path.write_bytes(b"raced-launcher\n")

    fixture.ops.snapshot_hooks[fixture.layout.launcher_path] = mutate_on_second_snapshot
    with pytest.raises(ReleaseTransitionError, match="exact CAS") as caught:
        _result(fixture)
    assert fixture.native.calls == []
    staged = [
        destination for source, destination in fixture.ops.copy_calls if source == fixture.desired
    ]
    assert len(staged) == 1
    assert staged[0].exists()
    assert any("stage evidence" in note for note in getattr(caught.value, "__notes__", ()))


def test_corrupt_content_addressed_backup_is_never_overwritten(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    backup = fixture.layout.backup_directory / (
        f"{fixture.request.expected_current.sha256}.launcher"
    )
    backup.write_bytes(b"corrupt")
    fixture.ops.register(backup, _OLD_DESCRIPTOR)
    with pytest.raises(ReleaseTransitionError, match="content-addressed backup"):
        _result(fixture)
    assert backup.read_bytes() == b"corrupt"
    assert fixture.native.calls == []


@pytest.mark.parametrize("fault", ["volume", "filesystem"])
def test_staging_must_be_a_new_file_on_same_ntfs_volume(tmp_path: Path, fault: str) -> None:
    fixture = _fixture(tmp_path)
    original_copy = fixture.ops.copy_create_new_and_flush

    def faulty_copy(source: Path, destination: Path) -> FileSnapshot:
        created = original_copy(source, destination)
        key = fixture.ops._key(destination)
        if fault == "volume":
            volume_id = fixture.request.expected_current.volume_id + 1
            fixture.ops.volume_overrides[key] = volume_id
            return replace(created, volume_id=volume_id)
        fixture.ops.file_systems[key] = "ReFS"
        return replace(created, file_system="ReFS")

    fixture.ops.copy_create_new_and_flush = faulty_copy  # type: ignore[method-assign]
    with pytest.raises(ReleaseTransitionError, match="same NTFS volume"):
        _result(fixture)
    assert fixture.native.calls == []


def test_receipt_chain_is_verified_and_linked(tmp_path: Path) -> None:
    first = _fixture(tmp_path / "first")
    first_result = _result(first)

    second = _fixture(tmp_path / "second", operation="rollback")
    chained = replace(
        second.request,
        receipt_chain=ReceiptChain(
            previous_receipt_path=first_result.result_path,
            previous_receipt_sha256=first_result.receipt_sha256,
        ),
    )
    second_result = transition_launcher(chained, ops=second.ops, native=second.native)
    assert (
        _canonical_payload(second_result.intent_path)["previous_receipt_sha256"]
        == first_result.receipt_sha256
    )

    third = _fixture(tmp_path / "third")
    invalid_chain = ReceiptChain(
        previous_receipt_path=first_result.result_path,
        previous_receipt_sha256="0" * 64,
    )
    with pytest.raises(ReceiptValidationError, match="previous receipt"):
        transition_launcher(
            replace(third.request, receipt_chain=invalid_chain),
            ops=third.ops,
            native=third.native,
        )
    assert third.native.calls == []


def test_transition_is_idempotent_and_serialized_by_external_lock(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.native.delay_seconds = 0.05
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_result, fixture) for _index in range(2)]
        results = [future.result(timeout=5) for future in futures]
    assert [result.receipt_sha256 for result in results] == [
        results[0].receipt_sha256,
        results[0].receipt_sha256,
    ]
    assert len(fixture.native.calls) == 1
    assert fixture.ops.max_active_locks == 1
    assert fixture.ops.active_locks == 0


@pytest.mark.parametrize("operation", ["promote", "rollback", "repromote"])
def test_promote_rollback_and_repromote_share_one_transition(
    tmp_path: Path, operation: str
) -> None:
    fixture = _fixture(tmp_path, operation=operation)
    result = _result(fixture)
    assert result.operation == operation
    assert result.status == "success"
    assert len(fixture.native.calls) == 1


class _Crash(RuntimeError):
    pass


def test_recovery_classifies_crash_after_intent_as_no_effect(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    def crash(name: str) -> None:
        if name == "after_intent":
            raise _Crash("after intent")

    with pytest.raises(_Crash, match="after intent"):
        _result(fixture, checkpoint=crash)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    recovered = recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)
    assert recovered.status == "no_effect"
    assert recovered.performed is False
    assert fixture.native.calls == []
    assert _canonical_payload(recovered.result_path)["status"] == "no_effect"


def test_recovery_classifies_desired_current_with_both_backups_as_success(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    def crash(name: str) -> None:
        if name == "after_replace":
            raise _Crash("after replace")

    with pytest.raises(_Crash, match="after replace"):
        _result(fixture, checkpoint=crash)
    assert len(fixture.native.calls) == 1
    _replaced, _staged, native_backup, _flags = fixture.native.calls[0]
    assert native_backup.exists()
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    recovered = recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)
    assert recovered.status == "success"
    assert recovered.performed is False
    assert len(fixture.native.calls) == 1
    assert not native_backup.exists()


@pytest.mark.parametrize("fault", ["other", "absent", "missing_backup"])
def test_recovery_abstains_as_uncertain_without_mutating_launcher(
    tmp_path: Path, fault: str
) -> None:
    fixture = _fixture(tmp_path)

    def crash(name: str) -> None:
        if name == "after_replace":
            raise _Crash("after replace")

    with pytest.raises(_Crash):
        _result(fixture, checkpoint=crash)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    _replaced, _staged, native_backup, _flags = fixture.native.calls[0]
    if fault == "other":
        fixture.layout.launcher_path.write_bytes(b"unknown-launcher\n")
    elif fault == "absent":
        fixture.layout.launcher_path.unlink()
        fixture.ops.descriptors.pop(fixture.layout.launcher_path)
    else:
        native_backup.unlink()
        fixture.ops.descriptors.pop(native_backup)
    before_calls = tuple(fixture.native.calls)
    recovered = recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)
    assert recovered.status == "uncertain"
    assert recovered.performed is False
    assert tuple(fixture.native.calls) == before_calls
    assert _canonical_payload(recovered.result_path)["status"] == "uncertain"


def test_result_write_crash_is_recoverable_and_does_not_repeat_replace(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.ops.write_error_suffix = ".result.json"
    with pytest.raises(OSError, match="receipt write failure"):
        _result(fixture)
    assert len(fixture.native.calls) == 1
    fixture.ops.write_error_suffix = None
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    recovered = recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)
    assert recovered.status == "success"
    assert len(fixture.native.calls) == 1


def test_cleanup_failure_never_masks_primary_failure(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    staged_path: Path | None = None
    original_copy = fixture.ops.copy_create_new_and_flush

    def remember_stage(source: Path, destination: Path) -> FileSnapshot:
        nonlocal staged_path
        created = original_copy(source, destination)
        if fixture.ops._key(source) == fixture.desired:
            staged_path = fixture.ops._key(destination)
            fixture.ops.remove_errors[staged_path] = OSError("cleanup failed")
        return created

    fixture.ops.copy_create_new_and_flush = remember_stage  # type: ignore[method-assign]

    def fail_before_replace(name: str) -> None:
        if name == "before_replace":
            raise _Crash("primary checkpoint failure")

    with pytest.raises(_Crash, match="primary checkpoint failure") as caught:
        _result(fixture, checkpoint=fail_before_replace)
    assert staged_path is not None
    assert any("cleanup failed" in note for note in caught.value.__notes__)


def test_acl_verification_fails_closed_and_preserves_native_backup(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.native.corrupt_final_acl = True
    with pytest.raises(ReleaseTransitionError, match="security descriptor"):
        _result(fixture)
    assert len(fixture.native.calls) == 1
    _replaced, _staged, native_backup, _flags = fixture.native.calls[0]
    assert native_backup.exists()


def test_native_wrapper_allows_only_write_through_and_empty_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = tmp_path / "current.exe"
    stage = tmp_path / "stage.exe"
    backup = tmp_path / "backup.exe"
    current.write_bytes(b"old")
    stage.write_bytes(b"new")
    calls: list[tuple[str, str, str, int]] = []

    def fake_replace(
        replaced: str,
        replacement: str,
        backup_name: str,
        flags: int,
        _exclude: object,
        _reserved: object,
    ) -> int:
        calls.append((replaced, replacement, backup_name, flags))
        return 1

    monkeypatch.setattr(release_windows, "_replace_file_w", fake_replace)
    native = WindowsReplaceFileNative()
    native.replace_file(
        current,
        stage,
        backup,
        flags=REPLACEFILE_WRITE_THROUGH,
    )
    assert calls[0][3] == REPLACEFILE_WRITE_THROUGH
    for forbidden in (
        REPLACEFILE_IGNORE_MERGE_ERRORS,
        REPLACEFILE_IGNORE_ACL_ERRORS,
        REPLACEFILE_WRITE_THROUGH | REPLACEFILE_IGNORE_ACL_ERRORS,
    ):
        with pytest.raises(ValueError, match="WRITE_THROUGH"):
            native.replace_file(current, stage, backup, flags=forbidden)
    backup.write_bytes(b"occupied")
    with pytest.raises(FileExistsError, match="backup"):
        native.replace_file(
            current,
            stage,
            backup,
            flags=REPLACEFILE_WRITE_THROUGH,
        )


def _chained_request(
    fixture: _Fixture,
    *,
    desired: Path,
    operation: str,
    chain: ReceiptChain,
) -> LauncherTransitionRequest:
    return LauncherTransitionRequest(
        layout=fixture.layout,
        desired_launcher=desired,
        expected_current=fixture.ops.snapshot_by_handle(fixture.layout.launcher_path),
        expected_desired=fixture.ops.snapshot_by_handle(desired),
        operation=operation,
        receipt_chain=chain,
    )


def test_promote_rollback_repromote_sequence_is_hash_exact_and_chained(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    promoted = _result(fixture)
    old_backup = promoted.external_backup_path
    old_hash = fixture.request.expected_current.sha256
    new_hash = fixture.request.expected_desired.sha256

    rollback_request = _chained_request(
        fixture,
        desired=old_backup,
        operation="rollback",
        chain=fixture.request.receipt_chain.advance(promoted),
    )
    rolled_back = transition_launcher(rollback_request, ops=fixture.ops, native=fixture.native)
    assert fixture.ops.snapshot_by_handle(fixture.layout.launcher_path).sha256 == old_hash
    assert (
        _canonical_payload(rolled_back.intent_path)["previous_receipt_sha256"]
        == promoted.receipt_sha256
    )

    repromote_request = _chained_request(
        fixture,
        desired=fixture.desired,
        operation="repromote",
        chain=rollback_request.receipt_chain.advance(rolled_back),
    )
    repromoted = transition_launcher(repromote_request, ops=fixture.ops, native=fixture.native)
    assert fixture.ops.snapshot_by_handle(fixture.layout.launcher_path).sha256 == new_hash
    assert (
        _canonical_payload(repromoted.intent_path)["previous_receipt_sha256"]
        == rolled_back.receipt_sha256
    )
    assert len(fixture.native.calls) == 3
    native_backups = [call[2] for call in fixture.native.calls]
    assert len(set(native_backups)) == 3
    assert all(path.parent == fixture.layout.launcher_path.parent for path in native_backups)
    assert {path.name for path in fixture.layout.backup_directory.glob("*.launcher")} == {
        f"{old_hash}.launcher",
        f"{new_hash}.launcher",
    }
    assert fixture.desired.read_bytes() == b"new-launcher-0.7.2\n"


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [("before", "no_effect"), ("after", "success")],
)
def test_native_exception_is_uncertain_and_recovered_from_physical_state(
    tmp_path: Path,
    failure: str,
    expected_status: str,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.native.fail = failure
    with pytest.raises(TransitionEffectUncertainError, match="may have committed"):
        _result(fixture)
    assert len(fixture.native.calls) == 1
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    fixture.native.fail = None
    recovered = recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)
    assert recovered.status == expected_status
    assert len(fixture.native.calls) == 1


@pytest.mark.parametrize("race", ["desired-bytes", "desired-identity", "current-acl"])
def test_second_cas_rejects_desired_identity_content_and_current_acl_races(
    tmp_path: Path,
    race: str,
) -> None:
    fixture = _fixture(tmp_path)

    if race.startswith("desired"):

        def desired_race(count: int) -> None:
            if count != 2:
                return
            if race == "desired-bytes":
                fixture.desired.write_bytes(b"changed desired\n")
                return
            replacement = fixture.desired.with_name("replacement.exe")
            replacement.write_bytes(fixture.desired.read_bytes())
            fixture.ops.register(replacement, _NEW_DESCRIPTOR)
            os.replace(replacement, fixture.desired)
            fixture.ops.descriptors[fixture.desired] = fixture.ops.descriptors.pop(replacement)

        fixture.ops.snapshot_hooks[fixture.desired] = desired_race
    else:

        def current_acl_race(count: int) -> None:
            if count == 2:
                fixture.ops.descriptors[fixture.layout.launcher_path] = b"tampered-acl"

        fixture.ops.snapshot_hooks[fixture.layout.launcher_path] = current_acl_race

    with pytest.raises(ReleaseTransitionError, match="exact CAS"):
        _result(fixture)
    assert fixture.native.calls == []


@pytest.mark.parametrize("corruption", ["bytes", "acl"])
def test_corrupt_staging_copy_is_removed_before_native(
    tmp_path: Path,
    corruption: str,
) -> None:
    fixture = _fixture(tmp_path)
    original_copy = fixture.ops.copy_create_new_and_flush
    staged: Path | None = None

    def corrupt_copy(source: Path, destination: Path) -> FileSnapshot:
        nonlocal staged
        created = original_copy(source, destination)
        if fixture.ops._key(source) != fixture.desired:
            return created
        staged = fixture.ops._key(destination)
        if corruption == "bytes":
            staged.write_bytes(b"corrupt stage")
        else:
            fixture.ops.descriptors[staged] = b"corrupt-stage-acl"
        return fixture.ops.snapshot_by_handle(staged)

    fixture.ops.copy_create_new_and_flush = corrupt_copy  # type: ignore[method-assign]
    with pytest.raises(ReleaseTransitionError, match="staged launcher"):
        _result(fixture)
    assert staged is not None and not staged.exists()
    assert fixture.native.calls == []


def _path_state(ops: _FakeOps, path: Path) -> tuple[bool, bytes | None, bytes | None]:
    key = ops._key(path)
    if not key.exists():
        return False, None, None
    return True, key.read_bytes(), ops.descriptors[key]


@pytest.mark.parametrize(
    "fault",
    [
        "external-absent",
        "external-bytes",
        "external-acl",
        "native-bytes",
        "native-acl",
    ],
)
def test_recovery_requires_both_exact_backups_and_preserves_uncertain_evidence(
    tmp_path: Path,
    fault: str,
) -> None:
    fixture = _fixture(tmp_path)

    def crash(name: str) -> None:
        if name == "after_replace":
            raise _Crash("after replace")

    with pytest.raises(_Crash):
        _result(fixture, checkpoint=crash)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    intent = _canonical_payload(intent_path)
    external = Path(intent["external_backup_path"])
    native = Path(intent["native_backup_path"])
    if fault == "external-absent":
        external.unlink()
        fixture.ops.descriptors.pop(external)
    elif fault == "external-bytes":
        external.write_bytes(b"corrupt external")
    elif fault == "external-acl":
        fixture.ops.descriptors[external] = b"corrupt external acl"
    elif fault == "native-bytes":
        native.write_bytes(b"corrupt native")
    else:
        fixture.ops.descriptors[native] = b"corrupt native acl"
    tracked = (
        fixture.layout.launcher_path,
        external,
        native,
        Path(intent["stage_path"]),
    )
    before = tuple(_path_state(fixture.ops, path) for path in tracked)

    recovered = recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)
    after = tuple(_path_state(fixture.ops, path) for path in tracked)
    assert recovered.status == "uncertain"
    assert before == after
    assert len(fixture.native.calls) == 1


@pytest.mark.parametrize("damage", ["noncanonical", "oversized", "wrong-layout"])
def test_recovery_rejects_untrusted_intent_without_mutation(
    tmp_path: Path,
    damage: str,
) -> None:
    fixture = _fixture(tmp_path / "owner")

    def crash(name: str) -> None:
        if name == "after_intent":
            raise _Crash("after intent")

    with pytest.raises(_Crash):
        _result(fixture, checkpoint=crash)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    original_launcher = fixture.ops.snapshot_by_handle(fixture.layout.launcher_path)
    if damage == "noncanonical":
        intent_path.write_bytes(intent_path.read_bytes() + b" ")
    elif damage == "oversized":
        intent_path.write_bytes(b"{" + b"x" * (1024 * 1024 + 1))
    else:
        other = _fixture(tmp_path / "other")
        with pytest.raises(ReceiptValidationError, match="outside"):
            recover_pending_transition(other.layout, intent_path, ops=fixture.ops)
        assert fixture.ops.snapshot_by_handle(fixture.layout.launcher_path) == original_launcher
        return
    with pytest.raises(ReceiptValidationError):
        recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)
    assert fixture.ops.snapshot_by_handle(fixture.layout.launcher_path) == original_launcher
    assert fixture.native.calls == []


def test_existing_conflicting_result_is_not_overwritten(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    def crash(name: str) -> None:
        if name == "after_intent":
            raise _Crash("after intent")

    with pytest.raises(_Crash):
        _result(fixture, checkpoint=crash)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    result_path = intent_path.with_name(intent_path.name.replace(".intent.", ".result."))
    result_path.write_bytes(b"{}")
    original = result_path.read_bytes()
    with pytest.raises(ReceiptValidationError):
        recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)
    assert result_path.read_bytes() == original


def test_recovery_is_idempotent_and_uses_the_external_lock(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    def crash(name: str) -> None:
        if name == "after_intent":
            raise _Crash("after intent")

    with pytest.raises(_Crash):
        _result(fixture, checkpoint=crash)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    fixture.ops.max_active_locks = 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        recover = pool.submit(
            recover_pending_transition,
            fixture.layout,
            intent_path,
            ops=fixture.ops,
        )
        retry = pool.submit(
            transition_launcher,
            fixture.request,
            ops=fixture.ops,
            native=fixture.native,
        )
        results = (recover.result(timeout=5), retry.result(timeout=5))
    assert results[0].receipt_sha256 == results[1].receipt_sha256
    assert fixture.ops.max_active_locks == 1
    assert fixture.native.calls == []


def test_valid_content_addressed_backup_is_reused_without_overwrite(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    backup = fixture.layout.backup_directory / (
        f"{fixture.request.expected_current.sha256}.launcher"
    )
    fixture.ops.copy_create_new_and_flush(fixture.layout.launcher_path, backup)
    existing = fixture.ops.snapshot_by_handle(backup)
    fixture.ops.copy_calls.clear()
    assert _result(fixture).status == "success"
    assert fixture.ops.snapshot_by_handle(backup) == existing
    assert all(source != fixture.layout.launcher_path for source, _ in fixture.ops.copy_calls)


def test_corrupt_new_backup_copy_is_removed_and_never_replaced(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    original_copy = fixture.ops.copy_create_new_and_flush

    def corrupt_backup(source: Path, destination: Path) -> FileSnapshot:
        created = original_copy(source, destination)
        if fixture.ops._key(source) != fixture.layout.launcher_path:
            return created
        destination.write_bytes(b"bad backup")
        return fixture.ops.snapshot_by_handle(destination)

    fixture.ops.copy_create_new_and_flush = corrupt_backup  # type: ignore[method-assign]
    backup = fixture.layout.backup_directory / (
        f"{fixture.request.expected_current.sha256}.launcher"
    )
    with pytest.raises(ReleaseTransitionError, match="content-addressed backup"):
        _result(fixture)
    assert not backup.exists()
    assert fixture.native.calls == []


def test_native_wrapper_rejects_zero_unknown_flags_and_translates_win32_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = tmp_path / "current.exe"
    stage = tmp_path / "stage.exe"
    backup = tmp_path / "backup.exe"
    current.write_bytes(b"old")
    stage.write_bytes(b"new")
    native = WindowsReplaceFileNative()
    for flags in (0, 0x8, REPLACEFILE_WRITE_THROUGH | 0x8):
        with pytest.raises(ValueError, match="WRITE_THROUGH"):
            native.replace_file(current, stage, backup, flags=flags)
    monkeypatch.setattr(release_windows, "_replace_file_w", lambda *_args: 0)
    monkeypatch.setattr(
        release_windows.ctypes,
        "get_last_error",
        lambda: 5,
        raising=False,
    )

    def win_error(code: int) -> OSError:
        error = OSError(code, "fixture Win32 error")
        error.winerror = code  # type: ignore[attr-defined]
        return error

    monkeypatch.setattr(
        release_windows.ctypes,
        "WinError",
        win_error,
        raising=False,
    )
    with pytest.raises(OSError) as caught:
        native.replace_file(
            current,
            stage,
            backup,
            flags=REPLACEFILE_WRITE_THROUGH,
        )
    assert caught.value.winerror == 5


@pytest.mark.parametrize(
    "race",
    ["current", "desired", "stage-identity", "native-backup"],
)
def test_final_pre_native_boundary_revalidates_every_mutable_input(
    tmp_path: Path,
    race: str,
) -> None:
    fixture = _fixture(tmp_path)

    def race_at_boundary(name: str) -> None:
        if name != "before_replace":
            return
        intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
        intent = _canonical_payload(intent_path)
        if race == "current":
            fixture.layout.launcher_path.write_bytes(b"late current race\n")
        elif race == "desired":
            fixture.desired.write_bytes(b"late desired race\n")
        elif race == "stage-identity":
            stage = Path(intent["stage_path"])
            replacement = stage.with_name("attacker-stage")
            replacement.write_bytes(stage.read_bytes())
            fixture.ops.register(replacement, fixture.ops.descriptors[stage])
            os.replace(replacement, stage)
            fixture.ops.descriptors[stage] = fixture.ops.descriptors.pop(replacement)
        else:
            native_backup = Path(intent["native_backup_path"])
            native_backup.write_bytes(b"occupied")
            fixture.ops.register(native_backup, _OLD_DESCRIPTOR)

    with pytest.raises(ReleaseTransitionError):
        _result(fixture, checkpoint=race_at_boundary)
    assert fixture.native.calls == []


def test_idempotent_success_revalidates_external_backup(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    result = _result(fixture)
    result.external_backup_path.write_bytes(b"corrupt after receipt")
    with pytest.raises(TransitionEffectUncertainError, match="backup"):
        _result(fixture)
    assert len(fixture.native.calls) == 1


def test_previous_chain_requires_a_canonical_result_receipt(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    previous = fixture.layout.receipts_directory / "unrelated.json"
    previous.write_bytes(b"{}\n")
    chain = ReceiptChain(
        previous_receipt_path=previous,
        previous_receipt_sha256=hashlib.sha256(previous.read_bytes()).hexdigest(),
    )
    with pytest.raises(ReceiptValidationError, match="previous receipt"):
        transition_launcher(
            replace(fixture.request, receipt_chain=chain),
            ops=fixture.ops,
            native=fixture.native,
        )
    assert fixture.native.calls == []


def test_existing_result_current_evidence_cannot_contradict_intent(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    result = _result(fixture)
    payload = _canonical_payload(result.result_path)
    payload["current"]["sha256"] = "0" * 64
    result.result_path.write_bytes(
        (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
    )
    with pytest.raises(ReceiptValidationError, match="current"):
        _result(fixture)
    assert len(fixture.native.calls) == 1


def test_pending_intent_binds_canonical_lock_before_recovery_mutates(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    def crash(name: str) -> None:
        if name == "after_intent":
            raise _Crash("after intent")

    with pytest.raises(_Crash):
        _result(fixture, checkpoint=crash)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    alternate_lock = (tmp_path / "alternate" / "different.lock").resolve()
    alternate_lock.parent.mkdir(parents=True)
    alternate_layout = replace(fixture.layout, lock_path=alternate_lock)
    writes_before = tuple(fixture.ops.receipt_writes)
    fixture.ops.opened_locks.clear()

    with pytest.raises(ReceiptValidationError, match="lock"):
        recover_pending_transition(
            alternate_layout,
            intent_path,
            ops=fixture.ops,
        )
    assert fixture.ops.opened_locks == []
    assert tuple(fixture.ops.receipt_writes) == writes_before
    assert not next(
        fixture.layout.receipts_directory.glob("*.result.json"),
        None,
    )


def test_idempotent_success_requires_exact_recorded_file_id(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    result = _result(fixture)
    stable = fixture.layout.launcher_path
    replacement = stable.with_name("same-bytes-new-identity.exe")
    replacement.write_bytes(stable.read_bytes())
    fixture.ops.register(replacement, _OLD_DESCRIPTOR)
    os.replace(replacement, stable)
    fixture.ops.descriptors[stable] = fixture.ops.descriptors.pop(replacement)
    assert fixture.ops.snapshot_by_handle(stable).file_id != result.current.file_id

    with pytest.raises(TransitionEffectUncertainError, match="identity"):
        _result(fixture)
    assert len(fixture.native.calls) == 1


@pytest.mark.parametrize(
    "damage",
    ["status", "operation", "snapshot", "booleans", "contradiction"],
)
def test_previous_receipt_requires_complete_result_semantics(
    tmp_path: Path,
    damage: str,
) -> None:
    first = _fixture(tmp_path / "first")
    result = _result(first)
    payload = _canonical_payload(result.result_path)
    if damage == "status":
        payload["status"] = "mystery"
    elif damage == "operation":
        payload["operation"] = "erase"
    elif damage == "snapshot":
        payload["before"]["size"] = True
    elif damage == "booleans":
        payload["performed"] = "yes"
    else:
        payload["status"] = "no_effect"
    result.result_path.write_bytes(
        (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
    )
    chain = ReceiptChain(
        previous_receipt_path=result.result_path,
        previous_receipt_sha256=hashlib.sha256(result.result_path.read_bytes()).hexdigest(),
    )
    second = _fixture(tmp_path / "second")

    with pytest.raises(ReceiptValidationError, match="previous receipt"):
        transition_launcher(
            replace(second.request, receipt_chain=chain),
            ops=second.ops,
            native=second.native,
        )
    assert second.native.calls == []
    assert second.ops.receipt_writes == []


@pytest.mark.parametrize("descriptor_bytes", [262_000, 400_000])
def test_oversized_generated_receipts_are_rejected_before_any_write(
    tmp_path: Path,
    descriptor_bytes: int,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.ops.register(
        fixture.layout.launcher_path,
        b"a" * descriptor_bytes,
    )
    fixture.ops.register(
        fixture.desired,
        b"b" * descriptor_bytes,
    )
    request = replace(
        fixture.request,
        expected_current=fixture.ops.snapshot_by_handle(fixture.layout.launcher_path),
        expected_desired=fixture.ops.snapshot_by_handle(fixture.desired),
    )
    fixture.ops.snapshot_calls.clear()
    fixture.ops.snapshot_counts.clear()

    with pytest.raises(ReceiptValidationError, match="size limit"):
        transition_launcher(request, ops=fixture.ops, native=fixture.native)
    assert fixture.ops.receipt_writes == []
    assert fixture.native.calls == []


def test_no_effect_recovery_removes_the_exact_owned_stage(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.native.fail = "before"
    with pytest.raises(TransitionEffectUncertainError):
        _result(fixture)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    stage = fixture.native.calls[0][1]
    assert stage.exists()
    expected_stage = fixture.ops.snapshot_by_handle(stage)

    recovered = recover_pending_transition(
        fixture.layout,
        intent_path,
        ops=fixture.ops,
    )
    assert recovered.status == "no_effect"
    assert not stage.exists()
    assert expected_stage.path == str(stage)


def test_no_effect_recovery_preserves_and_reports_an_untrusted_stage(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.native.fail = "before"
    with pytest.raises(TransitionEffectUncertainError):
        _result(fixture)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    stage = fixture.native.calls[0][1]
    stage.write_bytes(b"foreign stage")
    fixture.ops.descriptors[stage] = b"foreign-stage-acl"
    before = _path_state(fixture.ops, stage)

    recovered = recover_pending_transition(
        fixture.layout,
        intent_path,
        ops=fixture.ops,
    )
    assert recovered.status == "uncertain"
    assert _path_state(fixture.ops, stage) == before


def _write_canonical_payload(path: Path, payload: dict[str, Any]) -> None:
    path.write_bytes(
        (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
    )


def test_no_effect_recovery_never_deletes_same_evidence_foreign_identity(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.native.fail = "before"
    with pytest.raises(TransitionEffectUncertainError):
        _result(fixture)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    stage = fixture.native.calls[0][1]
    original = fixture.ops.snapshot_by_handle(stage)
    foreign_path = stage.with_name("same-evidence-foreign.stage")
    foreign_path.write_bytes(stage.read_bytes())
    fixture.ops.register(foreign_path, fixture.ops.descriptors[stage])
    os.replace(foreign_path, stage)
    fixture.ops.descriptors[stage] = fixture.ops.descriptors.pop(foreign_path)
    foreign = fixture.ops.snapshot_by_handle(stage)
    assert foreign.file_id != original.file_id

    recovered = recover_pending_transition(
        fixture.layout,
        intent_path,
        ops=fixture.ops,
    )
    assert recovered.status == "uncertain"
    assert fixture.ops.snapshot_by_handle(stage) == foreign


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_no_effect_recovery_requires_valid_stage_evidence_before_delete(
    tmp_path: Path,
    damage: str,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.native.fail = "before"
    with pytest.raises(TransitionEffectUncertainError):
        _result(fixture)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    intent = _canonical_payload(intent_path)
    stage = Path(intent["stage_path"])
    stage_evidence = Path(intent["stage_evidence_path"])
    before = fixture.ops.snapshot_by_handle(stage)
    if damage == "missing":
        stage_evidence.unlink()
    else:
        stage_evidence.write_bytes(b"{}\n")

    recovered = recover_pending_transition(
        fixture.layout,
        intent_path,
        ops=fixture.ops,
    )
    assert recovered.status == "uncertain"
    assert fixture.ops.snapshot_by_handle(stage) == before


def test_no_effect_result_links_exact_stage_evidence_before_cleanup(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.native.fail = "before"
    with pytest.raises(TransitionEffectUncertainError):
        _result(fixture)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    intent = _canonical_payload(intent_path)
    stage = Path(intent["stage_path"])
    stage_evidence = Path(intent["stage_evidence_path"])
    expected_sha = hashlib.sha256(stage_evidence.read_bytes()).hexdigest()

    recovered = recover_pending_transition(
        fixture.layout,
        intent_path,
        ops=fixture.ops,
    )
    result_payload = _canonical_payload(recovered.result_path)
    assert recovered.status == "no_effect"
    assert not stage.exists()
    assert recovered.stage_evidence_path == stage_evidence
    assert recovered.stage_evidence_sha256 == expected_sha
    assert result_payload["stage_evidence_sha256"] == expected_sha


def test_no_stage_is_represented_as_canonical_absence(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    def crash(name: str) -> None:
        if name == "after_intent":
            raise _Crash("after intent")

    with pytest.raises(_Crash):
        _result(fixture, checkpoint=crash)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    intent = _canonical_payload(intent_path)
    assert not Path(intent["stage_path"]).exists()
    assert not Path(intent["stage_evidence_path"]).exists()

    recovered = recover_pending_transition(
        fixture.layout,
        intent_path,
        ops=fixture.ops,
    )
    assert recovered.status == "no_effect"
    assert recovered.stage_evidence_sha256 is None
    assert _canonical_payload(recovered.result_path)["stage_evidence_sha256"] is None


@pytest.mark.parametrize("field", ["operation", "status"])
def test_previous_result_unhashable_scalars_fail_as_receipt_validation(
    tmp_path: Path,
    field: str,
) -> None:
    first = _fixture(tmp_path / "first")
    result = _result(first)
    payload = _canonical_payload(result.result_path)
    payload[field] = []
    _write_canonical_payload(result.result_path, payload)
    chain = ReceiptChain(
        previous_receipt_path=result.result_path,
        previous_receipt_sha256=hashlib.sha256(result.result_path.read_bytes()).hexdigest(),
    )
    second = _fixture(tmp_path / "second")

    with pytest.raises(ReceiptValidationError, match=field):
        transition_launcher(
            replace(second.request, receipt_chain=chain),
            ops=second.ops,
            native=second.native,
        )
    assert second.native.calls == []
    assert second.ops.receipt_writes == []


def test_pending_intent_unhashable_operation_is_receipt_validation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    def crash(name: str) -> None:
        if name == "after_intent":
            raise _Crash("after intent")

    with pytest.raises(_Crash):
        _result(fixture, checkpoint=crash)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    payload = _canonical_payload(intent_path)
    payload["operation"] = []
    _write_canonical_payload(intent_path, payload)

    with pytest.raises(ReceiptValidationError, match="operation"):
        recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)


def test_existing_result_unhashable_status_is_receipt_validation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    result = _result(fixture)
    payload = _canonical_payload(result.result_path)
    payload["status"] = []
    _write_canonical_payload(result.result_path, payload)

    with pytest.raises(ReceiptValidationError, match="status"):
        _result(fixture)
    assert len(fixture.native.calls) == 1


@pytest.mark.parametrize(
    ("performed", "recovered"),
    [(False, False), (True, True)],
)
def test_existing_success_requires_coherent_status_flag_matrix(
    tmp_path: Path,
    performed: bool,
    recovered: bool,
) -> None:
    fixture = _fixture(tmp_path)
    result = _result(fixture)
    payload = _canonical_payload(result.result_path)
    payload["performed"] = performed
    payload["recovered"] = recovered
    _write_canonical_payload(result.result_path, payload)

    with pytest.raises(ReceiptValidationError, match="flags"):
        _result(fixture)
    assert len(fixture.native.calls) == 1


@pytest.mark.parametrize(
    ("receipt", "fault"),
    [
        ("intent", "missing"),
        ("intent", "wrong"),
        ("result", "missing"),
        ("result", "wrong"),
    ],
)
def test_receipt_writer_must_persist_exact_canonical_bytes(
    tmp_path: Path,
    receipt: str,
    fault: str,
) -> None:
    fixture = _fixture(tmp_path)
    original_write = fixture.ops.write_create_new_and_flush

    def faulty_write(path: Path, payload: bytes) -> None:
        if not path.name.endswith(f".{receipt}.json"):
            original_write(path, payload)
        elif fault == "wrong":
            original_write(path, b'{"wrong":true}\n')

    fixture.ops.write_create_new_and_flush = faulty_write  # type: ignore[method-assign]
    with pytest.raises(ReceiptValidationError, match="write verification"):
        _result(fixture)

    if receipt == "intent":
        assert fixture.native.calls == []
    else:
        assert len(fixture.native.calls) == 1
        assert fixture.native.calls[0][2].exists()


@pytest.mark.parametrize("receipt", ["intent", "result"])
def test_receipt_writer_exception_after_exact_write_is_recoverable(
    tmp_path: Path,
    receipt: str,
) -> None:
    fixture = _fixture(tmp_path)
    original_write = fixture.ops.write_create_new_and_flush

    def write_then_fail(path: Path, payload: bytes) -> None:
        original_write(path, payload)
        if path.name.endswith(f".{receipt}.json"):
            raise OSError("synthetic post-write failure")

    fixture.ops.write_create_new_and_flush = write_then_fail  # type: ignore[method-assign]
    with pytest.raises(OSError, match="post-write failure"):
        _result(fixture)
    fixture.ops.write_create_new_and_flush = original_write  # type: ignore[method-assign]

    recovered = _result(fixture)
    assert recovered.status == ("no_effect" if receipt == "intent" else "success")
    assert len(fixture.native.calls) == (0 if receipt == "intent" else 1)


@pytest.mark.parametrize(
    ("evidence", "damage"),
    [
        ("result", "missing"),
        ("result", "corrupt"),
        ("external_backup", "missing"),
        ("external_backup", "corrupt"),
    ],
)
def test_native_backup_cleanup_requires_exact_success_evidence(
    tmp_path: Path,
    evidence: str,
    damage: str,
) -> None:
    fixture = _fixture(tmp_path)
    damaged: Path | None = None

    def damage_after_result(name: str) -> None:
        nonlocal damaged
        if name != "after_result":
            return
        intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
        intent = _canonical_payload(intent_path)
        if evidence == "result":
            damaged = Path(str(intent_path).replace(".intent.json", ".result.json"))
        else:
            damaged = Path(intent["external_backup_path"])
        if damage == "missing":
            damaged.unlink()
        else:
            damaged.write_bytes(b"corrupt cleanup evidence\n")

    with pytest.raises(TransitionEffectUncertainError, match="cleanup evidence"):
        _result(fixture, checkpoint=damage_after_result)

    assert damaged is not None
    native_backup = fixture.native.calls[0][2]
    assert native_backup.exists()


def test_atomic_cleanup_boundary_preserves_foreign_native_backup(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    foreign: FileSnapshot | None = None

    def arm_atomic_replacement(name: str) -> None:
        if name != "after_verification":
            return
        native_backup = fixture.native.calls[0][2]
        foreign_path = native_backup.with_name(native_backup.name + ".foreign")
        foreign_path.write_bytes(native_backup.read_bytes())
        fixture.ops.register(foreign_path, fixture.ops.descriptors[native_backup])

        def replace_before_atomic_remove() -> None:
            nonlocal foreign
            os.replace(foreign_path, native_backup)
            fixture.ops.descriptors[native_backup] = fixture.ops.descriptors.pop(foreign_path)
            foreign = fixture.ops.snapshot_by_handle(native_backup)

        fixture.ops.remove_hooks[native_backup] = replace_before_atomic_remove

    with pytest.raises(TransitionEffectUncertainError, match="cleanup target"):
        _result(fixture, checkpoint=arm_atomic_replacement)

    assert foreign is not None
    native_backup = fixture.native.calls[0][2]
    assert fixture.ops.snapshot_by_handle(native_backup) == foreign


def test_atomic_remove_seam_is_idempotent_and_rejects_foreign_identity(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    absent = tmp_path / "absent-owned.launcher"
    absent_snapshot = fixture.ops.copy_create_new_and_flush(fixture.layout.launcher_path, absent)
    absent.unlink()
    fixture.ops.descriptors.pop(absent)
    fixture.ops.remove_file_if_snapshot(absent, absent_snapshot)

    target = tmp_path / "target-owned.launcher"
    expected = fixture.ops.copy_create_new_and_flush(fixture.layout.launcher_path, target)
    foreign_path = tmp_path / "target-foreign.launcher"
    fixture.ops.copy_create_new_and_flush(fixture.layout.launcher_path, foreign_path)
    os.replace(foreign_path, target)
    fixture.ops.descriptors[target] = fixture.ops.descriptors.pop(foreign_path)
    foreign = fixture.ops.snapshot_by_handle(target)
    assert foreign.file_id != expected.file_id

    with pytest.raises(ReleaseTransitionError, match="identity mismatch"):
        fixture.ops.remove_file_if_snapshot(target, expected)
    assert fixture.ops.snapshot_by_handle(target) == foreign


@pytest.mark.parametrize("receipt", ["intent", "result"])
def test_receipt_writer_exception_before_create_retries_once(
    tmp_path: Path,
    receipt: str,
) -> None:
    fixture = _fixture(tmp_path)
    original_write = fixture.ops.write_create_new_and_flush

    def fail_before_write(path: Path, payload: bytes) -> None:
        if path.name.endswith(f".{receipt}.json"):
            raise OSError("synthetic pre-write failure")
        original_write(path, payload)

    fixture.ops.write_create_new_and_flush = fail_before_write  # type: ignore[method-assign]
    with pytest.raises(OSError, match="pre-write failure"):
        _result(fixture)
    fixture.ops.write_create_new_and_flush = original_write  # type: ignore[method-assign]

    assert _result(fixture).status == "success"
    assert len(fixture.native.calls) == 1


@pytest.mark.parametrize("origin", ["direct", "recovery"])
def test_success_requires_consumed_stage_path_after_result(
    tmp_path: Path,
    origin: str,
) -> None:
    fixture = _fixture(tmp_path)
    foreign_stage: FileSnapshot | None = None

    def recreate_stage() -> None:
        nonlocal foreign_stage
        intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
        stage = Path(_canonical_payload(intent_path)["stage_path"])
        stage.write_bytes(fixture.desired.read_bytes())
        fixture.ops.register(stage, fixture.ops.descriptors[fixture.desired])
        foreign_stage = fixture.ops.snapshot_by_handle(stage)

    if origin == "direct":

        def recreate_after_result(name: str) -> None:
            if name == "after_result":
                recreate_stage()

        with pytest.raises(TransitionEffectUncertainError, match="stage evidence"):
            _result(fixture, checkpoint=recreate_after_result)
    else:
        fixture.ops.write_error_suffix = ".result.json"
        with pytest.raises(OSError, match="receipt write failure"):
            _result(fixture)
        fixture.ops.write_error_suffix = None
        intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
        original_write = fixture.ops.write_create_new_and_flush

        def recreate_during_result(path: Path, payload: bytes) -> None:
            original_write(path, payload)
            if path.name.endswith(".result.json"):
                recreate_stage()

        fixture.ops.write_create_new_and_flush = recreate_during_result  # type: ignore[method-assign]
        with pytest.raises(TransitionEffectUncertainError, match="stage evidence"):
            recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)

    assert foreign_stage is not None
    stage = Path(foreign_stage.path)
    assert fixture.ops.snapshot_by_handle(stage) == foreign_stage
    native_backup = fixture.native.calls[0][2]
    assert native_backup.exists()
    with pytest.raises(TransitionEffectUncertainError):
        _result(fixture)
    assert native_backup.exists()


@pytest.mark.parametrize(
    "evidence",
    ["launcher", "intent", "result", "external_backup"],
)
def test_success_cleanup_revalidates_evidence_after_target_observation(
    tmp_path: Path,
    evidence: str,
) -> None:
    fixture = _fixture(tmp_path)
    native_backup: Path | None = None

    def arm_target_observation(name: str) -> None:
        nonlocal native_backup
        if name != "after_verification":
            return
        intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
        intent = _canonical_payload(intent_path)
        native_backup = fixture.native.calls[0][2]
        snapshots_before = fixture.ops.snapshot_counts[native_backup]

        def damage_during_target_snapshot(count: int) -> None:
            if count != snapshots_before + 1:
                return
            if evidence == "launcher":
                fixture.layout.launcher_path.unlink()
                fixture.ops.descriptors.pop(fixture.layout.launcher_path)
            elif evidence == "intent":
                intent_path.write_bytes(b'{"broken":true}\n')
            elif evidence == "result":
                result_path = Path(str(intent_path).replace(".intent.json", ".result.json"))
                result_path.write_bytes(b'{"broken":true}\n')
            else:
                Path(intent["external_backup_path"]).write_bytes(b"broken backup\n")

        fixture.ops.snapshot_hooks[native_backup] = damage_during_target_snapshot

    expected_message = (
        "cleanup evidence" if evidence in {"result", "external_backup"} else "stage evidence"
    )
    with pytest.raises(TransitionEffectUncertainError, match=expected_message):
        _result(fixture, checkpoint=arm_target_observation)

    assert native_backup is not None and native_backup.exists()


def test_no_stage_recovery_revalidates_canonical_absence_after_result_write(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    def crash_after_intent(name: str) -> None:
        if name == "after_intent":
            raise _Crash("pending without stage")

    with pytest.raises(_Crash, match="pending without stage"):
        _result(fixture, checkpoint=crash_after_intent)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    intent = _canonical_payload(intent_path)
    stage = Path(intent["stage_path"])
    sidecar = Path(intent["stage_evidence_path"])
    original_write = fixture.ops.write_create_new_and_flush

    def create_sidecar_after_result(path: Path, payload: bytes) -> None:
        original_write(path, payload)
        if path.name.endswith(".result.json"):
            sidecar.write_bytes(b'{"broken":true}\n')

    fixture.ops.write_create_new_and_flush = create_sidecar_after_result  # type: ignore[method-assign]
    with pytest.raises(TransitionEffectUncertainError, match="stage evidence"):
        recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)

    assert not stage.exists()
    assert sidecar.read_bytes() == b'{"broken":true}\n'
    result_path = next(fixture.layout.receipts_directory.glob("*.result.json"))
    assert _canonical_payload(result_path)["status"] == "no_effect"


@pytest.mark.parametrize(
    ("target", "race"),
    [
        ("external_backup", "collision"),
        ("external_backup", "replacement"),
        ("stage", "collision"),
        ("stage", "replacement"),
    ],
)
def test_copy_creation_never_deletes_or_adopts_foreign_identity(
    tmp_path: Path,
    target: str,
    race: str,
) -> None:
    fixture = _fixture(tmp_path)
    original_copy = fixture.ops.copy_create_new_and_flush
    target_source = fixture.layout.launcher_path if target == "external_backup" else fixture.desired
    foreign: tuple[Path, FileSnapshot] | None = None

    def race_copy(source: Path, destination: Path) -> FileSnapshot:
        nonlocal foreign
        source_key = fixture.ops._key(source)
        destination_key = fixture.ops._key(destination)
        if source_key != target_source:
            original_copy(source, destination)
            return fixture.ops.snapshot_by_handle(destination_key)
        if race == "collision":
            destination_key.write_bytes(source_key.read_bytes())
            fixture.ops.register(destination_key, fixture.ops.descriptors[source_key])
            foreign = destination_key, fixture.ops.snapshot_by_handle(destination_key)
            raise OSError("synthetic create collision")
        original_copy(source, destination)
        owned = fixture.ops.snapshot_by_handle(destination_key)
        foreign_path = destination_key.with_name(destination_key.name + ".foreign")
        foreign_path.write_bytes(destination_key.read_bytes())
        fixture.ops.register(foreign_path, fixture.ops.descriptors[destination_key])
        os.replace(foreign_path, destination_key)
        fixture.ops.descriptors[destination_key] = fixture.ops.descriptors.pop(foreign_path)
        foreign_snapshot = fixture.ops.snapshot_by_handle(destination_key)
        assert foreign_snapshot.file_id != owned.file_id
        foreign = destination_key, foreign_snapshot
        return owned

    fixture.ops.copy_create_new_and_flush = race_copy  # type: ignore[method-assign]
    if race == "collision":
        with pytest.raises(OSError, match="create collision"):
            _result(fixture)
    else:
        with pytest.raises(ReleaseTransitionError, match="ownership"):
            _result(fixture)

    assert foreign is not None
    path, expected = foreign
    assert fixture.ops.snapshot_by_handle(path) == expected
    assert fixture.native.calls == []


def test_direct_pre_native_cleanup_revalidates_sidecar_after_target_cas(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    armed: tuple[Path, Path, FileSnapshot] | None = None

    def crash_after_arming(name: str) -> None:
        nonlocal armed
        if name != "before_replace":
            return
        intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
        intent = _canonical_payload(intent_path)
        stage = Path(intent["stage_path"])
        sidecar = Path(intent["stage_evidence_path"])
        stage_before = fixture.ops.snapshot_by_handle(stage)
        snapshots_before = fixture.ops.snapshot_counts[stage]

        def corrupt_sidecar_at_cleanup(count: int) -> None:
            if count == snapshots_before + 1:
                sidecar.write_bytes(b'{"broken":true}\n')

        fixture.ops.snapshot_hooks[stage] = corrupt_sidecar_at_cleanup
        armed = stage, sidecar, stage_before
        raise _Crash("armed pre-native cleanup")

    with pytest.raises(_Crash, match="armed pre-native cleanup") as caught:
        _result(fixture, checkpoint=crash_after_arming)

    assert armed is not None
    stage, sidecar, stage_before = armed
    assert fixture.ops.snapshot_by_handle(stage) == stage_before
    assert sidecar.read_bytes() == b'{"broken":true}\n'
    assert any("stage evidence" in note for note in getattr(caught.value, "__notes__", ()))
    assert fixture.native.calls == []


def test_direct_success_cleanup_revalidates_sidecar_after_target_cas(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    armed: tuple[Path, Path, FileSnapshot] | None = None

    def arm_after_verification(name: str) -> None:
        nonlocal armed
        if name != "after_verification":
            return
        intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
        sidecar = Path(_canonical_payload(intent_path)["stage_evidence_path"])
        native_backup = fixture.native.calls[0][2]
        backup_before = fixture.ops.snapshot_by_handle(native_backup)
        snapshots_before = fixture.ops.snapshot_counts[native_backup]

        def corrupt_sidecar_at_cleanup(count: int) -> None:
            if count == snapshots_before + 1:
                sidecar.write_bytes(b'{"broken":true}\n')

        fixture.ops.snapshot_hooks[native_backup] = corrupt_sidecar_at_cleanup
        armed = native_backup, sidecar, backup_before

    with pytest.raises(TransitionEffectUncertainError, match="stage evidence"):
        _result(fixture, checkpoint=arm_after_verification)

    assert armed is not None
    native_backup, sidecar, backup_before = armed
    assert fixture.ops.snapshot_by_handle(native_backup) == backup_before
    assert sidecar.read_bytes() == b'{"broken":true}\n'
    result_path = next(fixture.layout.receipts_directory.glob("*.result.json"))
    assert _canonical_payload(result_path)["status"] == "success"


def test_recovered_success_cleanup_revalidates_sidecar_after_target_cas(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.ops.write_error_suffix = ".result.json"
    with pytest.raises(OSError, match="receipt write failure"):
        _result(fixture)
    fixture.ops.write_error_suffix = None
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    intent = _canonical_payload(intent_path)
    sidecar = Path(intent["stage_evidence_path"])
    native_backup = Path(intent["native_backup_path"])
    backup_before = fixture.ops.snapshot_by_handle(native_backup)
    snapshots_before = fixture.ops.snapshot_counts[native_backup]

    def corrupt_sidecar_at_cleanup(count: int) -> None:
        if count == snapshots_before + 2:
            sidecar.write_bytes(b'{"broken":true}\n')

    fixture.ops.snapshot_hooks[native_backup] = corrupt_sidecar_at_cleanup
    with pytest.raises(TransitionEffectUncertainError, match="stage evidence"):
        recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)

    assert fixture.ops.snapshot_by_handle(native_backup) == backup_before
    assert sidecar.read_bytes() == b'{"broken":true}\n'
    result_path = next(fixture.layout.receipts_directory.glob("*.result.json"))
    assert _canonical_payload(result_path)["status"] == "success"


def test_recovery_revalidates_launcher_before_no_effect_cleanup(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.native.fail = "before"
    with pytest.raises(TransitionEffectUncertainError):
        _result(fixture)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    intent = _canonical_payload(intent_path)
    stage = Path(intent["stage_path"])
    stage_before = fixture.ops.snapshot_by_handle(stage)
    native_calls = tuple(fixture.native.calls)
    original_write = fixture.ops.write_create_new_and_flush

    def race_after_result(path: Path, payload: bytes) -> None:
        original_write(path, payload)
        if path.name.endswith(".result.json"):
            fixture.layout.launcher_path.write_bytes(b"recovery-raced-launcher\n")

    fixture.ops.write_create_new_and_flush = race_after_result  # type: ignore[method-assign]
    with pytest.raises(TransitionEffectUncertainError, match="stage evidence"):
        recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)

    assert fixture.ops.snapshot_by_handle(stage) == stage_before
    assert tuple(fixture.native.calls) == native_calls
    result_path = next(fixture.layout.receipts_directory.glob("*.result.json"))
    assert _canonical_payload(result_path)["status"] == "no_effect"


def test_recovery_revalidates_sidecar_after_cleanup_target_cas(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.native.fail = "before"
    with pytest.raises(TransitionEffectUncertainError):
        _result(fixture)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    intent = _canonical_payload(intent_path)
    stage = Path(intent["stage_path"])
    sidecar = Path(intent["stage_evidence_path"])
    stage_before = fixture.ops.snapshot_by_handle(stage)
    snapshots_before = fixture.ops.snapshot_counts[stage]

    def corrupt_sidecar_at_cleanup(count: int) -> None:
        if count == snapshots_before + 2:
            sidecar.write_bytes(b'{"broken":true}\n')

    fixture.ops.snapshot_hooks[stage] = corrupt_sidecar_at_cleanup
    with pytest.raises(TransitionEffectUncertainError, match="stage evidence"):
        recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)

    assert fixture.ops.snapshot_by_handle(stage) == stage_before
    assert sidecar.read_bytes() == b'{"broken":true}\n'
    result_path = next(fixture.layout.receipts_directory.glob("*.result.json"))
    assert _canonical_payload(result_path)["status"] == "no_effect"


def _damage_result_receipt(path: Path, damage: str) -> None:
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b'{"broken":true}\n')
    else:
        replacement = path.with_name(f"foreign-{path.name}")
        replacement.write_bytes(b'{"foreign":true}\n')
        os.replace(replacement, path)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "replacement"])
def test_no_effect_stage_cleanup_revalidates_exact_result_after_target_snapshot(
    tmp_path: Path,
    damage: str,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.native.fail = "before"
    with pytest.raises(TransitionEffectUncertainError):
        _result(fixture)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    intent = _canonical_payload(intent_path)
    stage = Path(intent["stage_path"])
    stage_before = fixture.ops.snapshot_by_handle(stage)
    snapshots_before = fixture.ops.snapshot_counts[stage]

    def damage_result_at_cleanup(count: int) -> None:
        if count == snapshots_before + 2:
            result_path = next(fixture.layout.receipts_directory.glob("*.result.json"))
            _damage_result_receipt(result_path, damage)

    fixture.ops.snapshot_hooks[stage] = damage_result_at_cleanup
    with pytest.raises(TransitionEffectUncertainError, match="result"):
        recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)

    assert fixture.ops.snapshot_by_handle(stage) == stage_before


@pytest.mark.parametrize("damage", ["missing", "corrupt", "replacement"])
def test_no_effect_without_stage_revalidates_exact_result_before_return(
    tmp_path: Path,
    damage: str,
) -> None:
    fixture = _fixture(tmp_path)

    def crash_after_intent(name: str) -> None:
        if name == "after_intent":
            raise _Crash("after intent")

    with pytest.raises(_Crash, match="after intent"):
        _result(fixture, checkpoint=crash_after_intent)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    launcher = fixture.layout.launcher_path
    snapshots_before = fixture.ops.snapshot_counts.get(launcher, 0)

    def damage_result_during_launcher_check(count: int) -> None:
        if count == snapshots_before + 2:
            result_path = next(fixture.layout.receipts_directory.glob("*.result.json"))
            _damage_result_receipt(result_path, damage)

    fixture.ops.snapshot_hooks[launcher] = damage_result_during_launcher_check
    with pytest.raises(TransitionEffectUncertainError, match="result"):
        recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "replacement"])
def test_existing_no_effect_stage_result_is_revalidated_after_target_snapshot(
    tmp_path: Path,
    damage: str,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.native.fail = "before"
    with pytest.raises(TransitionEffectUncertainError):
        _result(fixture)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    intent = _canonical_payload(intent_path)
    stage = Path(intent["stage_path"])
    fixture.ops.remove_errors[stage] = OSError("preserve stage for retry")
    with pytest.raises(TransitionEffectUncertainError):
        recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)
    fixture.ops.remove_errors.pop(stage)
    stage_before = fixture.ops.snapshot_by_handle(stage)
    snapshots_before = fixture.ops.snapshot_counts[stage]

    def damage_result_at_retry_cleanup(count: int) -> None:
        if count == snapshots_before + 2:
            result_path = next(fixture.layout.receipts_directory.glob("*.result.json"))
            _damage_result_receipt(result_path, damage)

    fixture.ops.snapshot_hooks[stage] = damage_result_at_retry_cleanup
    with pytest.raises(TransitionEffectUncertainError, match="result"):
        recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)

    assert fixture.ops.snapshot_by_handle(stage) == stage_before


@pytest.mark.parametrize("damage", ["missing", "corrupt", "replacement"])
def test_existing_no_effect_without_stage_revalidates_result_before_return(
    tmp_path: Path,
    damage: str,
) -> None:
    fixture = _fixture(tmp_path)

    def crash_after_intent(name: str) -> None:
        if name == "after_intent":
            raise _Crash("after intent")

    with pytest.raises(_Crash, match="after intent"):
        _result(fixture, checkpoint=crash_after_intent)
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    recovered = recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)
    assert recovered.status == "no_effect"
    launcher = fixture.layout.launcher_path
    snapshots_before = fixture.ops.snapshot_counts[launcher]

    def damage_result_during_retry_check(count: int) -> None:
        if count == snapshots_before + 2:
            _damage_result_receipt(recovered.result_path, damage)

    fixture.ops.snapshot_hooks[launcher] = damage_result_during_retry_check
    with pytest.raises(TransitionEffectUncertainError, match="result"):
        recover_pending_transition(fixture.layout, intent_path, ops=fixture.ops)


@pytest.mark.parametrize("receipt", ["sidecar", "intent"])
def test_pre_native_receipt_read_cannot_swap_exact_stage_identity(
    tmp_path: Path,
    receipt: str,
) -> None:
    fixture = _fixture(tmp_path)
    original_stage: FileSnapshot | None = None
    foreign_stage: FileSnapshot | None = None
    stage_path: Path | None = None

    def arm_swap(name: str) -> None:
        nonlocal original_stage, foreign_stage, stage_path
        if name != "after_stage":
            return
        intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
        intent = _canonical_payload(intent_path)
        stage_path = Path(intent["stage_path"])
        sidecar = Path(intent["stage_evidence_path"])
        target = sidecar if receipt == "sidecar" else intent_path
        original_stage = fixture.ops.snapshot_by_handle(stage_path)
        reads_before = fixture.ops.read_counts.get(target, 0)

        def swap_stage_on_read(count: int) -> None:
            nonlocal foreign_stage
            if count != reads_before + 1:
                return
            assert stage_path is not None
            replacement = stage_path.with_name(f"foreign-{stage_path.name}")
            replacement.write_bytes(stage_path.read_bytes())
            fixture.ops.register(replacement, fixture.ops.descriptors[stage_path])
            os.replace(replacement, stage_path)
            fixture.ops.descriptors[stage_path] = fixture.ops.descriptors.pop(replacement)
            foreign_stage = fixture.ops.snapshot_by_handle(stage_path)

        fixture.ops.read_hooks[target] = swap_stage_on_read

    with pytest.raises(ReleaseTransitionError):
        _result(fixture, checkpoint=arm_swap)

    assert fixture.native.calls == []
    assert fixture.layout.launcher_path.read_bytes() == b"old-launcher-arbitrary-baseline\n"
    assert stage_path is not None
    assert original_stage is not None and foreign_stage is not None
    assert foreign_stage.file_id != original_stage.file_id
    assert fixture.ops.snapshot_by_handle(stage_path) == foreign_stage


def _damage_stage_evidence_at_checkpoint(
    fixture: _Fixture,
    damage: str,
) -> tuple[Path, tuple[bool, bytes | None]]:
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    sidecar = Path(_canonical_payload(intent_path)["stage_evidence_path"])
    if damage == "missing":
        sidecar.unlink()
    elif damage == "corrupt":
        sidecar.write_bytes(b'{"broken":true}\n')
    else:
        payload = _canonical_payload(sidecar)
        payload["stage"]["file_id"] = "f" * 32
        replacement = sidecar.with_name("foreign-stage-evidence.json")
        _write_canonical_payload(replacement, payload)
        os.replace(replacement, sidecar)
    state = (sidecar.exists(), sidecar.read_bytes() if sidecar.exists() else None)
    return sidecar, state


@pytest.mark.parametrize("damage", ["missing", "corrupt", "replacement"])
def test_stage_evidence_mismatch_before_native_preserves_everything(
    tmp_path: Path,
    damage: str,
) -> None:
    fixture = _fixture(tmp_path)
    damaged: tuple[Path, tuple[bool, bytes | None]] | None = None

    def damage_after_stage(name: str) -> None:
        nonlocal damaged
        if name == "after_stage":
            damaged = _damage_stage_evidence_at_checkpoint(fixture, damage)

    with pytest.raises(TransitionEffectUncertainError, match="stage evidence"):
        _result(fixture, checkpoint=damage_after_stage)
    assert damaged is not None
    sidecar, expected_state = damaged
    intent_path = next(fixture.layout.receipts_directory.glob("*.intent.json"))
    intent = _canonical_payload(intent_path)
    stage = Path(intent["stage_path"])
    native_backup = Path(intent["native_backup_path"])
    assert fixture.native.calls == []
    assert stage.exists()
    assert not native_backup.exists()
    assert not next(fixture.layout.receipts_directory.glob("*.result.json"), None)
    assert (sidecar.exists(), sidecar.read_bytes() if sidecar.exists() else None) == (
        expected_state
    )


@pytest.mark.parametrize(
    ("checkpoint_name", "damage"),
    [
        ("after_replace", "missing"),
        ("after_replace", "corrupt"),
        ("after_replace", "replacement"),
        ("after_verification", "replacement"),
        ("after_result", "missing"),
        ("after_result", "corrupt"),
        ("after_result", "replacement"),
    ],
)
def test_stage_evidence_mismatch_after_native_preserves_backup_and_surfaces_uncertain(
    tmp_path: Path,
    checkpoint_name: str,
    damage: str,
) -> None:
    fixture = _fixture(tmp_path)
    damaged: tuple[Path, tuple[bool, bytes | None]] | None = None

    def damage_after_native(name: str) -> None:
        nonlocal damaged
        if name == checkpoint_name:
            damaged = _damage_stage_evidence_at_checkpoint(fixture, damage)

    with pytest.raises(TransitionEffectUncertainError, match="stage evidence"):
        _result(fixture, checkpoint=damage_after_native)
    assert damaged is not None
    sidecar, expected_state = damaged
    assert len(fixture.native.calls) == 1
    _stable, stage, native_backup, _flags = fixture.native.calls[0]
    assert not stage.exists()
    assert native_backup.exists()
    result_path = next(
        fixture.layout.receipts_directory.glob("*.result.json"),
        None,
    )
    if checkpoint_name == "after_result":
        assert result_path is not None
        with pytest.raises(TransitionEffectUncertainError, match="stage evidence"):
            _result(fixture)
        assert native_backup.exists()
    else:
        assert result_path is None
    assert (sidecar.exists(), sidecar.read_bytes() if sidecar.exists() else None) == (
        expected_state
    )


# endregion [02]
