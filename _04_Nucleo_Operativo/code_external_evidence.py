"""Bounded external code evidence over inventory-owned immutable versions.

Ruff is deliberately not a ``LanguageAnalyzer``: it observes a complete set of
published Python files after the internal graph is ready.  The adapter never
loads project configuration, imports observed code, applies fixes, or walks a
directory independently.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal

from .bounded_subprocess import SubprocessOutputLimitError, run_bounded_capture
from .semantic_models import canonical_json, fingerprint_bytes, fingerprint_text


EXTERNAL_EVIDENCE_SCHEMA = "neocortex.external-code-evidence/v1"
RUFF_ADAPTER = "neocortex-ruff-lint-v1"
RUFF_SOURCE = "external:ruff"
RUFF_TOOL_NAME = "ruff"
RUFF_RULES = "E4,E7,E9,F"
RUFF_MAX_FILES = 2_000
RUFF_MAX_TOTAL_BYTES = 512 * 1024 * 1024
RUFF_MAX_DIAGNOSTICS = 10_000
RUFF_MAX_RESULT_BYTES = 4 * 1024 * 1024
RUFF_MAX_PROVENANCE_BYTES = 8 * 1024 * 1024
RUFF_STDOUT_LIMIT_BYTES = 8 * 1024 * 1024
RUFF_STDERR_LIMIT_BYTES = 64 * 1024
RUFF_BATCH_FILES = 50
RUFF_BATCH_ARGV_CHARS = 24 * 1024
RUFF_BATCH_TIMEOUT_SECONDS = 30.0
RUFF_TOTAL_TIMEOUT_SECONDS = 180.0
RUFF_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024

ExternalRunStatus = Literal["completed", "failed", "timeout", "unavailable", "skipped"]


@dataclass(frozen=True, slots=True)
class ExternalEvidenceFile:
    """One current immutable Code version authorized as external input."""

    version_id: int
    path: str
    relative_path: str
    size: int
    mtime_ns: int
    raw_xxh3_128: str
    raw_xxh3_64_guard: str

    def signature_payload(self) -> dict[str, object]:
        return {
            "version_id": self.version_id,
            "path": self.relative_path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "raw_xxh3_128": self.raw_xxh3_128,
            "raw_xxh3_64_guard": self.raw_xxh3_64_guard,
        }


def _external_file_sort_key(item: ExternalEvidenceFile) -> tuple[object, ...]:
    return (item.relative_path.casefold(), item.relative_path, item.version_id)


@dataclass(frozen=True, slots=True)
class ExternalDiagnostic:
    """One exact tool report mapped to a current immutable file version."""

    version_id: int
    relative_path: str
    code: str
    message: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    url: str | None
    fix_available: bool
    identity: str

    def result_payload(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "version_id": self.version_id,
            "path": self.relative_path,
            "code": self.code,
            "message": self.message,
            "start_line": self.start_line,
            "start_column": self.start_column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "url": self.url,
            "fix_available": self.fix_available,
        }


@dataclass(frozen=True, slots=True)
class ExternalEvidenceBaseline:
    """Comparable prior publication retained in one bounded provenance row."""

    tool_run_id: int
    analysis_run_id: int
    tool_version: str
    configuration_signature: str
    root: str
    input_signature: str
    version_ids: tuple[int, ...]
    result_digest: str
    diagnostic_ids: tuple[str, ...]
    records: tuple[ExternalDiagnostic, ...] | None


@dataclass(frozen=True, slots=True)
class ExternalEvidencePublication:
    """Terminal Ruff attempt ready for atomic owner publication."""

    tool_name: str
    tool_version: str
    configuration_signature: str
    status: ExternalRunStatus
    started_ns: int
    completed_ns: int
    provenance: Mapping[str, object]
    diagnostics: tuple[ExternalDiagnostic, ...] = ()

    @property
    def execution(self) -> str:
        value = self.provenance.get("execution")
        return value if isinstance(value, str) else "unknown"

    @property
    def diagnostic_count(self) -> int:
        result = self.provenance.get("result")
        if not isinstance(result, Mapping):
            return 0
        value = result.get("diagnostics")
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    @property
    def added_count(self) -> int:
        result = self.provenance.get("result")
        if not isinstance(result, Mapping):
            return 0
        value = result.get("added")
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    @property
    def resolved_count(self) -> int:
        result = self.provenance.get("result")
        if not isinstance(result, Mapping):
            return 0
        value = result.get("resolved")
        return value if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass(frozen=True, slots=True)
class ExternalEvidenceStatus:
    """Bounded read model exposed by status, review and publication diff."""

    status: Literal["ready", "abstained", "not_recorded"]
    reason: str | None
    provider: str
    tool_run_id: int | None
    effective_tool_run_id: int | None
    tool_status: str | None
    tool_version: str | None
    configuration_signature: str | None
    input_signature: str | None
    execution: str | None
    eligible_files: int
    covered_files: int
    diagnostics: int
    added: int | None
    resolved: int | None
    comparable: bool
    result_digest: str | None
    gate: Literal["passed", "failed", "baseline", "not_evaluated"]
    authority: Literal["advisory"] = "advisory"
    mutation_authority: bool = False
    content_executed: bool = False

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": EXTERNAL_EVIDENCE_SCHEMA,
            "status": self.status,
            "reason": self.reason,
            "provider": self.provider,
            "tool_run_id": self.tool_run_id,
            "effective_tool_run_id": self.effective_tool_run_id,
            "tool_status": self.tool_status,
            "tool_version": self.tool_version,
            "configuration_signature": self.configuration_signature,
            "input_signature": self.input_signature,
            "execution": self.execution,
            "eligible_files": self.eligible_files,
            "covered_files": self.covered_files,
            "diagnostics": self.diagnostics,
            "added": self.added,
            "resolved": self.resolved,
            "comparable": self.comparable,
            "result_digest": self.result_digest,
            "gate": self.gate,
            "authority": self.authority,
            "mutation_authority": self.mutation_authority,
            "content_executed": self.content_executed,
        }


def _configuration_payload() -> dict[str, object]:
    return {
        "schema": EXTERNAL_EVIDENCE_SCHEMA,
        "adapter": RUFF_ADAPTER,
        "target_version": "py313",
        "rules": RUFF_RULES,
        "configuration": "isolated",
        "cache": "disabled",
        "fixes": "unfixable-all",
        "input": "inventory-current-python-with-exact-fingerprint",
        "input_materialization": "verified-staged-copy-v1",
        "staging_root_policy": "explicit-disjoint-state-or-validated-temp-v1",
        "batch_files": RUFF_BATCH_FILES,
        "batch_argv_chars": RUFF_BATCH_ARGV_CHARS,
        "batch_timeout_seconds": RUFF_BATCH_TIMEOUT_SECONDS,
        "total_timeout_seconds": RUFF_TOTAL_TIMEOUT_SECONDS,
        "stdout_limit_bytes": RUFF_STDOUT_LIMIT_BYTES,
        "stderr_limit_bytes": RUFF_STDERR_LIMIT_BYTES,
        "memory_limit_bytes": RUFF_MEMORY_LIMIT_BYTES,
        "max_files": RUFF_MAX_FILES,
        "max_total_bytes": RUFF_MAX_TOTAL_BYTES,
        "max_diagnostics": RUFF_MAX_DIAGNOSTICS,
        "max_result_bytes": RUFF_MAX_RESULT_BYTES,
        "max_provenance_bytes": RUFF_MAX_PROVENANCE_BYTES,
        "environment_policy": "minimal-os-python-isolated-v1",
    }


def _configuration_signature(payload: Mapping[str, object]) -> str:
    return "external-ruff-v1:xxh3_128:" + fingerprint_text(canonical_json(payload)).xxh3_128


RUFF_CONFIGURATION_SIGNATURE = _configuration_signature(_configuration_payload())


def external_input_signature(files: Sequence[ExternalEvidenceFile]) -> str:
    ordered = sorted(files, key=_external_file_sort_key)
    payload = canonical_json({"files": [item.signature_payload() for item in ordered]})
    return "external-input-v1:xxh3_128:" + fingerprint_text(payload).xxh3_128


def _controlled_environment() -> dict[str, str]:
    allowed = {
        "comspec",
        "lang",
        "lc_all",
        "systemroot",
        "temp",
        "tmp",
        "tmpdir",
        "windir",
    }
    environment = {key: value for key, value in os.environ.items() if key.casefold() in allowed}
    environment.update(
        {
            "NO_COLOR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    return environment


def _ruff_argv_prefix() -> tuple[str, ...]:
    return (
        sys.executable,
        "-I",
        "-m",
        "ruff",
        "check",
        "--isolated",
        "--target-version",
        "py313",
        "--select",
        RUFF_RULES,
        "--no-preview",
        "--output-format",
        "json",
        "--no-cache",
        "--no-fix",
        "--no-fix-only",
        "--no-unsafe-fixes",
        "--no-show-fixes",
        "--config",
        "lint.unfixable = ['ALL']",
        "--color",
        "never",
    )


def _batches(paths: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    batches: list[tuple[str, ...]] = []
    current: list[str] = []
    current_chars = 0
    for path in paths:
        argument_chars = len(path) + 3
        if current and (
            len(current) >= RUFF_BATCH_FILES
            or current_chars + argument_chars > RUFF_BATCH_ARGV_CHARS
        ):
            batches.append(tuple(current))
            current = []
            current_chars = 0
        current.append(path)
        current_chars += argument_chars
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def _read_exact_current_file(item: ExternalEvidenceFile) -> bytes:
    path = Path(item.path)
    metadata = os.lstat(path)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    if path.is_symlink() or attributes & reparse or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Ruff input is not a regular non-reparse file: {item.path}")
    if metadata.st_size != item.size or metadata.st_mtime_ns != item.mtime_ns:
        raise ValueError(f"Ruff input changed since Code publication: {item.path}")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if before.st_size != item.size or before.st_mtime_ns != item.mtime_ns:
            raise ValueError(f"Ruff input changed before bounded read: {item.path}")
        chunks: list[bytes] = []
        remaining = item.size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError(f"Ruff input ended before published size: {item.path}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError(f"Ruff input exceeds published size: {item.path}")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or after.st_size != item.size
        or after.st_mtime_ns != item.mtime_ns
    ):
        raise ValueError(f"Ruff input changed during bounded read: {item.path}")
    content = b"".join(chunks)
    observed = fingerprint_bytes(content)
    if observed.xxh3_128 != item.raw_xxh3_128 or observed.xxh3_64_guard != item.raw_xxh3_64_guard:
        raise ValueError(f"Ruff input fingerprint is stale: {item.path}")
    return content


def validate_external_inputs(files: Sequence[ExternalEvidenceFile]) -> None:
    if len(files) > RUFF_MAX_FILES:
        raise ValueError(f"Ruff input exceeds {RUFF_MAX_FILES} files")
    total_bytes = sum(item.size for item in files)
    if total_bytes > RUFF_MAX_TOTAL_BYTES:
        raise ValueError(f"Ruff input exceeds {RUFF_MAX_TOTAL_BYTES} bytes")
    for item in files:
        _read_exact_current_file(item)


def _stage_external_inputs(
    files: Sequence[ExternalEvidenceFile],
    stage_root: Path,
) -> dict[str, ExternalEvidenceFile]:
    if len(files) > RUFF_MAX_FILES:
        raise ValueError(f"Ruff input exceeds {RUFF_MAX_FILES} files")
    if sum(item.size for item in files) > RUFF_MAX_TOTAL_BYTES:
        raise ValueError(f"Ruff input exceeds {RUFF_MAX_TOTAL_BYTES} bytes")
    staged: dict[str, ExternalEvidenceFile] = {}
    normalized_stage_root = os.path.normcase(os.path.abspath(stage_root))
    for item in files:
        relative = PurePosixPath(item.relative_path)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} or ":" in part for part in relative.parts)
        ):
            raise ValueError("Ruff staged relative path is invalid")
        destination = stage_root.joinpath(*relative.parts)
        normalized_destination = os.path.normcase(os.path.abspath(destination))
        if (
            os.path.commonpath((normalized_stage_root, normalized_destination))
            != normalized_stage_root
        ):
            raise ValueError("Ruff staged path escapes its temporary root")
        if normalized_destination in staged:
            raise ValueError("Ruff staged path is duplicated")
        content = _read_exact_current_file(item)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(content)
        staged_fingerprint = fingerprint_bytes(destination.read_bytes())
        if (
            staged_fingerprint.xxh3_128 != item.raw_xxh3_128
            or staged_fingerprint.xxh3_64_guard != item.raw_xxh3_64_guard
        ):
            raise ValueError("Ruff staged copy fingerprint is invalid")
        staged[normalized_destination] = item
    return staged


def _bounded_text(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"Ruff {label} is invalid")
    return value


def _location(value: object, *, label: str) -> tuple[int, int]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Ruff {label} is missing")
    row = value.get("row")
    column = value.get("column")
    if (
        isinstance(row, bool)
        or not isinstance(row, int)
        or row < 1
        or isinstance(column, bool)
        or not isinstance(column, int)
        or column < 1
    ):
        raise ValueError(f"Ruff {label} is invalid")
    return row, column - 1


def _external_diagnostic_identity(
    relative_path: str,
    code: str,
    message: str,
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
) -> str:
    identity_payload = canonical_json(
        {
            "path": relative_path,
            "code": code,
            "message": message,
            "start_line": start_line,
            "start_column": start_column,
            "end_line": end_line,
            "end_column": end_column,
        }
    )
    return "ruff-diagnostic-v1:xxh3_128:" + fingerprint_text(identity_payload).xxh3_128


def _normalized_absolute_path(value: str) -> str | None:
    """Normalize persisted paths without allowing malformed state to escape."""

    try:
        if not os.path.isabs(value):
            return None
        return os.path.normcase(os.path.abspath(value))
    except (OSError, TypeError, ValueError):
        return None


def _diagnostic_sort_key(item: ExternalDiagnostic) -> tuple[object, ...]:
    return (
        item.relative_path.casefold(),
        item.code,
        item.start_line,
        item.start_column,
        item.end_line,
        item.end_column,
        item.message,
        item.identity,
    )


def _external_result_digest(records: Sequence[ExternalDiagnostic]) -> str:
    portable = [
        {key: value for key, value in item.result_payload().items() if key != "version_id"}
        for item in records
    ]
    canonical = canonical_json({"diagnostics": portable})
    if len(canonical.encode("utf-8")) > RUFF_MAX_RESULT_BYTES:
        raise ValueError("Ruff canonical result exceeds its bound")
    return "external-result-v1:xxh3_128:" + fingerprint_text(canonical).xxh3_128


def _decode_result_record(raw: object) -> ExternalDiagnostic | None:
    if not isinstance(raw, dict):
        return None
    version_id = raw.get("version_id")
    relative_path = raw.get("path")
    code = raw.get("code")
    message = raw.get("message")
    start_line = raw.get("start_line")
    start_column = raw.get("start_column")
    end_line = raw.get("end_line")
    end_column = raw.get("end_column")
    url = raw.get("url")
    fix_available = raw.get("fix_available")
    identity = raw.get("identity")
    if (
        not isinstance(version_id, int)
        or isinstance(version_id, bool)
        or version_id <= 0
        or not isinstance(relative_path, str)
        or not relative_path
        or len(relative_path.encode("utf-8")) > 32_768
        or "\\" in relative_path
        or PurePosixPath(relative_path).is_absolute()
        or any(
            part in {"", ".", ".."} or ":" in part for part in PurePosixPath(relative_path).parts
        )
        or not isinstance(code, str)
        or not code
        or len(code.encode("utf-8")) > 128
        or not isinstance(message, str)
        or not message
        or len(message.encode("utf-8")) > 2_048
        or not isinstance(start_line, int)
        or isinstance(start_line, bool)
        or start_line < 1
        or not isinstance(start_column, int)
        or isinstance(start_column, bool)
        or start_column < 0
        or not isinstance(end_line, int)
        or isinstance(end_line, bool)
        or end_line < start_line
        or not isinstance(end_column, int)
        or isinstance(end_column, bool)
        or end_column < 0
        or (end_line == start_line and end_column < start_column)
        or (url is not None and not isinstance(url, str))
        or (isinstance(url, str) and len(url.encode("utf-8")) > 2_048)
        or not isinstance(fix_available, bool)
        or not isinstance(identity, str)
        or len(identity.encode("utf-8")) > 256
    ):
        return None
    observed_identity = _external_diagnostic_identity(
        relative_path,
        code,
        message,
        start_line,
        start_column,
        end_line,
        end_column,
    )
    if observed_identity != identity:
        return None
    return ExternalDiagnostic(
        version_id,
        relative_path,
        code,
        message,
        start_line,
        start_column,
        end_line,
        end_column,
        url,
        fix_available,
        identity,
    )


def read_external_evidence_files(
    connection: sqlite3.Connection,
    root: Path | str,
) -> tuple[ExternalEvidenceFile, ...]:
    """Read the bounded current Python input projection without touching files."""

    root_text = str(root)
    normalized_root = _normalized_absolute_path(root_text)
    if normalized_root is None:
        raise ValueError("external evidence owner root is invalid")
    rows = connection.execute(
        """SELECT v.version_id,f.current_path,v.size,v.mtime_ns,
        v.raw_xxh3_128,v.raw_xxh3_64_guard
        FROM files f JOIN file_versions v ON v.version_id=f.current_version_id
        WHERE f.status='current' AND v.invalidated_ns IS NULL
        AND v.language='python' AND v.generated=0 AND v.vendored=0
        ORDER BY f.current_path COLLATE NOCASE,v.version_id LIMIT ?""",
        (RUFF_MAX_FILES + 1,),
    ).fetchall()
    if len(rows) > RUFF_MAX_FILES:
        raise ValueError(f"external evidence exceeds {RUFF_MAX_FILES} files")
    files: list[ExternalEvidenceFile] = []
    seen_paths: set[str] = set()
    total_bytes = 0
    for row in rows:
        path = str(row["current_path"])
        normalized_path = _normalized_absolute_path(path)
        if normalized_path is None:
            raise ValueError("external evidence path is invalid")
        try:
            common = os.path.commonpath((normalized_root, normalized_path))
            relative_path = os.path.relpath(path, root_text).replace("\\", "/")
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("external evidence path is outside the owner root") from exc
        if common != normalized_root or relative_path == ".." or relative_path.startswith("../"):
            raise ValueError("external evidence path is outside the owner root")
        if normalized_path in seen_paths:
            raise ValueError("external evidence contains duplicate current paths")
        seen_paths.add(normalized_path)
        version_id = row["version_id"]
        size = row["size"]
        mtime_ns = row["mtime_ns"]
        raw_xxh3_128 = row["raw_xxh3_128"]
        raw_xxh3_64_guard = row["raw_xxh3_64_guard"]
        if (
            not isinstance(version_id, int)
            or isinstance(version_id, bool)
            or version_id <= 0
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(mtime_ns, int)
            or isinstance(mtime_ns, bool)
            or mtime_ns < 0
            or not isinstance(raw_xxh3_128, str)
            or not isinstance(raw_xxh3_64_guard, str)
        ):
            raise ValueError("external evidence requires exact current fingerprints")
        total_bytes += size
        if total_bytes > RUFF_MAX_TOTAL_BYTES:
            raise ValueError(f"external evidence exceeds {RUFF_MAX_TOTAL_BYTES} bytes")
        files.append(
            ExternalEvidenceFile(
                version_id,
                path,
                relative_path,
                size,
                mtime_ns,
                raw_xxh3_128,
                raw_xxh3_64_guard,
            )
        )
    return tuple(sorted(files, key=_external_file_sort_key))


def _parse_diagnostics(
    raw: bytes,
    files_by_path: Mapping[str, ExternalEvidenceFile],
) -> tuple[ExternalDiagnostic, ...]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Ruff JSON output is malformed") from exc
    if not isinstance(decoded, list):
        raise ValueError("Ruff JSON output must be an array")
    if len(decoded) > RUFF_MAX_DIAGNOSTICS:
        raise ValueError(f"Ruff output exceeds {RUFF_MAX_DIAGNOSTICS} diagnostics")
    diagnostics: list[ExternalDiagnostic] = []
    for raw_item in decoded:
        if not isinstance(raw_item, Mapping):
            raise ValueError("Ruff diagnostic is not an object")
        filename = _bounded_text(raw_item.get("filename"), label="filename", maximum=32_768)
        normalized = os.path.normcase(os.path.abspath(filename))
        owner = files_by_path.get(normalized)
        if owner is None:
            raise ValueError(f"Ruff reported an unowned path: {filename}")
        code = _bounded_text(raw_item.get("code"), label="code", maximum=128)
        message = _bounded_text(raw_item.get("message"), label="message", maximum=2_048)
        start_line, start_column = _location(raw_item.get("location"), label="location")
        end_line, end_column = _location(raw_item.get("end_location"), label="end_location")
        if end_line < start_line or (end_line == start_line and end_column < start_column):
            raise ValueError("Ruff diagnostic range is invalid")
        url_value = raw_item.get("url")
        url = None if url_value is None else _bounded_text(url_value, label="url", maximum=2_048)
        identity = _external_diagnostic_identity(
            owner.relative_path,
            code,
            message,
            start_line,
            start_column,
            end_line,
            end_column,
        )
        diagnostics.append(
            ExternalDiagnostic(
                owner.version_id,
                owner.relative_path,
                code,
                message,
                start_line,
                start_column,
                end_line,
                end_column,
                url,
                raw_item.get("fix") is not None,
                identity,
            )
        )
    diagnostics.sort(key=_diagnostic_sort_key)
    if len({item.identity for item in diagnostics}) != len(diagnostics):
        raise ValueError("Ruff output contains duplicate diagnostic identities")
    return tuple(diagnostics)


def _base_provenance(
    root: Path,
    files: Sequence[ExternalEvidenceFile],
    input_signature: str,
    *,
    execution: str,
) -> dict[str, object]:
    return {
        "schema": EXTERNAL_EVIDENCE_SCHEMA,
        "adapter": RUFF_ADAPTER,
        "root": str(root),
        "execution": execution,
        "configuration": _configuration_payload(),
        "input": {
            "signature": input_signature,
            "eligible_files": len(files),
            "total_bytes": sum(item.size for item in files),
            "version_ids": [item.version_id for item in files],
        },
        "mutation_authority": False,
        "content_executed": False,
    }


def _failure_publication(
    root: Path,
    files: Sequence[ExternalEvidenceFile],
    input_signature: str,
    tool_version: str,
    status: ExternalRunStatus,
    reason: str,
    started_ns: int,
    *,
    error: BaseException | None = None,
) -> ExternalEvidencePublication:
    provenance = _base_provenance(
        root,
        files,
        input_signature,
        execution="attempted" if status != "unavailable" else "unavailable",
    )
    provenance["error"] = {
        "reason": reason,
        "type": None if error is None else type(error).__name__,
        "message": None if error is None else str(error)[:4_096],
    }
    return ExternalEvidencePublication(
        RUFF_TOOL_NAME,
        tool_version,
        RUFF_CONFIGURATION_SIGNATURE,
        status,
        started_ns,
        time.time_ns(),
        provenance,
    )


def skipped_external_publication(
    root: Path,
    files: Sequence[ExternalEvidenceFile],
    *,
    reason: str,
) -> ExternalEvidencePublication:
    """Record why a protected Code run could not publish project-wide evidence."""

    ordered = tuple(sorted(files, key=_external_file_sort_key))
    now = time.time_ns()
    version = RuffEvidenceProvider.tool_version() or "unavailable"
    provenance = _base_provenance(
        root,
        ordered,
        external_input_signature(ordered),
        execution="skipped",
    )
    provenance["error"] = {"reason": reason, "type": None, "message": None}
    return ExternalEvidencePublication(
        RUFF_TOOL_NAME,
        version,
        RUFF_CONFIGURATION_SIGNATURE,
        "skipped",
        now,
        now,
        provenance,
    )


def failed_external_publication(
    root: Path,
    *,
    reason: str,
    error: BaseException,
) -> ExternalEvidencePublication:
    """Fail only external evidence when its bounded input projection is unsafe."""

    started_ns = time.time_ns()
    version = RuffEvidenceProvider.tool_version() or "unavailable"
    return _failure_publication(
        root,
        (),
        external_input_signature(()),
        version,
        "failed",
        reason,
        started_ns,
        error=error,
    )


@dataclass(frozen=True, slots=True)
class _ExternalAttemptFailure:
    status: Literal["failed", "timeout"]
    reason: str
    error: BaseException | None = None


def _run_staged_batches(
    stage_root: Path,
    staged_paths: Sequence[str],
) -> tuple[list[bytes], tuple[tuple[str, ...], ...]] | _ExternalAttemptFailure:
    if not staged_paths:
        return [b"[]"], ()
    outputs: list[bytes] = []
    argv_batches = _batches(staged_paths)
    deadline = time.monotonic() + RUFF_TOTAL_TIMEOUT_SECONDS
    stdout_bytes = 0
    stderr_bytes = 0
    environment = _controlled_environment()
    for name in ("TEMP", "TMP", "TMPDIR"):
        environment[name] = str(stage_root)
    for paths in argv_batches:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            return _ExternalAttemptFailure("timeout", "total_timeout")
        command = (*_ruff_argv_prefix(), *paths)
        try:
            completed = run_bounded_capture(
                command,
                timeout_seconds=min(
                    RUFF_BATCH_TIMEOUT_SECONDS,
                    remaining_seconds,
                ),
                stdout_limit_bytes=max(
                    0,
                    RUFF_STDOUT_LIMIT_BYTES - stdout_bytes,
                ),
                stderr_limit_bytes=max(
                    0,
                    RUFF_STDERR_LIMIT_BYTES - stderr_bytes,
                ),
                cwd=stage_root,
                environment=environment,
                memory_limit_bytes=(RUFF_MEMORY_LIMIT_BYTES if os.name == "nt" else None),
            )
        except subprocess.TimeoutExpired as exc:
            return _ExternalAttemptFailure("timeout", "batch_timeout", exc)
        except SubprocessOutputLimitError as exc:
            return _ExternalAttemptFailure("failed", "output_limit", exc)
        except (OSError, RuntimeError, ValueError) as exc:
            return _ExternalAttemptFailure("failed", "subprocess_failure", exc)
        stdout_bytes += len(completed.stdout)
        stderr_bytes += len(completed.stderr)
        if completed.returncode not in {0, 1}:
            return _ExternalAttemptFailure(
                "failed",
                f"unexpected_exit:{completed.returncode}",
            )
        outputs.append(completed.stdout)
    return outputs, argv_batches


def _validated_staging_parent(root: Path, scratch_root: Path | None) -> Path:
    try:
        owner = root.resolve(strict=True)
        selected = (Path(tempfile.gettempdir()) if scratch_root is None else scratch_root).resolve(
            strict=True
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("Ruff staging parent cannot be resolved") from exc
    if not selected.is_dir():
        raise ValueError("Ruff staging parent is not a directory")
    normalized_owner = os.path.normcase(os.path.abspath(owner))
    normalized_selected = os.path.normcase(os.path.abspath(selected))
    try:
        common = os.path.commonpath((normalized_owner, normalized_selected))
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("Ruff staging parent is incompatible with owner root") from exc
    if common == normalized_owner:
        raise ValueError("Ruff staging parent is inside the owner root")
    return selected


class RuffEvidenceProvider:
    """Fixed Ruff adapter for the protected self-analysis owner root."""

    tool_name = RUFF_TOOL_NAME
    configuration_signature = RUFF_CONFIGURATION_SIGNATURE

    @staticmethod
    def tool_version() -> str | None:
        try:
            version = importlib.metadata.version("ruff")
        except importlib.metadata.PackageNotFoundError:
            return None
        return version if version and len(version.encode("utf-8")) <= 256 else None

    def run(
        self,
        root: Path,
        files: Sequence[ExternalEvidenceFile],
        *,
        baseline: ExternalEvidenceBaseline | None,
        scratch_root: Path | None = None,
    ) -> ExternalEvidencePublication:
        ordered = tuple(sorted(files, key=_external_file_sort_key))
        input_signature = external_input_signature(ordered)
        started_ns = time.time_ns()
        version = self.tool_version()
        if version is None:
            return _failure_publication(
                root,
                ordered,
                input_signature,
                "unavailable",
                "unavailable",
                "ruff_distribution_missing",
                started_ns,
            )
        try:
            staging_parent = _validated_staging_parent(root, scratch_root)
        except (OSError, ValueError) as exc:
            return _failure_publication(
                root,
                ordered,
                input_signature,
                version,
                "failed",
                "unsafe_staging_root",
                started_ns,
                error=exc,
            )
        try:
            with tempfile.TemporaryDirectory(
                prefix="neocortex-ruff-",
                dir=staging_parent,
            ) as temporary:
                temporary_root = Path(temporary)
                files_by_path = _stage_external_inputs(
                    ordered,
                    temporary_root / "source",
                )
                attempt = _run_staged_batches(
                    temporary_root,
                    tuple(files_by_path),
                )
                if isinstance(attempt, _ExternalAttemptFailure):
                    return _failure_publication(
                        root,
                        ordered,
                        input_signature,
                        version,
                        attempt.status,
                        attempt.reason,
                        started_ns,
                        error=attempt.error,
                    )
                outputs, argv_batches = attempt
        except (OSError, ValueError) as exc:
            return _failure_publication(
                root,
                ordered,
                input_signature,
                version,
                "failed",
                "input_validation_failed",
                started_ns,
                error=exc,
            )
        try:
            diagnostics = tuple(
                item for output in outputs for item in _parse_diagnostics(output, files_by_path)
            )
            diagnostics = tuple(sorted(diagnostics, key=_diagnostic_sort_key))
            if len(diagnostics) > RUFF_MAX_DIAGNOSTICS:
                raise ValueError("Ruff aggregate diagnostic limit exceeded")
            if len({item.identity for item in diagnostics}) != len(diagnostics):
                raise ValueError("Ruff aggregate diagnostics are duplicated")
            validate_external_inputs(ordered)
            diagnostic_payloads = [item.result_payload() for item in diagnostics]
            canonical_records = canonical_json({"diagnostics": diagnostic_payloads})
            if len(canonical_records.encode("utf-8")) > RUFF_MAX_RESULT_BYTES:
                raise ValueError("Ruff canonical result exceeds its bound")
            result_digest = _external_result_digest(diagnostics)
        except (OSError, ValueError) as exc:
            return _failure_publication(
                root,
                ordered,
                input_signature,
                version,
                "failed",
                "result_validation_failed",
                started_ns,
                error=exc,
            )
        identities = tuple(item.identity for item in diagnostics)
        baseline_ids = frozenset(() if baseline is None else baseline.diagnostic_ids)
        current_ids = frozenset(identities)
        comparable = baseline is not None
        provenance = _base_provenance(
            root,
            ordered,
            input_signature,
            execution="full",
        )
        provenance["command"] = {
            "argv_prefix": list(_ruff_argv_prefix()),
            "batch_count": len(argv_batches),
            "max_batch_files": max((len(paths) for paths in argv_batches), default=0),
            "shell": False,
        }
        provenance["result"] = {
            "digest": result_digest,
            "diagnostics": len(diagnostics),
            "diagnostic_ids": list(identities),
            "records": diagnostic_payloads,
            "comparable": comparable,
            "baseline_tool_run_id": None if baseline is None else baseline.tool_run_id,
            "added": None if not comparable else len(current_ids - baseline_ids),
            "resolved": None if not comparable else len(baseline_ids - current_ids),
        }
        if len(canonical_json(provenance).encode("utf-8")) > RUFF_MAX_PROVENANCE_BYTES:
            return _failure_publication(
                root,
                ordered,
                input_signature,
                version,
                "failed",
                "provenance_limit_exceeded",
                started_ns,
            )
        return ExternalEvidencePublication(
            RUFF_TOOL_NAME,
            version,
            RUFF_CONFIGURATION_SIGNATURE,
            "completed",
            started_ns,
            time.time_ns(),
            provenance,
            diagnostics,
        )

    def replay(
        self,
        root: Path,
        files: Sequence[ExternalEvidenceFile],
        baseline: ExternalEvidenceBaseline,
    ) -> ExternalEvidencePublication:
        ordered = tuple(sorted(files, key=_external_file_sort_key))
        started_ns = time.time_ns()
        input_signature = external_input_signature(ordered)
        try:
            validate_external_inputs(ordered)
        except (OSError, ValueError) as exc:
            return _failure_publication(
                root,
                ordered,
                input_signature,
                baseline.tool_version,
                "failed",
                "cache_input_validation_failed",
                started_ns,
                error=exc,
            )
        provenance = _base_provenance(
            root,
            ordered,
            input_signature,
            execution="cache_replay",
        )
        provenance["reused_tool_run_id"] = baseline.tool_run_id
        provenance["result"] = {
            "digest": baseline.result_digest,
            "diagnostics": len(baseline.diagnostic_ids),
            "diagnostic_ids": list(baseline.diagnostic_ids),
            "records": None,
            "comparable": True,
            "baseline_tool_run_id": baseline.tool_run_id,
            "added": 0,
            "resolved": 0,
        }
        if len(canonical_json(provenance).encode("utf-8")) > RUFF_MAX_PROVENANCE_BYTES:
            return _failure_publication(
                root,
                ordered,
                input_signature,
                baseline.tool_version,
                "failed",
                "provenance_limit_exceeded",
                started_ns,
            )
        return ExternalEvidencePublication(
            RUFF_TOOL_NAME,
            baseline.tool_version,
            RUFF_CONFIGURATION_SIGNATURE,
            "skipped",
            started_ns,
            time.time_ns(),
            provenance,
        )


def _decode_external_record(
    tool_run_id: int,
    analysis_run_id: int,
    tool_version: str,
    configuration_signature: str,
    raw_provenance: str,
) -> tuple[dict[str, object], ExternalEvidenceBaseline | None] | None:
    try:
        provenance_bytes = len(raw_provenance.encode("utf-8"))
    except UnicodeError:
        return None
    if provenance_bytes > RUFF_MAX_PROVENANCE_BYTES:
        return None
    try:
        provenance = json.loads(raw_provenance)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(provenance, dict) or provenance.get("schema") != EXTERNAL_EVIDENCE_SCHEMA:
        return None
    input_payload = provenance.get("input")
    result = provenance.get("result")
    root = provenance.get("root")
    configuration = provenance.get("configuration")
    if not isinstance(input_payload, dict) or not isinstance(configuration, dict):
        return None
    try:
        observed_configuration_signature = _configuration_signature(configuration)
    except (TypeError, ValueError):
        return None
    if observed_configuration_signature != configuration_signature:
        return None
    input_signature = input_payload.get("signature")
    eligible_files = input_payload.get("eligible_files")
    total_bytes = input_payload.get("total_bytes")
    version_ids = input_payload.get("version_ids")
    if (
        not isinstance(root, str)
        or not root
        or len(root.encode("utf-8")) > 32_768
        or _normalized_absolute_path(root) is None
        or not isinstance(input_signature, str)
        or not input_signature.startswith("external-input-v1:xxh3_128:")
        or len(input_signature.encode("utf-8")) > 256
        or not isinstance(eligible_files, int)
        or isinstance(eligible_files, bool)
        or not 0 <= eligible_files <= RUFF_MAX_FILES
        or not isinstance(total_bytes, int)
        or isinstance(total_bytes, bool)
        or not 0 <= total_bytes <= RUFF_MAX_TOTAL_BYTES
        or not isinstance(version_ids, list)
        or len(version_ids) != eligible_files
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in version_ids
        )
        or len(set(version_ids)) != len(version_ids)
    ):
        return None
    if result is None:
        return provenance, None
    if not isinstance(result, dict):
        return None
    result_digest = result.get("digest")
    diagnostic_count = result.get("diagnostics")
    diagnostic_ids = result.get("diagnostic_ids")
    raw_records = result.get("records")
    comparable = result.get("comparable")
    baseline_tool_run_id = result.get("baseline_tool_run_id")
    added = result.get("added")
    resolved = result.get("resolved")
    execution = provenance.get("execution")
    if (
        not isinstance(result_digest, str)
        or not result_digest.startswith("external-result-v1:xxh3_128:")
        or len(result_digest.encode("utf-8")) > 256
        or not isinstance(diagnostic_count, int)
        or isinstance(diagnostic_count, bool)
        or not 0 <= diagnostic_count <= RUFF_MAX_DIAGNOSTICS
        or not isinstance(diagnostic_ids, list)
        or len(diagnostic_ids) != diagnostic_count
        or any(not isinstance(item, str) or len(item) > 256 for item in diagnostic_ids)
        or len(set(diagnostic_ids)) != len(diagnostic_ids)
        or not isinstance(comparable, bool)
        or execution not in {"full", "cache_replay"}
    ):
        return None
    if comparable:
        if (
            not isinstance(baseline_tool_run_id, int)
            or isinstance(baseline_tool_run_id, bool)
            or baseline_tool_run_id <= 0
            or not isinstance(added, int)
            or isinstance(added, bool)
            or not 0 <= added <= RUFF_MAX_DIAGNOSTICS
            or not isinstance(resolved, int)
            or isinstance(resolved, bool)
            or not 0 <= resolved <= RUFF_MAX_DIAGNOSTICS
        ):
            return None
    elif baseline_tool_run_id is not None or added is not None or resolved is not None:
        return None
    records: tuple[ExternalDiagnostic, ...] | None
    if execution == "full":
        if not isinstance(raw_records, list) or len(raw_records) != diagnostic_count:
            return None
        decoded_records = tuple(_decode_result_record(item) for item in raw_records)
        if any(item is None for item in decoded_records):
            return None
        records = tuple(item for item in decoded_records if item is not None)
        if records != tuple(sorted(records, key=_diagnostic_sort_key)):
            return None
        if tuple(item.identity for item in records) != tuple(diagnostic_ids):
            return None
        try:
            observed_digest = _external_result_digest(records)
        except (TypeError, ValueError):
            return None
        if observed_digest != result_digest:
            return None
    else:
        reused_tool_run_id = provenance.get("reused_tool_run_id")
        if (
            raw_records is not None
            or not comparable
            or added != 0
            or resolved != 0
            or not isinstance(reused_tool_run_id, int)
            or isinstance(reused_tool_run_id, bool)
            or reused_tool_run_id <= 0
            or reused_tool_run_id != baseline_tool_run_id
        ):
            return None
        records = None
    return (
        provenance,
        ExternalEvidenceBaseline(
            tool_run_id,
            analysis_run_id,
            tool_version,
            configuration_signature,
            root,
            input_signature,
            tuple(version_ids),
            result_digest,
            tuple(diagnostic_ids),
            records,
        ),
    )


def decode_external_baseline(
    tool_run_id: int,
    analysis_run_id: int,
    tool_version: str,
    configuration_signature: str,
    raw_provenance: str,
) -> ExternalEvidenceBaseline | None:
    decoded = _decode_external_record(
        tool_run_id,
        analysis_run_id,
        tool_version,
        configuration_signature,
        raw_provenance,
    )
    return None if decoded is None else decoded[1]


def external_status_from_row(
    row: Mapping[str, object] | None,
) -> ExternalEvidenceStatus:
    if row is None:
        return ExternalEvidenceStatus(
            "not_recorded",
            "external_evidence_not_recorded",
            RUFF_TOOL_NAME,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            0,
            0,
            0,
            None,
            None,
            False,
            None,
            "not_evaluated",
        )
    raw_tool_run_id = row["tool_run_id"]
    raw_analysis_run_id = row["analysis_run_id"]
    if (
        not isinstance(raw_tool_run_id, int)
        or isinstance(raw_tool_run_id, bool)
        or raw_tool_run_id <= 0
        or not isinstance(raw_analysis_run_id, int)
        or isinstance(raw_analysis_run_id, bool)
        or raw_analysis_run_id <= 0
    ):
        return ExternalEvidenceStatus(
            "abstained",
            "external_evidence_row_invalid",
            RUFF_TOOL_NAME,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            0,
            0,
            0,
            None,
            None,
            False,
            None,
            "not_evaluated",
        )
    tool_run_id = raw_tool_run_id
    tool_status = str(row["status"])
    tool_version = str(row["tool_version"])
    configuration_signature = str(row["configuration_signature"])
    raw = str(row["provenance_json"])
    decoded = _decode_external_record(
        tool_run_id,
        raw_analysis_run_id,
        tool_version,
        configuration_signature,
        raw,
    )
    if decoded is None:
        return ExternalEvidenceStatus(
            "abstained",
            "external_evidence_provenance_invalid",
            RUFF_TOOL_NAME,
            tool_run_id,
            None,
            tool_status,
            tool_version,
            configuration_signature,
            None,
            None,
            0,
            0,
            0,
            None,
            None,
            False,
            None,
            "not_evaluated",
        )
    provenance, baseline = decoded
    input_payload = provenance.get("input")
    result = provenance.get("result")
    execution = provenance.get("execution")
    ready = (tool_status == "completed" and execution == "full") or (
        tool_status == "skipped" and execution == "cache_replay"
    )
    if (
        not ready
        or baseline is None
        or not isinstance(input_payload, dict)
        or not isinstance(result, dict)
    ):
        error = provenance.get("error")
        reason = "external_evidence_provenance_invalid" if ready else None
        if isinstance(error, dict) and isinstance(error.get("reason"), str):
            reason = str(error["reason"])
        raw_eligible = (
            input_payload.get("eligible_files", 0) if isinstance(input_payload, dict) else 0
        )
        eligible_files = (
            raw_eligible
            if isinstance(raw_eligible, int)
            and not isinstance(raw_eligible, bool)
            and 0 <= raw_eligible <= RUFF_MAX_FILES
            else 0
        )
        return ExternalEvidenceStatus(
            "abstained",
            reason or f"external_tool_{tool_status}",
            RUFF_TOOL_NAME,
            tool_run_id,
            None,
            tool_status,
            tool_version,
            configuration_signature,
            input_payload.get("signature") if isinstance(input_payload, dict) else None,
            execution if isinstance(execution, str) else None,
            eligible_files,
            0,
            0,
            None,
            None,
            False,
            None,
            "not_evaluated",
        )
    comparable = result.get("comparable") is True
    added = result.get("added") if comparable else None
    resolved = result.get("resolved") if comparable else None
    if isinstance(added, bool) or not isinstance(added, (int, type(None))):
        added = None
    if isinstance(resolved, bool) or not isinstance(resolved, (int, type(None))):
        resolved = None
    diagnostics = len(baseline.diagnostic_ids)
    if not comparable:
        gate: Literal["passed", "failed", "baseline", "not_evaluated"] = "baseline"
    elif added == 0:
        gate = "passed"
    else:
        gate = "failed"
    eligible = input_payload.get("eligible_files", 0)
    eligible_files = eligible if isinstance(eligible, int) and not isinstance(eligible, bool) else 0
    effective = provenance.get("reused_tool_run_id", tool_run_id)
    effective_tool_run_id = (
        effective if isinstance(effective, int) and effective > 0 else tool_run_id
    )
    return ExternalEvidenceStatus(
        "ready",
        None,
        RUFF_TOOL_NAME,
        tool_run_id,
        effective_tool_run_id,
        tool_status,
        tool_version,
        configuration_signature,
        baseline.input_signature,
        execution if isinstance(execution, str) else None,
        eligible_files,
        eligible_files,
        diagnostics,
        added,
        resolved,
        comparable,
        baseline.result_digest,
        gate,
    )


def current_external_status_from_row(
    row: Mapping[str, object] | None,
) -> ExternalEvidenceStatus:
    """Read current-runtime evidence and abstain when its provider is stale."""

    status = external_status_from_row(row)
    if row is None:
        return status
    if row["configuration_signature"] != RUFF_CONFIGURATION_SIGNATURE:
        return replace(
            status,
            status="abstained",
            reason="external_configuration_stale",
            effective_tool_run_id=None,
            covered_files=0,
            diagnostics=0,
            added=None,
            resolved=None,
            comparable=False,
            result_digest=None,
            gate="not_evaluated",
        )
    runtime_version = RuffEvidenceProvider().tool_version()
    if runtime_version is None:
        reason = "external_tool_unavailable"
    elif row["tool_version"] != runtime_version:
        reason = "external_tool_version_stale"
    else:
        return status
    return replace(
        status,
        status="abstained",
        reason=reason,
        effective_tool_run_id=None,
        covered_files=0,
        diagnostics=0,
        added=None,
        resolved=None,
        comparable=False,
        result_digest=None,
        gate="not_evaluated",
    )


def _abstain_external_status(
    status: ExternalEvidenceStatus,
    reason: str,
) -> ExternalEvidenceStatus:
    return replace(
        status,
        status="abstained",
        reason=reason,
        effective_tool_run_id=None,
        covered_files=0,
        diagnostics=0,
        added=None,
        resolved=None,
        comparable=False,
        result_digest=None,
        gate="not_evaluated",
    )


def read_external_evidence(
    connection: sqlite3.Connection,
    analysis_run_id: int,
    *,
    enforce_current_runtime: bool,
) -> tuple[ExternalEvidenceStatus, frozenset[str], dict[str, object] | None]:
    """Read one external owner and verify its current diagnostic projection."""

    raw_row = connection.execute(
        """SELECT r.tool_run_id,r.analysis_run_id,r.tool_version,
        r.configuration_signature,r.status,r.provenance_json,a.status AS owner_status
        FROM external_tool_runs r
        JOIN analysis_runs a ON a.analysis_run_id=r.analysis_run_id
        WHERE r.analysis_run_id=? AND r.tool_name='ruff'
        ORDER BY r.tool_run_id DESC LIMIT 1""",
        (analysis_run_id,),
    ).fetchone()
    row = None if raw_row is None else dict(raw_row)
    status = (
        current_external_status_from_row(row)
        if enforce_current_runtime
        else external_status_from_row(row)
    )
    if row is None or status.status != "ready":
        return status, frozenset(), row
    if row.get("owner_status") != "completed":
        return (
            _abstain_external_status(status, "external_provider_owner_not_completed"),
            frozenset(),
            row,
        )
    decoded = _decode_external_record(
        int(row["tool_run_id"]),
        int(row["analysis_run_id"]),
        str(row["tool_version"]),
        str(row["configuration_signature"]),
        str(row["provenance_json"]),
    )
    if decoded is None:
        return (
            _abstain_external_status(status, "external_evidence_provenance_invalid"),
            frozenset(),
            row,
        )
    _, baseline = decoded
    if baseline is None:
        return (
            _abstain_external_status(status, "external_evidence_provenance_invalid"),
            frozenset(),
            row,
        )
    try:
        current_inputs = read_external_evidence_files(connection, baseline.root)
        current_input_signature = external_input_signature(current_inputs)
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        current_inputs = ()
        current_input_signature = None
    if (
        current_input_signature != baseline.input_signature
        or tuple(item.version_id for item in current_inputs) != baseline.version_ids
        or len(current_inputs) != status.eligible_files
    ):
        return (
            _abstain_external_status(status, "external_input_projection_mismatch"),
            frozenset(),
            row,
        )
    expected_records = baseline.records
    effective_tool_run_id = status.effective_tool_run_id
    if effective_tool_run_id is None:
        return (
            _abstain_external_status(status, "external_replay_source_invalid"),
            frozenset(),
            row,
        )
    if status.execution == "cache_replay":
        source_row = connection.execute(
            """SELECT r.tool_run_id,r.analysis_run_id,r.tool_version,
            r.configuration_signature,r.status,r.provenance_json
            FROM external_tool_runs r JOIN analysis_runs a
            ON a.analysis_run_id=r.analysis_run_id
            WHERE r.tool_run_id=? AND r.tool_name='ruff'
            AND r.status='completed' AND a.status='completed'""",
            (effective_tool_run_id,),
        ).fetchone()
        if source_row is None:
            return (
                _abstain_external_status(status, "external_replay_source_invalid"),
                frozenset(),
                row,
            )
        source = dict(source_row)
        source_decoded = _decode_external_record(
            int(source["tool_run_id"]),
            int(source["analysis_run_id"]),
            str(source["tool_version"]),
            str(source["configuration_signature"]),
            str(source["provenance_json"]),
        )
        source_baseline = None if source_decoded is None else source_decoded[1]
        if source_baseline is None or source_baseline.records is None:
            return (
                _abstain_external_status(status, "external_replay_source_invalid"),
                frozenset(),
                row,
            )
        source_root = _normalized_absolute_path(source_baseline.root)
        replay_root = _normalized_absolute_path(baseline.root)
        if (
            source_baseline.tool_version != baseline.tool_version
            or source_baseline.configuration_signature != baseline.configuration_signature
            or source_root is None
            or replay_root is None
            or source_root != replay_root
            or source_baseline.input_signature != baseline.input_signature
            or source_baseline.result_digest != baseline.result_digest
            or source_baseline.diagnostic_ids != baseline.diagnostic_ids
        ):
            return (
                _abstain_external_status(status, "external_replay_source_invalid"),
                frozenset(),
                row,
            )
        expected_records = source_baseline.records
    if expected_records is None:
        return (
            _abstain_external_status(status, "external_evidence_provenance_invalid"),
            frozenset(),
            row,
        )
    projection_rows = connection.execute(
        """SELECT d.version_id,d.code,d.message,d.start_line,d.start_column,
        d.end_line,d.end_column,d.metadata_json,d.tool_name,d.tool_version,
        d.severity,d.confirmed,d.confidence,f.current_path
        FROM diagnostics d JOIN file_versions v ON v.version_id=d.version_id
        JOIN files f ON f.current_version_id=v.version_id
        WHERE d.source=? AND f.status='current' AND v.invalidated_ns IS NULL
        ORDER BY d.diagnostic_id LIMIT ?""",
        (RUFF_SOURCE, RUFF_MAX_DIAGNOSTICS + 1),
    ).fetchall()
    if len(projection_rows) > RUFF_MAX_DIAGNOSTICS:
        return (
            _abstain_external_status(status, "external_projection_mismatch"),
            frozenset(),
            row,
        )
    identities: list[str] = []
    observed_records: list[ExternalDiagnostic] = []
    normalized_root = _normalized_absolute_path(baseline.root)
    if normalized_root is None:
        return (
            _abstain_external_status(status, "external_evidence_provenance_invalid"),
            frozenset(),
            row,
        )
    for projection in projection_rows:
        raw_metadata = str(projection["metadata_json"])
        if len(raw_metadata.encode("utf-8")) > 64 * 1024:
            return (
                _abstain_external_status(status, "external_projection_mismatch"),
                frozenset(),
                row,
            )
        try:
            metadata = json.loads(raw_metadata)
        except (TypeError, ValueError, json.JSONDecodeError):
            return (
                _abstain_external_status(status, "external_projection_mismatch"),
                frozenset(),
                row,
            )
        identity = (
            metadata.get("external_diagnostic_identity") if isinstance(metadata, dict) else None
        )
        owner = metadata.get("external_tool_run_id") if isinstance(metadata, dict) else None
        try:
            current_path = os.path.abspath(str(projection["current_path"]))
            normalized_current_path = _normalized_absolute_path(current_path)
            if normalized_current_path is None:
                raise ValueError("external projection path is invalid")
            common = os.path.commonpath((normalized_root, normalized_current_path))
            relative_path = os.path.relpath(current_path, baseline.root).replace("\\", "/")
        except (OSError, TypeError, ValueError):
            common = ""
            relative_path = ".."
        code = projection["code"]
        message = projection["message"]
        start_line = projection["start_line"]
        start_column = projection["start_column"]
        end_line = projection["end_line"]
        end_column = projection["end_column"]
        url = metadata.get("url") if isinstance(metadata, dict) else None
        fix_available = metadata.get("fix_available") if isinstance(metadata, dict) else None
        if (
            common != normalized_root
            or relative_path == ".."
            or relative_path.startswith("../")
            or not isinstance(identity, str)
            or not isinstance(owner, int)
            or isinstance(owner, bool)
            or owner != effective_tool_run_id
            or not isinstance(metadata, dict)
            or metadata.get("schema") != "neocortex.external-diagnostic/v1"
            or metadata.get("relative_path") != relative_path
            or metadata.get("claim_scope") != "tool_reported"
            or metadata.get("authority") != "advisory"
            or metadata.get("mutation_authority") is not False
            or (url is not None and not isinstance(url, str))
            or (isinstance(url, str) and len(url.encode("utf-8")) > 2_048)
            or not isinstance(fix_available, bool)
            or projection["tool_name"] != RUFF_TOOL_NAME
            or projection["tool_version"] != baseline.tool_version
            or projection["severity"] != "warning"
            or projection["confirmed"] != 1
            or projection["confidence"] != 1.0
            or not isinstance(projection["version_id"], int)
            or isinstance(projection["version_id"], bool)
            or projection["version_id"] <= 0
            or not isinstance(code, str)
            or not isinstance(message, str)
            or not isinstance(start_line, int)
            or isinstance(start_line, bool)
            or not isinstance(start_column, int)
            or isinstance(start_column, bool)
            or not isinstance(end_line, int)
            or isinstance(end_line, bool)
            or not isinstance(end_column, int)
            or isinstance(end_column, bool)
            or start_line < 1
            or start_column < 0
            or end_line < start_line
            or end_column < 0
            or (end_line == start_line and end_column < start_column)
        ):
            return (
                _abstain_external_status(status, "external_projection_mismatch"),
                frozenset(),
                row,
            )
        observed_identity = _external_diagnostic_identity(
            relative_path,
            code,
            message,
            start_line,
            start_column,
            end_line,
            end_column,
        )
        if observed_identity != identity:
            return (
                _abstain_external_status(status, "external_projection_mismatch"),
                frozenset(),
                row,
            )
        identities.append(identity)
        observed_records.append(
            ExternalDiagnostic(
                projection["version_id"],
                relative_path,
                code,
                message,
                start_line,
                start_column,
                end_line,
                end_column,
                url,
                fix_available,
                identity,
            )
        )
    observed_records.sort(key=_diagnostic_sort_key)
    if (
        len(set(identities)) != len(identities)
        or tuple(sorted(identities)) != tuple(sorted(baseline.diagnostic_ids))
        or tuple(observed_records) != expected_records
    ):
        return (
            _abstain_external_status(status, "external_projection_mismatch"),
            frozenset(),
            row,
        )
    try:
        projection_digest = _external_result_digest(observed_records)
    except (TypeError, ValueError):
        projection_digest = None
    if projection_digest != baseline.result_digest:
        return (
            _abstain_external_status(status, "external_projection_mismatch"),
            frozenset(),
            row,
        )
    return status, frozenset(identities), row


def external_status_digest_payload(
    status: ExternalEvidenceStatus,
) -> dict[str, object]:
    """Return portable content evidence, excluding local run/cache identities."""

    return {
        "status": status.status,
        "reason": status.reason,
        "provider": status.provider,
        "tool_version": status.tool_version,
        "configuration_signature": status.configuration_signature,
        "eligible_files": status.eligible_files,
        "covered_files": status.covered_files,
        "diagnostics": status.diagnostics,
        "added": status.added,
        "resolved": status.resolved,
        "comparable": status.comparable,
        "result_digest": status.result_digest,
        "gate": status.gate,
        "authority": status.authority,
        "mutation_authority": status.mutation_authority,
        "content_executed": status.content_executed,
    }


__all__ = [
    "EXTERNAL_EVIDENCE_SCHEMA",
    "RUFF_CONFIGURATION_SIGNATURE",
    "RUFF_SOURCE",
    "RUFF_TOOL_NAME",
    "ExternalDiagnostic",
    "ExternalEvidenceBaseline",
    "ExternalEvidenceFile",
    "ExternalEvidencePublication",
    "ExternalEvidenceStatus",
    "RuffEvidenceProvider",
    "current_external_status_from_row",
    "decode_external_baseline",
    "external_input_signature",
    "external_status_digest_payload",
    "external_status_from_row",
    "failed_external_publication",
    "read_external_evidence",
    "read_external_evidence_files",
    "skipped_external_publication",
    "validate_external_inputs",
]
