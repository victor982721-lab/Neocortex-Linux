"""Fail-closed validation and reproducibility checks for release artifacts."""
# region [00] Contexto del módulo
# Módulo: tools/release_artifacts.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import base64
import configparser
import csv
import gzip
import hashlib
import importlib.util
import io
import os
import re
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import BinaryIO, Final, Literal

from tools.release_archive_safety import ArchiveSafetyError, scan_archive
# endregion [01]

# region [02] Implementación


SOURCE_DATE_EPOCH: Final = 1_785_369_600
_DISTRIBUTION: Final = "neocortex-framework"
_WHEEL_DISTRIBUTION: Final = "neocortex_framework"
_ENTRY_POINT: Final = ("Neocortex", "neocortex.cli:entrypoint")
_TYPED_PACKAGES: Final = ("_04_Nucleo_Operativo", "neocortex")
_UI_ASSETS: Final = (
    "neocortex/interface/presentation/assets/neocortex-app-icon.ico",
    "neocortex/interface/presentation/assets/neocortex-app-icon.png",
    "neocortex/interface/presentation/assets/neocortex-app-icon.svg",
)
_UI_ASSET_SHA256: Final = {
    "neocortex/interface/presentation/assets/neocortex-app-icon.ico": (
        "FD9520EB4D9FF6E8EDF6D9F8318E6AFE9D9162D4313317E3AC13EB3C28297A47"
    ),
    "neocortex/interface/presentation/assets/neocortex-app-icon.png": (
        "C8DAAEC11AAF57872B5AD010D55117919B16856721D6FD3CA1BE7D9B2EF1E94C"
    ),
    "neocortex/interface/presentation/assets/neocortex-app-icon.svg": (
        "09D29874482810D65C7AA0D5B858C0660D0C2BC9E15D41BFE66C89B9F2BF440A"
    ),
}
_SOURCE_ONLY_TOOLS: Final = (
    "tools/__init__.py",
    "tools/pyright_runtime.py",
    "tools/pyright_runtime_lock/package-lock.json",
    "tools/pyright_runtime_lock/package.json",
    "tools/release_archive_safety.py",
    "tools/release_artifacts.py",
    "tools/release_windows.py",
    "tools/release_windows_receipts.py",
)
_RELEASE_INTERNAL_NAMES: Final = frozenset(
    {"agents.md", "agents.override.md", "neocortex_agents.md"}
)
_RELEASE_INTERNAL_COMPONENTS: Final = frozenset({"fixtures", "tests"})
_RELEASE_REPORT_PREFIXES: Final = (
    "knowledge_evolution_",
    "technical_audit_",
    "technical_evolution_",
)
_REQUIRED_SDIST_CONTENT: Final = (
    "MANIFEST.in",
    "README.md",
    "pyproject.toml",
    "neocortex/__init__.py",
    "neocortex/cli.py",
    "neocortex/py.typed",
    "_04_Nucleo_Operativo/py.typed",
)
_CACHE_COMPONENTS: Final = frozenset(
    {
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "cache",
        "caches",
    }
)
_STATE_COMPONENTS: Final = frozenset({".state", "state"})
_BACKUP_COMPONENTS: Final = frozenset({".backup", "backup", "backups"})
_TEMP_COMPONENTS: Final = frozenset({".temp", ".tmp", "temp", "temporary", "tmp"})
_SECRET_NAMES: Final = frozenset(
    {
        ".env",
        ".netrc",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)
_DATABASE_SUFFIXES: Final = (
    ".db",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
)
_SECRET_SUFFIXES: Final = (".key", ".p12", ".pem", ".pfx")
_COMPONENT_CATEGORIES: Final = (
    (_CACHE_COMPONENTS, "cache"),
    (_STATE_COMPONENTS, "state"),
    (_BACKUP_COMPONENTS, "backup"),
    (_TEMP_COMPONENTS, "temporary"),
)
_UNC_COMPONENT: Final = r'[^<>:"/\\|?*\x00-\x1f\s]+'
_UNC_HOST_COMPONENT: Final = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_PRIVATE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/](?:Users|Documents[ ]and[ ]Settings)[\\/]"
    r"|/(?:home|Users)/[A-Za-z0-9._-]+(?:/|\b)"
    rf"|(?<!http:)(?<!https:)(?:\\\\|//){_UNC_HOST_COMPONENT}"
    rf"[\\/]{_UNC_COMPONENT})",
    re.IGNORECASE,
)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"(?:api[_-]?key|client[_-]?secret|password|secret|access[_-]?token)"
    r"\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{12,}",
    re.IGNORECASE,
)
_TOKEN = re.compile(
    r"(?:AKIA[0-9A-Z]{16}|github_pat_[A-Za-z0-9_]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_-]{16,})"
)
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_]*\Z")


class ArtifactValidationError(ValueError):
    """A release artifact violates a bounded or reproducibility contract."""


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    """Hard limits applied before and during archive parsing."""

    max_archive_bytes: int = 256 * 1024 * 1024
    max_members: int = 10_000
    max_member_bytes: int = 32 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_path_length: int = 512
    max_path_depth: int = 32
    max_compression_ratio: int = 200
    max_central_directory_bytes: int = 32 * 1024 * 1024
    max_tar_stream_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        values = tuple(getattr(self, name) for name in self.__slots__)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise TypeError("archive limits must be integers")
        if any(value <= 0 for value in values):
            raise ValueError("archive limits must be positive")


DEFAULT_LIMITS: Final = ArchiveLimits()


@dataclass(frozen=True, slots=True)
class ArchiveMember:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ArchiveInspection:
    path: Path
    kind: Literal["zip", "tar", "wheel", "sdist"]
    archive_sha256: str
    members: tuple[ArchiveMember, ...]
    root: str | None = None
    distribution: str | None = None
    version: str | None = None
    entry_points: tuple[str, ...] = ()
    typed_packages: tuple[str, ...] = ()
    record_verified: bool = False


@dataclass(frozen=True, slots=True)
class LogicalPayload:
    kind: Literal["wheel", "sdist"]
    members: tuple[ArchiveMember, ...]
    sha256: str


@dataclass(frozen=True, slots=True)
class _Scan:
    report: ArchiveInspection
    payloads: Mapping[str, bytes]
    all_paths: tuple[str, ...]


def _canonical_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _is_release_report(leaf: str) -> bool:
    return leaf.endswith(".md") and leaf.startswith(_RELEASE_REPORT_PREFIXES)


def _is_release_internal_member(
    components: tuple[str, ...],
    leaf: str,
) -> bool:
    if leaf in _RELEASE_INTERNAL_NAMES:
        return True
    if any(part in _RELEASE_INTERNAL_COMPONENTS for part in components):
        return True
    return _is_release_report(leaf)


def _member_category(path: str) -> str | None:
    components = tuple(part.casefold() for part in path.split("/"))
    leaf = components[-1]
    if _is_release_internal_member(components, leaf):
        return "release-internal"
    if leaf.endswith((".pyc", ".pyo")):
        return "bytecode"
    if leaf.endswith(_DATABASE_SUFFIXES):
        return "SQLite"
    for names, category in _COMPONENT_CATEGORIES:
        if any(part in names for part in components):
            return category
    if leaf.endswith((".bak", ".backup")):
        return "backup"
    if leaf.endswith((".temp", ".tmp")):
        return "temporary"
    if leaf in _SECRET_NAMES or leaf.endswith(_SECRET_SUFFIXES) or leaf.startswith("~$"):
        return "secret"
    return None


def _validate_member_policy(path: str) -> None:
    category = _member_category(path)
    if category is not None:
        raise ArtifactValidationError(f"forbidden artifact member category={category}: {path}")


def _ui_asset_contract_name(path: str) -> str | None:
    normalized = path.casefold()
    for asset in _UI_ASSETS:
        canonical = asset.casefold()
        if normalized == canonical or normalized.endswith(f"/{canonical}"):
            return asset
    return None


def _text_views(payload: bytes) -> tuple[str, ...]:
    views = [payload.decode("utf-8", errors="ignore")]
    if len(payload) >= 4:
        for offset in (0, 1):
            fragment = payload[offset:]
            views.append(fragment.decode("utf-16-le", errors="ignore"))
            views.append(fragment.decode("utf-16-be", errors="ignore"))
    return tuple(views)


def _validate_payload_policy(path: str, payload: bytes) -> None:
    asset = _ui_asset_contract_name(path)
    if asset is not None:
        if hashlib.sha256(payload).hexdigest().upper() != _UI_ASSET_SHA256[asset]:
            raise ArtifactValidationError(f"UI asset payload hash is unexpected: {path}")
        return
    if payload.startswith(b"SQLite format 3\x00"):
        raise ArtifactValidationError(f"SQLite payload is forbidden: {path}")
    if payload.startswith(importlib.util.MAGIC_NUMBER):
        raise ArtifactValidationError(f"bytecode payload is forbidden: {path}")
    for text in _text_views(payload):
        if _PRIVATE_PATH.search(text):
            raise ArtifactValidationError(f"private path payload is forbidden: {path}")
        if _PRIVATE_KEY.search(text) or _SECRET_ASSIGNMENT.search(text) or _TOKEN.search(text):
            raise ArtifactValidationError(f"secret payload is forbidden: {path}")


def _scan_archive(path: str | Path, limits: ArchiveLimits) -> _Scan:
    try:
        scanned = scan_archive(
            path,
            limits,
            path_policy=_validate_member_policy,
            payload_policy=_validate_payload_policy,
        )
    except (ArchiveSafetyError, ArtifactValidationError) as exc:
        if isinstance(exc, ArtifactValidationError):
            raise
        raise ArtifactValidationError(str(exc)) from exc
    members = tuple(ArchiveMember(item.path, item.size, item.sha256) for item in scanned.members)
    report = ArchiveInspection(scanned.path, scanned.kind, scanned.archive_sha256, members)
    return _Scan(report, scanned.payloads, scanned.all_paths)


def inspect_archive(
    path: str | Path,
    *,
    limits: ArchiveLimits = DEFAULT_LIMITS,
) -> ArchiveInspection:
    """Inspect one ZIP or TAR without extracting it."""

    return _scan_archive(path, limits).report


def _required(payloads: Mapping[str, bytes], path: str, label: str) -> bytes:
    try:
        return payloads[path]
    except KeyError as exc:
        raise ArtifactValidationError(f"wheel is missing required {label}: {path}") from exc


def _required_members(
    payloads: Mapping[str, bytes],
    required: tuple[str, ...],
    label: str,
    *,
    root: str = "",
) -> None:
    prefix = f"{root}/" if root else ""
    missing = [path for path in required if f"{prefix}{path}" not in payloads]
    if missing:
        raise ArtifactValidationError(f"artifact is missing required {label}: {', '.join(missing)}")


def _reject_wheel_source_only_tools(payloads: Mapping[str, bytes]) -> None:
    leaked = sorted(path for path in payloads if path.split("/", 1)[0].casefold() == "tools")
    if leaked:
        raise ArtifactValidationError(f"wheel contains source-only tool: {', '.join(leaked)}")


def _one_header(message: Message, name: str, label: str) -> str:
    values = message.get_all(name, [])
    if len(values) != 1 or not str(values[0]).strip():
        raise ArtifactValidationError(f"{label} requires one {name} header")
    return str(values[0]).strip()


def _metadata_identity(payload: bytes, label: str) -> tuple[str, str]:
    try:
        message = BytesParser(policy=policy.default).parsebytes(payload)
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError(f"{label} is invalid") from exc
    _one_header(message, "Metadata-Version", label)
    distribution = _one_header(message, "Name", label)
    version = _one_header(message, "Version", label)
    if not _VERSION.fullmatch(version):
        raise ArtifactValidationError(f"{label} Version is invalid")
    return distribution, version


def _wheel_filename(path: Path) -> tuple[str, str]:
    pattern = rf"{_WHEEL_DISTRIBUTION}-(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_]*)-py3-none-any\.whl"
    match = re.fullmatch(pattern, path.name, re.IGNORECASE)
    if match is None:
        raise ArtifactValidationError("wheel filename is invalid")
    return _WHEEL_DISTRIBUTION, match.group("version")


def _wheel_headers(payload: bytes) -> None:
    message = BytesParser(policy=policy.default).parsebytes(payload)
    if _one_header(message, "Wheel-Version", "WHEEL") != "1.0":
        raise ArtifactValidationError("WHEEL version is unsupported")
    if _one_header(message, "Root-Is-Purelib", "WHEEL").casefold() != "true":
        raise ArtifactValidationError("WHEEL must be pure Python")
    tags = tuple(str(value).strip() for value in message.get_all("Tag", []))
    if tags != ("py3-none-any",):
        raise ArtifactValidationError("WHEEL tags are not canonical")


class _CaseSensitiveConfigParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _entry_points(payload: bytes) -> tuple[str, ...]:
    parser = _CaseSensitiveConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(payload.decode("utf-8"))
    except (UnicodeDecodeError, configparser.Error) as exc:
        raise ArtifactValidationError("entry_points.txt is invalid") from exc
    command, target = _ENTRY_POINT
    if parser.sections() != ["console_scripts"] or dict(parser.items("console_scripts")) != {
        command: target
    }:
        raise ArtifactValidationError("wheel console entrypoint is invalid")
    return (f"{command} = {target}",)


def _record_rows(payload: bytes, limits: ArchiveLimits) -> dict[str, tuple[str, str]]:
    try:
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8"), newline="")))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ArtifactValidationError("wheel RECORD is invalid") from exc
    result: dict[str, tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3 or row[0] in result or len(row[0]) > limits.max_path_length:
            raise ArtifactValidationError("wheel RECORD is invalid")
        result[row[0]] = (row[1], row[2])
    return result


def _validate_record(
    payloads: Mapping[str, bytes], record_path: str, limits: ArchiveLimits
) -> None:
    rows = _record_rows(payloads[record_path], limits)
    if set(rows) != set(payloads):
        raise ArtifactValidationError("wheel RECORD member set is inconsistent")
    for path, payload in payloads.items():
        digest_text, size_text = rows[path]
        if path == record_path:
            if digest_text or size_text:
                raise ArtifactValidationError("wheel RECORD self row must be unhashed")
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
        if digest_text != f"sha256={digest}":
            raise ArtifactValidationError(f"wheel RECORD hash mismatch: {path}")
        if size_text != str(len(payload)):
            raise ArtifactValidationError(f"wheel RECORD size mismatch: {path}")


def _typed_packages(payloads: Mapping[str, bytes], root: str = "") -> tuple[str, ...]:
    prefix = f"{root}/" if root else ""
    missing = [
        package for package in _TYPED_PACKAGES if f"{prefix}{package}/py.typed" not in payloads
    ]
    if missing:
        raise ArtifactValidationError(f"artifact is missing py.typed: {', '.join(missing)}")
    return tuple(sorted(_TYPED_PACKAGES))


def _wheel_dist_info(payloads: Mapping[str, bytes]) -> str:
    roots = {
        path.split("/", 1)[0] for path in payloads if path.split("/", 1)[0].endswith(".dist-info")
    }
    if len(roots) != 1:
        raise ArtifactValidationError("wheel must contain exactly one dist-info root")
    return next(iter(roots))


def _validate_wheel_scan(
    scan: _Scan, expected_version: str | None, limits: ArchiveLimits
) -> ArchiveInspection:
    if scan.report.kind != "zip":
        raise ArtifactValidationError("wheel must use the ZIP container")
    filename_distribution, filename_version = _wheel_filename(scan.report.path)
    _reject_wheel_source_only_tools(scan.payloads)
    _required_members(scan.payloads, _UI_ASSETS, "UI asset")
    dist_info = _wheel_dist_info(scan.payloads)
    expected_dist_info = f"{filename_distribution}-{filename_version}.dist-info"
    if dist_info.casefold() != expected_dist_info.casefold():
        raise ArtifactValidationError("wheel filename metadata is inconsistent")
    metadata = _required(scan.payloads, f"{dist_info}/METADATA", "METADATA")
    wheel = _required(scan.payloads, f"{dist_info}/WHEEL", "WHEEL")
    entry_points = _required(scan.payloads, f"{dist_info}/entry_points.txt", "entry_points.txt")
    record_path = f"{dist_info}/RECORD"
    _required(scan.payloads, record_path, "RECORD")
    distribution, version = _metadata_identity(metadata, "METADATA")
    if _canonical_distribution(distribution) != _DISTRIBUTION or version != filename_version:
        raise ArtifactValidationError("wheel filename metadata is inconsistent")
    if expected_version is not None and version != expected_version:
        raise ArtifactValidationError("wheel version is unexpected")
    _wheel_headers(wheel)
    entries = _entry_points(entry_points)
    typed = _typed_packages(scan.payloads)
    _validate_record(scan.payloads, record_path, limits)
    return replace(
        scan.report,
        kind="wheel",
        distribution=distribution,
        version=version,
        entry_points=entries,
        typed_packages=typed,
        record_verified=True,
    )


def validate_wheel(
    path: str | Path,
    *,
    expected_version: str | None = None,
    limits: ArchiveLimits = DEFAULT_LIMITS,
) -> ArchiveInspection:
    """Validate wheel metadata, entrypoint, typing markers, hashes and sizes."""

    scan = _scan_archive(path, limits)
    return _validate_wheel_scan(scan, expected_version, limits)


def _validate_sdist_filename(scan: _Scan, version: str) -> None:
    expected_name = f"{_WHEEL_DISTRIBUTION}-{version}.tar.gz"
    if scan.report.path.name.casefold() != expected_name.casefold():
        raise ArtifactValidationError("sdist filename metadata is inconsistent")


def _validate_sdist_scan(
    scan: _Scan,
    expected_version: str | None,
    *,
    check_filename: bool = True,
) -> ArchiveInspection:
    if scan.report.kind != "tar":
        raise ArtifactValidationError("sdist must use the TAR container")
    roots = {path.split("/", 1)[0] for path in scan.all_paths}
    if len(roots) != 1:
        raise ArtifactValidationError("sdist must contain a single root")
    root = next(iter(roots))
    try:
        pkg_info = scan.payloads[f"{root}/PKG-INFO"]
    except KeyError as exc:
        raise ArtifactValidationError("sdist is missing root PKG-INFO") from exc
    distribution, version = _metadata_identity(pkg_info, "PKG-INFO")
    root_distribution, separator, root_version = root.rpartition("-")
    if (
        not separator
        or _canonical_distribution(root_distribution) != _DISTRIBUTION
        or root_version != version
    ):
        raise ArtifactValidationError("sdist root metadata is inconsistent")
    if _canonical_distribution(distribution) != _DISTRIBUTION:
        raise ArtifactValidationError("sdist distribution is unexpected")
    if check_filename:
        _validate_sdist_filename(scan, version)
    if expected_version is not None and version != expected_version:
        raise ArtifactValidationError("sdist version is unexpected")
    missing = [path for path in _REQUIRED_SDIST_CONTENT if f"{root}/{path}" not in scan.payloads]
    if missing:
        raise ArtifactValidationError(
            f"sdist is missing required sdist content: {', '.join(missing)}"
        )
    _required_members(scan.payloads, _UI_ASSETS, "UI asset", root=root)
    _required_members(
        scan.payloads,
        _SOURCE_ONLY_TOOLS,
        "source-only tool",
        root=root,
    )
    typed = _typed_packages(scan.payloads, root)
    return replace(
        scan.report,
        kind="sdist",
        root=root,
        distribution=distribution,
        version=version,
        typed_packages=typed,
    )


def validate_sdist(
    path: str | Path,
    *,
    expected_version: str | None = None,
    limits: ArchiveLimits = DEFAULT_LIMITS,
) -> ArchiveInspection:
    """Validate one bounded TAR source distribution."""

    return _validate_sdist_scan(_scan_archive(path, limits), expected_version)


def validate_release_artifact(
    path: str | Path,
    *,
    expected_version: str | None = None,
    limits: ArchiveLimits = DEFAULT_LIMITS,
) -> ArchiveInspection:
    """Dispatch strict wheel or sdist validation."""

    name = Path(path).name.casefold()
    if name.endswith(".whl"):
        return validate_wheel(path, expected_version=expected_version, limits=limits)
    if name.endswith(".tar.gz"):
        return validate_sdist(path, expected_version=expected_version, limits=limits)
    raise ArtifactValidationError(f"unsupported release artifact: {Path(path).name}")


def logical_payload(inspection: ArchiveInspection) -> LogicalPayload:
    """Return container-independent file identities."""

    if inspection.kind == "wheel":
        kind: Literal["wheel", "sdist"] = "wheel"
    elif inspection.kind == "sdist":
        kind = "sdist"
    else:
        raise ArtifactValidationError("logical payload requires a validated release artifact")
    members: list[ArchiveMember] = []
    for member in inspection.members:
        path = member.path
        if kind == "wheel" and path.endswith(".dist-info/RECORD"):
            continue
        if kind == "sdist":
            prefix = f"{inspection.root}/"
            if inspection.root is None or not path.startswith(prefix):
                raise ArtifactValidationError("sdist member escapes its validated root")
            path = path[len(prefix) :]
        members.append(ArchiveMember(path, member.size, member.sha256))
    ordered = tuple(sorted(members, key=lambda member: member.path))
    digest = hashlib.sha256(kind.encode("ascii") + b"\x00")
    for member in ordered:
        path_bytes = member.path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(4, "big") + path_bytes)
        digest.update(member.size.to_bytes(8, "big") + bytes.fromhex(member.sha256))
    return LogicalPayload(kind, ordered, digest.hexdigest())


def _validated_artifact(
    value: str | Path | ArchiveInspection,
    limits: ArchiveLimits,
) -> ArchiveInspection:
    if not isinstance(value, ArchiveInspection):
        return validate_release_artifact(value, limits=limits)
    fresh = validate_release_artifact(value.path, limits=limits)
    if fresh != value:
        raise ArtifactValidationError("artifact inspection is stale or forged")
    return fresh


def compare_logical_payloads(
    left: str | Path | ArchiveInspection,
    right: str | Path | ArchiveInspection,
    *,
    limits: ArchiveLimits = DEFAULT_LIMITS,
) -> LogicalPayload:
    """Require two independently validated artifacts to have equal payloads."""

    left_payload = logical_payload(_validated_artifact(left, limits))
    right_payload = logical_payload(_validated_artifact(right, limits))
    if left_payload != right_payload:
        raise ArtifactValidationError("release artifact logical payload mismatch")
    return left_payload


def _epoch(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("source_date_epoch must be an integer")
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("source_date_epoch is outside the gzip timestamp range")
    return value


def _write_canonical_tar(stream: BinaryIO, payloads: Mapping[str, bytes], epoch: int) -> None:
    with gzip.GzipFile(
        filename="", mode="wb", compresslevel=9, fileobj=stream, mtime=epoch
    ) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path in sorted(payloads):
                payload = payloads[path]
                info = tarfile.TarInfo(path)
                info.size = len(payload)
                info.mtime = epoch
                info.mode = 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.pax_headers = {}
                archive.addfile(info, io.BytesIO(payload))


def _safe_unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def canonicalize_sdist(
    source: str | Path,
    reference: str | Path,
    destination: str | Path,
    *,
    source_date_epoch: int = SOURCE_DATE_EPOCH,
    limits: ArchiveLimits = DEFAULT_LIMITS,
) -> ArchiveInspection:
    """Publish a deterministic sdist only after two distinct builds compare equal."""

    epoch = _epoch(source_date_epoch)
    source_report = validate_sdist(source, limits=limits)
    reference_report = validate_sdist(reference, limits=limits)
    if os.path.samefile(source_report.path, reference_report.path):
        raise ArtifactValidationError("sdist comparison requires two distinct builds")
    expected = compare_logical_payloads(source_report, reference_report, limits=limits)
    source_scan = _scan_archive(source_report.path, limits)
    rescanned = _validate_sdist_scan(source_scan, source_report.version)
    if (
        rescanned.archive_sha256 != source_report.archive_sha256
        or logical_payload(rescanned) != expected
    ):
        raise ArtifactValidationError("source sdist changed after logical comparison")
    output = Path(destination).resolve(strict=False)
    if output.exists():
        raise ArtifactValidationError(f"canonical destination already exists: {output}")
    expected_name = f"{_WHEEL_DISTRIBUTION}-{source_report.version}.tar.gz"
    if not output.parent.is_dir() or output.name.casefold() != expected_name.casefold():
        raise ArtifactValidationError("canonical sdist destination is invalid")
    candidate: Path | None = None
    published = False
    try:
        descriptor, raw_candidate = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tar.gz", dir=output.parent
        )
        candidate = Path(raw_candidate)
        with os.fdopen(descriptor, "wb") as stream:
            _write_canonical_tar(stream, source_scan.payloads, epoch)
        candidate_report = _validate_sdist_scan(
            _scan_archive(candidate, limits),
            source_report.version,
            check_filename=False,
        )
        if logical_payload(candidate_report) != expected:
            raise ArtifactValidationError("canonical sdist logical payload changed")
        os.link(candidate, output)
        published = True
        result = validate_sdist(output, expected_version=source_report.version, limits=limits)
        if logical_payload(result) != expected:
            raise ArtifactValidationError("published sdist logical payload changed")
    except BaseException:
        if published:
            _safe_unlink(output)
        _safe_unlink(candidate)
        raise
    _safe_unlink(candidate)
    return result


# This order is a characterized public compatibility contract.
__all__ = [  # noqa: RUF022
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
# endregion [02]
