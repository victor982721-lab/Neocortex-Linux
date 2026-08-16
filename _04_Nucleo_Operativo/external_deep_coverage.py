"""Bounded trusted-deep pytest and branch-coverage execution.

This module owns orchestration, durable shard checkpoints and normalization.
The worker is a deliberately small executable boundary: it executes trusted
project tests, while this adapter validates every byte it accepts before the
generic external-evidence store can publish it.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from .bounded_subprocess import run_bounded_capture
from .code_architecture_contracts import PRODUCTION_ROOT_PACKAGES
from .code_external_evidence import ExternalEvidenceFile, validate_external_inputs
from .external_evidence_models import (
    ExternalProviderFinding,
    ExternalProviderMetric,
    ExternalProviderRelation,
    external_metric_identity,
    external_relation_identity,
    external_signature,
)
from .semantic_models import fingerprint_bytes

PYTEST_COVERAGE_PROVIDER_ID = "pytest-coverage-trusted-deep"
DEEP_COVERAGE_PROVIDER_SCHEMA = "neocortex.pytest-coverage-trusted-deep/v1"
DEEP_COVERAGE_REQUEST_SCHEMA = "neocortex.external-deep-coverage-request/v1"
DEEP_COVERAGE_COLLECT_SCHEMA = "neocortex.external-deep-coverage-worker/collect-v1"
DEEP_COVERAGE_SHARD_SCHEMA = "neocortex.external-deep-coverage-worker/shard-v1"
DEEP_COVERAGE_CHECKPOINT_SCHEMA = "neocortex.deep-coverage-checkpoint/v2"

_PRODUCTION_ROOTS = frozenset(PRODUCTION_ROOT_PACKAGES)
_MAX_TIME_BUDGET_SECONDS = 900.0
_MAX_TESTS = 10_000
_MAX_SHARD_SIZE = 250
_MAX_SELECTORS = 2_000
_MAX_OUTPUT_BYTES = 32 * 1024 * 1024
_MAX_STDERR_BYTES = 512 * 1024
_MAX_CHECKPOINT_BYTES = 40 * 1024 * 1024
_MAX_FINDINGS = 2_000
_MAX_CONTEXTS = 100_000
_MAX_RELATIONS = 250_000
_MAX_METADATA_RANGES = 256
_MAX_METADATA_ARCS = 256
_MAX_RELATION_LINES = 512
_MAX_RELATION_CONTEXTS = 64
_MEMORY_LIMIT_BYTES = 4 * 1024 * 1024 * 1024
_MAX_SUPPORT_FILES = 20_000
_MAX_SUPPORT_BYTES = 1024 * 1024 * 1024
_MAX_SUPPORT_FILE_BYTES = 64 * 1024 * 1024
_PROGRESS_EXTENSION_MULTIPLIER = 2.0


@dataclass(frozen=True, slots=True)
class DeepCoverageConfig:
    """Exact, bounded selection contract supplied by ``CodeRouteConfig``."""

    test_selectors: tuple[str, ...]
    max_tests: int
    time_budget_seconds: float
    shard_size: int
    configuration_signature: str

    def __post_init__(self) -> None:
        if len(self.test_selectors) > _MAX_SELECTORS:
            raise ValueError("deep coverage has too many test selectors")
        if tuple(sorted(set(self.test_selectors), key=str.casefold)) != self.test_selectors:
            raise ValueError("deep coverage selectors must be unique and deterministically sorted")
        for selector in self.test_selectors:
            if (
                not selector
                or len(selector.encode("utf-8")) > 4096
                or "\x00" in selector
                or selector.startswith("-")
            ):
                raise ValueError("deep coverage test selector is invalid")
        if isinstance(self.max_tests, bool) or not 1 <= self.max_tests <= _MAX_TESTS:
            raise ValueError(f"deep coverage max_tests must be within 1..{_MAX_TESTS}")
        if (
            isinstance(self.time_budget_seconds, bool)
            or not 0 < float(self.time_budget_seconds) <= _MAX_TIME_BUDGET_SECONDS
        ):
            raise ValueError("deep coverage time budget must be within 0..900 seconds")
        object.__setattr__(self, "time_budget_seconds", float(self.time_budget_seconds))
        if isinstance(self.shard_size, bool) or not 1 <= self.shard_size <= min(
            self.max_tests, _MAX_SHARD_SIZE
        ):
            raise ValueError("deep coverage shard_size is outside its bound")
        if (
            not self.configuration_signature
            or len(self.configuration_signature.encode("utf-8")) > 512
        ):
            raise ValueError("deep coverage configuration signature is invalid")

    @property
    def suite_selection(self) -> Literal["full", "selected"]:
        return "selected" if self.test_selectors else "full"


@dataclass(frozen=True, slots=True)
class DeepCoverageExecution:
    findings: tuple[ExternalProviderFinding, ...]
    metrics: tuple[ExternalProviderMetric, ...]
    relations: tuple[ExternalProviderRelation, ...]
    stdout_bytes: int
    stderr_bytes: int
    process_invocations: int
    suite_selection: Literal["full", "selected"]
    measurement_complete: bool
    suite_signature: str
    measurement_scope_signature: str
    counters: Mapping[str, int]
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeepCoverageProgress:
    """One live, bounded observation of collect/shard progress."""

    phase: Literal["collected", "shard_started", "shard_reused", "shard_completed"]
    completed_shards: int
    total_shards: int
    current_shard: int | None
    selected_tests: int
    reused_shards: int
    elapsed_seconds: int
    shard_duration_seconds: int | None = None


DeepCoverageProgressCallback = Callable[[DeepCoverageProgress], None]


@dataclass(frozen=True, slots=True)
class DeepCoveragePreparedInput:
    """Read-only exact input observation reusable by lookup and execution."""

    trusted_root: str
    configuration_signature: str
    code_input_signature: str
    support_signature: str
    publication_input_signature: str
    tool_versions: Mapping[str, str]
    manifest: tuple[Mapping[str, object], ...]
    support_files_verified: int
    support_bytes_verified: int
    preparation_milliseconds: int
    stdout_bytes: int
    stderr_bytes: int
    process_invocations: int


@dataclass(slots=True)
class _CoverageAggregate:
    relative_path: str
    module: str
    statements: set[int] = field(default_factory=set)
    executed: set[int] = field(default_factory=set)
    possible_arcs: set[tuple[int, int]] = field(default_factory=set)
    executed_arcs: set[tuple[int, int]] = field(default_factory=set)
    contexts: dict[int, set[str]] = field(default_factory=lambda: defaultdict(set))


@dataclass(slots=True)
class _ModuleAggregate:
    relative_path: str
    version_id: int
    statements: set[int] = field(default_factory=set)
    executed: set[int] = field(default_factory=set)
    possible_arcs: set[tuple[int, int]] = field(default_factory=set)
    executed_arcs: set[tuple[int, int]] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _SymbolObservation:
    relative_path: str
    module: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int


@dataclass(slots=True)
class _CoverageTotals:
    executable_lines: int = 0
    covered_lines: int = 0
    branch_exits: int = 0
    covered_branch_exits: int = 0

    def add(
        self,
        *,
        statements: set[int],
        executed: set[int],
        possible_arcs: set[tuple[int, int]],
        executed_arcs: set[tuple[int, int]],
    ) -> None:
        self.executable_lines += len(statements)
        self.covered_lines += len(statements & executed)
        self.branch_exits += len(possible_arcs)
        self.covered_branch_exits += len(possible_arcs & executed_arcs)


@dataclass(frozen=True, slots=True)
class _DeepCoverageContext:
    started: float
    project_root: Path
    durable_scratch: Path
    runtime_root: Path
    owners: Mapping[str, ExternalEvidenceFile]
    environment: Mapping[str, str]
    config: DeepCoverageConfig
    prepared: DeepCoveragePreparedInput
    preparation_elapsed: float
    progress: DeepCoverageProgressCallback | None


@dataclass(frozen=True, slots=True)
class _CollectedSuite:
    collected_nodeids: tuple[str, ...]
    selected_nodeids: tuple[str, ...]
    shards: tuple[tuple[str, ...], ...]
    raw_symbols: tuple[Mapping[str, object], ...]
    suite_signature: str
    measurement_scope_signature: str
    measurement_complete: bool
    stdout_bytes: int
    stderr_bytes: int


@dataclass(frozen=True, slots=True)
class _ShardPlan:
    index: int
    nodeids: tuple[str, ...]
    shard_signature: str
    checkpoint: Path


@dataclass(frozen=True, slots=True)
class _ShardExecution:
    results: tuple[Mapping[str, object], ...]
    shards_reused: int
    stdout_bytes: int
    stderr_bytes: int
    process_invocations: int


def _required_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} is not an object")
    return value


def _required_list(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} is not a list")
    return value


def _required_text(value: object, *, label: str, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum
        or "\x00" in value
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _required_int(value: object, *, label: str, maximum: int = 10_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{label} is invalid")
    return value


def _required_bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} is invalid")
    return value


def _relative_path(value: object, *, label: str) -> str:
    text = _required_text(value, label=label, maximum=32_768).replace("\\", "/")
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
    ):
        raise ValueError(f"{label} is not a safe relative path")
    return path.as_posix()


def _tool_versions() -> dict[str, str]:
    result: dict[str, str] = {"python": sys.version.split()[0]}
    for package in ("coverage", "pytest"):
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ValueError(f"{package}_unavailable") from exc
        result[package] = _required_text(version, label=f"{package} version", maximum=256)
    return result


def _owners_by_relative(
    staged: Mapping[str, ExternalEvidenceFile],
) -> dict[str, ExternalEvidenceFile]:
    owners: dict[str, ExternalEvidenceFile] = {}
    for owner in staged.values():
        relative = _relative_path(owner.relative_path, label="staged path")
        key = relative.casefold()
        if key in owners:
            raise ValueError("deep coverage staged relative path is duplicated")
        owners[key] = owner
    return owners


def _module_from_relative(relative_path: str) -> str:
    path = PurePosixPath(relative_path)
    if path.suffix.casefold() not in {".py", ".pyi"}:
        raise ValueError("deep coverage module path is not Python")
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts:
        raise ValueError("deep coverage module path has no module")
    return ".".join(parts)


def _source_manifest(files: Sequence[ExternalEvidenceFile]) -> list[dict[str, object]]:
    manifest = []
    for owner in sorted(
        files, key=lambda item: (item.relative_path.casefold(), item.relative_path)
    ):
        relative = _relative_path(owner.relative_path, label="staged path")
        if PurePosixPath(relative).suffix.casefold() not in {".py", ".pyi"}:
            continue
        manifest.append(
            {
                "relative_path": relative,
                "module": _module_from_relative(relative),
                "size": owner.size,
                "content_digest": (
                    f"xxh3_128:{owner.raw_xxh3_128}:xxh3_64:{owner.raw_xxh3_64_guard}"
                ),
                "production": PurePosixPath(relative).parts[0] in _PRODUCTION_ROOTS,
            }
        )
    return manifest


def _input_signature(manifest: Sequence[Mapping[str, object]]) -> str:
    return external_signature(
        "deep-coverage-input-v1",
        {"files": [dict(item) for item in manifest]},
    )


def _validate_scratch(
    stage_root: Path,
    scratch_root: Path,
    trusted_root: Path,
) -> tuple[Path, Path]:
    stage = stage_root.resolve(strict=True)
    scratch = scratch_root.resolve(strict=True)
    if not stage.is_dir() or not scratch.is_dir():
        raise ValueError("deep coverage roots must be directories")
    stage_normalized = os.path.normcase(os.path.abspath(stage))
    scratch_normalized = os.path.normcase(os.path.abspath(scratch))
    trusted_normalized = os.path.normcase(os.path.abspath(trusted_root))
    if os.path.commonpath((stage_normalized, scratch_normalized)) in {
        stage_normalized,
        scratch_normalized,
    }:
        raise ValueError("deep coverage runtime and durable scratch cannot overlap")
    if os.path.commonpath((stage_normalized, trusted_normalized)) in {
        stage_normalized,
        trusted_normalized,
    }:
        raise ValueError("deep coverage runtime and trusted project cannot overlap")
    if os.path.commonpath((trusted_normalized, scratch_normalized)) in {
        trusted_normalized,
        scratch_normalized,
    }:
        raise ValueError("deep coverage scratch cannot be inside the trusted project")
    return stage, scratch


def _canonical_repository_root() -> Path:
    return Path.home() / "Neocortex" / "Repository"


def _validate_trusted_root(root: Path) -> Path:
    """Require the one physically canonical trusted-execution root."""

    try:
        observed = root.resolve(strict=True)
        expected = _canonical_repository_root().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("trusted-deep root cannot be resolved") from exc
    if not observed.is_dir() or not expected.is_dir():
        raise ValueError("trusted-deep root is not a directory")
    try:
        same = os.path.samefile(observed, expected)
    except OSError as exc:
        raise ValueError("trusted-deep root identity cannot be verified") from exc
    if not same:
        raise ValueError("trusted-deep is restricted to the canonical Neocortex repository")
    return observed


def trusted_deep_home_directory() -> str:
    """Return the canonical home required by trusted project imports.

    The generic provider environment intentionally strips user-specific
    variables.  Trusted-deep executes the real test suite, whose imports may
    legitimately resolve ``Path.home()``.  Preserve only that canonical
    directory instead of forwarding the caller's complete environment.
    """

    try:
        home = Path.home().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("trusted-deep home directory is unavailable") from exc
    if not home.is_dir():
        raise ValueError("trusted-deep home directory is not a directory")
    return os.fspath(home)


def _trusted_support_signature(
    root: Path,
    files: Sequence[ExternalEvidenceFile],
    *,
    environment: Mapping[str, str],
    deadline: float,
) -> tuple[str, int, int, int, int]:
    """Fingerprint Git-owned/non-ignored support without traversing ignored noise."""

    validate_external_inputs(files)
    normalized_root = os.path.normcase(os.path.abspath(root))
    owned: dict[str, ExternalEvidenceFile] = {}
    for owner in files:
        path = Path(owner.path).resolve(strict=True)
        normalized = os.path.normcase(os.path.abspath(path))
        if os.path.commonpath((normalized_root, normalized)) != normalized_root:
            raise ValueError("deep coverage Code input escapes the trusted root")
        if normalized in owned:
            raise ValueError("deep coverage Code input path is duplicated")
        owned[normalized] = owner
    git = shutil.which("git")
    if git is None:
        raise ValueError("git_unavailable_for_deep_coverage_support_signature")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired((git, "ls-files"), 0)
    completed = run_bounded_capture(
        (
            git,
            "-C",
            str(root),
            "-c",
            "core.quotepath=false",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ),
        timeout_seconds=min(60.0, remaining),
        stdout_limit_bytes=8 * 1024 * 1024,
        stderr_limit_bytes=_MAX_STDERR_BYTES,
        cwd=root,
        environment=environment,
        memory_limit_bytes=512 * 1024 * 1024 if os.name == "nt" else None,
    )
    if completed.returncode != 0 or completed.stderr:
        detail = " ".join(completed.stderr.decode("utf-8", errors="replace").split())[:2048]
        raise ValueError(f"git_ls_files_unusable:{completed.returncode}:{detail}")
    try:
        decoded = completed.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("git ls-files output is not UTF-8") from exc
    raw_paths = decoded.split("\x00")
    if raw_paths and raw_paths[-1] == "":
        raw_paths.pop()
    normalized_paths = [_relative_path(value, label="Git support path") for value in raw_paths]
    included_paths = [
        value for value in normalized_paths if PurePosixPath(value).parts[0] != ".codex"
    ]
    relative_paths = tuple(
        sorted(
            set(included_paths),
            key=lambda value: (value.casefold(), value),
        )
    )
    if len(relative_paths) != len(included_paths):
        raise ValueError("Git support path list contains duplicates")
    if len(relative_paths) > _MAX_SUPPORT_FILES:
        raise ValueError("deep coverage support tree exceeds its file bound")
    records: list[dict[str, object]] = []
    total_bytes = 0
    for relative in relative_paths:
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired((git, "support-fingerprint"), 0)
        path = root.joinpath(*PurePosixPath(relative).parts)
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise ValueError("Git support path is absent from the worktree") from exc
        reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if path.is_symlink() or attributes & reparse or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Git support path is not a regular non-reparse file")
        if metadata.st_size > _MAX_SUPPORT_FILE_BYTES:
            raise ValueError("deep coverage support file exceeds its bound")
        total_bytes += metadata.st_size
        if total_bytes > _MAX_SUPPORT_BYTES:
            raise ValueError("deep coverage support tree exceeds its byte bound")
        normalized = os.path.normcase(os.path.abspath(path))
        owned_file = owned.get(normalized)
        if owned_file is not None:
            digest = f"xxh3_128:{owned_file.raw_xxh3_128}:xxh3_64:{owned_file.raw_xxh3_64_guard}"
        else:
            parts = PurePosixPath(relative).parts
            if (
                path.suffix.casefold() in {".py", ".pyi"}
                and parts
                and (parts[0] in _PRODUCTION_ROOTS or parts[0].casefold() == "tests")
            ):
                raise ValueError("trusted-deep Python file is absent from the Code manifest")
            before_size = metadata.st_size
            before_mtime = metadata.st_mtime_ns
            content = path.read_bytes()
            after = path.stat()
            if (
                len(content) != before_size
                or after.st_size != before_size
                or after.st_mtime_ns != before_mtime
            ):
                raise ValueError("deep coverage support file changed during read")
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired((git, "support-fingerprint"), 0)
            observed = fingerprint_bytes(content)
            digest = f"xxh3_128:{observed.xxh3_128}:xxh3_64:{observed.xxh3_64_guard}"
        records.append({"path": relative, "size": metadata.st_size, "digest": digest})
    signature = external_signature("deep-coverage-support-tree-v1", {"files": records})
    return (
        signature,
        len(records),
        total_bytes,
        len(completed.stdout),
        len(completed.stderr),
    )


def _preparation_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    allowed = {"comspec", "lang", "lc_all", "systemroot", "windir"}
    result = {key: value for key, value in source.items() if key.casefold() in allowed}
    result.update({"NO_COLOR": "1", "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1"})
    return result


def _publication_input_signature(
    root: Path,
    *,
    code_input_signature: str,
    support_signature: str,
    config: DeepCoverageConfig,
    tool_versions: Mapping[str, str],
) -> str:
    return external_signature(
        "deep-coverage-publication-input-v1",
        {
            "trusted_root": os.path.normcase(os.path.abspath(root)),
            "code_input_signature": code_input_signature,
            "support_signature": support_signature,
            "configuration_signature": config.configuration_signature,
            "test_selectors": list(config.test_selectors),
            "max_tests": config.max_tests,
            "time_budget_seconds": config.time_budget_seconds,
            "shard_size": config.shard_size,
            "tool_versions": dict(tool_versions),
        },
    )


def _prepare_deep_coverage_input(
    root: Path,
    files: Sequence[ExternalEvidenceFile],
    config: DeepCoverageConfig,
    *,
    environment: Mapping[str, str] | None,
    deadline: float,
) -> DeepCoveragePreparedInput:
    started = time.monotonic()
    trusted_root = _validate_trusted_root(root)
    ordered = tuple(
        sorted(files, key=lambda item: (item.relative_path.casefold(), item.relative_path))
    )
    manifest = tuple(_source_manifest(ordered))
    if not manifest:
        raise ValueError("deep coverage has no Python inputs")
    code_input_signature = _input_signature(manifest)
    selected_environment = _preparation_environment(environment)
    support_signature, support_files, support_bytes, stdout_bytes, stderr_bytes = (
        _trusted_support_signature(
            trusted_root,
            ordered,
            environment=selected_environment,
            deadline=deadline,
        )
    )
    versions = _tool_versions()
    publication_input_signature = _publication_input_signature(
        trusted_root,
        code_input_signature=code_input_signature,
        support_signature=support_signature,
        config=config,
        tool_versions=versions,
    )
    return DeepCoveragePreparedInput(
        str(trusted_root),
        config.configuration_signature,
        code_input_signature,
        support_signature,
        publication_input_signature,
        versions,
        manifest,
        support_files,
        support_bytes,
        max(0, int((time.monotonic() - started) * 1000)),
        stdout_bytes,
        stderr_bytes,
        1,
    )


def prepare_deep_coverage_input(
    root: Path,
    files: Sequence[ExternalEvidenceFile],
    config: DeepCoverageConfig,
    *,
    environment: Mapping[str, str] | None = None,
) -> DeepCoveragePreparedInput:
    """Observe exact replay inputs without importing or executing project code."""

    return _prepare_deep_coverage_input(
        root,
        files,
        config,
        environment=environment,
        deadline=time.monotonic() + config.time_budget_seconds,
    )


def deep_coverage_input_signature(
    root: Path,
    files: Sequence[ExternalEvidenceFile],
    config: DeepCoverageConfig,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Return the exact whole-publication replay key without running pytest."""

    return prepare_deep_coverage_input(
        root,
        files,
        config,
        environment=environment,
    ).publication_input_signature


def _request_digest(request: Mapping[str, object]) -> str:
    payload = {key: value for key, value in request.items() if key != "request_signature"}
    return external_signature("deep-coverage-request-v1", payload)


def _checkpoint_request_digest(request: Mapping[str, object]) -> str:
    """Bind replay to the exact request except its per-attempt runtime path."""

    _required_text(
        request.get("scratch_root"),
        label="deep coverage request scratch root",
        maximum=32_768,
    )
    payload = {
        key: value
        for key, value in request.items()
        if key not in {"request_signature", "scratch_root"}
    }
    return external_signature("deep-coverage-checkpoint-request-v1", payload)


def _owned_plain_directory(
    path: Path,
    *,
    allow_existing: bool,
    label: str,
    require_private: bool,
) -> Path:
    """Create or revalidate one owner-controlled, non-aliased directory."""

    configured = Path(os.path.abspath(path))
    try:
        configured.mkdir(mode=0o700)
    except FileExistsError as exc:
        if not allow_existing:
            raise ValueError(f"{label} was precreated") from exc
    except OSError as exc:
        raise ValueError(f"{label} cannot be created") from exc
    try:
        metadata = os.lstat(configured)
        resolved = configured.resolve(strict=True)
        canonical_metadata = os.stat(resolved, follow_symlinks=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"{label} cannot be identity-verified") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    effective_uid = getattr(os, "geteuid", lambda: int(metadata.st_uid))()
    unsafe_permissions = mode & (0o077 if require_private else 0o022)
    if (
        configured != resolved
        or not stat.S_ISDIR(metadata.st_mode)
        or not stat.S_ISDIR(canonical_metadata.st_mode)
        or (int(metadata.st_dev), int(metadata.st_ino))
        != (int(canonical_metadata.st_dev), int(canonical_metadata.st_ino))
        or int(metadata.st_uid) != int(effective_uid)
        or unsafe_permissions
        or (mode & 0o700) != 0o700
    ):
        raise ValueError(f"{label} is not an owner-controlled plain directory")
    return resolved


def _private_runtime_directory(
    path: Path,
    *,
    allow_existing: bool,
    label: str,
) -> Path:
    return _owned_plain_directory(
        path,
        allow_existing=allow_existing,
        label=label,
        require_private=True,
    )


def _replace_bytes_atomically(path: Path, payload: bytes, *, label: str) -> None:
    """Publish bytes through an unpredictable, descriptor-bound regular file."""

    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(raw_temporary)
    open_descriptor = descriptor
    identity: tuple[int, int] | None = None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            open_descriptor = -1
            metadata = os.fstat(stream.fileno())
            identity = int(metadata.st_dev), int(metadata.st_ino)
            written = stream.write(payload)
            if written != len(payload):
                raise OSError(f"{label} write was incomplete")
            stream.flush()
            os.fsync(stream.fileno())
        assert identity is not None
        metadata = os.lstat(temporary)
        mode = stat.S_IMODE(metadata.st_mode)
        effective_uid = getattr(os, "geteuid", lambda: int(metadata.st_uid))()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (int(metadata.st_dev), int(metadata.st_ino)) != identity
            or metadata.st_nlink != 1
            or int(metadata.st_uid) != int(effective_uid)
            or mode & 0o077
            or (mode & 0o600) != 0o600
        ):
            raise ValueError(f"{label} temporary file changed identity")
        os.replace(temporary, path)
        published = os.lstat(path)
        published_mode = stat.S_IMODE(published.st_mode)
        if (
            not stat.S_ISREG(published.st_mode)
            or (int(published.st_dev), int(published.st_ino)) != identity
            or published.st_nlink != 1
            or int(published.st_uid) != int(effective_uid)
            or published_mode & 0o077
            or (published_mode & 0o600) != 0o600
        ):
            raise ValueError(f"{label} publication changed identity")
    except BaseException as primary:
        if open_descriptor >= 0:
            try:
                os.close(open_descriptor)
            except OSError as cleanup:
                primary.add_note(f"{label} descriptor cleanup also failed: {cleanup}")
        try:
            temporary.unlink(missing_ok=True)
        except OSError as cleanup:
            primary.add_note(f"{label} temporary cleanup also failed: {cleanup}")
        raise


def _read_regular_file_without_links(path: Path, *, maximum: int) -> bytes | None:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        return None
    flags = os.O_RDONLY | int(no_follow) | int(getattr(os, "O_CLOEXEC", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            mode = stat.S_IMODE(before.st_mode)
            effective_uid = getattr(os, "geteuid", lambda: int(before.st_uid))()
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or int(before.st_uid) != int(effective_uid)
                or mode & 0o022
                or not 0 <= before.st_size <= maximum
            ):
                return None
            payload = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
    except OSError:
        return None
    if (
        len(payload) != before.st_size
        or len(payload) > maximum
        or (int(before.st_dev), int(before.st_ino))
        != (int(after.st_dev), int(after.st_ino))
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_mode != after.st_mode
        or before.st_uid != after.st_uid
        or before.st_nlink != after.st_nlink
    ):
        return None
    return payload


def _run_worker(
    request: Mapping[str, object],
    *,
    scratch_root: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
) -> tuple[Mapping[str, object], int, int]:
    signature = _request_digest(request)
    materialized = dict(request)
    materialized["request_signature"] = signature
    request_root = _private_runtime_directory(
        scratch_root / "requests",
        allow_existing=True,
        label="deep coverage worker request directory",
    )
    request_path = request_root / f"{signature.rsplit(':', 1)[-1]}.json"
    encoded = json.dumps(
        materialized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > 16 * 1024 * 1024:
        raise ValueError("deep coverage worker request exceeds its bound")
    _replace_bytes_atomically(
        request_path,
        encoded,
        label="deep coverage worker request",
    )
    worker = Path(__file__).with_name("external_deep_coverage_worker.py")
    command = (sys.executable, "-I", str(worker), "--request", str(request_path))
    completed = run_bounded_capture(
        command,
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=_MAX_OUTPUT_BYTES,
        stderr_limit_bytes=_MAX_STDERR_BYTES,
        cwd=scratch_root,
        environment=environment,
        memory_limit_bytes=_MEMORY_LIMIT_BYTES if os.name == "nt" else None,
    )
    if completed.returncode != 0:
        detail = " ".join(
            (completed.stderr or completed.stdout).decode("utf-8", errors="replace").split()
        )
        raise ValueError(f"deep_coverage_worker_exit:{completed.returncode}:{detail[:2048]}")
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("deep coverage worker JSON is malformed") from exc
    result = _required_mapping(payload, label="deep coverage worker payload")
    if result.get("request_signature") != signature:
        raise ValueError("deep coverage worker request signature disagrees")
    return result, len(completed.stdout), len(completed.stderr)


def _request_base(
    *,
    mode: Literal["collect", "shard"],
    project_root: Path,
    scratch_root: Path,
    manifest: Sequence[Mapping[str, object]],
    input_signature: str,
    support_signature: str,
    config: DeepCoverageConfig,
    tool_versions: Mapping[str, str],
) -> dict[str, object]:
    return {
        "schema": DEEP_COVERAGE_REQUEST_SCHEMA,
        "mode": mode,
        "project_root": str(project_root),
        "scratch_root": str(scratch_root),
        "source_manifest": [dict(item) for item in manifest],
        "input_signature": input_signature,
        "support_signature": support_signature,
        "configuration_signature": config.configuration_signature,
        "tool_versions": dict(sorted(tool_versions.items())),
        "limits": {
            "max_tests": config.max_tests,
            "time_budget_seconds": config.time_budget_seconds,
            "shard_size": config.shard_size,
            "max_output_bytes": _MAX_OUTPUT_BYTES,
            "max_failures": _MAX_FINDINGS,
            "max_contexts": _MAX_CONTEXTS,
        },
    }


def _validate_tool_versions(value: object, expected: Mapping[str, str]) -> dict[str, str]:
    raw = _required_mapping(value, label="deep coverage tool versions")
    observed = {
        _required_text(key, label="deep coverage tool name", maximum=64): _required_text(
            item, label="deep coverage tool version", maximum=256
        )
        for key, item in raw.items()
    }
    if observed != dict(expected):
        raise ValueError("deep coverage worker tool versions disagree")
    return observed


def _validate_collect(
    payload: Mapping[str, object],
    *,
    request_signature: str,
    tool_versions: Mapping[str, str],
) -> tuple[tuple[str, ...], tuple[Mapping[str, object], ...]]:
    if (
        payload.get("schema") != DEEP_COVERAGE_COLLECT_SCHEMA
        or payload.get("status") != "ready"
        or payload.get("mode") != "collect"
        or payload.get("request_signature") != request_signature
    ):
        raise ValueError("deep coverage collect contract is incompatible")
    _validate_tool_versions(payload.get("tool_versions"), tool_versions)
    nodeids = tuple(
        _required_text(item, label="pytest nodeid", maximum=16_384)
        for item in _required_list(payload.get("nodeids"), label="pytest nodeids")
    )
    if nodeids != tuple(sorted(set(nodeids), key=lambda item: (item.casefold(), item))):
        raise ValueError("deep coverage nodeids are not deterministic and unique")
    if len(nodeids) > _MAX_TESTS * 10:
        raise ValueError("deep coverage collection exceeds its absolute bound")
    symbols = tuple(
        _required_mapping(item, label="coverage symbol")
        for item in _required_list(payload.get("symbols"), label="coverage symbols")
    )
    return nodeids, symbols


def _checkpoint_digest(payload: Mapping[str, object]) -> str:
    return external_signature("deep-coverage-shard-result-v1", payload)


def _checkpoint_path(checkpoint_root: Path, shard_signature: str) -> Path:
    return checkpoint_root / f"{shard_signature.rsplit(':', 1)[-1]}.json"


def _load_checkpoint(
    path: Path,
    *,
    shard_signature: str,
    checkpoint_request_signature: str,
) -> tuple[Mapping[str, object], str] | None:
    try:
        raw = _read_regular_file_without_links(path, maximum=_MAX_CHECKPOINT_BYTES)
        if raw is None:
            return None
        decoded = json.loads(raw.decode("utf-8"))
        record = _required_mapping(decoded, label="deep coverage checkpoint")
        if (
            record.get("schema") != DEEP_COVERAGE_CHECKPOINT_SCHEMA
            or record.get("status") != "passed"
            or record.get("shard_signature") != shard_signature
            or record.get("checkpoint_request_signature")
            != checkpoint_request_signature
        ):
            return None
        result = _required_mapping(record.get("result"), label="checkpoint result")
        execution_request_signature = _required_text(
            record.get("execution_request_signature"),
            label="checkpoint execution request signature",
            maximum=512,
        )
        if (
            result.get("request_signature") != execution_request_signature
            or record.get("result_digest") != _checkpoint_digest(result)
        ):
            return None
        return result, execution_request_signature
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _save_checkpoint(
    path: Path,
    *,
    shard_signature: str,
    checkpoint_request_signature: str,
    result: Mapping[str, object],
) -> None:
    validated_checkpoint_signature = _required_text(
        checkpoint_request_signature,
        label="checkpoint request signature",
        maximum=512,
    )
    execution_request_signature = _required_text(
        result.get("request_signature"),
        label="checkpoint execution request signature",
        maximum=512,
    )
    record = {
        "schema": DEEP_COVERAGE_CHECKPOINT_SCHEMA,
        "status": "passed",
        "shard_signature": shard_signature,
        "checkpoint_request_signature": validated_checkpoint_signature,
        "execution_request_signature": execution_request_signature,
        "result_digest": _checkpoint_digest(result),
        "result": dict(result),
    }
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(encoded) > _MAX_CHECKPOINT_BYTES:
        raise ValueError("deep coverage checkpoint exceeds its bound")
    _replace_bytes_atomically(
        path,
        encoded,
        label="deep coverage checkpoint",
    )


def _validate_arc(value: object, *, label: str) -> tuple[int, int]:
    raw = _required_list(value, label=label)
    if len(raw) != 2:
        raise ValueError(f"{label} must contain two lines")

    def endpoint(candidate: object, *, endpoint_label: str) -> int:
        if (
            isinstance(candidate, bool)
            or not isinstance(candidate, int)
            or candidate == 0
            or abs(candidate) > 10_000_000
        ):
            raise ValueError(f"{endpoint_label} is invalid")
        return candidate

    return (
        endpoint(raw[0], endpoint_label=f"{label} source"),
        endpoint(raw[1], endpoint_label=f"{label} target"),
    )


def _validate_shard(
    payload: Mapping[str, object],
    *,
    request_signature: str,
    shard_nodeids: tuple[str, ...],
    tool_versions: Mapping[str, str],
    owners: Mapping[str, ExternalEvidenceFile],
) -> Mapping[str, object]:
    if (
        payload.get("schema") != DEEP_COVERAGE_SHARD_SCHEMA
        or payload.get("status") != "ready"
        or payload.get("mode") != "shard"
        or payload.get("request_signature") != request_signature
    ):
        raise ValueError("deep coverage shard contract is incompatible")
    _validate_tool_versions(payload.get("tool_versions"), tool_versions)
    returned_nodeids = tuple(
        _required_text(item, label="shard nodeid", maximum=16_384)
        for item in _required_list(payload.get("nodeids"), label="shard nodeids")
    )
    if returned_nodeids != shard_nodeids:
        raise ValueError("deep coverage shard nodeids disagree")
    tests = _required_list(payload.get("tests"), label="shard tests")
    observed_tests: dict[str, str] = {}
    for raw in tests:
        test = _required_mapping(raw, label="shard test")
        nodeid = _required_text(test.get("nodeid"), label="shard test nodeid", maximum=16_384)
        outcome = _required_text(test.get("outcome"), label="shard test outcome", maximum=32)
        if outcome not in {"passed", "failed", "skipped"} or nodeid in observed_tests:
            raise ValueError("deep coverage shard test outcome is invalid")
        observed_tests[nodeid] = outcome
    if set(observed_tests) != set(shard_nodeids):
        raise ValueError("deep coverage shard did not report every selected test")
    files = _required_list(payload.get("files"), label="coverage files")
    if len(files) > len(owners):
        raise ValueError("deep coverage worker reported too many files")
    seen_files: set[str] = set()
    context_count = 0
    for raw in files:
        item = _required_mapping(raw, label="coverage file")
        relative = _relative_path(item.get("relative_path"), label="coverage file path")
        if relative.casefold() not in owners or relative.casefold() in seen_files:
            raise ValueError("deep coverage worker reported an unowned or duplicate file")
        seen_files.add(relative.casefold())
        for name in ("statements", "executed_lines", "missing_lines", "excluded_lines"):
            line_values = tuple(
                _required_int(line, label=f"coverage {name} line")
                for line in _required_list(item.get(name), label=f"coverage {name}")
            )
            if line_values != tuple(sorted(set(line_values))):
                raise ValueError(f"deep coverage {name} is not sorted and unique")
        for name in ("executed_branches", "missing_branches"):
            arc_values = tuple(
                _validate_arc(arc, label=f"coverage {name} arc")
                for arc in _required_list(item.get(name), label=f"coverage {name}")
            )
            if arc_values != tuple(sorted(set(arc_values))):
                raise ValueError(f"deep coverage {name} is not sorted and unique")
        contexts = _required_mapping(item.get("contexts"), label="coverage contexts")
        for raw_line, raw_contexts in contexts.items():
            if not isinstance(raw_line, str) or not raw_line.isdecimal():
                raise ValueError("coverage context line is invalid")
            _required_int(int(raw_line), label="coverage context line")
            context_values = tuple(
                _required_text(context, label="coverage context", maximum=20_000)
                for context in _required_list(raw_contexts, label="coverage line contexts")
            )
            if context_values != tuple(sorted(set(context_values))):
                raise ValueError("deep coverage contexts are not sorted and unique")
            context_count += len(context_values)
    if context_count > _MAX_CONTEXTS:
        raise ValueError("deep coverage context count exceeds its bound")
    failures = _required_list(payload.get("failures"), label="pytest failures")
    if len(failures) > _MAX_FINDINGS:
        raise ValueError("deep coverage failure count exceeds its bound")
    contract = _required_mapping(payload.get("analysis_contract"), label="coverage contract")
    if not _required_bool(
        contract.get("main_process_only"), label="main process only"
    ) or _required_bool(contract.get("subprocess_coverage"), label="subprocess coverage"):
        raise ValueError("deep coverage worker overstates subprocess coverage")
    return payload


def _ranges(lines: Sequence[int]) -> tuple[tuple[int, int], ...]:
    if not lines:
        return ()
    result: list[tuple[int, int]] = []
    start = previous = lines[0]
    for line in lines[1:]:
        if line == previous + 1:
            previous = line
            continue
        result.append((start, previous))
        start = previous = line
    result.append((start, previous))
    return tuple(result)


def _shard_is_cacheable(payload: Mapping[str, object]) -> bool:
    if payload.get("suite_status") != "passed" or _required_list(
        payload.get("failures"), label="pytest failures"
    ):
        return False
    return all(
        _required_mapping(item, label="shard test").get("outcome") in {"passed", "skipped"}
        for item in _required_list(payload.get("tests"), label="shard tests")
    )


def _metric(
    *,
    subject_kind: Literal["run", "file", "module", "symbol"],
    subject_key: str,
    name: str,
    value: float,
    unit: str,
    version_id: int | None,
    metadata: Mapping[str, object],
) -> ExternalProviderMetric:
    return ExternalProviderMetric(
        external_metric_identity(
            PYTEST_COVERAGE_PROVIDER_ID,
            subject_kind=subject_kind,
            subject_key=subject_key,
            category="coverage",
            metric_name=name,
            unit=unit,
        ),
        subject_kind,
        subject_key,
        "coverage",
        name,
        value,
        unit,
        version_id=version_id,
        metadata=metadata,
    )


def _coverage_metrics(
    *,
    subject_kind: Literal["run", "file", "module", "symbol"],
    subject_key: str,
    statements: set[int],
    executed: set[int],
    possible_arcs: set[tuple[int, int]],
    executed_arcs: set[tuple[int, int]],
    version_id: int | None,
    metadata: Mapping[str, object],
) -> list[ExternalProviderMetric]:
    covered_lines = statements & executed
    missing_lines = statements - executed
    covered_arcs = possible_arcs & executed_arcs
    missing_arcs = possible_arcs - executed_arcs
    line_rate = len(covered_lines) / len(statements) if statements else 1.0
    branch_rate = len(covered_arcs) / len(possible_arcs) if possible_arcs else 1.0
    values = (
        ("executable_lines", len(statements), "count"),
        ("covered_lines", len(covered_lines), "count"),
        ("missing_lines", len(missing_lines), "count"),
        ("line_coverage_percent", line_rate * 100.0, "percent"),
        ("branch_exits", len(possible_arcs), "count"),
        ("covered_branch_exits", len(covered_arcs), "count"),
        ("missing_branch_exits", len(missing_arcs), "count"),
        ("branch_coverage_percent", branch_rate * 100.0, "percent"),
    )
    return [
        _metric(
            subject_kind=subject_kind,
            subject_key=subject_key,
            name=name,
            value=float(value),
            unit=unit,
            version_id=version_id,
            metadata=metadata,
        )
        for name, value, unit in values
    ]


def _phase_context(value: str) -> tuple[str, str] | None:
    nodeid, separator, phase = value.rpartition("|")
    if not separator or phase not in {"setup", "call", "teardown"} or not nodeid:
        return None
    return nodeid, phase


def _normalize_symbols(
    raw_symbols: Sequence[Mapping[str, object]],
    owners: Mapping[str, ExternalEvidenceFile],
) -> dict[tuple[str, str, str, int, int], _SymbolObservation]:
    symbols: dict[tuple[str, str, str, int, int], _SymbolObservation] = {}
    for symbol in raw_symbols:
        relative = _relative_path(symbol.get("relative_path"), label="symbol path")
        module = _required_text(symbol.get("module"), label="symbol module")
        qualified = _required_text(symbol.get("qualified_name"), label="symbol qualified name")
        kind = _required_text(symbol.get("kind"), label="symbol kind", maximum=64)
        start = _required_int(symbol.get("start_line"), label="symbol start line")
        end = _required_int(symbol.get("end_line"), label="symbol end line")
        if start < 1 or end < start or relative.casefold() not in owners:
            raise ValueError("deep coverage symbol location is invalid")
        symbol_identity = (relative.casefold(), qualified, kind, start, end)
        if symbol_identity in symbols:
            raise ValueError("deep coverage symbol observation is duplicated")
        symbols[symbol_identity] = _SymbolObservation(
            relative,
            module,
            qualified,
            kind,
            start,
            end,
        )
    return symbols


def _coverage_line_sets(item: Mapping[str, object]) -> tuple[set[int], set[int]]:
    statements = {
        _required_int(value, label="coverage statement")
        for value in _required_list(item.get("statements"), label="coverage statements")
    }
    executed = {
        _required_int(value, label="coverage executed line")
        for value in _required_list(item.get("executed_lines"), label="coverage executed lines")
    }
    missing = {
        _required_int(value, label="coverage missing line")
        for value in _required_list(item.get("missing_lines"), label="coverage missing lines")
    }
    if statements != executed | missing or executed & missing:
        raise ValueError("deep coverage line accounting is inconsistent")
    return statements, executed


def _coverage_arc_sets(
    item: Mapping[str, object],
) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    executed_arcs = {
        _validate_arc(value, label="coverage executed branch")
        for value in _required_list(
            item.get("executed_branches"), label="coverage executed branches"
        )
    }
    missing_arcs = {
        _validate_arc(value, label="coverage missing branch")
        for value in _required_list(item.get("missing_branches"), label="coverage missing branches")
    }
    return executed_arcs, missing_arcs


def _merge_coverage_file(
    item: Mapping[str, object],
    files: dict[str, _CoverageAggregate],
) -> None:
    relative = _relative_path(item.get("relative_path"), label="coverage path")
    file_key = relative.casefold()
    module = _required_text(item.get("module"), label="coverage module")
    aggregate = files.get(file_key)
    if aggregate is None:
        aggregate = _CoverageAggregate(relative, module)
        files[file_key] = aggregate
    if aggregate.module != module:
        raise ValueError("deep coverage module observations disagree")
    statements, executed = _coverage_line_sets(item)
    executed_arcs, missing_arcs = _coverage_arc_sets(item)
    aggregate.statements.update(statements)
    aggregate.executed.update(executed)
    aggregate.possible_arcs.update(executed_arcs | missing_arcs)
    aggregate.executed_arcs.update(executed_arcs)
    raw_context_map = _required_mapping(item.get("contexts"), label="coverage contexts")
    for raw_line, raw_values in raw_context_map.items():
        if not isinstance(raw_line, str) or not raw_line.isdecimal():
            raise ValueError("coverage context line is invalid")
        line_number = int(raw_line)
        context_values = {
            _required_text(value, label="coverage context", maximum=20_000)
            for value in _required_list(raw_values, label="coverage line contexts")
        }
        aggregate.contexts[line_number].update(context_values)


def _normalize_shards(
    shards: Sequence[Mapping[str, object]],
) -> tuple[dict[str, _CoverageAggregate], dict[str, str], list[Mapping[str, object]]]:
    files: dict[str, _CoverageAggregate] = {}
    tests: dict[str, str] = {}
    failures: list[Mapping[str, object]] = []
    for shard in shards:
        for raw_test in _required_list(shard.get("tests"), label="shard tests"):
            test = _required_mapping(raw_test, label="shard test")
            nodeid = str(test["nodeid"])
            if nodeid in tests:
                raise ValueError("deep coverage combined duplicate test")
            tests[nodeid] = str(test["outcome"])
        failures.extend(
            _required_mapping(item, label="pytest failure")
            for item in _required_list(shard.get("failures"), label="pytest failures")
        )
        for raw_file in _required_list(shard.get("files"), label="coverage files"):
            _merge_coverage_file(_required_mapping(raw_file, label="coverage file"), files)
    return files, tests, failures


def _common_coverage_metadata(
    *,
    config: DeepCoverageConfig,
    tool_versions: Mapping[str, str],
    suite_signature: str,
    code_input_signature: str,
    support_signature: str,
    publication_input_signature: str,
    measurement_scope_signature: str,
    measurement_complete: bool,
) -> dict[str, object]:
    return {
        "suite_selection": config.suite_selection,
        "measurement_complete": measurement_complete,
        "content_executed": True,
        "tool_versions": dict(sorted(tool_versions.items())),
        "suite_signature": suite_signature,
        "code_input_signature": code_input_signature,
        "support_signature": support_signature,
        "publication_input_signature": publication_input_signature,
        "configuration_signature": config.configuration_signature,
        "measurement_scope_signature": measurement_scope_signature,
        "subprocess_coverage": False,
        "coverage_scope": "main_process_only",
    }


def _coverage_scope_metadata(
    common: Mapping[str, object],
    *,
    relative: str,
    module: str,
    statements: set[int],
    executed: set[int],
    possible_arcs: set[tuple[int, int]],
    executed_arcs: set[tuple[int, int]],
) -> dict[str, object]:
    ranges = _ranges(sorted(statements - executed))
    arcs = sorted(possible_arcs - executed_arcs)
    return {
        **common,
        "relative_path": relative,
        "module_key": module,
        "missing_line_ranges": [list(value) for value in ranges[:_MAX_METADATA_RANGES]],
        "missing_line_ranges_truncated": len(ranges) > _MAX_METADATA_RANGES,
        "missing_branch_arcs": [list(value) for value in arcs[:_MAX_METADATA_ARCS]],
        "missing_branch_arcs_truncated": len(arcs) > _MAX_METADATA_ARCS,
    }


def _merge_module_coverage(
    modules: dict[str, _ModuleAggregate],
    *,
    module: str,
    relative: str,
    owner: ExternalEvidenceFile,
    statements: set[int],
    executed: set[int],
    possible_arcs: set[tuple[int, int]],
    executed_arcs: set[tuple[int, int]],
) -> None:
    module_data = modules.get(module)
    if module_data is None:
        module_data = _ModuleAggregate(relative, owner.version_id)
        modules[module] = module_data
    if module_data.relative_path != relative:
        raise ValueError("deep coverage module maps to multiple files")
    module_data.statements.update(statements)
    module_data.executed.update(executed)
    module_data.possible_arcs.update(possible_arcs)
    module_data.executed_arcs.update(executed_arcs)


def _file_coverage_metrics(
    files: Mapping[str, _CoverageAggregate],
    owners: Mapping[str, ExternalEvidenceFile],
    common: Mapping[str, object],
) -> tuple[list[ExternalProviderMetric], dict[str, _ModuleAggregate], _CoverageTotals]:
    metrics: list[ExternalProviderMetric] = []
    modules: dict[str, _ModuleAggregate] = {}
    totals = _CoverageTotals()
    for file_key in sorted(files):
        file_aggregate = files[file_key]
        relative = file_aggregate.relative_path
        if PurePosixPath(relative).parts[0] not in _PRODUCTION_ROOTS:
            continue
        owner = owners.get(file_key)
        if owner is None:
            raise ValueError("deep coverage normalized file is unowned")
        module = file_aggregate.module
        statements = set(file_aggregate.statements)
        executed = set(file_aggregate.executed)
        possible_arcs = set(file_aggregate.possible_arcs)
        executed_arcs = set(file_aggregate.executed_arcs)
        metrics.extend(
            _coverage_metrics(
                subject_kind="file",
                subject_key=relative,
                statements=statements,
                executed=executed,
                possible_arcs=possible_arcs,
                executed_arcs=executed_arcs,
                version_id=owner.version_id,
                metadata=_coverage_scope_metadata(
                    common,
                    relative=relative,
                    module=module,
                    statements=statements,
                    executed=executed,
                    possible_arcs=possible_arcs,
                    executed_arcs=executed_arcs,
                ),
            )
        )
        _merge_module_coverage(
            modules,
            module=module,
            relative=relative,
            owner=owner,
            statements=statements,
            executed=executed,
            possible_arcs=possible_arcs,
            executed_arcs=executed_arcs,
        )
        totals.add(
            statements=statements,
            executed=executed,
            possible_arcs=possible_arcs,
            executed_arcs=executed_arcs,
        )
    return metrics, modules, totals


def _module_coverage_metrics(
    modules: Mapping[str, _ModuleAggregate],
    common: Mapping[str, object],
) -> list[ExternalProviderMetric]:
    metrics: list[ExternalProviderMetric] = []
    for module in sorted(modules):
        module_aggregate = modules[module]
        statements = set(module_aggregate.statements)
        executed = set(module_aggregate.executed)
        possible_arcs = set(module_aggregate.possible_arcs)
        executed_arcs = set(module_aggregate.executed_arcs)
        metrics.extend(
            _coverage_metrics(
                subject_kind="module",
                subject_key=module,
                statements=statements,
                executed=executed,
                possible_arcs=possible_arcs,
                executed_arcs=executed_arcs,
                version_id=module_aggregate.version_id,
                metadata=_coverage_scope_metadata(
                    common,
                    relative=module_aggregate.relative_path,
                    module=module,
                    statements=statements,
                    executed=executed,
                    possible_arcs=possible_arcs,
                    executed_arcs=executed_arcs,
                ),
            )
        )
    return metrics


def _symbol_coverage_sets(
    symbol: _SymbolObservation,
    file_data: _CoverageAggregate,
) -> tuple[set[int], set[int], set[tuple[int, int]], set[tuple[int, int]]]:
    start = symbol.start_line
    end = symbol.end_line
    return (
        {line for line in file_data.statements if start <= line <= end},
        {line for line in file_data.executed if start <= line <= end},
        {arc for arc in file_data.possible_arcs if start <= arc[0] <= end},
        {arc for arc in file_data.executed_arcs if start <= arc[0] <= end},
    )


def _symbol_test_evidence(
    file_data: _CoverageAggregate,
    executed: set[int],
    tests: Mapping[str, str],
) -> tuple[dict[str, set[int]], dict[str, set[str]]]:
    by_test_lines: dict[str, set[int]] = defaultdict(set)
    by_test_contexts: dict[str, set[str]] = defaultdict(set)
    for line in sorted(executed):
        for context in sorted(file_data.contexts.get(line, set())):
            parsed = _phase_context(context)
            if parsed is None:
                continue
            nodeid, _phase = parsed
            if nodeid not in tests:
                raise ValueError("deep coverage context references an unselected test")
            by_test_lines[nodeid].add(line)
            by_test_contexts[nodeid].add(context)
    return by_test_lines, by_test_contexts


def _one_symbol_coverage(
    symbol: _SymbolObservation,
    file_data: _CoverageAggregate,
    owner: ExternalEvidenceFile,
    tests: Mapping[str, str],
    common: Mapping[str, object],
    measurement_scope_signature: str,
) -> tuple[list[ExternalProviderMetric], dict[tuple[str, str], dict[str, object]]]:
    relative = symbol.relative_path
    module = symbol.module
    qualified = symbol.qualified_name
    start = symbol.start_line
    end = symbol.end_line
    stable_symbol = f"{module}:{qualified}:{start}:{end}"
    statements, executed, possible_arcs, executed_arcs = _symbol_coverage_sets(symbol, file_data)
    metadata = {
        **_coverage_scope_metadata(
            common,
            relative=relative,
            module=module,
            statements=statements,
            executed=executed,
            possible_arcs=possible_arcs,
            executed_arcs=executed_arcs,
        ),
        "symbol_key": stable_symbol,
        "qualified_name": qualified,
        "symbol_kind": symbol.kind,
        "start_line": start,
        "end_line": end,
    }
    metrics = _coverage_metrics(
        subject_kind="symbol",
        subject_key=stable_symbol,
        statements=statements,
        executed=executed,
        possible_arcs=possible_arcs,
        executed_arcs=executed_arcs,
        version_id=owner.version_id,
        metadata=metadata,
    )
    by_test_lines, by_test_contexts = _symbol_test_evidence(file_data, executed, tests)
    relation_metadata: dict[tuple[str, str], dict[str, object]] = {}
    for nodeid in sorted(by_test_lines):
        evidence_lines = by_test_lines[nodeid]
        evidence_contexts = by_test_contexts[nodeid]
        relation_metadata[(nodeid, stable_symbol)] = {
            "relative_path": relative,
            "module_key": module,
            "symbol_key": stable_symbol,
            "qualified_name": qualified,
            "start_line": start,
            "end_line": end,
            "test_nodeids": [nodeid],
            "lines": sorted(evidence_lines)[:_MAX_RELATION_LINES],
            "lines_truncated": len(evidence_lines) > _MAX_RELATION_LINES,
            "contexts": sorted(evidence_contexts)[:_MAX_RELATION_CONTEXTS],
            "contexts_truncated": len(evidence_contexts) > _MAX_RELATION_CONTEXTS,
            "measurement_scope_signature": measurement_scope_signature,
        }
    return metrics, relation_metadata


def _coverage_relations(
    relation_metadata: Mapping[tuple[str, str], Mapping[str, object]],
    owners: Mapping[str, ExternalEvidenceFile],
) -> tuple[ExternalProviderRelation, ...]:
    return tuple(
        ExternalProviderRelation(
            external_relation_identity(
                PYTEST_COVERAGE_PROVIDER_ID,
                relation_kind="test_covers_symbol",
                source_kind="symbol",
                source_key=f"pytest-nodeid:{nodeid}",
                target_kind="symbol",
                target_key=symbol,
            ),
            "test_covers_symbol",
            "symbol",
            f"pytest-nodeid:{nodeid}",
            "symbol",
            symbol,
            confidence=1.0,
            target_version_id=owners[str(metadata["relative_path"]).casefold()].version_id,
            metadata=metadata,
        )
        for (nodeid, symbol), metadata in sorted(relation_metadata.items())
    )


def _test_outcome_relations(
    tests: Mapping[str, str],
    *,
    common: Mapping[str, object],
    measurement_scope_signature: str,
) -> tuple[ExternalProviderRelation, ...]:
    """Publish each exact selected test outcome as a provider-owned receipt.

    The relation intentionally uses portable contract/run endpoints because a
    pytest nodeid is neither a Code symbol nor a durable first-class Test row in
    schema v5.  It establishes execution outcome only; it does not claim that
    the test asserts or proves any invariant.
    """

    run_key = f"coverage-run:{measurement_scope_signature}"
    return tuple(
        ExternalProviderRelation(
            external_relation_identity(
                PYTEST_COVERAGE_PROVIDER_ID,
                relation_kind="declared_test_outcome",
                source_kind="contract",
                source_key=f"pytest-nodeid:{nodeid}",
                target_kind="run",
                target_key=run_key,
            ),
            "declared_test_outcome",
            "contract",
            f"pytest-nodeid:{nodeid}",
            "run",
            run_key,
            confidence=1.0,
            metadata={
                **common,
                "nodeid": nodeid,
                "outcome": outcome,
                "claim_scope": "exact_selected_test_execution_outcome",
                "assertion_or_invariant_proof": False,
                "measurement_scope_signature": measurement_scope_signature,
            },
        )
        for nodeid, outcome in sorted(tests.items())
    )


def _symbol_coverage_metrics_and_relations(
    symbols: Mapping[tuple[str, str, str, int, int], _SymbolObservation],
    files: Mapping[str, _CoverageAggregate],
    owners: Mapping[str, ExternalEvidenceFile],
    tests: Mapping[str, str],
    common: Mapping[str, object],
    measurement_scope_signature: str,
) -> tuple[list[ExternalProviderMetric], tuple[ExternalProviderRelation, ...]]:
    metrics: list[ExternalProviderMetric] = []
    relation_metadata: dict[tuple[str, str], dict[str, object]] = {}
    for symbol_identity in sorted(symbols):
        symbol = symbols[symbol_identity]
        if PurePosixPath(symbol.relative_path).parts[0] not in _PRODUCTION_ROOTS:
            continue
        file_data = files.get(symbol.relative_path.casefold())
        if file_data is None:
            continue
        symbol_metrics, symbol_relations = _one_symbol_coverage(
            symbol,
            file_data,
            owners[symbol.relative_path.casefold()],
            tests,
            common,
            measurement_scope_signature,
        )
        metrics.extend(symbol_metrics)
        relation_metadata.update(symbol_relations)
    if len(relation_metadata) > _MAX_RELATIONS:
        raise ValueError("deep coverage relation count exceeds its bound")
    return metrics, _coverage_relations(relation_metadata, owners)


def _coverage_test_counts(
    tests: Mapping[str, str],
    *,
    collected_count: int,
    selected_nodeids: tuple[str, ...],
    shards_total: int,
    shards_reused: int,
) -> dict[str, int]:
    return {
        "tests_collected": collected_count,
        "tests_selected": len(selected_nodeids),
        "tests_passed": sum(value == "passed" for value in tests.values()),
        "tests_failed": sum(value == "failed" for value in tests.values()),
        "tests_skipped": sum(value == "skipped" for value in tests.values()),
        "shards_total": shards_total,
        "shards_reused": shards_reused,
    }


def _run_coverage_metrics(
    totals: _CoverageTotals,
    test_counts: Mapping[str, int],
    *,
    run_key: str,
    common: Mapping[str, object],
) -> list[ExternalProviderMetric]:
    run_values: dict[str, tuple[float, str]] = {
        "executable_lines": (totals.executable_lines, "count"),
        "covered_lines": (totals.covered_lines, "count"),
        "missing_lines": (totals.executable_lines - totals.covered_lines, "count"),
        "line_coverage_percent": (
            (totals.covered_lines / totals.executable_lines * 100.0)
            if totals.executable_lines
            else 100.0,
            "percent",
        ),
        "branch_exits": (totals.branch_exits, "count"),
        "covered_branch_exits": (totals.covered_branch_exits, "count"),
        "missing_branch_exits": (
            totals.branch_exits - totals.covered_branch_exits,
            "count",
        ),
        "branch_coverage_percent": (
            (totals.covered_branch_exits / totals.branch_exits * 100.0)
            if totals.branch_exits
            else 100.0,
            "percent",
        ),
    }
    for name, count_value in test_counts.items():
        run_values[name] = (float(count_value), "count")
    return [
        _metric(
            subject_kind="run",
            subject_key=run_key,
            name=name,
            value=float(metric_value),
            unit=unit,
            version_id=None,
            metadata=common,
        )
        for name, (metric_value, unit) in run_values.items()
    ]


def _failure_findings(
    failures: Sequence[Mapping[str, object]],
    tests: Mapping[str, str],
    owners: Mapping[str, ExternalEvidenceFile],
    common: Mapping[str, object],
) -> list[ExternalProviderFinding]:
    findings: list[ExternalProviderFinding] = []
    for raw in failures:
        nodeid = _required_text(raw.get("nodeid"), label="failure nodeid", maximum=16_384)
        phase = _required_text(raw.get("phase"), label="failure phase", maximum=32)
        message = _required_text(raw.get("message"), label="failure message", maximum=4096)
        relative = _relative_path(raw.get("relative_path"), label="failure path")
        owner = owners.get(relative.casefold())
        if owner is None or nodeid not in tests:
            raise ValueError("pytest failure is not owned by the selected suite")
        line = max(1, _required_int(raw.get("line"), label="failure line"))
        code = f"pytest_{phase}_failed"
        identity = external_signature(
            "external-finding-v1",
            {
                "provider_id": PYTEST_COVERAGE_PROVIDER_ID,
                "path": relative,
                "category": "test_failure",
                "code": code,
                "nodeid": nodeid,
                "message": message,
                "line": line,
            },
        )
        findings.append(
            ExternalProviderFinding(
                identity,
                owner.version_id,
                owner.relative_path,
                "test_failure",
                code,
                "warning",
                message,
                True,
                1.0,
                None,
                "trusted_deep_test",
                line,
                0,
                line,
                0,
                metadata={
                    **common,
                    "nodeid": nodeid,
                    "phase": phase,
                    "outcome": "failed",
                },
            )
        )
    return findings


def _validate_normalized_publications(
    findings: Sequence[ExternalProviderFinding],
    metrics: Sequence[ExternalProviderMetric],
) -> None:
    if len({item.portable_finding_id for item in findings}) != len(findings):
        raise ValueError("deep coverage produced duplicate findings")
    if len({item.portable_metric_id for item in metrics}) != len(metrics):
        raise ValueError("deep coverage produced duplicate metrics")


def _normalize(
    shards: Sequence[Mapping[str, object]],
    *,
    raw_symbols: Sequence[Mapping[str, object]],
    owners: Mapping[str, ExternalEvidenceFile],
    config: DeepCoverageConfig,
    tool_versions: Mapping[str, str],
    suite_signature: str,
    code_input_signature: str,
    support_signature: str,
    publication_input_signature: str,
    measurement_scope_signature: str,
    measurement_complete: bool,
    collected_count: int,
    selected_nodeids: tuple[str, ...],
    shards_reused: int,
) -> tuple[
    tuple[ExternalProviderFinding, ...],
    tuple[ExternalProviderMetric, ...],
    tuple[ExternalProviderRelation, ...],
    dict[str, int],
]:
    symbols = _normalize_symbols(raw_symbols, owners)
    files, tests, failures = _normalize_shards(shards)
    common = _common_coverage_metadata(
        config=config,
        tool_versions=tool_versions,
        suite_signature=suite_signature,
        code_input_signature=code_input_signature,
        support_signature=support_signature,
        publication_input_signature=publication_input_signature,
        measurement_scope_signature=measurement_scope_signature,
        measurement_complete=measurement_complete,
    )
    metrics, modules, totals = _file_coverage_metrics(files, owners, common)
    metrics.extend(_module_coverage_metrics(modules, common))
    symbol_metrics, coverage_relations = _symbol_coverage_metrics_and_relations(
        symbols,
        files,
        owners,
        tests,
        common,
        measurement_scope_signature,
    )
    metrics.extend(symbol_metrics)
    relations = (
        *coverage_relations,
        *_test_outcome_relations(
            tests,
            common=common,
            measurement_scope_signature=measurement_scope_signature,
        ),
    )
    if len(relations) > _MAX_RELATIONS:
        raise ValueError("deep coverage total relation count exceeds its bound")
    test_counts = _coverage_test_counts(
        tests,
        collected_count=collected_count,
        selected_nodeids=selected_nodeids,
        shards_total=len(shards),
        shards_reused=shards_reused,
    )
    metrics.extend(
        _run_coverage_metrics(
            totals,
            test_counts,
            run_key=f"coverage-run:{measurement_scope_signature}",
            common=common,
        )
    )
    findings = _failure_findings(failures, tests, owners, common)
    _validate_normalized_publications(findings, metrics)
    return (
        tuple(sorted(findings, key=lambda item: item.portable_finding_id)),
        tuple(sorted(metrics, key=lambda item: item.portable_metric_id)),
        tuple(sorted(relations, key=lambda item: item.portable_relation_id)),
        test_counts,
    )


def _controlled_execution_environment(
    environment: Mapping[str, str],
    durable_scratch: Path,
    runtime_root: Path,
) -> tuple[dict[str, str], Path]:
    controlled = dict(environment)
    home_directory = trusted_deep_home_directory()
    controlled["HOME"] = home_directory
    if os.name == "nt":
        controlled["USERPROFILE"] = home_directory
    host_names: tuple[str, ...] = ("PATH", "PATHEXT")
    if os.name == "nt":
        # CPython 3.14 imports Winsock-backed modules while pytest configures
        # debugging. Windows cannot initialize those providers without its
        # canonical runtime roots, even when PATH itself is present.
        host_names += ("COMSPEC", "SYSTEMROOT", "WINDIR")
    for name in host_names:
        value = os.environ.get(name)
        if value:
            controlled[name] = value
    controlled["NEOCORTEX_AUDIT_LAB_ROOT"] = str(durable_scratch)
    for name in ("TEMP", "TMP", "TMPDIR", "PYTHONPYCACHEPREFIX"):
        controlled[name] = str(runtime_root)
    controlled["PYTEST_ADDOPTS"] = ""
    controlled["COVERAGE_FILE"] = str(runtime_root / ".coverage")
    return controlled, runtime_root


def _validate_prepared_execution_input(
    prepared: DeepCoveragePreparedInput,
    *,
    project_root: Path,
    staged: Mapping[str, ExternalEvidenceFile],
    config: DeepCoverageConfig,
) -> None:
    expected_manifest = tuple(_source_manifest(tuple(staged.values())))
    expected_code_signature = _input_signature(expected_manifest)
    expected_tool_versions = _tool_versions()
    expected_publication_signature = _publication_input_signature(
        project_root,
        code_input_signature=prepared.code_input_signature,
        support_signature=prepared.support_signature,
        config=config,
        tool_versions=prepared.tool_versions,
    )
    checks = (
        Path(prepared.trusted_root).resolve(strict=True) == project_root,
        prepared.configuration_signature == config.configuration_signature,
        prepared.manifest == expected_manifest,
        prepared.code_input_signature == expected_code_signature,
        dict(prepared.tool_versions) == expected_tool_versions,
        prepared.publication_input_signature == expected_publication_signature,
        prepared.process_invocations == 1,
        prepared.support_files_verified >= 1,
        prepared.support_bytes_verified >= 0,
        prepared.preparation_milliseconds >= 0,
    )
    if not all(checks):
        raise ValueError("deep coverage prepared input is incompatible")


def _execution_context(
    stage_root: Path,
    staged: Mapping[str, ExternalEvidenceFile],
    environment: Mapping[str, str],
    *,
    trusted_root: Path,
    scratch_root: Path,
    config: DeepCoverageConfig,
    prepared_input: DeepCoveragePreparedInput | None,
    progress: DeepCoverageProgressCallback | None,
    started: float,
) -> _DeepCoverageContext:
    project_root = _validate_trusted_root(trusted_root)
    runtime_container, durable_scratch = _validate_scratch(
        stage_root,
        scratch_root,
        project_root,
    )
    runtime_root = _private_runtime_directory(
        runtime_container / "r",
        allow_existing=False,
        label="deep coverage runtime directory",
    )
    owners = _owners_by_relative(staged)
    controlled_environment, runtime_root = _controlled_execution_environment(
        environment,
        durable_scratch,
        runtime_root,
    )
    if prepared_input is None:
        prepared = _prepare_deep_coverage_input(
            project_root,
            tuple(staged.values()),
            config,
            environment=controlled_environment,
            deadline=started + config.time_budget_seconds,
        )
        preparation_elapsed = 0.0
    else:
        prepared = prepared_input
        _validate_prepared_execution_input(
            prepared,
            project_root=project_root,
            staged=staged,
            config=config,
        )
        preparation_elapsed = prepared.preparation_milliseconds / 1000.0
    return _DeepCoverageContext(
        started,
        project_root,
        durable_scratch,
        runtime_root,
        owners,
        controlled_environment,
        config,
        prepared,
        preparation_elapsed,
        progress,
    )


def _remaining_execution_seconds(
    context: _DeepCoverageContext,
    command: tuple[str, ...],
    *,
    progress_made: bool = False,
) -> float:
    elapsed = time.monotonic() - context.started + context.preparation_elapsed
    multiplier = _PROGRESS_EXTENSION_MULTIPLIER if progress_made else 1.0
    budget = context.config.time_budget_seconds * multiplier
    remaining = budget - elapsed
    if remaining <= 0:
        raise subprocess.TimeoutExpired(command, budget)
    return max(0.001, remaining)


def _enforce_execution_deadline(
    context: _DeepCoverageContext,
    phase: str,
    *,
    progress_made: bool,
) -> None:
    _remaining_execution_seconds(
        context,
        ("pytest-coverage-trusted-deep", phase),
        progress_made=progress_made,
    )


def _emit_coverage_progress(
    context: _DeepCoverageContext,
    suite: _CollectedSuite,
    *,
    phase: Literal["collected", "shard_started", "shard_reused", "shard_completed"],
    completed_shards: int,
    current_shard: int | None,
    reused_shards: int,
    shard_duration_seconds: int | None = None,
) -> None:
    if context.progress is None:
        return
    elapsed = max(
        0,
        round(time.monotonic() - context.started + context.preparation_elapsed),
    )
    try:
        context.progress(
            DeepCoverageProgress(
                phase,
                completed_shards,
                len(suite.shards),
                current_shard,
                len(suite.selected_nodeids),
                reused_shards,
                elapsed,
                shard_duration_seconds,
            )
        )
    except Exception:
        # Progress is advisory; a broken frontend cannot invalidate measured evidence.
        return


def _context_request(
    context: _DeepCoverageContext,
    *,
    mode: Literal["collect", "shard"],
) -> dict[str, object]:
    prepared = context.prepared
    return _request_base(
        mode=mode,
        project_root=context.project_root,
        scratch_root=context.runtime_root,
        manifest=prepared.manifest,
        input_signature=prepared.code_input_signature,
        support_signature=prepared.support_signature,
        config=context.config,
        tool_versions=prepared.tool_versions,
    )


def _collect_suite(context: _DeepCoverageContext) -> _CollectedSuite:
    remaining = _remaining_execution_seconds(context, ("pytest", "collect"))
    request = _context_request(context, mode="collect")
    request["selectors"] = list(context.config.test_selectors)
    request["nodeids"] = []
    request_signature = _request_digest(request)
    payload, stdout_bytes, stderr_bytes = _run_worker(
        request,
        scratch_root=context.runtime_root,
        environment=context.environment,
        timeout_seconds=remaining,
    )
    collected, raw_symbols = _validate_collect(
        payload,
        request_signature=request_signature,
        tool_versions=context.prepared.tool_versions,
    )
    if not collected:
        raise ValueError("deep coverage collected no tests")
    selected = collected[: context.config.max_tests]
    suite_signature = external_signature(
        "deep-coverage-suite-v1",
        {
            "suite_selection": context.config.suite_selection,
            "selectors": list(context.config.test_selectors),
            "nodeids": list(collected),
        },
    )
    measurement_scope_signature = external_signature(
        "deep-coverage-scope-v1",
        {
            "suite_signature": suite_signature,
            "configuration_signature": context.config.configuration_signature,
            "tool_versions": dict(context.prepared.tool_versions),
            "selected_nodeids": list(selected),
        },
    )
    shards = tuple(
        tuple(selected[index : index + context.config.shard_size])
        for index in range(0, len(selected), context.config.shard_size)
    )
    return _CollectedSuite(
        collected,
        selected,
        shards,
        raw_symbols,
        suite_signature,
        measurement_scope_signature,
        len(selected) == len(collected),
        stdout_bytes,
        stderr_bytes,
    )


def _shard_signature(
    context: _DeepCoverageContext,
    suite: _CollectedSuite,
    *,
    index: int,
    nodeids: tuple[str, ...],
) -> str:
    prepared = context.prepared
    return external_signature(
        "deep-coverage-shard-v1",
        {
            "input_signature": prepared.code_input_signature,
            "support_signature": prepared.support_signature,
            "configuration_signature": context.config.configuration_signature,
            "tool_versions": dict(prepared.tool_versions),
            "suite_signature": suite.suite_signature,
            "measurement_scope_signature": suite.measurement_scope_signature,
            "index": index,
            "nodeids": list(nodeids),
        },
    )


def _plan_shard(
    context: _DeepCoverageContext,
    suite: _CollectedSuite,
    checkpoint_root: Path,
    *,
    index: int,
    nodeids: tuple[str, ...],
) -> _ShardPlan:
    shard_signature = _shard_signature(context, suite, index=index, nodeids=nodeids)
    return _ShardPlan(
        index,
        nodeids,
        shard_signature,
        _checkpoint_path(checkpoint_root, shard_signature),
    )


def _shard_request(
    context: _DeepCoverageContext,
    suite: _CollectedSuite,
    plan: _ShardPlan,
) -> dict[str, object]:
    request = _context_request(context, mode="shard")
    request.update(
        {
            "selectors": [],
            "nodeids": list(plan.nodeids),
            "suite_signature": suite.suite_signature,
            "measurement_scope_signature": suite.measurement_scope_signature,
            "shard_signature": plan.shard_signature,
            "shard_index": plan.index,
        }
    )
    return request


def _validated_checkpoint(
    plan: _ShardPlan,
    context: _DeepCoverageContext,
    suite: _CollectedSuite,
) -> Mapping[str, object] | None:
    request = _shard_request(context, suite, plan)
    loaded = _load_checkpoint(
        plan.checkpoint,
        shard_signature=plan.shard_signature,
        checkpoint_request_signature=_checkpoint_request_digest(request),
    )
    if loaded is None:
        return None
    cached, execution_request_signature = loaded
    try:
        validated = _validate_shard(
            cached,
            request_signature=execution_request_signature,
            shard_nodeids=plan.nodeids,
            tool_versions=context.prepared.tool_versions,
            owners=context.owners,
        )
    except (TypeError, ValueError):
        return None
    return validated if _shard_is_cacheable(validated) else None


def _run_shard(
    plan: _ShardPlan,
    context: _DeepCoverageContext,
    suite: _CollectedSuite,
    *,
    progress_made: bool,
) -> tuple[Mapping[str, object], int, int, str]:
    remaining = _remaining_execution_seconds(
        context,
        ("pytest",),
        progress_made=progress_made,
    )
    request = _shard_request(context, suite, plan)
    payload, stdout_bytes, stderr_bytes = _run_worker(
        request,
        scratch_root=context.runtime_root,
        environment=context.environment,
        timeout_seconds=remaining,
    )
    validated = _validate_shard(
        payload,
        request_signature=_request_digest(request),
        shard_nodeids=plan.nodeids,
        tool_versions=context.prepared.tool_versions,
        owners=context.owners,
    )
    return validated, stdout_bytes, stderr_bytes, _checkpoint_request_digest(request)


def _execute_shards(
    context: _DeepCoverageContext,
    suite: _CollectedSuite,
) -> _ShardExecution:
    checkpoint_root = _owned_plain_directory(
        context.durable_scratch / "checkpoints",
        allow_existing=True,
        label="deep coverage checkpoint directory",
        require_private=False,
    )
    results: list[Mapping[str, object]] = []
    shards_reused = 0
    stdout_bytes = context.prepared.stdout_bytes + suite.stdout_bytes
    stderr_bytes = context.prepared.stderr_bytes + suite.stderr_bytes
    process_invocations = context.prepared.process_invocations + 1
    for index, nodeids in enumerate(suite.shards):
        _enforce_execution_deadline(
            context,
            "plan-shard",
            progress_made=bool(results),
        )
        plan = _plan_shard(
            context,
            suite,
            checkpoint_root,
            index=index,
            nodeids=nodeids,
        )
        cached = _validated_checkpoint(plan, context, suite)
        _enforce_execution_deadline(
            context,
            "validate-checkpoint",
            progress_made=bool(results),
        )
        if cached is not None:
            results.append(cached)
            shards_reused += 1
            _emit_coverage_progress(
                context,
                suite,
                phase="shard_reused",
                completed_shards=len(results),
                current_shard=index,
                reused_shards=shards_reused,
            )
            _enforce_execution_deadline(
                context,
                "report-reused-shard",
                progress_made=True,
            )
            continue
        _emit_coverage_progress(
            context,
            suite,
            phase="shard_started",
            completed_shards=len(results),
            current_shard=index,
            reused_shards=shards_reused,
        )
        shard_started = time.monotonic()
        validated, out_bytes, err_bytes, checkpoint_request_signature = _run_shard(
            plan,
            context,
            suite,
            progress_made=bool(results),
        )
        _enforce_execution_deadline(
            context,
            "validate-shard-result",
            progress_made=bool(results),
        )
        results.append(validated)
        stdout_bytes += out_bytes
        stderr_bytes += err_bytes
        process_invocations += 1
        if _shard_is_cacheable(validated):
            _save_checkpoint(
                plan.checkpoint,
                shard_signature=plan.shard_signature,
                checkpoint_request_signature=checkpoint_request_signature,
                result=validated,
            )
        _enforce_execution_deadline(
            context,
            "save-shard-checkpoint",
            progress_made=True,
        )
        _emit_coverage_progress(
            context,
            suite,
            phase="shard_completed",
            completed_shards=len(results),
            current_shard=index,
            reused_shards=shards_reused,
            shard_duration_seconds=max(0, round(time.monotonic() - shard_started)),
        )
        _enforce_execution_deadline(
            context,
            "report-completed-shard",
            progress_made=True,
        )
    _enforce_execution_deadline(
        context,
        "complete-shards",
        progress_made=bool(results),
    )
    return _ShardExecution(
        tuple(results),
        shards_reused,
        stdout_bytes,
        stderr_bytes,
        process_invocations,
    )


def _finalize_execution(
    context: _DeepCoverageContext,
    suite: _CollectedSuite,
    shards: _ShardExecution,
) -> DeepCoverageExecution:
    prepared = context.prepared
    progress_made = bool(shards.results)
    _enforce_execution_deadline(
        context,
        "normalize-results",
        progress_made=progress_made,
    )
    findings, metrics, relations, normalized_counts = _normalize(
        shards.results,
        raw_symbols=suite.raw_symbols,
        owners=context.owners,
        config=context.config,
        tool_versions=prepared.tool_versions,
        suite_signature=suite.suite_signature,
        code_input_signature=prepared.code_input_signature,
        support_signature=prepared.support_signature,
        publication_input_signature=prepared.publication_input_signature,
        measurement_scope_signature=suite.measurement_scope_signature,
        measurement_complete=suite.measurement_complete,
        collected_count=len(suite.collected_nodeids),
        selected_nodeids=suite.selected_nodeids,
        shards_reused=shards.shards_reused,
    )
    _enforce_execution_deadline(
        context,
        "finalize-results",
        progress_made=progress_made,
    )
    limitations = [
        "coverage_main_process_only",
        "subprocess_coverage_not_collected",
        "git_ignored_support_files_excluded_from_support_signature",
        "codex_control_files_excluded_from_support_signature",
    ]
    if not suite.measurement_complete:
        limitations.append("suite_truncated_by_max_tests")
    counters = {
        **normalized_counts,
        "process_invocations": shards.process_invocations,
        "stdout_bytes": shards.stdout_bytes,
        "stderr_bytes": shards.stderr_bytes,
        "measurement_complete": int(suite.measurement_complete),
        "support_files_verified": prepared.support_files_verified,
        "support_bytes_verified": prepared.support_bytes_verified,
        "preparation_milliseconds": prepared.preparation_milliseconds,
        "nominal_time_budget_seconds": round(context.config.time_budget_seconds),
        "progress_time_limit_seconds": round(
            context.config.time_budget_seconds * _PROGRESS_EXTENSION_MULTIPLIER
        ),
    }
    return DeepCoverageExecution(
        findings,
        metrics,
        relations,
        shards.stdout_bytes,
        shards.stderr_bytes,
        shards.process_invocations,
        context.config.suite_selection,
        suite.measurement_complete,
        suite.suite_signature,
        suite.measurement_scope_signature,
        counters,
        tuple(limitations),
    )


def execute_pytest_coverage(
    stage_root: Path,
    staged: Mapping[str, ExternalEvidenceFile],
    environment: Mapping[str, str],
    *,
    trusted_root: Path,
    scratch_root: Path,
    config: DeepCoverageConfig,
    prepared_input: DeepCoveragePreparedInput | None = None,
    progress: DeepCoverageProgressCallback | None = None,
) -> DeepCoverageExecution:
    """Execute and normalize one exact trusted-deep suite selection.

    Pytest executes only the physically canonical trusted repository. The
    staged mapping supplies immutable Code ownership/fingerprints; a bounded
    Git support observation covers configuration and non-ignored fixtures.
    ``scratch_root`` remains outside both roots so validated passing shard
    results survive an interrupted provider attempt.
    """

    context = _execution_context(
        stage_root,
        staged,
        environment,
        trusted_root=trusted_root,
        scratch_root=scratch_root,
        config=config,
        prepared_input=prepared_input,
        progress=progress,
        started=time.monotonic(),
    )
    suite = _collect_suite(context)
    _enforce_execution_deadline(
        context,
        "complete-collection",
        progress_made=False,
    )
    _emit_coverage_progress(
        context,
        suite,
        phase="collected",
        completed_shards=0,
        current_shard=None,
        reused_shards=0,
    )
    _enforce_execution_deadline(
        context,
        "report-collection",
        progress_made=False,
    )
    shards = _execute_shards(context, suite)
    return _finalize_execution(context, suite, shards)


__all__ = [
    "DEEP_COVERAGE_CHECKPOINT_SCHEMA",
    "DEEP_COVERAGE_COLLECT_SCHEMA",
    "DEEP_COVERAGE_PROVIDER_SCHEMA",
    "DEEP_COVERAGE_REQUEST_SCHEMA",
    "DEEP_COVERAGE_SHARD_SCHEMA",
    "PYTEST_COVERAGE_PROVIDER_ID",
    "DeepCoverageConfig",
    "DeepCoverageExecution",
    "DeepCoveragePreparedInput",
    "DeepCoverageProgress",
    "DeepCoverageProgressCallback",
    "deep_coverage_input_signature",
    "execute_pytest_coverage",
    "prepare_deep_coverage_input",
]
