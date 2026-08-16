"""Bounded trusted-execution worker for pytest collection and Coverage.py shards.

The worker is invoked directly with isolated Python and receives its complete
contract through a JSON file.  It intentionally executes trusted project test
content, but it keeps every pytest, temporary, bytecode, and coverage artifact
below the caller-supplied scratch root.  Coverage is limited to this process;
subprocess coverage is deliberately unsupported.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

REQUEST_SCHEMA = "neocortex.external-deep-coverage-request/v1"
COLLECT_SCHEMA = "neocortex.external-deep-coverage-worker/collect-v1"
SHARD_SCHEMA = "neocortex.external-deep-coverage-worker/shard-v1"
ERROR_SCHEMA = "neocortex.external-deep-coverage-worker/error-v1"

DEFAULT_MAX_TESTS = 256
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_FAILURE_CHARS = 8_000
HARD_MAX_TESTS = 10_000
HARD_MAX_SHARD_TESTS = 250
HARD_MAX_COLLECTED_TESTS = 50_000
HARD_MAX_SELECTORS = 2_000
HARD_MAX_SOURCE_FILES = 20_000
HARD_MAX_SOURCE_BYTES = 1024 * 1024 * 1024
HARD_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
HARD_MAX_FAILURE_CHARS = 65_536
HARD_MAX_FAILURES = 2_000
HARD_MAX_CONTEXTS = 250_000
HARD_MAX_REQUEST_BYTES = 16 * 1024 * 1024
HARD_MAX_NODEID_CHARS = 16_384
HARD_MAX_PATH_CHARS = 4_096
HARD_MAX_LINE_MAGNITUDE = 10_000_000
_FINGERPRINT_GUARD_SEED = 0x4E454F43
_WORKER_RUNS_DIRECTORY = "w"

_ORIGINAL_SQLITE_CONNECT = sqlite3.connect

_COMMON_REQUEST_KEYS = frozenset(
    {
        "schema",
        "mode",
        "project_root",
        "scratch_root",
        "source_manifest",
        "input_signature",
        "support_signature",
        "configuration_signature",
        "tool_versions",
        "limits",
        "request_signature",
    }
)


class _CoverageSQLiteFacade:
    """Keep Coverage persistence independent from project sqlite monkeypatches."""

    __slots__ = ("_module", "connect")

    def __init__(self, module: Any, connect: Any) -> None:
        self._module = module
        self.connect = connect

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)


_LIMIT_KEYS = frozenset(
    {
        "max_tests",
        "time_budget_seconds",
        "shard_size",
        "max_output_bytes",
        "max_failures",
        "max_contexts",
    }
)
_MANIFEST_KEYS = frozenset({"relative_path", "module", "size", "content_digest", "production"})
_MODULE_PATTERN = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_RUNTIME_ROOTS: list[Path] = []


class WorkerContractError(ValueError):
    """A stable, user-safe contract failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class WorkerLimits:
    max_tests: int
    time_budget_seconds: float
    shard_size: int
    max_output_bytes: int
    max_failures: int
    max_contexts: int

    def as_payload(self) -> dict[str, int | float]:
        return {
            "max_contexts": self.max_contexts,
            "max_failures": self.max_failures,
            "max_output_bytes": self.max_output_bytes,
            "max_tests": self.max_tests,
            "shard_size": self.shard_size,
            "time_budget_seconds": self.time_budget_seconds,
        }


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: Path
    relative_path: str
    module: str
    size: int
    content_digest: str
    production: bool
    raw: bytes


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    scratch_root: Path
    base_temp: Path
    cache: Path
    coverage_directory: Path
    coverage_data: Path
    coverage_report: Path
    temp: Path
    pycache: Path


def _is_reparse_point(path: Path) -> bool:
    metadata = os.lstat(path)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(int(getattr(metadata, "st_file_attributes", 0)) & reparse)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_plain_tree_path(path: Path, root: Path, *, label: str) -> None:
    current = path
    while True:
        if _is_reparse_point(current):
            raise WorkerContractError("unsafe_path", f"{label} traverses a reparse point")
        if current == root:
            return
        if not _inside(current, root):
            raise WorkerContractError("unsafe_path", f"{label} escapes its declared root")
        current = current.parent


def _required_string(payload: Mapping[str, object], key: str, *, maximum: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise WorkerContractError("invalid_request", f"{key} must be a bounded non-empty string")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise WorkerContractError("invalid_request", f"{key} contains a forbidden character")
    return value


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise WorkerContractError("invalid_request", f"{key} must be an integer")
    return value


def _required_number(payload: Mapping[str, object], key: str) -> float:
    value = payload.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise WorkerContractError("invalid_request", f"{key} must be numeric")
    return float(value)


def _exact_keys(payload: Mapping[str, object], expected: frozenset[str], *, label: str) -> None:
    observed = frozenset(payload)
    if observed != expected:
        raise WorkerContractError(
            "invalid_request",
            f"{label} fields do not match schema; missing={sorted(expected - observed)!r} "
            f"unexpected={sorted(observed - expected)!r}",
        )


def _validate_limits(raw: object) -> WorkerLimits:
    if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
        raise WorkerContractError("invalid_request", "limits must be an object")
    _exact_keys(raw, _LIMIT_KEYS, label="limits")
    limits = WorkerLimits(
        max_tests=_required_int(raw, "max_tests"),
        time_budget_seconds=_required_number(raw, "time_budget_seconds"),
        shard_size=_required_int(raw, "shard_size"),
        max_output_bytes=_required_int(raw, "max_output_bytes"),
        max_failures=_required_int(raw, "max_failures"),
        max_contexts=_required_int(raw, "max_contexts"),
    )
    checks = (
        (limits.max_tests, 1, HARD_MAX_TESTS, "max_tests"),
        (limits.shard_size, 1, HARD_MAX_SHARD_TESTS, "shard_size"),
        (limits.max_output_bytes, 2_048, HARD_MAX_OUTPUT_BYTES, "max_output_bytes"),
        (limits.max_failures, 1, HARD_MAX_FAILURES, "max_failures"),
        (limits.max_contexts, 1, HARD_MAX_CONTEXTS, "max_contexts"),
    )
    for value, minimum, maximum, label in checks:
        if not minimum <= value <= maximum:
            raise WorkerContractError("invalid_limit", f"{label} is outside its hard bound")
    if not 0 < limits.time_budget_seconds <= 900:
        raise WorkerContractError("invalid_limit", "time_budget_seconds is outside its hard bound")
    if limits.shard_size > limits.max_tests:
        raise WorkerContractError("invalid_limit", "shard_size exceeds max_tests")
    return limits


def _absolute_directory(raw: object, *, label: str) -> Path:
    if not isinstance(raw, str) or not raw or len(raw) > HARD_MAX_PATH_CHARS:
        raise WorkerContractError("invalid_root", f"{label} must be a bounded absolute path")
    path = Path(raw)
    if not path.is_absolute() or not path.exists() or not path.is_dir():
        raise WorkerContractError("invalid_root", f"{label} must be an existing absolute directory")
    absolute = path.absolute()
    if _is_reparse_point(absolute):
        raise WorkerContractError("unsafe_path", f"{label} cannot be a reparse point")
    return absolute.resolve(strict=True)


def _validate_roots(request: Mapping[str, object]) -> tuple[Path, Path, Path]:
    project_root = _absolute_directory(request.get("project_root"), label="project_root")
    scratch_root = _absolute_directory(request.get("scratch_root"), label="scratch_root")
    test_root = (project_root / "tests").resolve(strict=True)
    if not test_root.is_dir() or not _inside(test_root, project_root):
        raise WorkerContractError("invalid_root", "project tests root is unavailable")
    _require_plain_tree_path(test_root, project_root, label="test_root")
    if _inside(scratch_root, project_root) or _inside(project_root, scratch_root):
        raise WorkerContractError("unsafe_path", "scratch_root and project_root must not overlap")
    return project_root, test_root, scratch_root


def _relative_path(raw: object, *, label: str) -> Path:
    if not isinstance(raw, str) or not raw or len(raw) > HARD_MAX_PATH_CHARS:
        raise WorkerContractError("invalid_request", f"{label} must be a bounded relative path")
    if "\x00" in raw or "\r" in raw or "\n" in raw:
        raise WorkerContractError("invalid_request", f"{label} contains a forbidden character")
    path = Path(raw)
    if path.is_absolute() or path.drive or any(part in ("", ".", "..") for part in path.parts):
        raise WorkerContractError("unsafe_path", f"{label} is not a canonical relative path")
    return path


def _stable_bytes(path: Path, *, expected_size: int | None = None) -> bytes:
    before = os.lstat(path)
    if _is_reparse_point(path) or not stat.S_ISREG(before.st_mode):
        raise WorkerContractError("unsafe_path", "input is not a regular file")
    if expected_size is not None and before.st_size != expected_size:
        raise WorkerContractError("input_changed", "input size differs from its manifest")
    raw = path.read_bytes()
    after = os.lstat(path)
    if (
        len(raw) != before.st_size
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise WorkerContractError("input_changed", "input changed while it was being read")
    return raw


def _xxhash_module() -> Any:
    try:
        import xxhash
    except ImportError as error:
        raise WorkerContractError("tool_unavailable", "xxhash is required") from error
    return xxhash


def _content_digest(raw: bytes) -> str:
    xxhash = _xxhash_module()
    return (
        f"xxh3_128:{xxhash.xxh3_128_hexdigest(raw)}:"
        f"xxh3_64:{xxhash.xxh3_64_hexdigest(raw, seed=_FINGERPRINT_GUARD_SEED)}"
    )


def _validate_selectors(
    raw_selectors: object,
    *,
    project_root: Path,
    test_root: Path,
    limits: WorkerLimits,
) -> tuple[str, ...]:
    if not isinstance(raw_selectors, list):
        raise WorkerContractError("invalid_request", "selectors must be an array")
    if len(raw_selectors) > HARD_MAX_SELECTORS:
        raise WorkerContractError("test_bound_exceeded", "selector bound exceeded")
    if not raw_selectors:
        return (test_root.relative_to(project_root).as_posix(),)
    normalized: list[str] = []
    for index, raw in enumerate(raw_selectors):
        if (
            not isinstance(raw, str)
            or raw.startswith("-")
            or any(character in raw for character in "\x00\r\n")
        ):
            raise WorkerContractError("invalid_request", f"selectors[{index}] is invalid")
        path_text, separator, selection = raw.partition("::")
        if separator and not selection:
            raise WorkerContractError("invalid_request", "selector nodeid suffix is empty")
        relative = _relative_path(path_text, label=f"selectors[{index}] path")
        lexical = project_root / relative
        if not lexical.exists():
            raise WorkerContractError("missing_test_path", "a selected test path is unavailable")
        resolved = lexical.resolve(strict=True)
        if not _inside(resolved, test_root):
            raise WorkerContractError("unsafe_path", "selected test path escapes test_root")
        _require_plain_tree_path(resolved, test_root, label="selected test path")
        if separator and not resolved.is_file():
            raise WorkerContractError("invalid_request", "exact nodeid selector must name a file")
        normalized.append(relative.as_posix() + (f"::{selection}" if separator else ""))
    expected = tuple(sorted(set(normalized), key=lambda item: (item.casefold(), item)))
    if tuple(normalized) != expected:
        raise WorkerContractError("invalid_request", "selectors must be sorted and unique")
    return expected


def _nodeid_file(nodeid: str) -> str:
    return nodeid.partition("::")[0]


def _portable_nodeid(nodeid: str) -> str:
    """Normalize only the path portion without corrupting escaped parameter ids."""

    path, separator, selection = nodeid.partition("::")
    normalized = path.replace("\\", "/")
    return normalized + (f"::{selection}" if separator else "")


def _validate_nodeids(
    raw_nodeids: object,
    *,
    project_root: Path,
    test_root: Path,
    limits: WorkerLimits,
) -> tuple[str, ...]:
    if not isinstance(raw_nodeids, list) or not raw_nodeids:
        raise WorkerContractError("invalid_request", "nodeids must be a non-empty array")
    if len(raw_nodeids) > min(limits.max_tests, limits.shard_size, HARD_MAX_SHARD_TESTS):
        raise WorkerContractError("test_bound_exceeded", "nodeid bound exceeded")
    normalized: set[str] = set()
    for index, raw in enumerate(raw_nodeids):
        if (
            not isinstance(raw, str)
            or not raw
            or len(raw) > HARD_MAX_NODEID_CHARS
            or "\x00" in raw
            or "\r" in raw
            or "\n" in raw
        ):
            raise WorkerContractError("invalid_request", f"nodeids[{index}] is invalid")
        if "::" not in raw:
            raise WorkerContractError("invalid_request", "nodeids must select exact tests")
        relative = _relative_path(_nodeid_file(raw), label=f"nodeids[{index}] path")
        path = (project_root / relative).resolve(strict=True)
        if not path.is_file() or not _inside(path, test_root):
            raise WorkerContractError("unsafe_path", "nodeid test file escapes test_root")
        _require_plain_tree_path(path, test_root, label="nodeid test file")
        normalized.add(_portable_nodeid(raw))
    if len(normalized) != len(raw_nodeids):
        raise WorkerContractError("invalid_request", "nodeids must be unique")
    return tuple(sorted(normalized, key=lambda item: (item.casefold(), item)))


def _validate_source_manifest(
    raw_manifest: object,
    *,
    project_root: Path,
    limits: WorkerLimits,
) -> tuple[SourceFile, ...]:
    if not isinstance(raw_manifest, list) or not raw_manifest:
        raise WorkerContractError("invalid_request", "source_manifest must be a non-empty array")
    if len(raw_manifest) > HARD_MAX_SOURCE_FILES:
        raise WorkerContractError("source_file_bound_exceeded", "source file bound exceeded")
    files: list[SourceFile] = []
    seen_paths: set[str] = set()
    seen_modules: set[str] = set()
    total_bytes = 0
    for index, raw in enumerate(raw_manifest):
        if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
            raise WorkerContractError("invalid_request", "source manifest entries must be objects")
        _exact_keys(raw, _MANIFEST_KEYS, label=f"source_manifest[{index}]")
        relative = _relative_path(raw.get("relative_path"), label="source relative_path")
        if relative.suffix.lower() not in {".py", ".pyi"}:
            raise WorkerContractError("invalid_source", "source manifest accepts only Python files")
        relative_path = relative.as_posix()
        module = _required_string(raw, "module", maximum=1_000)
        if not _MODULE_PATTERN.fullmatch(module):
            raise WorkerContractError("invalid_source", "source module is not canonical")
        size = _required_int(raw, "size")
        digest = _required_string(raw, "content_digest", maximum=96).lower()
        production = raw.get("production")
        if size < 0 or not re.fullmatch(r"xxh3_128:[0-9a-f]{32}:xxh3_64:[0-9a-f]{16}", digest):
            raise WorkerContractError("invalid_source", "source size or digest is invalid")
        if not isinstance(production, bool):
            raise WorkerContractError("invalid_source", "source production flag is invalid")
        if relative_path in seen_paths or module in seen_modules:
            raise WorkerContractError("invalid_source", "source paths and modules must be unique")
        try:
            path = (project_root / relative).resolve(strict=True)
        except OSError as error:
            raise WorkerContractError("missing_source", "manifest source is unavailable") from error
        if not _inside(path, project_root):
            raise WorkerContractError("unsafe_path", "source file escapes project_root")
        _require_plain_tree_path(path, project_root, label="source file")
        raw_bytes = _stable_bytes(path, expected_size=size)
        if _content_digest(raw_bytes) != digest:
            raise WorkerContractError("input_changed", "source digest differs from its manifest")
        total_bytes += size
        if total_bytes > HARD_MAX_SOURCE_BYTES:
            raise WorkerContractError("source_byte_bound_exceeded", "source byte bound exceeded")
        seen_paths.add(relative_path)
        seen_modules.add(module)
        files.append(SourceFile(path, relative_path, module, size, digest, production, raw_bytes))
    return tuple(sorted(files, key=lambda item: item.relative_path))


def _validate_sources_unchanged(sources: Sequence[SourceFile]) -> None:
    for source in sources:
        raw = _stable_bytes(source.path, expected_size=source.size)
        if _content_digest(raw) != source.content_digest:
            raise WorkerContractError("input_changed", "source changed during test execution")


def _runtime_paths(scratch_root: Path, request_signature: str) -> RuntimePaths:
    worker_runs = scratch_root / _WORKER_RUNS_DIRECTORY
    worker_runs.mkdir(exist_ok=True)
    if not worker_runs.is_dir() or _is_reparse_point(worker_runs):
        raise WorkerContractError("unsafe_path", "worker scratch root is unsafe")
    # The random suffix supplied by mkdtemp provides invocation uniqueness; an
    # eight-character signature prefix is enough for local diagnostics and
    # leaves headroom when trusted-deep tests exercise the worker recursively.
    prefix = hashlib.sha256(request_signature.encode("utf-8")).hexdigest()[:8] + "-"
    invocation_root = Path(tempfile.mkdtemp(prefix=prefix, dir=worker_runs)).resolve(strict=True)
    _require_plain_tree_path(invocation_root, scratch_root, label="worker invocation scratch")
    _RUNTIME_ROOTS.append(invocation_root)
    base_temp = invocation_root / "b"
    cache = invocation_root / "c"
    coverage_directory = invocation_root / "v"
    temp = invocation_root / "t"
    pycache = invocation_root / "p"
    coverage_directory.mkdir()
    temp.mkdir()
    pycache.mkdir()
    return RuntimePaths(
        scratch_root=invocation_root,
        base_temp=base_temp,
        cache=cache,
        coverage_directory=coverage_directory,
        coverage_data=coverage_directory / ".coverage",
        coverage_report=coverage_directory / "coverage.json",
        temp=temp,
        pycache=pycache,
    )


def _cleanup_runtime_roots() -> None:
    while _RUNTIME_ROOTS:
        root = _RUNTIME_ROOTS.pop()
        parent = root.parent
        try:
            resolved = root.resolve(strict=True)
            if parent.name != _WORKER_RUNS_DIRECTORY or resolved.parent != parent.resolve(
                strict=True
            ):
                raise WorkerContractError(
                    "unsafe_cleanup_target", "worker scratch cleanup target is unsafe"
                )
            shutil.rmtree(resolved)
        except WorkerContractError:
            raise
        except OSError as error:
            raise WorkerContractError(
                "scratch_cleanup_failed", "worker scratch could not be removed"
            ) from error


def _cleanup_error_or(primary: WorkerContractError) -> WorkerContractError:
    try:
        _cleanup_runtime_roots()
    except WorkerContractError as cleanup_error:
        primary.add_note(
            "worker runtime cleanup also failed: "
            f"{cleanup_error.code}: {str(cleanup_error)[:512]}"
        )
    return primary


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", value)
    if match is None:
        raise WorkerContractError("tool_version_incompatible", "tool version is malformed")
    return tuple(int(part) for part in match.groups(default="0"))


def _load_tools() -> tuple[Any, Any, dict[str, str]]:
    try:
        import coverage
        import pytest
        from coverage import sqlitedb as coverage_sqlitedb
    except ImportError as error:
        raise WorkerContractError(
            "tool_unavailable", "Coverage.py and pytest are required"
        ) from error
    coverage_version = str(coverage.__version__)
    pytest_version = str(pytest.__version__)
    coverage_parts = _version_tuple(coverage_version)
    pytest_parts = _version_tuple(pytest_version)
    if coverage_parts[0] != 7 or coverage_parts[1] < 14 or pytest_parts[0] != 9:
        raise WorkerContractError(
            "tool_version_incompatible",
            "worker requires Coverage.py >=7.14,<8 and pytest >=9,<10",
        )
    # Project tests are allowed to monkeypatch the public stdlib sqlite3 module.
    # Coverage must retain its own real connection factory through setup/call/
    # teardown context switches or a test double can corrupt its private store.
    coverage_namespace: Any = vars(coverage_sqlitedb)
    if not isinstance(coverage_namespace.get("sqlite3"), _CoverageSQLiteFacade):
        coverage_namespace["sqlite3"] = _CoverageSQLiteFacade(sqlite3, _ORIGINAL_SQLITE_CONNECT)
    return (
        coverage,
        pytest,
        {
            "coverage": coverage_version,
            "pytest": pytest_version,
            "python": sys.version.split()[0],
        },
    )


def _validate_tool_versions(raw: object, observed: Mapping[str, str]) -> None:
    if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
        raise WorkerContractError("invalid_request", "tool_versions must be an object")
    if dict(raw) != dict(observed):
        raise WorkerContractError("tool_version_incompatible", "requested tool versions disagree")


@contextmanager
def _execution_environment(project_root: Path, paths: RuntimePaths) -> Iterator[None]:
    updates = {
        "COVERAGE_FILE": os.fspath(paths.coverage_data),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.longpaths",
        "GIT_CONFIG_VALUE_0": "true",
        "NEOCORTEX_AUDIT_LAB_ROOT": os.fspath(paths.scratch_root),
        "NO_COLOR": "1",
        "PYTEST_ADDOPTS": "",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPYCACHEPREFIX": os.fspath(paths.pycache),
        "TEMP": os.fspath(paths.temp),
        "TMP": os.fspath(paths.temp),
        "TMPDIR": os.fspath(paths.temp),
    }
    removed = ("COVERAGE_PROCESS_START",)
    original_environment = {key: os.environ.get(key) for key in (*updates, *removed)}
    original_directory = Path.cwd()
    original_pycache_prefix = sys.pycache_prefix
    original_temp_directory = tempfile.tempdir
    project_import_path = os.fspath(project_root)
    try:
        os.environ.update(updates)
        for key in removed:
            os.environ.pop(key, None)
        os.chdir(project_root)
        sys.pycache_prefix = os.fspath(paths.pycache)
        tempfile.tempdir = os.fspath(paths.temp)
        sys.path.insert(0, project_import_path)
        yield
    finally:
        try:
            sys.path.remove(project_import_path)
        except ValueError:
            pass
        sys.pycache_prefix = original_pycache_prefix
        tempfile.tempdir = original_temp_directory
        os.chdir(original_directory)
        for key, value in original_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class _PytestEvidencePlugin:
    def __init__(self, pytest_module: Any, coverage_object: Any | None) -> None:
        self._pytest = pytest_module
        self._coverage = coverage_object
        self.collected: list[tuple[str, Path]] = []
        self.reports: list[Any] = []

    def pytest_collection_finish(self, session: Any) -> None:
        self.collected = [(str(item.nodeid), Path(item.path).absolute()) for item in session.items]

    def _switch(self, item: Any, phase: str) -> None:
        if self._coverage is not None:
            self._coverage.switch_context(f"{_portable_nodeid(str(item.nodeid))}|{phase}")

    @property
    def pytest_runtest_setup(self) -> Any:
        @self._pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def hook(item: Any) -> Iterator[None]:
            self._switch(item, "setup")
            yield

        return hook

    @property
    def pytest_runtest_call(self) -> Any:
        @self._pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def hook(item: Any) -> Iterator[None]:
            self._switch(item, "call")
            yield

        return hook

    @property
    def pytest_runtest_teardown(self) -> Any:
        @self._pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def hook(item: Any) -> Iterator[None]:
            self._switch(item, "teardown")
            yield

        return hook

    def pytest_runtest_logreport(self, report: Any) -> None:
        self.reports.append(report)


def _pytest_arguments(
    project_root: Path,
    paths: RuntimePaths,
    selections: Sequence[str],
    *,
    collect_only: bool,
) -> list[str]:
    arguments = [
        "--rootdir",
        os.fspath(project_root),
        "--basetemp",
        os.fspath(paths.base_temp),
        "-o",
        f"cache_dir={paths.cache}",
        "--disable-warnings",
        "-q",
    ]
    if collect_only:
        arguments.append("--collect-only")
    arguments.extend(selections)
    return arguments


def _run_pytest(
    pytest_module: Any,
    plugin: _PytestEvidencePlugin,
    arguments: Sequence[str],
    *,
    project_root: Path,
    paths: RuntimePaths,
) -> tuple[int, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        _execution_environment(project_root, paths),
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        exit_code = int(pytest_module.main(list(arguments), plugins=[plugin]))
    diagnostic = "\n".join(value for value in (stdout.getvalue(), stderr.getvalue()) if value)
    return exit_code, diagnostic


def _validate_collected(
    collected: Sequence[tuple[str, Path]],
    *,
    test_root: Path,
    maximum: int,
) -> tuple[str, ...]:
    if not collected:
        raise WorkerContractError("empty_selection", "pytest collected no tests")
    if len(collected) > maximum:
        raise WorkerContractError("test_bound_exceeded", "collected test bound exceeded")
    nodeids: list[str] = []
    for nodeid, raw_path in collected:
        if (
            not nodeid
            or len(nodeid) > HARD_MAX_NODEID_CHARS
            or any(character in nodeid for character in "\x00\r\n")
        ):
            raise WorkerContractError("invalid_collection", "pytest emitted an invalid nodeid")
        path = raw_path.absolute().resolve(strict=True)
        if not _inside(path, test_root):
            raise WorkerContractError("unsafe_collection", "pytest collected outside test_root")
        _require_plain_tree_path(path, test_root, label="collected test")
        nodeids.append(_portable_nodeid(nodeid))
    if len(set(nodeids)) != len(nodeids):
        raise WorkerContractError("invalid_collection", "pytest emitted duplicate nodeids")
    return tuple(sorted(nodeids, key=lambda item: (item.casefold(), item)))


def _analysis_contract(*, executes_tests: bool) -> dict[str, object]:
    return {
        "branch": True,
        "coverage_config_file": False,
        "executes_project_content": True,
        "executes_tests": executes_tests,
        "loads_project_conftest": True,
        "main_process_only": True,
        "pytest_programmatic": True,
        "subprocess_coverage": False,
        "uses_network": False,
    }


def collect_tests(request: Mapping[str, object]) -> dict[str, object]:
    limits = _validate_limits(request.get("limits"))
    project_root, test_root, scratch_root = _validate_roots(request)
    selections = _validate_selectors(
        request.get("selectors"),
        project_root=project_root,
        test_root=test_root,
        limits=limits,
    )
    if request.get("nodeids") != []:
        raise WorkerContractError("invalid_request", "collect nodeids must be empty")
    sources = _validate_source_manifest(
        request.get("source_manifest"), project_root=project_root, limits=limits
    )
    request_signature = _required_string(request, "request_signature", maximum=512)
    paths = _runtime_paths(scratch_root, request_signature)
    _, pytest_module, tool_versions = _load_tools()
    _validate_tool_versions(request.get("tool_versions"), tool_versions)
    plugin = _PytestEvidencePlugin(pytest_module, None)
    exit_code, diagnostic = _run_pytest(
        pytest_module,
        plugin,
        _pytest_arguments(project_root, paths, selections, collect_only=True),
        project_root=project_root,
        paths=paths,
    )
    if exit_code != 0:
        raise WorkerContractError(
            "pytest_collection_failed",
            _bounded_diagnostic(
                diagnostic,
                project_root,
                paths.scratch_root,
                min(DEFAULT_MAX_FAILURE_CHARS, 4_096),
            ),
        )
    nodeids = _validate_collected(
        plugin.collected, test_root=test_root, maximum=HARD_MAX_COLLECTED_TESTS
    )
    _validate_sources_unchanged(sources)
    symbols = [item for source in sources for item in _symbol_payloads(source)]
    return {
        "analysis_contract": _analysis_contract(executes_tests=False),
        "limits": limits.as_payload(),
        "mode": "collect",
        "nodeids": list(nodeids),
        "request_signature": request_signature,
        "schema": COLLECT_SCHEMA,
        "status": "ready",
        "symbols": symbols,
        "test_count": len(nodeids),
        "tool_versions": tool_versions,
    }


def _bounded_diagnostic(text: str, project_root: Path, scratch_root: Path, maximum: int) -> str:
    normalized = text.replace(os.fspath(project_root), "$PROJECT")
    normalized = normalized.replace(os.fspath(scratch_root), "$SCRATCH")
    normalized = normalized.replace("\\", "/").replace("\r\n", "\n").strip()
    if not normalized:
        normalized = "pytest did not provide a diagnostic"
    encoded = normalized.encode("utf-8")
    if len(encoded) <= maximum:
        return normalized
    marker = "\n...[truncated]...\n"
    marker_bytes = marker.encode("utf-8")
    available = maximum - len(marker_bytes)
    head = available // 2
    tail = available - head
    return (
        encoded[:head].decode("utf-8", errors="ignore")
        + marker
        + encoded[-tail:].decode("utf-8", errors="ignore")
    )


def _test_evidence(
    reports: Sequence[Any],
    *,
    project_root: Path,
    scratch_root: Path,
    limits: WorkerLimits,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    by_nodeid: dict[str, list[Any]] = {}
    for report in reports:
        nodeid = _portable_nodeid(str(report.nodeid))
        by_nodeid.setdefault(nodeid, []).append(report)
    tests: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    for nodeid in sorted(by_nodeid, key=lambda item: (item.casefold(), item)):
        phase_reports = sorted(
            by_nodeid[nodeid], key=lambda item: ("setup", "call", "teardown").index(item.when)
        )
        phases: list[dict[str, str]] = []
        overall = "passed"
        for report in phase_reports:
            outcome = str(report.outcome)
            phases.append({"outcome": outcome, "phase": str(report.when)})
            if report.failed:
                overall = "failed"
                location = getattr(report, "location", ("", 0, ""))
                raw_line = location[1] if isinstance(location, tuple) and len(location) >= 2 else 0
                failures.append(
                    {
                        "message": _bounded_diagnostic(
                            str(report.longreprtext),
                            project_root,
                            scratch_root,
                            min(DEFAULT_MAX_FAILURE_CHARS, 4_096),
                        ),
                        "nodeid": nodeid,
                        "phase": str(report.when),
                        "relative_path": _nodeid_file(nodeid),
                        "line": max(1, int(raw_line) + 1),
                    }
                )
            elif overall == "passed" and outcome == "skipped":
                overall = "skipped"
        tests.append({"nodeid": nodeid, "outcome": overall, "phases": phases})
    if len(failures) > limits.max_failures:
        raise WorkerContractError("failure_bound_exceeded", "pytest failure bound exceeded")
    failures.sort(key=lambda item: (str(item["nodeid"]), str(item["phase"])))
    return tests, failures


def _symbol_payloads(source: SourceFile) -> list[dict[str, object]]:
    try:
        tree = ast.parse(source.raw, filename=source.relative_path)
    except (SyntaxError, ValueError) as error:
        raise WorkerContractError(
            "source_parse_failed", "a manifest source is not valid Python"
        ) from error
    line_count = max(1, len(source.raw.splitlines()))
    symbols: list[dict[str, object]] = [
        {
            "end_line": line_count,
            "kind": "module",
            "module": source.module,
            "qualified_name": source.module,
            "relative_path": source.relative_path,
            "start_line": 1,
        }
    ]

    def visit(nodes: Sequence[ast.stmt], scope: tuple[str, ...], parent_class: bool) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified_parts = (*scope, node.name)
                decorators = [int(item.lineno) for item in node.decorator_list]
                start = min((int(node.lineno), *decorators))
                end = int(node.end_lineno or node.lineno)
                if isinstance(node, ast.ClassDef):
                    kind = "class"
                elif isinstance(node, ast.AsyncFunctionDef):
                    kind = "async_method" if parent_class else "async_function"
                else:
                    kind = "method" if parent_class else "function"
                symbols.append(
                    {
                        "end_line": end,
                        "kind": kind,
                        "module": source.module,
                        "qualified_name": f"{source.module}.{'.'.join(qualified_parts)}",
                        "relative_path": source.relative_path,
                        "start_line": start,
                    }
                )
                visit(node.body, qualified_parts, isinstance(node, ast.ClassDef))
            else:
                nested: list[ast.stmt] = []
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.stmt):
                        nested.append(child)
                visit(nested, scope, parent_class)

    visit(tree.body, (), False)
    return sorted(
        symbols,
        key=lambda item: (
            str(item["relative_path"]),
            cast(int, item["start_line"]),
            cast(int, item["end_line"]),
            str(item["qualified_name"]),
        ),
    )


def _coverage_payloads(
    coverage_object: Any,
    sources: Sequence[SourceFile],
    report_path: Path,
    *,
    max_contexts: int,
) -> list[dict[str, object]]:
    coverage_object.json_report(
        morfs=[os.fspath(source.path) for source in sources],
        outfile=os.fspath(report_path),
        pretty_print=False,
        show_contexts=True,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    raw_files = report.get("files")
    if not isinstance(raw_files, Mapping):
        raise WorkerContractError("coverage_projection_failed", "Coverage.py report is malformed")
    result: list[dict[str, object]] = []
    context_count = 0
    for source in sources:
        raw_file: object | None = None
        for raw_name, candidate in raw_files.items():
            if not isinstance(raw_name, str):
                continue
            candidate_path = Path(raw_name)
            if not candidate_path.is_absolute():
                candidate_path = Path.cwd() / candidate_path
            try:
                resolved = candidate_path.resolve(strict=True)
            except OSError:
                continue
            if os.path.normcase(os.fspath(resolved)) == os.path.normcase(os.fspath(source.path)):
                raw_file = candidate
                break
        if not isinstance(raw_file, Mapping):
            raise WorkerContractError(
                "coverage_projection_failed", "manifest source is absent from coverage report"
            )

        def line_values(name: str, file_payload: Mapping[str, object] = raw_file) -> list[int]:
            raw_values = file_payload.get(name)
            if not isinstance(raw_values, list) or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in raw_values
            ):
                raise WorkerContractError(
                    "coverage_projection_failed", f"Coverage.py {name} is malformed"
                )
            return sorted(set(raw_values))

        def arc_values(name: str, file_payload: Mapping[str, object] = raw_file) -> list[list[int]]:
            raw_values = file_payload.get(name)
            if not isinstance(raw_values, list):
                raise WorkerContractError(
                    "coverage_projection_failed", f"Coverage.py {name} is malformed"
                )
            arcs: set[tuple[int, int]] = set()
            for value in raw_values:
                if (
                    not isinstance(value, list)
                    or len(value) != 2
                    or any(
                        not isinstance(part, int)
                        or isinstance(part, bool)
                        or part == 0
                        or abs(part) > HARD_MAX_LINE_MAGNITUDE
                        for part in value
                    )
                ):
                    raise WorkerContractError(
                        "coverage_projection_failed", f"Coverage.py {name} is malformed"
                    )
                arcs.add((value[0], value[1]))
            return [[start, end] for start, end in sorted(arcs)]

        raw_contexts = raw_file.get("contexts")
        if not isinstance(raw_contexts, Mapping):
            raise WorkerContractError(
                "coverage_projection_failed", "Coverage.py contexts are malformed"
            )
        contexts: dict[str, list[str]] = {}
        for raw_line, raw_values in raw_contexts.items():
            try:
                line = int(raw_line)
            except (TypeError, ValueError) as error:
                raise WorkerContractError(
                    "coverage_projection_failed", "Coverage.py context line is malformed"
                ) from error
            if (
                line < 0
                or not isinstance(raw_values, list)
                or any(
                    not isinstance(value, str) or not value or len(value) > 20_000
                    for value in raw_values
                )
            ):
                raise WorkerContractError(
                    "coverage_projection_failed", "Coverage.py contexts are malformed"
                )
            values = sorted(set(raw_values))
            context_count += len(values)
            if context_count > max_contexts:
                raise WorkerContractError(
                    "context_bound_exceeded", "coverage context bound exceeded"
                )
            contexts[str(line)] = values
        executed = line_values("executed_lines")
        missing = line_values("missing_lines")
        result.append(
            {
                "contexts": contexts,
                "excluded_lines": line_values("excluded_lines"),
                "executed_lines": executed,
                "executed_branches": arc_values("executed_branches"),
                "missing_lines": missing,
                "missing_branches": arc_values("missing_branches"),
                "module": source.module,
                "relative_path": source.relative_path,
                "statements": sorted({*executed, *missing}),
            }
        )
    return result


def run_shard(request: Mapping[str, object]) -> dict[str, object]:
    limits = _validate_limits(request.get("limits"))
    project_root, test_root, scratch_root = _validate_roots(request)
    if request.get("selectors") != []:
        raise WorkerContractError("invalid_request", "shard selectors must be empty")
    nodeids = _validate_nodeids(
        request.get("nodeids"),
        project_root=project_root,
        test_root=test_root,
        limits=limits,
    )
    sources = _validate_source_manifest(
        request.get("source_manifest"), project_root=project_root, limits=limits
    )
    request_signature = _required_string(request, "request_signature", maximum=512)
    paths = _runtime_paths(scratch_root, request_signature)
    coverage_module, pytest_module, tool_versions = _load_tools()
    _validate_tool_versions(request.get("tool_versions"), tool_versions)
    coverage_object = coverage_module.Coverage(
        branch=True,
        config_file=False,
        data_file=os.fspath(paths.coverage_data),
        include=[os.fspath(source.path) for source in sources],
    )
    plugin = _PytestEvidencePlugin(pytest_module, coverage_object)
    coverage_object.start()
    coverage_object.switch_context("collection")
    try:
        exit_code, diagnostic = _run_pytest(
            pytest_module,
            plugin,
            _pytest_arguments(project_root, paths, nodeids, collect_only=False),
            project_root=project_root,
            paths=paths,
        )
    finally:
        coverage_object.stop()
    if exit_code not in (0, 1):
        raise WorkerContractError(
            "pytest_execution_failed",
            _bounded_diagnostic(
                diagnostic,
                project_root,
                paths.scratch_root,
                min(DEFAULT_MAX_FAILURE_CHARS, 4_096),
            ),
        )
    collected = _validate_collected(
        plugin.collected,
        test_root=test_root,
        maximum=min(limits.shard_size, HARD_MAX_SHARD_TESTS),
    )
    if collected != nodeids:
        raise WorkerContractError(
            "selection_mismatch", "pytest did not collect the exact requested nodeids"
        )
    _validate_sources_unchanged(sources)
    coverage_object.get_data().touch_files([os.fspath(source.path) for source in sources])
    coverage_object.save()
    tests, failures = _test_evidence(
        plugin.reports,
        project_root=project_root,
        scratch_root=paths.scratch_root,
        limits=limits,
    )
    if tuple(item["nodeid"] for item in tests) != nodeids:
        raise WorkerContractError(
            "execution_mismatch", "pytest did not execute every selected nodeid"
        )
    coverage_files = _coverage_payloads(
        coverage_object,
        sources,
        paths.coverage_report,
        max_contexts=limits.max_contexts,
    )
    return {
        "analysis_contract": _analysis_contract(executes_tests=True),
        "failures": failures,
        "files": coverage_files,
        "limits": limits.as_payload(),
        "mode": "shard",
        "nodeids": list(nodeids),
        "request_signature": request_signature,
        "schema": SHARD_SCHEMA,
        "status": "ready",
        "suite_status": "failed" if failures else "passed",
        "tests": tests,
        "tool_versions": tool_versions,
    }


def _reject_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WorkerContractError("invalid_json", "request JSON contains duplicate keys")
        result[key] = value
    return result


def _read_request(path: Path) -> Mapping[str, object]:
    if not path.is_absolute() or not path.exists() or not path.is_file() or _is_reparse_point(path):
        raise WorkerContractError(
            "invalid_request_path", "request must be an absolute regular file"
        )
    raw = _stable_bytes(path)
    if len(raw) > HARD_MAX_REQUEST_BYTES:
        raise WorkerContractError(
            "request_byte_bound_exceeded", "request exceeds its hard byte bound"
        )
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                WorkerContractError("invalid_json", f"invalid JSON constant: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkerContractError("invalid_json", "request is not canonical UTF-8 JSON") from error
    if not isinstance(payload, Mapping) or any(not isinstance(key, str) for key in payload):
        raise WorkerContractError("invalid_request", "request must be a JSON object")
    return payload


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _validate_request_signature(request: Mapping[str, object]) -> str:
    observed = _required_string(request, "request_signature", maximum=512)
    unsigned = {key: value for key, value in request.items() if key != "request_signature"}
    digest = _xxhash_module().xxh3_128_hexdigest(_canonical_json(unsigned))
    expected = f"deep-coverage-request-v1:xxh3_128:{digest}"
    if observed != expected:
        raise WorkerContractError("request_signature_mismatch", "request signature disagrees")
    return observed


def _emit(payload: Mapping[str, object], max_output_bytes: int) -> None:
    encoded = _canonical_json(payload)
    if len(encoded) > max_output_bytes:
        raise WorkerContractError(
            "output_byte_bound_exceeded",
            "worker output exceeds its declared byte bound",
        )
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.write(b"\n")


def _fail(error: WorkerContractError, max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES) -> NoReturn:
    payload = {
        "error": {"code": error.code, "message": str(error)},
        "schema": ERROR_SCHEMA,
        "status": "error",
    }
    encoded = _canonical_json(payload)
    if len(encoded) <= max(2_048, min(max_output_bytes, HARD_MAX_OUTPUT_BYTES)):
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.write(b"\n")
    raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--request", type=Path, required=True)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    namespace = _parser().parse_args(arguments)
    max_output_bytes = DEFAULT_MAX_OUTPUT_BYTES
    try:
        request = _read_request(namespace.request)
        schema = _required_string(request, "schema", maximum=200)
        if schema != REQUEST_SCHEMA:
            raise WorkerContractError("unsupported_schema", "request schema is unsupported")
        mode = _required_string(request, "mode", maximum=20)
        if mode not in ("collect", "shard"):
            raise WorkerContractError("unsupported_mode", "request mode is unsupported")
        expected = _COMMON_REQUEST_KEYS | (
            frozenset({"selectors", "nodeids"})
            if mode == "collect"
            else frozenset(
                {
                    "selectors",
                    "nodeids",
                    "suite_signature",
                    "measurement_scope_signature",
                    "shard_signature",
                    "shard_index",
                }
            )
        )
        _exact_keys(request, expected, label="request")
        _validate_request_signature(request)
        for key in ("input_signature", "support_signature", "configuration_signature"):
            _required_string(request, key, maximum=512)
        if mode == "shard":
            for key in ("suite_signature", "measurement_scope_signature", "shard_signature"):
                _required_string(request, key, maximum=512)
            shard_index = request.get("shard_index")
            if not isinstance(shard_index, int) or isinstance(shard_index, bool) or shard_index < 0:
                raise WorkerContractError("invalid_request", "shard_index must be non-negative")
        limits = _validate_limits(request.get("limits"))
        max_output_bytes = limits.max_output_bytes
        payload = collect_tests(request) if mode == "collect" else run_shard(request)
        _cleanup_runtime_roots()
        _emit(payload, max_output_bytes)
    except WorkerContractError as error:
        _fail(_cleanup_error_or(error), max_output_bytes)
    except Exception:
        primary = WorkerContractError("internal_error", "deep coverage worker failed closed")
        _fail(_cleanup_error_or(primary), max_output_bytes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
