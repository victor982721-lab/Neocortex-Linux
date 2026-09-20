"""End-to-end security contract for physical ZIP intake.

These tests deliberately exercise only temporary fixtures under ``tmp_path``.
They never use the personal corpus and never invoke an installed command with
an effect outside that fixture.

The new ZIP-intake boundary is intentionally explicit here.  The production
implementation exposes ``neocortex.capabilities.formats.archive.intake`` with
``run_zip_intake`` and a bounded limits dataclass.  Until that module exists
the tests fail with a diagnostic blocker instead of silently falling back to
the retired virtual-Archive implementation.
"""

from __future__ import annotations

import importlib
import inspect
import io
import os
import shutil
import stat
import struct
import subprocess
import time
import warnings
import zipfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Mapping

import pytest

from neocortex.runtime.control.cancellation import CancellationToken


INTAKE_MODULE = "neocortex.capabilities.formats.archive.intake"
RUNNER_NAME = "run_zip_intake"


class RecordingTrash:
    """Fixture-only KIO seam.

    A real KIO client is deliberately not started by these tests.  The intake
    contract must accept an injected trash service so that the physical
    frontier can be tested without touching the user's desktop Trash.
    """

    def __init__(self, *, root: Path | None = None, fail: BaseException | None = None,
                 mutate_source: bool = False) -> None:
        self.root = root
        self.fail = fail
        self.mutate_source = mutate_source
        self.calls: list[Path] = []

    def __call__(self, source: object, *args: object, **kwargs: object) -> object:
        return self.move(source, *args, **kwargs)

    def move(self, source: object, *args: object, **kwargs: object) -> object:
        del args, kwargs
        source_path = _as_path(source)
        self.calls.append(source_path)
        if self.mutate_source:
            source_path.write_bytes(source_path.read_bytes() + b"\x00mutation")
        if self.fail is not None:
            raise self.fail
        if self.root is not None and source_path.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            shutil.move(os.fspath(source_path), os.fspath(self.root / source_path.name))
        return SimpleNamespace(status="applied", reason="fixture_trash")

    move_to_trash = move
    trash = move


def _as_path(value: object) -> Path:
    if isinstance(value, Path):
        return value
    candidate = getattr(value, "path", value)
    return Path(os.fspath(candidate))


def _api() -> ModuleType:
    try:
        module = importlib.import_module(INTAKE_MODULE)
    except ModuleNotFoundError as exc:  # pragma: no cover - intentional blocker
        pytest.fail(
            "BLOCKER: ZIP Intake integration is not present: "
            f"cannot import {INTAKE_MODULE!r}. The retired virtual Archive "
            "route must not be used as an implicit fallback.",
            pytrace=False,
        )
        raise AssertionError from exc
    runner = getattr(module, RUNNER_NAME, None)
    if not callable(runner):  # pragma: no cover - intentional blocker
        pytest.fail(
            f"BLOCKER: {INTAKE_MODULE}.{RUNNER_NAME} is not callable; "
            "expose the physical intake boundary before enabling E2E tests.",
            pytrace=False,
        )
    return module


@pytest.fixture
def api() -> ModuleType:
    """Resolve the new physical intake boundary or report its blocker."""

    return _api()


def _limits_type(module: ModuleType) -> type[Any]:
    for name in ("ZipIntakeLimits", "ArchiveIntakeLimits", "ArchiveMaterializationLimits"):
        value = getattr(module, name, None)
        if isinstance(value, type):
            return value
    pytest.fail(
        f"BLOCKER: {INTAKE_MODULE} exposes no bounded limits dataclass; "
        "member/total/depth/ratio/time budgets must be explicit.",
        pytrace=False,
    )
    raise AssertionError


def _make_limits(module: ModuleType, **overrides: object) -> object:
    """Build bounded limits while accepting the two established depth names."""

    limit_type = _limits_type(module)
    parameters = inspect.signature(limit_type).parameters
    defaults: dict[str, object] = {
        "max_members": 128,
        "max_member_bytes": 8 * 1024 * 1024,
        "max_total_uncompressed_bytes": 32 * 1024 * 1024,
        "max_total_temp_bytes": 64 * 1024 * 1024,
        "max_input_bytes": 64 * 1024 * 1024,
        "max_depth": 4,
        "max_nested_depth": 4,
        "max_compression_ratio": 200.0,
        "max_central_directory_bytes": 8 * 1024 * 1024,
        "timeout_seconds": 10.0,
    }
    defaults.update(overrides)
    kwargs: dict[str, object] = {}
    for name, value in defaults.items():
        if name in parameters:
            kwargs[name] = value
    if not kwargs:
        pytest.fail(
            f"BLOCKER: {limit_type.__name__} has no recognized bounded limit fields",
            pytrace=False,
        )
    return limit_type(**kwargs)


def _call_intake(
    module: ModuleType,
    source: Path,
    destination: Path,
    *,
    apply: bool,
    limits: object | None = None,
    max_file_bytes: int | None = None,
    cancellation: CancellationToken | None = None,
    trash: RecordingTrash | None = None,
    staging: Path | None = None,
    publisher: object | None = None,
    deadline: float | None = None,
) -> object:
    """Call the narrow intake API and reject missing safety seams loudly."""

    runner = getattr(module, RUNNER_NAME)
    parameters = inspect.signature(runner).parameters
    kwargs: dict[str, object] = {"apply": apply}
    if "destination" in parameters:
        kwargs["destination"] = destination
    if apply and "staging" in parameters:
        kwargs["staging"] = staging or destination.parent / ".zip-intake-staging"
    if publisher is not None:
        if "publisher" not in parameters:
            pytest.fail("BLOCKER: run_zip_intake must expose a no-replace publisher seam", pytrace=False)
        kwargs["publisher"] = publisher
    if deadline is not None:
        if "deadline" not in parameters:
            pytest.fail("BLOCKER: run_zip_intake must expose a bounded deadline seam", pytrace=False)
        kwargs["deadline"] = deadline
    if limits is not None:
        if "limits" not in parameters:
            pytest.fail("BLOCKER: intake_zip must accept one bounded limits object", pytrace=False)
        kwargs["limits"] = limits
    if max_file_bytes is not None:
        for name in ("max_file_bytes", "global_max_file_bytes"):
            if name in parameters:
                kwargs[name] = max_file_bytes
                break
        else:
            pytest.fail(
                "BLOCKER: intake_zip must consume the global -S admission field",
                pytrace=False,
            )
    if cancellation is not None:
        for name in ("cancellation", "cancellation_token", "cancel"):
            if name in parameters:
                kwargs[name] = cancellation
                break
        else:
            pytest.fail("BLOCKER: intake_zip has no cancellation seam", pytrace=False)
    if trash is not None:
        for name in ("trash_backend", "trash_service", "kio_trash", "trash"):
            if name in parameters:
                kwargs[name] = trash
                break
        else:
            pytest.fail(
                "BLOCKER: intake_zip must accept an injected KIO Trash service for a "
                "no-desktop E2E test",
                pytrace=False,
            )
    try:
        return runner(source, **kwargs)
    except TypeError as exc:
        pytest.fail(f"ZIP Intake contract mismatch: {exc}", pytrace=False)
        raise AssertionError from exc


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value):
        return asdict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return result
    return {
        name: getattr(value, name)
        for name in dir(value)
        if not name.startswith("_") and not callable(getattr(value, name, None))
    }


def _value(result: object, *names: str, default: object = None) -> object:
    current: object = result
    for index, name in enumerate(names):
        mapping = _as_mapping(current)
        if name in mapping:
            current = mapping[name]
        elif hasattr(current, name):
            current = getattr(current, name)
        else:
            return default
        if index == len(names) - 1:
            return current
    return current


def _status(result: object) -> str:
    value = _value(result, "status", default="")
    return str(getattr(value, "value", value)).casefold()


def _evidence(result: object) -> str:
    mapping = _as_mapping(result)
    pieces: list[str] = []
    for key in ("status", "reason", "detail", "error", "errors", "evidence", "warnings"):
        value = mapping.get(key)
        if value is not None:
            pieces.append(str(value))
    return " ".join(pieces).casefold()


def _assert_rejected(result: object, *tokens: str) -> None:
    status = _status(result)
    assert status not in {"complete", "applied", "success", "ok"}, _as_mapping(result)
    evidence = _evidence(result)
    assert any(token.casefold() in evidence for token in tokens), (
        status,
        evidence,
        _as_mapping(result),
    )


def _zip_bytes(entries: list[tuple[str, bytes | str, int | None]], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        for name, payload, mode in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.compress_type = compression
            if mode is None:
                info.external_attr = (stat.S_IFREG | 0o600) << 16
            else:
                info.external_attr = mode << 16
            archive.writestr(info, payload)
    return stream.getvalue()


def _write_zip(path: Path, entries: list[tuple[str, bytes | str, int | None]], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    payload = _zip_bytes(entries, compression=compression)
    path.write_bytes(payload)
    return payload


def _patch_member_name(path: Path, old: bytes, new: bytes) -> None:
    if len(old) != len(new):
        raise ValueError("test name patch must preserve encoded length")
    raw = bytearray(path.read_bytes())
    hits = raw.count(old)
    if hits < 2:
        raise AssertionError(f"expected local and central name records for {old!r}")
    path.write_bytes(bytes(raw).replace(old, new))


def _write_encrypted_marker(path: Path) -> None:
    _write_zip(path, [("secret.txt", b"password protected", None)], compression=zipfile.ZIP_STORED)
    raw = bytearray(path.read_bytes())
    local = raw.find(b"PK\x03\x04")
    central = raw.find(b"PK\x01\x02")
    assert local >= 0 and central >= 0
    struct.pack_into("<H", raw, local + 6, struct.unpack_from("<H", raw, local + 6)[0] | 0x1)
    struct.pack_into("<H", raw, central + 8, struct.unpack_from("<H", raw, central + 8)[0] | 0x1)
    path.write_bytes(raw)


def _write_crc_corrupt(path: Path) -> None:
    _write_zip(path, [("corrupt.txt", b"CRC evidence", None)], compression=zipfile.ZIP_STORED)
    raw = bytearray(path.read_bytes())
    central = raw.find(b"PK\x01\x02")
    assert central > 0
    raw[central - 1] ^= 0xFF
    path.write_bytes(raw)


def _write_package(path: Path, kind: str) -> bytes:
    if kind == "ooxml":
        entries = [
            ("[Content_Types].xml", b"<Types/>", None),
            ("word/document.xml", b"<document/>", None),
        ]
    elif kind == "odf":
        entries = [
            ("mimetype", b"application/vnd.oasis.opendocument.text", None),
            (
                "content.xml",
                b'<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"/>',
                None,
            ),
            (
                "META-INF/manifest.xml",
                b'<manifest xmlns="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"/>',
                None,
            ),
        ]
    elif kind == "epub":
        entries = [
            ("mimetype", b"application/epub+zip", None),
            ("META-INF/container.xml", b"<container/>", None),
            ("OEBPS/content.xhtml", b"<html/>", None),
        ]
    elif kind == "apk":
        entries = [("AndroidManifest.xml", b"binary manifest", None), ("classes.dex", b"dex", None)]
    elif kind == "jar":
        entries = [("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n", None), ("Main.class", b"class", None)]
    else:
        raise AssertionError(kind)
    return _write_zip(path, entries)


def _assert_no_staging_left(root: Path) -> None:
    leftovers = [
        child
        for child in root.iterdir()
        if child.name.startswith((".neocortex", "neocortex-zip-intake"))
    ]
    assert not leftovers, leftovers


@pytest.mark.parametrize(
    ("member_name", "escape_name"),
    [
        ("../escape.txt", "escape.txt"),
        ("../../escape.txt", "escape.txt"),
        ("/absolute.txt", "absolute.txt"),
        ("//double-absolute.txt", "double-absolute.txt"),
        (r"C:\\drive.txt", "drive.txt"),
        (r"C:/drive.txt", "drive.txt"),
        (r"\\\\server\\share\\unc.txt", "unc.txt"),
        (".", "."),
    ],
)
def test_path_traversal_absolute_drive_and_unc_are_atomic_rejections(
    api: ModuleType, tmp_path: Path, member_name: str, escape_name: str
) -> None:
    source = tmp_path / "unsafe.zip"
    original = _write_zip(source, [(member_name, b"must not publish", None)])
    destination = tmp_path / "unsafe"

    result = _call_intake(api, source, destination, apply=True, trash=RecordingTrash())

    _assert_rejected(result, "unsafe", "traversal", "absolute", "drive", "path")
    assert source.read_bytes() == original
    assert not destination.exists()
    if escape_name != ".":
        assert not (tmp_path.parent / escape_name).exists()


def test_nul_member_name_is_rejected_before_any_publication(api: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "nul.zip"
    _write_zip(source, [("badXname.txt", b"NUL", None)])
    _patch_member_name(source, b"badXname.txt", b"bad\x00name.txt")
    original = source.read_bytes()

    result = _call_intake(api, source, tmp_path / "nul", apply=True, trash=RecordingTrash())

    _assert_rejected(result, "nul", "unsafe", "name", "corrupt")
    assert source.read_bytes() == original
    assert not (tmp_path / "nul").exists()


def test_duplicate_member_names_are_rejected_before_extracting_any_member(
    api: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "duplicate.zip"
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=r"Duplicate name: 'same.txt'")
        _write_zip(
            source,
            [("same.txt", b"first", None), ("same.txt", b"second", None)],
            compression=zipfile.ZIP_STORED,
        )
    original = source.read_bytes()

    result = _call_intake(api, source, tmp_path / "duplicate", apply=True, trash=RecordingTrash())

    _assert_rejected(result, "duplicate", "unsafe", "member")
    assert source.read_bytes() == original
    assert not (tmp_path / "duplicate").exists()


def test_file_directory_collision_is_rejected_without_merging(
    api: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "file-directory-collision.zip"
    _write_zip(
        source,
        [("a", b"file", None), ("a/child.txt", b"child", None)],
        compression=zipfile.ZIP_STORED,
    )
    original = source.read_bytes()

    result = _call_intake(api, source, tmp_path / "file-directory-collision", apply=True, trash=RecordingTrash())

    _assert_rejected(result, "collision", "unsafe", "directory", "member")
    assert source.read_bytes() == original
    assert not (tmp_path / "file-directory-collision").exists()


@pytest.mark.parametrize(
    "special_mode",
    [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600, stat.S_IFCHR | 0o600, stat.S_IFBLK | 0o600],
)
def test_symlink_fifo_and_device_like_members_are_never_published(
    api: ModuleType, tmp_path: Path, special_mode: int
) -> None:
    source = tmp_path / "special.zip"
    original = _write_zip(source, [("special", b"not a regular file", special_mode)])

    result = _call_intake(api, source, tmp_path / "special", apply=True, trash=RecordingTrash())

    _assert_rejected(result, "special", "unsafe", "permission", "device", "symlink")
    assert source.read_bytes() == original
    assert not (tmp_path / "special").exists()


def test_executable_bits_setuid_and_content_extensions_are_data_only(
    api: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "executed.marker"
    shell = f"#!/bin/sh\nprintf executed > {marker}\n".encode()
    elf = b"\x7fELF\x02\x01\x01\x00" + b"not executable here"
    exe = b"MZ" + b"not executable here"
    source = tmp_path / "executables.zip"
    _write_zip(
        source,
        [
            ("run.sh", shell, stat.S_IFREG | stat.S_IRWXU | stat.S_ISUID),
            ("run.elf", elf, stat.S_IFREG | 0o777),
            ("run.exe", exe, stat.S_IFREG | 0o777),
        ],
        compression=zipfile.ZIP_STORED,
    )
    destination = tmp_path / "executables"

    def fail_if_executed(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"Archive Intake executed extracted content: {args!r} {kwargs!r}")

    monkeypatch.setattr(subprocess, "run", fail_if_executed)
    trash = RecordingTrash(root=tmp_path / "trash")
    result = _call_intake(api, source, destination, apply=True, trash=trash)

    assert _status(result) in {"complete", "applied", "success"}, _as_mapping(result)
    for name in ("run.sh", "run.elf", "run.exe"):
        output = destination / name
        assert output.is_file()
        assert output.read_bytes() in {shell, elf, exe}
        assert stat.S_IMODE(output.stat().st_mode) & 0o111 == 0
        assert stat.S_IMODE(output.stat().st_mode) & 0o600 == 0o600
    assert not marker.exists()
    assert len(trash.calls) == 1


def test_published_regular_files_are_0600_and_directories_0700(
    api: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "modes.zip"
    _write_zip(
        source,
        [
            ("tree/", b"", stat.S_IFDIR | 0o777),
            ("tree/data.txt", b"data", stat.S_IFREG | 0o777),
        ],
        compression=zipfile.ZIP_STORED,
    )
    destination = tmp_path / "modes"
    result = _call_intake(api, source, destination, apply=True, trash=RecordingTrash(root=tmp_path / "trash"))

    assert _status(result) in {"complete", "applied", "success"}, _as_mapping(result)
    assert stat.S_IMODE((destination / "tree").stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "tree" / "data.txt").stat().st_mode) == 0o600


@pytest.mark.parametrize("package_kind", ["ooxml", "odf", "epub", "apk", "jar"])
def test_atomic_zip_based_packages_are_not_generic_storage(
    api: ModuleType, tmp_path: Path, package_kind: str
) -> None:
    source = tmp_path / f"package-{package_kind}.zip"
    original = _write_package(source, package_kind)
    destination = tmp_path / f"package-{package_kind}"
    trash = RecordingTrash(root=tmp_path / "trash")

    result = _call_intake(api, source, destination, apply=True, trash=trash)

    status = _status(result)
    assert status in {"atomic", "preserved", "complete", "validated"}, _as_mapping(result)
    classification = _value(result, "classification", default={})
    kind = str(_value(classification, "unit_kind", "kind", default=_value(result, "unit_kind", default=""))).casefold()
    assert kind not in {"storage_archive", "generic", "generic_zip"}, _as_mapping(result)
    assert source.read_bytes() == original
    assert not destination.exists()
    assert not trash.calls


def test_project_zip_is_physical_data_not_a_preserved_virtual_unit(
    api: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "project.zip"
    _write_zip(
        source,
        [
            ("pyproject.toml", b"[project]\nname='fixture'\n", None),
            ("src/app.py", b"print('data only')\n", stat.S_IFREG | 0o777),
            ("README.md", b"project", None),
        ],
    )
    destination = tmp_path / "project"
    trash = RecordingTrash(root=tmp_path / "trash")

    result = _call_intake(api, source, destination, apply=True, trash=trash)

    assert _status(result) in {"complete", "applied", "success"}, _as_mapping(result)
    assert (destination / "pyproject.toml").read_bytes().startswith(b"[project]")
    assert (destination / "src" / "app.py").is_file()
    assert not (destination / "project.zip").exists()
    assert len(trash.calls) == 1


def test_nested_generic_zip_expands_inside_staging_before_publish(
    api: ModuleType, tmp_path: Path
) -> None:
    inner = _zip_bytes([("document.txt", b"nested data", None)])
    source = tmp_path / "A.zip"
    _write_zip(source, [("folder/B.zip", inner, None), ("root.txt", b"root", None)])
    destination = tmp_path / "A"
    trash = RecordingTrash(root=tmp_path / "trash")

    result = _call_intake(api, source, destination, apply=True, trash=trash)

    assert _status(result) in {"complete", "applied", "success"}, _as_mapping(result)
    assert (destination / "folder" / "B" / "document.txt").read_bytes() == b"nested data"
    assert (destination / "root.txt").read_bytes() == b"root"
    assert not (destination / "folder" / "B.zip").exists()
    assert len(trash.calls) == 1


def test_nested_atomic_packages_remain_regular_files_inside_generic_tree(
    api: ModuleType, tmp_path: Path
) -> None:
    packages = {
        "document.docx": _zip_bytes(
            [("[Content_Types].xml", b"<Types/>", None), ("word/document.xml", b"<document/>", None)]
        ),
        "book.epub": _zip_bytes(
            [("mimetype", b"application/epub+zip", None), ("META-INF/container.xml", b"<container/>", None)]
        ),
        "app.apk": _zip_bytes([("AndroidManifest.xml", b"manifest", None)]),
        "library.jar": _zip_bytes([("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n", None)]),
    }
    source = tmp_path / "packages.zip"
    _write_zip(source, [(name, payload, None) for name, payload in packages.items()])
    destination = tmp_path / "packages"

    result = _call_intake(
        api,
        source,
        destination,
        apply=True,
        trash=RecordingTrash(root=tmp_path / "trash"),
    )

    assert _status(result) in {"complete", "applied", "success"}, _as_mapping(result)
    for name, payload in packages.items():
        output = destination / name
        assert output.is_file()
        assert output.read_bytes() == payload
        assert not output.with_suffix("").is_dir()


def test_expired_deadline_blocks_before_publication(api: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "deadline.zip"
    _write_zip(source, [("data.txt", b"deadline", None)])
    original = source.read_bytes()

    result = _call_intake(
        api,
        source,
        tmp_path / "deadline",
        apply=True,
        trash=RecordingTrash(),
        # The production contract uses a monotonic deadline, not wall clock.
        deadline=time.monotonic() - 1.0,
    )

    _assert_rejected(result, "timeout", "deadline", "budget")
    assert source.read_bytes() == original
    assert not (tmp_path / "deadline").exists()


def test_dry_run_has_no_physical_effect_or_trash_call(api: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "dry.zip"
    original = _write_zip(source, [("data.txt", b"dry run", None)])
    destination = tmp_path / "dry"
    trash = RecordingTrash(root=tmp_path / "trash")

    result = _call_intake(api, source, destination, apply=False, trash=trash)

    assert _status(result) in {"planned", "dry_run", "preview", "complete"}, _as_mapping(result)
    assert source.read_bytes() == original
    assert not destination.exists()
    assert not trash.calls
    _assert_no_staging_left(tmp_path)


def test_apply_and_replay_are_idempotent_without_virtual_member_outputs(
    api: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "replay.zip"
    _write_zip(source, [("data.txt", b"replay", None), ("sub/note.txt", b"note", None)])
    destination = tmp_path / "replay"
    trash = RecordingTrash(root=tmp_path / "trash")

    first = _call_intake(api, source, destination, apply=True, trash=trash)
    snapshot = {path.relative_to(destination): path.read_bytes() for path in destination.rglob("*") if path.is_file()}
    # A replay may be recognized from intake state or may report source_absent
    # after the first verified KIO move. It must not replace or duplicate data.
    replay_source = source if source.exists() else tmp_path / "trash" / source.name
    second = _call_intake(api, replay_source, destination, apply=True, trash=trash)

    assert _status(first) in {"complete", "applied", "success"}, _as_mapping(first)
    assert _status(second) in {"complete", "applied", "success", "reused", "already_applied", "source_absent", "collision"}, _as_mapping(second)
    assert snapshot == {
        path.relative_to(destination): path.read_bytes() for path in destination.rglob("*") if path.is_file()
    }
    assert all("!/" not in os.fspath(path) for path in destination.rglob("*"))


@pytest.mark.parametrize(
    "builder_and_tokens",
    [
        ("encrypted", ("password", "encrypted", "blocked")),
        ("crc", ("crc", "corrupt", "integrity")),
    ],
)
def test_encrypted_and_crc_corrupt_zips_remain_intact(
    api: ModuleType, tmp_path: Path, builder_and_tokens: tuple[str, tuple[str, ...]]
) -> None:
    kind, tokens = builder_and_tokens
    source = tmp_path / f"{kind}.zip"
    if kind == "encrypted":
        _write_encrypted_marker(source)
    else:
        _write_crc_corrupt(source)
    original = source.read_bytes()

    result = _call_intake(api, source, tmp_path / kind, apply=True, trash=RecordingTrash())

    _assert_rejected(result, *tokens)
    assert source.read_bytes() == original
    assert not (tmp_path / kind).exists()


def test_crc_failure_cannot_publish_a_valid_prefix(api: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "late-crc.zip"
    _write_zip(source, [("good.txt", b"good", None), ("bad.txt", b"bad", None)], compression=zipfile.ZIP_STORED)
    raw = bytearray(source.read_bytes())
    central = raw.find(b"PK\x01\x02")
    assert central > 0
    raw[central - 1] ^= 0xFF
    source.write_bytes(raw)
    original = source.read_bytes()

    result = _call_intake(api, source, tmp_path / "late-crc", apply=True, trash=RecordingTrash())

    _assert_rejected(result, "crc", "corrupt", "integrity")
    assert source.read_bytes() == original
    assert not (tmp_path / "late-crc").exists()


def test_compression_bomb_and_member_budget_fail_closed_without_partial_tree(
    api: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "bomb.zip"
    _write_zip(source, [("bomb.bin", b"A" * 512_000, None)])
    original = source.read_bytes()
    limits = _make_limits(api, max_compression_ratio=2.0, max_member_bytes=1_000_000)

    result = _call_intake(api, source, tmp_path / "bomb", apply=True, limits=limits, trash=RecordingTrash())

    _assert_rejected(result, "budget", "ratio", "bomb", "unsafe")
    assert source.read_bytes() == original
    assert not (tmp_path / "bomb").exists()


@pytest.mark.parametrize(
    ("limit_overrides", "tokens"),
    [
        ({"max_member_bytes": 3}, ("budget", "member")),
        ({"max_total_uncompressed_bytes": 5}, ("budget", "total")),
        ({"max_members": 1}, ("budget", "members")),
    ],
)
def test_member_total_and_count_limits_are_transactional(
    api: ModuleType,
    tmp_path: Path,
    limit_overrides: dict[str, object],
    tokens: tuple[str, ...],
) -> None:
    source = tmp_path / "limits.zip"
    _write_zip(source, [("one.txt", b"1234", None), ("two.txt", b"5678", None)])
    original = source.read_bytes()
    limits = _make_limits(api, **limit_overrides)

    result = _call_intake(api, source, tmp_path / "limits", apply=True, limits=limits, trash=RecordingTrash())

    _assert_rejected(result, *tokens)
    assert source.read_bytes() == original
    assert not (tmp_path / "limits").exists()


def test_nested_depth_budget_rejects_the_root_transaction(api: ModuleType, tmp_path: Path) -> None:
    deepest = _zip_bytes([("leaf.txt", b"leaf", None)])
    middle = _zip_bytes([("deep.zip", deepest, None)])
    source = tmp_path / "depth.zip"
    _write_zip(source, [("middle.zip", middle, None)])
    original = source.read_bytes()
    limits = _make_limits(api, max_depth=1, max_nested_depth=1)

    result = _call_intake(api, source, tmp_path / "depth", apply=True, limits=limits, trash=RecordingTrash())

    _assert_rejected(result, "depth", "budget", "nested", "limit")
    assert source.read_bytes() == original
    assert not (tmp_path / "depth").exists()


def test_cancellation_leaves_source_and_no_staging_tree(api: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "cancel.zip"
    _write_zip(source, [(f"item-{index}.txt", b"payload", None) for index in range(30)])
    original = source.read_bytes()
    token = CancellationToken()
    token.cancel()

    result = _call_intake(api, source, tmp_path / "cancel", apply=True, cancellation=token, trash=RecordingTrash())

    _assert_rejected(result, "cancel", "aborted", "requested")
    assert source.read_bytes() == original
    assert not (tmp_path / "cancel").exists()
    _assert_no_staging_left(tmp_path)


def test_destination_collision_abstains_without_merge_or_overwrite(
    api: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "collision.zip"
    _write_zip(source, [("new.txt", b"must not merge", None)])
    destination = tmp_path / "collision"
    destination.mkdir()
    sentinel = destination / "existing.txt"
    sentinel.write_bytes(b"canonical existing data")
    original = source.read_bytes()

    result = _call_intake(api, source, destination, apply=True, trash=RecordingTrash())

    _assert_rejected(result, "collision", "destination", "abstain")
    assert source.read_bytes() == original
    assert sentinel.read_bytes() == b"canonical existing data"
    assert not (destination / "new.txt").exists()


def test_source_identity_change_before_publish_requires_recovery_and_no_claim(
    api: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "raced.zip"
    _write_zip(source, [("data.txt", b"race", None)])
    original = source.read_bytes()
    from neocortex.capabilities.formats.archive.intake import FilesystemPublishHook

    base_publisher = FilesystemPublishHook()

    class MutatingPublisher:
        def publish(self, staged_path: Path, destination: Path) -> object:
            receipt = base_publisher.publish(staged_path, destination)
            source.write_bytes(source.read_bytes() + b"\x00mutation")
            return receipt

        def rollback(self, receipt: object) -> bool:
            return base_publisher.rollback(receipt)  # type: ignore[arg-type]

    result = _call_intake(
        api,
        source,
        tmp_path / "raced",
        apply=True,
        trash=RecordingTrash(root=tmp_path / "trash"),
        publisher=MutatingPublisher(),
    )

    _assert_rejected(result, "recovery", "changed", "identity", "stale")
    assert source.exists()
    assert source.read_bytes() != original
    assert not (tmp_path / "trash" / source.name).exists()


def test_kio_failure_rolls_back_or_leaves_an_explicit_recovery_state(
    api: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "kio-failure.zip"
    _write_zip(source, [("data.txt", b"data", None)])
    original = source.read_bytes()
    trash = RecordingTrash(fail=OSError("fixture KIO unavailable"))

    result = _call_intake(api, source, tmp_path / "kio-failure", apply=True, trash=trash)

    _assert_rejected(result, "kio", "recovery", "blocked", "unavailable")
    assert source.exists()
    assert source.read_bytes() == original
    assert not (tmp_path / "kio-failure" / "data.txt").exists()


def test_global_size_admission_skips_source_before_opening_zip(api: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "large-source.zip"
    source.write_bytes(b"X" * 10_000_001)
    destination = tmp_path / "large-source"

    def fail_if_opened(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"oversize source was opened: {args!r} {kwargs!r}")

    monkeypatch.setattr(zipfile, "ZipFile", fail_if_opened)
    result = _call_intake(
        api,
        source,
        destination,
        apply=True,
        max_file_bytes=10_000_000,
        trash=RecordingTrash(),
    )

    _assert_rejected(result, "skipped_by_size", "oversize", "size")
    assert source.exists()
    assert not destination.exists()


def test_small_container_can_publish_a_member_larger_than_global_limit(
    api: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "small-container.zip"
    payload = (bytes(range(256)) * 390) + bytes(range(105))
    _write_zip(source, [("huge.bin", payload, None)])
    assert source.stat().st_size < 10_000_000
    destination = tmp_path / "small-container"
    result = _call_intake(
        api,
        source,
        destination,
        apply=True,
        max_file_bytes=10_000_000,
        trash=RecordingTrash(root=tmp_path / "trash"),
    )

    assert _status(result) in {"complete", "applied", "success"}, _as_mapping(result)
    # ZIP source admission and downstream member admission are separate: the
    # source is eligible here, while the normal pipeline must later apply -S
    # to this physical successor.
    assert (destination / "huge.bin").read_bytes() == payload


def test_receipt_contains_transaction_identity_and_no_virtual_paths(api: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "receipt.zip"
    _write_zip(source, [("data.txt", b"receipt", None)])
    destination = tmp_path / "receipt"
    result = _call_intake(api, source, destination, apply=False)
    mapping = _as_mapping(result)

    serialized = str(mapping)
    identity = _value(result, "source_identity", default={})
    assert _value(identity, "size", default=None) == source.stat().st_size
    assert "source_path" in mapping
    assert "!/" not in serialized
    assert _value(result, "apply", default=False) is False
