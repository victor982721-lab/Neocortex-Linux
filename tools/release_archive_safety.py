"""Low-level bounded readers for offline release artifact inspection."""
# region [00] Contexto del módulo
# Módulo: tools/release_archive_safety.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

import gzip
import hashlib
import os
import stat
import struct
import tarfile
import unicodedata
import zipfile
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal, Protocol, cast

from neocortex.platform.zip_safety import (
    ZipStructure,
    ZipStructureError,
    inspect_zip_structure,
)
# endregion [01]

# region [02] Implementación


_CHUNK_BYTES = 1024 * 1024
_CENTRAL_FILE = struct.Struct("<4s6H3I5H2I")
_CENTRAL_FILE_SIGNATURE = b"PK\x01\x02"
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)

PathPolicy = Callable[[str], None]
PayloadPolicy = Callable[[str, bytes], None]


class _Readable(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class ArchiveSafetyError(ValueError):
    """An archive structure or read exceeded a fail-closed safety contract."""


class ArchiveBounds(Protocol):
    @property
    def max_archive_bytes(self) -> int: ...

    @property
    def max_members(self) -> int: ...

    @property
    def max_member_bytes(self) -> int: ...

    @property
    def max_total_bytes(self) -> int: ...

    @property
    def max_path_length(self) -> int: ...

    @property
    def max_path_depth(self) -> int: ...

    @property
    def max_compression_ratio(self) -> int: ...

    @property
    def max_central_directory_bytes(self) -> int: ...

    @property
    def max_tar_stream_bytes(self) -> int: ...


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class ScannedMember:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ScannedArchive:
    path: Path
    kind: Literal["zip", "tar"]
    identity: ArtifactIdentity
    archive_sha256: str
    members: tuple[ScannedMember, ...]
    all_paths: tuple[str, ...]
    payloads: Mapping[str, bytes]


def _identity(observed: os.stat_result) -> ArtifactIdentity:
    return ArtifactIdentity(
        int(observed.st_dev),
        int(observed.st_ino),
        int(observed.st_size),
        int(observed.st_mtime_ns),
    )


def _resolve_artifact(
    path: str | Path, bounds: ArchiveBounds
) -> tuple[Path, ArtifactIdentity]:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ArchiveSafetyError("artifact path cannot be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
        observed = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise ArchiveSafetyError(
            f"release artifact is unavailable: {candidate}"
        ) from exc
    if not stat.S_ISREG(observed.st_mode):
        raise ArchiveSafetyError(f"release artifact is not a file: {resolved}")
    identity = _identity(observed)
    if identity.size > bounds.max_archive_bytes:
        raise ArchiveSafetyError("archive size limit exceeded")
    return resolved, identity


def _require_identity(path: Path, expected: ArtifactIdentity) -> None:
    try:
        current = _identity(path.stat(follow_symlinks=False))
    except OSError as exc:
        raise ArchiveSafetyError("release artifact changed during inspection") from exc
    if current != expected:
        raise ArchiveSafetyError("release artifact changed during inspection")


def _hash_handle(stream: BinaryIO) -> str:
    stream.seek(0)
    digest = hashlib.sha256()
    while chunk := stream.read(_CHUNK_BYTES):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def _validate_segment(segment: str) -> None:
    if not segment or segment in {".", ".."}:
        raise ArchiveSafetyError("unsafe archive member path")
    if segment.endswith((" ", ".")) or any(ord(char) < 32 for char in segment):
        raise ArchiveSafetyError("unsafe archive member path")
    if segment.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES or ":" in segment:
        raise ArchiveSafetyError("unsafe archive member path")


def _normal_path(raw: str, directory: bool, bounds: ArchiveBounds) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise ArchiveSafetyError("unsafe archive member path")
    if len(raw) > bounds.max_path_length or raw.startswith(("/", "//")):
        raise ArchiveSafetyError("unsafe archive member path")
    normalized = raw[:-1] if directory and raw.endswith("/") else raw
    if not normalized or normalized.endswith("/"):
        raise ArchiveSafetyError("unsafe archive member path")
    parts = normalized.split("/")
    if len(parts) > bounds.max_path_depth:
        raise ArchiveSafetyError("unsafe archive member path")
    for part in parts:
        _validate_segment(part)
    if PurePosixPath(normalized).as_posix() != normalized:
        raise ArchiveSafetyError("unsafe archive member path")
    return normalized


class _PathRegistry:
    def __init__(self, bounds: ArchiveBounds, policy: PathPolicy) -> None:
        self._bounds = bounds
        self._policy = policy
        self._paths: dict[str, tuple[str, bool]] = {}

    def add(self, raw: str, directory: bool) -> str:
        path = _normal_path(raw, directory, self._bounds)
        folded = unicodedata.normalize("NFC", path).casefold()
        if folded in self._paths:
            previous = self._paths[folded][0]
            raise ArchiveSafetyError(
                f"archive member casefold collision: {previous} / {path}"
            )
        parts = folded.split("/")
        for depth in range(1, len(parts)):
            parent = "/".join(parts[:depth])
            if parent in self._paths and not self._paths[parent][1]:
                raise ArchiveSafetyError("archive file/directory prefix collision")
        if not directory and any(key.startswith(f"{folded}/") for key in self._paths):
            raise ArchiveSafetyError("archive file/directory prefix collision")
        self._paths[folded] = (path, directory)
        self._policy(path)
        return path

    def ordered_paths(self) -> tuple[str, ...]:
        return tuple(sorted(original for original, _directory in self._paths.values()))


def _read_member(
    stream: _Readable, expected: int, bounds: ArchiveBounds, path: str
) -> bytes:
    output = bytearray()
    while len(output) <= expected:
        chunk = stream.read(min(_CHUNK_BYTES, expected + 1 - len(output)))
        if not chunk:
            break
        output.extend(chunk)
        if len(output) > bounds.max_member_bytes:
            raise ArchiveSafetyError(f"member size limit exceeded: {path}")
    if len(output) != expected:
        raise ArchiveSafetyError(f"archive member size is inconsistent: {path}")
    return bytes(output)


def _updated_total(size: int, total: int, path: str, bounds: ArchiveBounds) -> int:
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ArchiveSafetyError(f"archive member size is invalid: {path}")
    if size > bounds.max_member_bytes:
        raise ArchiveSafetyError(f"member size limit exceeded: {path}")
    total += size
    if total > bounds.max_total_bytes:
        raise ArchiveSafetyError("archive total size limit exceeded")
    return total


def _member(path: str, payload: bytes) -> ScannedMember:
    return ScannedMember(path, len(payload), hashlib.sha256(payload).hexdigest())


def _validate_raw_zip_names(stream: BinaryIO, structure: ZipStructure) -> None:
    stream.seek(structure.central_directory_offset)
    directory = stream.read(structure.central_directory_bytes)
    if len(directory) != structure.central_directory_bytes:
        raise ArchiveSafetyError("truncated ZIP central directory")
    offset = 0
    for _index in range(structure.members):
        if offset + _CENTRAL_FILE.size > len(directory):
            raise ArchiveSafetyError("truncated ZIP central directory")
        values = _CENTRAL_FILE.unpack_from(directory, offset)
        if values[0] != _CENTRAL_FILE_SIGNATURE:
            raise ArchiveSafetyError("invalid ZIP central directory member")
        name_bytes, extra_bytes, comment_bytes = values[10:13]
        name_start = offset + _CENTRAL_FILE.size
        name_end = name_start + name_bytes
        if name_end > len(directory):
            raise ArchiveSafetyError("truncated ZIP central directory member")
        if b"\\" in directory[name_start:name_end]:
            raise ArchiveSafetyError("unsafe archive member path")
        offset = name_end + extra_bytes + comment_bytes
    if offset != len(directory):
        raise ArchiveSafetyError("ZIP central directory has trailing records")
    stream.seek(0)


def _scan_zip_stream(
    stream: BinaryIO,
    path: Path,
    structure: ZipStructure,
    bounds: ArchiveBounds,
    path_policy: PathPolicy,
    payload_policy: PayloadPolicy,
) -> tuple[tuple[ScannedMember, ...], tuple[str, ...], Mapping[str, bytes]]:
    members: list[ScannedMember] = []
    payloads: dict[str, bytes] = {}
    registry = _PathRegistry(bounds, path_policy)
    total = 0
    try:
        _validate_raw_zip_names(stream, structure)
        with zipfile.ZipFile(stream, "r", allowZip64=True) as archive:
            infos = archive.infolist()
            if len(infos) != structure.members:
                raise ArchiveSafetyError("ZIP member count changed after preflight")
            for info in infos:
                directory = info.is_dir()
                member_path = registry.add(info.filename, directory)
                if info.flag_bits & 0x1:
                    raise ArchiveSafetyError(
                        f"encrypted ZIP member is forbidden: {member_path}"
                    )
                mode = info.external_attr >> 16
                if mode and stat.S_ISLNK(mode):
                    raise ArchiveSafetyError(f"ZIP links are forbidden: {member_path}")
                member_type = stat.S_IFMT(mode)
                expected_type = stat.S_IFDIR if directory else stat.S_IFREG
                if member_type and member_type != expected_type:
                    raise ArchiveSafetyError(
                        f"non-regular ZIP member is forbidden: {member_path}"
                    )
                if directory:
                    continue
                total = _updated_total(info.file_size, total, member_path, bounds)
                if info.file_size and (
                    info.compress_size == 0
                    or info.file_size
                    > info.compress_size * bounds.max_compression_ratio
                ):
                    raise ArchiveSafetyError(
                        f"ZIP compression ratio limit exceeded: {member_path}"
                    )
                with archive.open(info, "r") as member_stream:
                    payload = _read_member(
                        member_stream, info.file_size, bounds, member_path
                    )
                payload_policy(member_path, payload)
                payloads[member_path] = payload
                members.append(_member(member_path, payload))
    except ArchiveSafetyError:
        raise
    except (
        OSError,
        RuntimeError,
        UnicodeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        raise ArchiveSafetyError(f"invalid ZIP artifact: {path}") from exc
    return (
        tuple(sorted(members, key=lambda item: item.path)),
        registry.ordered_paths(),
        payloads,
    )


class _BoundedReader:
    def __init__(self, source: _Readable, limit: int) -> None:
        self._source = source
        self._limit = limit
        self._read = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self._limit - self._read
        if remaining < 0:
            raise ArchiveSafetyError("decompressed TAR stream limit exceeded")
        requested = remaining + 1 if size < 0 else min(size, remaining + 1)
        payload = self._source.read(requested)
        self._read += len(payload)
        if self._read > self._limit:
            raise ArchiveSafetyError("decompressed TAR stream limit exceeded")
        return payload


def _tar_source(
    stack: ExitStack, stream: BinaryIO, path: Path, bounds: ArchiveBounds
) -> _BoundedReader:
    compressed = path.name.casefold().endswith((".tar.gz", ".tgz"))
    source: _Readable
    if compressed:
        if stream.read(2) != b"\x1f\x8b":
            raise ArchiveSafetyError("gzip TAR artifact has invalid magic")
        stream.seek(0)
        source = stack.enter_context(gzip.GzipFile(fileobj=stream, mode="rb"))
    else:
        source = stream
    return _BoundedReader(source, bounds.max_tar_stream_bytes)


def _validate_tar_type(info: tarfile.TarInfo, path: str) -> None:
    if info.issym() or info.islnk():
        raise ArchiveSafetyError(f"TAR links are forbidden: {path}")
    if info.issparse():
        raise ArchiveSafetyError(f"sparse TAR member is forbidden: {path}")
    if not info.isfile() and not info.isdir():
        raise ArchiveSafetyError(f"special TAR member is forbidden: {path}")


def _scan_tar_stream(
    stream: BinaryIO,
    path: Path,
    bounds: ArchiveBounds,
    path_policy: PathPolicy,
    payload_policy: PayloadPolicy,
) -> tuple[tuple[ScannedMember, ...], tuple[str, ...], Mapping[str, bytes]]:
    members: list[ScannedMember] = []
    payloads: dict[str, bytes] = {}
    registry = _PathRegistry(bounds, path_policy)
    total = 0
    count = 0
    try:
        with ExitStack() as stack:
            source = _tar_source(stack, stream, path, bounds)
            archive = stack.enter_context(
                tarfile.open(fileobj=cast(BinaryIO, source), mode="r|")
            )
            for info in archive:
                count += 1
                if count > bounds.max_members:
                    raise ArchiveSafetyError("archive member count limit exceeded")
                directory = info.isdir()
                member_path = registry.add(info.name, directory)
                _validate_tar_type(info, member_path)
                if directory:
                    continue
                total = _updated_total(info.size, total, member_path, bounds)
                member_stream = archive.extractfile(info)
                if member_stream is None:
                    raise ArchiveSafetyError(
                        f"TAR member cannot be read: {member_path}"
                    )
                with member_stream:
                    payload = _read_member(
                        member_stream, info.size, bounds, member_path
                    )
                payload_policy(member_path, payload)
                payloads[member_path] = payload
                members.append(_member(member_path, payload))
    except ArchiveSafetyError:
        raise
    except (EOFError, OSError, tarfile.TarError) as exc:
        raise ArchiveSafetyError(f"invalid TAR artifact: {path}") from exc
    return (
        tuple(sorted(members, key=lambda item: item.path)),
        registry.ordered_paths(),
        payloads,
    )


def scan_archive(
    path: str | Path,
    bounds: ArchiveBounds,
    *,
    path_policy: PathPolicy,
    payload_policy: PayloadPolicy,
) -> ScannedArchive:
    """Hash and inspect one immutable file handle under explicit archive bounds."""

    resolved, identity = _resolve_artifact(path, bounds)
    name = resolved.name.casefold()
    is_zip = name.endswith((".whl", ".zip"))
    is_tar = name.endswith((".tar.gz", ".tgz", ".tar"))
    if not is_zip and not is_tar:
        raise ArchiveSafetyError(f"unsupported archive type: {resolved.name}")
    structure: ZipStructure | None = None
    if is_zip:
        try:
            structure = inspect_zip_structure(
                resolved,
                max_members=bounds.max_members,
                max_central_directory_bytes=bounds.max_central_directory_bytes,
            )
        except (OSError, ZipStructureError) as exc:
            message = str(exc)
            if "member name is not decodable" in message:
                # Preserve the established artifact-validation envelope for
                # malformed filename encodings while retaining the precise
                # cause on the exception chain.
                raise ArchiveSafetyError(f"invalid ZIP artifact: {resolved}") from exc
            if " members; limit is " in message:
                message = f"archive member count limit exceeded: {message}"
            raise ArchiveSafetyError(f"unsafe ZIP structure: {message}") from exc
        _require_identity(resolved, identity)
    try:
        with resolved.open("rb", buffering=0) as stream:
            if _identity(os.fstat(stream.fileno())) != identity:
                raise ArchiveSafetyError("release artifact changed before inspection")
            archive_sha256 = _hash_handle(stream)
            if is_zip:
                if structure is None:
                    raise ArchiveSafetyError("ZIP preflight result is unavailable")
                members, all_paths, payloads = _scan_zip_stream(
                    stream,
                    resolved,
                    structure,
                    bounds,
                    path_policy,
                    payload_policy,
                )
                kind: Literal["zip", "tar"] = "zip"
            else:
                members, all_paths, payloads = _scan_tar_stream(
                    stream, resolved, bounds, path_policy, payload_policy
                )
                kind = "tar"
            if _hash_handle(stream) != archive_sha256:
                raise ArchiveSafetyError(
                    "release artifact bytes changed during inspection"
                )
            if _identity(os.fstat(stream.fileno())) != identity:
                raise ArchiveSafetyError("release artifact changed during inspection")
    except ArchiveSafetyError:
        raise
    except OSError as exc:
        raise ArchiveSafetyError(
            f"release artifact cannot be inspected: {resolved}"
        ) from exc
    _require_identity(resolved, identity)
    return ScannedArchive(
        resolved,
        kind,
        identity,
        archive_sha256,
        members,
        all_paths,
        payloads,
    )


__all__ = [
    "ArchiveBounds",
    "ArchiveSafetyError",
    "ArtifactIdentity",
    "ScannedArchive",
    "ScannedMember",
    "scan_archive",
]
# endregion [02]
