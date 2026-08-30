"""Isolated sequential worker for focal Cosmic Ray mutation testing."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

# The worker is launched with ``-I`` from its exact installed/source path.  Add
# only NeoCortex's own package root so the bounded process primitive remains
# available without making the staged project importable in this process.
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.fspath(_PACKAGE_ROOT))
from neocortex.runtime.control.bounded_subprocess import (  # noqa: E402
    SubprocessOutputLimitError,
    run_bounded_capture,
)

REQUEST_SCHEMA = "neocortex.external-mutation-cosmic-ray-request/v1"
RESULT_SCHEMA = "neocortex.external-mutation-cosmic-ray-worker/v1"
ERROR_SCHEMA = "neocortex.external-mutation-cosmic-ray-worker/error-v1"

_COSMIC_RAY_VERSION = "8.4.6"
_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_FILES = 20_000
_MAX_INPUT_BYTES = 1024 * 1024 * 1024
_MAX_MUTANTS = 100
_MAX_TIMEOUT_SECONDS = 120.0
_MAX_BUDGET_SECONDS = 900.0
_MAX_OUTPUT_BYTES = 1024 * 1024
_TEST_MEMORY_BYTES = 2 * 1024 * 1024 * 1024


class WorkerContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Limits:
    max_mutants: int
    mutant_timeout_seconds: float
    time_budget_seconds: float
    max_output_bytes: int


@dataclass(frozen=True, slots=True)
class Source:
    relative_path: str
    path: Path
    size: int
    sha256: str


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_reparse(path: Path) -> bool:
    metadata = os.lstat(path)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(int(getattr(metadata, "st_file_attributes", 0)) & reparse)


def _plain_path(path: Path, root: Path, *, label: str) -> None:
    current = path
    while True:
        if _is_reparse(current):
            raise WorkerContractError("unsafe_path", f"{label} traverses a reparse point")
        if current == root:
            return
        if not _inside(current, root):
            raise WorkerContractError("unsafe_path", f"{label} escapes its root")
        current = current.parent


def _relative_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 32_768:
        raise WorkerContractError("invalid_request", f"{label} is invalid")
    value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
    ):
        raise WorkerContractError("unsafe_path", f"{label} is unsafe")
    return path.as_posix()


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise WorkerContractError("invalid_request", f"{label} must be an object")
    return value


def _integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkerContractError("invalid_request", f"{label} must be an integer")
    return value


def _number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkerContractError("invalid_request", f"{label} must be numeric")
    return float(value)


def _text(value: object, *, label: str, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum
        or any(character in value for character in "\x00\r\n")
    ):
        raise WorkerContractError("invalid_request", f"{label} is invalid")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _request_signature(request: Mapping[str, object]) -> str:
    payload = {key: value for key, value in request.items() if key != "request_signature"}
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "mutation-request-v1:sha256:" + hashlib.sha256(encoded).hexdigest()


def _roots(request: Mapping[str, object]) -> tuple[Path, Path]:
    project_raw = request.get("project_root")
    scratch_raw = request.get("scratch_root")
    if not isinstance(project_raw, str) or not isinstance(scratch_raw, str):
        raise WorkerContractError("invalid_root", "worker roots must be strings")
    project = Path(project_raw)
    scratch = Path(scratch_raw)
    if not project.is_absolute() or not scratch.is_absolute():
        raise WorkerContractError("invalid_root", "worker roots must be absolute")
    project = project.resolve(strict=True)
    scratch = scratch.resolve(strict=True)
    if not project.is_dir() or not scratch.is_dir() or project == scratch:
        raise WorkerContractError("invalid_root", "worker roots must be distinct directories")
    if _inside(project, scratch) or _inside(scratch, project):
        raise WorkerContractError("invalid_root", "worker roots cannot contain one another")
    _plain_path(project, project, label="project root")
    _plain_path(scratch, scratch, label="scratch root")
    return project, scratch


def _limits(value: object) -> Limits:
    raw = _mapping(value, label="limits")
    limits = Limits(
        _integer(raw.get("max_mutants"), label="max_mutants"),
        _number(raw.get("mutant_timeout_seconds"), label="mutant_timeout_seconds"),
        _number(raw.get("time_budget_seconds"), label="time_budget_seconds"),
        _integer(raw.get("max_output_bytes"), label="max_output_bytes"),
    )
    if not 1 <= limits.max_mutants <= _MAX_MUTANTS:
        raise WorkerContractError("invalid_limit", "max_mutants is outside its bound")
    if not 1 <= limits.mutant_timeout_seconds <= _MAX_TIMEOUT_SECONDS:
        raise WorkerContractError("invalid_limit", "mutant timeout is outside its bound")
    if not 10 <= limits.time_budget_seconds <= _MAX_BUDGET_SECONDS:
        raise WorkerContractError("invalid_limit", "time budget is outside its bound")
    if not 2048 <= limits.max_output_bytes <= _MAX_OUTPUT_BYTES:
        raise WorkerContractError("invalid_limit", "test output bound is invalid")
    return limits


def _sources(value: object, project: Path) -> tuple[Source, ...]:
    if not isinstance(value, list) or not value or len(value) > _MAX_FILES:
        raise WorkerContractError("invalid_request", "source manifest is invalid")
    sources = []
    total = 0
    seen: set[str] = set()
    for raw_value in value:
        raw = _mapping(raw_value, label="source manifest row")
        relative = _relative_path(raw.get("relative_path"), label="source path")
        key = relative.casefold()
        if key in seen:
            raise WorkerContractError("invalid_request", "source path is duplicated")
        seen.add(key)
        size = _integer(raw.get("size"), label="source size")
        digest = _text(raw.get("sha256"), label="source digest", maximum=64)
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise WorkerContractError("invalid_request", "source digest is malformed")
        path = project.joinpath(*PurePosixPath(relative).parts).resolve(strict=True)
        if not _inside(path, project) or not path.is_file():
            raise WorkerContractError("source_missing", "source manifest path is missing")
        _plain_path(path, project, label="source path")
        metadata = path.stat()
        if metadata.st_size != size or _sha256(path) != digest:
            raise WorkerContractError("source_changed", "source digest disagrees")
        total += size
        if total > _MAX_INPUT_BYTES:
            raise WorkerContractError("input_bound_exceeded", "source input exceeds its bound")
        sources.append(Source(relative, path, size, digest))
    return tuple(sources)


def _selectors(value: object, project: Path, sources: Sequence[Source]) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > 2_000:
        raise WorkerContractError("test_missing", "mutation tests were not declared")
    source_paths = {item.relative_path.casefold() for item in sources}
    selectors = []
    for item in value:
        selector = _text(item, label="test selector", maximum=16_384)
        if selector.startswith("-"):
            raise WorkerContractError("invalid_request", "test selector cannot be an option")
        test_path = _relative_path(selector.split("::", 1)[0], label="test path")
        if test_path.casefold() not in source_paths:
            raise WorkerContractError("test_missing", "declared test is not in the exact manifest")
        path = project.joinpath(*PurePosixPath(test_path).parts)
        if not path.is_file():
            raise WorkerContractError("test_missing", "declared test file is missing")
        selectors.append(selector.replace("\\", "/"))
    expected = tuple(sorted(set(selectors), key=str.casefold))
    if tuple(selectors) != expected:
        raise WorkerContractError("invalid_request", "test selectors must be unique and sorted")
    return expected


def _tool_versions(value: object) -> dict[str, str]:
    raw = _mapping(value, label="tool versions")
    try:
        cosmic = importlib.metadata.version("cosmic-ray")
        pytest = importlib.metadata.version("pytest")
    except importlib.metadata.PackageNotFoundError as exc:
        raise WorkerContractError(
            "tool_unavailable", "required mutation tool is unavailable"
        ) from exc
    observed = {
        "cosmic-ray": cosmic,
        "pytest": pytest,
        "python": sys.version.split()[0],
    }
    if cosmic != _COSMIC_RAY_VERSION or dict(raw) != observed:
        raise WorkerContractError("tool_version_incompatible", "mutation tool versions disagree")
    return observed


def _symbol_range(target: Path, relative: str, requested: object) -> tuple[str | None, int, int]:
    if requested is None:
        return None, 1, 10_000_000
    symbol = _text(requested, label="target symbol", maximum=512)
    module_parts = list(PurePosixPath(relative).with_suffix("").parts)
    if module_parts[-1] == "__init__":
        module_parts.pop()
    module = ".".join(module_parts)
    try:
        tree = ast.parse(target.read_text(encoding="utf-8"), filename=relative)
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise WorkerContractError("target_unparseable", "mutation target cannot be parsed") from exc
    candidates: list[tuple[str, int, int]] = []

    def visit(nodes: Sequence[ast.stmt], parents: tuple[str, ...]) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = (*parents, node.name)
                canonical = ".".join((module, *names))
                end = int(getattr(node, "end_lineno", node.lineno))
                if canonical == symbol or canonical.endswith("." + symbol):
                    candidates.append((canonical, int(node.lineno), end))
                visit(node.body, names)

    visit(tree.body, ())
    unique = sorted(set(candidates))
    if len(unique) != 1:
        raise WorkerContractError("symbol_not_found", "target symbol is missing or ambiguous")
    return unique[0]


def _mutation_id(relative: str, mutation: Any) -> str:
    payload = {
        "path": relative,
        "operator": str(mutation.operator_name),
        "operator_args": dict(mutation.operator_args),
        "occurrence": int(mutation.occurrence),
        "start_pos": list(mutation.start_pos),
        "end_pos": list(mutation.end_pos),
        "definition_name": mutation.definition_name,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "cosmic-ray-mutant-v1:sha256:" + hashlib.sha256(encoded).hexdigest()


def _work_items(
    target: Path, relative: str, start_line: int, end_line: int, root: Path
) -> list[tuple[str, Any]]:
    import cosmic_ray.commands  # type: ignore[import-not-found]
    from cosmic_ray.work_db import WorkDB, use_db  # type: ignore[import-not-found]

    database_path = root / "generation.sqlite"
    with use_db(database_path, mode=WorkDB.Mode.create) as database:
        cosmic_ray.commands.init((target,), database, {})
        items = database.work_items
    selected = []
    for item in items:
        if len(item.mutations) != 1:
            continue
        mutation = item.mutations[0]
        if start_line <= int(mutation.start_pos[0]) <= end_line:
            selected.append((_mutation_id(relative, mutation), mutation))
    selected.sort(
        key=lambda pair: (
            int(pair[1].start_pos[0]),
            int(pair[1].start_pos[1]),
            str(pair[1].operator_name),
            int(pair[1].occurrence),
            pair[0],
        )
    )
    return selected


def _runtime_environment(scratch: Path) -> dict[str, str]:
    runtime = scratch / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.longpaths",
            "GIT_CONFIG_VALUE_0": "true",
            "NO_COLOR": "1",
            "PYTEST_ADDOPTS": "",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": os.fspath(runtime / "pycache"),
            "TEMP": os.fspath(runtime),
            "TMP": os.fspath(runtime),
            "TMPDIR": os.fspath(runtime),
        }
    )
    return environment


def _test_command(project: Path, scratch: Path, selectors: Sequence[str]) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "pytest",
        "--rootdir",
        os.fspath(project),
        "--basetemp",
        os.fspath(scratch / "basetemp"),
        "-o",
        f"cache_dir={scratch / 'pytest-cache'}",
        "--disable-warnings",
        "-q",
        *selectors,
    )


def _run_tests(
    command: Sequence[str],
    *,
    project: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    output_limit: int,
) -> tuple[str, int, str, int]:
    started = time.monotonic()
    try:
        completed = run_bounded_capture(
            command,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=output_limit,
            stderr_limit_bytes=output_limit,
            cwd=project,
            environment=environment,
            memory_limit_bytes=_TEST_MEMORY_BYTES if os.name == "nt" else None,
        )
    except subprocess.TimeoutExpired as exc:
        raw = bytes(exc.output or b"") + bytes(exc.stderr or b"")
        return (
            "timeout",
            int((time.monotonic() - started) * 1000),
            hashlib.sha256(raw).hexdigest(),
            len(raw),
        )
    except SubprocessOutputLimitError as exc:
        raw = str(exc).encode("utf-8")
        return (
            "incompetent",
            int((time.monotonic() - started) * 1000),
            hashlib.sha256(raw).hexdigest(),
            len(raw),
        )
    raw = completed.stdout + completed.stderr
    if completed.returncode == 0:
        outcome = "survived"
    elif completed.returncode == 1:
        outcome = "killed"
    else:
        outcome = "incompetent"
    return (
        outcome,
        int((time.monotonic() - started) * 1000),
        hashlib.sha256(raw).hexdigest(),
        len(raw),
    )


def _checkpoint(path: Path, *, mutation_id: str, scope: str) -> dict[str, object] | None:
    if not path.is_file() or path.stat().st_size > 64 * 1024:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "neocortex.cosmic-ray-mutation-checkpoint/v1"
        or payload.get("mutation_id") != mutation_id
        or payload.get("measurement_scope_signature") != scope
        or payload.get("outcome") not in {"killed", "survived", "timeout", "incompetent"}
    ):
        return None
    return payload


def _save_checkpoint(path: Path, payload: Mapping[str, object]) -> None:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(encoded)
    os.replace(temporary, path)


def _verify_sources(sources: Sequence[Source]) -> None:
    for source in sources:
        if (
            not source.path.is_file()
            or source.path.stat().st_size != source.size
            or _sha256(source.path) != source.sha256
        ):
            raise WorkerContractError(
                "source_changed", "staged source changed during mutation execution"
            )


def execute(request: Mapping[str, object]) -> dict[str, object]:
    started = time.monotonic()
    if request.get("schema") != REQUEST_SCHEMA:
        raise WorkerContractError("unsupported_schema", "mutation request schema is unsupported")
    signature = _text(request.get("request_signature"), label="request signature", maximum=128)
    if signature != _request_signature(request):
        raise WorkerContractError(
            "request_signature_mismatch", "mutation request signature disagrees"
        )
    project, scratch = _roots(request)
    limits = _limits(request.get("limits"))
    sources = _sources(request.get("source_manifest"), project)
    selectors = _selectors(request.get("test_selectors"), project, sources)
    tool_versions = _tool_versions(request.get("tool_versions"))
    target_relative = _relative_path(request.get("target"), label="target")
    source_map = {item.relative_path.casefold(): item for item in sources}
    target_source = source_map.get(target_relative.casefold())
    if target_source is None:
        raise WorkerContractError("target_missing", "mutation target is absent from manifest")
    canonical_symbol, symbol_start, symbol_end = _symbol_range(
        target_source.path, target_relative, request.get("symbol")
    )
    scope = _text(
        request.get("measurement_scope_signature"), label="measurement scope", maximum=512
    )
    # Keep the execution root short enough for nested pytest/git fixtures on
    # Windows. Every checkpoint also stores and revalidates the full scope.
    scope_root = scratch / "m" / hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24]
    scope_root.mkdir(parents=True, exist_ok=True)
    _plain_path(scope_root, scratch, label="checkpoint root")
    invocation = scope_root / "invocation"
    if invocation.exists():
        shutil.rmtree(invocation)
    invocation.mkdir()
    environment = _runtime_environment(scope_root)
    command = _test_command(project, scope_root, selectors)
    baseline_checkpoint = scope_root / "baseline.json"
    baseline = _checkpoint(baseline_checkpoint, mutation_id="baseline", scope=scope)
    process_invocations = 0
    if baseline is None:
        baseline_outcome, baseline_ms, baseline_digest, baseline_bytes = _run_tests(
            command,
            project=project,
            environment=environment,
            timeout_seconds=min(limits.mutant_timeout_seconds, limits.time_budget_seconds),
            output_limit=limits.max_output_bytes,
        )
        process_invocations += 1
        if baseline_outcome == "timeout":
            raise WorkerContractError("baseline_timeout", "focal mutation baseline timed out")
        if baseline_outcome != "survived":
            raise WorkerContractError("baseline_failed", "focal mutation baseline did not pass")
        baseline = {
            "schema": "neocortex.cosmic-ray-mutation-checkpoint/v1",
            "mutation_id": "baseline",
            "measurement_scope_signature": scope,
            "outcome": "survived",
            "duration_milliseconds": baseline_ms,
            "output_sha256": baseline_digest,
            "output_bytes": baseline_bytes,
        }
        _save_checkpoint(baseline_checkpoint, baseline)
    baseline_ms = _integer(baseline.get("duration_milliseconds"), label="baseline duration")
    generated = _work_items(
        target_source.path, target_relative, symbol_start, symbol_end, invocation
    )
    if not generated:
        raise WorkerContractError("empty_mutation_selection", "target produced no mutants")
    selected = generated[: limits.max_mutants]
    results: list[dict[str, object]] = []
    reused = 0
    for mutation_id, mutation in selected:
        checkpoint_path = scope_root / f"{mutation_id.rsplit(':', 1)[-1]}.json"
        cached = _checkpoint(checkpoint_path, mutation_id=mutation_id, scope=scope)
        if cached is not None:
            results.append(cached)
            reused += 1
            continue
        remaining = limits.time_budget_seconds - (time.monotonic() - started)
        if remaining < limits.mutant_timeout_seconds:
            raise WorkerContractError("time_budget_exhausted", "mutation time budget exhausted")
        from cosmic_ray.mutating import use_mutation  # type: ignore[import-not-found]
        from cosmic_ray.plugins import get_operator  # type: ignore[import-not-found]

        mutant_started = time.monotonic()
        try:
            operator = get_operator(mutation.operator_name)(**dict(mutation.operator_args))
            with use_mutation(target_source.path, operator, mutation.occurrence) as (
                _original,
                mutated,
            ):
                if mutated is None:
                    outcome = "incompetent"
                    duration_ms = int((time.monotonic() - mutant_started) * 1000)
                    output_digest = hashlib.sha256(b"mutation_not_applied").hexdigest()
                    output_bytes = 0
                else:
                    outcome, duration_ms, output_digest, output_bytes = _run_tests(
                        command,
                        project=project,
                        environment=environment,
                        timeout_seconds=limits.mutant_timeout_seconds,
                        output_limit=limits.max_output_bytes,
                    )
                    process_invocations += 1
        except Exception as exc:  # Cosmic Ray reports malformed mutants as incompetent evidence.
            outcome = "incompetent"
            duration_ms = int((time.monotonic() - mutant_started) * 1000)
            raw = f"{type(exc).__name__}:{exc}".encode("utf-8", errors="replace")[:4096]
            output_digest = hashlib.sha256(raw).hexdigest()
            output_bytes = len(raw)
        result = {
            "schema": "neocortex.cosmic-ray-mutation-checkpoint/v1",
            "mutation_id": mutation_id,
            "measurement_scope_signature": scope,
            "operator": str(mutation.operator_name),
            "occurrence": int(mutation.occurrence),
            "definition_name": mutation.definition_name,
            "start_line": int(mutation.start_pos[0]),
            "start_column": int(mutation.start_pos[1]),
            "end_line": int(mutation.end_pos[0]),
            "end_column": int(mutation.end_pos[1]),
            "outcome": outcome,
            "duration_milliseconds": duration_ms,
            "output_sha256": output_digest,
            "output_bytes": output_bytes,
        }
        _save_checkpoint(checkpoint_path, result)
        results.append(result)
        _verify_sources(sources)
    _verify_sources(sources)
    counts = dict.fromkeys(("killed", "survived", "timed_out", "incompetent"), 0)
    for result in results:
        outcome = str(result["outcome"])
        counts["timed_out" if outcome == "timeout" else outcome] += 1
    return {
        "schema": RESULT_SCHEMA,
        "status": "ready",
        "request_signature": signature,
        "measurement_scope_signature": scope,
        "canonical_symbol": canonical_symbol,
        "tool_versions": tool_versions,
        "target": target_relative,
        "test_selectors": list(selectors),
        "baseline_passed": True,
        "baseline_duration_milliseconds": baseline_ms,
        "duration_milliseconds": int((time.monotonic() - started) * 1000),
        "measurement_complete": len(results) == len(selected),
        "selection_truncated": len(selected) < len(generated),
        "counts": {
            "generated": len(generated),
            "selected": len(selected),
            "completed": len(results),
            "killed": counts["killed"],
            "survived": counts["survived"],
            "timed_out": counts["timed_out"],
            "incompetent": counts["incompetent"],
            "reused": reused,
            "process_invocations": process_invocations,
        },
        "mutations": results,
        "limitations": [
            "focal_declared_target_and_tests_only",
            "sequential_single_process_execution",
            "timeouts_excluded_from_mutation_score",
            "incompetent_mutants_excluded_from_mutation_score",
            "advisory_only_no_mutation_authority",
            "mutation_score_is_not_defect_probability",
        ]
        + (["mutant_selection_truncated_by_limit"] if len(selected) < len(generated) else []),
        "analysis_contract": {
            "executes_project_content": True,
            "executes_tests": True,
            "mutates_only_staged_copy": True,
            "max_concurrent_mutants": 1,
            "source_hashes_verified_before_and_after": True,
            "uses_network": True,
            "mutation_authority": False,
        },
    }


def _error_payload(error: WorkerContractError, signature: str | None) -> dict[str, object]:
    return {
        "schema": ERROR_SCHEMA,
        "status": "error",
        "request_signature": signature,
        "error": {"code": error.code, "message": str(error)[:2048]},
    }


def _emit(payload: Mapping[str, object]) -> None:
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > _MAX_OUTPUT_BYTES * 8:
        raise WorkerContractError("output_bound_exceeded", "mutation result exceeds output bound")
    sys.stdout.buffer.write(encoded + b"\n")


def main(argv: Sequence[str] | None = None) -> NoReturn:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--request", required=True)
    arguments = parser.parse_args(argv)
    signature: str | None = None
    try:
        request_path = Path(arguments.request)
        if not request_path.is_absolute() or request_path.stat().st_size > _MAX_REQUEST_BYTES:
            raise WorkerContractError("invalid_request", "request file is invalid")
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request = dict(_mapping(request, label="request"))
        raw_signature = request.get("request_signature")
        signature = raw_signature if isinstance(raw_signature, str) else None
        _emit(execute(request))
    except WorkerContractError as error:
        _emit(_error_payload(error, signature))
        raise SystemExit(2) from None
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
        wrapped = WorkerContractError("worker_failure", f"{type(error).__name__}:{error}")
        _emit(_error_payload(wrapped, signature))
        raise SystemExit(2) from None
    raise SystemExit(0)


if __name__ == "__main__":
    main()

