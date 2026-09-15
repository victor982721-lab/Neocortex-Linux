"""Bounded Archive inventory and no-replace materialization service.

This module is intentionally independent from Framework and from KIO.  It
offers a virtual, read-only manifest by default and an explicit local
materialization operation for callers that already hold the physical-effect
gate.  The service never replaces an existing destination and never deletes a
source container.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
import zipfile
import zlib
from contextlib import contextmanager
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Literal, cast

from neocortex.platform.zip_safety import (
    DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
    ZipMemberStructure,
    ZipStructureError,
    inspect_zip_structure,
)

from .units import (
    ArchiveUnitClassification,
    DEFAULT_CLASSIFICATION_MAX_MEMBER_BYTES,
    DEFAULT_CLASSIFICATION_MAX_MEMBERS,
    DEFAULT_CLASSIFICATION_MAX_RATIO,
    DEFAULT_CLASSIFICATION_MAX_TOTAL_BYTES,
    classify_archive,
)


EntryStatus = Literal[
    "validated",
    "partial",
    "corrupt",
    "password",
    "permission",
    "dependency",
    "timeout",
    "budget",
]
MaterializationStatus = Literal[
    "virtual",
    "complete",
    "partial",
    "corrupt",
    "password",
    "permission",
    "dependency",
    "timeout",
    "budget",
    "collision",
]

DEFAULT_MAX_ARCHIVE_INPUT_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_TEMP_BYTES = 512 * 1024 * 1024
ARCHIVE_STAGE_CHUNK_BYTES = 64 * 1024
REGISTERED_SCRATCH_OWNER = "archive-materialization"


@dataclass(frozen=True, slots=True)
class ArchiveMaterializationLimits:
    """Independent bounds for archive inventory and materialization."""

    max_members: int = DEFAULT_CLASSIFICATION_MAX_MEMBERS
    max_member_bytes: int = DEFAULT_CLASSIFICATION_MAX_MEMBER_BYTES
    max_total_uncompressed_bytes: int = DEFAULT_CLASSIFICATION_MAX_TOTAL_BYTES
    max_total_temp_bytes: int = DEFAULT_MAX_TEMP_BYTES
    max_input_bytes: int = DEFAULT_MAX_ARCHIVE_INPUT_BYTES
    max_depth: int = 5
    max_compression_ratio: float = DEFAULT_CLASSIFICATION_MAX_RATIO
    max_central_directory_bytes: int = DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES
    timeout_seconds: float = 60.0

    def validate(self) -> None:
        values = {
            "max_members": self.max_members,
            "max_member_bytes": self.max_member_bytes,
            "max_total_uncompressed_bytes": self.max_total_uncompressed_bytes,
            "max_total_temp_bytes": self.max_total_temp_bytes,
            "max_input_bytes": self.max_input_bytes,
            "max_depth": self.max_depth,
            "max_central_directory_bytes": self.max_central_directory_bytes,
        }
        for name, value in values.items():
            if type(value) is not int or value < 1:
                raise ValueError(f"archive {name} must be a positive integer")
        if not isinstance(self.max_compression_ratio, (int, float)) or self.max_compression_ratio <= 0:
            raise ValueError("archive max_compression_ratio must be positive")
        if not isinstance(self.timeout_seconds, (int, float)) or self.timeout_seconds <= 0:
            raise ValueError("archive timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    """One entry in a virtual archive manifest.

    ``identity`` is derived from the source snapshot, chain, central-directory
    ordinal and physical local-header offset.  It remains unique when two ZIP
    entries have the same filename, including when their bytes differ.
    """

    identity: str
    ordinal: int
    header_offset: int
    chain: str
    name: str
    original_name: str
    depth: int
    declared_size: int
    compressed_size: int
    expected_crc32: int
    actual_size: int | None
    actual_crc32: int | None
    sha256: str | None
    status: EntryStatus
    content_kind: str
    unit_kind: str | None = None
    error_code: str | None = None
    detail: str | None = None
    output_relative_path: str | None = None
    output_status: str | None = None

    @property
    def member_chain(self) -> str:
        """Compatibility name used by Archive query consumers."""

        return self.chain

    @property
    def header_boundary(self) -> int | None:
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "ordinal": self.ordinal,
            "header_offset": self.header_offset,
            "chain": self.chain,
            "member_chain": self.chain,
            "name": self.name,
            "original_name": self.original_name,
            "depth": self.depth,
            "declared_size": self.declared_size,
            "compressed_size": self.compressed_size,
            "expected_crc32": self.expected_crc32,
            "actual_size": self.actual_size,
            "actual_crc32": self.actual_crc32,
            "sha256": self.sha256,
            "status": self.status,
            "content_kind": self.content_kind,
            "unit_kind": self.unit_kind,
            "error_code": self.error_code,
            "detail": self.detail,
            "output_relative_path": self.output_relative_path,
            "output_status": self.output_status,
        }


@dataclass(frozen=True, slots=True)
class ArchiveMaterializedOutput:
    entry_identity: str
    relative_path: str
    absolute_path: str
    status: Literal["applied", "reused", "collision", "skipped"]
    sha256: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "entry_identity": self.entry_identity,
            "relative_path": self.relative_path,
            "absolute_path": self.absolute_path,
            "status": self.status,
            "sha256": self.sha256,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    source_path: str
    source_size: int
    source_sha256: str | None
    source_mtime_ns: int | None
    classification: ArchiveUnitClassification
    entries: tuple[ArchiveEntry, ...]
    status: MaterializationStatus
    apply: bool = False
    destination: str | None = None
    outputs: tuple[ArchiveMaterializedOutput, ...] = ()
    errors: tuple[str, ...] = ()
    manifest_digest: str = ""

    @property
    def virtual(self) -> bool:
        return not self.apply

    @property
    def complete(self) -> bool:
        return self.status == "complete"

    @property
    def container_normalized(self) -> bool:
        """Whether a caller may consider this generic container removable.

        The service itself never removes the source.  This predicate is an
        evidence gate for an owner that may later perform a separately
        authorised effect: every member must be validated and either published
        or intentionally traversed as a generic nested container.  Functional
        packages/projects are preserved as one unit and therefore are *not*
        reported as normalized here.
        """

        if not self.apply or self.status != "complete":
            return False
        if self.classification.preserve_as_unit:
            return False
        if any(entry.status != "validated" for entry in self.entries):
            return False
        outputs_by_identity = {output.entry_identity: output for output in self.outputs}
        for entry in (entry for entry in self.entries if entry.status == "validated"):
            output = outputs_by_identity.get(entry.identity)
            if output is None:
                return False
            if output.status in {"applied", "reused"}:
                continue
            if (
                output.status == "skipped"
                and entry.content_kind == "storage_archive"
                and any(other.chain.startswith(entry.chain + "!/") for other in self.entries)
            ):
                continue
            return False
        return True

    @property
    def normalization_disposition(self) -> str:
        """Explain why a source container is or is not removable."""

        if self.container_normalized:
            return "container_normalized"
        if not self.apply:
            return "virtual_only"
        if self.classification.preserve_as_unit and self.status == "complete":
            return "functional_unit_preserved"
        return "pending"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "neocortex.archive-manifest/v1",
            "source_path": self.source_path,
            "source_size": self.source_size,
            "source_sha256": self.source_sha256,
            "source_mtime_ns": self.source_mtime_ns,
            "classification": self.classification.to_dict(),
            "entries": [entry.to_dict() for entry in self.entries],
            "status": self.status,
            "apply": self.apply,
            "virtual": self.virtual,
            "container_normalized": self.container_normalized,
            "normalization_disposition": self.normalization_disposition,
            "destination": self.destination,
            "outputs": [output.to_dict() for output in self.outputs],
            "errors": list(self.errors),
            "manifest_digest": self.manifest_digest,
        }


@dataclass(slots=True)
class _ScanBudget:
    members: int = 0
    uncompressed_bytes: int = 0
    temp_bytes: int = 0
    started: float = 0.0


class ArchiveMaterializationError(ValueError):
    """Invalid caller input, never a permission to change an original."""


ManifestHook = Callable[[ArchiveManifest], None]
JournalHook = Callable[[dict[str, object]], None]


def _scratch_failure_reason(error: BaseException) -> str:
    """Return a bounded failure reason suitable for a scratch manifest."""

    reason = f"{type(error).__name__}: {error}".replace("\x00", "\\0")
    if not reason:
        reason = "archive materialization failed"
    encoded = reason.encode("utf-8")
    if len(encoded) <= 16 * 1024:
        return reason
    return encoded[: 16 * 1024 - 3].decode("utf-8", "ignore") + "..."


def _fail_registered_workspace(workspace: object, error: BaseException) -> None:
    """Transition a created workspace to retained failure."""

    fail = getattr(workspace, "fail", None)
    if not callable(fail):
        raise TypeError("registered Archive scratch workspace has no fail() transition")
    fail(_scratch_failure_reason(error))


@contextmanager
def _registered_scratch_workspace(
    root: Path,
    *,
    metadata: Mapping[str, object] | None = None,
) -> Iterator[Path]:
    """Yield one runtime-owned workspace for archive staging.

    The runtime ``ScratchManager`` is deliberately imported lazily so virtual
    archive inventory remains usable without importing the lifecycle service.
    A successful operation retires its workspace through the runtime owner;
    any failure is retained for bounded recovery inspection.  There is no
    unregistered cleanup fallback: losing the lifecycle transition must not
    silently erase evidence of a failed materialization.
    """

    try:
        from neocortex.runtime import scratch as _runtime_scratch
    except (ImportError, ModuleNotFoundError) as error:
        raise ArchiveMaterializationError(
            "registered Archive scratch service is unavailable"
        ) from error

    manager_type = getattr(_runtime_scratch, "ScratchManager", None)
    if not callable(manager_type):
        raise ArchiveMaterializationError(
            "registered Archive scratch service has no ScratchManager"
        )
    manager = manager_type(
        root,
        owner=REGISTERED_SCRATCH_OWNER,
        create_root=True,
    )
    create = getattr(manager, "create", None)
    if not callable(create):
        create = getattr(manager, "create_workspace", None)
    if not callable(create):
        raise ArchiveMaterializationError(
            "registered Archive scratch service has no workspace creator"
        )
    workspace = create(
        run_id=None,
        retain_on_success=False,
        metadata={} if metadata is None else dict(metadata),
    )
    path = getattr(workspace, "path", None)
    if not isinstance(path, Path):
        raise ArchiveMaterializationError(
            "registered Archive scratch workspace has no Path path"
        )

    try:
        yield path
    except BaseException as error:
        # Never let a secondary lifecycle failure replace the extraction or
        # publication error that the caller needs to diagnose.
        try:
            _fail_registered_workspace(workspace, error)
        except BaseException:
            pass
        raise
    else:
        complete = getattr(workspace, "complete", None)
        if not callable(complete):
            raise ArchiveMaterializationError(
                "registered Archive scratch workspace has no complete() transition"
            )
        complete()


def _check_deadline(budget: _ScanBudget, limits: ArchiveMaterializationLimits) -> None:
    if time.monotonic() - budget.started > limits.timeout_seconds:
        raise TimeoutError("archive materialization deadline exceeded")


def _safe_name(name: str) -> tuple[bool, str]:
    normalized = name.replace("\\", "/")
    if "\x00" in normalized or normalized.startswith("/"):
        return False, normalized
    first = normalized.split("/", 1)[0]
    if len(first) == 2 and first[1] == ":" and first[0].isalpha():
        return False, normalized
    trimmed = normalized.rstrip("/")
    parts = trimmed.split("/") if trimmed else []
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return False, normalized
    return True, "/".join(parts) + ("/" if normalized.endswith("/") else "")


def _entry_identity(source_sha256: str | None, chain: str, ordinal: int, header_offset: int) -> str:
    payload = json.dumps(
        {
            "source_sha256": source_sha256,
            "chain": chain,
            "ordinal": ordinal,
            "header_offset": header_offset,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", "surrogatepass")
    return "archive-entry:" + hashlib.sha256(payload).hexdigest()


def _error_entry(
    *,
    source_sha256: str | None,
    structure: ZipMemberStructure,
    chain: str,
    name: str,
    original_name: str,
    depth: int,
    status: EntryStatus,
    content_kind: str = "binary",
    unit_kind: str | None = None,
    error_code: str | None = None,
    detail: str | None = None,
) -> ArchiveEntry:
    return ArchiveEntry(
        identity=_entry_identity(source_sha256, chain, structure.ordinal, structure.header_offset),
        ordinal=structure.ordinal,
        header_offset=structure.header_offset,
        chain=chain,
        name=name,
        original_name=original_name,
        depth=depth,
        declared_size=structure.uncompressed_size,
        compressed_size=structure.compressed_size,
        expected_crc32=structure.crc32,
        actual_size=None,
        actual_crc32=None,
        sha256=None,
        status=status,
        content_kind=content_kind,
        unit_kind=unit_kind,
        error_code=error_code,
        detail=None if detail is None else detail[:2_000],
    )


def _content_kind(name: str) -> str:
    suffix = PurePosixPath(name).suffix.casefold()
    return suffix.removeprefix(".") or "binary"


def _stream_to_stage(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    stage: Path,
    *,
    budget: _ScanBudget,
    limits: ArchiveMaterializationLimits,
) -> tuple[int, int, str]:
    if info.flag_bits & 0x1:
        raise PermissionError("encrypted ZIP member requires a password")
    if int(info.file_size) > limits.max_member_bytes:
        raise MemoryError("member exceeds max_member_bytes")
    if int(info.file_size) > limits.max_total_uncompressed_bytes - budget.uncompressed_bytes:
        raise MemoryError("archive total uncompressed budget exhausted")
    if info.file_size and float(info.file_size) / float(max(1, info.compress_size)) > limits.max_compression_ratio:
        raise MemoryError("member compression ratio exceeds safety bound")
    if info.compress_type not in {
        zipfile.ZIP_STORED,
        zipfile.ZIP_DEFLATED,
        zipfile.ZIP_BZIP2,
        zipfile.ZIP_LZMA,
    }:
        raise NotImplementedError(f"unsupported ZIP compression method {info.compress_type}")
    if int(info.file_size) > limits.max_total_temp_bytes - budget.temp_bytes:
        raise MemoryError("archive temporary-space budget exhausted")
    stage.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    crc = 0
    actual = 0
    try:
        with archive.open(info) as source, stage.open("wb") as output:
            while chunk := source.read(ARCHIVE_STAGE_CHUNK_BYTES):
                _check_deadline(budget, limits)
                actual += len(chunk)
                if actual > limits.max_member_bytes:
                    raise MemoryError("member output exceeds max_member_bytes")
                if actual > limits.max_total_uncompressed_bytes - budget.uncompressed_bytes:
                    raise MemoryError("archive total uncompressed budget exhausted")
                if actual > limits.max_total_temp_bytes - budget.temp_bytes:
                    raise MemoryError("archive temporary-space budget exhausted")
                output.write(chunk)
                digest.update(chunk)
                crc = zlib.crc32(chunk, crc)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        try:
            stage.unlink()
        except OSError:
            pass
        raise
    if actual != int(info.file_size):
        raise zipfile.BadZipFile(
            f"member produced {actual} bytes but declares {info.file_size}"
        )
    crc &= 0xFFFFFFFF
    if crc != int(info.CRC):
        raise zipfile.BadZipFile(
            f"member CRC mismatch: expected {int(info.CRC):08x}, got {crc:08x}"
        )
    budget.uncompressed_bytes += actual
    budget.temp_bytes += actual
    return actual, crc, digest.hexdigest()


def _status_for_exception(exc: BaseException) -> tuple[EntryStatus, str, str]:
    message = str(exc)[:2_000]
    lower = message.casefold()
    if isinstance(exc, TimeoutError):
        return "timeout", "archive_timeout", message
    if isinstance(exc, MemoryError):
        return "budget", "archive_budget_exhausted", message
    if isinstance(exc, PermissionError):
        if "encrypt" in lower or "password" in lower:
            return "password", "archive_password_required", message
        return "permission", "archive_permission_denied", message
    if isinstance(exc, NotImplementedError):
        return "dependency", "archive_decoder_unavailable", message
    if isinstance(exc, (zipfile.BadZipFile, zlib.error, ZipStructureError)):
        return "corrupt", "archive_member_corrupt", message
    if isinstance(exc, OSError):
        return "permission", "archive_io_error", message
    return "partial", "archive_member_unavailable", message


def _record_journal(hook: JournalHook | None, event: dict[str, object]) -> None:
    if hook is not None:
        hook(dict(event))


def _scan_zip(
    path: Path,
    *,
    prefix: str,
    depth: int,
    source_sha256: str | None,
    entries: list[ArchiveEntry],
    stage_paths: dict[str, Path],
    temp_root: Path,
    budget: _ScanBudget,
    limits: ArchiveMaterializationLimits,
    journal_hook: JournalHook | None,
) -> None:
    _check_deadline(budget, limits)
    if depth > limits.max_depth:
        return
    structure = inspect_zip_structure(
        path,
        max_members=min(limits.max_members, max(1, limits.max_members - budget.members)),
        max_central_directory_bytes=limits.max_central_directory_bytes,
    )
    if structure.members > limits.max_members - budget.members:
        raise MemoryError("archive member budget exhausted")
    with zipfile.ZipFile(path) as archive:
        infos = tuple(archive.infolist())
        if len(infos) != len(structure.entries):
            raise ZipStructureError("ZIP entries changed after structural preflight")
        for ordinal, info in enumerate(infos):
            _check_deadline(budget, limits)
            if budget.members >= limits.max_members:
                raise MemoryError("archive member budget exhausted")
            budget.members += 1
            structure_entry = structure.entries[ordinal]
            safe, normalized = _safe_name(info.filename)
            chain = f"{prefix}!/{normalized}" if prefix else normalized
            identity = _entry_identity(source_sha256, chain, ordinal, structure_entry.header_offset)
            if not safe:
                entry = _error_entry(
                    source_sha256=source_sha256,
                    structure=structure_entry,
                    chain=chain,
                    name=normalized,
                    original_name=info.filename,
                    depth=depth,
                    status="partial",
                    error_code="archive_unsafe_member_name",
                    detail="ZIP member name is absolute, traversing or not portable",
                )
                entries.append(entry)
                _record_journal(journal_hook, {"event": "entry", **entry.to_dict()})
                continue
            if info.is_dir():
                entry = ArchiveEntry(
                    identity=identity,
                    ordinal=ordinal,
                    header_offset=structure_entry.header_offset,
                    chain=chain,
                    name=normalized.rstrip("/"),
                    original_name=info.filename,
                    depth=depth,
                    declared_size=0,
                    compressed_size=int(info.compress_size),
                    expected_crc32=int(info.CRC),
                    actual_size=0,
                    actual_crc32=int(info.CRC),
                    sha256=hashlib.sha256(b"").hexdigest(),
                    status="validated",
                    content_kind="directory",
                )
                entries.append(entry)
                _record_journal(journal_hook, {"event": "entry", **entry.to_dict()})
                continue
            stage = temp_root / f"entry-{identity.removeprefix('archive-entry:')}"
            try:
                actual_size, actual_crc, digest = _stream_to_stage(
                    archive,
                    info,
                    stage,
                    budget=budget,
                    limits=limits,
                )
                stage_paths[identity] = stage
                content_kind = _content_kind(normalized)
                unit_kind: str | None = None
                nested = False
                nested_classification: ArchiveUnitClassification | None = None
                if actual_size >= 4:
                    with stage.open("rb") as sample:
                        magic = sample.read(8)
                    nested = magic.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")) or PurePosixPath(normalized).suffix.casefold() in {".zip", ".zipx", ".cbz"}
                if nested:
                    nested_classification = classify_archive(
                        stage,
                        max_members=min(limits.max_members, max(1, limits.max_members - budget.members)),
                        max_member_bytes=limits.max_member_bytes,
                        max_total_bytes=min(
                            limits.max_total_uncompressed_bytes,
                            limits.max_total_uncompressed_bytes - budget.uncompressed_bytes,
                        ),
                        max_compression_ratio=limits.max_compression_ratio,
                        max_central_directory_bytes=limits.max_central_directory_bytes,
                    )
                    if nested_classification.preserve_as_unit and nested_classification.status == "validated":
                        unit_kind = nested_classification.unit_kind
                        content_kind = nested_classification.kind
                    elif nested_classification.status == "corrupt":
                        raise zipfile.BadZipFile(nested_classification.detail or "nested ZIP is corrupt")
                    elif nested_classification.status in {"password", "permission", "dependency", "timeout", "budget"}:
                        status = cast(EntryStatus, nested_classification.status)
                        raise ArchiveMaterializationError(
                            f"{status}: {nested_classification.detail or status}"
                        )
                    else:
                        content_kind = "storage_archive"
                entry = ArchiveEntry(
                    identity=identity,
                    ordinal=ordinal,
                    header_offset=structure_entry.header_offset,
                    chain=chain,
                    name=normalized,
                    original_name=info.filename,
                    depth=depth,
                    declared_size=int(info.file_size),
                    compressed_size=int(info.compress_size),
                    expected_crc32=int(info.CRC),
                    actual_size=actual_size,
                    actual_crc32=actual_crc,
                    sha256=digest,
                    status="validated",
                    content_kind=content_kind,
                    unit_kind=unit_kind,
                )
                entries.append(entry)
                _record_journal(journal_hook, {"event": "entry", **entry.to_dict()})
                if nested and unit_kind is None and depth < limits.max_depth:
                    entry_index = len(entries) - 1
                    try:
                        _scan_zip(
                            stage,
                            prefix=chain,
                            depth=depth + 1,
                            source_sha256=source_sha256,
                            entries=entries,
                            stage_paths=stage_paths,
                            temp_root=temp_root,
                            budget=budget,
                            limits=limits,
                            journal_hook=journal_hook,
                        )
                    except BaseException as nested_exc:
                        nested_status, nested_code, nested_detail = _status_for_exception(
                            nested_exc
                        )
                        entries[entry_index] = replace(
                            entries[entry_index],
                            status=nested_status,
                            error_code=nested_code,
                            detail=nested_detail,
                        )
                        _record_journal(
                            journal_hook,
                            {
                                "event": "entry_update",
                                **entries[entry_index].to_dict(),
                            },
                        )
                elif nested and unit_kind is None and depth >= limits.max_depth:
                    # The container itself remains valid, but the manifest is
                    # partial because its descendants were not accounted for.
                    entries[-1] = replace(
                        entries[-1],
                        status="partial",
                        error_code="archive_depth_limit",
                        detail=f"nested ZIP depth exceeds {limits.max_depth}",
                    )
            except BaseException as exc:
                status, code, detail = _status_for_exception(exc)
                entry = _error_entry(
                    source_sha256=source_sha256,
                    structure=structure_entry,
                    chain=chain,
                    name=normalized,
                    original_name=info.filename,
                    depth=depth,
                    status=status,
                    content_kind="storage_archive" if normalized.casefold().endswith((".zip", ".zipx", ".cbz")) else _content_kind(normalized),
                    error_code=code,
                    detail=detail,
                )
                entries.append(entry)
                _record_journal(journal_hook, {"event": "entry", **entry.to_dict()})


def _hash_file(path: Path, *, max_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb", buffering=0) as source:
        while chunk := source.read(ARCHIVE_STAGE_CHUNK_BYTES):
            size += len(chunk)
            if size > max_bytes:
                raise MemoryError("source exceeds max_input_bytes")
            digest.update(chunk)
    return size, digest.hexdigest()


def _digest_manifest(manifest: ArchiveManifest) -> str:
    payload = manifest.to_dict()
    payload["manifest_digest"] = ""
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _status_for_manifest(classification: ArchiveUnitClassification, entries: tuple[ArchiveEntry, ...]) -> MaterializationStatus:
    if not entries:
        if classification.status == "password":
            return "password"
        if classification.status == "permission":
            return "permission"
        if classification.status == "dependency":
            return "dependency"
        if classification.status == "timeout":
            return "timeout"
        if classification.status == "budget":
            return "budget"
        if classification.status == "corrupt":
            return "corrupt"
    if any(entry.status == "timeout" for entry in entries):
        return "timeout"
    if any(entry.status == "password" for entry in entries):
        return "password"
    if any(entry.status == "permission" for entry in entries):
        return "permission"
    if any(entry.status == "dependency" for entry in entries):
        return "dependency"
    if any(entry.status == "budget" for entry in entries):
        return "budget"
    if any(entry.status == "corrupt" for entry in entries) or classification.status == "corrupt":
        return "corrupt" if not any(entry.status == "validated" for entry in entries) else "partial"
    if any(entry.status == "partial" for entry in entries) or classification.status == "partial":
        return "partial"
    return "complete"


def _scan_manifest(
    source: Path,
    *,
    limits: ArchiveMaterializationLimits,
    journal_hook: JournalHook | None,
    apply: bool,
    destination: Path | None,
    temp_root: Path,
) -> tuple[ArchiveManifest, dict[str, Path]]:
    limits.validate()
    try:
        source_stat = source.stat()
    except PermissionError as exc:
        classification = ArchiveUnitClassification("storage_archive", "permission", "storage_archive", detail=str(exc))
        manifest = ArchiveManifest(str(source), 0, None, None, classification, (), "permission", apply=apply, destination=None if destination is None else str(destination))
        return replace(manifest, manifest_digest=_digest_manifest(manifest)), {}
    except OSError as exc:
        classification = ArchiveUnitClassification("storage_archive", "permission", "storage_archive", detail=f"{type(exc).__name__}: {exc}")
        manifest = ArchiveManifest(str(source), 0, None, None, classification, (), "permission", apply=apply, destination=None if destination is None else str(destination))
        return replace(manifest, manifest_digest=_digest_manifest(manifest)), {}
    if not stat.S_ISREG(source_stat.st_mode):
        classification = ArchiveUnitClassification("storage_archive", "permission", "storage_archive", detail="source is not a regular file")
        manifest = ArchiveManifest(str(source), int(source_stat.st_size), None, int(source_stat.st_mtime_ns), classification, (), "permission", apply=apply, destination=None if destination is None else str(destination))
        return replace(manifest, manifest_digest=_digest_manifest(manifest)), {}
    budget = _ScanBudget(started=time.monotonic())
    try:
        source_size, source_sha256 = _hash_file(source, max_bytes=limits.max_input_bytes)
        classification = classify_archive(
            source,
            max_members=limits.max_members,
            max_member_bytes=limits.max_member_bytes,
            max_total_bytes=limits.max_total_uncompressed_bytes,
            max_compression_ratio=limits.max_compression_ratio,
            max_central_directory_bytes=limits.max_central_directory_bytes,
        )
    except MemoryError as exc:
        classification = ArchiveUnitClassification("storage_archive", "budget", "storage_archive", detail=str(exc))
        manifest = ArchiveManifest(str(source), int(source_stat.st_size), None, int(source_stat.st_mtime_ns), classification, (), "budget", apply=apply, destination=None if destination is None else str(destination), errors=(str(exc),))
        return replace(manifest, manifest_digest=_digest_manifest(manifest)), {}
    except PermissionError as exc:
        classification = ArchiveUnitClassification("storage_archive", "permission", "storage_archive", detail=str(exc))
        manifest = ArchiveManifest(str(source), source_size, source_sha256, int(source_stat.st_mtime_ns), classification, (), "permission", apply=apply, destination=None if destination is None else str(destination), errors=(str(exc),))
        return replace(manifest, manifest_digest=_digest_manifest(manifest)), {}
    entries: list[ArchiveEntry] = []
    stage_paths: dict[str, Path] = {}
    errors: list[str] = []
    try:
        _scan_zip(
            source,
            prefix="",
            depth=1,
            source_sha256=source_sha256,
            entries=entries,
            stage_paths=stage_paths,
            temp_root=temp_root,
            budget=budget,
            limits=limits,
            journal_hook=journal_hook,
        )
    except BaseException as exc:
        status, code, detail = _status_for_exception(exc)
        errors.append(f"{code}: {detail}")
        # A malformed root has no reliable entry identity.  Keep every member
        # already observed and expose the root failure separately.
        if not entries:
            classification = replace(classification, status=status, detail=detail)
    status = _status_for_manifest(classification, tuple(entries))
    manifest = ArchiveManifest(
        source_path=str(source),
        source_size=source_size,
        source_sha256=source_sha256,
        source_mtime_ns=int(source_stat.st_mtime_ns),
        classification=classification,
        entries=tuple(entries),
        status=status,
        apply=apply,
        destination=None if destination is None else str(destination),
        errors=tuple(errors),
    )
    return replace(manifest, manifest_digest=_digest_manifest(manifest)), stage_paths


def _relative_output_path(entry: ArchiveEntry, used: set[str]) -> str:
    segments = [segment for segment in entry.chain.split("!/") if segment]
    safe_segments: list[str] = []
    for segment in segments:
        valid, normalized = _safe_name(segment)
        if not valid:
            raise ArchiveMaterializationError("unsafe materialization path")
        safe_segments.extend(part for part in normalized.rstrip("/").split("/") if part)
    if not safe_segments:
        raise ArchiveMaterializationError("empty materialization path")
    candidate = PurePosixPath(*safe_segments)
    value = candidate.as_posix()
    if value in used:
        stem = candidate.stem
        suffix = candidate.suffix
        parent = candidate.parent
        value = (parent / f"{stem}.__entry_{entry.ordinal}{suffix}").as_posix()
        counter = 2
        while value in used:
            value = (parent / f"{stem}.__entry_{entry.ordinal}_{counter}{suffix}").as_posix()
            counter += 1
    used.add(value)
    return value


def _file_digest(path: Path, *, max_bytes: int) -> tuple[int, str]:
    return _hash_file(path, max_bytes=max_bytes)


def _discard_staging_paths(stage_paths: Mapping[str, Path]) -> None:
    """Drop successful-operation stage names before scratch retirement.

    Publication deliberately uses a no-replace hard link when possible.  The
    destination therefore shares the stage inode until the stage name is
    unlinked; leaving that name in a registered workspace would correctly be
    treated by ``ScratchManager`` as a hard-linked payload and would block
    retirement.  Unlinking removes only the private stage name and preserves
    the published destination bytes.
    """

    for stage in stage_paths.values():
        stage.unlink(missing_ok=True)


def _publish_no_replace(stage: Path, destination: Path, *, expected_size: int, expected_sha256: str, max_bytes: int) -> tuple[str, str | None]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(stage, destination)
    except FileExistsError:
        try:
            actual_size, actual_digest = _file_digest(destination, max_bytes=max_bytes)
        except OSError as exc:
            return "collision", str(exc)
        if actual_size == expected_size and actual_digest == expected_sha256:
            return "reused", None
        return "collision", "destination exists with different bytes"
    except OSError as exc:
        if exc.errno != getattr(os, "EXDEV", 18):
            if exc.errno == getattr(os, "EEXIST", 17):
                return "collision", "destination was created concurrently"
            # Cross-filesystem staging uses an O_EXCL copy, never replace.
            if exc.errno not in {getattr(os, "EPERM", 1), getattr(os, "EINVAL", 22)}:
                return "collision", f"cannot publish destination: {exc}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(destination, flags, 0o600)
        except FileExistsError:
            try:
                actual_size, actual_digest = _file_digest(destination, max_bytes=max_bytes)
            except OSError as read_exc:
                return "collision", str(read_exc)
            if actual_size == expected_size and actual_digest == expected_sha256:
                return "reused", None
            return "collision", "destination was created concurrently"
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as output, stage.open("rb") as source:
                shutil.copyfileobj(source, output, ARCHIVE_STAGE_CHUNK_BYTES)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            try:
                destination.unlink()
            except OSError:
                pass
            raise
    return "applied", None


def _apply_manifest(
    manifest: ArchiveManifest,
    stage_paths: dict[str, Path],
    *,
    destination: Path,
    limits: ArchiveMaterializationLimits,
    journal_hook: JournalHook | None,
) -> ArchiveManifest:
    destination.mkdir(parents=True, exist_ok=True)
    outputs: list[ArchiveMaterializedOutput] = []
    used: set[str] = set()
    updated_entries: list[ArchiveEntry] = []
    # Functional packages and detected projects remain one unit.  The source is
    # copied as a whole; member rows are retained for evidence but are not
    # promoted as independent files.
    if manifest.classification.preserve_as_unit and manifest.classification.status == "validated":
        relative = Path(manifest.source_path).name
        stage = destination.parent / f".{relative}.archive-stage-{manifest.manifest_digest[:16]}"
        try:
            with Path(manifest.source_path).open("rb") as source, stage.open("wb") as output:
                copied = 0
                digest = hashlib.sha256()
                while chunk := source.read(ARCHIVE_STAGE_CHUNK_BYTES):
                    copied += len(chunk)
                    if copied > limits.max_input_bytes:
                        raise MemoryError("source exceeds max_input_bytes")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            status, detail = _publish_no_replace(
                stage,
                destination / relative,
                expected_size=manifest.source_size,
                expected_sha256=manifest.source_sha256 or digest.hexdigest(),
                max_bytes=limits.max_input_bytes,
            )
            output_status: Literal["applied", "reused", "collision", "skipped"] = status  # type: ignore[assignment]
            outputs.append(ArchiveMaterializedOutput("archive-unit:" + manifest.manifest_digest, relative, str(destination / relative), output_status, manifest.source_sha256, detail))
            _record_journal(journal_hook, {"event": "materialization", "status": status, "relative_path": relative, "manifest_digest": manifest.manifest_digest, "detail": detail})
        finally:
            try:
                stage.unlink()
            except OSError:
                pass
        final_status: MaterializationStatus = manifest.status if not outputs or outputs[0].status == "collision" else ("partial" if manifest.status != "complete" else "complete")
        result = replace(manifest, status=final_status, apply=True, destination=str(destination), outputs=tuple(outputs), manifest_digest="")
        return replace(result, manifest_digest=_digest_manifest(result))
    for entry in manifest.entries:
        if entry.status != "validated":
            updated_entries.append(entry)
            outputs.append(ArchiveMaterializedOutput(entry.identity, entry.name, str(destination / entry.name), "skipped", entry.sha256, entry.detail))
            continue
        # A generic nested ZIP is a traversal node, not a leaf to copy beside
        # its descendants.  Omitting it also avoids a file/directory collision
        # for the deterministic ``nested.zip/<member>`` representation.
        if entry.content_kind == "storage_archive" and any(
            other.chain.startswith(entry.chain + "!/") for other in manifest.entries
        ):
            updated_entries.append(replace(entry, output_status="skipped", detail="nested storage container traversed"))
            outputs.append(
                ArchiveMaterializedOutput(
                    entry.identity,
                    entry.name,
                    str(destination / entry.name),
                    "skipped",
                    entry.sha256,
                    "nested storage container traversed",
                )
            )
            continue
        relative = _relative_output_path(entry, used)
        target = destination / Path(relative)
        if entry.content_kind == "directory":
            try:
                target.mkdir(parents=True, exist_ok=True)
                output = ArchiveMaterializedOutput(entry.identity, relative, str(target), "applied", entry.sha256)
                outputs.append(output)
                updated_entries.append(replace(entry, output_relative_path=relative, output_status="applied"))
                _record_journal(journal_hook, {"event": "materialization", **output.to_dict()})
            except OSError as exc:
                output = ArchiveMaterializedOutput(entry.identity, relative, str(target), "collision", entry.sha256, str(exc))
                outputs.append(output)
                updated_entries.append(replace(entry, output_relative_path=relative, output_status="collision", detail=str(exc)))
            continue
        stage = stage_paths.get(entry.identity)
        if stage is None or not stage.is_file():
            output = ArchiveMaterializedOutput(entry.identity, relative, str(target), "skipped", entry.sha256, "staged representation unavailable")
            outputs.append(output)
            updated_entries.append(replace(entry, output_relative_path=relative, output_status="skipped"))
            continue
        assert entry.actual_size is not None and entry.sha256 is not None
        status, detail = _publish_no_replace(stage, target, expected_size=entry.actual_size, expected_sha256=entry.sha256, max_bytes=limits.max_member_bytes)
        output_status = status  # type: ignore[assignment]
        output = ArchiveMaterializedOutput(entry.identity, relative, str(target), output_status, entry.sha256, detail)
        outputs.append(output)
        updated_entries.append(replace(entry, output_relative_path=relative, output_status=status))
        _record_journal(journal_hook, {"event": "materialization", **output.to_dict()})
    if any(output.status == "collision" for output in outputs):
        final_status: MaterializationStatus = "collision"
    else:
        entry_by_identity = {entry.identity: entry for entry in updated_entries}
        unexpected_skip = any(
            output.status == "skipped"
            and not (
                (entry := entry_by_identity.get(output.entry_identity)) is not None
                and entry.content_kind == "storage_archive"
                and any(
                    other.chain.startswith(entry.chain + "!/")
                    for other in manifest.entries
                )
            )
            for output in outputs
        )
        final_status = (
            "partial"
            if unexpected_skip
            else manifest.status
            if manifest.status != "complete"
            else "complete"
        )
    result = replace(manifest, entries=tuple(updated_entries), status=final_status, apply=True, destination=str(destination), outputs=tuple(outputs), manifest_digest="")
    return replace(result, manifest_digest=_digest_manifest(result))


def scan_archive(
    source: str | os.PathLike[str],
    *,
    limits: ArchiveMaterializationLimits | None = None,
    journal_hook: JournalHook | None = None,
    manifest_hook: ManifestHook | None = None,
) -> ArchiveManifest:
    """Return a bounded virtual manifest; no destination or source is changed."""

    effective = limits or ArchiveMaterializationLimits()
    source_path = Path(source)
    with tempfile.TemporaryDirectory(prefix="neocortex_archive_scan_") as directory:
        manifest, _stage_paths = _scan_manifest(
            source_path,
            limits=effective,
            journal_hook=journal_hook,
            apply=False,
            destination=None,
            temp_root=Path(directory),
        )
    if manifest_hook is not None:
        manifest_hook(manifest)
    return manifest


def materialize_archive(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    apply: bool = False,
    limits: ArchiveMaterializationLimits | None = None,
    journal_hook: JournalHook | None = None,
    manifest_hook: ManifestHook | None = None,
    scratch_directory: str | os.PathLike[str] | None = None,
) -> ArchiveManifest:
    """Scan and optionally materialize a ZIP using no-replace publication.

    ``apply=False`` is the default and is a pure virtual operation.  Setting
    ``apply=True`` only writes new files below ``destination``; it does not
    remove, replace or rename the source ZIP and does not call KIO/Framework.
    Apply staging is owned by a registered private ``ScratchManager``
    workspace.  ``scratch_directory`` may select its private manager root;
    otherwise a sibling ``.neocortex-archive-scratch`` root is used below the
    destination parent.
    """

    effective = limits or ArchiveMaterializationLimits()
    source_path = Path(source)
    destination_path = Path(destination)
    if not apply:
        return scan_archive(
            source_path,
            limits=effective,
            journal_hook=journal_hook,
            manifest_hook=manifest_hook,
        )
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    scratch_root = (
        Path(scratch_directory)
        if scratch_directory is not None
        else destination_path.parent.absolute() / ".neocortex-archive-scratch"
    )
    with _registered_scratch_workspace(
        scratch_root,
        metadata={
            "component": REGISTERED_SCRATCH_OWNER,
            "operation": "materialize_archive",
        },
    ) as directory:
        manifest, stage_paths = _scan_manifest(
            source_path,
            limits=effective,
            journal_hook=journal_hook,
            apply=True,
            destination=destination_path,
            temp_root=Path(directory),
        )
        result = _apply_manifest(
            manifest,
            stage_paths,
            destination=destination_path,
            limits=effective,
            journal_hook=journal_hook,
        )
        _discard_staging_paths(stage_paths)
    if manifest_hook is not None:
        manifest_hook(result)
    return result


# Friendly predicate for lifecycle owners.  It is intentionally read-only and
# does not remove the source or create a Framework action by itself.
def is_container_normalized(manifest: ArchiveManifest) -> bool:
    """Return the conservative removable-container gate for a final manifest."""

    if not isinstance(manifest, ArchiveManifest):
        raise TypeError("manifest must be an ArchiveManifest")
    return manifest.container_normalized


# Friendly aliases for callers that use inventory terminology.
inventory_archive = scan_archive
materialize_zip = materialize_archive


__all__ = (
    "ArchiveEntry",
    "ArchiveManifest",
    "ArchiveMaterializationError",
    "ArchiveMaterializationLimits",
    "ArchiveMaterializedOutput",
    "EntryStatus",
    "inventory_archive",
    "is_container_normalized",
    "materialize_archive",
    "materialize_zip",
    "scan_archive",
)
