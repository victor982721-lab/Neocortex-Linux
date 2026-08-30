"""Isolated Vulture worker over an explicit, immutable staged-file manifest.

The worker imports Vulture itself, never the analyzed project.  It receives the
complete input list through bounded stdin, validates every regular file before
and after static AST analysis, and returns a versioned JSON object.  No project
configuration, plugin, network, autofix, or content execution path is exposed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn

import vulture  # type: ignore[import-untyped]
from vulture.core import ExitCode  # type: ignore[import-untyped]

VULTURE_UNUSED_WORKER_SCHEMA = "neocortex.external-unused-vulture-worker/v1"
VULTURE_UNUSED_INPUT_SCHEMA = "neocortex.external-unused-vulture-input/v1"
WORKER_ERROR_SCHEMA = "neocortex.external-unused-vulture-worker-error/v1"

DEFAULT_MAX_FILES = 2_000
DEFAULT_MAX_INPUT_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_FINDINGS = 10_000
HARD_MAX_FILES = 16_384
HARD_MAX_INPUT_BYTES = 512 * 1024 * 1024
HARD_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
HARD_MAX_FINDINGS = 100_000
MAX_MANIFEST_BYTES = 4 * 1024 * 1024

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


class WorkerContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class WorkerLimits:
    max_files: int
    max_input_bytes: int
    max_output_bytes: int
    max_findings: int

    def __post_init__(self) -> None:
        if not 1 <= self.max_files <= HARD_MAX_FILES:
            raise WorkerContractError("invalid_limit", "max-files is outside its hard bound")
        if not 1 <= self.max_input_bytes <= HARD_MAX_INPUT_BYTES:
            raise WorkerContractError(
                "invalid_limit", "max-input-bytes is outside its hard bound"
            )
        if not 1024 <= self.max_output_bytes <= HARD_MAX_OUTPUT_BYTES:
            raise WorkerContractError(
                "invalid_limit", "max-output-bytes is outside its hard bound"
            )
        if not 1 <= self.max_findings <= HARD_MAX_FINDINGS:
            raise WorkerContractError(
                "invalid_limit", "max-findings is outside its hard bound"
            )


@dataclass(frozen=True, slots=True)
class ManifestFile:
    path: Path
    relative_path: str
    size: int
    sha256: str


def _is_reparse_point(path: Path) -> bool:
    metadata = os.lstat(path)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & reparse)


def _inside_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _required_int(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise WorkerContractError("invalid_manifest", f"{label} is invalid")
    return value


def _required_text(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise WorkerContractError("invalid_manifest", f"{label} is invalid")
    return value


def _read_manifest() -> Mapping[str, object]:
    raw = sys.stdin.buffer.read(MAX_MANIFEST_BYTES + 1)
    if not raw or len(raw) > MAX_MANIFEST_BYTES:
        raise WorkerContractError("invalid_manifest", "input manifest size is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerContractError("invalid_manifest", "input manifest JSON is malformed") from exc
    if not isinstance(payload, Mapping):
        raise WorkerContractError("invalid_manifest", "input manifest is not an object")
    if set(payload) != {"schema", "files"}:
        raise WorkerContractError("invalid_manifest", "input manifest fields are incompatible")
    if payload.get("schema") != VULTURE_UNUSED_INPUT_SCHEMA:
        raise WorkerContractError("invalid_manifest", "input manifest schema is incompatible")
    return payload


def _validate_root(value: str) -> Path:
    root = Path(value).absolute()
    if not root.is_dir() or _is_reparse_point(root):
        raise WorkerContractError("invalid_root", "staged source root is not a regular directory")
    return root.resolve(strict=True)


def _canonical_relative_path(value: object) -> str:
    text = _required_text(value, label="relative path", maximum=4096)
    if "\\" in text:
        raise WorkerContractError("invalid_manifest", "relative path is not canonical")
    relative = PurePosixPath(text)
    if (
        relative.is_absolute()
        or relative.as_posix() != text
        or not relative.parts
        or any(part in {"", ".", ".."} or ":" in part for part in relative.parts)
        or relative.suffix.casefold() not in {".py", ".pyi"}
    ):
        raise WorkerContractError("invalid_manifest", "relative path is invalid")
    return text


def _sha256_regular_file(path: Path, *, expected_size: int) -> str:
    before = os.lstat(path)
    if _is_reparse_point(path) or not stat.S_ISREG(before.st_mode):
        raise WorkerContractError("unsafe_input", "staged input is not a regular file")
    if before.st_size != expected_size:
        raise WorkerContractError("input_changed", "staged input size disagrees")
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
        raise WorkerContractError("input_changed", "staged input changed during read")
    return digest.hexdigest()


def _collect_manifest_files(
    root: Path,
    payload: Mapping[str, object],
    limits: WorkerLimits,
) -> tuple[ManifestFile, ...]:
    rows = payload.get("files")
    if not isinstance(rows, list) or not rows or len(rows) > limits.max_files:
        raise WorkerContractError("invalid_manifest", "manifest file count is invalid")
    files: list[ManifestFile] = []
    observed_paths: set[str] = set()
    total_bytes = 0
    for raw in rows:
        if not isinstance(raw, Mapping) or set(raw) != {"relative_path", "size", "sha256"}:
            raise WorkerContractError("invalid_manifest", "manifest file entry is incompatible")
        relative_path = _canonical_relative_path(raw.get("relative_path"))
        key = relative_path.casefold()
        if key in observed_paths:
            raise WorkerContractError("invalid_manifest", "manifest path is duplicated")
        observed_paths.add(key)
        size = _required_int(raw.get("size"), label="file size")
        total_bytes += size
        if total_bytes > limits.max_input_bytes:
            raise WorkerContractError("input_bound_exceeded", "manifest byte bound exceeded")
        expected_sha256 = _required_text(raw.get("sha256"), label="sha256", maximum=64)
        if len(expected_sha256) != 64 or any(char not in "0123456789abcdef" for char in expected_sha256):
            raise WorkerContractError("invalid_manifest", "sha256 is invalid")
        path = root.joinpath(*PurePosixPath(relative_path).parts)
        resolved = path.resolve(strict=True)
        if not _inside_root(resolved, root) or resolved != path.absolute():
            raise WorkerContractError("unsafe_input", "staged input escapes its exact path")
        for parent in path.parents:
            if parent == root:
                break
            if _is_reparse_point(parent):
                raise WorkerContractError("unsafe_input", "staged input traverses a reparse point")
        if _sha256_regular_file(path, expected_size=size) != expected_sha256:
            raise WorkerContractError("input_changed", "staged input digest disagrees")
        files.append(ManifestFile(path, relative_path, size, expected_sha256))
    return tuple(files)


def _manifest_digest(files: Sequence[ManifestFile]) -> str:
    rows = [
        {"relative_path": item.relative_path, "size": item.size, "sha256": item.sha256}
        for item in files
    ]
    raw = json.dumps(rows, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(raw).hexdigest()


def _validate_inputs_unchanged(files: Sequence[ManifestFile]) -> None:
    for item in files:
        if _sha256_regular_file(item.path, expected_size=item.size) != item.sha256:
            raise WorkerContractError("input_changed", "staged input changed during analysis")


def _bounded_item_text(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise WorkerContractError("tool_contract_error", f"Vulture {label} is invalid")
    return value


def _normalize_findings(
    analyzer: vulture.Vulture,
    files: Sequence[ManifestFile],
    limits: WorkerLimits,
) -> list[dict[str, object]]:
    by_path = {os.path.normcase(os.path.abspath(item.path)): item for item in files}
    raw_items = analyzer.get_unused_code(min_confidence=0, sort_by_size=False)
    if len(raw_items) > limits.max_findings:
        raise WorkerContractError("finding_bound_exceeded", "Vulture finding bound exceeded")
    findings: list[dict[str, object]] = []
    for item in raw_items:
        owner = by_path.get(os.path.normcase(os.path.abspath(item.filename)))
        if owner is None:
            raise WorkerContractError(
                "tool_contract_error", "Vulture reported a finding outside the exact manifest"
            )
        kind = _bounded_item_text(item.typ, label="item kind", maximum=64)
        if kind not in _ITEM_KINDS:
            raise WorkerContractError("tool_contract_error", "Vulture item kind is unsupported")
        name = _bounded_item_text(item.name, label="item name", maximum=1024)
        message = _bounded_item_text(item.message, label="item message", maximum=4096)
        first_line = _required_int(item.first_lineno, label="first line", minimum=1)
        last_line = _required_int(item.last_lineno, label="last line", minimum=first_line)
        confidence = _required_int(item.confidence, label="confidence")
        if confidence > 100:
            raise WorkerContractError("tool_contract_error", "Vulture confidence is invalid")
        size = _required_int(item.size, label="size", minimum=1)
        if size != last_line - first_line + 1:
            raise WorkerContractError("tool_contract_error", "Vulture item size disagrees")
        findings.append(
            {
                "relative_path": owner.relative_path,
                "kind": kind,
                "name": name,
                "message": message,
                "confidence_percent": confidence,
                "size": size,
                "start_line": first_line,
                "end_line": last_line,
            }
        )
    findings.sort(
        key=lambda item: (
            str(item["relative_path"]).casefold(),
            _required_int(item["start_line"], label="finding start line", minimum=1),
            str(item["kind"]),
            str(item["name"]),
        )
    )
    return findings


def _run(root: Path, payload: Mapping[str, object], limits: WorkerLimits) -> dict[str, object]:
    files = _collect_manifest_files(root, payload, limits)
    analyzer = vulture.Vulture(verbose=False, ignore_names=[], ignore_decorators=[])
    analyzer.scavenge([str(item.path) for item in files], exclude=[])
    if analyzer.exit_code == ExitCode.InvalidInput:
        raise WorkerContractError("invalid_source", "Vulture rejected at least one staged input")
    findings = _normalize_findings(analyzer, files, limits)
    _validate_inputs_unchanged(files)
    return {
        "schema": VULTURE_UNUSED_WORKER_SCHEMA,
        "status": "ready",
        "tool": {
            "name": "vulture",
            "version": importlib.metadata.version("vulture"),
            "api": "Vulture.scavenge/get_unused_code",
        },
        "inputs": {
            "file_count": len(files),
            "total_bytes": sum(item.size for item in files),
            "content_manifest_sha256": _manifest_digest(files),
        },
        "findings": findings,
        "limitations": list(_LIMITATIONS),
    }


def _emit(payload: Mapping[str, object], *, maximum_bytes: int) -> None:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(raw) > maximum_bytes:
        raise WorkerContractError("output_bound_exceeded", "worker output bound exceeded")
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.write(b"\n")


def _fail(error: BaseException) -> NoReturn:
    code = error.code if isinstance(error, WorkerContractError) else "internal_error"
    detail = " ".join(str(error).split())[:2048] or type(error).__name__
    payload = {
        "schema": WORKER_ERROR_SCHEMA,
        "status": "error",
        "error": {"code": code, "message": detail},
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    sys.stderr.buffer.write(raw[:8192])
    sys.stderr.buffer.write(b"\n")
    raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run bounded static Vulture analysis")
    parser.add_argument("--root", required=True)
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-input-bytes", type=int, default=DEFAULT_MAX_INPUT_BYTES)
    parser.add_argument("--max-output-bytes", type=int, default=DEFAULT_MAX_OUTPUT_BYTES)
    parser.add_argument("--max-findings", type=int, default=DEFAULT_MAX_FINDINGS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        limits = WorkerLimits(
            arguments.max_files,
            arguments.max_input_bytes,
            arguments.max_output_bytes,
            arguments.max_findings,
        )
        root = _validate_root(arguments.root)
        payload = _read_manifest()
        _emit(_run(root, payload, limits), maximum_bytes=limits.max_output_bytes)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        _fail(error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "VULTURE_UNUSED_INPUT_SCHEMA",
    "VULTURE_UNUSED_WORKER_SCHEMA",
    "WorkerContractError",
    "WorkerLimits",
    "main",
]

