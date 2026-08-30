"""Bounded adapter for raw advisory Vulture evidence.

This module intentionally stops at faithful static findings.  Consensus with
the internal graph, exports, registries, type checkers, and dynamic coverage is
owned by the HITO 4 consumer, which can explain or abstain without changing
source content.
"""

from __future__ import annotations
import hashlib
import importlib.metadata
import json
import os
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from neocortex.runtime.control.bounded_subprocess import run_bounded_capture
from .code_external_evidence import ExternalEvidenceFile
from .external_evidence_models import (
    ExternalProviderFinding,
    external_signature,
)

VULTURE_UNUSED_PROVIDER_ID = "vulture-unused-static"
VULTURE_UNUSED_PROVIDER_SCHEMA = "neocortex.vulture-unused-static/v1"

_WORKER_SCHEMA = "neocortex.external-unused-vulture-worker/v1"
_INPUT_SCHEMA = "neocortex.external-unused-vulture-input/v1"
_TIMEOUT_SECONDS = 180.0
_STDOUT_LIMIT_BYTES = 8 * 1024 * 1024
_STDERR_LIMIT_BYTES = 128 * 1024
_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
_MAX_FILES = 2_000
_MAX_INPUT_BYTES = 512 * 1024 * 1024
_MAX_FINDINGS = 10_000
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_ITEM_KINDS = frozenset(
    {
        "attribute",
        "class",
        "function",
        "import",
        "method",
        "property",
        "unreachable_code",
        "variable",
    }
)
_LIMITATIONS = (
    "vulture_confidence_below_100_is_heuristic",
    "static_name_analysis_cannot_prove_runtime_unused",
    "decorators_callbacks_registries_reexports_and_dynamic_access_require_correlation",
    "advisory_only_no_mutation_authority",
)


@dataclass(frozen=True, slots=True)
class VultureUnusedExecution:
    findings: tuple[ExternalProviderFinding, ...]
    stdout_bytes: int
    stderr_bytes: int
    process_invocations: int
    limitations: tuple[str, ...]


def _unexpected_exit(completed: subprocess.CompletedProcess[bytes]) -> ValueError:
    raw = completed.stderr or completed.stdout
    detail = " ".join(raw.decode("utf-8", errors="replace").split())[:2048]
    message = f"vulture_worker_unexpected_exit:{completed.returncode}"
    return ValueError(message if not detail else f"{message}:{detail}")


def _is_reparse_point(path: Path) -> bool:
    metadata = os.lstat(path)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & reparse)


def _sha256_regular_file(path: Path, *, expected_size: int) -> str:
    before = os.lstat(path)
    if _is_reparse_point(path) or not stat.S_ISREG(before.st_mode):
        raise ValueError("Vulture staged input is not a regular file")
    if before.st_size != expected_size:
        raise ValueError("Vulture staged input size disagrees")
    digest = hashlib.sha256()
    observed_size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            observed_size += len(chunk)
            digest.update(chunk)
    after = os.lstat(path)
    if (
        observed_size != expected_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise ValueError("Vulture staged input changed during manifest construction")
    return digest.hexdigest()


def _owners_by_relative(
    staged: Mapping[str, ExternalEvidenceFile],
) -> dict[str, ExternalEvidenceFile]:
    owners: dict[str, ExternalEvidenceFile] = {}
    for owner in staged.values():
        relative = owner.relative_path.replace("\\", "/")
        key = relative.casefold()
        if key in owners:
            raise ValueError("Vulture staged relative path is duplicated")
        owners[key] = owner
    return owners


def _input_manifest(
    stage_root: Path,
    staged: Mapping[str, ExternalEvidenceFile],
) -> tuple[bytes, str, int, int]:
    if not staged or len(staged) > _MAX_FILES:
        raise ValueError("Vulture staged file count is outside its bound")
    source_root = (stage_root / "source").absolute()
    rows: list[dict[str, object]] = []
    total_bytes = 0
    seen: set[str] = set()
    for absolute_key, owner in sorted(
        staged.items(), key=lambda item: item[1].relative_path.casefold()
    ):
        relative = owner.relative_path.replace("\\", "/")
        pure = PurePosixPath(relative)
        if (
            relative != pure.as_posix()
            or pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} or ":" in part for part in pure.parts)
            or pure.suffix.casefold() not in {".py", ".pyi"}
        ):
            raise ValueError("Vulture staged relative path is invalid")
        expected = source_root.joinpath(*pure.parts).absolute()
        if os.path.normcase(os.path.abspath(absolute_key)) != os.path.normcase(
            os.path.abspath(expected)
        ):
            raise ValueError("Vulture staged mapping does not describe the exact source path")
        key = relative.casefold()
        if key in seen:
            raise ValueError("Vulture staged relative path is duplicated")
        seen.add(key)
        total_bytes += owner.size
        if total_bytes > _MAX_INPUT_BYTES:
            raise ValueError("Vulture staged bytes exceed the input bound")
        rows.append(
            {
                "relative_path": relative,
                "size": owner.size,
                "sha256": _sha256_regular_file(expected, expected_size=owner.size),
            }
        )
    canonical_rows = json.dumps(
        rows, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    manifest_digest = hashlib.sha256(canonical_rows).hexdigest()
    manifest = json.dumps(
        {"schema": _INPUT_SCHEMA, "files": rows},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(manifest) > _MAX_MANIFEST_BYTES:
        raise ValueError("Vulture input manifest exceeds its bound")
    return manifest, manifest_digest, len(rows), total_bytes


def _decode_object(raw: bytes) -> Mapping[str, object]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Vulture worker JSON output is malformed") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("Vulture worker JSON output is not an object")
    return payload


def _required_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Vulture {label} is not an object")
    return value


def _required_list(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"Vulture {label} is not a list")
    return value


def _required_text(value: object, *, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"Vulture {label} is invalid")
    return value


def _required_int(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"Vulture {label} is invalid")
    return value


def _validate_tool_version(value: str) -> None:
    release = value.split("+", 1)[0].split("-", 1)[0].split(".")
    if len(release) < 2 or release[0] != "2" or release[1] != "16":
        raise ValueError("Vulture worker version is outside the supported 2.16 line")


def _normalize_finding(
    raw: Mapping[str, object],
    owners: Mapping[str, ExternalEvidenceFile],
) -> ExternalProviderFinding:
    expected_fields = {
        "relative_path",
        "kind",
        "name",
        "message",
        "confidence_percent",
        "size",
        "start_line",
        "end_line",
    }
    if set(raw) != expected_fields:
        raise ValueError("Vulture finding fields are incompatible")
    relative_path = _required_text(raw.get("relative_path"), label="finding path")
    owner = owners.get(relative_path.replace("\\", "/").casefold())
    if owner is None:
        raise ValueError("Vulture reported a finding for an unowned path")
    kind = _required_text(raw.get("kind"), label="finding kind", maximum=64)
    if kind not in _ITEM_KINDS:
        raise ValueError("Vulture finding kind is unsupported")
    name = _required_text(raw.get("name"), label="finding symbol", maximum=1024)
    message = _required_text(raw.get("message"), label="finding message")
    confidence = _required_int(raw.get("confidence_percent"), label="confidence")
    if confidence > 100:
        raise ValueError("Vulture finding confidence is invalid")
    size = _required_int(raw.get("size"), label="finding size", minimum=1)
    start_line = _required_int(raw.get("start_line"), label="finding start line", minimum=1)
    end_line = _required_int(raw.get("end_line"), label="finding end line", minimum=start_line)
    if size != end_line - start_line + 1:
        raise ValueError("Vulture finding size disagrees with its line span")
    code = "VULTURE_UNUSED_" + kind.upper()
    identity = external_signature(
        "external-finding-v1",
        {
            "provider_id": VULTURE_UNUSED_PROVIDER_ID,
            "path": owner.relative_path,
            "category": "unused_code",
            "code": code,
            "message": message,
            "start_line": start_line,
            "start_column": 0,
            "end_line": end_line,
            "end_column": 0,
        },
    )
    return ExternalProviderFinding(
        identity,
        owner.version_id,
        owner.relative_path,
        "unused_code",
        code,
        "warning",
        message,
        True,
        confidence / 100.0,
        None,
        "advisory",
        start_line,
        0,
        end_line,
        0,
        metadata={
            "provider_schema": VULTURE_UNUSED_PROVIDER_SCHEMA,
            "symbol_name": name,
            "symbol_kind": kind,
            "confidence_percent": confidence,
            "size": size,
            "line_span": [start_line, end_line],
            "dynamic_correlation_required": True,
        },
    )


def _vulture_worker_command(stage_root: Path) -> tuple[str, ...]:
    worker = Path(__file__).with_name("external_unused_vulture_worker.py").resolve(
        strict=True
    )
    return (
        sys.executable,
        "-I",
        str(worker),
        "--root",
        str((stage_root / "source").absolute()),
        "--max-files",
        str(_MAX_FILES),
        "--max-input-bytes",
        str(_MAX_INPUT_BYTES),
        "--max-output-bytes",
        str(_STDOUT_LIMIT_BYTES),
        "--max-findings",
        str(_MAX_FINDINGS),
    )


def _run_vulture_worker(
    stage_root: Path,
    manifest: bytes,
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[bytes]:
    return run_bounded_capture(
        _vulture_worker_command(stage_root),
        input_bytes=manifest,
        timeout_seconds=_TIMEOUT_SECONDS,
        stdout_limit_bytes=_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes=_STDERR_LIMIT_BYTES,
        cwd=stage_root,
        environment=environment,
        memory_limit_bytes=_MEMORY_LIMIT_BYTES if os.name == "nt" else None,
    )


def _validate_worker_envelope(payload: Mapping[str, object]) -> None:
    if set(payload) != {"schema", "status", "tool", "inputs", "findings", "limitations"}:
        raise ValueError("Vulture worker fields are incompatible")
    if payload.get("schema") != _WORKER_SCHEMA or payload.get("status") != "ready":
        raise ValueError("Vulture worker contract is incompatible")


def _validate_worker_tool(payload: Mapping[str, object]) -> None:
    tool = _required_mapping(payload.get("tool"), label="tool")
    if set(tool) != {"name", "version", "api"}:
        raise ValueError("Vulture tool fields are incompatible")
    observed_version = _required_text(tool.get("version"), label="tool version", maximum=256)
    try:
        installed_version = importlib.metadata.version("vulture")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ValueError("Vulture runtime dependency is unavailable") from exc
    if (
        tool.get("name") != "vulture"
        or tool.get("api") != "Vulture.scavenge/get_unused_code"
        or observed_version != installed_version
    ):
        raise ValueError("Vulture worker tool identity disagrees")
    _validate_tool_version(observed_version)


def _validate_worker_inputs(
    payload: Mapping[str, object],
    *,
    expected_files: int,
    expected_bytes: int,
    manifest_digest: str,
) -> None:
    inputs = _required_mapping(payload.get("inputs"), label="inputs")
    if set(inputs) != {"file_count", "total_bytes", "content_manifest_sha256"}:
        raise ValueError("Vulture input evidence fields are incompatible")
    if (
        _required_int(inputs.get("file_count"), label="input file count") != expected_files
        or _required_int(inputs.get("total_bytes"), label="input byte count")
        != expected_bytes
        or _required_text(inputs.get("content_manifest_sha256"), label="manifest digest", maximum=64)
        != manifest_digest
    ):
        raise ValueError("Vulture worker input evidence disagrees")


def _validated_worker_limitations(payload: Mapping[str, object]) -> tuple[str, ...]:
    raw_limitations = _required_list(payload.get("limitations"), label="limitations")
    limitations = tuple(
        _required_text(item, label="limitation", maximum=256) for item in raw_limitations
    )
    if limitations != _LIMITATIONS:
        raise ValueError("Vulture worker limitations are incompatible")
    return limitations


def _normalized_worker_findings(
    payload: Mapping[str, object],
    staged: Mapping[str, ExternalEvidenceFile],
) -> tuple[ExternalProviderFinding, ...]:
    raw_findings = _required_list(payload.get("findings"), label="findings")
    if len(raw_findings) > _MAX_FINDINGS:
        raise ValueError("Vulture worker findings exceed their bound")
    owners = _owners_by_relative(staged)
    findings_by_id: dict[str, ExternalProviderFinding] = {}
    for raw in raw_findings:
        finding = _normalize_finding(
            _required_mapping(raw, label="finding"),
            owners,
        )
        existing = findings_by_id.get(finding.portable_finding_id)
        if existing is not None and existing != finding:
            raise ValueError("Vulture finding identity collision")
        findings_by_id[finding.portable_finding_id] = finding
    return tuple(sorted(findings_by_id.values(), key=lambda item: item.portable_finding_id))


def execute_vulture_unused(
    stage_root: Path,
    staged: Mapping[str, ExternalEvidenceFile],
    environment: Mapping[str, str],
) -> VultureUnusedExecution:
    """Run Vulture's API over only the exact staged files and normalize evidence."""

    manifest, manifest_digest, expected_files, expected_bytes = _input_manifest(
        stage_root, staged
    )
    completed = _run_vulture_worker(stage_root, manifest, environment)
    if completed.returncode != 0:
        raise _unexpected_exit(completed)
    payload = _decode_object(completed.stdout)
    _validate_worker_envelope(payload)
    _validate_worker_tool(payload)
    _validate_worker_inputs(
        payload,
        expected_files=expected_files,
        expected_bytes=expected_bytes,
        manifest_digest=manifest_digest,
    )
    limitations = _validated_worker_limitations(payload)
    findings = _normalized_worker_findings(payload, staged)
    return VultureUnusedExecution(
        findings,
        len(completed.stdout),
        len(completed.stderr),
        1,
        limitations,
    )


__all__ = [
    "VULTURE_UNUSED_PROVIDER_ID",
    "VULTURE_UNUSED_PROVIDER_SCHEMA",
    "VultureUnusedExecution",
    "execute_vulture_unused",
]
