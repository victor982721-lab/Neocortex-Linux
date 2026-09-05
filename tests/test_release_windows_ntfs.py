# region [00] Contexto del módulo
# Módulo: tests/test_release_windows_ntfs.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations


import ast
import hashlib
import importlib
import importlib.util
import inspect
import os
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from radon.complexity import cc_visit  # type: ignore[import-untyped]

from tests.audit_lab_guard import (
    AUDIT_LAB_ENVIRONMENT,
    capture_audit_lab_directory_identity,
    require_unchanged_audit_lab_directory,
)
from tools.release_windows import (
    REPLACEFILE_WRITE_THROUGH,
    ReleaseFileOperations,
    recover_pending_transition,
    transition_launcher,
)
from tools.release_windows_receipts import (
    LauncherTransitionRequest,
    ReceiptChain,
    ReceiptValidationError,
    ReleaseLayout,
    ReleaseTransitionError,
    TransitionEffectUncertainError,
)


TEST_CAPABILITIES = ("platform",)
TEST_PLATFORMS = ("win32",)
# endregion [01]

# region [02] Implementación


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows NTFS contract")

_MODULE_NAME = "tools.release_windows_ntfs"
_NATIVE_MODULE_NAME = "tools.release_windows_ntfs_native"
_REPOSITORY = Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPOSITORY / "tools" / "release_windows_ntfs.py"
_NATIVE_MODULE_PATH = _REPOSITORY / "tools" / "release_windows_ntfs_native.py"
_LABORATORY = _REPOSITORY.parent / "Laboratory"
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
_DELETE = 0x00010000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_WRITE_THROUGH = 0x80000000
_CHUNK_BYTES = 64 * 1024


class _FatalCheckpoint(BaseException):
    pass


class _InjectedWriteError(OSError):
    pass


@dataclass(frozen=True, slots=True)
class _NtfsCase:
    root: Path
    layout: ReleaseLayout
    launcher: Path
    desired: Path
    old_bytes: bytes
    new_bytes: bytes


class _ApiProxy:
    def __init__(self, base: Any) -> None:
        self.base = base
        self.overrides: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        override = self.overrides.get(name)
        return getattr(self.base, name) if override is None else override


class _NativeReplaceStub:
    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[tuple[Path, Path, Path, int]] = []
        self.fail = fail

    def replace_file(
        self,
        replaced: Path,
        replacement: Path,
        backup: Path,
        *,
        flags: int,
    ) -> None:
        assert flags == REPLACEFILE_WRITE_THROUGH
        assert replaced.parent == replacement.parent == backup.parent
        assert not backup.exists()
        self.calls.append((replaced, replacement, backup, flags))
        if self.fail == "before":
            raise OSError("synthetic pre-effect native failure")
        os.replace(replaced, backup)
        if self.fail == "partial":
            raise OSError("synthetic partial native failure")
        os.replace(replacement, replaced)
        if self.fail == "after":
            raise OSError("synthetic post-effect native failure")


def _load_module() -> ModuleType:
    spec = importlib.util.find_spec(_MODULE_NAME)
    assert spec is not None, f"missing production module: {_MODULE_NAME}"
    return importlib.import_module(_MODULE_NAME)


def _source() -> str:
    assert _MODULE_PATH.is_file(), f"missing production module: {_MODULE_PATH}"
    return _MODULE_PATH.read_text(encoding="utf-8")


def _assert_under(path: Path, parent: Path) -> None:
    resolved = path.resolve(strict=False)
    assert resolved == parent or resolved.is_relative_to(parent)
    assert resolved.drive.casefold() == parent.drive.casefold()


@pytest.fixture
def ntfs_module() -> ModuleType:
    return _load_module()


@pytest.fixture
def ntfs_case(tmp_path: Path) -> Iterator[_NtfsCase]:
    raw_root = os.environ.get(AUDIT_LAB_ENVIRONMENT)
    assert raw_root is not None, "NTFS tests require an activated audit laboratory"
    audit_root = Path(raw_root).resolve(strict=True)
    assert audit_root.parent.samefile(_LABORATORY)
    identity = capture_audit_lab_directory_identity(
        audit_root,
        label="W2 NTFS audit laboratory",
    )
    root = tmp_path.resolve(strict=True)
    _assert_under(root, audit_root)
    launcher = root / "install" / "bin" / "Neocortex.exe"
    desired = launcher.with_name("Neocortex-0.7.2.exe")
    receipts = root / "control" / "receipts"
    backups = root / "control" / "backups"
    lock = root / "control" / "locks" / "launcher.lock"
    launcher.parent.mkdir(parents=True)
    receipts.mkdir(parents=True)
    backups.mkdir(parents=True)
    lock.parent.mkdir(parents=True)
    old_bytes = b"synthetic-launcher-0.7.1\n"
    new_bytes = b"synthetic-launcher-0.7.2\n"
    launcher.write_bytes(old_bytes)
    desired.write_bytes(new_bytes)
    case = _NtfsCase(
        root,
        ReleaseLayout(launcher, receipts, backups, lock),
        launcher,
        desired,
        old_bytes,
        new_bytes,
    )
    yield case
    require_unchanged_audit_lab_directory(
        identity,
        label="W2 NTFS audit laboratory",
    )


def _ops(
    module: ModuleType,
    case: _NtfsCase,
    *,
    proxy: _ApiProxy | None = None,
) -> Any:
    return module.WindowsNtfsReleaseFileOperations(
        case.layout,
        api=None if proxy is None else proxy,
    )


def _base_and_proxy(module: ModuleType) -> tuple[Any, _ApiProxy]:
    base = module.WindowsNtfsApi()
    return base, _ApiProxy(base)


def _receipt_path(case: _NtfsCase, role: str = "intent") -> Path:
    return case.layout.receipts_directory / f"{'a' * 64}.{role}.json"


def _layout_parent_paths(case: _NtfsCase) -> dict[str, Path]:
    return {
        "launcher": case.launcher.parent,
        "receipts": case.layout.receipts_directory,
        "backup": case.layout.backup_directory,
        "lock": case.layout.lock_path.parent,
    }


def _probe_parent_rename(parent: Path, moved: Path) -> bool:
    try:
        os.replace(parent, moved)
    except OSError as blocked:
        assert getattr(blocked, "winerror", None) in {5, 32}
        return False
    os.replace(moved, parent)
    return True


def _signature_shape(
    callable_object: Any,
) -> tuple[tuple[str, inspect._ParameterKind], ...]:
    return tuple(
        (parameter.name, parameter.kind)
        for parameter in inspect.signature(callable_object).parameters.values()
    )


def _complexity_blocks(source: str) -> tuple[Any, ...]:
    top_level = cc_visit(source)
    methods = tuple(
        method for block in top_level for method in getattr(block, "methods", ())
    )
    return tuple(top_level) + methods


def test_ntfs_module_exists() -> None:
    assert importlib.util.find_spec(_MODULE_NAME) is not None
    assert _MODULE_PATH.is_file()
    assert _NATIVE_MODULE_PATH.is_file()


def test_ntfs_public_api_and_protocol_signatures_are_frozen(
    ntfs_module: ModuleType,
) -> None:
    assert ntfs_module.__all__ == [
        "WindowsNtfsApi",
        "WindowsNtfsReleaseFileOperations",
    ]
    implementation = ntfs_module.WindowsNtfsReleaseFileOperations
    parameters = inspect.signature(implementation.__init__).parameters
    assert tuple(parameters) == ("self", "layout", "api")
    assert parameters["api"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["api"].default is None
    for name in (
        "open_external_lock",
        "snapshot_by_handle",
        "copy_create_new_and_flush",
        "write_create_new_and_flush",
        "read_bytes",
        "path_exists",
        "remove_file_if_snapshot",
    ):
        assert _signature_shape(getattr(implementation, name)) == _signature_shape(
            getattr(ReleaseFileOperations, name)
        )
    api = ntfs_module.WindowsNtfsApi
    assert tuple(inspect.signature(api).parameters) == ()
    for name in (
        "open_file",
        "close_handle",
        "read_file",
        "write_file",
        "flush_file_buffers",
        "inspect_handle",
        "set_security_descriptor",
        "set_delete_disposition",
        "lock_file",
        "unlock_file",
        "path_exists",
    ):
        assert callable(getattr(api, name))


def test_ntfs_module_has_one_way_dag_bounded_size_and_no_path_delete() -> None:
    sources = (
        _source(),
        _NATIVE_MODULE_PATH.read_text(encoding="utf-8"),
    )
    forbidden = (
        ".unlink(",
        "os.unlink(",
        "os.remove(",
        "DeleteFileW",
        ".read_bytes(",
        ".write_bytes(",
    )
    for source in sources:
        tree = ast.parse(source)
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imports.add(node.module)
        assert "tools.release_windows" not in imports
        assert "tools.release_windows_evidence" not in imports
        assert len(source.splitlines()) <= 900
        blocks = _complexity_blocks(source)
        assert blocks and max(block.complexity for block in blocks) <= 15
        assert all(token not in source for token in forbidden)
    for facade in (
        _REPOSITORY / "tools" / "release_windows.py",
        _REPOSITORY / "tools" / "release_windows_evidence.py",
    ):
        assert _MODULE_NAME not in facade.read_text(encoding="utf-8")
    assert "SetFileInformationByHandle" in sources[1]


def test_security_descriptor_canonicalization_is_exact_and_idempotent() -> None:
    native = importlib.import_module(_NATIVE_MODULE_NAME)
    descriptor = bytes((1, 0, 0x04, 0x84, *range(16), 0xAA, 0x55))

    canonical = native._canonical_security_descriptor(descriptor)

    assert canonical[:2] == descriptor[:2]
    assert canonical[2:4] == bytes((0x04, 0x80))
    assert canonical[4:] == descriptor[4:]
    assert native._canonical_security_descriptor(canonical) == canonical


def test_security_descriptor_canonicalization_rejects_short_header() -> None:
    native = importlib.import_module(_NATIVE_MODULE_NAME)

    with pytest.raises(ReleaseTransitionError, match="relative header"):
        native._canonical_security_descriptor(bytes(19))


def test_snapshot_by_handle_captures_exact_identity_content_acl_and_topology(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    payload = bytes(range(251)) * 700
    ntfs_case.desired.write_bytes(payload)
    ops = _ops(ntfs_module, ntfs_case)
    first = ops.snapshot_by_handle(ntfs_case.desired)
    second = ops.snapshot_by_handle(ntfs_case.desired)
    assert first == second
    assert first.path == str(ntfs_case.desired.resolve())
    assert first.size == len(payload)
    assert first.sha256 == hashlib.sha256(payload).hexdigest()
    assert first.volume_id > 0 and first.file_id > 0
    assert first.file_system.casefold() == "ntfs"
    assert first.security_descriptor
    assert (
        first.security_descriptor_sha256
        == hashlib.sha256(first.security_descriptor).hexdigest()
    )
    assert first.link_count == 1
    assert first.is_reparse_point is False


def test_snapshot_reports_hardlink_identity_and_link_count(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    ops = _ops(ntfs_module, ntfs_case)
    alias = ntfs_case.desired.with_name("desired-hardlink.exe")
    os.link(ntfs_case.desired, alias)
    desired = ops.snapshot_by_handle(ntfs_case.desired)
    linked = ops.snapshot_by_handle(alias)
    assert (desired.volume_id, desired.file_id) == (linked.volume_id, linked.file_id)
    assert desired.link_count == linked.link_count == 2
    assert desired.sha256 == linked.sha256
    assert desired.security_descriptor == linked.security_descriptor


@pytest.mark.parametrize("field", ["size", "file_id", "security_descriptor"])
def test_snapshot_rejects_handle_facts_drift_during_hashing(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
    field: str,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    ops = _ops(ntfs_module, ntfs_case, proxy=proxy)
    original = base.inspect_handle
    calls = 0

    def drift(handle: int, *, security_information: int) -> Any:
        nonlocal calls
        calls += 1
        facts = original(handle, security_information=security_information)
        if calls < 2:
            return facts
        if field == "security_descriptor":
            return replace(facts, security_descriptor=facts.security_descriptor + b"x")
        return replace(facts, **{field: getattr(facts, field) + 1})

    proxy.overrides["inspect_handle"] = drift
    with pytest.raises(ReleaseTransitionError, match=r"changed|drift"):
        ops.snapshot_by_handle(ntfs_case.desired)


@pytest.mark.parametrize(
    ("field", "value"),
    [("file_system", "FAT32"), ("is_reparse_point", True)],
)
def test_layout_rejects_non_ntfs_or_reparse_before_mutation(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
    field: str,
    value: object,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    original = base.inspect_handle

    def incompatible(handle: int, *, security_information: int) -> Any:
        facts = original(handle, security_information=security_information)
        return replace(facts, **{field: value})

    proxy.overrides["inspect_handle"] = incompatible
    with pytest.raises(ReleaseTransitionError, match=r"NTFS|reparse"):
        _ops(ntfs_module, ntfs_case, proxy=proxy)


def test_copy_create_new_flushes_preserves_acl_and_returns_creator_snapshot(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    opens: list[tuple[Path, Any, int]] = []
    flushes: list[int] = []
    original_open = base.open_file
    original_flush = base.flush_file_buffers

    def record_open(path: Path, spec: Any) -> int:
        handle = int(original_open(path, spec))
        opens.append((path, spec, handle))
        return handle

    def record_flush(handle: int) -> None:
        flushes.append(handle)
        original_flush(handle)

    proxy.overrides.update(
        {"open_file": record_open, "flush_file_buffers": record_flush}
    )
    ops = _ops(ntfs_module, ntfs_case, proxy=proxy)
    destination = ntfs_case.layout.backup_directory / f"{'b' * 64}.launcher"
    source = ops.snapshot_by_handle(ntfs_case.desired)
    created = ops.copy_create_new_and_flush(ntfs_case.desired, destination)
    observed = ops.snapshot_by_handle(destination)
    assert created == observed
    assert created.file_id != source.file_id
    assert (created.size, created.sha256) == (source.size, source.sha256)
    assert created.security_descriptor == source.security_descriptor
    creator_calls = [entry for entry in opens if entry[0] == destination]
    assert len(creator_calls) == 2
    creator_spec = creator_calls[0][1]
    assert creator_spec.creation_disposition == _CREATE_NEW
    assert creator_spec.share_mode == 0
    assert creator_spec.flags_and_attributes & _FILE_FLAG_WRITE_THROUGH
    assert creator_spec.flags_and_attributes & _FILE_FLAG_OPEN_REPARSE_POINT
    assert creator_calls[0][2] in flushes


def test_copy_create_new_collision_preserves_foreign_identity(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    ops = _ops(ntfs_module, ntfs_case)
    destination = ntfs_case.layout.backup_directory / f"{'c' * 64}.launcher"
    ops.copy_create_new_and_flush(ntfs_case.desired, destination)
    before = ops.snapshot_by_handle(destination)
    with pytest.raises(FileExistsError):
        ops.copy_create_new_and_flush(ntfs_case.launcher, destination)
    assert ops.snapshot_by_handle(destination) == before


def test_copy_mid_write_failure_deletes_only_owned_partial_by_creator_handle(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    ops = _ops(ntfs_module, ntfs_case, proxy=proxy)
    ntfs_case.desired.write_bytes(b"x" * (_CHUNK_BYTES * 3 + 17))
    destination = ntfs_case.layout.backup_directory / f"{'d' * 64}.launcher"
    sentinel = ntfs_case.layout.backup_directory / "foreign-sentinel.launcher"
    sentinel.write_bytes(b"foreign\n")
    original_open = base.open_file
    original_write = base.write_file
    original_delete = base.set_delete_disposition
    creator: int | None = None
    deleted: list[int] = []
    writes = 0
    primary = _InjectedWriteError("synthetic mid-copy write failure")

    def record_open(path: Path, spec: Any) -> int:
        nonlocal creator
        handle = int(original_open(path, spec))
        if path == destination and spec.creation_disposition == _CREATE_NEW:
            creator = handle
        return handle

    def fail_second_write(handle: int, payload: bytes) -> int:
        nonlocal writes
        assert len(payload) <= _CHUNK_BYTES
        writes += 1
        if writes == 2:
            raise primary
        return int(original_write(handle, payload))

    def record_delete(
        handle: int,
        *,
        information_class: int,
        flags: int,
    ) -> None:
        deleted.append(handle)
        original_delete(
            handle,
            information_class=information_class,
            flags=flags,
        )

    proxy.overrides.update(
        {
            "open_file": record_open,
            "write_file": fail_second_write,
            "set_delete_disposition": record_delete,
        }
    )
    with pytest.raises(_InjectedWriteError) as captured:
        ops.copy_create_new_and_flush(ntfs_case.desired, destination)
    assert captured.value is primary
    assert creator is not None and deleted == [creator]
    assert not destination.exists()
    assert sentinel.read_bytes() == b"foreign\n"


def test_write_create_new_flushes_exact_bytes_and_collision_is_non_destructive(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    flushes: list[int] = []
    original_flush = base.flush_file_buffers

    def record_flush(handle: int) -> None:
        flushes.append(handle)
        original_flush(handle)

    proxy.overrides["flush_file_buffers"] = record_flush
    ops = _ops(ntfs_module, ntfs_case, proxy=proxy)
    path = _receipt_path(ntfs_case)
    payload = b'{"receipt":"synthetic"}\n'
    ops.write_create_new_and_flush(path, payload)
    before = path.read_bytes()
    assert before == payload and flushes
    with pytest.raises(FileExistsError):
        ops.write_create_new_and_flush(path, b"foreign\n")
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("payload", "maximum", "allowed"),
    [(b"", 0, True), (b"x", 1, True), (b"xy", 1, False)],
)
def test_read_bytes_enforces_exact_bound_without_oversize_payload_read(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
    payload: bytes,
    maximum: int,
    allowed: bool,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    reads = 0
    original_read = base.read_file

    def record_read(handle: int, max_bytes: int) -> bytes:
        nonlocal reads
        reads += 1
        return bytes(original_read(handle, max_bytes))

    proxy.overrides["read_file"] = record_read
    ops = _ops(ntfs_module, ntfs_case, proxy=proxy)
    path = _receipt_path(ntfs_case, "result")
    ops.write_create_new_and_flush(path, payload)
    reads = 0
    if allowed:
        assert ops.read_bytes(path, max_bytes=maximum) == payload
        assert reads >= 1 or not payload
    else:
        with pytest.raises(ReceiptValidationError, match="size limit"):
            ops.read_bytes(path, max_bytes=maximum)
        assert reads == 0


def test_read_bytes_retains_one_handle_and_blocks_path_swap(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    ops = _ops(ntfs_module, ntfs_case, proxy=proxy)
    path = _receipt_path(ntfs_case, "result")
    payload = b"r" * (_CHUNK_BYTES + 11)
    ops.write_create_new_and_flush(path, payload)
    replacement = path.with_name("foreign-result.json")
    replacement.write_bytes(b"foreign\n")
    original_read = base.read_file
    attempted = False

    def attempt_swap(handle: int, max_bytes: int) -> bytes:
        nonlocal attempted
        if not attempted:
            attempted = True
            with pytest.raises(OSError) as blocked:
                os.replace(replacement, path)
            assert getattr(blocked.value, "winerror", None) in {5, 32}
        return bytes(original_read(handle, max_bytes))

    proxy.overrides["read_file"] = attempt_swap
    assert ops.read_bytes(path, max_bytes=len(payload)) == payload
    assert attempted and replacement.read_bytes() == b"foreign\n"


def test_remove_if_snapshot_absent_is_idempotent_and_exact_uses_same_handle(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    ops = _ops(ntfs_module, ntfs_case, proxy=proxy)
    path = ntfs_case.launcher.with_name(f".neocortex-{'1' * 64}.stage")
    path.write_bytes(b"owned\n")
    expected = ops.snapshot_by_handle(path)
    original_open = base.open_file
    original_inspect = base.inspect_handle
    original_delete = base.set_delete_disposition
    deletion_handle: int | None = None
    inspected: list[int] = []
    deleted: list[int] = []

    def record_open(candidate: Path, spec: Any) -> int:
        nonlocal deletion_handle
        handle = int(original_open(candidate, spec))
        if candidate == path and spec.desired_access & _DELETE:
            deletion_handle = handle
        return handle

    def record_inspect(handle: int, *, security_information: int) -> Any:
        inspected.append(handle)
        return original_inspect(handle, security_information=security_information)

    def record_delete(
        handle: int,
        *,
        information_class: int,
        flags: int,
    ) -> None:
        deleted.append(handle)
        original_delete(
            handle,
            information_class=information_class,
            flags=flags,
        )

    proxy.overrides.update(
        {
            "open_file": record_open,
            "inspect_handle": record_inspect,
            "set_delete_disposition": record_delete,
        }
    )
    monkeypatch.setattr(Path, "unlink", lambda *_args, **_kwargs: pytest.fail("unlink"))
    monkeypatch.setattr(os, "unlink", lambda *_args, **_kwargs: pytest.fail("unlink"))
    monkeypatch.setattr(os, "remove", lambda *_args, **_kwargs: pytest.fail("remove"))
    ops.remove_file_if_snapshot(path, expected)
    assert not path.exists()
    assert deletion_handle is not None
    assert deletion_handle in inspected and deleted == [deletion_handle]
    ops.remove_file_if_snapshot(path, expected)


def test_remove_if_snapshot_preserves_same_evidence_foreign_identity(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    ops = _ops(ntfs_module, ntfs_case)
    path = ntfs_case.launcher.with_name(f".neocortex-{'2' * 64}.stage")
    path.write_bytes(b"same-evidence\n")
    original = ops.snapshot_by_handle(path)
    moved = path.with_name("original-moved.stage")
    os.replace(path, moved)
    foreign = ops.copy_create_new_and_flush(moved, path)
    assert foreign.file_id != original.file_id
    assert foreign.sha256 == original.sha256
    assert foreign.security_descriptor == original.security_descriptor
    with pytest.raises(ReleaseTransitionError, match="identity mismatch"):
        ops.remove_file_if_snapshot(path, original)
    assert ops.snapshot_by_handle(path) == foreign
    assert ops.snapshot_by_handle(moved).file_id == original.file_id


def test_remove_if_snapshot_delete_boundary_keeps_creator_handle_exclusive(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    ops = _ops(ntfs_module, ntfs_case, proxy=proxy)
    path = ntfs_case.launcher.with_name(f".neocortex-{'3' * 64}.stage")
    path.write_bytes(b"owned\n")
    expected = ops.snapshot_by_handle(path)
    foreign = path.with_name("foreign-cleanup.stage")
    foreign.write_bytes(b"foreign\n")
    original_delete = base.set_delete_disposition
    attempted = False

    def attempt_swap(
        handle: int,
        *,
        information_class: int,
        flags: int,
    ) -> None:
        nonlocal attempted
        attempted = True
        with pytest.raises(OSError) as blocked:
            os.replace(foreign, path)
        assert getattr(blocked.value, "winerror", None) in {5, 32}
        original_delete(
            handle,
            information_class=information_class,
            flags=flags,
        )

    proxy.overrides["set_delete_disposition"] = attempt_swap
    ops.remove_file_if_snapshot(path, expected)
    assert attempted and not path.exists()
    assert foreign.read_bytes() == b"foreign\n"


def test_layout_binds_one_canonical_lock_and_rejects_alias_authorization(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    ops = _ops(ntfs_module, ntfs_case)
    lock = ops.open_external_lock(ntfs_case.layout.lock_path)
    lock.acquire()
    lock.release()
    alias = ntfs_case.layout.lock_path.with_name("alternate.lock")
    os.link(ntfs_case.layout.lock_path, alias)
    with pytest.raises(ReleaseTransitionError, match="canonical lock"):
        ops.open_external_lock(alias)
    unrelated = alias.with_name("unrelated.lock")
    with pytest.raises(ReleaseTransitionError, match="canonical lock"):
        ops.open_external_lock(unrelated)


def test_external_lock_serializes_cross_process_and_releases_cleanly(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    ops = _ops(ntfs_module, ntfs_case)
    parent_lock = ops.open_external_lock(ntfs_case.layout.lock_path)
    attempting = ntfs_case.root / "child-attempting.marker"
    acquired = ntfs_case.root / "child-acquired.marker"
    code = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from tools.release_windows_receipts import ReleaseLayout
        from tools.release_windows_ntfs import WindowsNtfsReleaseFileOperations

        launcher, receipts, backups, lock_path, attempting, acquired = map(Path, sys.argv[1:])
        layout = ReleaseLayout(launcher, receipts, backups, lock_path)
        lock = WindowsNtfsReleaseFileOperations(layout).open_external_lock(lock_path)
        attempting.write_bytes(b"attempting\\n")
        lock.acquire()
        try:
            acquired.write_bytes(b"acquired\\n")
        finally:
            lock.release()
        """
    )
    parent_lock.acquire()
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            str(ntfs_case.launcher),
            str(ntfs_case.layout.receipts_directory),
            str(ntfs_case.layout.backup_directory),
            str(ntfs_case.layout.lock_path),
            str(attempting),
            str(acquired),
        ],
        cwd=_REPOSITORY,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not attempting.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                pytest.fail("child did not reach lock acquisition")
            time.sleep(0.01)
        assert attempting.exists() and not acquired.exists()
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.25)
        parent_lock.release()
        stdout, stderr = process.communicate(timeout=5.0)
        assert process.returncode == 0, (stdout, stderr)
        assert acquired.read_bytes() == b"acquired\n"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5.0)
        try:
            parent_lock.release()
        except BaseException:
            pass


def test_snapshot_and_bounded_read_each_open_exactly_one_observation_handle(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    original_open = base.open_file
    opens: list[tuple[Path, Any]] = []

    def record_open(path: Path, spec: Any) -> int:
        opens.append((path, spec))
        return int(original_open(path, spec))

    proxy.overrides["open_file"] = record_open
    ops = _ops(ntfs_module, ntfs_case, proxy=proxy)

    def reject_path_probe(path: Path) -> bool:
        pytest.fail(f"observation reopened or preflighted by path: {path}")

    proxy.overrides["path_exists"] = reject_path_probe
    opens.clear()
    snapshot = ops.snapshot_by_handle(ntfs_case.desired)
    assert snapshot.sha256 == hashlib.sha256(ntfs_case.new_bytes).hexdigest()
    assert len(opens) == 1
    assert opens[0][0].resolve() == ntfs_case.desired.resolve()
    assert opens[0][1].creation_disposition == _OPEN_EXISTING

    receipt = _receipt_path(ntfs_case, "result")
    payload = b"bounded-observation\n"
    receipt.write_bytes(payload)
    opens.clear()
    assert ops.read_bytes(receipt, max_bytes=len(payload)) == payload
    assert len(opens) == 1
    assert opens[0][0].resolve() == receipt.resolve()
    assert opens[0][1].creation_disposition == _OPEN_EXISTING


@pytest.mark.parametrize(
    ("role", "fault"),
    [
        ("receipts", "reparse"),
        ("backups", "alias"),
        ("lock_parent", "volume"),
        ("launcher", "volume"),
        ("receipts_and_backups", "identity"),
    ],
)
def test_layout_rejects_physical_component_alias_volume_identity_or_reparse(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
    role: str,
    fault: str,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    original = base.inspect_handle
    targets = {
        "launcher": ntfs_case.launcher.resolve(),
        "receipts": ntfs_case.layout.receipts_directory.resolve(),
        "backups": ntfs_case.layout.backup_directory.resolve(),
        "lock_parent": ntfs_case.layout.lock_path.parent.resolve(),
    }

    def incompatible(handle: int, *, security_information: int) -> Any:
        facts = original(handle, security_information=security_information)
        physical = Path(facts.path).resolve(strict=False)
        if fault == "identity" and physical in {
            targets["receipts"],
            targets["backups"],
        }:
            return replace(facts, file_id=1)
        if role not in targets or physical != targets[role]:
            return facts
        if fault == "reparse":
            return replace(facts, is_reparse_point=True)
        if fault == "alias":
            return replace(facts, path=str(targets["receipts"]))
        if fault == "volume":
            return replace(facts, volume_id=facts.volume_id + 1)
        raise AssertionError(f"unhandled physical-layout fault: {fault}")

    proxy.overrides["inspect_handle"] = incompatible
    with pytest.raises(
        ReleaseTransitionError,
        match=r"(?i)layout|canonical|volume|identity|reparse|same file|alias",
    ):
        _ops(ntfs_module, ntfs_case, proxy=proxy)


def test_external_lock_acquire_baseexception_closes_handle_and_allows_retry(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    original_open = base.open_file
    original_close = base.close_handle
    original_lock = base.lock_file
    opened: list[int] = []
    closed: list[int] = []

    def record_open(path: Path, spec: Any) -> int:
        handle = int(original_open(path, spec))
        opened.append(handle)
        return handle

    def record_close(handle: int) -> None:
        closed.append(handle)
        original_close(handle)

    primary = _FatalCheckpoint("synthetic post-lock BaseException")

    def lock_then_fail(handle: int) -> None:
        original_lock(handle)
        raise primary

    proxy.overrides.update(
        {
            "open_file": record_open,
            "close_handle": record_close,
            "lock_file": lock_then_fail,
        }
    )
    ops = _ops(ntfs_module, ntfs_case, proxy=proxy)
    opened.clear()
    closed.clear()
    lock = ops.open_external_lock(ntfs_case.layout.lock_path)
    with pytest.raises(_FatalCheckpoint) as captured:
        lock.acquire()
    assert captured.value is primary
    assert len(opened) == 5
    assert closed == [opened[-1], *reversed(opened[:-1])]

    proxy.overrides.pop("lock_file")
    retry = ops.open_external_lock(ntfs_case.layout.lock_path)
    retry.acquire()
    retry.release()


def test_external_lock_release_baseexception_closes_once_and_is_idempotent(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    original_open = base.open_file
    original_close = base.close_handle
    original_unlock = base.unlock_file
    opened: list[int] = []
    closed: list[int] = []
    unlocks: list[int] = []

    def record_open(path: Path, spec: Any) -> int:
        handle = int(original_open(path, spec))
        opened.append(handle)
        return handle

    def record_close(handle: int) -> None:
        closed.append(handle)
        original_close(handle)

    proxy.overrides.update({"open_file": record_open, "close_handle": record_close})
    ops = _ops(ntfs_module, ntfs_case, proxy=proxy)
    opened.clear()
    closed.clear()
    lock = ops.open_external_lock(ntfs_case.layout.lock_path)
    lock.acquire()
    assert len(opened) == 5
    primary = _FatalCheckpoint("synthetic post-unlock BaseException")

    def unlock_then_fail(handle: int) -> None:
        unlocks.append(handle)
        original_unlock(handle)
        raise primary

    proxy.overrides["unlock_file"] = unlock_then_fail
    with pytest.raises(_FatalCheckpoint) as captured:
        lock.release()
    assert captured.value is primary
    assert unlocks == [opened[-1]]
    assert closed == [opened[-1], *reversed(opened[:-1])]
    lock.release()
    assert unlocks == [opened[-1]]
    assert closed == [opened[-1], *reversed(opened[:-1])]

    proxy.overrides.pop("unlock_file")
    retry = ops.open_external_lock(ntfs_case.layout.lock_path)
    retry.acquire()
    retry.release()


def test_held_external_lock_blocks_replacement_and_unlink_of_lock_file(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    ops = _ops(ntfs_module, ntfs_case)
    lock = ops.open_external_lock(ntfs_case.layout.lock_path)
    lock.acquire()
    foreign = ntfs_case.layout.lock_path.with_name("foreign.lock")
    foreign.write_bytes(b"foreign\n")
    try:
        with pytest.raises(OSError) as replace_blocked:
            os.replace(foreign, ntfs_case.layout.lock_path)
        assert getattr(replace_blocked.value, "winerror", None) in {5, 32}
        with pytest.raises(OSError) as unlink_blocked:
            os.unlink(ntfs_case.layout.lock_path)
        assert getattr(unlink_blocked.value, "winerror", None) in {5, 32}
    finally:
        lock.release()
    assert ntfs_case.layout.lock_path.is_file()
    assert foreign.read_bytes() == b"foreign\n"


def test_abrupt_process_termination_releases_external_lock(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    held = ntfs_case.root / "child-held.marker"
    code = textwrap.dedent(
        """
        import sys
        import time
        from pathlib import Path
        from tools.release_windows_receipts import ReleaseLayout
        from tools.release_windows_ntfs import WindowsNtfsReleaseFileOperations

        launcher, receipts, backups, lock_path, held = map(Path, sys.argv[1:])
        layout = ReleaseLayout(launcher, receipts, backups, lock_path)
        lock = WindowsNtfsReleaseFileOperations(layout).open_external_lock(lock_path)
        lock.acquire()
        held.write_bytes(b"held\\n")
        while True:
            time.sleep(0.1)
        """
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            str(ntfs_case.launcher),
            str(ntfs_case.layout.receipts_directory),
            str(ntfs_case.layout.backup_directory),
            str(ntfs_case.layout.lock_path),
            str(held),
        ],
        cwd=_REPOSITORY,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not held.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                pytest.fail("child did not acquire external lock")
            time.sleep(0.01)
        if not held.exists():
            stdout, stderr = process.communicate(timeout=1.0)
            pytest.fail(f"child exited before acquiring lock: {(stdout, stderr)}")
        process.kill()
        process.communicate(timeout=5.0)
        assert process.returncode != 0
        retry = _ops(ntfs_module, ntfs_case).open_external_lock(
            ntfs_case.layout.lock_path
        )
        retry.acquire()
        retry.release()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5.0)


@pytest.mark.parametrize(
    ("fail", "expected_status", "expected_launcher"),
    [
        ("before", "no_effect", b"synthetic-launcher-0.7.1\n"),
        ("after", "success", b"synthetic-launcher-0.7.2\n"),
        ("partial", "uncertain", None),
    ],
)
def test_real_ops_recovery_classifies_native_failure_without_replacement_replay(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
    fail: str,
    expected_status: str,
    expected_launcher: bytes | None,
) -> None:
    ops = _ops(ntfs_module, ntfs_case)
    native = _NativeReplaceStub(fail)
    request = LauncherTransitionRequest(
        layout=ntfs_case.layout,
        desired_launcher=ntfs_case.desired,
        expected_current=ops.snapshot_by_handle(ntfs_case.launcher),
        expected_desired=ops.snapshot_by_handle(ntfs_case.desired),
        operation="promote",
        receipt_chain=ReceiptChain(),
    )
    with pytest.raises(TransitionEffectUncertainError):
        transition_launcher(request, ops=ops, native=native)
    assert len(native.calls) == 1
    _replaced, stage, native_backup, _flags = native.calls[0]
    intent_path = next(ntfs_case.layout.receipts_directory.glob("*.intent.json"))
    recovered = recover_pending_transition(ntfs_case.layout, intent_path, ops=ops)
    assert recovered.status == expected_status
    assert recovered.performed is False
    assert len(native.calls) == 1
    if expected_launcher is None:
        assert not ntfs_case.launcher.exists()
        assert stage.is_file() and native_backup.is_file()
    else:
        assert ntfs_case.launcher.read_bytes() == expected_launcher
    if expected_status == "no_effect":
        assert not stage.exists() and not native_backup.exists()
    elif expected_status == "success":
        assert not stage.exists() and not native_backup.exists()


def test_parent_guard_spec_is_directory_handle_without_delete_share() -> None:
    native = importlib.import_module(_NATIVE_MODULE_NAME)
    spec = native._PARENT_GUARD_SPEC
    assert spec.creation_disposition == _OPEN_EXISTING
    assert not spec.share_mode & _FILE_SHARE_DELETE
    assert spec.flags_and_attributes & _FILE_FLAG_OPEN_REPARSE_POINT
    assert spec.flags_and_attributes & _FILE_FLAG_BACKUP_SEMANTICS


@pytest.mark.parametrize("parent_role", ["launcher", "receipts", "backup", "lock"])
def test_external_lock_retains_each_layout_parent_against_rename(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
    parent_role: str,
) -> None:
    ops = _ops(ntfs_module, ntfs_case)
    lock = ops.open_external_lock(ntfs_case.layout.lock_path)
    parent = _layout_parent_paths(ntfs_case)[parent_role]
    moved = parent.with_name(f"{parent.name}-held-guard-probe")
    swapped = False
    lock.acquire()
    try:
        try:
            os.replace(parent, moved)
        except OSError as blocked:
            assert getattr(blocked, "winerror", None) in {5, 32}
        else:
            swapped = True
    finally:
        lock.release()
        if swapped:
            os.replace(moved, parent)
    assert not swapped, f"external lock did not retain {parent_role} parent"


def test_lock_spec_boundary_cannot_split_lock_after_parent_swap(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    ops = _ops(ntfs_module, ntfs_case, proxy=proxy)
    original_open = base.open_file
    lock_parent = ntfs_case.layout.lock_path.parent
    moved_parent = lock_parent.with_name("locks-bound-original")
    attempted = False
    swapped = False

    def swap_at_lock_open(path: Path, spec: Any) -> int:
        nonlocal attempted, swapped
        if (
            path == ntfs_case.layout.lock_path
            and spec.creation_disposition == _OPEN_ALWAYS
            and not attempted
        ):
            attempted = True
            try:
                os.replace(lock_parent, moved_parent)
            except OSError as blocked:
                assert getattr(blocked, "winerror", None) in {5, 32}
            else:
                swapped = True
                lock_parent.mkdir()
        return int(original_open(path, spec))

    proxy.overrides["open_file"] = swap_at_lock_open
    first = ops.open_external_lock(ntfs_case.layout.lock_path)
    first_acquired = False
    second: Any = None
    second_acquired = False
    split = False
    try:
        first.acquire()
        first_acquired = True
        if swapped:
            old_layout = ReleaseLayout(
                ntfs_case.launcher,
                ntfs_case.layout.receipts_directory,
                ntfs_case.layout.backup_directory,
                moved_parent / ntfs_case.layout.lock_path.name,
            )
            second_ops = ntfs_module.WindowsNtfsReleaseFileOperations(old_layout)
            second = second_ops.open_external_lock(old_layout.lock_path)
            second.acquire()
            second_acquired = True
            split = True
    finally:
        if second_acquired:
            second.release()
        if first_acquired:
            first.release()
        if swapped:
            for lock_path in (
                ntfs_case.layout.lock_path,
                moved_parent / ntfs_case.layout.lock_path.name,
            ):
                if lock_path.exists():
                    lock_path.unlink()
            lock_parent.rmdir()
            os.replace(moved_parent, lock_parent)
    assert attempted
    assert not swapped and not split, "one logical lock admitted two physical locks"


@pytest.mark.parametrize("mutation", ["receipt", "backup", "stage"])
def test_parent_swap_is_blocked_at_create_new_boundary(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
    mutation: str,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    ops = _ops(ntfs_module, ntfs_case, proxy=proxy)
    original_open = base.open_file
    if mutation == "receipt":
        destination = _receipt_path(ntfs_case, "intent")
    elif mutation == "backup":
        destination = ntfs_case.layout.backup_directory / f"{'e' * 64}.launcher"
    else:
        destination = ntfs_case.launcher.with_name(f".neocortex-{'f' * 64}.stage")
    parent = destination.parent
    moved_parent = parent.with_name(f"{parent.name}-bound-{mutation}")
    attempted = False
    swapped = False

    def swap_before_create(path: Path, spec: Any) -> int:
        nonlocal attempted, swapped
        if (
            path == destination
            and spec.creation_disposition == _CREATE_NEW
            and not attempted
        ):
            attempted = True
            try:
                os.replace(parent, moved_parent)
            except OSError as blocked:
                assert getattr(blocked, "winerror", None) in {5, 32}
            else:
                swapped = True
                parent.mkdir()
        return int(original_open(path, spec))

    proxy.overrides["open_file"] = swap_before_create
    try:
        if mutation == "receipt":
            ops.write_create_new_and_flush(destination, b"guarded-receipt\n")
        else:
            ops.copy_create_new_and_flush(ntfs_case.desired, destination)
        assert attempted
        assert not swapped, f"{mutation} CREATE_NEW escaped its bound parent"
    finally:
        if swapped:
            if destination.exists():
                destination.unlink()
            parent.rmdir()
            os.replace(moved_parent, parent)


def test_remove_parent_swap_cannot_be_misreported_as_absence(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    ops = _ops(ntfs_module, ntfs_case, proxy=proxy)
    target = ntfs_case.launcher.with_name(f".neocortex-{'4' * 64}.stage")
    target.write_bytes(b"owned-stage\n")
    expected = ops.snapshot_by_handle(target)
    original_open = base.open_file
    parent = target.parent
    moved_parent = parent.with_name("bin-bound-remove-absence")
    attempted = False
    swapped = False
    survived = False

    def swap_before_remove_open(path: Path, spec: Any) -> int:
        nonlocal attempted, swapped
        if path == target and spec.desired_access & _DELETE and not attempted:
            attempted = True
            try:
                os.replace(parent, moved_parent)
            except OSError as blocked:
                assert getattr(blocked, "winerror", None) in {5, 32}
            else:
                swapped = True
                parent.mkdir()
        return int(original_open(path, spec))

    proxy.overrides["open_file"] = swap_before_remove_open
    try:
        ops.remove_file_if_snapshot(target, expected)
        survived = swapped and (moved_parent / target.name).is_file()
    finally:
        if swapped:
            parent.rmdir()
            os.replace(moved_parent, parent)
        if target.exists():
            target.unlink()
    assert attempted
    assert not swapped and not survived, (
        "remove returned absent after parent substitution"
    )


@pytest.mark.parametrize("boundary", ["delete", "close"])
def test_remove_retains_parent_guard_through_delete_and_creator_close(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
    boundary: str,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    ops = _ops(ntfs_module, ntfs_case, proxy=proxy)
    target = ntfs_case.launcher.with_name(f".neocortex-{'5' * 64}.stage")
    target.write_bytes(b"owned-stage\n")
    expected = ops.snapshot_by_handle(target)
    original_open = base.open_file
    original_delete = base.set_delete_disposition
    original_close = base.close_handle
    target_handle: int | None = None
    attempted = False
    unblocked = False
    moved_parent = target.parent.with_name(f"bin-remove-{boundary}-probe")

    def record_open(path: Path, spec: Any) -> int:
        nonlocal target_handle
        handle = int(original_open(path, spec))
        if path == target and spec.desired_access & _DELETE:
            target_handle = handle
        return handle

    def probe_delete(handle: int, *, information_class: int, flags: int) -> None:
        nonlocal attempted, unblocked
        if boundary == "delete" and handle == target_handle:
            attempted = True
            unblocked = _probe_parent_rename(target.parent, moved_parent)
        original_delete(handle, information_class=information_class, flags=flags)

    def probe_close(handle: int) -> None:
        nonlocal attempted, unblocked
        original_close(handle)
        if boundary == "close" and handle == target_handle:
            attempted = True
            unblocked = _probe_parent_rename(target.parent, moved_parent)

    proxy.overrides.update(
        {
            "open_file": record_open,
            "set_delete_disposition": probe_delete,
            "close_handle": probe_close,
        }
    )
    ops.remove_file_if_snapshot(target, expected)
    assert attempted and not unblocked
    assert not target.exists()


@pytest.mark.parametrize("observation", ["snapshot", "read", "path_exists"])
def test_observations_reject_a_substitute_for_the_bound_parent(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
    observation: str,
) -> None:
    ops = _ops(ntfs_module, ntfs_case)
    parent = ntfs_case.layout.receipts_directory
    trusted = _receipt_path(ntfs_case, "result")
    trusted.write_bytes(b"trusted\n")
    moved_parent = parent.with_name(f"receipts-bound-{observation}")
    os.replace(parent, moved_parent)
    parent.mkdir()
    substitute = _receipt_path(ntfs_case, "result")
    substitute.write_bytes(b"substitute\n")
    try:
        with pytest.raises(
            ReleaseTransitionError,
            match=r"(?i)layout|parent|identity|canonical|bound",
        ):
            if observation == "snapshot":
                ops.snapshot_by_handle(substitute)
            elif observation == "read":
                ops.read_bytes(substitute, max_bytes=64)
            else:
                ops.path_exists(substitute)
    finally:
        substitute.unlink()
        parent.rmdir()
        os.replace(moved_parent, parent)


def test_transition_retains_all_parent_guards_while_native_replaces_children(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    parents = tuple(_layout_parent_paths(ntfs_case).values())
    active_guards: dict[int, Path] = {}
    original_open = base.open_file
    original_close = base.close_handle

    def record_open(path: Path, spec: Any) -> int:
        handle = int(original_open(path, spec))
        if path in parents and not spec.share_mode & _FILE_SHARE_DELETE:
            active_guards[handle] = path
        return handle

    def record_close(handle: int) -> None:
        original_close(handle)
        active_guards.pop(handle, None)

    proxy.overrides.update({"open_file": record_open, "close_handle": record_close})
    ops = _ops(ntfs_module, ntfs_case, proxy=proxy)
    active_parent_sets: list[set[Path]] = []
    unblocked_parents: list[Path] = []

    class ProbeNative(_NativeReplaceStub):
        def replace_file(
            self,
            replaced: Path,
            replacement: Path,
            backup: Path,
            *,
            flags: int,
        ) -> None:
            active_parent_sets.append(set(active_guards.values()))
            for index, parent in enumerate(parents):
                moved = parent.with_name(f"{parent.name}-native-probe-{index}")
                if _probe_parent_rename(parent, moved):
                    unblocked_parents.append(parent)
            super().replace_file(
                replaced,
                replacement,
                backup,
                flags=flags,
            )

    request = LauncherTransitionRequest(
        layout=ntfs_case.layout,
        desired_launcher=ntfs_case.desired,
        expected_current=ops.snapshot_by_handle(ntfs_case.launcher),
        expected_desired=ops.snapshot_by_handle(ntfs_case.desired),
        operation="promote",
        receipt_chain=ReceiptChain(),
    )
    result = transition_launcher(request, ops=ops, native=ProbeNative())
    assert result.status == "success"
    assert active_parent_sets == [set(parents)]
    assert not unblocked_parents
    assert not active_guards


def test_guard_cleanup_is_reverse_order_and_never_masks_primary_baseexception(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    base, proxy = _base_and_proxy(ntfs_module)
    parents = set(_layout_parent_paths(ntfs_case).values())
    active_guards: dict[int, Path] = {}
    original_open = base.open_file
    original_close = base.close_handle
    retained: list[int] = []
    close_order: list[int] = []
    fault_handle: int | None = None
    armed = False
    cleanup_raised = False
    cleanup = _FatalCheckpoint("synthetic intermediate guard cleanup")

    def record_open(path: Path, spec: Any) -> int:
        handle = int(original_open(path, spec))
        if path in parents and not spec.share_mode & _FILE_SHARE_DELETE:
            active_guards[handle] = path
        return handle

    def close_with_fault(handle: int) -> None:
        nonlocal cleanup_raised
        is_guard = handle in active_guards
        original_close(handle)
        if is_guard:
            active_guards.pop(handle, None)
            if armed and handle in retained:
                close_order.append(handle)
            if armed and handle == fault_handle:
                cleanup_raised = True
                raise cleanup

    proxy.overrides.update({"open_file": record_open, "close_handle": close_with_fault})
    ops = _ops(ntfs_module, ntfs_case, proxy=proxy)
    request = LauncherTransitionRequest(
        layout=ntfs_case.layout,
        desired_launcher=ntfs_case.desired,
        expected_current=ops.snapshot_by_handle(ntfs_case.launcher),
        expected_desired=ops.snapshot_by_handle(ntfs_case.desired),
        operation="promote",
        receipt_chain=ReceiptChain(),
    )
    primary = _FatalCheckpoint("synthetic transition primary")

    def stop_after_intent(name: str) -> None:
        nonlocal fault_handle, armed
        if name == "after_intent":
            retained.extend(active_guards)
            fault_handle = retained[len(retained) // 2] if retained else None
            armed = True
            raise primary

    with pytest.raises(_FatalCheckpoint) as captured:
        transition_launcher(
            request,
            ops=ops,
            native=_NativeReplaceStub(),
            checkpoint=stop_after_intent,
        )
    assert captured.value is primary
    assert len(retained) == 4
    assert close_order == list(reversed(retained))
    assert cleanup_raised and not active_guards
    assert any(
        "external lock cleanup failed" in note
        for note in getattr(primary, "__notes__", ())
    )

    proxy.overrides.pop("open_file")
    proxy.overrides.pop("close_handle")
    retry = ops.open_external_lock(ntfs_case.layout.lock_path)
    retry.acquire()
    retry.release()


def test_w1_baseexception_releases_real_external_lock(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    ops = _ops(ntfs_module, ntfs_case)
    request = LauncherTransitionRequest(
        layout=ntfs_case.layout,
        desired_launcher=ntfs_case.desired,
        expected_current=ops.snapshot_by_handle(ntfs_case.launcher),
        expected_desired=ops.snapshot_by_handle(ntfs_case.desired),
        operation="promote",
        receipt_chain=ReceiptChain(),
    )
    primary = _FatalCheckpoint("synthetic BaseException")

    def stop_after_intent(name: str) -> None:
        if name == "after_intent":
            raise primary

    with pytest.raises(_FatalCheckpoint) as captured:
        transition_launcher(
            request,
            ops=ops,
            native=_NativeReplaceStub(),
            checkpoint=stop_after_intent,
        )
    assert captured.value is primary
    replay_lock = _ops(ntfs_module, ntfs_case).open_external_lock(
        ntfs_case.layout.lock_path
    )
    replay_lock.acquire()
    replay_lock.release()


def test_real_ops_integrate_with_w1_promote_replay_and_rollback_stub(
    ntfs_module: ModuleType,
    ntfs_case: _NtfsCase,
) -> None:
    ops = _ops(ntfs_module, ntfs_case)
    native = _NativeReplaceStub()
    before = ops.snapshot_by_handle(ntfs_case.launcher)
    desired = ops.snapshot_by_handle(ntfs_case.desired)
    assert before.security_descriptor == desired.security_descriptor
    promote_request = LauncherTransitionRequest(
        layout=ntfs_case.layout,
        desired_launcher=ntfs_case.desired,
        expected_current=before,
        expected_desired=desired,
        operation="promote",
        receipt_chain=ReceiptChain(),
    )
    promoted = transition_launcher(promote_request, ops=ops, native=native)
    assert promoted.status == "success" and promoted.performed is True
    assert ntfs_case.launcher.read_bytes() == ntfs_case.new_bytes
    assert len(native.calls) == 1
    assert promoted.external_backup_path.read_bytes() == ntfs_case.old_bytes
    assert not promoted.native_backup_path.exists()
    assert transition_launcher(promote_request, ops=ops, native=native) == promoted
    assert len(native.calls) == 1

    rollback_source = promoted.external_backup_path
    rollback_request = LauncherTransitionRequest(
        layout=ntfs_case.layout,
        desired_launcher=rollback_source,
        expected_current=ops.snapshot_by_handle(ntfs_case.launcher),
        expected_desired=ops.snapshot_by_handle(rollback_source),
        operation="rollback",
        receipt_chain=ReceiptChain().advance(promoted),
    )
    rolled_back = transition_launcher(rollback_request, ops=ops, native=native)
    assert rolled_back.status == "success" and rolled_back.performed is True
    assert ntfs_case.launcher.read_bytes() == ntfs_case.old_bytes
    assert ops.snapshot_by_handle(ntfs_case.launcher).security_descriptor == (
        before.security_descriptor
    )
    assert len(native.calls) == 2
    assert transition_launcher(rollback_request, ops=ops, native=native) == rolled_back
    assert len(native.calls) == 2


# endregion [02]
