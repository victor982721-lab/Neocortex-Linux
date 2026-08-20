"""Real bounded providers for protected, trusted-static and trusted-deep analysis."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path, PurePosixPath

from packaging.utils import canonicalize_name
from neocortex.semgrep_tool_contract import managed_semgrep_version

from _03_Progreso import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress

from .bounded_subprocess import SubprocessOutputLimitError, run_bounded_capture
from .code_architecture_contracts import (
    ARCHITECTURE_BASELINE_ID,
    ARCHITECTURE_CONTRACT_SCHEMA,
    PRODUCTION_ROOT_PACKAGES,
)
from .code.contracts.target_registry import (
    CORE_RESPONSIBILITY_REGISTRY_SCHEMA,
    core_architecture_target_fingerprint,
)
from .code_external_evidence import (
    RUFF_CONFIGURATION_SIGNATURE,
    RUFF_MAX_DIAGNOSTICS,
    RUFF_MAX_TOTAL_BYTES,
    ExternalEvidenceFile,
    ExternalEvidencePublication,
    ExternalRunStatus,
    RuffEvidenceProvider,
    _controlled_environment,
    _parse_diagnostics,
    _stage_external_inputs,
    _validated_staging_parent,
    external_input_signature,
    validate_external_inputs,
)
from .code_contracts import (
    DEEP_CONFIGURATION_SCHEMA,
    LEGACY_DEEP_CONFIGURATION_SCHEMA,
    deep_configuration_payload as build_deep_configuration_payload,
    deep_configuration_signature as calculate_deep_configuration_signature,
)
from .external_deep_coverage import (
    DEEP_COVERAGE_PROVIDER_SCHEMA,
    PYTEST_COVERAGE_PROVIDER_ID,
    DeepCoverageConfig,
    DeepCoverageExecution,
    DeepCoveragePreparedInput,
    DeepCoverageProgress,
    _owned_plain_directory,
    execute_pytest_coverage,
    prepare_deep_coverage_input,
    trusted_deep_home_directory,
)
from .external_dependency_hygiene import (
    DEPTRY_LIMITATIONS,
    DEPTRY_PACKAGE_MODULE_NAME_MAP,
    DEPTRY_PROVIDER_ID,
    DEPTRY_PROVIDER_SCHEMA,
    DependencyHygieneExecution,
    execute_deptry_dependency_hygiene,
)
from .external_evidence_models import (
    AnalysisProfile,
    ExternalEvidenceProvider,
    ExternalProviderBaseline,
    ExternalProviderFinding,
    ExternalProviderMetric,
    ExternalProviderPublication,
    ExternalProviderRelation,
    ExternalRunInput,
    InvalidationStrategy,
    ProviderDescriptor,
    ProviderLimits,
    ProviderTrust,
    external_finding_identity,
    external_provider_result_digest,
    external_root_identity,
    external_signature,
    normalize_external_finding_message,
)
from .external_git_history import (
    GIT_HISTORY_PROVIDER_ID,
    GIT_HISTORY_PROVIDER_SCHEMA,
    GitHistoryConfig,
    GitHistoryExecution,
    GitRepositorySnapshot,
    execute_git_history,
    git_history_input_signature,
    inspect_git_repository,
)
from .external_mutation_cosmic_ray import (
    COSMIC_RAY_MUTATION_PROVIDER_ID,
    COSMIC_RAY_MUTATION_PROVIDER_SCHEMA,
    FocalMutationConfig,
    FocalMutationExecution,
    MutationAbstentionError,
    cosmic_ray_tool_version,
    execute_cosmic_ray_mutation,
    mutation_input_signature,
)
from .external_architecture_providers import (
    ArchitectureProviderExecution,
    execute_complexipy_cognitive,
    execute_grimp_architecture,
    execute_ruff_analyze_imports,
)
from .external_unused_vulture import (
    VULTURE_UNUSED_PROVIDER_ID,
    VULTURE_UNUSED_PROVIDER_SCHEMA,
    VultureUnusedExecution,
    execute_vulture_unused,
)
from .external_semgrep_invariants import (
    SEMGREP_INVARIANTS_PROVIDER_ID,
    SEMGREP_INVARIANTS_PROVIDER_SCHEMA,
    SEMGREP_INVARIANT_RULE_IDS,
    SEMGREP_RULESET_SHA256,
    SEMGREP_RULESET_VERSION,
    SemgrepInvariantExecution,
    execute_semgrep_invariants,
)
from .external_supply_chain_audit import (
    INSTALLED_PACKAGE_PROVIDER_ID,
    INSTALLED_PACKAGE_PROVIDER_SCHEMA,
    PIP_AUDIT_PROVIDER_ID,
    PIP_AUDIT_PROVIDER_SCHEMA,
    PIP_AUDIT_LIMITATIONS,
    PIP_AUDIT_SERVICE,
    InstalledPackageInventoryExecution,
    PipAuditExecution,
    execute_installed_package_inventory,
    execute_pip_audit_known_vulnerabilities,
    installed_environment_distributions,
)
from .semantic_models import fingerprint_bytes

RUFF_PROTECTED_PROVIDER_ID = "ruff-protected-basic"
RUFF_TRUSTED_PROVIDER_ID = "ruff-trusted-project"
MYPY_PROVIDER_ID = "mypy-trusted-project"
PYRIGHT_PROVIDER_ID = "pyright-trusted-project"
RUFF_ANALYZE_PROVIDER_ID = "ruff-analyze-imports"
GRIMP_ARCHITECTURE_PROVIDER_ID = "grimp-architecture"
COMPLEXIPY_COGNITIVE_PROVIDER_ID = "complexipy-cognitive"

_CONFIG_LIMIT_BYTES = 1024 * 1024
_STDOUT_LIMIT_BYTES = 8 * 1024 * 1024
_STDERR_LIMIT_BYTES = 128 * 1024
_TOOL_TIMEOUT_SECONDS = 180.0
_MYPY_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
_PYRIGHT_MEMORY_BYTES = 3 * 1024 * 1024 * 1024
_PYRIGHT_NODE_OLD_SPACE_MIB = 2304
_RUFF_MEMORY_BYTES = 512 * 1024 * 1024
_GRIMP_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
_COMPLEXIPY_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
_VULTURE_MEMORY_BYTES = 512 * 1024 * 1024
_SEMGREP_MEMORY_BYTES = 1024 * 1024 * 1024
_DEPTRY_MEMORY_BYTES = 1024 * 1024 * 1024
_PIP_AUDIT_MEMORY_BYTES = 512 * 1024 * 1024
_PIP_AUDIT_SOURCE_INPUTS = (
    "MANIFEST.in",
    "constraints.txt",
    "constraints-linux-cp314.lock",
    "pyproject.toml",
    "tools/quality_gate_supply_policy.json",
    "tools/release_linux.py",
)
_PACKAGE_INVENTORY_MEMORY_BYTES = 512 * 1024 * 1024
_GIT_HISTORY_MEMORY_BYTES = 512 * 1024 * 1024
_FOCAL_MUTATION_MEMORY_BYTES = 4 * 1024 * 1024 * 1024
_FOCAL_MUTATION_OUTPUT_BYTES = 8 * 1024 * 1024 + 256 * 1024
_DEEP_COVERAGE_MEMORY_BYTES = 4 * 1024 * 1024 * 1024
_DEEP_COVERAGE_OUTPUT_BYTES = 32 * 1024 * 1024
_DEEP_COVERAGE_FINDING_BOUND = 2_000
_FOCAL_MUTATION_DURABLE_NAMESPACE = "m2"
_DEEP_COVERAGE_DURABLE_NAMESPACE = "d2"
_VULTURE_LIMITATIONS = (
    "vulture_confidence_below_100_is_heuristic",
    "static_name_analysis_cannot_prove_runtime_unused",
    "decorators_callbacks_registries_reexports_and_dynamic_access_require_correlation",
    "advisory_only_no_mutation_authority",
)


def _unexpected_exit_message(
    tool_name: str,
    completed: subprocess.CompletedProcess[bytes],
) -> str:
    """Return one bounded single-line explanation for a non-contractual exit."""

    raw = completed.stderr or completed.stdout
    detail = " ".join(raw.decode("utf-8", errors="replace").split())[:2048]
    prefix = f"{tool_name}_unexpected_exit:{completed.returncode}"
    return prefix if not detail else f"{prefix}:{detail}"


def _package_version(name: str) -> str | None:
    try:
        version = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    return version if version and len(version.encode("utf-8")) <= 256 else None


def _deep_tool_version() -> str | None:
    pytest_version = _package_version("pytest")
    coverage_version = _package_version("coverage")
    if pytest_version is None or coverage_version is None:
        return None
    value = f"pytest={pytest_version};coverage={coverage_version}"
    return value if len(value.encode("utf-8")) <= 256 else None


def _runtime_parent_candidate(
    candidate: Path,
    *,
    trusted: str,
    audit_lab: str,
    require_private: bool,
) -> Path | None:
    """Resolve one real runtime parent and reject aliases or unsafe permissions."""

    if not candidate.is_absolute():
        return None
    try:
        configured = Path(os.path.abspath(candidate))
        metadata = os.lstat(configured)
        resolved = configured.resolve(strict=True)
        canonical_metadata = os.stat(resolved, follow_symlinks=False)
    except (OSError, RuntimeError, ValueError):
        return None
    if (
        configured != resolved
        or not stat.S_ISDIR(metadata.st_mode)
        or not stat.S_ISDIR(canonical_metadata.st_mode)
        or (int(metadata.st_dev), int(metadata.st_ino))
        != (int(canonical_metadata.st_dev), int(canonical_metadata.st_ino))
        or not os.access(resolved, os.W_OK | os.X_OK)
    ):
        return None
    mode = stat.S_IMODE(metadata.st_mode)
    effective_uid = getattr(os, "geteuid", lambda: int(metadata.st_uid))()
    normalized = os.path.normcase(os.path.abspath(resolved))
    if require_private:
        if int(metadata.st_uid) != int(effective_uid) or mode & 0o077 or (mode & 0o700) != 0o700:
            return None
    else:
        # The canonical host tmp.mount is deliberately owned by the unprivileged
        # overflow/nobody UID.  It remains a valid mkdtemp parent because the
        # sticky world-writable directory is a real, non-aliased `/tmp` or
        # `/var/tmp`; no process running as that locked UID can replace entries.
        canonical_sticky_roots = {
            os.path.normcase(os.path.abspath(Path("/tmp").resolve(strict=True))),
            os.path.normcase(os.path.abspath(Path("/var/tmp").resolve(strict=True))),
        }
        allowed_owner = int(metadata.st_uid) in {0, int(effective_uid)} or (
            int(metadata.st_uid) == 65_534 and normalized in canonical_sticky_roots
        )
        if not allowed_owner or not mode & stat.S_ISVTX or not mode & 0o002:
            return None
    try:
        if (
            os.path.commonpath((audit_lab, normalized)) == audit_lab
            or os.path.commonpath((trusted, normalized)) == trusted
        ):
            return None
    except ValueError:
        pass
    return resolved


def _deep_coverage_runtime_parent(*, root: Path, audit_lab_root: Path) -> Path:
    """Select a private runtime parent, falling back to a sticky OS temp root."""

    trusted = os.path.normcase(os.path.abspath(root.resolve(strict=True)))
    audit_lab = os.path.normcase(os.path.abspath(audit_lab_root.resolve(strict=True)))
    private_candidates: list[Path] = []
    for name in ("RUNTIME_DIRECTORY", "XDG_RUNTIME_DIR", "RUNNER_TEMP"):
        value = os.environ.get(name)
        if value:
            private_candidates.extend(Path(item) for item in value.split(os.pathsep) if item)
    for candidate in private_candidates:
        selected = _runtime_parent_candidate(
            candidate,
            trusted=trusted,
            audit_lab=audit_lab,
            require_private=True,
        )
        if selected is not None:
            return selected

    fallback_candidates = (Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp"))
    for candidate in fallback_candidates:
        private = _runtime_parent_candidate(
            candidate,
            trusted=trusted,
            audit_lab=audit_lab,
            require_private=True,
        )
        sticky = private or _runtime_parent_candidate(
            candidate,
            trusted=trusted,
            audit_lab=audit_lab,
            require_private=False,
        )
        if sticky is not None:
            return sticky
    raise ValueError("trusted-deep has no safe ephemeral runtime parent")


def _durable_provider_scratch(
    staging_parent: Path,
    *,
    namespace: str,
    identity: str,
    label: str,
) -> Path:
    """Create one versioned private namespace without trusting legacy scratch."""

    namespace_root = _owned_plain_directory(
        staging_parent / namespace,
        allow_existing=True,
        label=f"{label} namespace",
        require_private=True,
    )
    return _owned_plain_directory(
        namespace_root / identity,
        allow_existing=True,
        label=f"{label} scratch",
        require_private=True,
    )


def _runtime_directory_identity(path: Path) -> tuple[int, int]:
    metadata = os.lstat(path)
    resolved = path.resolve(strict=True)
    canonical_metadata = os.stat(resolved, follow_symlinks=False)
    mode = stat.S_IMODE(metadata.st_mode)
    effective_uid = getattr(os, "geteuid", lambda: int(metadata.st_uid))()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or resolved != Path(os.path.abspath(path))
        or not stat.S_ISDIR(canonical_metadata.st_mode)
        or (int(metadata.st_dev), int(metadata.st_ino))
        != (int(canonical_metadata.st_dev), int(canonical_metadata.st_ino))
        or int(metadata.st_uid) != int(effective_uid)
        or mode & 0o077
        or (mode & 0o700) != 0o700
    ):
        raise RuntimeError("trusted-deep runtime directory is not private and identity-bound")
    return int(metadata.st_dev), int(metadata.st_ino)


def _cleanup_deep_coverage_runtime(path: Path, identity: tuple[int, int]) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise RuntimeError("trusted-deep runtime disappeared before cleanup") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    effective_uid = getattr(os, "geteuid", lambda: int(metadata.st_uid))()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or (int(metadata.st_dev), int(metadata.st_ino)) != identity
        or path.resolve(strict=True) != Path(os.path.abspath(path))
        or int(metadata.st_uid) != int(effective_uid)
        or mode & 0o077
        or (mode & 0o700) != 0o700
    ):
        raise RuntimeError("trusted-deep runtime identity changed before cleanup")
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise RuntimeError("trusted-deep runtime cleanup lacks symlink-safe traversal")
    shutil.rmtree(path)
    if os.path.lexists(path):
        raise RuntimeError("trusted-deep runtime cleanup was incomplete")


@contextmanager
def _deep_coverage_runtime(*, root: Path, audit_lab_root: Path) -> Iterator[Path]:
    """Own one unpredictable private runtime without masking execution failures."""

    parent = _deep_coverage_runtime_parent(root=root, audit_lab_root=audit_lab_root)
    runtime = Path(tempfile.mkdtemp(prefix="neocortex-pytest-coverage-", dir=parent))
    identity = _runtime_directory_identity(runtime)
    try:
        yield runtime
    except BaseException as primary:
        try:
            _cleanup_deep_coverage_runtime(runtime, identity)
        except BaseException as cleanup:
            primary.add_note(
                "trusted-deep runtime cleanup also failed: "
                f"{type(cleanup).__name__}: {str(cleanup)[:512]}"
            )
        raise
    else:
        try:
            _cleanup_deep_coverage_runtime(runtime, identity)
        except Exception as cleanup:
            raise RuntimeError("trusted-deep runtime cleanup failed") from cleanup


def _git_tool_probe() -> tuple[Path | None, str | None]:
    """Resolve and identify Git without reading repository state."""

    executable = shutil.which("git")
    if executable is None:
        return None, None
    try:
        completed = run_bounded_capture(
            (executable, "--version"),
            timeout_seconds=10.0,
            stdout_limit_bytes=4_096,
            stderr_limit_bytes=4_096,
            environment=_controlled_environment(),
            memory_limit_bytes=_GIT_HISTORY_MEMORY_BYTES if os.name == "nt" else None,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired, SubprocessOutputLimitError):
        return Path(executable), None
    if completed.returncode != 0 or completed.stderr:
        return Path(executable), None
    try:
        output = completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        return Path(executable), None
    prefix = "git version "
    version = output.removeprefix(prefix) if output.startswith(prefix) else ""
    if not version or any(character.isspace() for character in version):
        return Path(executable), None
    if len(version.encode("utf-8")) > 256:
        return Path(executable), None
    return Path(executable), version


def _validated_deep_configuration(
    payload: Mapping[str, object] | None,
    signature: str | None,
) -> tuple[dict[str, object], DeepCoverageConfig]:
    if payload is None or signature is None:
        raise ValueError("trusted-deep requires its exact configuration and signature")
    selectors_value = payload.get("test_selectors")
    max_tests = payload.get("max_tests")
    time_budget_seconds = payload.get("time_budget_seconds")
    shard_size = payload.get("shard_size")
    if (
        not isinstance(selectors_value, list)
        or any(not isinstance(item, str) for item in selectors_value)
        or isinstance(max_tests, bool)
        or not isinstance(max_tests, int)
        or isinstance(time_budget_seconds, bool)
        or not isinstance(time_budget_seconds, int)
        or isinstance(shard_size, bool)
        or not isinstance(shard_size, int)
    ):
        raise ValueError("trusted-deep configuration types are invalid")
    selectors = tuple(item for item in selectors_value if isinstance(item, str))
    schema = payload.get("schema")
    if schema == DEEP_CONFIGURATION_SCHEMA:
        mutation_target = payload.get("mutation_target")
        mutation_symbol = payload.get("mutation_symbol")
        mutation_max_mutants = payload.get("mutation_max_mutants")
        mutation_timeout_seconds = payload.get("mutation_timeout_seconds")
        mutation_time_budget_seconds = payload.get("mutation_time_budget_seconds")
        if (
            (mutation_target is not None and not isinstance(mutation_target, str))
            or (mutation_symbol is not None and not isinstance(mutation_symbol, str))
            or isinstance(mutation_max_mutants, bool)
            or not isinstance(mutation_max_mutants, int)
            or isinstance(mutation_timeout_seconds, bool)
            or not isinstance(mutation_timeout_seconds, int)
            or isinstance(mutation_time_budget_seconds, bool)
            or not isinstance(mutation_time_budget_seconds, int)
        ):
            raise ValueError("trusted-deep mutation configuration types are invalid")
        current = build_deep_configuration_payload(
            analysis_profile=str(payload.get("analysis_profile")),
            test_selectors=selectors,
            max_tests=max_tests,
            time_budget_seconds=time_budget_seconds,
            shard_size=shard_size,
            mutation_target=mutation_target,
            mutation_symbol=mutation_symbol,
            mutation_max_mutants=mutation_max_mutants,
            mutation_timeout_seconds=mutation_timeout_seconds,
            mutation_time_budget_seconds=mutation_time_budget_seconds,
        )
    elif schema != LEGACY_DEEP_CONFIGURATION_SCHEMA:
        raise ValueError("trusted-deep configuration schema is unsupported")
    else:
        current = build_deep_configuration_payload(
            analysis_profile=str(payload.get("analysis_profile")),
            test_selectors=selectors,
            max_tests=max_tests,
            time_budget_seconds=time_budget_seconds,
            shard_size=shard_size,
        )
    normalized = current
    if schema == LEGACY_DEEP_CONFIGURATION_SCHEMA:
        normalized = {
            name: value
            for name, value in current.items()
            if name
            in {
                "analysis_profile",
                "content_executed",
                "suite_selection",
                "test_selectors",
                "max_tests",
                "time_budget_seconds",
                "shard_size",
            }
        }
        normalized["schema"] = LEGACY_DEEP_CONFIGURATION_SCHEMA
    if normalized != dict(payload) or normalized["analysis_profile"] != "trusted-deep":
        raise ValueError("trusted-deep configuration is not canonical")
    observed_signature = calculate_deep_configuration_signature(normalized)
    if observed_signature != signature:
        raise ValueError("trusted-deep configuration signature disagrees")
    return normalized, DeepCoverageConfig(
        selectors,
        max_tests,
        float(time_budget_seconds),
        shard_size,
        signature,
    )


def _read_exact_config(path: Path) -> bytes:
    metadata = os.lstat(path)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    if path.is_symlink() or attributes & reparse or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("trusted project configuration is not a regular file")
    if metadata.st_size > _CONFIG_LIMIT_BYTES:
        raise ValueError("trusted project configuration exceeds its bound")
    raw = path.read_bytes()
    after = os.lstat(path)
    if (
        len(raw) != metadata.st_size
        or after.st_size != metadata.st_size
        or after.st_mtime_ns != metadata.st_mtime_ns
    ):
        raise ValueError("trusted project configuration changed during read")
    return raw


def _pip_audit_source_input_signature(root: Path) -> tuple[str, int]:
    """Bind a network snapshot to the exact local supply/release contract."""

    rows: list[dict[str, object]] = []
    total_bytes = 0
    for relative_path in _PIP_AUDIT_SOURCE_INPUTS:
        path = root / relative_path
        if not path.exists():
            rows.append({"path": relative_path, "present": False})
            continue
        raw = _read_exact_config(path)
        total_bytes += len(raw)
        rows.append(
            {
                "path": relative_path,
                "present": True,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return external_signature("pip-audit-source-input-v1", {"files": rows}), total_bytes


def _project_configuration(root: Path) -> tuple[bytes, Mapping[str, object], str]:
    path = root / "pyproject.toml"
    raw = _read_exact_config(path)
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("trusted pyproject.toml is malformed") from exc
    if not isinstance(parsed, dict):
        raise ValueError("trusted pyproject.toml must be a table")
    digest = "pyproject-v1:xxh3_128:" + fingerprint_bytes(raw).xxh3_128
    return raw, parsed, digest


def _environment_signature(
    *,
    tool_name: str,
    tool_version: str,
    node_path: str | None = None,
    home_directory: str | None = None,
    path_value: str | None = None,
    pathext_value: str | None = None,
) -> str:
    # A release install changes ``sys.executable`` from the candidate-wheel
    # staging path to the final immutable release path without changing the
    # interpreter ABI or any provider semantics.  Binding the signature to
    # that physical path made every freshly published provider stale as soon
    # as the verified wheel was promoted.  Keep the semantic runtime identity
    # here; providers whose executable discovery is behaviorally relevant
    # continue to pass PATH/node/home explicitly below.
    payload: dict[str, object] = {
        "python_version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "implementation_cache_tag": getattr(sys.implementation, "cache_tag", None),
        "abi_flags": getattr(sys, "abiflags", ""),
        "platform": platform.platform(),
        "tool_name": tool_name,
        "tool_version": tool_version,
        "node_path": (None if node_path is None else _normalized_runtime_path_entry(node_path)),
    }
    if home_directory is not None:
        payload["home_directory"] = os.path.normcase(os.path.abspath(home_directory))
    if path_value is not None:
        payload["path"] = _normalized_runtime_search_path(path_value)
    if pathext_value is not None:
        payload["pathext"] = pathext_value
    return external_signature("external-environment-v2", payload)


def _normalized_runtime_path_entry(value: str) -> str:
    """Replace only the active immutable runtime prefix with a stable token."""

    observed = os.path.normcase(os.path.abspath(value))
    runtime = os.path.normcase(os.path.abspath(sys.prefix))
    try:
        within_runtime = os.path.commonpath((observed, runtime)) == runtime
    except ValueError:
        within_runtime = False
    if not within_runtime:
        return observed
    relative = os.path.relpath(observed, runtime).replace(os.sep, "/")
    return "$NEOCORTEX_RUNTIME" if relative == "." else f"$NEOCORTEX_RUNTIME/{relative}"


def _normalized_runtime_search_path(value: str) -> str:
    return os.pathsep.join(
        _normalized_runtime_path_entry(item) for item in value.split(os.pathsep) if item
    )


def _python_provider_files(
    files: Sequence[ExternalEvidenceFile],
    *,
    suffixes: frozenset[str],
) -> tuple[ExternalEvidenceFile, ...]:
    """Return one deterministic exact subset for a provider's real language domain."""

    return tuple(
        sorted(
            (
                item
                for item in files
                if PurePosixPath(item.relative_path).suffix.casefold() in suffixes
            ),
            key=lambda item: item.relative_path.casefold(),
        )
    )


def _installed_distribution_signature(*, utc_date: str | None = None) -> str:
    """Fingerprint installed names/versions without importing package content."""

    rows: list[tuple[str, str]] = []
    names: set[str] = set()
    for distribution in installed_environment_distributions():
        if len(rows) >= 2_000:
            raise ValueError("installed distribution count exceeds its bound")
        name = distribution.metadata.get("Name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("installed distribution name is unavailable")
        version = distribution.version
        if not isinstance(version, str) or not version.strip():
            raise ValueError("installed distribution version is unavailable")
        normalized_name = canonicalize_name(name.strip())
        if normalized_name in names:
            raise ValueError("installed distribution identity is duplicated")
        names.add(normalized_name)
        rows.append((normalized_name, version.strip()))
    rows.sort()
    payload: dict[str, object] = {"distributions": rows}
    if utc_date is not None:
        payload["utc_date"] = utc_date
    return external_signature("installed-python-environment-v1", payload)


def _supply_environment_signature(
    *,
    tool_name: str,
    tool_version: str,
    installed_signature: str | None = None,
    utc_date: str | None = None,
) -> str:
    return external_signature(
        "external-supply-environment-v1",
        {
            "runtime": _environment_signature(
                tool_name=tool_name,
                tool_version=tool_version,
            ),
            "installed_signature": installed_signature,
            "utc_date": utc_date,
        },
    )


def _attach_supply_execution(
    publication: ExternalProviderPublication,
    *,
    counters: Mapping[str, int],
    details: Mapping[str, object],
) -> ExternalProviderPublication:
    merged_counters = dict(publication.counters)
    merged_counters.update({name: int(value) for name, value in counters.items()})
    provenance = dict(publication.publication.provenance)
    provenance["supply_chain_execution"] = dict(details)
    inner = replace(publication.publication, provenance=provenance)
    return replace(publication, publication=inner, counters=merged_counters)


def _limits(*, memory: int) -> ProviderLimits:
    return ProviderLimits(
        _TOOL_TIMEOUT_SECONDS,
        memory,
        RUFF_MAX_TOTAL_BYTES,
        _STDOUT_LIMIT_BYTES,
        RUFF_MAX_DIAGNOSTICS,
    )


def _provider_descriptor(
    *,
    provider_id: str,
    provider_schema: str,
    tool_name: str,
    tool_version: str,
    profile: AnalysisProfile,
    source: str,
    configuration_payload: Mapping[str, object],
    project_configuration_digest: str | None,
    environment_signature: str,
    root_identity: str,
    execution_strategy: str,
    invalidation_strategy: InvalidationStrategy,
    memory: int,
    loads_project_configuration: bool,
    scope: str = "current-inventory-python",
    limits: ProviderLimits | None = None,
    loads_plugins: bool = False,
    imports_content: bool = False,
    executes_content: bool = False,
    uses_network: bool = False,
) -> ProviderDescriptor:
    trust_requirement: ProviderTrust
    if profile == "protected":
        trust_requirement = "untrusted-safe"
    elif profile == "trusted-static":
        trust_requirement = "trusted-static"
    else:
        trust_requirement = "trusted-execution"
    configuration_signature = external_signature(
        f"{provider_id}-configuration-v1", configuration_payload
    )
    comparability_signature = external_signature(
        f"{provider_id}-comparable-v1",
        {
            "provider_id": provider_id,
            "provider_schema": provider_schema,
            "tool_name": tool_name,
            "tool_version": tool_version,
            "profile": profile,
            "configuration_signature": configuration_signature,
            "project_configuration_digest": project_configuration_digest,
            "environment_signature": environment_signature,
            "root_identity": root_identity,
            "scope": scope,
            "execution_strategy": execution_strategy,
        },
    )
    return ProviderDescriptor(
        provider_id,
        provider_schema,
        tool_name,
        profile,
        trust_requirement,
        scope,
        source,
        configuration_signature,
        project_configuration_digest,
        environment_signature,
        comparability_signature,
        execution_strategy,
        invalidation_strategy,
        "exact-publication-replay-v1",
        _limits(memory=memory) if limits is None else limits,
        loads_project_configuration=loads_project_configuration,
        loads_plugins=loads_plugins,
        imports_content=imports_content,
        executes_content=executes_content,
        uses_network=uses_network,
    )


def _input_records(
    files: Sequence[ExternalEvidenceFile],
    *,
    covered: bool,
    reason: str | None = None,
) -> tuple[ExternalRunInput, ...]:
    return tuple(
        ExternalRunInput.from_file(item, covered=covered, coverage_reason=reason) for item in files
    )


def _portable_publication_id(
    descriptor: ProviderDescriptor,
    *,
    input_signature: str,
    result_digest: str | None,
) -> str:
    return external_signature(
        "external-publication-v1",
        {
            "provider_id": descriptor.provider_id,
            "provider_schema": descriptor.provider_schema,
            "profile": descriptor.profile,
            "configuration_signature": descriptor.configuration_signature,
            "environment_signature": descriptor.environment_signature,
            "input_signature": input_signature,
            "result_digest": result_digest,
        },
    )


def _result_counters(
    files: Sequence[ExternalEvidenceFile],
    findings: Sequence[ExternalProviderFinding],
    metrics: Sequence[ExternalProviderMetric],
    relations: Sequence[ExternalProviderRelation],
    baseline: ExternalProviderBaseline | None,
    *,
    wall_milliseconds: int,
    stdout_bytes: int,
    stderr_bytes: int,
    process_invocations: int,
    bytes_read: int,
    bytes_staged: int,
) -> dict[str, int]:
    current_ids = {item.portable_finding_id for item in findings}
    baseline_ids = set(() if baseline is None else baseline.portable_finding_ids)
    current_metric_ids = {item.portable_metric_id for item in metrics}
    baseline_metric_ids = set(() if baseline is None else baseline.portable_metric_ids)
    current_relation_ids = {item.portable_relation_id for item in relations}
    baseline_relation_ids = set(() if baseline is None else baseline.portable_relation_ids)
    counters = {
        "eligible_files": len(files),
        "covered_files": len(files),
        "files_verified": len(files),
        "bytes_verified": sum(item.size for item in files),
        "bytes_read": bytes_read,
        "bytes_staged": bytes_staged,
        "process_invocations": process_invocations,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "wall_milliseconds": max(0, wall_milliseconds),
        "findings": len(findings),
        "metrics": len(metrics),
        "relations": len(relations),
        "comparable": int(baseline is not None),
        "cache_hits": 0,
        "cache_misses": 1,
        "errors": 0,
        "timeouts": 0,
        "skipped": 0,
        "unavailable": 0,
    }
    if baseline is not None:
        counters["added"] = len(current_ids - baseline_ids)
        counters["resolved"] = len(baseline_ids - current_ids)
        counters["metrics_added"] = len(current_metric_ids - baseline_metric_ids)
        counters["metrics_resolved"] = len(baseline_metric_ids - current_metric_ids)
        counters["relations_added"] = len(current_relation_ids - baseline_relation_ids)
        counters["relations_resolved"] = len(baseline_relation_ids - current_relation_ids)
    return counters


def _provenance(
    descriptor: ProviderDescriptor,
    root: Path,
    input_signature: str,
    *,
    execution: str,
    result_digest: str | None,
    findings: int,
    metrics: int = 0,
    relations: int = 0,
    reason: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "neocortex.external-code-evidence/v2",
        "provider": descriptor.as_payload(),
        "root": str(root),
        "execution": execution,
        "input": {"signature": input_signature},
        "result": None
        if result_digest is None
        else {
            "digest": result_digest,
            "findings": findings,
            "metrics": metrics,
            "relations": relations,
        },
        "authority": "advisory",
        "mutation_authority": False,
        "content_executed": descriptor.executes_content,
    }
    if reason is not None:
        payload["error"] = {"reason": reason}
    return payload


def _failure(
    descriptor: ProviderDescriptor,
    root: Path,
    files: Sequence[ExternalEvidenceFile],
    *,
    tool_version: str,
    status: ExternalRunStatus,
    reason: str,
    started_ns: int,
    process_invocations: int | None = None,
) -> ExternalProviderPublication:
    input_signature = external_input_signature(files)
    execution = "unavailable" if status == "unavailable" else "attempted"
    inner = ExternalEvidencePublication(
        descriptor.tool_name,
        tool_version,
        descriptor.configuration_signature,
        status,
        started_ns,
        time.time_ns(),
        _provenance(
            descriptor,
            root,
            input_signature,
            execution=execution,
            result_digest=None,
            findings=0,
            reason=reason,
        ),
    )
    counters = {
        "eligible_files": len(files),
        "covered_files": 0,
        "files_verified": 0,
        "bytes_verified": 0,
        "bytes_read": 0,
        "bytes_staged": 0,
        "process_invocations": (
            int(status != "unavailable") if process_invocations is None else process_invocations
        ),
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "wall_milliseconds": max(0, (time.time_ns() - started_ns) // 1_000_000),
        "findings": 0,
        "metrics": 0,
        "relations": 0,
        "comparable": 0,
        "cache_hits": 0,
        "cache_misses": 1,
        "errors": int(status == "failed"),
        "timeouts": int(status == "timeout"),
        "skipped": 0,
        "unavailable": int(status == "unavailable"),
    }
    return ExternalProviderPublication(
        descriptor,
        inner,
        str(root),
        external_root_identity(root),
        input_signature,
        _input_records(files, covered=False, reason=reason),
        (),
        counters,
        False,
        None,
        _portable_publication_id(descriptor, input_signature=input_signature, result_digest=None),
        limitations=(reason,),
    )


def _abstention(
    descriptor: ProviderDescriptor,
    root: Path,
    files: Sequence[ExternalEvidenceFile],
    *,
    tool_version: str,
    reason: str,
    started_ns: int,
    input_signature_override: str | None = None,
    process_invocations: int = 0,
    stdout_bytes: int = 0,
    stderr_bytes: int = 0,
) -> ExternalProviderPublication:
    """Publish one terminal advisory abstention without pretending coverage."""

    validate_external_inputs(files)
    input_signature = (
        external_input_signature(files)
        if input_signature_override is None
        else input_signature_override
    )
    result_digest = external_provider_result_digest((), (), ())
    completed_ns = time.time_ns()
    inner = ExternalEvidencePublication(
        descriptor.tool_name,
        tool_version,
        descriptor.configuration_signature,
        "skipped",
        started_ns,
        completed_ns,
        _provenance(
            descriptor,
            root,
            input_signature,
            execution="skipped",
            result_digest=result_digest,
            findings=0,
            reason=reason,
        ),
    )
    counters = {
        "eligible_files": len(files),
        "covered_files": 0,
        "files_verified": 0,
        "bytes_verified": 0,
        "bytes_read": 0,
        "bytes_staged": 0,
        "process_invocations": process_invocations,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "wall_milliseconds": max(0, (completed_ns - started_ns) // 1_000_000),
        "findings": 0,
        "metrics": 0,
        "relations": 0,
        "comparable": 0,
        "cache_hits": 0,
        "cache_misses": 1,
        "errors": 0,
        "timeouts": 0,
        "skipped": 1,
        "unavailable": 0,
    }
    return ExternalProviderPublication(
        descriptor,
        inner,
        str(root),
        external_root_identity(root),
        input_signature,
        _input_records(files, covered=False, reason=reason),
        (),
        counters,
        False,
        result_digest,
        _portable_publication_id(
            descriptor,
            input_signature=input_signature,
            result_digest=result_digest,
        ),
        limitations=(reason,),
    )


def _exact_replay(
    descriptor: ProviderDescriptor,
    root: Path,
    files: Sequence[ExternalEvidenceFile],
    baseline: ExternalProviderBaseline,
    *,
    limitations: Sequence[str] = (),
    input_signature_override: str | None = None,
) -> ExternalProviderPublication:
    started_ns = time.time_ns()
    validate_external_inputs(files)
    input_signature = (
        external_input_signature(files)
        if input_signature_override is None
        else input_signature_override
    )
    if not input_signature or len(input_signature.encode("utf-8")) > 512:
        raise ValueError("external provider input signature is invalid")
    if baseline.reuse_mode != "exact_replay":
        raise ValueError("external provider baseline is comparison-only")
    if baseline.input_signature != input_signature:
        raise ValueError("external provider replay input signature is not exact")
    counters = {
        "eligible_files": len(files),
        "covered_files": len(files),
        "files_verified": len(files),
        "bytes_verified": sum(item.size for item in files),
        "bytes_read": sum(item.size for item in files),
        "bytes_staged": 0,
        "process_invocations": 0,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "wall_milliseconds": max(0, (time.time_ns() - started_ns) // 1_000_000),
        "findings": len(baseline.portable_finding_ids),
        "metrics": len(baseline.portable_metric_ids),
        "relations": len(baseline.portable_relation_ids),
        "comparable": 1,
        "added": 0,
        "resolved": 0,
        "metrics_added": 0,
        "metrics_resolved": 0,
        "relations_added": 0,
        "relations_resolved": 0,
        "cache_hits": 1,
        "cache_misses": 0,
        "errors": 0,
        "timeouts": 0,
        "skipped": 1,
        "unavailable": 0,
    }
    inner = ExternalEvidencePublication(
        descriptor.tool_name,
        baseline.tool_version,
        descriptor.configuration_signature,
        "skipped",
        started_ns,
        time.time_ns(),
        _provenance(
            descriptor,
            root,
            input_signature,
            execution="cache_replay",
            result_digest=baseline.result_digest,
            findings=len(baseline.portable_finding_ids),
            metrics=len(baseline.portable_metric_ids),
            relations=len(baseline.portable_relation_ids),
        ),
    )
    verification = external_signature(
        "external-replay-verification-v1",
        {
            "provider_id": descriptor.provider_id,
            "source_tool_run_id": baseline.tool_run_id,
            "input_signature": input_signature,
            "files_verified": len(files),
            "bytes_verified": sum(item.size for item in files),
        },
    )
    return ExternalProviderPublication(
        descriptor,
        inner,
        str(root),
        external_root_identity(root),
        input_signature,
        _input_records(files, covered=True),
        (),
        counters,
        True,
        baseline.result_digest,
        _portable_publication_id(
            descriptor,
            input_signature=input_signature,
            result_digest=baseline.result_digest,
        ),
        baseline.tool_run_id,
        verification,
        tuple(limitations),
    )


def _baseline_allows_exact_replay(
    baseline: ExternalProviderBaseline | None,
    input_signature: str,
) -> bool:
    """Keep portable comparison evidence separate from physical replay authority."""

    return (
        baseline is not None
        and baseline.reuse_mode == "exact_replay"
        and baseline.input_signature == input_signature
    )


def _success(
    descriptor: ProviderDescriptor,
    root: Path,
    files: Sequence[ExternalEvidenceFile],
    findings: Sequence[ExternalProviderFinding],
    baseline: ExternalProviderBaseline | None,
    *,
    metrics: Sequence[ExternalProviderMetric] = (),
    relations: Sequence[ExternalProviderRelation] = (),
    tool_version: str,
    started_ns: int,
    stdout_bytes: int,
    stderr_bytes: int,
    process_invocations: int,
    bytes_staged: int,
    limitations: Sequence[str] = (),
    input_signature_override: str | None = None,
) -> ExternalProviderPublication:
    ordered_findings = tuple(sorted(findings, key=lambda item: item.portable_finding_id))
    if len({item.portable_finding_id for item in ordered_findings}) != len(ordered_findings):
        raise ValueError("external provider produced duplicate finding identities")
    ordered_metrics = tuple(sorted(metrics, key=lambda item: item.portable_metric_id))
    if len({item.portable_metric_id for item in ordered_metrics}) != len(ordered_metrics):
        raise ValueError("external provider produced duplicate metric identities")
    ordered_relations = tuple(sorted(relations, key=lambda item: item.portable_relation_id))
    if len({item.portable_relation_id for item in ordered_relations}) != len(ordered_relations):
        raise ValueError("external provider produced duplicate relation identities")
    validate_external_inputs(files)
    result_digest = external_provider_result_digest(
        ordered_findings,
        ordered_metrics,
        ordered_relations,
    )
    input_signature = (
        external_input_signature(files)
        if input_signature_override is None
        else input_signature_override
    )
    if not input_signature or len(input_signature.encode("utf-8")) > 512:
        raise ValueError("external provider input signature is invalid")
    counters = _result_counters(
        files,
        ordered_findings,
        ordered_metrics,
        ordered_relations,
        baseline,
        wall_milliseconds=(time.time_ns() - started_ns) // 1_000_000,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        process_invocations=process_invocations,
        bytes_read=sum(item.size for item in files),
        bytes_staged=bytes_staged,
    )
    inner = ExternalEvidencePublication(
        descriptor.tool_name,
        tool_version,
        descriptor.configuration_signature,
        "completed",
        started_ns,
        time.time_ns(),
        _provenance(
            descriptor,
            root,
            input_signature,
            execution="full",
            result_digest=result_digest,
            findings=len(ordered_findings),
            metrics=len(ordered_metrics),
            relations=len(ordered_relations),
        ),
    )
    return ExternalProviderPublication(
        descriptor,
        inner,
        str(root),
        external_root_identity(root),
        input_signature,
        _input_records(files, covered=True),
        ordered_findings,
        counters,
        True,
        result_digest,
        _portable_publication_id(
            descriptor,
            input_signature=input_signature,
            result_digest=result_digest,
        ),
        limitations=tuple(limitations),
        metrics=ordered_metrics,
        relations=ordered_relations,
    )


def _finding(
    *,
    provider_id: str,
    owner: ExternalEvidenceFile,
    category: str,
    code: str,
    severity: str,
    message: str,
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
    metadata: Mapping[str, object] | None = None,
    url: str | None = None,
    fix_available: bool = False,
) -> ExternalProviderFinding:
    portable_message = normalize_external_finding_message(provider_id, message)
    identity = external_finding_identity(
        provider_id,
        relative_path=owner.relative_path,
        category=category,
        code=code,
        message=portable_message,
        start_line=start_line,
        start_column=start_column,
        end_line=end_line,
        end_column=end_column,
    )
    return ExternalProviderFinding(
        identity,
        owner.version_id,
        owner.relative_path,
        category,
        code,
        severity,
        portable_message,
        True,
        1.0,
        None,
        "advisory",
        start_line,
        start_column,
        end_line,
        end_column,
        url,
        fix_available,
        {} if metadata is None else metadata,
    )


def _normalized_severity(value: object) -> str:
    observed = str(value or "info").casefold()
    if observed == "error":
        return "error"
    if observed in {"warning", "warn"}:
        return "warning"
    return "info"


class RuffProtectedBasicProvider:
    """Compatibility-backed protected Ruff policy with normalized ownership."""

    def __init__(self, root: Path):
        version = _package_version("ruff") or "unavailable"
        environment = _environment_signature(tool_name="ruff", tool_version=version)
        self.descriptor = _provider_descriptor(
            provider_id=RUFF_PROTECTED_PROVIDER_ID,
            provider_schema="neocortex.ruff-protected-basic/v1",
            tool_name="ruff",
            tool_version=version,
            profile="protected",
            source="external:ruff-protected-basic",
            configuration_payload={
                "legacy_configuration_signature": RUFF_CONFIGURATION_SIGNATURE,
                "rules": "E4,E7,E9,F",
                "isolated": True,
                "target": "py313",
                "fixes": False,
            },
            project_configuration_digest=None,
            environment_signature=environment,
            root_identity=external_root_identity(root),
            execution_strategy="verified-staged-batches-v1",
            invalidation_strategy="project_wide",
            memory=_RUFF_MEMORY_BYTES,
            loads_project_configuration=False,
        )
        self._version = None if version == "unavailable" else version

    def tool_version(self) -> str | None:
        return self._version

    def normalize_legacy(
        self,
        root: Path,
        files: Sequence[ExternalEvidenceFile],
        publication: ExternalEvidencePublication,
        *,
        baseline: ExternalProviderBaseline | None,
    ) -> ExternalProviderPublication:
        """Normalize the already-executed legacy Ruff result without rerunning it."""

        if publication.execution == "cache_replay":
            if not _baseline_allows_exact_replay(
                baseline,
                external_input_signature(files),
            ):
                raise ValueError("normalized Ruff replay has no exact normalized source")
            assert baseline is not None
            return _exact_replay(self.descriptor, root, files, baseline)
        if publication.status != "completed":
            reason = "ruff_protected_failed"
            error = publication.provenance.get("error")
            if isinstance(error, Mapping) and isinstance(error.get("reason"), str):
                reason = str(error["reason"])
            return _failure(
                self.descriptor,
                root,
                files,
                tool_version=publication.tool_version,
                status=publication.status,
                reason=reason,
                started_ns=publication.started_ns,
            )
        findings = tuple(
            ExternalProviderFinding.from_diagnostic(item, category="correctness")
            for item in publication.diagnostics
        )
        return _success(
            self.descriptor,
            root,
            files,
            findings,
            baseline,
            tool_version=publication.tool_version,
            started_ns=publication.started_ns,
            stdout_bytes=0,
            stderr_bytes=0,
            process_invocations=max(1, (len(files) + 49) // 50),
            bytes_staged=sum(item.size for item in files),
        )

    def run(
        self,
        root: Path,
        files: Sequence[ExternalEvidenceFile],
        *,
        baseline: ExternalProviderBaseline | None,
        scratch_root: Path,
    ) -> ExternalProviderPublication:
        if _baseline_allows_exact_replay(baseline, external_input_signature(files)):
            assert baseline is not None
            return _exact_replay(self.descriptor, root, files, baseline)
        started_ns = time.time_ns()
        if self._version is None:
            return _failure(
                self.descriptor,
                root,
                files,
                tool_version="unavailable",
                status="failed",
                reason="ruff_distribution_missing",
                started_ns=started_ns,
            )
        legacy = RuffEvidenceProvider().run(
            root,
            files,
            baseline=None,
            scratch_root=scratch_root,
        )
        if legacy.status != "completed":
            reason = "ruff_protected_failed"
            error = legacy.provenance.get("error")
            if isinstance(error, Mapping) and isinstance(error.get("reason"), str):
                reason = str(error["reason"])
            return _failure(
                self.descriptor,
                root,
                files,
                tool_version=self._version,
                status=legacy.status,
                reason=reason,
                started_ns=started_ns,
            )
        findings = tuple(
            ExternalProviderFinding.from_diagnostic(item, category="correctness")
            for item in legacy.diagnostics
        )
        return _success(
            self.descriptor,
            root,
            files,
            findings,
            baseline,
            tool_version=self._version,
            started_ns=started_ns,
            stdout_bytes=0,
            stderr_bytes=0,
            process_invocations=max(1, (len(files) + 49) // 50),
            bytes_staged=sum(item.size for item in files),
        )


class _TrustedStaticProvider:
    provider_id: str
    provider_schema: str
    tool_name: str
    source: str
    memory_bound: int
    execution_strategy: str

    def __init__(self, root: Path):
        self.root = root
        self._config_error: str | None = None
        try:
            self._config_raw, self._config, config_digest = _project_configuration(root)
            self._validate_configuration(self._config)
        except (OSError, ValueError) as exc:
            self._config_raw = b""
            self._config = {}
            config_digest = None
            self._config_error = f"{type(exc).__name__}:{exc}"
        self._version = self._resolve_version()
        version = self._version or "unavailable"
        node_path = self._node_path_for_signature()
        environment = _environment_signature(
            tool_name=self.tool_name,
            tool_version=version,
            node_path=node_path,
        )
        configuration_payload = self._configuration_payload()
        self.descriptor = _provider_descriptor(
            provider_id=self.provider_id,
            provider_schema=self.provider_schema,
            tool_name=self.tool_name,
            tool_version=version,
            profile="trusted-static",
            source=self.source,
            configuration_payload=configuration_payload,
            project_configuration_digest=config_digest,
            environment_signature=environment,
            root_identity=external_root_identity(root),
            execution_strategy=self.execution_strategy,
            invalidation_strategy="project_wide",
            memory=self.memory_bound,
            loads_project_configuration=True,
        )

    def _resolve_version(self) -> str | None:
        raise NotImplementedError

    def _node_path_for_signature(self) -> str | None:
        return None

    def _validate_configuration(self, config: Mapping[str, object]) -> None:
        raise NotImplementedError

    def _configuration_payload(self) -> Mapping[str, object]:
        raise NotImplementedError

    def _execute(
        self,
        stage_root: Path,
        staged: Mapping[str, ExternalEvidenceFile],
        config_path: Path,
        environment: Mapping[str, str],
    ) -> tuple[tuple[ExternalProviderFinding, ...], int, int, int]:
        raise NotImplementedError

    def _limitations(self) -> tuple[str, ...]:
        return ()

    def tool_version(self) -> str | None:
        return self._version

    def run(
        self,
        root: Path,
        files: Sequence[ExternalEvidenceFile],
        *,
        baseline: ExternalProviderBaseline | None,
        scratch_root: Path,
    ) -> ExternalProviderPublication:
        if _baseline_allows_exact_replay(baseline, external_input_signature(files)):
            assert baseline is not None
            return _exact_replay(
                self.descriptor,
                root,
                files,
                baseline,
                limitations=self._limitations(),
            )
        started_ns = time.time_ns()
        if self._config_error is not None:
            return _failure(
                self.descriptor,
                root,
                files,
                tool_version=self._version or "unavailable",
                status="failed",
                reason=f"project_configuration_invalid:{self._config_error}"[:4096],
                started_ns=started_ns,
            )
        if self._version is None:
            return _failure(
                self.descriptor,
                root,
                files,
                tool_version="unavailable",
                status="unavailable",
                reason=f"{self.tool_name}_unavailable",
                started_ns=started_ns,
            )
        try:
            staging_parent = _validated_staging_parent(root, scratch_root)
            with tempfile.TemporaryDirectory(
                prefix=f"neocortex-{self.provider_id}-", dir=staging_parent
            ) as temporary:
                stage_root = Path(temporary)
                staged = _stage_external_inputs(files, stage_root / "source")
                config_path = stage_root / "pyproject.toml"
                config_path.write_bytes(self._config_raw)
                environment = _controlled_environment()
                for name in ("TEMP", "TMP", "TMPDIR"):
                    environment[name] = str(stage_root)
                findings, stdout_bytes, stderr_bytes, invocations = self._execute(
                    stage_root, staged, config_path, environment
                )
        except subprocess.TimeoutExpired:
            return _failure(
                self.descriptor,
                root,
                files,
                tool_version=self._version,
                status="timeout",
                reason="provider_timeout",
                started_ns=started_ns,
            )
        except (OSError, RuntimeError, TypeError, ValueError, SubprocessOutputLimitError) as exc:
            return _failure(
                self.descriptor,
                root,
                files,
                tool_version=self._version,
                status="failed",
                reason=f"provider_failure:{type(exc).__name__}:{exc}"[:4096],
                started_ns=started_ns,
            )
        return _success(
            self.descriptor,
            root,
            files,
            findings,
            baseline,
            tool_version=self._version,
            started_ns=started_ns,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            process_invocations=invocations,
            bytes_staged=sum(item.size for item in files) + len(self._config_raw),
            limitations=self._limitations(),
        )


class RuffTrustedProjectProvider(_TrustedStaticProvider):
    provider_id = RUFF_TRUSTED_PROVIDER_ID
    provider_schema = "neocortex.ruff-trusted-project/v1"
    tool_name = "ruff"
    source = "external:ruff-trusted-project"
    memory_bound = _RUFF_MEMORY_BYTES
    execution_strategy = "trusted-config-staged-batches-v1"

    def _resolve_version(self) -> str | None:
        return _package_version("ruff")

    def _validate_configuration(self, config: Mapping[str, object]) -> None:
        tool = config.get("tool")
        ruff = tool.get("ruff") if isinstance(tool, Mapping) else None
        if not isinstance(ruff, Mapping):
            raise ValueError("tool.ruff is missing")
        if ruff.get("extend") not in (None, ""):
            raise ValueError("ruff extend is not authorized in trusted-static")

    def _configuration_payload(self) -> Mapping[str, object]:
        return {
            "adapter": self.provider_schema,
            "configuration": "project-pyproject",
            "target": "py313",
            "fixes": False,
            "cache": False,
            "output": "json",
        }

    def _execute(
        self,
        stage_root: Path,
        staged: Mapping[str, ExternalEvidenceFile],
        config_path: Path,
        environment: Mapping[str, str],
    ) -> tuple[tuple[ExternalProviderFinding, ...], int, int, int]:
        paths = tuple(staged)
        outputs: list[bytes] = []
        stderr_bytes = 0
        stdout_bytes = 0
        invocations = 0
        for offset in range(0, len(paths), 50):
            batch = paths[offset : offset + 50]
            command = (
                sys.executable,
                "-I",
                "-m",
                "ruff",
                "check",
                "--config",
                str(config_path),
                "--output-format",
                "json",
                "--no-cache",
                "--no-fix",
                "--no-fix-only",
                "--no-unsafe-fixes",
                "--no-show-fixes",
                "--color",
                "never",
                *batch,
            )
            completed = run_bounded_capture(
                command,
                timeout_seconds=_TOOL_TIMEOUT_SECONDS,
                stdout_limit_bytes=max(0, _STDOUT_LIMIT_BYTES - stdout_bytes),
                stderr_limit_bytes=max(0, _STDERR_LIMIT_BYTES - stderr_bytes),
                cwd=stage_root,
                environment=environment,
                memory_limit_bytes=_RUFF_MEMORY_BYTES if os.name == "nt" else None,
            )
            invocations += 1
            if completed.returncode not in {0, 1}:
                raise ValueError(_unexpected_exit_message("ruff", completed))
            outputs.append(completed.stdout)
            stdout_bytes += len(completed.stdout)
            stderr_bytes += len(completed.stderr)
        diagnostics = tuple(
            item for output in outputs for item in _parse_diagnostics(output, staged)
        )
        findings = tuple(
            ExternalProviderFinding.from_diagnostic(item, category="correctness")
            for item in diagnostics
        )
        return findings, stdout_bytes, stderr_bytes, invocations


class MypyTrustedProjectProvider(_TrustedStaticProvider):
    provider_id = MYPY_PROVIDER_ID
    provider_schema = "neocortex.mypy-trusted-project/v1"
    tool_name = "mypy"
    source = "external:mypy"
    memory_bound = _MYPY_MEMORY_BYTES
    execution_strategy = "trusted-config-staged-project-v1"

    def _resolve_version(self) -> str | None:
        return _package_version("mypy")

    def _limitations(self) -> tuple[str, ...]:
        return ("unresolved_third_party_imports_are_treated_as_any",)

    def _validate_configuration(self, config: Mapping[str, object]) -> None:
        tool = config.get("tool")
        mypy = tool.get("mypy") if isinstance(tool, Mapping) else None
        if not isinstance(mypy, Mapping):
            raise ValueError("tool.mypy is missing")
        if mypy.get("plugins") not in (None, (), []):
            raise ValueError("mypy plugins are not authorized in trusted-static")
        if mypy.get("mypy_path") not in (None, "", (), []):
            raise ValueError("mypy_path is not authorized in trusted-static")

    def _configuration_payload(self) -> Mapping[str, object]:
        return {
            "adapter": self.provider_schema,
            "configuration": "project-pyproject",
            "python_version": "3.13",
            "platform": "win32" if os.name == "nt" else sys.platform,
            "output": "json-lines",
            "cache": "ephemeral-owned",
            "daemon": False,
            "ignore_missing_imports": True,
            "arguments": "relative-to-staged-source-root",
        }

    def _execute(
        self,
        stage_root: Path,
        staged: Mapping[str, ExternalEvidenceFile],
        config_path: Path,
        environment: Mapping[str, str],
    ) -> tuple[tuple[ExternalProviderFinding, ...], int, int, int]:
        argfile = stage_root / "mypy-arguments.txt"
        source_root = stage_root / "source"
        argfile.write_text(
            "\n".join(os.path.relpath(path, source_root) for path in staged) + "\n",
            encoding="utf-8",
        )
        cache_dir = stage_root / "mypy-cache"
        command = (
            sys.executable,
            "-I",
            "-m",
            "mypy",
            "-O",
            "json",
            "--config-file",
            str(config_path),
            "--python-version",
            "3.13",
            "--platform",
            "win32" if os.name == "nt" else sys.platform,
            "--python-executable",
            sys.executable,
            "--cache-dir",
            str(cache_dir),
            "--no-error-summary",
            "--no-pretty",
            "--no-color-output",
            "--ignore-missing-imports",
            f"@{argfile}",
        )
        completed = run_bounded_capture(
            command,
            timeout_seconds=_TOOL_TIMEOUT_SECONDS,
            stdout_limit_bytes=_STDOUT_LIMIT_BYTES,
            stderr_limit_bytes=_STDERR_LIMIT_BYTES,
            cwd=source_root,
            environment=environment,
            memory_limit_bytes=_MYPY_MEMORY_BYTES if os.name == "nt" else None,
        )
        if completed.returncode not in {0, 1}:
            raise ValueError(_unexpected_exit_message("mypy", completed))
        findings: list[ExternalProviderFinding] = []
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("mypy JSON Lines output is malformed") from exc
            if not isinstance(item, Mapping):
                raise ValueError("mypy diagnostic is not an object")
            filename = item.get("file")
            if not isinstance(filename, str):
                raise ValueError("mypy diagnostic path is missing")
            reported_path = Path(filename)
            if not reported_path.is_absolute():
                reported_path = stage_root / "source" / reported_path
            owner = staged.get(os.path.normcase(os.path.abspath(reported_path)))
            if owner is None:
                raise ValueError("mypy reported an unowned path")
            raw_line = int(item.get("line", -1))
            raw_column = int(item.get("column", -1))
            location_precision = "range"
            if raw_line < 1:
                line_number = end_line = 1
                column = end_column = 0
                location_precision = "file"
            else:
                line_number = raw_line
                if raw_column < 0:
                    column = 0
                    location_precision = "line"
                else:
                    column = raw_column
                raw_end_line = item.get("end_line")
                end_line = int(raw_end_line) if raw_end_line is not None else line_number
                end_line = max(line_number, end_line)
                raw_end_column = item.get("end_column")
                end_column = int(raw_end_column) if raw_end_column is not None else column
                if end_column < 0 or (end_line == line_number and end_column < column):
                    end_column = column
            message = str(item.get("message", ""))[:4096]
            code = str(item.get("code") or "mypy")[:128]
            severity = str(item.get("severity") or "error")
            findings.append(
                _finding(
                    provider_id=self.provider_id,
                    owner=owner,
                    category="typing",
                    code=code,
                    severity=_normalized_severity(severity),
                    message=message,
                    start_line=line_number,
                    start_column=column,
                    end_line=end_line,
                    end_column=end_column,
                    metadata={
                        "hint": item.get("hint"),
                        "reported_severity": severity,
                        "location_precision": location_precision,
                    },
                )
            )
        unique_findings: dict[str, ExternalProviderFinding] = {}
        observation_counts: dict[str, int] = {}
        for finding in findings:
            identity = finding.portable_finding_id
            existing = unique_findings.get(identity)
            if existing is not None and existing != finding:
                raise ValueError("mypy finding identity collision")
            if existing is None:
                unique_findings[identity] = finding
            observation_counts[identity] = observation_counts.get(identity, 0) + 1
        findings = [
            replace(
                finding,
                metadata={
                    **finding.metadata,
                    "duplicate_observations": observation_counts[finding.portable_finding_id],
                },
            )
            if observation_counts[finding.portable_finding_id] > 1
            else finding
            for finding in unique_findings.values()
        ]
        if len(findings) > RUFF_MAX_DIAGNOSTICS:
            raise ValueError("mypy diagnostics exceed their bound")
        return (
            tuple(findings),
            len(completed.stdout),
            len(completed.stderr),
            1,
        )


def _pyright_locations() -> tuple[Path | None, Path | None, str | None]:
    node = shutil.which("node")
    if node is None:
        return None, None, None
    candidates: list[Path] = []
    local_app = os.environ.get("LOCALAPPDATA")
    appdata = os.environ.get("APPDATA")
    if local_app:
        candidates.append(
            Path(local_app)
            / "Programs"
            / "Neocortex"
            / "tools"
            / "pyright"
            / "node_modules"
            / "pyright"
        )
    if appdata:
        candidates.append(Path(appdata) / "npm" / "node_modules" / "pyright")
    candidates.append(Path(sys.prefix) / "tools" / "pyright" / "node_modules" / "pyright")
    for package_root in candidates:
        index = package_root / "index.js"
        manifest = package_root / "package.json"
        if not index.is_file() or not manifest.is_file():
            continue
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        version = payload.get("version") if isinstance(payload, Mapping) else None
        if isinstance(version, str) and version:
            return Path(node), index, version
    return Path(node), None, None


def provider_tool_versions() -> dict[str, str | None]:
    """Probe provider runtimes without reading project configuration or content."""

    _node, _index, pyright_version = _pyright_locations()
    ruff_version = _package_version("ruff")
    _git_executable, git_version = _git_tool_probe()
    return {
        RUFF_PROTECTED_PROVIDER_ID: ruff_version,
        RUFF_TRUSTED_PROVIDER_ID: ruff_version,
        MYPY_PROVIDER_ID: _package_version("mypy"),
        PYRIGHT_PROVIDER_ID: pyright_version,
        RUFF_ANALYZE_PROVIDER_ID: ruff_version,
        GRIMP_ARCHITECTURE_PROVIDER_ID: _package_version("grimp"),
        COMPLEXIPY_COGNITIVE_PROVIDER_ID: _package_version("complexipy"),
        VULTURE_UNUSED_PROVIDER_ID: _package_version("vulture"),
        SEMGREP_INVARIANTS_PROVIDER_ID: managed_semgrep_version(),
        DEPTRY_PROVIDER_ID: _package_version("deptry"),
        PIP_AUDIT_PROVIDER_ID: _package_version("pip-audit"),
        INSTALLED_PACKAGE_PROVIDER_ID: _package_version("neocortex-framework"),
        GIT_HISTORY_PROVIDER_ID: git_version,
        PYTEST_COVERAGE_PROVIDER_ID: _deep_tool_version(),
        COSMIC_RAY_MUTATION_PROVIDER_ID: cosmic_ray_tool_version(),
    }


class PyrightTrustedProjectProvider(_TrustedStaticProvider):
    provider_id = PYRIGHT_PROVIDER_ID
    provider_schema = "neocortex.pyright-trusted-project/v3"
    tool_name = "pyright"
    source = "external:pyright"
    memory_bound = _PYRIGHT_MEMORY_BYTES
    execution_strategy = "trusted-config-staged-project-runtime-search-node-memory-v4"

    def __init__(self, root: Path):
        self._node, self._index, self._pyright_version = _pyright_locations()
        super().__init__(root)

    def _resolve_version(self) -> str | None:
        return self._pyright_version

    def _node_path_for_signature(self) -> str | None:
        return None if self._node is None else str(self._node)

    def _validate_configuration(self, config: Mapping[str, object]) -> None:
        tool = config.get("tool")
        pyright = tool.get("pyright") if isinstance(tool, Mapping) else None
        if not isinstance(pyright, Mapping):
            raise ValueError("tool.pyright is missing")
        forbidden = {
            "extends",
            "typeshedPath",
            "stubPath",
            "extraPaths",
            "venvPath",
            "venv",
        }
        observed = forbidden & set(pyright)
        if observed:
            raise ValueError(
                "pyright external path settings are not authorized: " + ",".join(sorted(observed))
            )

    def _configuration_payload(self) -> Mapping[str, object]:
        return {
            "adapter": self.provider_schema,
            "configuration": "project-pyproject",
            "python_version": "3.13",
            "python_platform": "Windows" if os.name == "nt" else platform.system(),
            "output": "json",
            "cache": "ephemeral-owned",
            "import_resolution": "canonical-runtime-purelib-explicit-v1",
            "node_old_space_mib": _PYRIGHT_NODE_OLD_SPACE_MIB,
        }

    @staticmethod
    def _runtime_search_path() -> Path:
        value = sysconfig.get_path("purelib")
        if not isinstance(value, str) or not value:
            raise ValueError("canonical runtime purelib is unavailable")
        path = Path(value)
        if not path.is_dir():
            raise ValueError("canonical runtime purelib is not a directory")
        return path

    def _execute(
        self,
        stage_root: Path,
        staged: Mapping[str, ExternalEvidenceFile],
        config_path: Path,
        environment: Mapping[str, str],
    ) -> tuple[tuple[ExternalProviderFinding, ...], int, int, int]:
        if self._node is None or self._index is None:
            raise ValueError("owned Pyright runtime is unavailable")
        tool = self._config.get("tool")
        configured = tool.get("pyright") if isinstance(tool, Mapping) else None
        if not isinstance(configured, Mapping):
            raise ValueError("tool.pyright is missing")
        payload = dict(configured)
        payload["include"] = [
            os.path.relpath(path, stage_root).replace("\\", "/") for path in staged
        ]
        payload["exclude"] = []
        payload["pythonVersion"] = "3.13"
        payload["pythonPlatform"] = "Windows" if os.name == "nt" else platform.system()
        payload["executionEnvironments"] = [
            {
                "root": "source",
                "pythonVersion": "3.13",
                "pythonPlatform": "Windows" if os.name == "nt" else platform.system(),
            }
        ]
        # Pyright 1.1.411 does not discover a CPython 3.14 venv's purelib
        # while intentionally checking 3.13-compatible source. Declare the
        # active immutable runtime's import root explicitly instead of
        # converting every third-party import into a project diagnostic.
        payload["extraPaths"] = [str(self._runtime_search_path())]
        generated_config = stage_root / "pyrightconfig.json"
        generated_config.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        selected_environment = dict(environment)
        selected_environment["PYRIGHT_TMPDIR"] = str(stage_root)
        command = (
            str(self._node),
            f"--max-old-space-size={_PYRIGHT_NODE_OLD_SPACE_MIB}",
            str(self._index),
            "--outputjson",
            "--project",
            str(generated_config),
            "--pythonpath",
            sys.executable,
            "--pythonversion",
            "3.13",
            "--pythonplatform",
            "Windows" if os.name == "nt" else platform.system(),
        )
        completed = run_bounded_capture(
            command,
            timeout_seconds=_TOOL_TIMEOUT_SECONDS,
            stdout_limit_bytes=_STDOUT_LIMIT_BYTES,
            stderr_limit_bytes=_STDERR_LIMIT_BYTES,
            cwd=stage_root,
            environment=selected_environment,
            memory_limit_bytes=_PYRIGHT_MEMORY_BYTES if os.name == "nt" else None,
        )
        if completed.returncode not in {0, 1}:
            raise ValueError(_unexpected_exit_message("pyright", completed))
        try:
            decoded = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("pyright JSON output is malformed") from exc
        if not isinstance(decoded, Mapping):
            raise ValueError("pyright JSON output is not an object")
        diagnostics = decoded.get("generalDiagnostics")
        summary = decoded.get("summary")
        if not isinstance(diagnostics, list) or not isinstance(summary, Mapping):
            raise ValueError("pyright JSON output is incomplete")
        analyzed = summary.get("filesAnalyzed")
        if not isinstance(analyzed, int) or analyzed < len(staged):
            raise ValueError("pyright did not cover every eligible file")
        findings: list[ExternalProviderFinding] = []
        for item in diagnostics:
            if not isinstance(item, Mapping):
                raise ValueError("pyright diagnostic is not an object")
            filename = item.get("file")
            if not isinstance(filename, str):
                raise ValueError("pyright diagnostic path is missing")
            owner = staged.get(os.path.normcase(os.path.abspath(filename)))
            if owner is None:
                raise ValueError("pyright reported an unowned path")
            raw_range = item.get("range")
            location_precision = "range"
            if raw_range is None:
                start_line = end_line = 1
                start_column = end_column = 0
                location_precision = "file"
            else:
                if not isinstance(raw_range, Mapping):
                    raise ValueError("pyright diagnostic range is malformed")
                start = raw_range.get("start")
                end = raw_range.get("end")
                if not isinstance(start, Mapping) or not isinstance(end, Mapping):
                    raise ValueError("pyright diagnostic range is malformed")
                start_line = int(start.get("line", 0)) + 1
                start_column = int(start.get("character", 0))
                end_line = int(end.get("line", start_line - 1)) + 1
                end_column = int(end.get("character", start_column))
            rule = item.get("rule")
            code = "pyright" if rule is None else str(rule)[:128]
            severity = str(item.get("severity") or "warning")
            message = str(item.get("message") or "")[:4096]
            findings.append(
                _finding(
                    provider_id=self.provider_id,
                    owner=owner,
                    category="typing",
                    code=code,
                    severity=_normalized_severity(severity),
                    message=message,
                    start_line=start_line,
                    start_column=start_column,
                    end_line=end_line,
                    end_column=end_column,
                    metadata={
                        "reported_severity": severity,
                        "location_precision": location_precision,
                    },
                )
            )
        if len(findings) > RUFF_MAX_DIAGNOSTICS:
            raise ValueError("pyright diagnostics exceed their bound")
        return (
            tuple(findings),
            len(completed.stdout),
            len(completed.stderr),
            1,
        )


class VultureUnusedStaticProvider:
    """Run advisory Vulture evidence over the exact project-wide Python input."""

    executor: Callable[
        [Path, Mapping[str, ExternalEvidenceFile], Mapping[str, str]],
        VultureUnusedExecution,
    ] = staticmethod(execute_vulture_unused)

    def __init__(self, root: Path) -> None:
        self.root = root
        self._version = _package_version("vulture")
        version = self._version or "unavailable"
        self.descriptor = _provider_descriptor(
            provider_id=VULTURE_UNUSED_PROVIDER_ID,
            provider_schema=VULTURE_UNUSED_PROVIDER_SCHEMA,
            tool_name="vulture",
            tool_version=version,
            profile="trusted-static",
            source="external:vulture-unused-static",
            configuration_payload={
                "adapter": VULTURE_UNUSED_PROVIDER_SCHEMA,
                "input": "exact-current-inventory-python",
                "api": "Vulture.scavenge/get_unused_code",
                "min_confidence": 0,
                "sort_by_size": False,
                "ignore_names": [],
                "ignore_decorators": [],
                "project_configuration": False,
                "plugins": False,
                "content_execution": False,
                "network": False,
                "autofix": False,
                "cache": False,
            },
            project_configuration_digest=None,
            environment_signature=_environment_signature(
                tool_name="vulture",
                tool_version=version,
            ),
            root_identity=external_root_identity(root),
            execution_strategy="isolated-python-worker-vulture-project-v1",
            invalidation_strategy="project_wide",
            memory=_VULTURE_MEMORY_BYTES,
            loads_project_configuration=False,
        )

    def tool_version(self) -> str | None:
        return self._version

    def run(
        self,
        root: Path,
        files: Sequence[ExternalEvidenceFile],
        *,
        baseline: ExternalProviderBaseline | None,
        scratch_root: Path,
    ) -> ExternalProviderPublication:
        if _baseline_allows_exact_replay(baseline, external_input_signature(files)):
            assert baseline is not None
            return _exact_replay(
                self.descriptor,
                root,
                files,
                baseline,
                limitations=_VULTURE_LIMITATIONS,
            )
        started_ns = time.time_ns()
        if self._version is None:
            return _failure(
                self.descriptor,
                root,
                files,
                tool_version="unavailable",
                status="unavailable",
                reason="vulture_unavailable",
                started_ns=started_ns,
            )
        try:
            staging_parent = _validated_staging_parent(root, scratch_root)
            with tempfile.TemporaryDirectory(
                prefix="neocortex-vulture-unused-", dir=staging_parent
            ) as temporary:
                stage_root = Path(temporary)
                staged = _stage_external_inputs(files, stage_root / "source")
                environment = _controlled_environment()
                for name in ("TEMP", "TMP", "TMPDIR"):
                    environment[name] = str(stage_root)
                result = self.executor(stage_root, staged, environment)
                if result.limitations != _VULTURE_LIMITATIONS:
                    raise ValueError("Vulture adapter limitations disagree with provider contract")
        except subprocess.TimeoutExpired:
            return _failure(
                self.descriptor,
                root,
                files,
                tool_version=self._version,
                status="timeout",
                reason="provider_timeout",
                started_ns=started_ns,
            )
        except (OSError, RuntimeError, TypeError, ValueError, SubprocessOutputLimitError) as exc:
            return _failure(
                self.descriptor,
                root,
                files,
                tool_version=self._version,
                status="failed",
                reason=f"provider_failure:{type(exc).__name__}:{exc}"[:4096],
                started_ns=started_ns,
            )
        return _success(
            self.descriptor,
            root,
            files,
            result.findings,
            baseline,
            tool_version=self._version,
            started_ns=started_ns,
            stdout_bytes=result.stdout_bytes,
            stderr_bytes=result.stderr_bytes,
            process_invocations=result.process_invocations,
            bytes_staged=sum(item.size for item in files),
            limitations=result.limitations,
        )


_SEMGREP_REPLAY_LIMITATIONS = (
    "semgrep_ce_single_file_analysis",
    "local_neocortex_rules_only",
    "advisory_only_no_mutation_authority",
    "autofix_disabled",
) + (("windows_pysemgrep_x509_compatibility",) if os.name == "nt" else ())
_SEMGREP_RULE_FIXTURE_PREFIX = ("tests", "fixtures", "semgrep_invariants")


class SemgrepNeocortexInvariantsProvider:
    """Run the small versioned Neocortex invariant ruleset without autofix."""

    def __init__(
        self,
        root: Path,
        *,
        executor: Callable[
            [Path, Mapping[str, ExternalEvidenceFile], Mapping[str, str]],
            SemgrepInvariantExecution,
        ] = execute_semgrep_invariants,
    ) -> None:
        self.root = root
        self.executor = executor
        self._version = managed_semgrep_version()
        version = self._version or "unavailable"
        self.descriptor = _provider_descriptor(
            provider_id=SEMGREP_INVARIANTS_PROVIDER_ID,
            provider_schema=SEMGREP_INVARIANTS_PROVIDER_SCHEMA,
            tool_name="semgrep",
            tool_version=version,
            profile="trusted-static",
            source="external:semgrep-neocortex-invariants",
            configuration_payload={
                "adapter": SEMGREP_INVARIANTS_PROVIDER_SCHEMA,
                "ruleset_version": SEMGREP_RULESET_VERSION,
                "ruleset_sha256": SEMGREP_RULESET_SHA256,
                "rule_ids": list(SEMGREP_INVARIANT_RULE_IDS),
                "configuration": "packaged-local-ruleset",
                "input": "exact-current-inventory-python-and-stubs-excluding-own-rule-fixtures",
                "excluded_paths": ["tests/fixtures/semgrep_invariants/**"],
                "metrics": False,
                "version_check": False,
                "autofix": False,
                "network": False,
            },
            project_configuration_digest=None,
            environment_signature=_supply_environment_signature(
                tool_name="semgrep",
                tool_version=version,
            ),
            root_identity=external_root_identity(root),
            execution_strategy="local-rules-staged-batches-v2",
            invalidation_strategy="project_wide",
            memory=_SEMGREP_MEMORY_BYTES,
            loads_project_configuration=False,
            scope="current-inventory-python-and-stubs-excluding-own-rule-fixtures",
        )

    @staticmethod
    def _files(files: Sequence[ExternalEvidenceFile]) -> tuple[ExternalEvidenceFile, ...]:
        return tuple(
            item
            for item in _python_provider_files(files, suffixes=frozenset({".py", ".pyi"}))
            if PurePosixPath(item.relative_path).parts[:3] != _SEMGREP_RULE_FIXTURE_PREFIX
        )

    def tool_version(self) -> str | None:
        return self._version

    def baseline_input_signature(self, files: Sequence[ExternalEvidenceFile]) -> str:
        return external_input_signature(self._files(files))

    def run(
        self,
        root: Path,
        files: Sequence[ExternalEvidenceFile],
        *,
        baseline: ExternalProviderBaseline | None,
        scratch_root: Path,
    ) -> ExternalProviderPublication:
        selected = self._files(files)
        signature = external_input_signature(selected)
        if _baseline_allows_exact_replay(baseline, signature):
            assert baseline is not None
            return _exact_replay(
                self.descriptor,
                root,
                selected,
                baseline,
                limitations=_SEMGREP_REPLAY_LIMITATIONS,
            )
        started_ns = time.time_ns()
        if self._version is None:
            return _failure(
                self.descriptor,
                root,
                selected,
                tool_version="unavailable",
                status="unavailable",
                reason="semgrep_unavailable",
                started_ns=started_ns,
            )
        try:
            staging_parent = _validated_staging_parent(root, scratch_root)
            with tempfile.TemporaryDirectory(
                prefix="neocortex-semgrep-invariants-",
                dir=staging_parent,
            ) as temporary:
                stage_root = Path(temporary)
                staged = _stage_external_inputs(selected, stage_root / "source")
                execution = self.executor(stage_root, staged, _controlled_environment())
                if (
                    execution.scanned_files != len(selected)
                    or execution.scanned_bytes != sum(item.size for item in selected)
                    or execution.rule_count != len(SEMGREP_INVARIANT_RULE_IDS)
                    or execution.ruleset_sha256 != SEMGREP_RULESET_SHA256
                    or execution.limitations != _SEMGREP_REPLAY_LIMITATIONS
                ):
                    raise ValueError("Semgrep execution disagrees with provider contract")
        except subprocess.TimeoutExpired:
            return _failure(
                self.descriptor,
                root,
                selected,
                tool_version=self._version,
                status="timeout",
                reason="provider_timeout",
                started_ns=started_ns,
            )
        except (OSError, RuntimeError, TypeError, ValueError, SubprocessOutputLimitError) as exc:
            return _failure(
                self.descriptor,
                root,
                selected,
                tool_version=self._version,
                status="failed",
                reason=f"provider_failure:{type(exc).__name__}:{exc}"[:4096],
                started_ns=started_ns,
            )
        publication = _success(
            self.descriptor,
            root,
            selected,
            execution.findings,
            baseline,
            tool_version=self._version,
            started_ns=started_ns,
            stdout_bytes=execution.stdout_bytes,
            stderr_bytes=execution.stderr_bytes,
            process_invocations=execution.process_invocations,
            bytes_staged=execution.scanned_bytes,
            limitations=execution.limitations,
        )
        return _attach_supply_execution(
            publication,
            counters={
                "semgrep_scanned_files": execution.scanned_files,
                "semgrep_scanned_bytes": execution.scanned_bytes,
                "semgrep_rule_count": execution.rule_count,
            },
            details={
                "ruleset_version": SEMGREP_RULESET_VERSION,
                "ruleset_sha256": execution.ruleset_sha256,
                "input_manifest_sha256": execution.input_manifest_sha256,
                "cli_variant": execution.cli_variant,
                "autofix": False,
            },
        )


class DeptryProjectDependenciesProvider:
    """Correlate exact Python imports with trusted project dependency declarations."""

    def __init__(
        self,
        root: Path,
        *,
        executor: Callable[
            [Path, Mapping[str, ExternalEvidenceFile], Path, Mapping[str, str]],
            DependencyHygieneExecution,
        ] = execute_deptry_dependency_hygiene,
    ) -> None:
        self.root = root
        self.executor = executor
        self._config_error: str | None = None
        try:
            self._config_raw, self._config, config_digest = _project_configuration(root)
        except (OSError, ValueError) as exc:
            self._config_raw = b""
            self._config = {}
            config_digest = None
            self._config_error = f"{type(exc).__name__}:{exc}"
        self._version = _package_version("deptry")
        version = self._version or "unavailable"
        self.descriptor = _provider_descriptor(
            provider_id=DEPTRY_PROVIDER_ID,
            provider_schema=DEPTRY_PROVIDER_SCHEMA,
            tool_name="deptry",
            tool_version=version,
            profile="trusted-static",
            source="external:deptry-project-dependencies",
            configuration_payload={
                "adapter": DEPTRY_PROVIDER_SCHEMA,
                "configuration": "project-pyproject",
                "input": "exact-current-inventory-python",
                "codes": ["DEP001", "DEP002", "DEP003", "DEP004", "DEP005"],
                "dev_optional_groups": ["dev"],
                "package_module_name_map": {
                    package: list(modules) for package, modules in DEPTRY_PACKAGE_MODULE_NAME_MAP
                },
                "notebooks": False,
                "autofix": False,
                "network": False,
            },
            project_configuration_digest=config_digest,
            environment_signature=_supply_environment_signature(
                tool_name="deptry",
                tool_version=version,
            ),
            root_identity=external_root_identity(root),
            execution_strategy="trusted-config-staged-project-deptry-v1",
            invalidation_strategy="project_wide",
            memory=_DEPTRY_MEMORY_BYTES,
            loads_project_configuration=True,
        )

    @staticmethod
    def _files(files: Sequence[ExternalEvidenceFile]) -> tuple[ExternalEvidenceFile, ...]:
        return _python_provider_files(files, suffixes=frozenset({".py"}))

    def tool_version(self) -> str | None:
        return self._version

    def baseline_input_signature(self, files: Sequence[ExternalEvidenceFile]) -> str:
        return external_input_signature(self._files(files))

    def run(
        self,
        root: Path,
        files: Sequence[ExternalEvidenceFile],
        *,
        baseline: ExternalProviderBaseline | None,
        scratch_root: Path,
    ) -> ExternalProviderPublication:
        selected = self._files(files)
        signature = external_input_signature(selected)
        if _baseline_allows_exact_replay(baseline, signature):
            assert baseline is not None
            return _exact_replay(
                self.descriptor,
                root,
                selected,
                baseline,
                limitations=DEPTRY_LIMITATIONS,
            )
        started_ns = time.time_ns()
        if self._config_error is not None:
            return _failure(
                self.descriptor,
                root,
                selected,
                tool_version=self._version or "unavailable",
                status="failed",
                reason=f"project_configuration_invalid:{self._config_error}"[:4096],
                started_ns=started_ns,
            )
        if self._version is None:
            return _failure(
                self.descriptor,
                root,
                selected,
                tool_version="unavailable",
                status="unavailable",
                reason="deptry_unavailable",
                started_ns=started_ns,
            )
        try:
            staging_parent = _validated_staging_parent(root, scratch_root)
            with tempfile.TemporaryDirectory(
                prefix="neocortex-deptry-project-",
                dir=staging_parent,
            ) as temporary:
                stage_root = Path(temporary)
                staged = _stage_external_inputs(selected, stage_root / "source")
                config_path = stage_root / "pyproject.toml"
                config_path.write_bytes(self._config_raw)
                execution = self.executor(
                    stage_root,
                    staged,
                    config_path,
                    _controlled_environment(),
                )
                if execution.limitations != DEPTRY_LIMITATIONS:
                    raise ValueError("Deptry execution disagrees with provider contract")
        except subprocess.TimeoutExpired:
            return _failure(
                self.descriptor,
                root,
                selected,
                tool_version=self._version,
                status="timeout",
                reason="provider_timeout",
                started_ns=started_ns,
            )
        except (OSError, RuntimeError, TypeError, ValueError, SubprocessOutputLimitError) as exc:
            return _failure(
                self.descriptor,
                root,
                selected,
                tool_version=self._version,
                status="failed",
                reason=f"provider_failure:{type(exc).__name__}:{exc}"[:4096],
                started_ns=started_ns,
            )
        publication = _success(
            self.descriptor,
            root,
            selected,
            execution.findings,
            baseline,
            metrics=execution.metrics,
            relations=execution.relations,
            tool_version=self._version,
            started_ns=started_ns,
            stdout_bytes=execution.stdout_bytes,
            stderr_bytes=execution.stderr_bytes,
            process_invocations=execution.process_invocations,
            bytes_staged=sum(item.size for item in selected) + len(self._config_raw),
            limitations=execution.limitations,
        )
        return _attach_supply_execution(
            publication,
            counters=execution.counters,
            details={
                "configuration": "project-pyproject",
                "project_configuration_digest": self.descriptor.project_configuration_digest,
                "autofix": False,
            },
        )


class PipAuditKnownVulnerabilitiesProvider:
    """Record one fresh, bounded advisory snapshot for the installed environment."""

    def __init__(
        self,
        root: Path,
        *,
        executor: Callable[..., PipAuditExecution] = execute_pip_audit_known_vulnerabilities,
    ) -> None:
        self.root = root
        self.executor = executor
        self._version = _package_version("pip-audit")
        self._utc_date = dt.datetime.now(tz=dt.UTC).date().isoformat()
        self._installed_signature: str | None
        self._source_input_signature: str | None
        self._source_input_bytes = 0
        environment_errors: list[str] = []
        environment_started = time.perf_counter_ns()
        try:
            (
                self._source_input_signature,
                self._source_input_bytes,
            ) = _pip_audit_source_input_signature(root)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._source_input_signature = None
            environment_errors.append(f"source:{type(exc).__name__}:{exc}")
        if self._version is None:
            self._installed_signature = None
        else:
            try:
                self._installed_signature = _installed_distribution_signature()
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                self._installed_signature = None
                environment_errors.append(f"environment:{type(exc).__name__}:{exc}")
        self._environment_error = ";".join(environment_errors) or None
        self._environment_preparation_milliseconds = max(
            0,
            (time.perf_counter_ns() - environment_started) // 1_000_000,
        )
        version = self._version or "unavailable"
        self.descriptor = _provider_descriptor(
            provider_id=PIP_AUDIT_PROVIDER_ID,
            provider_schema=PIP_AUDIT_PROVIDER_SCHEMA,
            tool_name="pip-audit",
            tool_version=version,
            profile="trusted-static",
            source="external:pip-audit-known-vulnerabilities",
            configuration_payload={
                "adapter": PIP_AUDIT_PROVIDER_SCHEMA,
                "service": PIP_AUDIT_SERVICE,
                "input": "installed-python-environment",
                "snapshot_freshness_seconds": 24 * 60 * 60,
                "snapshot_replay": "published-fresh-until-fence-v1",
                "source_input_signature": self._source_input_signature,
                "descriptions": False,
                "aliases": True,
                "fix": False,
            },
            project_configuration_digest=None,
            environment_signature=_supply_environment_signature(
                tool_name="pip-audit",
                tool_version=version,
                installed_signature=self._installed_signature,
            ),
            root_identity=external_root_identity(root),
            execution_strategy="installed-environment-pypi-snapshot-v1",
            # The persisted v5 contract has no environment-local enum. The
            # provider's input signature is nevertheless environment-only;
            # keep the conservative project-wide label until that schema is
            # versioned rather than writing an unsupported value.
            invalidation_strategy="project_wide",
            memory=_PIP_AUDIT_MEMORY_BYTES,
            loads_project_configuration=False,
            scope="installed-python-environment",
            uses_network=True,
        )

    def tool_version(self) -> str | None:
        return self._version

    def baseline_input_signature(self, _files: Sequence[ExternalEvidenceFile]) -> str:
        if self._installed_signature is None or self._source_input_signature is None:
            raise ValueError("installed environment or source signature is unavailable")
        return external_signature(
            "pip-audit-provider-input-v1",
            {
                "installed_signature": self._installed_signature,
                "source_input_signature": self._source_input_signature,
                "service": PIP_AUDIT_SERVICE,
            },
        )

    def run(
        self,
        root: Path,
        files: Sequence[ExternalEvidenceFile],
        *,
        baseline: ExternalProviderBaseline | None,
        scratch_root: Path,
    ) -> ExternalProviderPublication:
        del files
        started_ns = time.time_ns()
        if self._environment_error is not None:
            return _failure(
                self.descriptor,
                root,
                (),
                tool_version=self._version or "unavailable",
                status="failed",
                reason=f"installed_environment_invalid:{self._environment_error}"[:4096],
                started_ns=started_ns,
            )
        if self._version is None:
            return _failure(
                self.descriptor,
                root,
                (),
                tool_version="unavailable",
                status="unavailable",
                reason="pip_audit_unavailable",
                started_ns=started_ns,
            )
        signature = self.baseline_input_signature(())
        limitations = PIP_AUDIT_LIMITATIONS
        replay_is_fresh = (
            _baseline_allows_exact_replay(baseline, signature)
            and baseline is not None
            and baseline.fresh_until_unix_seconds is not None
            and time.time() <= baseline.fresh_until_unix_seconds
        )
        if replay_is_fresh:
            assert baseline is not None
            replay = _exact_replay(
                self.descriptor,
                root,
                (),
                baseline,
                limitations=limitations,
                input_signature_override=signature,
            )
            return _attach_supply_execution(
                replay,
                counters={
                    "environment_preparation_milliseconds": (
                        self._environment_preparation_milliseconds
                    ),
                    "wall_milliseconds": max(
                        int(replay.counters.get("wall_milliseconds", 0)),
                        self._environment_preparation_milliseconds,
                    ),
                },
                details={
                    "whole_publication_replay": True,
                    "source_input_signature": self._source_input_signature,
                    "source_input_bytes": self._source_input_bytes,
                    "fresh_until_unix_seconds": baseline.fresh_until_unix_seconds,
                    "freshness_checked_at_unix_seconds": time.time(),
                    "uses_network": False,
                    "fix": False,
                },
            )
        if os.environ.get("NEOCORTEX_PIP_AUDIT_NETWORK_POLICY") == "disabled-by-code-validation":
            return _failure(
                self.descriptor,
                root,
                (),
                tool_version=self._version,
                status="failed",
                reason=("pip_audit_network_unavailable:disabled_by_local_code_validation_policy"),
                started_ns=started_ns,
                process_invocations=0,
            )
        try:
            staging_parent = _validated_staging_parent(root, scratch_root)
            with tempfile.TemporaryDirectory(
                prefix="neocortex-pip-audit-", dir=staging_parent
            ) as temporary:
                environment = _controlled_environment()
                for name in ("TEMP", "TMP", "TMPDIR"):
                    environment[name] = temporary
                execution = self.executor(environment)
            if execution.tool_version != self._version:
                raise ValueError("pip-audit execution version disagrees with provider")
            if execution.observed_date_utc != self._utc_date:
                raise ValueError("pip-audit snapshot crossed its UTC date boundary")
            if not execution.uses_network or execution.limitations != limitations:
                raise ValueError("pip-audit execution disagrees with provider contract")
        except subprocess.TimeoutExpired:
            return _failure(
                self.descriptor,
                root,
                (),
                tool_version=self._version,
                status="timeout",
                reason="provider_timeout",
                started_ns=started_ns,
            )
        except (OSError, RuntimeError, TypeError, ValueError, SubprocessOutputLimitError) as exc:
            return _failure(
                self.descriptor,
                root,
                (),
                tool_version=self._version,
                status="failed",
                reason=f"provider_failure:{type(exc).__name__}:{exc}"[:4096],
                started_ns=started_ns,
            )
        publication = _success(
            self.descriptor,
            root,
            (),
            (),
            baseline,
            metrics=execution.metrics,
            relations=execution.relations,
            tool_version=self._version,
            started_ns=started_ns,
            stdout_bytes=execution.stdout_bytes,
            stderr_bytes=execution.stderr_bytes,
            process_invocations=execution.process_invocations,
            bytes_staged=self._source_input_bytes,
            limitations=execution.limitations,
            input_signature_override=signature,
        )
        return _attach_supply_execution(
            publication,
            counters={
                **{name: int(value) for name, value in asdict(execution.counters).items()},
                "environment_preparation_milliseconds": (
                    self._environment_preparation_milliseconds
                ),
                "wall_milliseconds": max(
                    int(publication.counters.get("wall_milliseconds", 0)),
                    self._environment_preparation_milliseconds,
                ),
            },
            details={
                "source": execution.source,
                "service": PIP_AUDIT_SERVICE,
                "observed_at_utc": execution.observed_at_utc,
                "observed_date_utc": execution.observed_date_utc,
                "fresh_until_utc": execution.fresh_until_utc,
                "freshness_status": execution.freshness_status,
                "snapshot_id": execution.snapshot_id,
                "source_input_signature": self._source_input_signature,
                "source_input_bytes": self._source_input_bytes,
                "uses_network": execution.uses_network,
                "fix": False,
            },
        )


class InstalledPackageInventoryProvider:
    """Verify the installed NeoCortex wheel and inventory package/license metadata."""

    def __init__(
        self,
        root: Path,
        *,
        executor: Callable[..., InstalledPackageInventoryExecution] = (
            execute_installed_package_inventory
        ),
    ) -> None:
        self.root = root
        self.executor = executor
        self._config_error: str | None = None
        self._project_digest: str | None
        try:
            _raw, _config, self._project_digest = _project_configuration(root)
        except (OSError, ValueError) as exc:
            self._project_digest = None
            self._config_error = f"{type(exc).__name__}:{exc}"
        self._version = _package_version("neocortex-framework")
        self._environment_error: str | None = None
        self._installed_signature: str | None
        if self._version is None:
            self._installed_signature = None
        else:
            try:
                self._installed_signature = _installed_distribution_signature()
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                self._installed_signature = None
                self._environment_error = f"{type(exc).__name__}:{exc}"
        version = self._version or "unavailable"
        self.descriptor = _provider_descriptor(
            provider_id=INSTALLED_PACKAGE_PROVIDER_ID,
            provider_schema=INSTALLED_PACKAGE_PROVIDER_SCHEMA,
            tool_name="importlib.metadata+RECORD",
            tool_version=version,
            profile="trusted-static",
            source="external:installed-package-inventory",
            configuration_payload={
                "adapter": INSTALLED_PACKAGE_PROVIDER_SCHEMA,
                "input": "installed-python-environment-and-project-pyproject",
                "record_verification": "hash-and-size",
                "base_dependency_constraints": "marker-and-specifier-evaluated",
                "optional_extra_constraints": "recorded-not-gated",
                "license_metadata": "inventory-only-no-legal-conclusion",
                "network": False,
                "mutation": False,
            },
            project_configuration_digest=self._project_digest,
            environment_signature=_supply_environment_signature(
                tool_name="importlib.metadata+RECORD",
                tool_version=version,
                installed_signature=self._installed_signature,
            ),
            root_identity=external_root_identity(root),
            execution_strategy="installed-wheel-record-and-metadata-v1",
            invalidation_strategy="project_wide",
            memory=_PACKAGE_INVENTORY_MEMORY_BYTES,
            loads_project_configuration=True,
            scope="installed-python-environment",
        )
        self._prepared: InstalledPackageInventoryExecution | None = None
        self._prepared_signature: str | None = None
        self._prepared_milliseconds = 0
        self._preparation_error: Exception | None = None

    def tool_version(self) -> str | None:
        return self._version

    def _prepare(self) -> tuple[InstalledPackageInventoryExecution, str, bool]:
        if self._prepared is not None and self._prepared_signature is not None:
            return self._prepared, self._prepared_signature, True
        if self._preparation_error is not None:
            raise self._preparation_error
        started = time.perf_counter_ns()
        try:
            execution = self.executor(self.root / "pyproject.toml")
            signature = external_signature(
                "installed-package-inventory-input-v1",
                {
                    "snapshot_id": execution.snapshot_id,
                    "pyproject_sha256": execution.pyproject_sha256,
                    "installed_project_version": execution.installed_project_version,
                },
            )
        except Exception as exc:
            self._preparation_error = exc
            raise
        self._prepared_milliseconds = max(0, (time.perf_counter_ns() - started) // 1_000_000)
        self._prepared = execution
        self._prepared_signature = signature
        return execution, signature, False

    def baseline_input_signature(self, _files: Sequence[ExternalEvidenceFile]) -> str:
        _execution, signature, _cached = self._prepare()
        return signature

    @staticmethod
    def _execution_counters(
        execution: InstalledPackageInventoryExecution,
    ) -> dict[str, int]:
        counters = {name: int(value) for name, value in asdict(execution.counters).items()}
        counters.update(
            {
                "inventory_files_hashed": execution.files_hashed,
                "inventory_bytes_hashed": execution.bytes_hashed,
            }
        )
        return counters

    def _attach_inventory(
        self,
        publication: ExternalProviderPublication,
        execution: InstalledPackageInventoryExecution,
    ) -> ExternalProviderPublication:
        counters = self._execution_counters(execution)
        counters["bytes_read"] = max(
            int(publication.counters.get("bytes_read", 0)),
            execution.bytes_hashed,
        )
        counters["wall_milliseconds"] = max(
            int(publication.counters.get("wall_milliseconds", 0)),
            self._prepared_milliseconds,
        )
        return _attach_supply_execution(
            publication,
            counters=counters,
            details={
                "source": execution.source,
                "observed_at_utc": execution.observed_at_utc,
                "observed_date_utc": execution.observed_date_utc,
                "freshness_status": execution.freshness_status,
                "snapshot_id": execution.snapshot_id,
                "pyproject_sha256": execution.pyproject_sha256,
                "installed_project_version": execution.installed_project_version,
                "uses_network": execution.uses_network,
            },
        )

    def run(
        self,
        root: Path,
        files: Sequence[ExternalEvidenceFile],
        *,
        baseline: ExternalProviderBaseline | None,
        scratch_root: Path,
    ) -> ExternalProviderPublication:
        del files, scratch_root
        started_ns = time.time_ns()
        if self._config_error is not None:
            return _failure(
                self.descriptor,
                root,
                (),
                tool_version=self._version or "unavailable",
                status="failed",
                reason=f"project_configuration_invalid:{self._config_error}"[:4096],
                started_ns=started_ns,
            )
        if self._environment_error is not None:
            return _failure(
                self.descriptor,
                root,
                (),
                tool_version=self._version or "unavailable",
                status="failed",
                reason=f"installed_environment_invalid:{self._environment_error}"[:4096],
                started_ns=started_ns,
            )
        if self._version is None:
            return _failure(
                self.descriptor,
                root,
                (),
                tool_version="unavailable",
                status="unavailable",
                reason="installed_neocortex_distribution_unavailable",
                started_ns=started_ns,
            )
        try:
            execution, signature, cached = self._prepare()
            if cached:
                started_ns -= self._prepared_milliseconds * 1_000_000
            if execution.installed_project_version != self._version:
                raise ValueError("installed inventory version disagrees with provider")
            if execution.uses_network:
                raise ValueError("installed inventory unexpectedly used network")
        except subprocess.TimeoutExpired:
            return _failure(
                self.descriptor,
                root,
                (),
                tool_version=self._version,
                status="timeout",
                reason="provider_timeout",
                started_ns=started_ns,
            )
        except (OSError, RuntimeError, TypeError, ValueError, SubprocessOutputLimitError) as exc:
            return _failure(
                self.descriptor,
                root,
                (),
                tool_version=self._version,
                status="failed",
                reason=f"provider_failure:{type(exc).__name__}:{exc}"[:4096],
                started_ns=started_ns,
            )
        if _baseline_allows_exact_replay(baseline, signature):
            assert baseline is not None
            replay = _exact_replay(
                self.descriptor,
                root,
                (),
                baseline,
                limitations=execution.limitations,
                input_signature_override=signature,
            )
            return self._attach_inventory(replay, execution)
        publication = _success(
            self.descriptor,
            root,
            (),
            (),
            baseline,
            metrics=execution.metrics,
            relations=execution.relations,
            tool_version=self._version,
            started_ns=started_ns,
            stdout_bytes=0,
            stderr_bytes=0,
            process_invocations=execution.process_invocations,
            bytes_staged=0,
            limitations=execution.limitations,
            input_signature_override=signature,
        )
        return self._attach_inventory(publication, execution)


def _architecture_files(
    files: Sequence[ExternalEvidenceFile],
) -> tuple[ExternalEvidenceFile, ...]:
    """Select the exact versioned production-package domain for Hito 2."""

    roots = frozenset(PRODUCTION_ROOT_PACKAGES)
    selected = []
    for item in files:
        parts = PurePosixPath(item.relative_path).parts
        if parts and parts[0] in roots:
            selected.append(item)
    return tuple(sorted(selected, key=lambda item: item.relative_path.casefold()))


class GitHistoryLocalProvider:
    """Publish bounded observations from the exact local Git object database."""

    def __init__(
        self,
        root: Path,
        *,
        config: GitHistoryConfig | None = None,
        inspector: Callable[..., GitRepositorySnapshot] = inspect_git_repository,
        executor: Callable[..., GitHistoryExecution] = execute_git_history,
    ) -> None:
        self.root = root
        self.config = GitHistoryConfig() if config is None else config
        self.inspector = inspector
        self.executor = executor
        self._root_identity = external_root_identity(root)
        self._git_executable, self._version = _git_tool_probe()
        self._prepared: tuple[str, GitRepositorySnapshot, int] | None = None
        version = self._version or "unavailable"
        executable_value = (
            None
            if self._git_executable is None
            else os.path.normcase(os.path.abspath(self._git_executable))
        )
        limits = ProviderLimits(
            self.config.timeout_seconds,
            _GIT_HISTORY_MEMORY_BYTES,
            RUFF_MAX_TOTAL_BYTES,
            self.config.stdout_limit_bytes + self.config.stderr_limit_bytes,
            self.config.max_relations,
        )
        self.descriptor = _provider_descriptor(
            provider_id=GIT_HISTORY_PROVIDER_ID,
            provider_schema=GIT_HISTORY_PROVIDER_SCHEMA,
            tool_name="git",
            tool_version=version,
            profile="trusted-static",
            source="external:git-history-local",
            configuration_payload={
                "adapter": GIT_HISTORY_PROVIDER_SCHEMA,
                "history": self.config.as_payload(),
                "repository_source": "local-object-database",
                "staging": False,
            },
            project_configuration_digest=None,
            environment_signature=_environment_signature(
                tool_name="git",
                tool_version=version,
                path_value=executable_value,
            ),
            root_identity=self._root_identity,
            execution_strategy="bounded-local-git-history-v2",
            invalidation_strategy="project_wide",
            memory=_GIT_HISTORY_MEMORY_BYTES,
            loads_project_configuration=False,
            scope="current-code-inventory-and-local-history-v1",
            limits=limits,
        )

    def tool_version(self) -> str | None:
        return self._version

    def _inspect(
        self,
        files: Sequence[ExternalEvidenceFile],
    ) -> tuple[str, GitRepositorySnapshot, int]:
        started_ns = time.time_ns()
        if self._git_executable is None:
            raise ValueError("git executable is unavailable")
        snapshot = self.inspector(
            self.root,
            _controlled_environment(),
            config=self.config,
            git_executable=str(self._git_executable),
        )
        signature = git_history_input_signature(files, snapshot, config=self.config)
        return signature, snapshot, started_ns

    def baseline_input_signature(self, files: Sequence[ExternalEvidenceFile]) -> str:
        prepared = self._inspect(files)
        self._prepared = prepared
        return prepared[0]

    def _prepared_snapshot(
        self,
        files: Sequence[ExternalEvidenceFile],
    ) -> tuple[str, GitRepositorySnapshot, int]:
        prepared, self._prepared = self._prepared, None
        if prepared is not None:
            signature, snapshot, started_ns = prepared
            if signature == git_history_input_signature(files, snapshot, config=self.config):
                return signature, snapshot, started_ns
        return self._inspect(files)

    @staticmethod
    def _attach_observation(
        publication: ExternalProviderPublication,
        *,
        snapshot: GitRepositorySnapshot,
        started_ns: int,
        execution: GitHistoryExecution | None,
    ) -> ExternalProviderPublication:
        counters = dict(publication.counters)
        if execution is None:
            counters.update(
                {
                    "process_invocations": snapshot.process_invocations,
                    "stdout_bytes": snapshot.stdout_bytes,
                    "stderr_bytes": snapshot.stderr_bytes,
                }
            )
            details: Mapping[str, object] = {
                "provider_schema": GIT_HISTORY_PROVIDER_SCHEMA,
                "source": "local_git_object_database",
                "requested_ref": snapshot.requested_ref,
                "head_commit": snapshot.head_commit,
                "repository_shallow": snapshot.repository_shallow,
                "execution": "head_verified_exact_replay",
                "uses_network": False,
                "executes_content": False,
            }
        else:
            counters.update({name: int(value) for name, value in execution.counters.items()})
            details = execution.provenance
        completed_ns = time.time_ns()
        counters["wall_milliseconds"] = max(0, (completed_ns - started_ns) // 1_000_000)
        provenance = dict(publication.publication.provenance)
        provenance["git_history_execution"] = dict(details)
        inner = replace(
            publication.publication,
            started_ns=started_ns,
            completed_ns=completed_ns,
            provenance=provenance,
        )
        return replace(
            publication,
            publication=inner,
            counters=counters,
            coverage_complete=not snapshot.repository_shallow,
        )

    def run(
        self,
        root: Path,
        files: Sequence[ExternalEvidenceFile],
        *,
        baseline: ExternalProviderBaseline | None,
        scratch_root: Path,
    ) -> ExternalProviderPublication:
        del scratch_root
        started_ns = time.time_ns()
        if external_root_identity(root) != self._root_identity:
            return _failure(
                self.descriptor,
                root,
                files,
                tool_version=self._version or "unavailable",
                status="failed",
                reason="git_history_root_changed_after_provider_construction",
                started_ns=started_ns,
            )
        if self._version is None or self._git_executable is None:
            return _failure(
                self.descriptor,
                root,
                files,
                tool_version="unavailable",
                status="unavailable",
                reason="git_unavailable",
                started_ns=started_ns,
            )
        try:
            signature, snapshot, started_ns = self._prepared_snapshot(files)
            replay_limitations = [
                "merge_commits_excluded_from_churn_window",
                "exact_replay_reuses_published_history_result",
            ]
            if snapshot.repository_shallow:
                replay_limitations.append("shallow_repository_history_incomplete")
            if _baseline_allows_exact_replay(baseline, signature):
                assert baseline is not None
                replay = _exact_replay(
                    self.descriptor,
                    root,
                    files,
                    baseline,
                    limitations=replay_limitations,
                    input_signature_override=signature,
                )
                return self._attach_observation(
                    replay,
                    snapshot=snapshot,
                    started_ns=started_ns,
                    execution=None,
                )
            execution = self.executor(
                root,
                files,
                _controlled_environment(),
                config=self.config,
                snapshot=snapshot,
                git_executable=str(self._git_executable),
            )
            if execution.history_input_signature != signature:
                raise ValueError("Git history execution input signature disagrees")
            if execution.configuration_signature != self.config.signature:
                raise ValueError("Git history execution configuration signature disagrees")
            if execution.head_commit != snapshot.head_commit:
                raise ValueError("Git history execution HEAD disagrees with inspection")
            if execution.process_invocations != snapshot.process_invocations + 2:
                raise ValueError("Git history execution process accounting disagrees")
        except subprocess.TimeoutExpired:
            return _failure(
                self.descriptor,
                root,
                files,
                tool_version=self._version,
                status="timeout",
                reason="provider_timeout",
                started_ns=started_ns,
            )
        except (OSError, RuntimeError, TypeError, ValueError, SubprocessOutputLimitError) as exc:
            return _failure(
                self.descriptor,
                root,
                files,
                tool_version=self._version,
                status="failed",
                reason=f"provider_failure:{type(exc).__name__}:{exc}"[:4096],
                started_ns=started_ns,
            )
        publication = _success(
            self.descriptor,
            root,
            files,
            execution.findings,
            baseline,
            metrics=execution.metrics,
            relations=execution.relations,
            tool_version=self._version,
            started_ns=started_ns,
            stdout_bytes=execution.stdout_bytes,
            stderr_bytes=execution.stderr_bytes,
            process_invocations=execution.process_invocations,
            bytes_staged=0,
            limitations=execution.limitations,
            input_signature_override=signature,
        )
        return self._attach_observation(
            publication,
            snapshot=snapshot,
            started_ns=started_ns,
            execution=execution,
        )


class _TrustedArchitectureProvider:
    provider_id: str
    provider_schema: str
    tool_name: str
    distribution: str
    source: str
    memory_bound: int
    execution_strategy: str
    target_architecture_fingerprint: str | None = None
    executor: Callable[
        [Path, Mapping[str, ExternalEvidenceFile], Mapping[str, str]],
        ArchitectureProviderExecution,
    ]

    def __init__(self, root: Path):
        self.root = root
        self._version = _package_version(self.distribution)
        version = self._version or "unavailable"
        configuration = {
            "adapter": self.provider_schema,
            "architecture_contract_schema": ARCHITECTURE_CONTRACT_SCHEMA,
            "architecture_baseline_id": ARCHITECTURE_BASELINE_ID,
            "domain": list(PRODUCTION_ROOT_PACKAGES),
            "static_only": True,
            "autofix": False,
            "network": False,
            "cache": False,
        }
        if self.target_architecture_fingerprint is not None:
            configuration.update(
                {
                    "core_responsibility_registry_schema": (CORE_RESPONSIBILITY_REGISTRY_SCHEMA),
                    "core_architecture_target_fingerprint": (self.target_architecture_fingerprint),
                }
            )
        self.descriptor = _provider_descriptor(
            provider_id=self.provider_id,
            provider_schema=self.provider_schema,
            tool_name=self.tool_name,
            tool_version=version,
            profile="trusted-static",
            source=self.source,
            configuration_payload=configuration,
            project_configuration_digest=None,
            environment_signature=_environment_signature(
                tool_name=self.tool_name,
                tool_version=version,
            ),
            root_identity=external_root_identity(root),
            execution_strategy=self.execution_strategy,
            invalidation_strategy="project_wide",
            memory=self.memory_bound,
            loads_project_configuration=False,
            scope="production-packages-python-v1",
        )

    def tool_version(self) -> str | None:
        return self._version

    def baseline_input_signature(self, files: Sequence[ExternalEvidenceFile]) -> str:
        return external_input_signature(_architecture_files(files))

    def run(
        self,
        root: Path,
        files: Sequence[ExternalEvidenceFile],
        *,
        baseline: ExternalProviderBaseline | None,
        scratch_root: Path,
    ) -> ExternalProviderPublication:
        selected = _architecture_files(files)
        if _baseline_allows_exact_replay(baseline, external_input_signature(selected)):
            assert baseline is not None
            return _exact_replay(self.descriptor, root, selected, baseline)
        started_ns = time.time_ns()
        if self._version is None:
            return _failure(
                self.descriptor,
                root,
                selected,
                tool_version="unavailable",
                status="unavailable",
                reason=f"{self.tool_name}_unavailable",
                started_ns=started_ns,
            )
        if not selected:
            return _failure(
                self.descriptor,
                root,
                selected,
                tool_version=self._version,
                status="failed",
                reason="architecture_production_domain_empty",
                started_ns=started_ns,
            )
        try:
            staging_parent = _validated_staging_parent(root, scratch_root)
            with tempfile.TemporaryDirectory(
                prefix=f"neocortex-{self.provider_id}-", dir=staging_parent
            ) as temporary:
                stage_root = Path(temporary)
                staged = _stage_external_inputs(selected, stage_root / "source")
                environment = _controlled_environment()
                for name in ("TEMP", "TMP", "TMPDIR"):
                    environment[name] = str(stage_root)
                result = self.executor(stage_root, staged, environment)
        except subprocess.TimeoutExpired:
            return _failure(
                self.descriptor,
                root,
                selected,
                tool_version=self._version,
                status="timeout",
                reason="provider_timeout",
                started_ns=started_ns,
            )
        except (OSError, RuntimeError, TypeError, ValueError, SubprocessOutputLimitError) as exc:
            return _failure(
                self.descriptor,
                root,
                selected,
                tool_version=self._version,
                status="failed",
                reason=f"provider_failure:{type(exc).__name__}:{exc}"[:4096],
                started_ns=started_ns,
            )
        return _success(
            self.descriptor,
            root,
            selected,
            result.findings,
            baseline,
            metrics=result.metrics,
            relations=result.relations,
            tool_version=self._version,
            started_ns=started_ns,
            stdout_bytes=result.stdout_bytes,
            stderr_bytes=result.stderr_bytes,
            process_invocations=result.process_invocations,
            bytes_staged=sum(item.size for item in selected),
        )


class RuffAnalyzeImportsProvider(_TrustedArchitectureProvider):
    provider_id = RUFF_ANALYZE_PROVIDER_ID
    provider_schema = "neocortex.ruff-analyze-imports/v1"
    tool_name = "ruff"
    distribution = "ruff"
    source = "external:ruff-analyze-imports"
    memory_bound = _RUFF_MEMORY_BYTES
    execution_strategy = "isolated-ruff-analyze-graph-v1"
    executor = staticmethod(execute_ruff_analyze_imports)


class GrimpArchitectureProvider(_TrustedArchitectureProvider):
    provider_id = GRIMP_ARCHITECTURE_PROVIDER_ID
    provider_schema = "neocortex.grimp-architecture/v2"
    tool_name = "grimp"
    distribution = "grimp"
    source = "external:grimp-architecture"
    memory_bound = _GRIMP_MEMORY_BYTES
    execution_strategy = "isolated-python-worker-grimp-v3"
    target_architecture_fingerprint = core_architecture_target_fingerprint()
    executor = staticmethod(execute_grimp_architecture)


class ComplexipyCognitiveProvider(_TrustedArchitectureProvider):
    provider_id = COMPLEXIPY_COGNITIVE_PROVIDER_ID
    provider_schema = "neocortex.complexipy-cognitive/v1"
    tool_name = "complexipy"
    distribution = "complexipy"
    source = "external:complexipy-cognitive"
    memory_bound = _COMPLEXIPY_MEMORY_BYTES
    execution_strategy = "isolated-python-worker-complexipy-v1"
    executor = staticmethod(execute_complexipy_cognitive)


_FOCAL_MUTATION_REPLAY_LIMITATIONS = (
    "focal_declared_target_and_tests_only",
    "exact_replay_reuses_published_mutation_result",
    "advisory_only_no_mutation_authority",
    "mutation_score_is_not_defect_probability",
)


class CosmicRayFocalMutationProvider:
    """Execute one explicitly declared focal mutation scope in owned scratch."""

    def __init__(
        self,
        root: Path,
        deep_configuration: Mapping[str, object] | None,
        deep_configuration_signature: str | None,
        *,
        executor: Callable[..., FocalMutationExecution] = execute_cosmic_ray_mutation,
    ) -> None:
        payload, _coverage_config = _validated_deep_configuration(
            deep_configuration,
            deep_configuration_signature,
        )
        assert deep_configuration_signature is not None
        self.root = root
        self._root_identity = external_root_identity(root)
        self.deep_configuration = payload
        self.deep_configuration_signature = deep_configuration_signature
        self.executor = executor
        self._version = cosmic_ray_tool_version()
        version = self._version or "unavailable"
        target = payload.get("mutation_target")
        self._abstention_reason: str | None = None
        self.config: FocalMutationConfig | None = None
        if payload.get("schema") == LEGACY_DEEP_CONFIGURATION_SCHEMA:
            self._abstention_reason = "mutation_not_declared_in_legacy_deep_configuration"
        elif target is None:
            self._abstention_reason = "mutation_target_not_declared"
        else:
            selectors_value = payload.get("test_selectors")
            symbol = payload.get("mutation_symbol")
            max_mutants = payload.get("mutation_max_mutants")
            timeout_seconds = payload.get("mutation_timeout_seconds")
            budget_seconds = payload.get("mutation_time_budget_seconds")
            if (
                not isinstance(target, str)
                or not isinstance(selectors_value, list)
                or any(not isinstance(item, str) for item in selectors_value)
                or (symbol is not None and not isinstance(symbol, str))
                or isinstance(max_mutants, bool)
                or not isinstance(max_mutants, int)
                or isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, int)
                or isinstance(budget_seconds, bool)
                or not isinstance(budget_seconds, int)
            ):
                raise ValueError("trusted-deep mutation configuration is invalid")
            self.config = FocalMutationConfig(
                target,
                symbol,
                tuple(item for item in selectors_value if isinstance(item, str)),
                max_mutants,
                float(timeout_seconds),
                float(budget_seconds),
                deep_configuration_signature,
            )
        configured_budget = 10.0 if self.config is None else self.config.time_budget_seconds
        configured_mutants = 1 if self.config is None else self.config.max_mutants
        pytest_version = _package_version("pytest") or "unavailable"
        self._home_directory = trusted_deep_home_directory()
        environment_signature = external_signature(
            "cosmic-ray-focal-environment-v1",
            {
                "python_executable": _normalized_runtime_path_entry(sys.executable),
                "python_version": platform.python_version(),
                "implementation": platform.python_implementation(),
                "implementation_cache_tag": getattr(sys.implementation, "cache_tag", None),
                "abi_flags": getattr(sys, "abiflags", ""),
                "platform": platform.platform(),
                "cosmic_ray_version": version,
                "pytest_version": pytest_version,
                "home_directory": self._home_directory,
                "path": (
                    None
                    if os.environ.get("PATH") is None
                    else _normalized_runtime_search_path(os.environ["PATH"])
                ),
                "pathext": os.environ.get("PATHEXT"),
            },
        )
        self.descriptor = _provider_descriptor(
            provider_id=COSMIC_RAY_MUTATION_PROVIDER_ID,
            provider_schema=COSMIC_RAY_MUTATION_PROVIDER_SCHEMA,
            tool_name="cosmic-ray",
            tool_version=version,
            profile="trusted-deep",
            source="external:cosmic-ray-focal-mutation",
            configuration_payload={
                "adapter": COSMIC_RAY_MUTATION_PROVIDER_SCHEMA,
                "deep_configuration": payload,
                "deep_configuration_signature": deep_configuration_signature,
                "staging": "exact-owned-scratch-copy",
                "autofix": False,
                "network": True,
            },
            project_configuration_digest=None,
            environment_signature=environment_signature,
            root_identity=self._root_identity,
            execution_strategy="canonical-root-focal-cosmic-ray-v1",
            invalidation_strategy="dynamic_suite",
            memory=_FOCAL_MUTATION_MEMORY_BYTES,
            loads_project_configuration=False,
            scope="canonical-neocortex-focal-mutation-v1",
            limits=ProviderLimits(
                configured_budget + 15.0,
                _FOCAL_MUTATION_MEMORY_BYTES,
                RUFF_MAX_TOTAL_BYTES,
                _FOCAL_MUTATION_OUTPUT_BYTES,
                configured_mutants,
            ),
            imports_content=True,
            executes_content=True,
            uses_network=True,
        )

    def tool_version(self) -> str | None:
        return self._version

    def baseline_input_signature(self, files: Sequence[ExternalEvidenceFile]) -> str:
        validate_external_inputs(files)
        if self.config is not None:
            return mutation_input_signature(files, self.config)
        return external_signature(
            "cosmic-ray-mutation-abstention-input-v1",
            {
                "inventory_signature": external_input_signature(files),
                "deep_configuration_signature": self.deep_configuration_signature,
                "reason": self._abstention_reason,
            },
        )

    def _abstain(
        self,
        root: Path,
        files: Sequence[ExternalEvidenceFile],
        *,
        reason: str,
        started_ns: int,
        process_invocations: int = 0,
    ) -> ExternalProviderPublication:
        return _abstention(
            self.descriptor,
            root,
            files,
            tool_version=self._version or "unavailable",
            reason=reason,
            started_ns=started_ns,
            input_signature_override=self.baseline_input_signature(files),
            process_invocations=process_invocations,
        )

    def _attach_execution(
        self,
        publication: ExternalProviderPublication,
        *,
        configuration: Mapping[str, object],
        execution: FocalMutationExecution | None,
    ) -> ExternalProviderPublication:
        counters = dict(publication.counters)
        provenance = dict(publication.publication.provenance)
        provenance["deep_configuration"] = {
            "payload": dict(configuration),
            "signature": self.deep_configuration_signature,
        }
        mutation_execution: dict[str, object] = {
            "content_executed": publication.execution == "full",
            "whole_publication_replay": publication.execution == "cache_replay",
            "abstained": publication.execution == "skipped",
            "mutation_authority": False,
            "uses_network": True,
        }
        if execution is not None:
            generic_wall = counters.get("wall_milliseconds", 0)
            counters.update({name: int(value) for name, value in execution.counters.items()})
            counters["wall_milliseconds"] = max(
                generic_wall,
                int(execution.counters.get("wall_milliseconds", 0)),
            )
            mutation_execution.update(
                {
                    "measurement_scope_signature": execution.measurement_scope_signature,
                    "measurement_complete": execution.measurement_complete,
                }
            )
        provenance["mutation_execution"] = mutation_execution
        inner = replace(publication.publication, provenance=provenance)
        return replace(publication, publication=inner, counters=counters)

    def run(
        self,
        root: Path,
        files: Sequence[ExternalEvidenceFile],
        *,
        baseline: ExternalProviderBaseline | None,
        scratch_root: Path,
    ) -> ExternalProviderPublication:
        started_ns = time.time_ns()
        if external_root_identity(root) != self._root_identity:
            return _failure(
                self.descriptor,
                root,
                files,
                tool_version=self._version or "unavailable",
                status="failed",
                reason="trusted_deep_mutation_root_changed_after_provider_construction",
                started_ns=started_ns,
            )
        if self.config is None:
            assert self._abstention_reason is not None
            publication = self._abstain(
                root,
                files,
                reason=self._abstention_reason,
                started_ns=started_ns,
            )
            return self._attach_execution(
                publication,
                configuration=self.deep_configuration,
                execution=None,
            )
        input_signature = mutation_input_signature(files, self.config)
        current_paths = {item.relative_path.casefold() for item in files}
        if self.config.target_relative_path.casefold() not in current_paths:
            publication = self._abstain(
                root,
                files,
                reason="mutation_target_not_indexed",
                started_ns=started_ns,
            )
            return self._attach_execution(
                publication,
                configuration=self.deep_configuration,
                execution=None,
            )
        if self._version is None:
            publication = self._abstain(
                root,
                files,
                reason="cosmic_ray_8_4_6_unavailable",
                started_ns=started_ns,
            )
            return self._attach_execution(
                publication,
                configuration=self.deep_configuration,
                execution=None,
            )
        if _baseline_allows_exact_replay(baseline, input_signature):
            assert baseline is not None
            replay = _exact_replay(
                self.descriptor,
                root,
                files,
                baseline,
                limitations=_FOCAL_MUTATION_REPLAY_LIMITATIONS,
                input_signature_override=input_signature,
            )
            return self._attach_execution(
                replay,
                configuration=self.deep_configuration,
                execution=None,
            )
        try:
            staging_parent = _validated_staging_parent(root, scratch_root)
            durable_identity = external_signature(
                "focal-mutation-scratch-v1",
                {
                    "root_identity": self._root_identity,
                    "configuration_signature": self.config.configuration_signature,
                },
            ).rsplit(":", 1)[-1]
            durable_scratch = _durable_provider_scratch(
                staging_parent,
                namespace=_FOCAL_MUTATION_DURABLE_NAMESPACE,
                identity=durable_identity,
                label="focal mutation durable",
            )
            with tempfile.TemporaryDirectory(
                prefix="neocortex-cosmic-ray-focal-",
                dir=staging_parent,
            ) as temporary:
                stage_root = Path(temporary)
                staged = _stage_external_inputs(files, stage_root / "source")
                environment = _controlled_environment()
                # trusted-deep executes the repository's declared tests.  Keep
                # their executable discovery equivalent to the validated host
                # environment (notably Git-based fixtures) while retaining the
                # generic provider's otherwise minimal environment.
                environment["HOME"] = self._home_directory
                if os.name == "nt":
                    environment["USERPROFILE"] = self._home_directory
                for name in ("PATH", "PATHEXT"):
                    value = os.environ.get(name)
                    if value:
                        environment[name] = value
                for name in ("TEMP", "TMP", "TMPDIR"):
                    environment[name] = str(durable_scratch)
                execution = self.executor(
                    stage_root,
                    staged,
                    environment,
                    trusted_root=root,
                    scratch_root=durable_scratch,
                    config=self.config,
                )
            if execution.measurement_scope_signature != input_signature:
                raise ValueError("focal mutation execution input signature disagrees")
            if execution.process_invocations != int(
                execution.counters.get("process_invocations", -1)
            ):
                raise ValueError("focal mutation execution process accounting disagrees")
        except MutationAbstentionError as exc:
            publication = self._abstain(
                root,
                files,
                reason=exc.reason,
                started_ns=started_ns,
            )
            return self._attach_execution(
                publication,
                configuration=self.deep_configuration,
                execution=None,
            )
        except subprocess.TimeoutExpired:
            return _failure(
                self.descriptor,
                root,
                files,
                tool_version=self._version,
                status="timeout",
                reason="provider_timeout",
                started_ns=started_ns,
            )
        except (OSError, RuntimeError, TypeError, ValueError, SubprocessOutputLimitError) as exc:
            return _failure(
                self.descriptor,
                root,
                files,
                tool_version=self._version,
                status="failed",
                reason=f"provider_failure:{type(exc).__name__}:{exc}"[:4096],
                started_ns=started_ns,
            )
        publication = _success(
            self.descriptor,
            root,
            files,
            execution.findings,
            baseline if execution.measurement_complete else None,
            metrics=execution.metrics,
            relations=execution.relations,
            tool_version=self._version,
            started_ns=started_ns,
            stdout_bytes=execution.stdout_bytes,
            stderr_bytes=execution.stderr_bytes,
            process_invocations=execution.process_invocations,
            bytes_staged=sum(item.size for item in files),
            limitations=execution.limitations,
            input_signature_override=input_signature,
        )
        publication = replace(
            publication,
            coverage_complete=execution.measurement_complete,
        )
        return self._attach_execution(
            publication,
            configuration=self.deep_configuration,
            execution=execution,
        )


_DEEP_COVERAGE_LIMITATIONS = (
    "coverage_main_process_only",
    "subprocess_coverage_not_collected",
    "git_ignored_support_files_excluded_from_support_signature",
)


class PytestCoverageTrustedDeepProvider:
    """Execute the explicit canonical test selection with branch coverage."""

    def __init__(
        self,
        root: Path,
        deep_configuration: Mapping[str, object] | None,
        deep_configuration_signature: str | None,
        progress: ProgressCallback | None = None,
    ) -> None:
        payload, config = _validated_deep_configuration(
            deep_configuration,
            deep_configuration_signature,
        )
        self.root = root
        self.deep_configuration = payload
        self.config = config
        self.progress = progress
        self._version = _deep_tool_version()
        version = self._version or "unavailable"
        self._root_identity = external_root_identity(root)
        _raw_project, _parsed_project, project_digest = _project_configuration(root)
        self.descriptor = _provider_descriptor(
            provider_id=PYTEST_COVERAGE_PROVIDER_ID,
            provider_schema=DEEP_COVERAGE_PROVIDER_SCHEMA,
            tool_name="pytest+coverage",
            tool_version=version,
            profile="trusted-deep",
            source="external:pytest-coverage-trusted-deep",
            configuration_payload={
                "deep_configuration": payload,
                "deep_configuration_signature": config.configuration_signature,
                "branch_coverage": True,
                "dynamic_contexts": "pytest-nodeid-phase-v1",
                "subprocess_coverage": False,
                "autofix": False,
            },
            project_configuration_digest=project_digest,
            environment_signature=_environment_signature(
                tool_name="pytest+coverage",
                tool_version=version,
                home_directory=trusted_deep_home_directory(),
                path_value=os.environ.get("PATH"),
                pathext_value=os.environ.get("PATHEXT"),
            ),
            root_identity=self._root_identity,
            execution_strategy="canonical-root-bounded-sharded-pytest-coverage-v1",
            invalidation_strategy="dynamic_suite",
            memory=_DEEP_COVERAGE_MEMORY_BYTES,
            loads_project_configuration=True,
            scope="canonical-neocortex-pytest-selection-v1",
            limits=ProviderLimits(
                config.time_budget_seconds,
                _DEEP_COVERAGE_MEMORY_BYTES,
                RUFF_MAX_TOTAL_BYTES,
                _DEEP_COVERAGE_OUTPUT_BYTES,
                _DEEP_COVERAGE_FINDING_BOUND,
            ),
            loads_plugins=True,
            imports_content=True,
            executes_content=True,
            # Pytest content is trusted but not network-sandboxed.  Claiming false
            # here would overstate a guarantee the provider does not enforce.
            uses_network=True,
        )
        self._prepared_key: str | None = None
        self._prepared: DeepCoveragePreparedInput | None = None
        self._preparation_error_key: str | None = None
        self._preparation_error: Exception | None = None

    def tool_version(self) -> str | None:
        return self._version

    def _emit_progress(self, progress: DeepCoverageProgress) -> None:
        current = progress.current_shard
        shard_label = "-" if current is None else f"{current + 1}/{progress.total_shards}"
        emit_progress(
            self.progress,
            ProgressEvent(
                operation="code",
                phase="trusted-deep-coverage",
                description=f"Coverage trusted-deep · shard {shard_label}",
                completed=progress.completed_shards,
                total=progress.total_shards,
                unit="shards",
                finished=(
                    progress.phase in {"shard_completed", "shard_reused"}
                    and progress.completed_shards == progress.total_shards
                ),
                metrics=(
                    ProgressMetric("status", progress.phase),
                    ProgressMetric("tests", progress.selected_tests),
                    ProgressMetric("reused", progress.reused_shards),
                    ProgressMetric("elapsed_seconds", progress.elapsed_seconds),
                    ProgressMetric(
                        "shard_seconds",
                        "-"
                        if progress.shard_duration_seconds is None
                        else progress.shard_duration_seconds,
                    ),
                ),
            ),
        )

    def _prepare(
        self,
        files: Sequence[ExternalEvidenceFile],
    ) -> tuple[DeepCoveragePreparedInput, bool]:
        key = external_input_signature(files)
        if self._prepared_key == key and self._prepared is not None:
            return self._prepared, True
        if self._preparation_error_key == key and self._preparation_error is not None:
            raise self._preparation_error
        try:
            prepared = prepare_deep_coverage_input(
                self.root,
                files,
                self.config,
                environment=_controlled_environment(),
            )
        except Exception as exc:
            self._preparation_error_key = key
            self._preparation_error = exc
            raise
        self._prepared_key = key
        self._prepared = prepared
        self._preparation_error_key = None
        self._preparation_error = None
        return prepared, False

    def baseline_input_signature(
        self,
        files: Sequence[ExternalEvidenceFile],
    ) -> str:
        """Return the exact replay key, caching its measured preflight."""

        prepared, _cached = self._prepare(files)
        return prepared.publication_input_signature

    def _attach_deep_contract(
        self,
        publication: ExternalProviderPublication,
        *,
        prepared: DeepCoveragePreparedInput | None,
        execution: DeepCoverageExecution | None,
    ) -> ExternalProviderPublication:
        counters = dict(publication.counters)
        if prepared is not None:
            counters.update(
                {
                    "support_files_verified": prepared.support_files_verified,
                    "support_bytes_verified": prepared.support_bytes_verified,
                    "preparation_milliseconds": prepared.preparation_milliseconds,
                    "bytes_read": max(
                        counters.get("bytes_read", 0),
                        prepared.support_bytes_verified,
                    ),
                }
            )
            if execution is None:
                counters.update(
                    {
                        "process_invocations": max(
                            counters.get("process_invocations", 0),
                            prepared.process_invocations,
                        ),
                        "stdout_bytes": max(
                            counters.get("stdout_bytes", 0),
                            prepared.stdout_bytes,
                        ),
                        "stderr_bytes": max(
                            counters.get("stderr_bytes", 0),
                            prepared.stderr_bytes,
                        ),
                        "wall_milliseconds": max(
                            counters.get("wall_milliseconds", 0),
                            prepared.preparation_milliseconds,
                        ),
                    }
                )
        if execution is not None:
            counters.update({name: int(value) for name, value in execution.counters.items()})
        provenance = dict(publication.publication.provenance)
        provenance["deep_configuration"] = {
            "payload": dict(self.deep_configuration),
            "signature": self.config.configuration_signature,
        }
        deep_execution: dict[str, object] = {
            "suite_selection": self.config.suite_selection,
            "content_executed": publication.execution != "cache_replay",
            "whole_publication_replay": publication.execution == "cache_replay",
        }
        if prepared is not None:
            deep_execution.update(
                {
                    "code_input_signature": prepared.code_input_signature,
                    "support_signature": prepared.support_signature,
                    "publication_input_signature": prepared.publication_input_signature,
                    "support_files_verified": prepared.support_files_verified,
                    "support_bytes_verified": prepared.support_bytes_verified,
                    "preparation_milliseconds": prepared.preparation_milliseconds,
                    "preparation_process_invocations": prepared.process_invocations,
                }
            )
        if execution is not None:
            deep_execution.update(
                {
                    "measurement_complete": execution.measurement_complete,
                    "suite_signature": execution.suite_signature,
                    "measurement_scope_signature": execution.measurement_scope_signature,
                }
            )
        provenance["deep_execution"] = deep_execution
        inner = replace(publication.publication, provenance=provenance)
        return replace(publication, publication=inner, counters=counters)

    def run(
        self,
        root: Path,
        files: Sequence[ExternalEvidenceFile],
        *,
        baseline: ExternalProviderBaseline | None,
        scratch_root: Path,
    ) -> ExternalProviderPublication:
        started_ns = time.time_ns()
        if self._version is None:
            return self._attach_deep_contract(
                _failure(
                    self.descriptor,
                    root,
                    files,
                    tool_version="unavailable",
                    status="unavailable",
                    reason="pytest_or_coverage_unavailable",
                    started_ns=started_ns,
                ),
                prepared=None,
                execution=None,
            )
        prepared: DeepCoveragePreparedInput | None = None
        try:
            if external_root_identity(root) != self._root_identity:
                raise ValueError("trusted-deep root changed after provider construction")
            prepared, was_cached = self._prepare(files)
            if was_cached:
                started_ns -= prepared.preparation_milliseconds * 1_000_000
        except subprocess.TimeoutExpired:
            return self._attach_deep_contract(
                _failure(
                    self.descriptor,
                    root,
                    files,
                    tool_version=self._version,
                    status="timeout",
                    reason="deep_coverage_preparation_timeout",
                    started_ns=started_ns,
                ),
                prepared=prepared,
                execution=None,
            )
        except (OSError, RuntimeError, TypeError, ValueError, SubprocessOutputLimitError) as exc:
            return self._attach_deep_contract(
                _failure(
                    self.descriptor,
                    root,
                    files,
                    tool_version=self._version,
                    status="failed",
                    reason=f"deep_coverage_preparation_failed:{type(exc).__name__}:{exc}"[:4096],
                    started_ns=started_ns,
                ),
                prepared=prepared,
                execution=None,
            )
        assert prepared is not None
        if _baseline_allows_exact_replay(
            baseline,
            prepared.publication_input_signature,
        ):
            assert baseline is not None
            replay = _exact_replay(
                self.descriptor,
                root,
                files,
                baseline,
                limitations=_DEEP_COVERAGE_LIMITATIONS,
                input_signature_override=prepared.publication_input_signature,
            )
            return self._attach_deep_contract(
                replay,
                prepared=prepared,
                execution=None,
            )
        try:
            staging_parent = _validated_staging_parent(root, scratch_root)
            durable_identity = external_signature(
                "deep-coverage-scratch-v2",
                {
                    "root_identity": external_root_identity(root),
                    "configuration_signature": self.config.configuration_signature,
                },
            ).rsplit(":", 1)[-1]
            # Keep the unpublished internal scratch layout deliberately short.  Pytest
            # adds node-id-derived directories below its basetemp and nested Git
            # fixtures must still fit the traditional Windows path budget.
            durable_scratch = _durable_provider_scratch(
                staging_parent,
                namespace=_DEEP_COVERAGE_DURABLE_NAMESPACE,
                identity=durable_identity,
                label="trusted-deep durable",
            )
            staged = {os.path.normcase(os.path.abspath(item.path)): item for item in files}
            if len(staged) != len(files):
                raise ValueError("trusted-deep input paths are duplicated")
            with _deep_coverage_runtime(
                root=root,
                audit_lab_root=staging_parent,
            ) as runtime_container:
                execution = execute_pytest_coverage(
                    runtime_container,
                    staged,
                    _controlled_environment(),
                    trusted_root=root,
                    scratch_root=durable_scratch,
                    config=self.config,
                    prepared_input=prepared,
                    progress=self._emit_progress,
                )
        except subprocess.TimeoutExpired:
            return self._attach_deep_contract(
                _failure(
                    self.descriptor,
                    root,
                    files,
                    tool_version=self._version,
                    status="timeout",
                    reason="provider_timeout",
                    started_ns=started_ns,
                ),
                prepared=prepared,
                execution=None,
            )
        except (OSError, RuntimeError, TypeError, ValueError, SubprocessOutputLimitError) as exc:
            return self._attach_deep_contract(
                _failure(
                    self.descriptor,
                    root,
                    files,
                    tool_version=self._version,
                    status="failed",
                    reason=f"provider_failure:{type(exc).__name__}:{exc}"[:4096],
                    started_ns=started_ns,
                ),
                prepared=prepared,
                execution=None,
            )
        publication = _success(
            self.descriptor,
            root,
            files,
            execution.findings,
            baseline if execution.measurement_complete else None,
            metrics=execution.metrics,
            relations=execution.relations,
            tool_version=self._version,
            started_ns=started_ns,
            stdout_bytes=execution.stdout_bytes,
            stderr_bytes=execution.stderr_bytes,
            process_invocations=execution.process_invocations,
            bytes_staged=0,
            limitations=execution.limitations,
            input_signature_override=prepared.publication_input_signature,
        )
        publication = replace(
            publication,
            coverage_complete=execution.measurement_complete,
        )
        return self._attach_deep_contract(
            publication,
            prepared=prepared,
            execution=execution,
        )


def providers_for_profile(
    profile: AnalysisProfile,
    root: Path,
    *,
    deep_configuration: Mapping[str, object] | None = None,
    deep_configuration_signature: str | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[ExternalEvidenceProvider, ...]:
    if profile != "trusted-deep" and (
        deep_configuration is not None or deep_configuration_signature is not None
    ):
        raise ValueError("deep provider configuration requires trusted-deep")
    if profile == "protected":
        return (RuffProtectedBasicProvider(root),)
    if profile == "trusted-static":
        return (
            RuffProtectedBasicProvider(root),
            RuffTrustedProjectProvider(root),
            MypyTrustedProjectProvider(root),
            PyrightTrustedProjectProvider(root),
            SemgrepNeocortexInvariantsProvider(root),
            DeptryProjectDependenciesProvider(root),
            PipAuditKnownVulnerabilitiesProvider(root),
            InstalledPackageInventoryProvider(root),
            VultureUnusedStaticProvider(root),
            RuffAnalyzeImportsProvider(root),
            GrimpArchitectureProvider(root),
            ComplexipyCognitiveProvider(root),
            GitHistoryLocalProvider(root),
        )
    if profile == "trusted-deep":
        static = providers_for_profile("trusted-static", root)
        return (
            *static,
            PytestCoverageTrustedDeepProvider(
                root,
                deep_configuration,
                deep_configuration_signature,
                progress,
            ),
            CosmicRayFocalMutationProvider(
                root,
                deep_configuration,
                deep_configuration_signature,
            ),
        )
    raise ValueError("external analysis profile is unsupported")


__all__ = [
    "COMPLEXIPY_COGNITIVE_PROVIDER_ID",
    "COSMIC_RAY_MUTATION_PROVIDER_ID",
    "DEPTRY_PROVIDER_ID",
    "GIT_HISTORY_PROVIDER_ID",
    "GRIMP_ARCHITECTURE_PROVIDER_ID",
    "INSTALLED_PACKAGE_PROVIDER_ID",
    "MYPY_PROVIDER_ID",
    "PIP_AUDIT_PROVIDER_ID",
    "PYRIGHT_PROVIDER_ID",
    "PYTEST_COVERAGE_PROVIDER_ID",
    "RUFF_ANALYZE_PROVIDER_ID",
    "RUFF_PROTECTED_PROVIDER_ID",
    "RUFF_TRUSTED_PROVIDER_ID",
    "SEMGREP_INVARIANTS_PROVIDER_ID",
    "VULTURE_UNUSED_PROVIDER_ID",
    "ComplexipyCognitiveProvider",
    "CosmicRayFocalMutationProvider",
    "DeptryProjectDependenciesProvider",
    "GitHistoryLocalProvider",
    "GrimpArchitectureProvider",
    "InstalledPackageInventoryProvider",
    "MypyTrustedProjectProvider",
    "PipAuditKnownVulnerabilitiesProvider",
    "PyrightTrustedProjectProvider",
    "PytestCoverageTrustedDeepProvider",
    "RuffAnalyzeImportsProvider",
    "RuffProtectedBasicProvider",
    "RuffTrustedProjectProvider",
    "SemgrepNeocortexInvariantsProvider",
    "VultureUnusedStaticProvider",
    "provider_tool_versions",
    "providers_for_profile",
]
