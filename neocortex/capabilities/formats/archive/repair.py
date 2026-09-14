"""Offline, bounded ZIP repair candidates.

Repair is deliberately a candidate-producing operation.  It never overwrites
the source, never invokes a shell, and never treats a repair tool's exit code
as proof that the resulting archive is usable.  The output is accepted only
after structural preflight *and* a bounded CRC/content pass over every entry.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from neocortex.platform.zip_safety import (
    DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
    ZipStructureError,
    inspect_zip_structure,
)
from neocortex.runtime.control.bounded_subprocess import (
    SubprocessOutputLimitError,
    run_bounded_capture,
)


RepairStatus = Literal[
    "not_needed",
    "recovered",
    "partial",
    "corrupt",
    "password",
    "permission",
    "dependency",
    "timeout",
    "budget",
]

DEFAULT_REPAIR_MAX_INPUT_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_REPAIR_MAX_OUTPUT_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_REPAIR_MAX_MEMBER_BYTES = 64 * 1024 * 1024
DEFAULT_REPAIR_MAX_TOTAL_BYTES = 512 * 1024 * 1024
DEFAULT_REPAIR_MAX_MEMBERS = 20_000
DEFAULT_REPAIR_MAX_RATIO = 200.0
DEFAULT_REPAIR_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ZipRepairLimits:
    max_input_bytes: int = DEFAULT_REPAIR_MAX_INPUT_BYTES
    max_output_bytes: int = DEFAULT_REPAIR_MAX_OUTPUT_BYTES
    max_member_bytes: int = DEFAULT_REPAIR_MAX_MEMBER_BYTES
    max_total_uncompressed_bytes: int = DEFAULT_REPAIR_MAX_TOTAL_BYTES
    max_members: int = DEFAULT_REPAIR_MAX_MEMBERS
    max_compression_ratio: float = DEFAULT_REPAIR_MAX_RATIO
    max_central_directory_bytes: int = DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES
    timeout_seconds: float = DEFAULT_REPAIR_TIMEOUT_SECONDS

    def validate(self) -> None:
        for name, value in (
            ("max_input_bytes", self.max_input_bytes),
            ("max_output_bytes", self.max_output_bytes),
            ("max_member_bytes", self.max_member_bytes),
            ("max_total_uncompressed_bytes", self.max_total_uncompressed_bytes),
            ("max_members", self.max_members),
            ("max_central_directory_bytes", self.max_central_directory_bytes),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"ZIP repair {name} must be a positive integer")
        if not isinstance(self.max_compression_ratio, (int, float)) or self.max_compression_ratio <= 0:
            raise ValueError("ZIP repair max_compression_ratio must be positive")
        if not isinstance(self.timeout_seconds, (int, float)) or self.timeout_seconds <= 0:
            raise ValueError("ZIP repair timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class ZipRepairResult:
    source_path: str
    status: RepairStatus
    engine: str | None = None
    candidate_path: str | None = None
    source_sha256: str | None = None
    candidate_sha256: str | None = None
    members: int = 0
    validated_members: int = 0
    actual_uncompressed_bytes: int = 0
    detail: str | None = None
    warnings: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status == "recovered" and self.candidate_path is not None

    @property
    def recovery_status(self) -> str:
        return self.status

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "neocortex.zip-repair/v1",
            "source_path": self.source_path,
            "status": self.status,
            "recovery_status": self.recovery_status,
            "engine": self.engine,
            "candidate_path": self.candidate_path,
            "source_sha256": self.source_sha256,
            "candidate_sha256": self.candidate_sha256,
            "members": self.members,
            "validated_members": self.validated_members,
            "actual_uncompressed_bytes": self.actual_uncompressed_bytes,
            "detail": self.detail,
            "warnings": list(self.warnings),
            "evidence": list(self.evidence),
        }


def _hash_file(path: Path, *, maximum: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb", buffering=0) as source:
        while chunk := source.read(64 * 1024):
            size += len(chunk)
            if size > maximum:
                raise MemoryError(f"file exceeds bounded repair input/output: {maximum}")
            digest.update(chunk)
    return size, digest.hexdigest()


def _classify_tool_error(exc: BaseException, detail: str) -> tuple[RepairStatus, str]:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timeout", "zip repair subprocess exceeded its deadline"
    if isinstance(exc, SubprocessOutputLimitError):
        return "budget", str(exc)
    if isinstance(exc, PermissionError):
        return "permission", str(exc)
    lower = detail.casefold()
    if "password" in lower or "encrypt" in lower:
        return "password", detail
    if isinstance(exc, FileNotFoundError):
        return "dependency", detail
    if isinstance(exc, OSError):
        return "permission", detail
    return "corrupt", detail


def _validate_candidate(
    path: Path,
    *,
    limits: ZipRepairLimits,
    checkpoint: Callable[[], None] | None = None,
) -> tuple[int, int, int, str, tuple[str, ...]]:
    """Validate structure, CRC and bounded decompressed content for all entries."""

    structure = inspect_zip_structure(
        path,
        max_members=limits.max_members,
        max_central_directory_bytes=limits.max_central_directory_bytes,
    )
    output_size = path.stat().st_size
    if output_size > limits.max_output_bytes:
        raise MemoryError("repaired ZIP exceeds max_output_bytes")
    members = 0
    validated = 0
    total = 0
    warnings: list[str] = []
    with zipfile.ZipFile(path) as archive:
        infos = tuple(archive.infolist())
        if len(infos) != structure.members:
            raise ZipStructureError("repaired ZIP entry count changed during validation")
        for info in infos:
            if checkpoint is not None:
                checkpoint()
            members += 1
            if info.flag_bits & 0x1:
                raise PermissionError("repaired ZIP contains an encrypted member")
            if int(info.file_size) > limits.max_member_bytes:
                raise MemoryError("repaired ZIP member exceeds max_member_bytes")
            if info.file_size and float(info.file_size) / float(max(1, info.compress_size)) > limits.max_compression_ratio:
                raise MemoryError("repaired ZIP member exceeds compression-ratio bound")
            if int(info.file_size) > limits.max_total_uncompressed_bytes - total:
                raise MemoryError("repaired ZIP exceeds total uncompressed bound")
            if info.is_dir():
                validated += 1
                continue
            actual = 0
            crc = 0
            with archive.open(info) as source:
                while chunk := source.read(64 * 1024):
                    if checkpoint is not None:
                        checkpoint()
                    actual += len(chunk)
                    if actual > limits.max_member_bytes or actual > limits.max_total_uncompressed_bytes - total:
                        raise MemoryError("repaired ZIP decompression exceeded its bound")
                    crc = zlib.crc32(chunk, crc)
            crc &= 0xFFFFFFFF
            if actual != int(info.file_size) or crc != int(info.CRC):
                raise zipfile.BadZipFile(
                    f"repaired ZIP member {info.filename!r} failed size/CRC validation"
                )
            total += actual
            validated += 1
    digest = _hash_file(path, maximum=limits.max_output_bytes)[1]
    return members, validated, total, digest, tuple(warnings)


def repair_zip_candidate(
    source: str | os.PathLike[str],
    *,
    limits: ZipRepairLimits | None = None,
    output: str | os.PathLike[str] | None = None,
    engines: tuple[Literal["zip-F", "zip-FF"], ...] = ("zip-F", "zip-FF"),
    checkpoint: Callable[[], None] | None = None,
) -> ZipRepairResult:
    """Try bounded offline ``zip -F``/``zip -FF`` candidates in order.

    A valid source is reported as ``not_needed`` and is never copied.  A
    successfully repaired candidate is kept only when ``output`` is supplied;
    otherwise its temporary path is removed before returning and the result is
    explicitly non-accepted.  This avoids dangling paths and prevents callers
    from mistaking a temporary candidate for a published artifact.
    """

    effective = limits or ZipRepairLimits()
    effective.validate()
    source_path = Path(source)
    source_sha256: str | None = None
    try:
        _source_size, source_sha256 = _hash_file(source_path, maximum=effective.max_input_bytes)
        try:
            structure = inspect_zip_structure(
                source_path,
                max_members=effective.max_members,
                max_central_directory_bytes=effective.max_central_directory_bytes,
            )
        except (ZipStructureError, zipfile.BadZipFile, OSError, zlib.error):
            structure = None
        if structure is not None:
            try:
                _validate_candidate(source_path, limits=effective, checkpoint=checkpoint)
            except PermissionError as exc:
                return ZipRepairResult(str(source_path), "password", source_sha256=source_sha256, detail=str(exc), evidence=("source_encrypted",))
            except (MemoryError, TimeoutError) as exc:
                status: RepairStatus = "budget" if isinstance(exc, MemoryError) else "timeout"
                return ZipRepairResult(str(source_path), status, source_sha256=source_sha256, detail=str(exc), evidence=("source_validation_bounded",))
            except BaseException:
                # A structurally valid but CRC-corrupt source remains a repair
                # candidate.  Do not classify it as already valid.
                pass
            else:
                return ZipRepairResult(
                    str(source_path),
                    "not_needed",
                    engine="none",
                    source_sha256=source_sha256,
                    members=structure.members,
                    validated_members=structure.members,
                    evidence=("source_structurally_and_content_valid",),
                )
    except MemoryError as exc:
        return ZipRepairResult(str(source_path), "budget", source_sha256=source_sha256, detail=str(exc))
    except PermissionError as exc:
        return ZipRepairResult(str(source_path), "permission", source_sha256=source_sha256, detail=str(exc))
    except OSError as exc:
        return ZipRepairResult(str(source_path), "permission", source_sha256=source_sha256, detail=str(exc))

    executable = shutil.which("zip")
    if executable is None:
        return ZipRepairResult(
            str(source_path),
            "dependency",
            source_sha256=source_sha256,
            detail="zip repair dependency is unavailable",
            evidence=("zip_command_not_found",),
        )
    requested_output = None if output is None else Path(output)
    if requested_output is not None:
        requested_output.parent.mkdir(parents=True, exist_ok=True)
        if requested_output.exists():
            return ZipRepairResult(
                str(source_path),
                "permission",
                source_sha256=source_sha256,
                detail="repair output exists; no-replace policy refuses overwrite",
                evidence=("output_exists",),
            )

    with tempfile.TemporaryDirectory(prefix="neocortex_zip_repair_") as directory:
        temporary_root = Path(directory)
        last_detail = "no repair engine produced a candidate"
        warnings: list[str] = []
        deadline = time.monotonic() + effective.timeout_seconds
        for engine in engines:
            if checkpoint is not None:
                checkpoint()
            if time.monotonic() > deadline:
                last_detail = "ZIP repair deadline exhausted"
                break
            candidate = temporary_root / f"candidate-{engine}.zip"
            command = (
                executable,
                "-F" if engine == "zip-F" else "-FF",
                os.fspath(source_path),
                "--out",
                os.fspath(candidate),
            )
            try:
                completed = run_bounded_capture(
                    command,
                    timeout_seconds=max(0.01, deadline - time.monotonic()),
                    stdout_limit_bytes=128 * 1024,
                    stderr_limit_bytes=256 * 1024,
                    cwd=str(temporary_root),
                    environment={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"},
                )
            except BaseException as exc:
                status, detail = _classify_tool_error(exc, str(exc))
                last_detail = detail
                if status == "timeout":
                    return ZipRepairResult(str(source_path), "timeout", engine=engine, source_sha256=source_sha256, detail=detail)
                if status in {"permission", "password"}:
                    warnings.append(detail)
                continue
            stderr = completed.stderr.decode("utf-8", "replace")[-4_000:]
            stdout = completed.stdout.decode("utf-8", "replace")[-2_000:]
            if stderr:
                warnings.append(stderr)
            if completed.returncode != 0 or not candidate.is_file():
                last_detail = (stderr or stdout or f"zip {engine} exited {completed.returncode}")[:2_000]
                if "password" in last_detail.casefold() or "encrypt" in last_detail.casefold():
                    return ZipRepairResult(str(source_path), "password", engine=engine, source_sha256=source_sha256, detail=last_detail, warnings=tuple(warnings))
                continue
            try:
                members, validated, total, candidate_sha256, validation_warnings = _validate_candidate(
                    candidate,
                    limits=effective,
                    checkpoint=checkpoint,
                )
            except subprocess.TimeoutExpired as exc:
                return ZipRepairResult(str(source_path), "timeout", engine=engine, source_sha256=source_sha256, detail=str(exc), warnings=tuple(warnings))
            except PermissionError as exc:
                return ZipRepairResult(str(source_path), "password", engine=engine, source_sha256=source_sha256, detail=str(exc), warnings=tuple(warnings))
            except MemoryError as exc:
                return ZipRepairResult(str(source_path), "budget", engine=engine, source_sha256=source_sha256, detail=str(exc), warnings=tuple(warnings))
            except (ZipStructureError, zipfile.BadZipFile, OSError, RuntimeError, zlib.error) as exc:
                last_detail = f"{type(exc).__name__}: {exc}"[:2_000]
                continue
            warnings.extend(validation_warnings)
            if requested_output is None:
                # Validation succeeded, but no durable destination was
                # provided.  Report partial rather than returning a dead path.
                return ZipRepairResult(
                    str(source_path),
                    "partial",
                    engine=engine,
                    source_sha256=source_sha256,
                    candidate_sha256=candidate_sha256,
                    members=members,
                    validated_members=validated,
                    actual_uncompressed_bytes=total,
                    detail="candidate validated but no output destination was requested",
                    warnings=tuple(warnings),
                    evidence=("candidate_validated_not_published",),
                )
            try:
                # Same-filesystem no-replace publication.  If a caller supplied
                # a cross-filesystem destination, copy through O_EXCL so no
                # existing file can be replaced.
                try:
                    os.link(candidate, requested_output)
                except OSError as exc:
                    if exc.errno != getattr(os, "EXDEV", 18):
                        raise
                    descriptor = os.open(requested_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    try:
                        with os.fdopen(descriptor, "wb") as destination_stream, candidate.open("rb") as candidate_stream:
                            shutil.copyfileobj(candidate_stream, destination_stream, 64 * 1024)
                            destination_stream.flush()
                            os.fsync(destination_stream.fileno())
                    except BaseException:
                        try:
                            requested_output.unlink()
                        except OSError:
                            pass
                        raise
                final_size, final_sha256 = _hash_file(requested_output, maximum=effective.max_output_bytes)
                if final_size != candidate.stat().st_size or final_sha256 != candidate_sha256:
                    try:
                        requested_output.unlink()
                    except OSError:
                        pass
                    return ZipRepairResult(str(source_path), "partial", engine=engine, source_sha256=source_sha256, detail="published repair candidate changed during final verification", warnings=tuple(warnings))
            except FileExistsError:
                return ZipRepairResult(str(source_path), "permission", engine=engine, source_sha256=source_sha256, detail="repair output was created concurrently; no replacement performed", warnings=tuple(warnings))
            except (OSError, MemoryError) as exc:
                return ZipRepairResult(str(source_path), "permission" if isinstance(exc, OSError) else "budget", engine=engine, source_sha256=source_sha256, detail=str(exc), warnings=tuple(warnings))
            return ZipRepairResult(
                str(source_path),
                "recovered",
                engine=engine,
                candidate_path=str(requested_output),
                source_sha256=source_sha256,
                candidate_sha256=candidate_sha256,
                members=members,
                validated_members=validated,
                actual_uncompressed_bytes=total,
                warnings=tuple(warnings),
                evidence=("structural_preflight_validated", "all_entries_crc_validated", "no_replace_published"),
            )
    return ZipRepairResult(str(source_path), "corrupt", source_sha256=source_sha256, detail=last_detail, warnings=tuple(warnings), evidence=("repair_candidates_rejected",))


# Short aliases used by callers that refer to the operation as recovery.
repair_zip = repair_zip_candidate
recover_zip_candidate = repair_zip_candidate


__all__ = (
    "RepairStatus",
    "ZipRepairLimits",
    "ZipRepairResult",
    "recover_zip_candidate",
    "repair_zip",
    "repair_zip_candidate",
)
