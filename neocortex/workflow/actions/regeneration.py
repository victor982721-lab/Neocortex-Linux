"""Bounded, read-only proofs that one candidate is reproducible from a package.

This module deliberately proves only a byte-preserving relationship between one
regular candidate and one caller-supplied package member.  It does not search a
corpus, execute code, extract an archive, or authorize an action.  Its optional
current-runtime ``.pyc`` path compiles only bounded source bytes in a private
temporary copy.  The caller owns the archive list and must preserve a selected
witness until any later effect is revalidated.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import importlib.util
import io
import json
import os
import py_compile
import re
import stat
import struct
import tarfile
import tempfile
import time
import warnings
import zipfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import BinaryIO, IO, Protocol

from neocortex.deduplication.domain.models import FileSnapshot
from neocortex.deduplication.fingerprinting import stat_matches_snapshot
from neocortex.platform.policy import stat_birthtime_ns
from neocortex.platform.zip_safety import (
    DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
    ZipStructureError,
    inspect_zip_stream,
)


# These are deliberately finite policy bounds, not claims about every package.
# A caller that needs a larger artifact must provide a separate bounded owner
# contract rather than silently turning this read-only probe into an unbounded
# corpus scan.
MAX_ARCHIVE_PATHS = 32
MAX_ARCHIVE_MEMBERS = 20_000
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_CENTRAL_DIRECTORY_BYTES = DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_MEMBER_BYTES = 256 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_PYC_BYTES = 64 * 1024 * 1024
MAX_PROOF_READ_BYTES = 512 * 1024 * 1024
MAX_PROOF_SECONDS = 10.0
READ_CHUNK_BYTES = 64 * 1024
MAX_COMPRESSION_RATIO = 200.0

WHEEL_METHOD = "wheel-archive-member-v1"
NUPKG_METHOD = "nupkg-archive-member-v1"
NPM_TGZ_METHOD = "npm-tgz-member-v1"
PYC_METHOD = "pyc-source-compile-v1-opt0"
_SUPPORTED_METHODS = frozenset({WHEEL_METHOD, NUPKG_METHOD, NPM_TGZ_METHOD})
_SUPPORTED_ZIP_METHODS = frozenset(
    {
        zipfile.ZIP_STORED,
        zipfile.ZIP_DEFLATED,
        zipfile.ZIP_BZIP2,
        zipfile.ZIP_LZMA,
    }
)
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECORD_HASH = re.compile(r"^sha256=[A-Za-z0-9_-]+$")
_NUPKG_PAYLOAD_ROOTS = frozenset(
    {
        "analyzers",
        "build",
        "buildtransitive",
        "content",
        "contentfiles",
        "lib",
        "ref",
        "runtimes",
        "tools",
    }
)
_WHEEL_LAYOUT_ANCHORS = frozenset({"site-packages", "dist-packages", "vendor"})
_NPM_LAYOUT_ANCHOR = "node_modules"
_NPM_NAME = re.compile(r"^(?:@[a-z0-9._~-]+/)?[a-z0-9][a-z0-9._~-]*$")
_NPM_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")


class _ProofAbstained(RuntimeError):
    """Internal fail-closed signal; public probes return None/False."""


class _ProofCancelled(RuntimeError):
    """Cancellation is propagated, rather than reported as absent evidence."""

    def __init__(self, original: BaseException) -> None:
        self.original = original
        super().__init__(str(original) or "regeneration proof cancelled")


class _ReadableBytes(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


@dataclass(slots=True)
class _ProofBudget:
    cancellation_check: Callable[[], None] | None
    started: float = field(default_factory=time.monotonic)
    bytes_read: int = 0

    def check(self) -> None:
        if self.cancellation_check is not None:
            try:
                self.cancellation_check()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:
                raise _ProofCancelled(exc) from exc
        if time.monotonic() - self.started > MAX_PROOF_SECONDS:
            raise _ProofAbstained("regeneration proof time budget exhausted")

    def consume(self, count: int) -> None:
        if count < 0 or self.bytes_read + count > MAX_PROOF_READ_BYTES:
            raise _ProofAbstained("regeneration proof read budget exhausted")
        self.bytes_read += count
        self.check()


@dataclass(frozen=True, slots=True)
class RegenerationProof:
    """Evidence for one candidate and one preserved package witness.

    ``witness_sha256`` hashes the preserved archive file, not merely the member.
    The member's bytes are compared exactly as well as hashed before this object
    is returned.  A proof is evidence only; it never grants permission to retire
    ``candidate``.
    """

    candidate: FileSnapshot
    candidate_sha256: str
    witnesses: tuple[FileSnapshot, ...]
    witness_sha256: tuple[str, ...]
    method: str
    source_member: str | None

    def to_dict(self) -> dict[str, object]:
        """Return JSON-compatible evidence without retaining raw bytes."""

        return {
            "candidate": _snapshot_to_dict(self.candidate),
            "candidate_sha256": self.candidate_sha256,
            "witnesses": [_snapshot_to_dict(item) for item in self.witnesses],
            "witness_sha256": list(self.witness_sha256),
            "method": self.method,
            "source_member": self.source_member,
        }


@dataclass(frozen=True, slots=True)
class _PackageMember:
    method: str
    name: str
    size: int
    kind: str


def _wheel_member_for_candidate(relative: str) -> str | None:
    parts = relative.split("/")
    anchors = [
        position
        for position, part in enumerate(parts)
        if part.casefold() in _WHEEL_LAYOUT_ANCHORS
    ]
    if len(anchors) > 1:
        return None
    if not anchors:
        return relative
    position = anchors[0] + 1
    if position >= len(parts):
        return None
    return "/".join(parts[position:])


def _npm_member_for_candidate(relative: str) -> tuple[str, str | None] | None:
    parts = relative.split("/")
    anchors = [position for position, part in enumerate(parts) if part == _NPM_LAYOUT_ANCHOR]
    if len(anchors) > 1:
        return None
    if not anchors:
        if relative == "package" or relative.startswith("package/"):
            return relative, None
        return f"package/{relative}", None
    position = anchors[0] + 1
    if position >= len(parts):
        return None
    if parts[position].startswith("@"):
        if position + 2 >= len(parts):
            return None
        package_name = f"{parts[position]}/{parts[position + 1]}"
        payload_position = position + 2
    else:
        if position + 1 >= len(parts):
            return None
        package_name = parts[position]
        payload_position = position + 1
    if payload_position >= len(parts):
        return None
    return f"package/{'/'.join(parts[payload_position:])}", package_name


def _nupkg_members_for_candidate(
    relative: str,
    package_id: str,
    package_version: str,
) -> tuple[str, ...]:
    parts = relative.split("/")
    options: list[str] = []
    if parts and parts[0].casefold() in _NUPKG_PAYLOAD_ROOTS:
        options.append(relative)
    package_id = package_id.casefold()
    package_version = package_version.casefold()
    for position in range(len(parts) - 1):
        if (
            parts[position].casefold() == package_id
            and parts[position + 1].casefold() == package_version
            and position + 2 < len(parts)
        ):
            options.append("/".join(parts[position + 2 :]))
    return tuple(dict.fromkeys(options))


def _snapshot_to_dict(snapshot: FileSnapshot) -> dict[str, object]:
    return {
        "path": snapshot.path,
        "volume_id": snapshot.volume_id,
        "file_id": snapshot.file_id,
        "size": snapshot.size,
        "mtime_ns": snapshot.mtime_ns,
        "birthtime_ns": snapshot.birthtime_ns,
    }


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _HEX_SHA256.fullmatch(value) is not None


def _absolute_path(raw: str | os.PathLike[str]) -> Path:
    try:
        value: object = os.fspath(raw)
        if not isinstance(value, str) or "\x00" in value:
            raise _ProofAbstained("path is not a safe text path")
        path = Path(value)
    except (OSError, TypeError, ValueError) as exc:
        raise _ProofAbstained("path is malformed") from exc
    return Path(os.path.abspath(os.fspath(path)))


def _validated_root(raw: str | os.PathLike[str]) -> Path:
    root = _absolute_path(raw)
    try:
        metadata = os.lstat(root)
    except OSError as exc:
        raise _ProofAbstained("corpus root is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise _ProofAbstained("corpus root is not a regular directory")
    return root


def _relative_member(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise _ProofAbstained("path is outside the corpus root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise _ProofAbstained("path has no safe relative corpus name")
    # The archive formats use POSIX member names.  Do not guess a mapping for a
    # platform-specific separator or a path whose spelling is ambiguous.
    if "\\" in relative.as_posix() or "\x00" in relative.as_posix():
        raise _ProofAbstained("path has an unsafe member spelling")
    return relative.as_posix()


def _within_tree(root: Path, path: Path, relative: str) -> None:
    current = root
    try:
        root_metadata = os.lstat(root)
    except OSError as exc:
        raise _ProofAbstained("corpus root changed") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise _ProofAbstained("corpus root changed")
    parts = relative.split("/")
    for position, part in enumerate(parts):
        current /= part
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise _ProofAbstained("candidate or witness disappeared") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise _ProofAbstained("symlink in candidate or witness tree")
        if position < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise _ProofAbstained("non-directory ancestor in candidate or witness tree")
    try:
        root_key = os.path.normcase(os.path.realpath(root))
        path_key = os.path.normcase(os.path.realpath(path))
        if os.path.commonpath((root_key, path_key)) != root_key:
            raise _ProofAbstained("physical path escapes the corpus root")
    except (OSError, ValueError) as exc:
        raise _ProofAbstained("physical containment is unavailable") from exc


def _snapshot_for_path(
    root: Path,
    raw: str | os.PathLike[str],
    *,
    expected: FileSnapshot | None = None,
) -> tuple[FileSnapshot, str]:
    path = _absolute_path(raw)
    relative = _relative_member(root, path)
    _within_tree(root, path, relative)
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise _ProofAbstained("regular source is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise _ProofAbstained("candidate or witness is not a regular file")
    if int(getattr(metadata, "st_nlink", 1)) != 1:
        raise _ProofAbstained("hardlinked candidate or witness is not safe")
    snapshot = FileSnapshot(
        str(path),
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(stat_birthtime_ns(metadata)),
    )
    if expected is not None:
        expected_path = _absolute_path(expected.path)
        if expected_path != path or not stat_matches_snapshot(expected, metadata):
            raise _ProofAbstained("candidate or witness identity changed")
    return snapshot, relative


def _open_descriptor(snapshot: FileSnapshot) -> int:
    path = Path(snapshot.path)
    common_flags = (
        os.O_RDONLY
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_NOFOLLOW", 0))
        | int(getattr(os, "O_NONBLOCK", 0))
    )
    if os.name != "posix":  # pragma: no cover - NeoCortex is Linux-only
        try:
            descriptor = os.open(os.fspath(path), common_flags)
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or int(getattr(observed, "st_nlink", 1)) != 1
                or not stat_matches_snapshot(snapshot, observed)
            ):
                os.close(descriptor)
                raise _ProofAbstained("regular source identity changed")
            return descriptor
        except OSError as exc:
            raise _ProofAbstained("regular source cannot be opened") from exc
    components = path.parts
    if not components or components[0] != os.sep or len(components) < 2:
        raise _ProofAbstained("regular source path is not absolute")
    directory_flags = common_flags | int(getattr(os, "O_DIRECTORY", 0))
    directory_descriptor: int | None = None
    file_descriptor: int | None = None
    try:
        directory_descriptor = os.open(os.sep, directory_flags)
        for component in components[1:-1]:
            next_descriptor = os.open(
                component,
                directory_flags,
                dir_fd=directory_descriptor,
            )
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        file_descriptor = os.open(
            components[-1],
            common_flags,
            dir_fd=directory_descriptor,
        )
        observed = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or int(getattr(observed, "st_nlink", 1)) != 1
            or not stat_matches_snapshot(snapshot, observed)
        ):
            raise _ProofAbstained("regular source identity changed")
        os.close(directory_descriptor)
        directory_descriptor = None
        return file_descriptor
    except _ProofAbstained:
        raise
    except OSError as exc:
        raise _ProofAbstained("regular source cannot be opened") from exc
    finally:
        if file_descriptor is not None and directory_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if directory_descriptor is not None:
            try:
                os.close(directory_descriptor)
            except OSError:
                pass


@contextmanager
def _open_regular(snapshot: FileSnapshot) -> Iterator[BinaryIO]:
    descriptor = _open_descriptor(snapshot)
    try:
        stream = os.fdopen(descriptor, "rb", buffering=0)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    try:
        yield stream
    finally:
        stream.close()


def _hash_snapshot(snapshot: FileSnapshot, budget: _ProofBudget) -> str:
    digest = hashlib.sha256()
    actual = 0
    try:
        with _open_regular(snapshot) as stream:
            while True:
                budget.check()
                chunk = stream.read(READ_CHUNK_BYTES)
                if not chunk:
                    break
                budget.consume(len(chunk))
                actual += len(chunk)
                if actual > snapshot.size:
                    raise _ProofAbstained("source grew during hashing")
                digest.update(chunk)
            observed = os.fstat(stream.fileno())
            if actual != snapshot.size or not stat_matches_snapshot(snapshot, observed):
                raise _ProofAbstained("source changed during hashing")
    except (OSError, ValueError) as exc:
        raise _ProofAbstained("source could not be hashed") from exc
    return digest.hexdigest()


def _read_snapshot_payload(
    snapshot: FileSnapshot,
    budget: _ProofBudget,
    *,
    limit: int,
) -> bytes:
    """Read one bounded regular source without ever writing or executing it."""

    with _open_regular(snapshot) as stream:
        payload = _read_stream(stream, budget, limit=limit)
        observed = os.fstat(stream.fileno())
        if len(payload) != snapshot.size or not stat_matches_snapshot(snapshot, observed):
            raise _ProofAbstained("source changed during bounded read")
    return payload


def _pyc_source_path(path: Path) -> Path | None:
    if path.suffix.casefold() != ".pyc":
        return None
    try:
        cached_source = Path(importlib.util.source_from_cache(os.fspath(path)))
    except (NotImplementedError, ValueError):
        cached_source = None
    if cached_source is not None and cached_source.suffix.casefold() == ".py":
        return cached_source
    if path.parent.name == "__pycache__":
        stem = path.name[:-4]
        return path.parent.parent / f"{stem.split('.', 1)[0]}.py"
    return path.with_suffix(".py")


def _pyc_matches_source(
    candidate_bytes: bytes,
    source_bytes: bytes,
    source: FileSnapshot,
    budget: _ProofBudget,
) -> bool:
    if len(candidate_bytes) < 16 or candidate_bytes[:4] != importlib.util.MAGIC_NUMBER:
        return False
    if len(source_bytes) != source.size:
        return False
    flags = struct.unpack("<I", candidate_bytes[4:8])[0]
    if flags & ~0x03:
        return False
    invalidation_mode = {
        0: py_compile.PycInvalidationMode.TIMESTAMP,
        1: py_compile.PycInvalidationMode.UNCHECKED_HASH,
        3: py_compile.PycInvalidationMode.CHECKED_HASH,
    }.get(flags)
    if invalidation_mode is None:
        return False
    # Source and bytecode are capped by MAX_SOURCE_BYTES/MAX_PYC_BYTES at the
    # caller.  The checks before and after py_compile are cooperative: Python's
    # compiler call is not forcibly interrupted by this read-only probe.
    budget.check()
    try:
        with tempfile.TemporaryDirectory(prefix="neocortex-regeneration-") as directory:
            # Compile only the bounded bytes already read through the guarded
            # descriptor.  ``py_compile`` must never reopen the original source
            # path; the temporary copy carries the observed timestamp solely so
            # the generated header can be compared with the candidate.
            temporary_source = Path(directory) / "source.py"
            temporary_source.write_bytes(source_bytes)
            if source.mtime_ns < 0:
                return False
            os.utime(
                temporary_source,
                ns=(source.mtime_ns, source.mtime_ns),
                follow_symlinks=False,
            )
            output = Path(directory) / "candidate.pyc"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                py_compile.compile(
                    os.fspath(temporary_source),
                    cfile=os.fspath(output),
                    dfile=source.path,
                    doraise=True,
                    optimize=0,
                    invalidation_mode=invalidation_mode,
                )
            budget.check()
            if not output.is_file() or output.stat().st_size > MAX_PYC_BYTES:
                return False
            generated = output.read_bytes()
            budget.consume(len(generated))
    except (SyntaxError, TypeError, ValueError, OverflowError):
        return False
    except (OSError, py_compile.PyCompileError):
        return False
    budget.check()
    return candidate_bytes == generated


def _find_pyc_proof(
    candidate: FileSnapshot,
    *,
    root: Path,
    budget: _ProofBudget,
) -> RegenerationProof | None:
    if candidate.size > MAX_PYC_BYTES:
        return None
    source_path = _pyc_source_path(Path(candidate.path))
    if source_path is None:
        return None
    try:
        source, source_relative = _snapshot_for_path(root, source_path)
    except _ProofAbstained:
        return None
    if source.identity == candidate.identity or source.size > MAX_SOURCE_BYTES:
        return None
    candidate_bytes = _read_snapshot_payload(candidate, budget, limit=MAX_PYC_BYTES)
    source_bytes = _read_snapshot_payload(source, budget, limit=MAX_SOURCE_BYTES)
    if not _pyc_matches_source(candidate_bytes, source_bytes, source, budget):
        return None
    current_source, _ = _snapshot_for_path(root, source.path, expected=source)
    current_source_bytes = _read_snapshot_payload(
        current_source, budget, limit=MAX_SOURCE_BYTES
    )
    current_candidate, _ = _snapshot_for_path(root, candidate.path, expected=candidate)
    current_candidate_bytes = _read_snapshot_payload(
        current_candidate, budget, limit=MAX_PYC_BYTES
    )
    if current_source_bytes != source_bytes or current_candidate_bytes != candidate_bytes:
        return None
    return RegenerationProof(
        candidate=candidate,
        candidate_sha256=hashlib.sha256(candidate_bytes).hexdigest(),
        witnesses=(source,),
        witness_sha256=(hashlib.sha256(source_bytes).hexdigest(),),
        method=PYC_METHOD,
        source_member=source_relative,
    )


def _revalidate_pyc_proof(
    proof: RegenerationProof,
    *,
    root: Path,
    budget: _ProofBudget,
) -> bool:
    candidate, _ = _snapshot_for_path(root, proof.candidate.path, expected=proof.candidate)
    if not candidate.path.casefold().endswith(".pyc") or candidate.size > MAX_PYC_BYTES:
        return False
    source_path = _pyc_source_path(Path(candidate.path))
    if source_path is None:
        return False
    source, source_relative = _snapshot_for_path(
        root, source_path, expected=proof.witnesses[0]
    )
    if source.identity == candidate.identity or source_relative != proof.source_member:
        return False
    if source.size > MAX_SOURCE_BYTES:
        return False
    candidate_bytes = _read_snapshot_payload(candidate, budget, limit=MAX_PYC_BYTES)
    source_bytes = _read_snapshot_payload(source, budget, limit=MAX_SOURCE_BYTES)
    if hashlib.sha256(candidate_bytes).hexdigest() != proof.candidate_sha256:
        return False
    if hashlib.sha256(source_bytes).hexdigest() != proof.witness_sha256[0]:
        return False
    if not _pyc_matches_source(candidate_bytes, source_bytes, source, budget):
        return False
    current_source, _ = _snapshot_for_path(root, source.path, expected=source)
    current_source_bytes = _read_snapshot_payload(
        current_source, budget, limit=MAX_SOURCE_BYTES
    )
    current_candidate, _ = _snapshot_for_path(root, candidate.path, expected=candidate)
    current_candidate_bytes = _read_snapshot_payload(
        current_candidate, budget, limit=MAX_PYC_BYTES
    )
    return current_source_bytes == source_bytes and current_candidate_bytes == candidate_bytes


def _safe_member_name(raw: object) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise _ProofAbstained("archive member name is unsafe")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise _ProofAbstained("archive member name escapes its package")
    directory = raw.endswith("/")
    value = raw.rstrip("/") if directory else raw
    parts = value.split("/")
    if not value or any(part in {"", ".", ".."} for part in parts):
        raise _ProofAbstained("archive member traversal is not supported")
    return value


def _zip_regular_or_directory(info: zipfile.ZipInfo) -> bool:
    mode = (int(info.external_attr) >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if info.is_dir():
        return file_type in {0, stat.S_IFDIR}
    return file_type in {0, stat.S_IFREG}


def _zip_inventory(
    archive: zipfile.ZipFile,
    budget: _ProofBudget,
) -> tuple[dict[str, zipfile.ZipInfo], frozenset[str]]:
    try:
        infos = archive.infolist()
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        raise _ProofAbstained("ZIP structure cannot be inspected") from exc
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise _ProofAbstained("archive member budget exhausted")
    entries: dict[str, zipfile.ZipInfo] = {}
    files: set[str] = set()
    total = 0
    for info in infos:
        budget.check()
        name = _safe_member_name(info.filename)
        if name in entries:
            raise _ProofAbstained("duplicate archive member is ambiguous")
        if not _zip_regular_or_directory(info):
            raise _ProofAbstained("archive contains a symlink or special member")
        if info.flag_bits & 0x1:
            raise _ProofAbstained("encrypted archive member is unsupported")
        if info.compress_type not in _SUPPORTED_ZIP_METHODS:
            raise _ProofAbstained("archive compression method is unsupported")
        size = int(info.file_size)
        compressed = int(info.compress_size)
        if size < 0 or size > MAX_MEMBER_BYTES:
            raise _ProofAbstained("archive member byte budget exhausted")
        if not info.is_dir():
            if size and compressed <= 0:
                raise _ProofAbstained("archive member compression metadata is invalid")
            if compressed and size / compressed > MAX_COMPRESSION_RATIO:
                raise _ProofAbstained("archive compression ratio exceeds the safe bound")
            total += size
            if total > MAX_TOTAL_MEMBER_BYTES:
                raise _ProofAbstained("archive total expansion budget exhausted")
            files.add(name)
        entries[name] = info
    return entries, frozenset(files)


def _read_stream(stream: IO[bytes], budget: _ProofBudget, *, limit: int) -> bytes:
    chunks: list[bytes] = []
    actual = 0
    while True:
        budget.check()
        chunk = stream.read(min(READ_CHUNK_BYTES, limit - actual + 1))
        if not chunk:
            break
        budget.consume(len(chunk))
        actual += len(chunk)
        if actual > limit:
            raise _ProofAbstained("metadata or member read budget exhausted")
        chunks.append(chunk)
    return b"".join(chunks)


def _read_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    budget: _ProofBudget,
    *,
    limit: int,
) -> bytes:
    try:
        with archive.open(info, "r") as stream:
            payload = _read_stream(stream, budget, limit=limit)
    except _ProofCancelled:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, NotImplementedError) as exc:
        raise _ProofAbstained("ZIP member cannot be read safely") from exc
    if len(payload) != int(info.file_size):
        raise _ProofAbstained("ZIP member was truncated or changed")
    return payload


def _message_headers(payload: bytes) -> Mapping[str, str]:
    try:
        message = BytesParser(policy=policy.compat32).parsebytes(payload)
    except (TypeError, ValueError) as exc:
        raise _ProofAbstained("package metadata headers are malformed") from exc
    if message.defects:
        raise _ProofAbstained("package metadata headers contain defects")
    return {
        str(key).casefold(): str(value).strip()
        for key, value in message.items()
    }


def _wheel_record_names(
    payload: bytes,
    files: frozenset[str],
) -> frozenset[str]:
    try:
        text = payload.decode("utf-8")
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise _ProofAbstained("wheel RECORD is malformed") from exc
    if not rows or len(rows) > MAX_ARCHIVE_MEMBERS:
        raise _ProofAbstained("wheel RECORD exceeds its bound")
    names: set[str] = set()
    for row in rows:
        if len(row) != 3:
            raise _ProofAbstained("wheel RECORD row is malformed")
        name = _safe_member_name(row[0])
        if name not in files or name in names:
            raise _ProofAbstained("wheel RECORD does not describe its archive")
        hash_value, size_value = row[1], row[2]
        if hash_value and _RECORD_HASH.fullmatch(hash_value) is None:
            raise _ProofAbstained("wheel RECORD hash is malformed")
        if size_value:
            try:
                if int(size_value) < 0:
                    raise ValueError
            except ValueError as exc:
                raise _ProofAbstained("wheel RECORD size is malformed") from exc
        names.add(name)
    if names != set(files):
        raise _ProofAbstained("wheel RECORD omits or invents a package file")
    return frozenset(names)


def _wheel_package(
    archive: zipfile.ZipFile,
    candidate_relative: str,
    entries: dict[str, zipfile.ZipInfo],
    files: frozenset[str],
    budget: _ProofBudget,
) -> _PackageMember | None:
    dist_infos = sorted(
        name[:-len("/WHEEL")]
        for name in files
        if name.endswith(".dist-info/WHEEL")
    )
    if len(dist_infos) != 1:
        raise _ProofAbstained("wheel has ambiguous WHEEL metadata")
    dist_info = dist_infos[0]
    metadata_name = f"{dist_info}/METADATA"
    record_name = f"{dist_info}/RECORD"
    if metadata_name not in files or record_name not in files:
        raise _ProofAbstained("wheel metadata is incomplete")
    wheel_headers = _message_headers(
        _read_zip_member(archive, entries[f"{dist_info}/WHEEL"], budget, limit=MAX_METADATA_BYTES)
    )
    if not wheel_headers.get("wheel-version") or not wheel_headers.get("generator"):
        raise _ProofAbstained("wheel metadata lacks its standard headers")
    if wheel_headers.get("root-is-purelib", "").casefold() not in {"true", "false"}:
        raise _ProofAbstained("wheel Root-Is-Purelib is malformed")
    if not wheel_headers.get("tag"):
        raise _ProofAbstained("wheel metadata lacks a tag")
    metadata_headers = _message_headers(
        _read_zip_member(archive, entries[metadata_name], budget, limit=MAX_METADATA_BYTES)
    )
    if not all(metadata_headers.get(key) for key in ("metadata-version", "name", "version")):
        raise _ProofAbstained("wheel METADATA lacks Name or Version")
    records = _wheel_record_names(
        _read_zip_member(archive, entries[record_name], budget, limit=MAX_METADATA_BYTES),
        files,
    )
    candidate_member = _wheel_member_for_candidate(candidate_relative)
    if candidate_member is None:
        return None
    if candidate_member not in records or candidate_member not in files:
        return None
    return _PackageMember(WHEEL_METHOD, candidate_member, int(entries[candidate_member].file_size), "zip")


def _local_name(tag: object) -> str:
    value = str(tag)
    return value.rsplit("}", 1)[-1].casefold()


def _nupkg_metadata(payload: bytes) -> tuple[str, str]:
    lowered = payload[:MAX_METADATA_BYTES].lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise _ProofAbstained("nupkg XML entities are unsupported")
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, ValueError) as exc:
        raise _ProofAbstained("nupkg nuspec is malformed") from exc
    if _local_name(root.tag) != "package":
        raise _ProofAbstained("nupkg nuspec has no package root")
    metadata = next((item for item in root if _local_name(item.tag) == "metadata"), None)
    if metadata is None:
        raise _ProofAbstained("nupkg nuspec has no metadata")
    values = {
        _local_name(item.tag): (item.text or "").strip()
        for item in metadata
        if item.text is not None
    }
    package_id = values.get("id", "")
    package_version = values.get("version", "")
    if not package_id or not package_version:
        raise _ProofAbstained("nupkg nuspec lacks id or version")
    if any(any(ord(char) < 0x20 for char in value) for value in values.values()):
        raise _ProofAbstained("nupkg nuspec metadata contains controls")
    return package_id, package_version


def _nupkg_package(
    archive: zipfile.ZipFile,
    candidate_relative: str,
    entries: dict[str, zipfile.ZipInfo],
    files: frozenset[str],
    budget: _ProofBudget,
) -> _PackageMember | None:
    nuspecs = [name for name in files if "/" not in name and name.casefold().endswith(".nuspec")]
    if len(nuspecs) != 1:
        raise _ProofAbstained("nupkg has ambiguous nuspec metadata")
    package_id, package_version = _nupkg_metadata(
        _read_zip_member(archive, entries[nuspecs[0]], budget, limit=MAX_METADATA_BYTES)
    )
    payload_roots = {
        name.split("/", 1)[0].casefold()
        for name in files
        if "/" in name
    }
    if not payload_roots.intersection(_NUPKG_PAYLOAD_ROOTS):
        raise _ProofAbstained("nupkg has no standard payload root")
    options = _nupkg_members_for_candidate(candidate_relative, package_id, package_version)
    options = tuple(option for option in options if option in files)
    if len(options) != 1:
        return None
    candidate_member = options[0]
    return _PackageMember(NUPKG_METHOD, candidate_member, int(entries[candidate_member].file_size), "zip")


class _BudgetedReader:
    """Bound every gzip/tar read, including skipped member payloads."""

    def __init__(self, stream: _ReadableBytes, budget: _ProofBudget) -> None:
        self._stream = stream
        self._budget = budget
        self._position = 0

    def read(self, size: int = -1) -> bytes:
        self._budget.check()
        if size is None or size < 0:
            size = READ_CHUNK_BYTES
        data = self._stream.read(size)
        self._budget.consume(len(data))
        self._position += len(data)
        return data

    def readinto(self, buffer: bytearray | memoryview) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def seek(self, *_args: object, **_kwargs: object) -> int:
        raise OSError("bounded regeneration stream is not seekable")

    def write(self, _buffer: bytes) -> int:
        raise OSError("bounded regeneration stream is read-only")

    def flush(self) -> None:
        raise OSError("bounded regeneration stream is read-only")

    def tell(self) -> int:
        return self._position

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def close(self) -> None:
        # The owning context closes the real raw/gzip streams.
        return None


@contextmanager
def _open_tar_archive(
    witness: FileSnapshot,
    budget: _ProofBudget,
) -> Iterator[tarfile.TarFile]:
    with _open_regular(witness) as raw:
        bounded_compressed = _BudgetedReader(raw, budget)
        try:
            with gzip.GzipFile(fileobj=bounded_compressed, mode="rb") as expanded:
                bounded_tar = _BudgetedReader(expanded, budget)
                with tarfile.open(fileobj=bounded_tar, mode="r|") as archive:
                    yield archive
        except _ProofCancelled:
            raise
        except (EOFError, OSError, tarfile.TarError, ValueError) as exc:
            raise _ProofAbstained("npm tar structure cannot be inspected") from exc


def _tar_member_bounds(
    member: tarfile.TarInfo,
    *,
    seen: set[str],
    total: int,
    archive_size: int,
) -> tuple[str, int, int]:
    name = _safe_member_name(member.name)
    if name in seen:
        raise _ProofAbstained("duplicate npm member is ambiguous")
    seen.add(name)
    if not (member.isdir() or member.isreg()):
        raise _ProofAbstained("npm archive contains a link or special member")
    size = int(member.size)
    if size < 0 or size > MAX_MEMBER_BYTES:
        raise _ProofAbstained("npm member byte budget exhausted")
    if member.isreg():
        total += size
        if total > MAX_TOTAL_MEMBER_BYTES:
            raise _ProofAbstained("npm total expansion budget exhausted")
        if total / max(1, archive_size) > MAX_COMPRESSION_RATIO:
            raise _ProofAbstained("npm compression ratio exceeds the safe bound")
    if len(seen) > MAX_ARCHIVE_MEMBERS:
        raise _ProofAbstained("npm archive member budget exhausted")
    return name, size, total


def _read_current_tar_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    budget: _ProofBudget,
) -> bytes:
    try:
        stream = archive.extractfile(member)
    except (KeyError, OSError, tarfile.TarError) as exc:
        raise _ProofAbstained("npm package metadata cannot be read") from exc
    if stream is None:
        raise _ProofAbstained("npm package metadata cannot be read")
    with stream:
        payload = _read_stream(stream, budget, limit=MAX_METADATA_BYTES)
    if len(payload) != int(member.size):
        raise _ProofAbstained("npm package metadata was truncated")
    return payload


def _npm_package(
    witness: FileSnapshot,
    candidate_relative: str,
    budget: _ProofBudget,
) -> _PackageMember | None:
    layout = _npm_member_for_candidate(candidate_relative)
    expected, installed_name = (None, None) if layout is None else layout
    package_json: bytes | None = None
    candidate_size: int | None = None
    seen: set[str] = set()
    total = 0
    with _open_tar_archive(witness, budget) as archive:
        while True:
            budget.check()
            try:
                member = archive.next()
            except (EOFError, OSError, tarfile.TarError, ValueError) as exc:
                raise _ProofAbstained("npm tar structure cannot be inspected") from exc
            if member is None:
                break
            name, size, total = _tar_member_bounds(
                member,
                seen=seen,
                total=total,
                archive_size=witness.size,
            )
            if name == "package/package.json":
                package_json = _read_current_tar_member(archive, member, budget)
            if expected is not None and name == expected:
                candidate_size = size
    if package_json is None:
        raise _ProofAbstained("npm package metadata is missing")
    try:
        metadata = json.loads(package_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ProofAbstained("npm package.json is malformed") from exc
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("name"), str)
        or not _NPM_NAME.fullmatch(metadata["name"])
        or not isinstance(metadata.get("version"), str)
        or not _NPM_VERSION.fullmatch(metadata["version"])
    ):
        raise _ProofAbstained("npm package metadata lacks a standard name/version")
    if expected is None or candidate_size is None:
        return None
    if installed_name is not None and metadata["name"].casefold() != installed_name.casefold():
        return None
    return _PackageMember(NPM_TGZ_METHOD, expected, candidate_size, "tar")


def _preflight_zip_stream(
    raw: BinaryIO,
    witness: FileSnapshot,
    budget: _ProofBudget,
) -> None:
    """Bound ZIP central-directory allocation before constructing ZipFile."""

    budget.check()
    # ``inspect_zip_stream`` restores the seek position and rejects declared
    # member/central-directory bounds before ``zipfile`` materializes ZipInfo.
    # Charge the bounded preflight envelope conservatively; it is still capped
    # by MAX_CENTRAL_DIRECTORY_BYTES and the operation read budget.
    budget.consume(min(witness.size, MAX_CENTRAL_DIRECTORY_BYTES))
    try:
        inspect_zip_stream(
            raw,
            witness.size,
            max_members=MAX_ARCHIVE_MEMBERS,
            max_central_directory_bytes=MAX_CENTRAL_DIRECTORY_BYTES,
        )
    except _ProofCancelled:
        raise
    except (OSError, ValueError, ZipStructureError) as exc:
        raise _ProofAbstained("ZIP structural preflight failed") from exc
    budget.check()
    raw.seek(0)


def _inspect_archive(
    witness: FileSnapshot,
    root: Path,
    candidate_relative: str,
    budget: _ProofBudget,
    *,
    expected_member: str | None = None,
) -> _PackageMember | None:
    if witness.size > MAX_ARCHIVE_BYTES:
        raise _ProofAbstained("archive byte budget exhausted")
    lower = witness.path.casefold()
    try:
        if lower.endswith((".whl", ".nupkg")):
            with _open_regular(witness) as raw:
                _preflight_zip_stream(raw, witness, budget)
                try:
                    with zipfile.ZipFile(raw) as archive:
                        entries, files = _zip_inventory(archive, budget)
                        if lower.endswith(".whl"):
                            result = _wheel_package(
                                archive, candidate_relative, entries, files, budget
                            )
                        else:
                            result = _nupkg_package(
                                archive, candidate_relative, entries, files, budget
                            )
                except _ProofCancelled:
                    raise
                except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
                    raise _ProofAbstained("package ZIP is corrupt") from exc
        elif lower.endswith((".tgz", ".tar.gz")):
            result = _npm_package(witness, candidate_relative, budget)
        else:
            return None
    except _ProofAbstained:
        raise
    if result is None:
        return None
    if expected_member is not None and result.name != expected_member:
        raise _ProofAbstained("proof source member no longer matches the package")
    return result


@contextmanager
def _open_member(
    witness: FileSnapshot,
    package_member: _PackageMember,
    budget: _ProofBudget,
) -> Iterator[IO[bytes]]:
    if package_member.kind == "zip":
        with _open_regular(witness) as raw:
            _preflight_zip_stream(raw, witness, budget)
            with zipfile.ZipFile(raw) as archive:
                try:
                    info = archive.getinfo(package_member.name)
                except KeyError as exc:
                    raise _ProofAbstained("package member disappeared") from exc
                if (
                    info.flag_bits & 0x1
                    or info.compress_type not in _SUPPORTED_ZIP_METHODS
                    or not _zip_regular_or_directory(info)
                    or info.is_dir()
                    or int(info.file_size) != package_member.size
                ):
                    raise _ProofAbstained("package member is no longer safe")
                try:
                    with archive.open(info, "r") as stream:
                        yield stream
                except _ProofCancelled:
                    raise
                except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
                    raise _ProofAbstained("package member cannot be read") from exc
        return
    with _open_tar_archive(witness, budget) as archive:
        seen: set[str] = set()
        total = 0
        while True:
            budget.check()
            member = archive.next()
            if member is None:
                break
            name, size, total = _tar_member_bounds(
                member,
                seen=seen,
                total=total,
                archive_size=witness.size,
            )
            if name != package_member.name:
                continue
            if not member.isreg() or size != package_member.size:
                raise _ProofAbstained("package member is no longer safe")
            tar_stream = archive.extractfile(member)
            if tar_stream is None:
                raise _ProofAbstained("package member cannot be read")
            with tar_stream:
                yield tar_stream
            return
        raise _ProofAbstained("package member disappeared")


def _member_sha256(
    witness: FileSnapshot,
    package_member: _PackageMember,
    budget: _ProofBudget,
) -> str:
    digest = hashlib.sha256()
    actual = 0
    with _open_member(witness, package_member, budget) as stream:
        while True:
            budget.check()
            chunk = stream.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            budget.consume(len(chunk))
            actual += len(chunk)
            if actual > package_member.size:
                raise _ProofAbstained("package member exceeded its declared size")
            digest.update(chunk)
    if actual != package_member.size:
        raise _ProofAbstained("package member was truncated")
    return digest.hexdigest()


def _candidate_equals_member(
    candidate: FileSnapshot,
    witness: FileSnapshot,
    package_member: _PackageMember,
    budget: _ProofBudget,
) -> bool:
    actual_candidate = 0
    actual_member = 0
    with _open_regular(candidate) as left, _open_member(witness, package_member, budget) as right:
        while True:
            budget.check()
            left_chunk = left.read(READ_CHUNK_BYTES)
            right_chunk = right.read(READ_CHUNK_BYTES)
            budget.consume(len(left_chunk) + len(right_chunk))
            actual_candidate += len(left_chunk)
            actual_member += len(right_chunk)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                break
            if actual_candidate > candidate.size or actual_member > package_member.size:
                raise _ProofAbstained("exact comparison exceeded a declared size")
        left_stat = os.fstat(left.fileno())
        if not stat_matches_snapshot(candidate, left_stat):
            raise _ProofAbstained("candidate changed during exact comparison")
    if actual_candidate != candidate.size or actual_member != package_member.size:
        raise _ProofAbstained("exact comparison encountered a truncated source")
    return True


def _bounded_archive_paths(archive_paths: Sequence[Path]) -> tuple[Path, ...]:
    archive_values: object = archive_paths
    if isinstance(archive_values, (str, bytes)):
        raise _ProofAbstained("archive_paths must be a bounded sequence")
    try:
        if len(archive_paths) > MAX_ARCHIVE_PATHS:
            raise _ProofAbstained("archive path budget exhausted")
        values = [_absolute_path(item) for item in archive_paths]
    except (TypeError, ValueError, OSError) as exc:
        raise _ProofAbstained("archive path sequence is malformed") from exc
    # Stable ordering makes witness choice deterministic; exact duplicate paths
    # are not a second independent witness.
    return tuple(dict.fromkeys(sorted(values, key=os.fspath)))


def _find_proof(
    snapshot: FileSnapshot,
    *,
    root: Path,
    archive_paths: Sequence[Path],
    budget: _ProofBudget,
) -> RegenerationProof | None:
    candidate, candidate_relative = _snapshot_for_path(root, snapshot.path, expected=snapshot)
    if candidate.path.casefold().endswith(".pyc"):
        return _find_pyc_proof(candidate, root=root, budget=budget)
    if candidate.path.casefold().endswith(".pyo"):
        return None
    bounded_archives = _bounded_archive_paths(archive_paths)
    if not bounded_archives:
        # The common no-source case must not read or hash every unknown
        # candidate merely to conclude that no proof exists.
        return None
    if candidate.size > MAX_MEMBER_BYTES:
        return None
    matches: list[tuple[FileSnapshot, _PackageMember]] = []
    seen_identities: set[tuple[int, int]] = set()
    for archive_path in bounded_archives:
        budget.check()
        witness, _archive_member = _snapshot_for_path(root, archive_path)
        if witness.identity == candidate.identity:
            continue
        if witness.identity in seen_identities:
            continue
        seen_identities.add(witness.identity)
        package_member = _inspect_archive(witness, root, candidate_relative, budget)
        if package_member is None:
            continue
        matches.append((witness, package_member))
    if not matches:
        return None
    candidate_sha = _hash_snapshot(candidate, budget)
    proved: list[tuple[FileSnapshot, _PackageMember, str]] = []
    for witness, package_member in matches:
        budget.check()
        member_sha = _member_sha256(witness, package_member, budget)
        if member_sha != candidate_sha:
            continue
        if not _candidate_equals_member(candidate, witness, package_member, budget):
            continue
        witness_sha = _hash_snapshot(witness, budget)
        _snapshot_for_path(root, candidate.path, expected=candidate)
        _snapshot_for_path(root, witness.path, expected=witness)
        proved.append((witness, package_member, witness_sha))
        if len(proved) > 1:
            return None
    if len(proved) != 1:
        return None
    witness, package_member, witness_sha = proved[0]
    return RegenerationProof(
        candidate=candidate,
        candidate_sha256=candidate_sha,
        witnesses=(witness,),
        witness_sha256=(witness_sha,),
        method=package_member.method,
        source_member=package_member.name,
    )


def find_regeneration_proof(
    snapshot: FileSnapshot,
    *,
    root: Path,
    archive_paths: Sequence[Path] = (),
    cancellation_check: Callable[[], None] | None = None,
) -> RegenerationProof | None:
    """Find one exact package witness from the caller-provided bounded list.

    Unsupported archive types simply do not match.  A supported package that is
    malformed, encrypted, traversal-bearing, over budget, or ambiguous causes
    abstention rather than a weaker name-based match.
    """

    budget = _ProofBudget(cancellation_check)
    try:
        budget.check()
        return _find_proof(
            snapshot,
            root=_validated_root(root),
            archive_paths=archive_paths,
            budget=budget,
        )
    except _ProofCancelled as exc:
        raise exc.original from None
    except (_ProofAbstained, OSError, TypeError, ValueError):
        return None


def revalidate_regeneration_proof(
    proof: RegenerationProof,
    *,
    root: Path,
    cancellation_check: Callable[[], None] | None = None,
) -> bool:
    """Repeat package validation, hashes, exact bytes, and all source fences."""

    budget = _ProofBudget(cancellation_check)
    try:
        budget.check()
        if (
            proof.method not in _SUPPORTED_METHODS | {PYC_METHOD}
            or proof.source_member is None
            or not isinstance(proof.source_member, str)
            or not _is_sha256(proof.candidate_sha256)
            or len(proof.witnesses) != 1
            or len(proof.witness_sha256) != 1
            or not _is_sha256(proof.witness_sha256[0])
        ):
            return False
        root_path = _validated_root(root)
        if proof.method == PYC_METHOD:
            return _revalidate_pyc_proof(proof, root=root_path, budget=budget)
        candidate, candidate_member = _snapshot_for_path(
            root_path, proof.candidate.path, expected=proof.candidate
        )
        if candidate.path.casefold().endswith((".pyc", ".pyo")):
            return False
        if candidate_member != _relative_member(root_path, Path(candidate.path)):
            return False
        if candidate.size > MAX_MEMBER_BYTES:
            return False
        candidate_sha = _hash_snapshot(candidate, budget)
        if candidate_sha != proof.candidate_sha256:
            return False
        witness, _ = _snapshot_for_path(
            root_path, proof.witnesses[0].path, expected=proof.witnesses[0]
        )
        if witness.identity == candidate.identity:
            return False
        package_member = _inspect_archive(
            witness,
            root_path,
            candidate_member,
            budget,
            expected_member=proof.source_member,
        )
        if package_member is None or package_member.method != proof.method:
            return False
        if _member_sha256(witness, package_member, budget) != candidate_sha:
            return False
        if not _candidate_equals_member(candidate, witness, package_member, budget):
            return False
        if _hash_snapshot(witness, budget) != proof.witness_sha256[0]:
            return False
        _snapshot_for_path(root_path, candidate.path, expected=candidate)
        _snapshot_for_path(root_path, witness.path, expected=witness)
        return True
    except _ProofCancelled as exc:
        raise exc.original from None
    except (_ProofAbstained, OSError, TypeError, ValueError):
        return False


__all__ = [
    "MAX_ARCHIVE_PATHS",
    "MAX_CENTRAL_DIRECTORY_BYTES",
    "NPM_TGZ_METHOD",
    "NUPKG_METHOD",
    "PYC_METHOD",
    "WHEEL_METHOD",
    "RegenerationProof",
    "find_regeneration_proof",
    "revalidate_regeneration_proof",
]
