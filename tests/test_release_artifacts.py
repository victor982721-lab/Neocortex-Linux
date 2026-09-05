# region [00] Contexto del módulo
# Módulo: tests/test_release_artifacts.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import importlib.util
import io
import stat
import struct
import tarfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO

import pytest

from tools import release_archive_safety, release_artifacts
from tools.release_artifacts import (
    DEFAULT_LIMITS,
    SOURCE_DATE_EPOCH,
    ArchiveLimits,
    ArtifactValidationError,
    canonicalize_sdist,
    compare_logical_payloads,
    inspect_archive,
    logical_payload,
    validate_release_artifact,
    validate_sdist,
    validate_wheel,
)
# endregion [01]

# region [02] Implementación


_DIST_INFO = "neocortex_framework-0.7.2.dist-info"
_SDIST_ROOT = "neocortex_framework-0.7.2"
_METADATA = (
    "Metadata-Version: 2.4\n"
    "Name: neocortex-framework\n"
    "Version: 0.7.2\n"
    "Summary: Synthetic release fixture\n"
).encode()
_WHEEL = (
    "Wheel-Version: 1.0\nGenerator: Neocortex fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
).encode()
_ENTRY_POINTS = ("[console_scripts]\nNeocortex = neocortex.interface.entrypoint:entrypoint\n").encode()
_UI_ASSETS = (
    "neocortex/interface/presentation/assets/neocortex-app-icon.ico",
    "neocortex/interface/presentation/assets/neocortex-app-icon.png",
    "neocortex/interface/presentation/assets/neocortex-app-icon.svg",
)
_SOURCE_ONLY_TOOLS = (
    "tools/__init__.py",
    "tools/pip_bootstrap.py",
    "tools/release_archive_safety.py",
    "tools/release_artifacts.py",
    "tools/release_linux.py",
)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_UI_ASSET_PAYLOADS = {asset: (_PROJECT_ROOT / asset).read_bytes() for asset in _UI_ASSETS}


def _record_bytes(files: Mapping[str, bytes]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name in sorted(files):
        digest = base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest())
        writer.writerow((name, f"sha256={digest.rstrip(b'=').decode()}", len(files[name])))
    writer.writerow((f"{_DIST_INFO}/RECORD", "", ""))
    return output.getvalue().encode()


def _corrupt_first_record_size(value: bytes) -> bytes:
    rows = list(csv.reader(io.StringIO(value.decode())))
    rows[0][2] = str(int(rows[0][2]) + 1)
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    return output.getvalue().encode()


def _wheel_payloads() -> dict[str, bytes]:
    payloads = {
        "neocortex/__init__.py": b'__version__ = "0.7.2"\n',
        "neocortex/interface/entrypoint.py": b"def entrypoint():\n    return 0\n",
        "neocortex/py.typed": b"",
        f"{_DIST_INFO}/METADATA": _METADATA,
        f"{_DIST_INFO}/WHEEL": _WHEEL,
        f"{_DIST_INFO}/entry_points.txt": _ENTRY_POINTS,
    }
    payloads.update(_UI_ASSET_PAYLOADS)
    return payloads


def _write_wheel(
    path: Path,
    *,
    updates: Mapping[str, bytes] | None = None,
    remove: Sequence[str] = (),
    record_transform: Callable[[bytes], bytes] | None = None,
    reverse: bool = False,
    timestamp: tuple[int, int, int, int, int, int] = (2026, 7, 30, 12, 0, 0),
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    files = _wheel_payloads()
    if updates:
        files.update(updates)
    for name in remove:
        files.pop(name, None)
    record_name = f"{_DIST_INFO}/RECORD"
    if record_name not in remove:
        record = _record_bytes(files)
        files[record_name] = record if record_transform is None else record_transform(record)
    names = sorted(files, reverse=reverse)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            info = zipfile.ZipInfo(name, date_time=timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, files[name])
    return path


def _sdist_payloads(root: str = _SDIST_ROOT) -> dict[str, bytes]:
    payloads = {
        f"{root}/PKG-INFO": _METADATA,
        f"{root}/pyproject.toml": b"[build-system]\nrequires=[]\n",
        f"{root}/MANIFEST.in": b"include README.md\n",
        f"{root}/README.md": b"# Synthetic Neocortex\n",
        f"{root}/neocortex/__init__.py": b'__version__ = "0.7.2"\n',
        f"{root}/neocortex/interface/entrypoint.py": b"def entrypoint():\n    return 0\n",
        f"{root}/neocortex/py.typed": b"",
    }
    payloads.update({f"{root}/{path}": payload for path, payload in _UI_ASSET_PAYLOADS.items()})
    payloads.update(
        {f"{root}/{path}": b"# synthetic source-only tool\n" for path in _SOURCE_ONLY_TOOLS}
    )
    return payloads


def _write_sdist(
    path: Path,
    *,
    payloads: Mapping[str, bytes] | None = None,
    reverse: bool = False,
    mtime: int = 1_700_000_000,
    extra_members: Sequence[tarfile.TarInfo] = (),
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    files = dict(_sdist_payloads() if payloads is None else payloads)
    names = sorted(files, reverse=reverse)
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for name in names:
            data = files[name]
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = mtime
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
        for info in extra_members:
            info.mtime = mtime
            archive.addfile(info)
    return path


def _sdist_path(root: Path, build: str) -> Path:
    return root / build / "neocortex_framework-0.7.2.tar.gz"


def _write_zip(path: Path, payloads: Mapping[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in payloads.items():
            archive.writestr(name, data)
    raw = path.read_bytes()
    for name in payloads:
        if "\\" not in name:
            continue
        normalized = name.replace("\\", "/").encode()
        encoded = name.encode()
        if raw.count(encoded) == 2:
            continue
        if len(normalized) != len(encoded) or raw.count(normalized) != 2:
            raise AssertionError("unable to preserve raw ZIP backslash fixture")
        raw = raw.replace(normalized, encoded)
    path.write_bytes(raw)
    return path


def _write_tar(path: Path, payloads: Mapping[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name, data in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def test_public_api_and_reproducibility_epoch_are_stable() -> None:
    assert release_artifacts.__all__ == [
        "ArchiveInspection",
        "ArchiveLimits",
        "ArchiveMember",
        "ArtifactValidationError",
        "DEFAULT_LIMITS",
        "LogicalPayload",
        "SOURCE_DATE_EPOCH",
        "canonicalize_sdist",
        "compare_logical_payloads",
        "inspect_archive",
        "logical_payload",
        "validate_release_artifact",
        "validate_sdist",
        "validate_wheel",
    ]
    assert SOURCE_DATE_EPOCH == 1_785_369_600
    assert DEFAULT_LIMITS == ArchiveLimits()
    assert DEFAULT_LIMITS.max_members > 0
    assert DEFAULT_LIMITS.max_member_bytes > 0
    assert DEFAULT_LIMITS.max_total_bytes >= DEFAULT_LIMITS.max_member_bytes


@pytest.mark.parametrize("archive_kind", ["zip", "tar"])
def test_inspection_is_bounded_and_reports_member_hashes(
    tmp_path: Path,
    archive_kind: str,
) -> None:
    payloads = {"package/module.py": b"answer = 42\n", "package/py.typed": b""}
    if archive_kind == "zip":
        path = _write_zip(tmp_path / "fixture.zip", payloads)
    else:
        path = _write_tar(tmp_path / "fixture.tar.gz", payloads)

    report = inspect_archive(path)

    assert report.kind == archive_kind
    assert report.path == path.resolve()
    assert report.archive_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert tuple(member.path for member in report.members) == tuple(sorted(payloads))
    assert tuple(member.size for member in report.members) == tuple(
        len(payloads[name]) for name in sorted(payloads)
    )
    assert tuple(member.sha256 for member in report.members) == tuple(
        hashlib.sha256(payloads[name]).hexdigest() for name in sorted(payloads)
    )


@pytest.mark.parametrize(
    "name",
    [
        "../escape.py",
        "safe/../../escape.py",
        "/absolute.py",
        "C:" + "/" + "Us" + "ers/Victor/private.py",
        "/" + "/" + "server/share/private.py",
        "safe\\windows.py",
    ],
)
@pytest.mark.parametrize("archive_kind", ["zip", "tar"])
def test_archives_reject_unsafe_member_paths(
    tmp_path: Path,
    archive_kind: str,
    name: str,
) -> None:
    path = tmp_path / ("unsafe.zip" if archive_kind == "zip" else "unsafe.tar.gz")
    writer = _write_zip if archive_kind == "zip" else _write_tar
    writer(path, {name: b"payload"})

    with pytest.raises(ArtifactValidationError, match="unsafe archive member path"):
        inspect_archive(path)


@pytest.mark.parametrize("archive_kind", ["zip", "tar"])
def test_archives_reject_casefold_collisions(
    tmp_path: Path,
    archive_kind: str,
) -> None:
    path = tmp_path / ("collision.zip" if archive_kind == "zip" else "collision.tar.gz")
    payloads = {"Package/File.py": b"first", "package/file.py": b"second"}
    writer = _write_zip if archive_kind == "zip" else _write_tar
    writer(path, payloads)

    with pytest.raises(ArtifactValidationError, match="casefold collision"):
        inspect_archive(path)


@pytest.mark.parametrize("link_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_tar_rejects_symbolic_and_hard_links(
    tmp_path: Path,
    link_type: bytes,
) -> None:
    link = tarfile.TarInfo(f"{_SDIST_ROOT}/linked.py")
    link.type = link_type
    link.linkname = f"{_SDIST_ROOT}/neocortex/interface/entrypoint.py"
    path = _write_sdist(_sdist_path(tmp_path, "linked"), extra_members=(link,))

    with pytest.raises(ArtifactValidationError, match="TAR links are forbidden"):
        inspect_archive(path)


def test_archive_member_and_total_limits_are_enforced(tmp_path: Path) -> None:
    path = _write_zip(tmp_path / "bounded.zip", {"a.txt": b"1234", "b.txt": b"5678"})

    with pytest.raises(ArtifactValidationError, match="member count limit"):
        inspect_archive(path, limits=replace(DEFAULT_LIMITS, max_members=1))
    with pytest.raises(ArtifactValidationError, match="member size limit"):
        inspect_archive(path, limits=replace(DEFAULT_LIMITS, max_member_bytes=3))
    with pytest.raises(ArtifactValidationError, match="total size limit"):
        inspect_archive(path, limits=replace(DEFAULT_LIMITS, max_total_bytes=7))
    with pytest.raises(ArtifactValidationError, match="archive size limit"):
        inspect_archive(path, limits=replace(DEFAULT_LIMITS, max_archive_bytes=1))


@pytest.mark.parametrize(
    "name",
    [
        "package/module.pyc",
        "package/__pycache__/module.py",
        "package/.pytest_cache/nodeids",
        "package/catalog.sqlite3",
        "package/state/current.json",
        "package/backups/source.py",
        "package/temp/output.txt",
        "package/.env",
        "package/id_rsa",
        "package/signing-key.pem",
    ],
)
def test_forbidden_artifact_member_categories_are_rejected(
    tmp_path: Path,
    name: str,
) -> None:
    path = _write_zip(tmp_path / "forbidden.zip", {name: b"payload"})

    with pytest.raises(ArtifactValidationError, match="forbidden artifact member"):
        inspect_archive(path)


@pytest.mark.parametrize(
    ("payload", "finding"),
    [
        (
            "source=C:".encode()
            + "\\".encode()
            + "Us".encode()
            + "ers\\Victor\\Documents\\private.txt".encode(),
            "private path",
        ),
        (
            ("source=C:" + "\\" + "Us" + "ers\\Victor\\private.txt").encode("utf-16-le"),
            "private path",
        ),
        (
            ("source=/" + "ho" + "me/victor/private.txt").encode("utf-16-be"),
            "private path",
        ),
        (("api_" + "key=" + "sk" + "-fixture-secret-value-123456").encode(), "secret"),
        (
            ("-----BEGIN PRIVATE " + "KEY-----\nfixture\n").encode("utf-16-le"),
            "secret",
        ),
        (b"SQLite " + b"format 3\x00fixture", "SQLite"),
        (importlib.util.MAGIC_NUMBER + b"disguised bytecode", "bytecode"),
        (
            b"\xff\xfe"
            + ("source=C:" + "\\" + "Us" + "ers\\Victor\\private.txt").encode("utf-16-le"),
            "private path",
        ),
        (
            b"x" + ("api_" + "key=" + "sk" + "-offset-secret-value-123456").encode("utf-16-le"),
            "secret",
        ),
        (
            b"x" + ("source=/" + "ho" + "me/victor/private.txt").encode("utf-16-be"),
            "private path",
        ),
    ],
)
def test_private_ascii_and_utf16_payloads_are_rejected(
    tmp_path: Path,
    payload: bytes,
    finding: str,
) -> None:
    path = _write_zip(tmp_path / "private.zip", {"package/data.bin": payload})

    with pytest.raises(ArtifactValidationError, match=finding):
        inspect_archive(path)


@pytest.mark.parametrize(
    "url",
    [
        "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        "https://github.com/openai/example",
    ],
)
def test_http_and_https_payloads_are_not_private_paths(
    tmp_path: Path,
    url: str,
) -> None:
    path = _write_zip(tmp_path / "public-url.zip", {"package/data.txt": url.encode()})

    report = inspect_archive(path)

    assert tuple(member.path for member in report.members) == ("package/data.txt",)


def test_file_url_payload_is_rejected_as_a_private_path(tmp_path: Path) -> None:
    path = _write_zip(
        tmp_path / "file-url.zip",
        {"package/data.txt": b"file:" + bytes((47, 47)) + b"server/share/private.txt"},
    )

    with pytest.raises(ArtifactValidationError, match="private path"):
        inspect_archive(path)


@pytest.mark.parametrize(
    "unc_path",
    [
        bytes((47, 47)) + b"server/share/private.txt",
        bytes((92, 92)) + b"server\\share\\private.txt",
    ],
)
def test_unc_payloads_are_rejected_as_private_paths(
    tmp_path: Path,
    unc_path: bytes,
) -> None:
    path = _write_zip(tmp_path / "unc.zip", {"package/data.txt": unc_path})

    with pytest.raises(ArtifactValidationError, match="private path"):
        inspect_archive(path)


def test_win32_device_namespace_template_is_not_treated_as_private_unc(
    tmp_path: Path,
) -> None:
    device_template = bytes((92, 92, 46, 92)) + b"{drive}:"
    path = _write_zip(
        tmp_path / "win32-device.zip",
        {"package/windows.py": b"device = rf'" + device_template + b"'\n"},
    )

    report = inspect_archive(path)

    assert tuple(member.path for member in report.members) == ("package/windows.py",)


def test_escaped_sql_backslash_literal_is_not_treated_as_private_unc(
    tmp_path: Path,
) -> None:
    escaped_backslash = bytes((92, 92))
    payload = b"normalized = replace(column,'" + escaped_backslash + b"','/')\n"
    path = _write_zip(tmp_path / "sql-literal.zip", {"package/query.py": payload})

    report = inspect_archive(path)

    assert tuple(member.path for member in report.members) == ("package/query.py",)


@pytest.mark.parametrize("archive_kind", ["zip", "tar"])
@pytest.mark.parametrize(
    "name",
    [
        "AGENTS.md",
        "AGENTS.override.md",
        "tests/test_internal.py",
        "tests/fixtures/knowledge/query.json",
        "docs/KNOWLEDGE_EVOLUTION_SAMPLE.md",
        "docs/TECHNICAL_AUDIT_SAMPLE.md",
        "docs/TECHNICAL_EVOLUTION_SAMPLE.md",
        "docs/TECHNICAL_EVOLUTION_REPORT_SAMPLE.md",
    ],
)
def test_release_internal_members_are_rejected(
    tmp_path: Path,
    archive_kind: str,
    name: str,
) -> None:
    path = tmp_path / ("internal.zip" if archive_kind == "zip" else "internal.tar.gz")
    writer = _write_zip if archive_kind == "zip" else _write_tar
    writer(path, {name: b"internal release material"})

    with pytest.raises(ArtifactValidationError, match="release-internal"):
        inspect_archive(path)


def test_valid_wheel_contract_includes_verified_record_and_typed_markers(
    tmp_path: Path,
) -> None:
    path = _write_wheel(tmp_path / "neocortex_framework-0.7.2-py3-none-any.whl")

    report = validate_wheel(path, expected_version="0.7.2")

    assert report.kind == "wheel"
    assert report.distribution == "neocortex-framework"
    assert report.version == "0.7.2"
    assert report.root is None
    assert report.entry_points == ("Neocortex = neocortex.interface.entrypoint:entrypoint",)
    assert report.record_verified
    assert report.typed_packages == ("neocortex",)
    assert validate_release_artifact(path) == report


def test_repository_ui_assets_pass_payload_policy(tmp_path: Path) -> None:
    path = _write_zip(
        tmp_path / "repository-assets.zip",
        _UI_ASSET_PAYLOADS,
    )

    report = inspect_archive(path)

    assert tuple(member.path for member in report.members) == _UI_ASSETS


def test_ui_asset_payload_hash_is_pinned(tmp_path: Path) -> None:
    asset = _UI_ASSETS[1]
    mutated = bytearray(_UI_ASSET_PAYLOADS[asset])
    mutated[-1] ^= 1
    path = _write_zip(tmp_path / "mutated-asset.zip", {asset: bytes(mutated)})

    with pytest.raises(ArtifactValidationError, match="UI asset payload hash"):
        inspect_archive(path)


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        (f"{_DIST_INFO}/METADATA", "METADATA"),
        (f"{_DIST_INFO}/WHEEL", "WHEEL"),
        (f"{_DIST_INFO}/entry_points.txt", "entry_points.txt"),
        (f"{_DIST_INFO}/RECORD", "RECORD"),
        ("neocortex/py.typed", "py.typed"),
    ],
)
def test_wheel_rejects_missing_contract_members(
    tmp_path: Path,
    missing: str,
    message: str,
) -> None:
    path = _write_wheel(
        tmp_path / "neocortex_framework-0.7.2-py3-none-any.whl",
        remove=(missing,),
    )

    with pytest.raises(ArtifactValidationError, match=message):
        validate_wheel(path)


@pytest.mark.parametrize("asset", _UI_ASSETS)
def test_wheel_requires_all_ui_assets(tmp_path: Path, asset: str) -> None:
    path = _write_wheel(
        tmp_path / "neocortex_framework-0.7.2-py3-none-any.whl",
        remove=(asset,),
    )

    with pytest.raises(ArtifactValidationError, match="required UI asset"):
        validate_wheel(path)


def test_wheel_rejects_source_only_release_tools(tmp_path: Path) -> None:
    path = _write_wheel(
        tmp_path / "neocortex_framework-0.7.2-py3-none-any.whl",
        updates={"tools/release_artifacts.py": b"# source-only\n"},
    )

    with pytest.raises(ArtifactValidationError, match="source-only tool"):
        validate_wheel(path)


def test_wheel_rejects_invalid_entrypoint(tmp_path: Path) -> None:
    path = _write_wheel(
        tmp_path / "neocortex_framework-0.7.2-py3-none-any.whl",
        updates={
            f"{_DIST_INFO}/entry_points.txt": (b"[console_scripts]\nNeocortex = legacy.cli:main\n")
        },
    )

    with pytest.raises(ArtifactValidationError, match="console entrypoint"):
        validate_wheel(path)


@pytest.mark.parametrize(
    ("transform", "message"),
    [
        (
            lambda value: value.replace(b"sha256=", b"sha256=broken", 1),
            "RECORD hash",
        ),
        (
            _corrupt_first_record_size,
            "RECORD size",
        ),
        (
            lambda value: b"\n".join(value.splitlines()[1:]) + b"\n",
            "RECORD member set",
        ),
    ],
)
def test_wheel_record_hashes_sizes_and_member_set_are_enforced(
    tmp_path: Path,
    transform: Callable[[bytes], bytes],
    message: str,
) -> None:
    path = _write_wheel(
        tmp_path / "neocortex_framework-0.7.2-py3-none-any.whl",
        record_transform=transform,
    )

    with pytest.raises(ArtifactValidationError, match=message):
        validate_wheel(path)


def test_wheel_metadata_and_filename_must_agree(tmp_path: Path) -> None:
    path = _write_wheel(tmp_path / "neocortex_framework-9.9.9-py3-none-any.whl")

    with pytest.raises(ArtifactValidationError, match="wheel filename metadata"):
        validate_wheel(path)


def test_valid_sdist_has_one_root_pkg_info_and_required_content(tmp_path: Path) -> None:
    path = _write_sdist(tmp_path / "neocortex_framework-0.7.2.tar.gz")

    report = validate_sdist(path, expected_version="0.7.2")

    assert report.kind == "sdist"
    assert report.root == _SDIST_ROOT
    assert report.distribution == "neocortex-framework"
    assert report.version == "0.7.2"
    assert not report.record_verified
    assert report.entry_points == ()
    assert report.typed_packages == ("neocortex",)
    assert validate_release_artifact(path) == report


def test_sdist_rejects_multiple_roots(tmp_path: Path) -> None:
    payloads = _sdist_payloads()
    payloads["foreign-root/file.py"] = b"payload"
    path = _write_sdist(_sdist_path(tmp_path, "multiple"), payloads=payloads)

    with pytest.raises(ArtifactValidationError, match="single root"):
        validate_sdist(path)


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("PKG-INFO", "PKG-INFO"),
        ("pyproject.toml", "required sdist content"),
        ("neocortex/py.typed", "required sdist content"),
    ],
)
def test_sdist_rejects_missing_contract_content(
    tmp_path: Path,
    missing: str,
    message: str,
) -> None:
    payloads = _sdist_payloads()
    payloads.pop(f"{_SDIST_ROOT}/{missing}")
    path = _write_sdist(_sdist_path(tmp_path, "missing"), payloads=payloads)

    with pytest.raises(ArtifactValidationError, match=message):
        validate_sdist(path)


@pytest.mark.parametrize("asset", _UI_ASSETS)
def test_sdist_requires_all_ui_assets(tmp_path: Path, asset: str) -> None:
    payloads = _sdist_payloads()
    payloads.pop(f"{_SDIST_ROOT}/{asset}")
    path = _write_sdist(_sdist_path(tmp_path, "missing-asset"), payloads=payloads)

    with pytest.raises(ArtifactValidationError, match="required UI asset"):
        validate_sdist(path)


@pytest.mark.parametrize("tool", _SOURCE_ONLY_TOOLS)
def test_sdist_requires_source_only_release_tools(tmp_path: Path, tool: str) -> None:
    payloads = _sdist_payloads()
    payloads.pop(f"{_SDIST_ROOT}/{tool}")
    path = _write_sdist(_sdist_path(tmp_path, "missing-tool"), payloads=payloads)

    with pytest.raises(ArtifactValidationError, match="required source-only tool"):
        validate_sdist(path)


def test_sdist_root_and_pkg_info_metadata_must_agree(tmp_path: Path) -> None:
    payloads = {
        name.replace(_SDIST_ROOT, "neocortex_framework-9.9.9", 1): data
        for name, data in _sdist_payloads().items()
    }
    path = _write_sdist(_sdist_path(tmp_path, "mismatch"), payloads=payloads)

    with pytest.raises(ArtifactValidationError, match="sdist root metadata"):
        validate_sdist(path)


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
def test_logical_payload_ignores_container_order_and_timestamps(
    tmp_path: Path,
    artifact_kind: str,
) -> None:
    if artifact_kind == "wheel":
        first = _write_wheel(
            tmp_path / "neocortex_framework-0.7.2-py3-none-any.whl",
            timestamp=(2025, 1, 1, 0, 0, 0),
        )
        second = _write_wheel(
            tmp_path / "build-two" / "neocortex_framework-0.7.2-py3-none-any.whl",
            reverse=True,
            timestamp=(2026, 7, 30, 12, 0, 0),
        )
    else:
        first = _write_sdist(
            tmp_path / "neocortex_framework-0.7.2.tar.gz",
            mtime=1_600_000_000,
        )
        second = _write_sdist(
            tmp_path / "build-two" / "neocortex_framework-0.7.2.tar.gz",
            reverse=True,
            mtime=1_700_000_000,
        )

    left = validate_wheel(first) if artifact_kind == "wheel" else validate_sdist(first)
    right = validate_wheel(second) if artifact_kind == "wheel" else validate_sdist(second)
    expected = logical_payload(left)

    assert logical_payload(right) == expected
    assert compare_logical_payloads(first, second) == expected


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
def test_logical_payload_comparison_rejects_changed_content(
    tmp_path: Path,
    artifact_kind: str,
) -> None:
    if artifact_kind == "wheel":
        first = _write_wheel(tmp_path / "neocortex_framework-0.7.2-py3-none-any.whl")
        second = _write_wheel(
            tmp_path / "changed" / "neocortex_framework-0.7.2-py3-none-any.whl",
            updates={"neocortex/interface/entrypoint.py": b"def entrypoint():\n    return 1\n"},
        )
    else:
        first = _write_sdist(tmp_path / "neocortex_framework-0.7.2.tar.gz")
        changed = _sdist_payloads()
        changed[f"{_SDIST_ROOT}/neocortex/interface/entrypoint.py"] = b"def entrypoint():\n    return 1\n"
        second = _write_sdist(
            tmp_path / "changed" / "neocortex_framework-0.7.2.tar.gz",
            payloads=changed,
        )

    with pytest.raises(ArtifactValidationError, match="logical payload mismatch"):
        compare_logical_payloads(first, second)


def test_sdist_canonicalization_requires_logical_equality_before_output(
    tmp_path: Path,
) -> None:
    first = _write_sdist(_sdist_path(tmp_path, "first"))
    changed = _sdist_payloads()
    changed[f"{_SDIST_ROOT}/neocortex/interface/entrypoint.py"] = b"changed\n"
    second = _write_sdist(_sdist_path(tmp_path, "second"), payloads=changed)
    destination = _sdist_path(tmp_path, "canonical")

    with pytest.raises(ArtifactValidationError, match="logical payload mismatch"):
        canonicalize_sdist(first, second, destination)

    assert not destination.exists()


def test_sdist_canonicalization_is_byte_deterministic(tmp_path: Path) -> None:
    first = _write_sdist(
        _sdist_path(tmp_path, "first"),
        mtime=1_600_000_000,
    )
    second = _write_sdist(
        _sdist_path(tmp_path, "second"),
        reverse=True,
        mtime=1_700_000_000,
    )
    output_a = tmp_path / "canonical-a" / "neocortex_framework-0.7.2.tar.gz"
    output_b = tmp_path / "canonical-b" / "neocortex_framework-0.7.2.tar.gz"
    output_a.parent.mkdir()
    output_b.parent.mkdir()

    report_a = canonicalize_sdist(first, second, output_a)
    report_b = canonicalize_sdist(second, first, output_b)

    assert report_a.archive_sha256 == report_b.archive_sha256
    assert output_a.read_bytes() == output_b.read_bytes()
    with output_a.open("rb") as stream:
        stream.read(4)
        assert int.from_bytes(stream.read(4), "little") == SOURCE_DATE_EPOCH
    with gzip.open(output_a, "rb") as stream:
        with tarfile.open(fileobj=stream, mode="r:") as archive:
            members = archive.getmembers()
    assert [member.name for member in members] == sorted(member.name for member in members)
    assert all(member.mtime == SOURCE_DATE_EPOCH for member in members)
    assert all(member.uid == member.gid == 0 for member in members)
    assert all(member.uname == member.gname == "" for member in members)
    assert validate_sdist(output_a) == report_a


def test_zip_preflight_rejects_forged_count_before_infolist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_zip(tmp_path / "forged.zip", {"package/module.py": b"safe\n"})
    raw = bytearray(path.read_bytes())
    eocd = raw.rfind(b"PK\x05\x06")
    assert eocd >= 0
    forged_count = DEFAULT_LIMITS.max_members + 1
    struct.pack_into("<H", raw, eocd + 8, forged_count)
    struct.pack_into("<H", raw, eocd + 10, forged_count)
    path.write_bytes(raw)

    def forbidden_infolist(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ZipFile must not be constructed before preflight")

    monkeypatch.setattr(release_archive_safety.zipfile, "ZipFile", forbidden_infolist)
    with pytest.raises(ArtifactValidationError, match="member count limit"):
        inspect_archive(path)


def test_invalid_utf8_zip_name_is_translated(tmp_path: Path) -> None:
    path = _write_zip(tmp_path / "invalid-utf8.zip", {"x": b"safe\n"})
    raw = bytearray(path.read_bytes())
    local = raw.find(b"PK\x03\x04")
    central = raw.find(b"PK\x01\x02")
    assert local >= 0 and central >= 0
    local_flags = struct.unpack_from("<H", raw, local + 6)[0]
    central_flags = struct.unpack_from("<H", raw, central + 8)[0]
    struct.pack_into("<H", raw, local + 6, local_flags | 0x800)
    struct.pack_into("<H", raw, central + 8, central_flags | 0x800)
    raw[local + 30] = 0xFF
    raw[central + 46] = 0xFF
    path.write_bytes(raw)

    with pytest.raises(ArtifactValidationError, match="invalid ZIP artifact"):
        inspect_archive(path)


def test_archive_is_rehashed_on_the_same_handle_after_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_zip(tmp_path / "rehash.zip", {"package/module.py": b"safe\n"})
    original = release_archive_safety._hash_handle
    calls = 0

    def changed_after_scan(stream: BinaryIO) -> str:
        nonlocal calls
        calls += 1
        digest = original(stream)
        return digest if calls == 1 else "0" * 64

    monkeypatch.setattr(release_archive_safety, "_hash_handle", changed_after_scan)
    with pytest.raises(ArtifactValidationError, match="bytes changed during inspection"):
        inspect_archive(path)
    assert calls == 2


def test_tar_decompressed_stream_limit_and_gzip_magic_are_enforced(
    tmp_path: Path,
) -> None:
    bounded = _write_sdist(_sdist_path(tmp_path, "bounded-stream"))
    with pytest.raises(ArtifactValidationError, match="decompressed TAR stream limit"):
        inspect_archive(
            bounded,
            limits=replace(DEFAULT_LIMITS, max_tar_stream_bytes=512),
        )

    invalid = _sdist_path(tmp_path, "invalid-gzip")
    invalid.parent.mkdir(parents=True)
    invalid.write_bytes(b"not a gzip stream")
    with pytest.raises(ArtifactValidationError, match="invalid magic"):
        inspect_archive(invalid)


def test_tar_sparse_and_zip_nonregular_members_are_rejected(tmp_path: Path) -> None:
    sparse = tarfile.TarInfo(f"{_SDIST_ROOT}/sparse.bin")
    sparse.type = tarfile.GNUTYPE_SPARSE
    sparse.size = 0
    sparse_path = _write_sdist(
        _sdist_path(tmp_path, "sparse"),
        extra_members=(sparse,),
    )
    with pytest.raises(ArtifactValidationError, match="sparse TAR member"):
        inspect_archive(sparse_path)

    device_path = tmp_path / "device.zip"
    with zipfile.ZipFile(device_path, "w") as archive:
        info = zipfile.ZipInfo("package/device")
        info.create_system = 3
        info.external_attr = (stat.S_IFCHR | 0o600) << 16
        archive.writestr(info, b"device")
    with pytest.raises(ArtifactValidationError, match="non-regular ZIP member"):
        inspect_archive(device_path)


def test_sdist_root_includes_empty_directories_and_filename_is_canonical(
    tmp_path: Path,
) -> None:
    foreign = tarfile.TarInfo("foreign-root/")
    foreign.type = tarfile.DIRTYPE
    path = _write_sdist(
        _sdist_path(tmp_path, "foreign-directory"),
        extra_members=(foreign,),
    )
    with pytest.raises(ArtifactValidationError, match="single root"):
        validate_sdist(path)

    arbitrary = _write_sdist(tmp_path / "arbitrary.tar.gz")
    with pytest.raises(ArtifactValidationError, match="sdist filename metadata"):
        validate_sdist(arbitrary)


def test_compare_revalidates_forged_reports(tmp_path: Path) -> None:
    path = _write_wheel(tmp_path / "neocortex_framework-0.7.2-py3-none-any.whl")
    report = validate_wheel(path)
    forged = replace(report, members=())

    with pytest.raises(ArtifactValidationError, match="stale or forged"):
        compare_logical_payloads(forged, report)


def test_wheel_record_receives_the_callers_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_wheel(tmp_path / "neocortex_framework-0.7.2-py3-none-any.whl")
    custom = replace(DEFAULT_LIMITS, max_path_length=300)
    original = release_artifacts._validate_record
    observed: list[ArchiveLimits] = []

    def observe(
        payloads: Mapping[str, bytes],
        record_path: str,
        limits: ArchiveLimits,
    ) -> None:
        observed.append(limits)
        original(payloads, record_path, limits)

    monkeypatch.setattr(release_artifacts, "_validate_record", observe)
    validate_wheel(path, limits=custom)
    assert observed == [custom]


def test_canonicalization_rejects_same_build_and_cleans_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _write_sdist(_sdist_path(tmp_path, "build-one"))
    destination = _sdist_path(tmp_path, "same-build-output")
    destination.parent.mkdir(parents=True)
    with pytest.raises(ArtifactValidationError, match="two distinct builds"):
        canonicalize_sdist(first, first, destination)
    assert not destination.exists()

    second = _write_sdist(_sdist_path(tmp_path, "build-two-interrupt"))
    expected = KeyboardInterrupt("canonical write interrupted")

    def interrupt(stream: BinaryIO, _payloads: Mapping[str, bytes], _epoch: int) -> None:
        stream.write(b"partial")
        raise expected

    monkeypatch.setattr(release_artifacts, "_write_canonical_tar", interrupt)
    with pytest.raises(KeyboardInterrupt) as raised:
        canonicalize_sdist(first, second, destination)
    assert raised.value is expected
    assert not destination.exists()
    assert tuple(destination.parent.glob(".*.tar.gz")) == ()


def test_release_artifact_tests_do_not_self_contaminate(tmp_path: Path) -> None:
    path = _write_zip(
        tmp_path / "test-source.zip",
        {"package/test_source.py": Path(__file__).read_bytes()},
    )
    report = inspect_archive(path)
    assert tuple(member.path for member in report.members) == ("package/test_source.py",)


# endregion [02]
