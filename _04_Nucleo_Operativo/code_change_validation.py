"""Canonical local Linux validation for one NeoCortex source change.

This module turns the existing Code publication, trusted-deep provider and
local quality gates into one fail-closed decision surface.  It deliberately
does not own Git mutation, a push, release promotion or corpus mutation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import venv
import xxhash
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast

from neocortex import pip_bootstrap

from .app_paths import self_analysis_data_directory, source_repository_directory
from .code_analysis_epistemics import (
    AnalysisQuestionEvaluation,
    AnalysisQuestionSpec,
    analysis_question_spec_fingerprint,
)
from .code_architecture_questions import ARCHITECTURE_CONTRACT_QUESTION
from .code_change_evolution_analysis import CODE_SCHEMA_EVOLUTION_QUESTION
from .code_route_capability_analysis import ROUTE_CAPABILITY_QUESTION
from .code_security_dependency_questions import (
    DEPENDENCY_EVIDENCE_QUESTION,
    SECURITY_EVIDENCE_QUESTION,
)
from .code_state_interaction_analysis import WORKFLOW_SQL_QUESTION
from .code_state_projection_analysis import TEXT_SEMANTIC_PROJECTION_QUESTION
from .code_validation_resources import (
    CODE_VALIDATION_RESOURCE_SCHEMA,
    current_code_validation_resource_admission,
    parse_code_validation_resource_admission,
)
from .code_review import review_code_state
from .code_schema import readonly_code_database, validate_code_schema
from .external_evidence_providers import (
    COMPLEXIPY_COGNITIVE_PROVIDER_ID,
    COSMIC_RAY_MUTATION_PROVIDER_ID,
    DEPTRY_PROVIDER_ID,
    GIT_HISTORY_PROVIDER_ID,
    GRIMP_ARCHITECTURE_PROVIDER_ID,
    INSTALLED_PACKAGE_PROVIDER_ID,
    MYPY_PROVIDER_ID,
    PIP_AUDIT_PROVIDER_ID,
    PYRIGHT_PROVIDER_ID,
    PYTEST_COVERAGE_PROVIDER_ID,
    RUFF_ANALYZE_PROVIDER_ID,
    RUFF_PROTECTED_PROVIDER_ID,
    RUFF_TRUSTED_PROVIDER_ID,
    SEMGREP_INVARIANTS_PROVIDER_ID,
    VULTURE_UNUSED_PROVIDER_ID,
)
from .semantic_models import canonical_json


CODE_CHANGE_VALIDATION_SCHEMA = "neocortex.code-change-validation/v3"
CODE_CHANGE_VALIDATION_POLICY = "local-linux-diff-aware-validation-v2"
MAX_CHANGED_PATHS = 2_000
MAX_SELECTED_TEST_FILES = 2_000
MAX_DEPENDENCY_DEPTH = 8
MAX_COMMAND_OUTPUT_BYTES = 32 * 1024
_COMMAND_HEARTBEAT_SECONDS = 30.0
_TEST_PATH = re.compile(r"^tests/(?:.*/)?test_[^/]+\.py$")
_PYTHON_SOURCE_ROOTS = frozenset(
    {
        "Orquestador.py",
        "_01_Enumeracion",
        "_02_Deduplicacion",
        "_03_Progreso",
        "_04_Nucleo_Operativo",
        "_05_Interfaz",
        "neocortex",
    }
)
_PYTHON_SOURCE_PREFIXES = frozenset(
    {
        "_01_Enumeracion",
        "_02_Deduplicacion",
        "_03_Progreso",
        "_04_Nucleo_Operativo",
        "_05_Interfaz",
        "neocortex",
        "tools",
    }
)

_TRUSTED_DEEP_REQUIRED_PROVIDER_IDS = frozenset(
    {
        COMPLEXIPY_COGNITIVE_PROVIDER_ID,
        DEPTRY_PROVIDER_ID,
        GIT_HISTORY_PROVIDER_ID,
        GRIMP_ARCHITECTURE_PROVIDER_ID,
        INSTALLED_PACKAGE_PROVIDER_ID,
        MYPY_PROVIDER_ID,
        PIP_AUDIT_PROVIDER_ID,
        PYRIGHT_PROVIDER_ID,
        PYTEST_COVERAGE_PROVIDER_ID,
        RUFF_ANALYZE_PROVIDER_ID,
        RUFF_PROTECTED_PROVIDER_ID,
        RUFF_TRUSTED_PROVIDER_ID,
        SEMGREP_INVARIANTS_PROVIDER_ID,
        VULTURE_UNUSED_PROVIDER_ID,
    }
)
_OPTIONAL_PROVIDER_ABSTENTIONS = {
    COSMIC_RAY_MUTATION_PROVIDER_ID: frozenset(
        {
            "mutation_target_not_declared",
            "mutation_not_declared_in_legacy_deep_configuration",
            "provider_abstained:mutation_target_not_declared",
            "provider_abstained:mutation_not_declared_in_legacy_deep_configuration",
        }
    )
}
_FULL_SUITE_BOUNDARIES = frozenset(
    {
        "constraints.txt",
        "MANIFEST.in",
        "pyproject.toml",
        "tools/quality_gate_coverage_baseline.json",
        "tools/quality_gate_static_baseline.json",
        "tools/quality_gate_supply_policy.json",
    }
)
_SOURCE_BOUNDARY_TESTS = {
    "tools/quality_gate.py": frozenset(
        {
            "tests/test_code_change_validation.py",
            "tests/test_packaging_entrypoint.py",
            "tests/test_quality_gate.py",
            "tests/test_release_artifacts.py",
            "tests/test_release_linux.py",
        }
    ),
    "_04_Nucleo_Operativo/code_schema.py": frozenset(
        {
            "tests/test_code_change_evolution_analysis.py",
            "tests/test_code_experiment_store.py",
            "tests/test_code_intelligence.py",
            "tests/test_code_publication_diff.py",
            "tests/test_code_schema_migration_v1_v2.py",
            "tests/test_external_provider_schema_v4.py",
            "tests/test_framework_code_path_collation.py",
        }
    )
}
_SUPPLY_CHAIN_BOUNDARIES = frozenset(
    {
        "constraints.txt",
        "MANIFEST.in",
        "pyproject.toml",
        "tools/release_linux.py",
        "tools/quality_gate_supply_policy.json",
    }
)
_REGISTERED_SCENARIO_TESTS = frozenset(
    {
        "tests/test_code_public_route_experiments.py",
        "tests/test_code_review_epistemics.py",
        "tests/test_code_state_interaction_analysis.py",
        "tests/test_code_state_projection_analysis.py",
        "tests/test_code_state_topology_analysis.py",
        "tests/test_semantic_text_staging_session.py",
        "tests/test_text_derivation_route.py",
    }
)
_CANONICAL_DEEP_SHARD_SIZE = 50

_EXPERIMENT_CONTROL_PLANE_PATHS = frozenset(
    {
        "_04_Nucleo_Operativo/cli_code.py",
        "_04_Nucleo_Operativo/code_analysis_epistemics.py",
        "_04_Nucleo_Operativo/code_change_validation.py",
        "_04_Nucleo_Operativo/code_experiment_executor.py",
        "_04_Nucleo_Operativo/code_experiment_planner.py",
        "_04_Nucleo_Operativo/code_experiment_store.py",
        "_04_Nucleo_Operativo/code_invariant_contracts.py",
        "_04_Nucleo_Operativo/code_review.py",
        "_04_Nucleo_Operativo/code_review_models.py",
        "_04_Nucleo_Operativo/code_review_serialization.py",
        "_04_Nucleo_Operativo/code_technical_verification.py",
        "_04_Nucleo_Operativo/code_validation_resources.py",
        "tests/test_code_change_validation.py",
        "tests/test_code_experiment_executor.py",
        "tests/test_code_experiment_planner.py",
        "tests/test_code_experiment_store.py",
        "tests/test_code_technical_verification.py",
        "tests/test_code_validation_resources.py",
    }
)

# Windows is preserved as historical source but is not an active validation
# target for Víctor's personal Linux installation.  Keep this list narrow and
# semantic: portable tests that merely exercise a Windows-shaped fixture remain
# eligible; only suites whose product under test is the retired Windows/NTFS
# runtime are excluded.
_LINUX_EXCLUDED_TEST_MODULES = frozenset(
    {
        "tests/test_ntfs_usn.py",
        "tests/test_release_windows.py",
        "tests/test_release_windows_ntfs.py",
        "tests/test_ui_elevation.py",
        "tests/test_windows_handle_mutation.py",
    }
)


class ChangeValidationError(RuntimeError):
    """The local validation boundary cannot prove a usable verdict."""


class _CommandRunner(Protocol):
    def __call__(
        self,
        arguments: Sequence[str | os.PathLike[str]],
        *,
        cwd: Path,
        timeout: float,
        environment: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class GitChangeSnapshot:
    head_sha: str
    baseline: str
    changed_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    staged_paths: tuple[str, ...]
    unstaged_paths: tuple[str, ...]
    content_digest: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{40}", self.head_sha) is None:
            raise ValueError("Git change head SHA is invalid")
        if re.fullmatch(r"[0-9a-f]{40}", self.baseline) is None:
            raise ValueError("Git change baseline SHA is invalid")
        for collection in (
            self.changed_paths,
            self.untracked_paths,
            self.staged_paths,
            self.unstaged_paths,
        ):
            if (
                tuple(sorted(set(collection), key=lambda item: (item.casefold(), item)))
                != collection
            ):
                raise ValueError("Git change paths must be unique and canonical")
            if any(
                PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts
                or not PurePosixPath(path).parts
                for path in collection
            ):
                raise ValueError("Git change contains an unsafe path")
        if any(path not in self.changed_paths for path in self.untracked_paths):
            raise ValueError("Git untracked paths must belong to the change")
        if any(
            path not in self.changed_paths for path in (*self.staged_paths, *self.unstaged_paths)
        ):
            raise ValueError("Git tracked paths must belong to the change")
        if re.fullmatch(r"[0-9a-f]{64}", self.content_digest) is None:
            raise ValueError("Git change content digest is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class AffectedTestSelection:
    strategy: Literal["none", "affected", "full"]
    selectors: tuple[str, ...]
    direct_tests: tuple[str, ...]
    dependency_tests: tuple[str, ...]
    convention_tests: tuple[str, ...]
    uncovered_sources: tuple[str, ...]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.strategy not in {"none", "affected", "full"}:
            raise ValueError("affected-test strategy is invalid")
        if len(self.selectors) > MAX_SELECTED_TEST_FILES:
            raise ValueError("affected-test selector bound exceeded")
        if tuple(sorted(set(self.selectors), key=lambda item: (item.casefold(), item))) != (
            self.selectors
        ):
            raise ValueError("affected-test selectors must be unique and canonical")
        if self.strategy == "none" and self.selectors:
            raise ValueError("empty selection strategy cannot carry selectors")
        if self.strategy in {"affected", "full"} and not self.selectors:
            raise ValueError("non-empty selection strategy requires selectors")
        if any(
            not _TEST_PATH.fullmatch(selector) or selector in _LINUX_EXCLUDED_TEST_MODULES
            for selector in self.selectors
        ):
            raise ValueError("affected-test selector is outside the Linux inventory")
        for label, collection in (
            ("direct", self.direct_tests),
            ("dependency", self.dependency_tests),
            ("convention", self.convention_tests),
        ):
            if (
                tuple(sorted(set(collection), key=lambda item: (item.casefold(), item)))
                != collection
            ):
                raise ValueError(f"affected-test {label} tests must be unique and canonical")
            if any(item not in self.selectors for item in collection):
                raise ValueError(f"affected-test {label} tests must belong to selectors")
        if (
            tuple(sorted(set(self.uncovered_sources), key=lambda item: (item.casefold(), item)))
            != self.uncovered_sources
        ):
            raise ValueError("affected-test uncovered sources must be unique and canonical")
        if any(_module_for_source(item) is None for item in self.uncovered_sources):
            raise ValueError("affected-test uncovered source is not a Python source path")
        if tuple(
            sorted(set(self.reasons), key=lambda item: (item.casefold(), item))
        ) != self.reasons or any(not item for item in self.reasons):
            raise ValueError("affected-test reasons must be non-empty, unique and canonical")


@dataclass(frozen=True, slots=True)
class _ValidationQuestionScope:
    """Versioned binding from a Git surface to one acceptance-relevant question."""

    scope_id: str
    spec: AnalysisQuestionSpec
    subject_prefix: str
    template_id: str | None
    changed_paths: frozenset[str]
    changed_prefixes: tuple[str, ...]
    test_selectors: frozenset[str]
    include_experiment_control_plane: bool = False

    def __post_init__(self) -> None:
        if not self.scope_id:
            raise ValueError("validation question scope identity is invalid")
        if not isinstance(self.spec, AnalysisQuestionSpec):
            raise ValueError("validation question scope spec is invalid")
        if self.template_id is not None and not self.template_id:
            raise ValueError("validation question scope template is invalid")
        if any(
            not item or item.startswith("/") or ".." in PurePosixPath(item).parts
            for item in self.changed_paths
        ):
            raise ValueError("validation question scope path is invalid")
        if any(
            not item or item.startswith("/") or ".." in PurePosixPath(item).parts
            for item in self.changed_prefixes
        ):
            raise ValueError("validation question scope prefix is invalid")
        if any(not _TEST_PATH.fullmatch(item) for item in self.test_selectors):
            raise ValueError("validation question scope selector is invalid")


@dataclass(frozen=True, slots=True)
class ValidationGate:
    gate_id: str
    status: Literal["passed", "failed", "abstained", "not_required"]
    reason: str
    duration_ms: int
    command: tuple[str, ...]
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.status not in {"passed", "failed", "abstained", "not_required"}:
            raise ValueError("validation gate status is invalid")
        if not self.gate_id or not self.reason or self.duration_ms < 0:
            raise ValueError("validation gate identity is incomplete")
        if not isinstance(self.evidence, Mapping):
            raise ValueError("validation gate evidence is invalid")


@dataclass(frozen=True, slots=True)
class CodeChangeValidationResult:
    status: Literal["passed", "failed", "abstained"]
    reason: str | None
    policy_id: str
    source_root: str
    state_directory: str
    git: GitChangeSnapshot
    selection: AffectedTestSelection
    gates: tuple[ValidationGate, ...]
    experiment_proposals: tuple[str, ...]
    executable_experiments: tuple[str, ...]
    experiment_receipts: tuple[Mapping[str, object], ...]
    resource_boundary: Mapping[str, object] | None
    source_unchanged: bool
    authority: Literal["validation"] = "validation"
    mutation_authority: Literal[False] = False
    digest: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"passed", "failed", "abstained"}:
            raise ValueError("change validation status is invalid")
        if self.status == "passed" and self.reason is not None:
            raise ValueError("passed validation cannot carry a failure reason")
        if self.status != "passed" and not self.reason:
            raise ValueError("non-passing validation requires a reason")
        if not self.gates:
            raise ValueError("change validation requires gates")
        gate_ids = tuple(item.gate_id for item in self.gates)
        if len(set(gate_ids)) != len(gate_ids):
            raise ValueError("change validation gate IDs must be unique")
        failed = tuple(item for item in self.gates if item.status == "failed")
        abstained = tuple(item for item in self.gates if item.status == "abstained")
        expected_status = "failed" if failed else "abstained" if abstained else "passed"
        expected_reason = (
            f"failed_gate:{failed[0].gate_id}"
            if failed
            else f"abstained_gate:{abstained[0].gate_id}"
            if abstained
            else None
        )
        if self.status != expected_status or self.reason != expected_reason:
            raise ValueError("change validation verdict does not match its gates")
        if gate_ids[-1] not in {"source_snapshot_unchanged", "source_change_present"}:
            raise ValueError("change validation lacks a terminal source fence")
        source_gate = self.gates[-1]
        source_gate_unchanged = (
            source_gate.reason == "no_source_change"
            if source_gate.gate_id == "source_change_present"
            else source_gate.status == "passed" and source_gate.reason == "source_unchanged"
        )
        if self.source_unchanged != source_gate_unchanged:
            raise ValueError("change validation source fence is inconsistent")
        if any(not isinstance(item, Mapping) for item in self.experiment_receipts):
            raise ValueError("change validation experiment receipts are invalid")
        if self.resource_boundary is not None:
            if not isinstance(self.resource_boundary, Mapping):
                raise ValueError("change validation resource boundary is invalid")
            if self.resource_boundary.get("schema") != CODE_VALIDATION_RESOURCE_SCHEMA:
                raise ValueError("change validation resource boundary schema is invalid")
            try:
                parsed_boundary = parse_code_validation_resource_admission(self.resource_boundary)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("change validation resource boundary is invalid") from exc
            if parsed_boundary.as_payload() != dict(self.resource_boundary):
                raise ValueError("change validation resource boundary fields are invalid")
        if self.authority != "validation" or self.mutation_authority:
            raise ValueError("change validation cannot authorize mutation")
        if not self.digest.startswith("sha256:") or len(self.digest) != 71:
            raise ValueError("change validation digest is malformed")
        if self.digest != _result_digest(self):
            raise ValueError("change validation digest is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": CODE_CHANGE_VALIDATION_SCHEMA, **asdict(self)}


def _result_digest(result: CodeChangeValidationResult) -> str:
    payload = asdict(result)
    payload.pop("digest", None)
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _default_runner(
    arguments: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    timeout: float,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        tuple(os.fspath(item) for item in arguments),
        cwd=cwd,
        env=None if environment is None else dict(environment),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _bounded_output(completed: subprocess.CompletedProcess[str]) -> str:
    value = (completed.stdout + "\n" + completed.stderr).strip()
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_COMMAND_OUTPUT_BYTES:
        return value
    return encoded[-MAX_COMMAND_OUTPUT_BYTES:].decode("utf-8", errors="replace")


def _run_text(
    runner: _CommandRunner,
    root: Path,
    arguments: Sequence[str | os.PathLike[str]],
    *,
    timeout: float = 60,
    allowed: frozenset[int] = frozenset({0}),
) -> str:
    completed = runner(arguments, cwd=root, timeout=timeout, environment=None)
    if completed.returncode not in allowed:
        raise ChangeValidationError(
            f"command_failed:{os.fspath(arguments[0])}:{completed.returncode}:"
            f"{_bounded_output(completed)}"
        )
    return completed.stdout


def _git_paths(raw: str) -> tuple[str, ...]:
    values = tuple(item for item in raw.split("\0") if item)
    normalized: list[str] = []
    for value in values:
        selected = value.replace("\\", "/")
        path = PurePosixPath(selected)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ChangeValidationError("git_reported_unsafe_path")
        normalized.append(path.as_posix())
    return tuple(sorted(set(normalized), key=lambda item: (item.casefold(), item)))


def _path_digest(root: Path, paths: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        path = root.joinpath(*PurePosixPath(relative).parts)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            digest.update(b"deleted\n")
            continue
        if path.is_symlink() or not path.is_file():
            digest.update(f"nonregular:{metadata.st_mode}\n".encode("ascii"))
            continue
        digest.update(str(metadata.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\n")
    return digest.hexdigest()


def capture_git_change(
    root: Path,
    *,
    baseline: str = "HEAD",
    runner: _CommandRunner = _default_runner,
) -> GitChangeSnapshot:
    """Capture tracked and untracked change identity without mutating Git."""

    source = Path(root).resolve(strict=True)
    head = _run_text(runner, source, ("git", "rev-parse", "HEAD")).strip()
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise ChangeValidationError("git_head_is_malformed")
    baseline_sha = _run_text(
        runner,
        source,
        ("git", "rev-parse", "--verify", f"{baseline}^{{commit}}"),
    ).strip()
    if re.fullmatch(r"[0-9a-f]{40}", baseline_sha) is None:
        raise ChangeValidationError("git_baseline_is_malformed")
    staged = _git_paths(
        _run_text(
            runner,
            source,
            (
                "git",
                "diff",
                "--cached",
                "--no-renames",
                "--name-only",
                "-z",
                "--diff-filter=ACDMRTUXB",
            ),
        )
    )
    unstaged = _git_paths(
        _run_text(
            runner,
            source,
            (
                "git",
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                "--diff-filter=ACDMRTUXB",
            ),
        )
    )
    committed = _git_paths(
        _run_text(
            runner,
            source,
            (
                "git",
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                "--diff-filter=ACDMRTUXB",
                f"{baseline_sha}..HEAD",
            ),
        )
    )
    untracked = _git_paths(
        _run_text(
            runner,
            source,
            ("git", "ls-files", "--others", "--exclude-standard", "-z"),
        )
    )
    changed = tuple(
        sorted(
            {*committed, *staged, *unstaged, *untracked},
            key=lambda item: (item.casefold(), item),
        )
    )
    if len(changed) > MAX_CHANGED_PATHS:
        raise ChangeValidationError("changed_path_bound_exceeded")
    return GitChangeSnapshot(
        head,
        baseline_sha,
        changed,
        untracked,
        staged,
        unstaged,
        _path_digest(source, changed),
    )


def _module_for_source(relative: str) -> str | None:
    if relative == "Orquestador.py":
        return "Orquestador"
    path = PurePosixPath(relative)
    if path.suffix.casefold() != ".py" or not path.parts:
        return None
    if path.parts[0] not in _PYTHON_SOURCE_PREFIXES or (
        path.parts[0] == "tools" and path.name.startswith("test_")
    ):
        return None
    parts = path.with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def _convention_candidates(root: Path, relative: str) -> tuple[str, ...]:
    module = _module_for_source(relative)
    if module is None:
        return ()
    leaf = module.rsplit(".", 1)[-1]
    normalized = leaf.removeprefix("_")
    candidates = (
        root / "tests" / f"test_{normalized}.py",
        root / "tests" / f"test_{normalized}_contracts.py",
        root / "tests" / f"test_{normalized}_service.py",
    )
    return tuple(
        path.relative_to(root).as_posix()
        for path in candidates
        if path.is_file()
        and not path.is_symlink()
        and path.relative_to(root).as_posix() not in _LINUX_EXCLUDED_TEST_MODULES
    )


def _source_boundary_tests(root: Path, relative: str) -> tuple[str, ...]:
    """Return the bounded compatibility matrix declared for a source boundary."""

    return tuple(
        sorted(
            (
                test
                for test in _SOURCE_BOUNDARY_TESTS.get(relative, ())
                if (root / test).is_file() and not (root / test).is_symlink()
            ),
            key=lambda item: (item.casefold(), item),
        )
    )


def _linux_test_files(root: Path) -> tuple[str, ...]:
    """Return the declared Linux test inventory without retired platform suites."""

    tests = root / "tests"
    paths = {
        path.relative_to(root).as_posix()
        for pattern in ("test_*.py", "*_test.py")
        for path in tests.rglob(pattern)
        if path.is_file() and not path.is_symlink()
    }
    return tuple(
        sorted(
            paths - _LINUX_EXCLUDED_TEST_MODULES,
            key=lambda item: (item.casefold(), item),
        )
    )


def _global_change_fallback_tests(root: Path) -> tuple[str, ...]:
    """Return bounded public-boundary evidence for an unresolved changed source."""

    candidates = {
        "tests/test_code_cli.py",
        "tests/test_cli_capabilities.py",
        "tests/test_cli_code_surface.py",
        "tests/test_cli_import_isolation.py",
        "tests/test_cli_lazy_leaf_imports.py",
        "tests/test_cli_operations_registry.py",
        "tests/test_packaging_entrypoint.py",
        "tests/test_quality_gate.py",
        *_REGISTERED_SCENARIO_TESTS,
    }
    return tuple(
        sorted(
            (item for item in candidates if (root / item).is_file()),
            key=lambda item: (item.casefold(), item),
        )
    )


def _current_import_graph(connection) -> dict[str, set[str]]:
    rows = connection.execute(
        """SELECT f.current_path AS source_path,tf.current_path AS target_path
        FROM dependencies d
        JOIN file_versions v ON v.version_id=d.version_id
        JOIN files f ON f.current_version_id=v.version_id
        JOIN file_versions tv ON tv.version_id=d.resolved_version_id
        JOIN files tf ON tf.current_version_id=tv.version_id
        WHERE f.status='current' AND tf.status='current'
        AND v.invalidated_ns IS NULL AND tv.invalidated_ns IS NULL
        AND d.confirmed=1 AND d.kind='python_import'"""
    ).fetchall()
    reverse: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        source = str(row["source_path"])
        target = str(row["target_path"])
        reverse[target].add(source)
    return reverse


def _absolute_by_relative(connection, root: Path) -> dict[str, str]:
    rows = connection.execute(
        """SELECT f.current_path FROM files f JOIN file_versions v
        ON v.version_id=f.current_version_id WHERE f.status='current'
        AND v.invalidated_ns IS NULL AND v.language='python'"""
    ).fetchall()
    result: dict[str, str] = {}
    for row in rows:
        absolute = Path(str(row["current_path"]))
        try:
            relative = absolute.relative_to(root).as_posix()
        except ValueError:
            continue
        result[relative] = str(absolute)
    return result


def _published_current_file_digests(connection, root: Path) -> dict[str, tuple[str, str]]:
    """Return exact published fingerprints keyed by canonical relative path."""

    rows = connection.execute(
        """SELECT f.current_path,v.raw_xxh3_128,v.raw_xxh3_64_guard
        FROM files f JOIN file_versions v ON v.version_id=f.current_version_id
        WHERE f.status='current' AND v.invalidated_ns IS NULL
        AND v.raw_xxh3_128 IS NOT NULL AND v.raw_xxh3_64_guard IS NOT NULL"""
    ).fetchall()
    result: dict[str, tuple[str, str]] = {}
    for row in rows:
        absolute = Path(str(row["current_path"]))
        try:
            relative = absolute.relative_to(root).as_posix()
        except ValueError:
            continue
        result[relative] = (str(row["raw_xxh3_128"]), str(row["raw_xxh3_64_guard"]))
    return result


def _unpublished_source_paths(
    root: Path,
    state_directory: Path,
    change: GitChangeSnapshot,
) -> tuple[str, ...]:
    """Return changed source files whose exact bytes are not in Code's current view."""

    database = Path(state_directory) / "code.sqlite3"
    if not database.is_file():
        return tuple(path for path in change.changed_paths if _module_for_source(path) is not None)
    try:
        with readonly_code_database(database) as connection:
            validate_code_schema(connection)
            published = _published_current_file_digests(connection, root)
    except (OSError, RuntimeError, ValueError):
        return tuple(path for path in change.changed_paths if _module_for_source(path) is not None)
    stale: list[str] = []
    for relative in change.changed_paths:
        if _module_for_source(relative) is None:
            continue
        path = root.joinpath(*PurePosixPath(relative).parts)
        if not path.is_file() or path.is_symlink():
            stale.append(relative)
            continue
        raw = path.read_bytes()
        expected = published.get(relative)
        observed = (
            xxhash.xxh3_128_hexdigest(raw),
            xxhash.xxh3_64_hexdigest(raw, seed=0x4E454F43),
        )
        if expected != observed:
            stale.append(relative)
    return tuple(stale)


def select_affected_tests(
    root: Path,
    state_directory: Path,
    change: GitChangeSnapshot,
) -> AffectedTestSelection:
    """Select tests by exact diff, Code import closure and conservative policy."""

    source = Path(root).resolve(strict=True)
    direct = tuple(
        path
        for path in change.changed_paths
        if _TEST_PATH.fullmatch(path) and path not in _LINUX_EXCLUDED_TEST_MODULES
    )
    production = tuple(
        path for path in change.changed_paths if _module_for_source(path) is not None
    )
    boundary = any(
        path in _FULL_SUITE_BOUNDARIES
        or path.startswith(
            (
                "tools/release_",
                "neocortex/platform_policy",
                "_04_Nucleo_Operativo/framework_schema",
            )
        )
        for path in change.changed_paths
    )
    reasons: list[str] = []
    if boundary:
        selectors = _linux_test_files(source)
        return AffectedTestSelection(
            strategy="full",
            selectors=selectors,
            direct_tests=direct,
            dependency_tests=(),
            convention_tests=(),
            uncovered_sources=(),
            reasons=("change_crosses_full_suite_boundary",),
        )
    dependency_tests: set[str] = set()
    sources_with_dependency_tests: set[str] = set()
    database = Path(state_directory) / "code.sqlite3"
    if database.is_file() and production:
        with readonly_code_database(database) as connection:
            validate_code_schema(connection)
            current = _absolute_by_relative(connection, source)
            reverse = _current_import_graph(connection)
        for production_path in production:
            initial = current.get(production_path)
            if initial is None:
                continue
            pending = deque(((initial, 0),))
            visited = {initial}
            selected_for_source: set[str] = set()
            while pending:
                target, depth = pending.popleft()
                if depth >= MAX_DEPENDENCY_DEPTH:
                    reasons.append("dependency_closure_depth_bound_reached")
                    continue
                for dependent in sorted(reverse.get(target, ())):
                    if dependent in visited:
                        continue
                    visited.add(dependent)
                    pending.append((dependent, depth + 1))
                    path = Path(dependent)
                    try:
                        relative = path.relative_to(source).as_posix()
                    except ValueError:
                        continue
                    if (
                        _TEST_PATH.fullmatch(relative)
                        and relative not in _LINUX_EXCLUDED_TEST_MODULES
                    ):
                        dependency_tests.add(relative)
                        selected_for_source.add(relative)
            if selected_for_source:
                sources_with_dependency_tests.add(production_path)
    elif production:
        reasons.append("published_import_graph_unavailable")
    convention = {
        test
        for path in production
        for test in (
            *_convention_candidates(source, path),
            *_source_boundary_tests(source, path),
        )
    }
    selected = tuple(
        sorted(
            {*direct, *dependency_tests, *convention},
            key=lambda item: (item.casefold(), item),
        )
    )
    if len(selected) > MAX_SELECTED_TEST_FILES:
        raise ChangeValidationError("affected_test_selection_bound_exceeded")
    uncovered = tuple(
        path
        for path in production
        if not _convention_candidates(source, path)
        and not _source_boundary_tests(source, path)
        and path not in sources_with_dependency_tests
    )
    if production and not selected:
        reasons.append("no_affected_test_evidence")
    elif uncovered:
        reasons.append("some_changed_sources_lack_affected_test_evidence")
    return AffectedTestSelection(
        strategy="affected" if selected else "none",
        selectors=selected,
        direct_tests=direct,
        dependency_tests=tuple(sorted(dependency_tests)),
        convention_tests=tuple(sorted(convention)),
        uncovered_sources=uncovered,
        reasons=tuple(sorted(set(reasons))),
    )


def _gate(
    gate_id: str,
    status: Literal["passed", "failed", "abstained", "not_required"],
    reason: str,
    started: int,
    command: Sequence[str | os.PathLike[str]],
    evidence: Mapping[str, object],
) -> ValidationGate:
    return ValidationGate(
        gate_id,
        status,
        reason,
        max(0, (time.monotonic_ns() - started) // 1_000_000),
        tuple(os.fspath(item) for item in command),
        dict(evidence),
    )


def _run_gate_command(
    gate_id: str,
    command: Sequence[str | os.PathLike[str]],
    *,
    root: Path,
    timeout: float,
    runner: _CommandRunner,
    environment: Mapping[str, str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> ValidationGate:
    started = time.monotonic_ns()
    stopped = threading.Event()

    def heartbeat() -> None:
        if progress is None:
            return
        while not stopped.wait(_COMMAND_HEARTBEAT_SECONDS):
            elapsed = max(0, (time.monotonic_ns() - started) // 1_000_000_000)
            progress(f"gate {gate_id} still running: elapsed_seconds={elapsed}")

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        completed = runner(command, cwd=root, timeout=timeout, environment=environment)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _gate(
            gate_id,
            "abstained",
            f"execution_unavailable:{type(exc).__name__}",
            started,
            command,
            {"error": str(exc)[:4096]},
        )
    finally:
        stopped.set()
        thread.join(timeout=1)
    output = _bounded_output(completed)
    return _gate(
        gate_id,
        "passed" if completed.returncode == 0 else "failed",
        "command_passed" if completed.returncode == 0 else f"exit_code:{completed.returncode}",
        started,
        command,
        {"exit_code": completed.returncode, "output": output},
    )


def _provider_failure(provider: object) -> bool:
    provider_id = str(getattr(provider, "provider_id", ""))
    status = str(getattr(provider, "status", ""))
    reason = getattr(provider, "reason", None)
    # Portable-finding deltas are deliberately advisory here.  Their identity
    # includes source coordinates and their nearest comparable publication can
    # legitimately predate the Git baseline, so a refactor can yield
    # added/resolved pairs without introducing a new diagnostic.  The
    # preceding ``static_no_regression`` gate is the canonical, versioned
    # path/rule/count enforcement boundary for Ruff, Mypy and Pyright.  This
    # review gate therefore decides only whether each provider was observed
    # successfully, not whether its historical delta happens to be zero.
    if status == "ready":
        return False
    allowed = _OPTIONAL_PROVIDER_ABSTENTIONS.get(provider_id, frozenset())
    return not (status == "abstained" and isinstance(reason, str) and reason in allowed)


def _effective_provider_projection_run(connection: Any, tool_run_id: int) -> int | None:
    row = connection.execute(
        """SELECT r.status,c.execution FROM external_tool_runs r
        JOIN external_run_contracts c USING(tool_run_id) WHERE r.tool_run_id=?""",
        (tool_run_id,),
    ).fetchone()
    if row is None:
        return None
    status = str(row["status"])
    execution = str(row["execution"])
    if status == "completed" and execution != "cache_replay":
        return tool_run_id
    if status != "skipped" or execution != "cache_replay":
        return None
    replay = connection.execute(
        "SELECT source_tool_run_id FROM external_run_replays WHERE tool_run_id=?",
        (tool_run_id,),
    ).fetchone()
    return None if replay is None else int(replay[0])


def _installed_versions(connection: Any, tool_run_id: int) -> dict[str, str] | None:
    effective = _effective_provider_projection_run(connection, tool_run_id)
    if effective is None:
        return None
    rows = connection.execute(
        """SELECT subject_key,value,metadata_json FROM external_metrics
        WHERE tool_run_id=? AND category='package_integrity'
        AND metric_name='distribution_present' ORDER BY subject_key LIMIT 2001""",
        (effective,),
    ).fetchall()
    if not rows or len(rows) > 2_000:
        return None
    versions: dict[str, str] = {}
    for row in rows:
        if float(row["value"]) != 1.0:
            return None
        try:
            metadata = json.loads(str(row["metadata_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(metadata, dict):
            return None
        name = metadata.get("normalized_name")
        version = metadata.get("installed_version")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            return None
        if str(row["subject_key"]) != f"package:{name}" or name in versions:
            return None
        versions[name] = version
    return versions


def _historical_pip_audit_fallback(
    state_directory: Path,
    *,
    analysis_run_id: int,
    change: GitChangeSnapshot | None,
) -> Mapping[str, object] | None:
    """Resolve one still-fresh audit only for an unchanged installed inventory."""

    if change is None or any(path in _SUPPLY_CHAIN_BOUNDARIES for path in change.changed_paths):
        return None
    database = Path(state_directory) / "code.sqlite3"
    if not database.is_file():
        return None
    try:
        with readonly_code_database(database) as connection:
            validate_code_schema(connection)
            current_inventory = connection.execute(
                """SELECT r.tool_run_id FROM external_tool_runs r
                JOIN external_run_contracts c USING(tool_run_id)
                WHERE r.analysis_run_id=? AND c.provider_id=?
                ORDER BY r.tool_run_id DESC LIMIT 1""",
                (analysis_run_id, INSTALLED_PACKAGE_PROVIDER_ID),
            ).fetchone()
            if current_inventory is None:
                return None
            audit = connection.execute(
                """SELECT r.tool_run_id,r.analysis_run_id,c.result_digest,m.value AS fresh_until
                FROM external_tool_runs r JOIN external_run_contracts c USING(tool_run_id)
                JOIN external_metrics m ON m.tool_run_id=r.tool_run_id
                WHERE c.provider_id=? AND r.status='completed'
                AND c.coverage_complete=1 AND c.result_digest IS NOT NULL
                AND m.subject_kind='project'
                AND m.subject_key='project:installed-environment'
                AND m.category='known_vulnerability'
                AND m.metric_name='audit_fresh_until_unix_seconds'
                AND m.unit='unix_seconds' AND m.value>=?
                ORDER BY r.tool_run_id DESC LIMIT 1""",
                (PIP_AUDIT_PROVIDER_ID, time.time()),
            ).fetchone()
            if audit is None:
                return None
            historical_inventory = connection.execute(
                """SELECT r.tool_run_id FROM external_tool_runs r
                JOIN external_run_contracts c USING(tool_run_id)
                WHERE r.analysis_run_id=? AND c.provider_id=?
                ORDER BY r.tool_run_id DESC LIMIT 1""",
                (int(audit["analysis_run_id"]), INSTALLED_PACKAGE_PROVIDER_ID),
            ).fetchone()
            if historical_inventory is None:
                return None
            current_versions = _installed_versions(
                connection,
                int(current_inventory["tool_run_id"]),
            )
            historical_versions = _installed_versions(
                connection,
                int(historical_inventory["tool_run_id"]),
            )
            if current_versions is None or current_versions != historical_versions:
                return None
            audit_run = int(audit["tool_run_id"])
            audit_counts = connection.execute(
                """SELECT
                SUM(CASE WHEN metric_name='known_vulnerability_count'
                    AND subject_key='project:installed-environment' THEN value ELSE 0 END)
                    AS vulnerabilities,
                SUM(CASE WHEN metric_name='audit_current_at_observation'
                    AND subject_key='project:installed-environment' THEN value ELSE 0 END)
                    AS current_markers
                FROM external_metrics WHERE tool_run_id=?""",
                (audit_run,),
            ).fetchone()
            if (
                audit_counts is None
                or float(audit_counts["vulnerabilities"] or 0) != 0.0
                or float(audit_counts["current_markers"] or 0) != 1.0
                or connection.execute(
                    "SELECT COUNT(*) FROM external_findings WHERE tool_run_id=?",
                    (audit_run,),
                ).fetchone()[0]
                != 0
            ):
                return None
            return {
                "tool_run_id": audit_run,
                "analysis_run_id": int(audit["analysis_run_id"]),
                "result_digest": str(audit["result_digest"]),
                "fresh_until_unix_seconds": float(audit["fresh_until"]),
                "installed_distributions": len(current_versions),
                "inventory_versions_identical": True,
                "known_vulnerabilities": 0,
            }
    except (OSError, RuntimeError, sqlite3.DatabaseError, TypeError, ValueError):
        return None


def _network_only_pip_failure(provider: object) -> bool:
    return (
        str(getattr(provider, "provider_id", "")) == PIP_AUDIT_PROVIDER_ID
        and str(getattr(provider, "status", "")) == "abstained"
        and "pip_audit_network_unavailable" in str(getattr(provider, "reason", ""))
    )


_VOLATILE_INVENTORY_METADATA_KEYS = frozenset(
    {"observed_at_utc", "observed_date_utc", "snapshot_id"}
)


def _normalized_inventory_metadata(raw: object) -> Mapping[str, object] | None:
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return {
        str(key): value
        for key, value in payload.items()
        if key not in _VOLATILE_INVENTORY_METADATA_KEYS
    }


def _installed_inventory_projection(
    connection: Any,
    tool_run_id: int,
) -> tuple[str, Mapping[str, int], Mapping[str, str]] | None:
    """Digest inventory semantics while excluding only observation-clock fields."""

    effective = _effective_provider_projection_run(connection, tool_run_id)
    if effective is None:
        return None
    metrics = connection.execute(
        """SELECT subject_kind,subject_key,category,metric_name,value,unit,metadata_json
        FROM external_metrics WHERE tool_run_id=?
        ORDER BY subject_kind,subject_key,category,metric_name,unit LIMIT 5001""",
        (effective,),
    ).fetchall()
    relations = connection.execute(
        """SELECT relation_kind,source_kind,source_key,target_kind,target_key,
        directed,confidence,metadata_json FROM external_relations WHERE tool_run_id=?
        ORDER BY portable_relation_id LIMIT 5001""",
        (effective,),
    ).fetchall()
    findings = connection.execute(
        """SELECT portable_finding_id,category,code,severity,message,start_line,
        start_column,end_line,end_column,metadata_json FROM external_findings
        WHERE tool_run_id=? ORDER BY portable_finding_id LIMIT 5001""",
        (effective,),
    ).fetchall()
    if len(metrics) > 5_000 or len(relations) > 5_000 or len(findings) > 5_000:
        return None
    normalized_metrics: list[Mapping[str, object]] = []
    for row in metrics:
        # This value is the re-observation clock itself, not inventory state.
        if str(row["metric_name"]) == "inventory_observed_at_unix_seconds":
            continue
        metadata = _normalized_inventory_metadata(row["metadata_json"])
        if metadata is None:
            return None
        normalized_metrics.append(
            {
                "subject_kind": str(row["subject_kind"]),
                "subject_key": str(row["subject_key"]),
                "category": str(row["category"]),
                "metric_name": str(row["metric_name"]),
                "value": float(row["value"]),
                "unit": str(row["unit"]),
                "metadata": metadata,
            }
        )
    normalized_relations: list[Mapping[str, object]] = []
    for row in relations:
        metadata = _normalized_inventory_metadata(row["metadata_json"])
        if metadata is None:
            return None
        normalized_relations.append(
            {
                "relation_kind": str(row["relation_kind"]),
                "source_kind": str(row["source_kind"]),
                "source_key": str(row["source_key"]),
                "target_kind": str(row["target_kind"]),
                "target_key": str(row["target_key"]),
                "directed": int(row["directed"]),
                "confidence": float(row["confidence"]),
                "metadata": metadata,
            }
        )
    normalized_findings: list[Mapping[str, object]] = []
    for row in findings:
        metadata = _normalized_inventory_metadata(row["metadata_json"])
        if metadata is None:
            return None
        normalized_findings.append(
            {
                "portable_finding_id": str(row["portable_finding_id"]),
                "category": str(row["category"]),
                "code": str(row["code"]),
                "severity": str(row["severity"]),
                "message": str(row["message"]),
                "range": [
                    row["start_line"],
                    row["start_column"],
                    row["end_line"],
                    row["end_column"],
                ],
                "metadata": metadata,
            }
        )
    versions = _installed_versions(connection, tool_run_id)
    if versions is None:
        return None
    payload = {
        "schema": "neocortex.installed-inventory-replay-projection/v1",
        "metrics": normalized_metrics,
        "relations": normalized_relations,
        "findings": normalized_findings,
    }
    return (
        "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest(),
        {
            "metrics": len(normalized_metrics),
            "relations": len(normalized_relations),
            "findings": len(normalized_findings),
            "installed_distributions": len(versions),
        },
        versions,
    )


def _installed_inventory_replay_receipt(
    state_directory: Path,
    *,
    first_analysis_run_id: int,
    replay_analysis_run_id: int,
) -> Mapping[str, object] | None:
    """Verify two deliberate inventory re-observations have identical semantics."""

    database = Path(state_directory) / "code.sqlite3"
    if not database.is_file():
        return None
    try:
        with readonly_code_database(database) as connection:
            validate_code_schema(connection)
            tool_runs: list[int] = []
            for analysis_run_id in (first_analysis_run_id, replay_analysis_run_id):
                row = connection.execute(
                    """SELECT r.tool_run_id FROM external_tool_runs r
                    JOIN external_run_contracts c USING(tool_run_id)
                    WHERE r.analysis_run_id=? AND c.provider_id=?
                    AND r.status='completed' AND c.execution='full'
                    AND c.coverage_complete=1 AND c.result_digest IS NOT NULL
                    ORDER BY r.tool_run_id DESC LIMIT 1""",
                    (analysis_run_id, INSTALLED_PACKAGE_PROVIDER_ID),
                ).fetchone()
                if row is None:
                    return None
                tool_runs.append(int(row["tool_run_id"]))
            first = _installed_inventory_projection(connection, tool_runs[0])
            replay = _installed_inventory_projection(connection, tool_runs[1])
            if first is None or replay is None or first != replay:
                return None
            digest, counts, _versions = first
            return {
                "schema": "neocortex.installed-inventory-replay-receipt/v1",
                "first_tool_run_id": tool_runs[0],
                "replay_tool_run_id": tool_runs[1],
                "semantic_projection_digest": digest,
                **counts,
                "observation_clock_excluded": True,
                "semantics_identical": True,
            }
    except (OSError, RuntimeError, sqlite3.DatabaseError, TypeError, ValueError):
        return None


def _coverage_gate(review: object | None) -> ValidationGate:
    started = time.monotonic_ns()
    command = ("Neocortex", "--self-analysis", "--analysis-profile", "trusted-deep")
    analysis = getattr(review, "test_coverage", None)
    if analysis is None or analysis.status != "ready":
        return _gate(
            "affected_coverage",
            "abstained",
            "coverage_not_published"
            if analysis is None
            else analysis.reason or "coverage_not_ready",
            started,
            command,
            {},
        )
    outcomes = analysis.outcomes
    failed = 0 if outcomes is None else outcomes.failed
    selected = 0 if outcomes is None else outcomes.selected
    if not analysis.measurement_complete or outcomes is None or selected <= 0:
        status: Literal["failed", "abstained", "passed"] = "abstained"
        reason = "coverage_measurement_incomplete"
    elif failed:
        status = "failed"
        reason = "selected_tests_failed"
    else:
        status = "passed"
        reason = "selected_tests_passed_with_branch_coverage"
    return _gate(
        "affected_coverage",
        status,
        reason,
        started,
        command,
        {
            "provider_id": analysis.provider_id,
            "tool_run_id": analysis.tool_run_id,
            "effective_tool_run_id": analysis.effective_tool_run_id,
            "suite_selection": analysis.suite_selection,
            "measurement_complete": analysis.measurement_complete,
            "tests_collected": outcomes.collected if outcomes is not None else 0,
            "tests_selected": selected,
            "tests_failed": failed,
            "suite_signature": analysis.suite_signature,
            "measurement_scope_signature": analysis.measurement_scope_signature,
            "limitations": list(analysis.limitations),
        },
    )


def _fresh_review_gate(
    state_directory: Path,
    *,
    expected_profile: str = "trusted-deep",
    change: GitChangeSnapshot | None = None,
) -> tuple[ValidationGate, object | None]:
    started = time.monotonic_ns()
    command = ("Neocortex", "--state-directory", str(state_directory), "--code-review")
    try:
        result = review_code_state(state_directory, limit=50)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return (
            _gate(
                "autoanalysis_verdict",
                "abstained",
                f"review_unavailable:{type(exc).__name__}",
                started,
                command,
                {"error": str(exc)[:4096]},
            ),
            None,
        )
    if result.status != "ready" or result.snapshot is None:
        return (
            _gate(
                "autoanalysis_verdict",
                "abstained",
                result.reason or "review_not_ready",
                started,
                command,
                {},
            ),
            result,
        )
    suite = result.external_evidence_suite
    providers = () if suite is None else suite.providers
    provider_ids = frozenset(item.provider_id for item in providers)
    missing_providers = tuple(sorted(_TRUSTED_DEEP_REQUIRED_PROVIDER_IDS - provider_ids))
    pip_provider = next(
        (item for item in providers if item.provider_id == PIP_AUDIT_PROVIDER_ID),
        None,
    )
    historical_pip_audit = (
        _historical_pip_audit_fallback(
            state_directory,
            analysis_run_id=result.snapshot.analysis_run_id,
            change=change,
        )
        if pip_provider is not None and _network_only_pip_failure(pip_provider)
        else None
    )
    provider_failures = tuple(
        item.provider_id
        for item in providers
        if _provider_failure(item)
        and not (item is pip_provider and historical_pip_audit is not None)
    )
    provider_delta_observations = tuple(
        {
            "provider_id": item.provider_id,
            "added": item.added,
            "resolved": item.resolved,
        }
        for item in providers
        if item.status == "ready" and item.gate == "failed"
    )
    evaluation_abstentions = sum(
        item.observation_status == "abstained" for item in result.question_evaluations
    )
    failed_supply_gates = tuple(
        item.gate
        for item in (result.supply_chain.gates if result.supply_chain else ())
        if item.status == "failed"
    )
    status: Literal["passed", "failed", "abstained"] = (
        "failed"
        if provider_failures or failed_supply_gates
        else "abstained"
        if missing_providers
        or suite is None
        or suite.profile != expected_profile
        or result.snapshot.freshness not in {"current", "publication_only"}
        else "passed"
    )
    reason = (
        "provider_or_supply_gate_failed"
        if status == "failed"
        else "trusted_deep_provider_set_incomplete"
        if missing_providers or suite is None or suite.profile != expected_profile
        else "review_snapshot_not_current_or_published"
        if status == "abstained"
        else "fresh_review_has_no_failed_machine_gate"
    )
    return (
        _gate(
            "autoanalysis_verdict",
            status,
            reason,
            started,
            command,
            {
                "schema": result.as_payload().get("schema"),
                "digest": None if result.digest is None else asdict(result.digest),
                "analysis_run_id": result.snapshot.analysis_run_id,
                "freshness": result.snapshot.freshness,
                "question_evaluations": len(result.question_evaluations),
                "evaluation_abstentions": evaluation_abstentions,
                "provider_failures": list(provider_failures),
                "provider_delta_observations": list(provider_delta_observations),
                "historical_pip_audit_fallback": historical_pip_audit,
                "missing_providers": list(missing_providers),
                "external_profile": None if suite is None else suite.profile,
                "failed_supply_gates": list(failed_supply_gates),
                "recommendations": len(result.recommendations),
                "mutation_authority": False,
            },
        ),
        result,
    )


def _candidate_wheel_gate(
    root: Path,
    *,
    runner: _CommandRunner,
) -> ValidationGate:
    """Build the exact dirty-tree candidate and smoke it outside the checkout."""

    started = time.monotonic_ns()
    command = (sys.executable, "-m", "build", "--wheel", "--no-isolation")
    try:
        with tempfile.TemporaryDirectory(prefix="neocortex-candidate-wheel-") as temporary:
            workspace = Path(temporary)
            wheelhouse = workspace / "wheelhouse"
            wheelhouse.mkdir()
            # CPython's installed setuptools includes the pinned bdist_wheel
            # command.  This uses no resolver and no network.
            build_command = (
                sys.executable,
                "-c",
                (
                    "from setuptools.build_meta import build_wheel; "
                    "import sys; print(build_wheel(sys.argv[1]))"
                ),
                wheelhouse,
            )
            completed = runner(
                build_command,
                cwd=root,
                timeout=10 * 60,
                environment=None,
            )
            if completed.returncode != 0:
                return _gate(
                    "candidate_wheel_smoke",
                    "failed",
                    f"wheel_build_exit:{completed.returncode}",
                    started,
                    command,
                    {"output": _bounded_output(completed)},
                )
            wheels = tuple(wheelhouse.glob("neocortex_framework-*.whl"))
            if len(wheels) != 1:
                raise ChangeValidationError("candidate_wheel_count_invalid")
            wheel = wheels[0]
            candidate = workspace / "candidate"
            venv.EnvBuilder(with_pip=False, clear=False, symlinks=True).create(candidate)
            candidate_python = pip_bootstrap.environment_python(candidate)
            dependency_source = (
                Path(sys.prefix)
                / "lib"
                / (f"python{sys.version_info.major}.{sys.version_info.minor}")
                / "site-packages"
            )
            if not dependency_source.is_dir():
                raise ChangeValidationError("canonical_runtime_site_packages_missing")
            install_result = runner(
                (
                    sys.executable,
                    "-m",
                    "pip",
                    "--python",
                    candidate_python,
                    "install",
                    "--isolated",
                    "--disable-pip-version-check",
                    "--no-index",
                    "--no-deps",
                    wheel,
                ),
                cwd=workspace,
                timeout=5 * 60,
                environment=None,
            )
            if install_result.returncode != 0:
                return _gate(
                    "candidate_wheel_smoke",
                    "failed",
                    f"wheel_install_exit:{install_result.returncode}",
                    started,
                    command,
                    {"output": _bounded_output(install_result)},
                )
            candidate_site_packages = (
                candidate_python.parent.parent
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
            )
            (candidate_site_packages / "_neocortex_canonical_runtime.pth").write_text(
                str(dependency_source) + "\n",
                encoding="utf-8",
            )
            probe = workspace / "probe"
            probe.mkdir()
            probe_script = (
                "import importlib.metadata,json,pathlib;"
                "import neocortex,_04_Nucleo_Operativo.code_change_validation as c;"
                "root=pathlib.Path(c.__file__).resolve();"
                "assert 'site-packages' in root.parts,root;"
                "rules=root.parent/'semgrep_rules'/'neocortex_invariants.yml';"
                "assert rules.is_file(),rules;"
                "print(json.dumps({'module':str(root),'version':"
                "importlib.metadata.version('neocortex-framework'),'rules':str(rules)},sort_keys=True))"
            )
            probe_result = runner(
                (candidate_python, "-I", "-c", probe_script),
                cwd=probe,
                timeout=60,
                environment=None,
            )
            if probe_result.returncode != 0:
                return _gate(
                    "candidate_wheel_smoke",
                    "failed",
                    f"wheel_probe_exit:{probe_result.returncode}",
                    started,
                    command,
                    {"output": _bounded_output(probe_result)},
                )
            entrypoint = candidate / "bin" / "Neocortex"
            version_result = runner(
                (entrypoint, "--version"),
                cwd=probe,
                timeout=60,
                environment=None,
            )
            help_result = runner(
                (entrypoint, "code", "validate", "--help"),
                cwd=probe,
                timeout=60,
                environment=None,
            )
            if (
                version_result.returncode != 0
                or help_result.returncode != 0
                or "Neocortex code validate" not in help_result.stdout
            ):
                return _gate(
                    "candidate_wheel_smoke",
                    "failed",
                    "installed_candidate_entrypoint_probe_failed",
                    started,
                    command,
                    {
                        "version_output": _bounded_output(version_result),
                        "help_output": _bounded_output(help_result),
                    },
                )
            return _gate(
                "candidate_wheel_smoke",
                "passed",
                "candidate_wheel_installed_and_executed_outside_checkout",
                started,
                command,
                {
                    "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    "wheel_bytes": wheel.stat().st_size,
                    "probe": probe_result.stdout.strip(),
                    "version": version_result.stdout.strip(),
                    "entrypoint": str(entrypoint),
                },
            )
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return _gate(
            "candidate_wheel_smoke",
            "abstained",
            f"candidate_wheel_unavailable:{type(exc).__name__}",
            started,
            command,
            {"error": str(exc)[:4096]},
        )


def _trusted_deep_command(
    root: Path,
    state_directory: Path,
    selectors: Sequence[str],
    *,
    max_tests: int,
    time_budget_seconds: int,
) -> tuple[str | os.PathLike[str], ...]:
    if not selectors:
        raise ChangeValidationError("trusted_deep_requires_selected_tests")
    command: list[str | os.PathLike[str]] = [
        sys.executable,
        "-m",
        "neocortex",
        "--self-analysis",
        "--analysis-profile",
        "trusted-deep",
        "--root",
        root,
        "--state-directory",
        state_directory,
    ]
    for selector in selectors:
        command.extend(("--deep-test-selector", selector))
    command.extend(
        (
            "--deep-max-tests",
            str(max_tests),
            "--deep-time-budget-seconds",
            str(time_budget_seconds),
            "--deep-shard-size",
            str(min(_CANONICAL_DEEP_SHARD_SIZE, max_tests)),
        )
    )
    return tuple(command)


def _documentation_only_change(change: GitChangeSnapshot) -> bool:
    """Recognize bounded documentation paths without classifying unknown files."""

    if not change.changed_paths:
        return False
    for relative in change.changed_paths:
        path = PurePosixPath(relative)
        if path.suffix.casefold() != ".md":
            return False
        if len(path.parts) == 1 or path.parts[0] == "docs":
            continue
        if len(path.parts) >= 3 and path.parts[:2] == (".codex", "handoffs"):
            continue
        return False
    return True


def _review_digest_payload(review: object) -> Mapping[str, object] | None:
    digest = getattr(review, "digest", None)
    return None if digest is None else asdict(digest)


def _replay_gate(
    first_review: object | None,
    replay_review: object | None,
    *,
    state_directory: Path,
    change: GitChangeSnapshot,
) -> ValidationGate:
    started = time.monotonic_ns()
    command = ("Neocortex", "--self-analysis", "--analysis-profile", "trusted-deep")
    if first_review is None or replay_review is None:
        return _gate(
            "trusted_deep_replay",
            "abstained",
            "replay_review_missing",
            started,
            command,
            {},
        )
    first_snapshot = getattr(first_review, "snapshot", None)
    replay_snapshot = getattr(replay_review, "snapshot", None)
    first_suite = getattr(first_review, "external_evidence_suite", None)
    replay_suite = getattr(replay_review, "external_evidence_suite", None)
    if any(item is None for item in (first_snapshot, replay_snapshot, first_suite, replay_suite)):
        return _gate(
            "trusted_deep_replay",
            "abstained",
            "replay_contract_incomplete",
            started,
            command,
            {},
        )
    first_providers = {
        item.provider_id: item
        for item in cast(Any, first_suite).providers
        if item.provider_id in _TRUSTED_DEEP_REQUIRED_PROVIDER_IDS
    }
    replay_providers = {
        item.provider_id: item
        for item in cast(Any, replay_suite).providers
        if item.provider_id in _TRUSTED_DEEP_REQUIRED_PROVIDER_IDS
    }
    first_pip = first_providers.get(PIP_AUDIT_PROVIDER_ID)
    replay_pip = replay_providers.get(PIP_AUDIT_PROVIDER_ID)
    first_historical_pip = (
        _historical_pip_audit_fallback(
            state_directory,
            analysis_run_id=cast(Any, first_snapshot).analysis_run_id,
            change=change,
        )
        if first_pip is not None and _network_only_pip_failure(first_pip)
        else None
    )
    replay_historical_pip = (
        _historical_pip_audit_fallback(
            state_directory,
            analysis_run_id=cast(Any, replay_snapshot).analysis_run_id,
            change=change,
        )
        if replay_pip is not None and _network_only_pip_failure(replay_pip)
        else None
    )
    historical_pip_replayed = (
        first_historical_pip is not None
        and replay_historical_pip is not None
        and first_historical_pip["tool_run_id"] == replay_historical_pip["tool_run_id"]
        and first_historical_pip["result_digest"] == replay_historical_pip["result_digest"]
    )
    installed_inventory_replay = _installed_inventory_replay_receipt(
        state_directory,
        first_analysis_run_id=cast(Any, first_snapshot).analysis_run_id,
        replay_analysis_run_id=cast(Any, replay_snapshot).analysis_run_id,
    )
    deliberately_reobserved_provider_ids = {
        provider_id
        for provider_id, resolved in (
            (PIP_AUDIT_PROVIDER_ID, historical_pip_replayed),
            (INSTALLED_PACKAGE_PROVIDER_ID, installed_inventory_replay is not None),
        )
        if resolved
    }
    identity_provider_ids = _TRUSTED_DEEP_REQUIRED_PROVIDER_IDS - (
        deliberately_reobserved_provider_ids
    )
    identities_match = first_providers.keys() == replay_providers.keys() and all(
        replay_providers[provider_id].result_digest == provider.result_digest
        and replay_providers[provider_id].comparability_signature
        == provider.comparability_signature
        for provider_id, provider in first_providers.items()
        if provider_id in identity_provider_ids
    )
    cache_replays = tuple(
        sorted(
            provider_id
            for provider_id, provider in replay_providers.items()
            if provider.execution == "cache_replay"
        )
    )
    expected = tuple(sorted(_TRUSTED_DEEP_REQUIRED_PROVIDER_IDS))
    expected_cache_replays = tuple(sorted(identity_provider_ids))
    status: Literal["passed", "failed", "abstained"] = (
        "failed"
        if not identities_match
        else "abstained"
        if tuple(sorted(cache_replays)) != expected_cache_replays
        else "passed"
    )
    reason = (
        "provider_results_changed_on_replay"
        if status == "failed"
        else "not_all_required_providers_replayed"
        if status == "abstained"
        else "all_required_provider_evidence_exactly_replayed"
    )
    return _gate(
        "trusted_deep_replay",
        status,
        reason,
        started,
        command,
        {
            "first_analysis_run_id": cast(Any, first_snapshot).analysis_run_id,
            "replay_analysis_run_id": cast(Any, replay_snapshot).analysis_run_id,
            "first_processing_signature": cast(Any, first_snapshot).processing_signature,
            "replay_processing_signature": cast(Any, replay_snapshot).processing_signature,
            "first_review_digest": _review_digest_payload(first_review),
            "replay_review_digest": _review_digest_payload(replay_review),
            "provider_result_identities_match": identities_match,
            "required_provider_ids": list(expected),
            "expected_cache_replay_ids": list(expected_cache_replays),
            "cache_replays": list(cache_replays),
            "historical_pip_audit_fallback": (
                replay_historical_pip if historical_pip_replayed else None
            ),
            "installed_inventory_replay": installed_inventory_replay,
        },
    )


def _known_question_specs() -> tuple[AnalysisQuestionSpec, ...]:
    """Return the complete v18 question vocabulary accepted by this validator.

    Adding a new question to Code review without classifying it here makes the
    canonical gate abstain.  This is intentional: an unknown question must not
    become acceptance-irrelevant merely because no runner exists yet.
    """

    from .code_analyzer_calibration import ANALYZER_CALIBRATION_EVIDENCE_QUESTION
    from .code_analyzer_effectiveness import (
        ANALYZER_CALIBRATION_QUESTION,
        ANALYZER_FRESHNESS_QUESTION,
    )
    from .code_architecture_questions import (
        ARCHITECTURE_CONTRACT_QUESTION,
        ARCHITECTURE_LOGICAL_OWNER_QUESTION,
        ARCHITECTURE_STATIC_GRAPH_QUESTION,
    )
    from .code_assurance_analysis import ASSURANCE_AVAILABILITY_QUESTION, ASSURANCE_QUESTION
    from .code_capability_reachability_analysis import (
        CAPABILITY_REACHABILITY_AVAILABILITY_QUESTION,
        CAPABILITY_REACHABILITY_QUESTION,
    )
    from .code_change_evolution_analysis import CHANGE_HISTORY_QUESTION, CHANGE_SURFACE_QUESTION
    from .code_class_surface_analysis import CLASS_SURFACE_QUESTION
    from .code_interface_surface_analysis import (
        CLI_SURFACE_QUESTION,
        CONFIGURATION_SURFACE_QUESTION,
        INTERFACE_SURFACE_AVAILABILITY_QUESTION,
        MODULE_SURFACE_QUESTION,
    )
    from .code_invariant_assurance_analysis import INVARIANT_ASSURANCE_QUESTION
    from .code_review_epistemics import STRUCTURAL_HOTSPOT_QUESTION
    from .code_route_capability_analysis import ROUTE_CAPABILITY_AVAILABILITY_QUESTION
    from .code_state_interaction_analysis import SQL_INTERACTION_QUESTION
    from .code_state_topology_analysis import TEXT_TERMINAL_PUBLICATION_QUESTION

    return (
        ANALYZER_CALIBRATION_EVIDENCE_QUESTION,
        ANALYZER_CALIBRATION_QUESTION,
        ANALYZER_FRESHNESS_QUESTION,
        ARCHITECTURE_CONTRACT_QUESTION,
        ARCHITECTURE_LOGICAL_OWNER_QUESTION,
        ARCHITECTURE_STATIC_GRAPH_QUESTION,
        ASSURANCE_AVAILABILITY_QUESTION,
        ASSURANCE_QUESTION,
        CAPABILITY_REACHABILITY_AVAILABILITY_QUESTION,
        CAPABILITY_REACHABILITY_QUESTION,
        CHANGE_HISTORY_QUESTION,
        CHANGE_SURFACE_QUESTION,
        CODE_SCHEMA_EVOLUTION_QUESTION,
        CLASS_SURFACE_QUESTION,
        CLI_SURFACE_QUESTION,
        CONFIGURATION_SURFACE_QUESTION,
        DEPENDENCY_EVIDENCE_QUESTION,
        INTERFACE_SURFACE_AVAILABILITY_QUESTION,
        INVARIANT_ASSURANCE_QUESTION,
        MODULE_SURFACE_QUESTION,
        ROUTE_CAPABILITY_AVAILABILITY_QUESTION,
        ROUTE_CAPABILITY_QUESTION,
        SECURITY_EVIDENCE_QUESTION,
        SQL_INTERACTION_QUESTION,
        STRUCTURAL_HOTSPOT_QUESTION,
        TEXT_SEMANTIC_PROJECTION_QUESTION,
        TEXT_TERMINAL_PUBLICATION_QUESTION,
        WORKFLOW_SQL_QUESTION,
    )


def _validation_question_scopes() -> tuple[_ValidationQuestionScope, ...]:
    """Declare which diff surfaces make unresolved evidence acceptance-critical."""

    return (
        _ValidationQuestionScope(
            "declared_import_architecture_contracts",
            ARCHITECTURE_CONTRACT_QUESTION,
            "architecture:contract:",
            "architecture.declared_import_contract_acceptance",
            frozenset(
                {
                    "Orquestador.py",
                    "tests/test_code_architecture_analysis.py",
                    "tests/test_code_architecture_contracts.py",
                    "tests/test_code_architecture_questions.py",
                }
            ),
            (
                "_01_Enumeracion/",
                "_02_Deduplicacion/",
                "_03_Progreso/",
                "_04_Nucleo_Operativo/",
                "_05_Interfaz/",
                "neocortex/",
                "tests/test_code_architecture_",
            ),
            frozenset({"tests/test_code_architecture_contracts.py"}),
            True,
        ),
        _ValidationQuestionScope(
            "public_text_route",
            ROUTE_CAPABILITY_QUESTION,
            "capability:route:text",
            "capability.public_route_acceptance",
            frozenset(
                {
                    "_04_Nucleo_Operativo/code_capability_reachability_analysis.py",
                    "_04_Nucleo_Operativo/code_route_capability_analysis.py",
                    "_04_Nucleo_Operativo/text_route.py",
                    "neocortex/cli.py",
                    "tests/test_code_public_route_experiments.py",
                }
            ),
            (
                "_04_Nucleo_Operativo/code_capability_",
                "_04_Nucleo_Operativo/code_route_capability_",
                "_04_Nucleo_Operativo/text_route",
            ),
            frozenset({"tests/test_code_public_route_experiments.py"}),
            True,
        ),
        _ValidationQuestionScope(
            "text_publication_sql",
            WORKFLOW_SQL_QUESTION,
            "workflow:text.derivation-publication:",
            "state.runtime_sql_trace",
            frozenset(
                {
                    "_04_Nucleo_Operativo/code_state_interaction_analysis.py",
                    "_04_Nucleo_Operativo/state_topology_contracts.py",
                    "_04_Nucleo_Operativo/text_derivation_repository.py",
                    "_04_Nucleo_Operativo/text_route.py",
                    "_04_Nucleo_Operativo/text_state.py",
                    "tests/test_code_state_interaction_analysis.py",
                    "tests/test_text_derivation_route.py",
                }
            ),
            (
                "_04_Nucleo_Operativo/code_state_interaction_",
                "_04_Nucleo_Operativo/state_topology_",
                "_04_Nucleo_Operativo/text_derivation_",
                "_04_Nucleo_Operativo/text_route",
                "_04_Nucleo_Operativo/text_state",
            ),
            frozenset(
                {
                    "tests/test_code_state_interaction_analysis.py",
                    "tests/test_text_derivation_route.py",
                }
            ),
            True,
        ),
        _ValidationQuestionScope(
            "text_semantic_projection_recovery",
            TEXT_SEMANTIC_PROJECTION_QUESTION,
            "workflow:text-to-semantic-published-projection",
            "state.semantic_process_death_recovery",
            frozenset(
                {
                    "_04_Nucleo_Operativo/code_state_projection_analysis.py",
                    "_04_Nucleo_Operativo/semantic_generation_repository.py",
                    "_04_Nucleo_Operativo/semantic_generation_worker.py",
                    "_04_Nucleo_Operativo/semantic_sources.py",
                    "_04_Nucleo_Operativo/semantic_text_index.py",
                    "_04_Nucleo_Operativo/text_derivation_repository.py",
                    "_04_Nucleo_Operativo/text_state.py",
                    "tests/test_semantic_text_staging_session.py",
                }
            ),
            (
                "_04_Nucleo_Operativo/code_state_projection_",
                "_04_Nucleo_Operativo/semantic_",
                "_04_Nucleo_Operativo/text_derivation_",
                "_04_Nucleo_Operativo/text_state",
            ),
            frozenset({"tests/test_semantic_text_staging_session.py"}),
            True,
        ),
        _ValidationQuestionScope(
            "code_schema_migration",
            CODE_SCHEMA_EVOLUTION_QUESTION,
            "code-owner-schema-subject-v1:",
            "evolution.code_schema_upgrade_matrix",
            frozenset(
                {
                    "_04_Nucleo_Operativo/code_change_evolution_analysis.py",
                    "_04_Nucleo_Operativo/code_experiment_store.py",
                    "_04_Nucleo_Operativo/code_schema.py",
                    "tests/test_code_experiment_store.py",
                    "tests/test_code_schema_migration_v1_v2.py",
                    "tests/test_framework_code_path_collation.py",
                }
            ),
            ("_04_Nucleo_Operativo/code_schema_migration_",),
            frozenset(
                {
                    "tests/test_code_schema_migration_v1_v2.py",
                    "tests/test_framework_code_path_collation.py",
                }
            ),
            True,
        ),
        _ValidationQuestionScope(
            "security_supply_boundary",
            SECURITY_EVIDENCE_QUESTION,
            "project:neocortex-security-evidence",
            None,
            frozenset(
                {
                    "MANIFEST.in",
                    "constraints.txt",
                    "pyproject.toml",
                    "_04_Nucleo_Operativo/code_security_dependency_questions.py",
                    "_04_Nucleo_Operativo/code_supply_chain_analysis.py",
                    "tools/quality_gate_supply_policy.json",
                }
            ),
            (
                "_04_Nucleo_Operativo/code_security_dependency_",
                "_04_Nucleo_Operativo/code_supply_chain_",
                "_04_Nucleo_Operativo/external_evidence_provider",
            ),
            frozenset(),
        ),
        _ValidationQuestionScope(
            "dependency_artifact_boundary",
            DEPENDENCY_EVIDENCE_QUESTION,
            "dependency:neocortex-environment",
            None,
            frozenset(
                {
                    "MANIFEST.in",
                    "constraints.txt",
                    "pyproject.toml",
                    "_04_Nucleo_Operativo/code_security_dependency_questions.py",
                    "_04_Nucleo_Operativo/code_supply_chain_analysis.py",
                    "tools/release_linux.py",
                    "tools/quality_gate_supply_policy.json",
                }
            ),
            (
                "_04_Nucleo_Operativo/code_security_dependency_",
                "_04_Nucleo_Operativo/code_supply_chain_",
                "_04_Nucleo_Operativo/external_evidence_provider",
            ),
            frozenset(),
        ),
    )


def _scope_relevance(
    scope: _ValidationQuestionScope,
    change: GitChangeSnapshot,
    selection: AffectedTestSelection,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    relevant_paths = set(scope.changed_paths)
    if scope.include_experiment_control_plane:
        relevant_paths.update(_EXPERIMENT_CONTROL_PLANE_PATHS)
    matched_paths = tuple(
        path
        for path in change.changed_paths
        if path in relevant_paths
        or any(path.startswith(prefix) for prefix in scope.changed_prefixes)
    )
    matched_selectors = tuple(
        selector for selector in selection.selectors if selector in scope.test_selectors
    )
    return matched_paths, matched_selectors


def _unknown_question_contracts(
    evaluations: Sequence[AnalysisQuestionEvaluation],
) -> tuple[tuple[str, str], ...]:
    known = {
        (spec.question_id, spec.version): analysis_question_spec_fingerprint(spec)
        for spec in _known_question_specs()
    }
    unknown: set[tuple[str, str]] = set()
    for evaluation in evaluations:
        identity = (evaluation.question_id, evaluation.question_version)
        if known.get(identity) != evaluation.question_spec_fingerprint:
            unknown.add(identity)
    return tuple(sorted(unknown))


def _relevant_question_state(
    review: object,
    *,
    change: GitChangeSnapshot,
    selection: AffectedTestSelection,
) -> tuple[
    tuple[dict[str, object], ...],
    tuple[tuple[_ValidationQuestionScope, AnalysisQuestionEvaluation], ...],
    tuple[str, ...],
]:
    raw_evaluations = getattr(review, "question_evaluations", None)
    if not isinstance(raw_evaluations, tuple) or any(
        not isinstance(item, AnalysisQuestionEvaluation) for item in raw_evaluations
    ):
        return (), (), ("question_evaluations_missing_or_untyped",)
    unknown = _unknown_question_contracts(raw_evaluations)
    errors = tuple(
        f"unclassified_question_contract:{question_id}:{version}"
        for question_id, version in unknown
    )
    bindings: list[dict[str, object]] = []
    relevant: list[tuple[_ValidationQuestionScope, AnalysisQuestionEvaluation]] = []
    for scope in _validation_question_scopes():
        matched_paths, matched_selectors = _scope_relevance(scope, change, selection)
        affected = bool(matched_paths or matched_selectors)
        evaluations = tuple(
            item
            for item in raw_evaluations
            if item.question_id == scope.spec.question_id
            and item.question_version == scope.spec.version
            and item.question_spec_fingerprint == analysis_question_spec_fingerprint(scope.spec)
            and (
                not scope.subject_prefix
                or item.subject.subject_key.startswith(scope.subject_prefix)
            )
        )
        bindings.append(
            {
                "scope_id": scope.scope_id,
                "question_id": scope.spec.question_id,
                "question_version": scope.spec.version,
                "subject_prefix": scope.subject_prefix,
                "relevance": "affected" if affected else "not_affected",
                "matched_changed_paths": list(matched_paths),
                "matched_test_selectors": list(matched_selectors),
                "evaluation_ids": [item.evaluation_id for item in evaluations],
            }
        )
        if not affected:
            continue
        if not evaluations:
            errors = (*errors, f"affected_question_evaluation_missing:{scope.scope_id}")
            continue
        relevant.extend((scope, item) for item in evaluations)
    return tuple(bindings), tuple(relevant), errors


def _technical_review_ids(review: object) -> dict[str, str]:
    verification = getattr(review, "technical_verification", None)
    raw_reviews = () if verification is None else getattr(verification, "reviews", ())
    return {
        item.evaluation_id: item.review_id
        for item in raw_reviews
        if getattr(item, "disposition", None) == "no_change_required_within_verified_scope"
    }


def _experiment_gate(
    review: object | None,
    *,
    root: Path,
    state_directory: Path,
    change: GitChangeSnapshot,
    selection: AffectedTestSelection,
) -> tuple[ValidationGate, tuple[Mapping[str, object], ...]]:
    started = time.monotonic_ns()
    command = ("Neocortex", "--code-experiment-run", "<registered-proposal>")
    plan = None if review is None else getattr(review, "experiment_plan", None)
    if plan is None:
        return (
            _gate(
                "allowlisted_experiments",
                "abstained",
                "experiment_plan_missing",
                started,
                command,
                {},
            ),
            (),
        )
    bindings, relevant, relevance_errors = _relevant_question_state(
        review,
        change=change,
        selection=selection,
    )
    evidence: dict[str, object] = {
        "change_content_digest": change.content_digest,
        "selection_strategy": selection.strategy,
        "question_bindings": list(bindings),
        "planned": plan.planned_count,
        "registry_gaps": plan.registry_gap_count,
    }
    if relevance_errors:
        evidence["relevance_errors"] = list(relevance_errors)
        return (
            _gate(
                "allowlisted_experiments",
                "abstained",
                "change_question_relevance_unresolvable",
                started,
                command,
                evidence,
            ),
            (),
        )
    if not relevant:
        return (
            _gate(
                "allowlisted_experiments",
                "not_required",
                "no_validation_required_question_is_affected",
                started,
                command,
                evidence,
            ),
            (),
        )
    proposals_by_evaluation = {item.evaluation_id: item for item in plan.proposals}
    technical_reviews = _technical_review_ids(review)
    proposals: list[Any] = []
    blockers: list[str] = []
    relevant_states: list[dict[str, object]] = []
    for scope, evaluation in relevant:
        proposal = proposals_by_evaluation.get(evaluation.evaluation_id)
        technical_review_id = technical_reviews.get(evaluation.evaluation_id)
        state: dict[str, object] = {
            "scope_id": scope.scope_id,
            "evaluation_id": evaluation.evaluation_id,
            "question_id": evaluation.question_id,
            "subject_key": evaluation.subject.subject_key,
            "decision_readiness": evaluation.decision_readiness,
            "expected_template_id": scope.template_id,
            "proposal_id": None if proposal is None else proposal.proposal_id,
            "technical_review_id": technical_review_id,
        }
        if evaluation.decision_readiness == "human_review_required":
            if technical_review_id is None:
                blockers.append(f"affected_question_lacks_technical_disposition:{scope.scope_id}")
                state["acceptance_state"] = "technical_disposition_missing"
            else:
                state["acceptance_state"] = "technical_disposition_verified"
        elif evaluation.decision_readiness == "experiment_required":
            if scope.template_id is None:
                blockers.append(f"affected_question_has_no_allowlisted_runner:{scope.scope_id}")
                state["acceptance_state"] = "allowlisted_runner_missing"
            elif (
                proposal is None
                or proposal.planning_status != "planned"
                or proposal.runner_kind == "none"
                or proposal.template_id != scope.template_id
            ):
                blockers.append(f"affected_question_experiment_unavailable:{scope.scope_id}")
                state["acceptance_state"] = "registered_experiment_unavailable"
            else:
                proposals.append(proposal)
                state["acceptance_state"] = "registered_experiment_selected"
        else:
            blockers.append(f"affected_question_evidence_incomplete:{scope.scope_id}")
            state["acceptance_state"] = "evidence_incomplete"
        relevant_states.append(state)
    evidence["relevant_questions"] = relevant_states
    if blockers:
        evidence["blocking_reasons"] = sorted(set(blockers))
        return (
            _gate(
                "allowlisted_experiments",
                "abstained",
                "affected_question_requires_unresolved_evidence",
                started,
                command,
                evidence,
            ),
            (),
        )
    if not proposals:
        return (
            _gate(
                "allowlisted_experiments",
                "passed",
                "affected_questions_have_verified_technical_dispositions",
                started,
                command,
                evidence,
            ),
            (),
        )
    snapshot = getattr(review, "snapshot", None)
    if snapshot is None:
        return (
            _gate(
                "allowlisted_experiments",
                "abstained",
                "experiment_snapshot_missing",
                started,
                command,
                {},
            ),
            (),
        )
    ordered_proposals = tuple(sorted(proposals, key=lambda item: item.proposal_id))
    unique_template_count = len(
        {(proposal.template_id, proposal.template_version) for proposal in proposals}
    )
    receipts: list[Mapping[str, object]] = []
    stored_receipt_ids: list[str] = []
    try:
        from .code_experiment_executor import execute_code_experiment
        from .code_experiment_store import (
            code_review_digest_identity,
            record_code_experiment_receipt,
        )

        database = Path(state_directory) / "code.sqlite3"
        review_digest = code_review_digest_identity(getattr(review, "digest", None))
        with tempfile.TemporaryDirectory(prefix="neocortex-change-experiment-") as temporary:
            scratch = Path(temporary)
            for proposal in ordered_proposals:
                receipt = execute_code_experiment(
                    cast(Any, proposal),
                    source_root=root,
                    code_database_path=database,
                    scratch_root=scratch,
                    source_version=snapshot.processing_signature,
                    expected_source_root=root,
                )
                receipts.append(receipt.as_payload())
                stored = record_code_experiment_receipt(
                    database,
                    receipt,
                    cast(Any, proposal),
                    analysis_run_id=snapshot.analysis_run_id,
                    processing_signature=snapshot.processing_signature,
                    review_digest=review_digest,
                )
                stored_receipt_ids.append(stored.receipt.receipt_id)
    except (
        OSError,
        RuntimeError,
        sqlite3.Error,
        TypeError,
        ValueError,
        subprocess.TimeoutExpired,
    ) as exc:
        return (
            _gate(
                "allowlisted_experiments",
                "abstained",
                f"experiment_execution_unavailable:{type(exc).__name__}",
                started,
                command,
                {
                    "error": str(exc)[:4096],
                    "stored_receipt_ids": stored_receipt_ids,
                    **evidence,
                },
            ),
            tuple(receipts),
        )
    failures = tuple(item for item in receipts if item.get("status") == "failed")
    abstentions = tuple(item for item in receipts if item.get("status") == "abstained")
    status: Literal["passed", "failed", "abstained"] = (
        "failed" if failures else "abstained" if abstentions else "passed"
    )
    reason = (
        "allowlisted_experiment_failed"
        if failures
        else "allowlisted_experiment_abstained"
        if abstentions
        else "unique_allowlisted_experiments_passed"
    )
    return (
        _gate(
            "allowlisted_experiments",
            status,
            reason,
            started,
            command,
            {
                "proposal_count": len(proposals),
                "unique_template_count": unique_template_count,
                "receipt_ids": [item.get("receipt_id") for item in receipts],
                "stored_receipt_ids": stored_receipt_ids,
                **evidence,
            },
        ),
        tuple(receipts),
    )


def _replay_technical_disposition_gate(
    review: object | None,
    *,
    change: GitChangeSnapshot,
    selection: AffectedTestSelection,
) -> ValidationGate:
    """Prove that every acceptance-relevant question closed after replay."""

    started = time.monotonic_ns()
    command = ("Neocortex", "--code-review", "<replay-technical-verification>")
    if review is None:
        return _gate(
            "diff_bound_technical_dispositions",
            "abstained",
            "replay_review_missing",
            started,
            command,
            {},
        )
    bindings, relevant, relevance_errors = _relevant_question_state(
        review,
        change=change,
        selection=selection,
    )
    evidence: dict[str, object] = {
        "change_content_digest": change.content_digest,
        "question_bindings": list(bindings),
    }
    if relevance_errors:
        evidence["relevance_errors"] = list(relevance_errors)
        return _gate(
            "diff_bound_technical_dispositions",
            "abstained",
            "replay_change_question_relevance_unresolvable",
            started,
            command,
            evidence,
        )
    if not relevant:
        return _gate(
            "diff_bound_technical_dispositions",
            "not_required",
            "no_validation_required_question_is_affected",
            started,
            command,
            evidence,
        )
    technical_reviews = _technical_review_ids(review)
    unresolved = tuple(
        sorted(
            {
                f"{scope.scope_id}:{evaluation.evaluation_id}"
                for scope, evaluation in relevant
                if evaluation.decision_readiness != "human_review_required"
                or evaluation.evaluation_id not in technical_reviews
            }
        )
    )
    evidence["relevant_evaluation_ids"] = [item.evaluation_id for _, item in relevant]
    evidence["technical_review_ids"] = [
        technical_reviews[item.evaluation_id]
        for _, item in relevant
        if item.evaluation_id in technical_reviews
    ]
    if unresolved:
        evidence["unresolved_relevant_evaluations"] = list(unresolved)
        return _gate(
            "diff_bound_technical_dispositions",
            "abstained",
            "affected_question_lacks_verified_technical_disposition_after_replay",
            started,
            command,
            evidence,
        )
    return _gate(
        "diff_bound_technical_dispositions",
        "passed",
        "all_affected_questions_have_verified_technical_dispositions",
        started,
        command,
        evidence,
    )


def _capture_unchanged(root: Path, change: GitChangeSnapshot) -> bool:
    current = capture_git_change(root, baseline=change.baseline)
    return (
        current.head_sha == change.head_sha
        and current.changed_paths == change.changed_paths
        and current.content_digest == change.content_digest
    )


def _build_result(values: Mapping[str, object]) -> CodeChangeValidationResult:
    provisional = dict(values)
    provisional["digest"] = "sha256:" + "0" * 64
    result = CodeChangeValidationResult.__new__(CodeChangeValidationResult)
    for name, value in provisional.items():
        object.__setattr__(result, name, value)
    digest = _result_digest(result)
    return CodeChangeValidationResult(digest=digest, **dict(values))  # type: ignore[arg-type]


def _finalize_validation(
    *,
    source: Path,
    state: Path,
    change: GitChangeSnapshot,
    selection: AffectedTestSelection,
    gates: Sequence[ValidationGate],
    experiment_proposals: Sequence[str] = (),
    executable_experiments: Sequence[str] = (),
    experiment_receipts: Sequence[Mapping[str, object]] = (),
) -> CodeChangeValidationResult:
    """Close one receipt against the same immutable source-change snapshot."""

    changed_during_run = not _capture_unchanged(source, change)
    completed_gates = (
        *gates,
        ValidationGate(
            "source_snapshot_unchanged",
            "failed" if changed_during_run else "passed",
            "source_changed_during_validation" if changed_during_run else "source_unchanged",
            0,
            ("git", "diff"),
            {"content_digest": change.content_digest},
        ),
    )
    failed = tuple(item for item in completed_gates if item.status == "failed")
    abstained = tuple(item for item in completed_gates if item.status == "abstained")
    status: Literal["passed", "failed", "abstained"] = (
        "failed" if failed else "abstained" if abstained else "passed"
    )
    reason = (
        f"failed_gate:{failed[0].gate_id}"
        if failed
        else f"abstained_gate:{abstained[0].gate_id}"
        if abstained
        else None
    )
    values: dict[str, object] = {
        "status": status,
        "reason": reason,
        "policy_id": CODE_CHANGE_VALIDATION_POLICY,
        "source_root": str(source),
        "state_directory": str(state),
        "git": change,
        "selection": selection,
        "gates": completed_gates,
        "experiment_proposals": tuple(experiment_proposals),
        "executable_experiments": tuple(executable_experiments),
        "experiment_receipts": tuple(experiment_receipts),
        "resource_boundary": (
            None
            if (admission := current_code_validation_resource_admission()) is None
            else admission.as_payload()
        ),
        "source_unchanged": not changed_during_run,
        "authority": "validation",
        "mutation_authority": False,
    }
    return _build_result(values)


def validate_code_change(
    *,
    root: Path | None = None,
    state_directory: Path | None = None,
    baseline: str = "HEAD",
    max_tests: int = 5000,
    time_budget_seconds: int = 900,
    runner: _CommandRunner = _default_runner,
    progress: Callable[[str], None] | None = None,
) -> CodeChangeValidationResult:
    """Run the canonical local Linux validation and return one bounded receipt."""

    if sys.platform != "linux" or os.name != "posix":
        raise ChangeValidationError("code change validation is Linux-only")
    if isinstance(max_tests, bool) or not 1 <= max_tests <= 5_000:
        raise ValueError("max_tests must be between 1 and 5000")
    if isinstance(time_budget_seconds, bool) or not 30 <= time_budget_seconds <= 900:
        raise ValueError("time_budget_seconds must be between 30 and 900")
    source = (source_repository_directory() if root is None else Path(root)).resolve(strict=True)
    resource_admission = current_code_validation_resource_admission()
    canonical_source = source_repository_directory().resolve(strict=True)
    if runner is _default_runner and source == canonical_source and resource_admission is None:
        raise ChangeValidationError(
            "canonical_resource_boundary_required:use `Neocortex code validate`"
        )
    state = (
        self_analysis_data_directory()
        if state_directory is None
        else Path(state_directory).expanduser().resolve(strict=False)
    )
    change = capture_git_change(source, baseline=baseline, runner=runner)
    selection = select_affected_tests(source, state, change)
    unpublished_sources = _unpublished_source_paths(source, state, change)
    gates: list[ValidationGate] = []
    report = progress if progress is not None else lambda _message: None
    report(f"snapshot captured: changed_paths={len(change.changed_paths)}")

    if not change.changed_paths:
        clean_values: dict[str, object] = {
            "status": "passed",
            "reason": None,
            "policy_id": CODE_CHANGE_VALIDATION_POLICY,
            "source_root": str(source),
            "state_directory": str(state),
            "git": change,
            "selection": selection,
            "gates": (
                ValidationGate(
                    "source_change_present",
                    "not_required",
                    "no_source_change",
                    0,
                    ("git", "diff"),
                    {"content_digest": change.content_digest},
                ),
            ),
            "experiment_proposals": (),
            "executable_experiments": (),
            "experiment_receipts": (),
            "resource_boundary": (
                None
                if (admission := current_code_validation_resource_admission()) is None
                else admission.as_payload()
            ),
            "source_unchanged": True,
            "authority": "validation",
            "mutation_authority": False,
        }
        return _build_result(clean_values)

    fallback_sources = tuple(
        sorted(
            {*selection.uncovered_sources, *unpublished_sources},
            key=lambda item: (item.casefold(), item),
        )
    )
    if fallback_sources:
        fallback = _global_change_fallback_tests(source)
        selection = AffectedTestSelection(
            strategy="affected",
            selectors=tuple(
                sorted(
                    {*selection.selectors, *fallback},
                    key=lambda item: (item.casefold(), item),
                )
            ),
            direct_tests=selection.direct_tests,
            dependency_tests=selection.dependency_tests,
            convention_tests=tuple(
                sorted(
                    {*selection.convention_tests, *fallback},
                    key=lambda item: (item.casefold(), item),
                )
            ),
            uncovered_sources=fallback_sources,
            reasons=tuple(
                sorted(
                    {
                        *selection.reasons,
                        "published_import_graph_stale_for_changed_source"
                        if unpublished_sources
                        else "published_import_graph_current_for_changed_source",
                        "unresolved_changed_source_covered_by_public_boundary_and_scenarios",
                    }
                )
            ),
        )
        report(
            "affected evidence incomplete; added public-boundary and registered-scenario tests: "
            f"tests={len(selection.selectors)}"
        )
    else:
        report(
            f"affected selection ready: strategy={selection.strategy} "
            f"tests={len(selection.selectors)}"
        )

    # Invoke the existing local tool instead of duplicating its Ruff/Mypy/
    # Pyright and architecture baseline logic inside the product module.
    quality_command = (sys.executable, source / "tools" / "quality_gate.py")
    report("running local static no-regression gate")
    static_gate = _run_gate_command(
        "static_no_regression",
        (*quality_command, "--root", source, "static"),
        root=source,
        timeout=15 * 60,
        runner=runner,
        progress=report,
    )
    gates.append(static_gate)
    if static_gate.status != "passed":
        report("static gate did not pass; stopping before executable providers")
        return _finalize_validation(
            source=source,
            state=state,
            change=change,
            selection=selection,
            gates=gates,
        )
    report("running local architecture contracts")
    architecture_gate = _run_gate_command(
        "architecture_contracts",
        (*quality_command, "--root", source, "architecture"),
        root=source,
        timeout=5 * 60,
        runner=runner,
        progress=report,
    )
    gates.append(architecture_gate)
    if architecture_gate.status != "passed":
        report("architecture gate did not pass; stopping before executable providers")
        return _finalize_validation(
            source=source,
            state=state,
            change=change,
            selection=selection,
            gates=gates,
        )

    selectors = selection.selectors
    if not selectors:
        documentation_only = _documentation_only_change(change)
        gates.extend(
            (
                ValidationGate(
                    "affected_coverage",
                    "not_required" if documentation_only else "abstained",
                    (
                        "documentation_change_has_no_affected_tests"
                        if documentation_only
                        else "no_affected_test_evidence"
                    ),
                    0,
                    (),
                    {
                        "changed_paths": list(change.changed_paths),
                        "documentation_only": documentation_only,
                    },
                ),
                ValidationGate(
                    "trusted_deep_publication",
                    "not_required",
                    (
                        "documentation_change_does_not_require_runtime_evidence"
                        if documentation_only
                        else "empty_selection_must_not_expand_to_full_suite"
                    ),
                    0,
                    (),
                    {"selection_strategy": selection.strategy},
                ),
            )
        )
        report(
            "documentation-only change; trusted execution is not required"
            if documentation_only
            else "affected evidence is empty; abstaining without expanding to a full suite"
        )
        return _finalize_validation(
            source=source,
            state=state,
            change=change,
            selection=selection,
            gates=gates,
        )
    # The trusted-deep producer publishes static and executable evidence in
    # one run.  The review below is the sole consumer and verdict surface.
    analyze_command = _trusted_deep_command(
        source,
        state,
        selectors,
        max_tests=max_tests,
        time_budget_seconds=time_budget_seconds,
    )
    report("publishing trusted-deep evidence and selected branch coverage")
    publication_gate = _run_gate_command(
        "trusted_deep_publication",
        analyze_command,
        root=source,
        timeout=time_budget_seconds + 15 * 60,
        runner=runner,
        environment={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        progress=report,
    )
    gates.append(publication_gate)
    if publication_gate.status != "passed":
        report("trusted-deep publication did not pass; stopping before downstream consumers")
        return _finalize_validation(
            source=source,
            state=state,
            change=change,
            selection=selection,
            gates=gates,
        )
    report("reading fail-closed code-review verdict")
    review_gate, review = _fresh_review_gate(state, change=change)
    gates.append(review_gate)
    if review_gate.status != "passed":
        report("autoanalysis verdict did not pass; stopping before experiments and artifact work")
        return _finalize_validation(
            source=source,
            state=state,
            change=change,
            selection=selection,
            gates=gates,
        )
    if selectors:
        coverage_gate = _coverage_gate(review)
        gates.append(coverage_gate)
        if coverage_gate.status != "passed":
            report("affected coverage did not pass; stopping before experiments and artifact work")
            return _finalize_validation(
                source=source,
                state=state,
                change=change,
                selection=selection,
                gates=gates,
            )
    experiment_gate, experiment_receipts = _experiment_gate(
        review,
        root=source,
        state_directory=state,
        change=change,
        selection=selection,
    )
    report(f"executed allow-listed experiment templates: receipts={len(experiment_receipts)}")
    gates.append(experiment_gate)
    if experiment_gate.status not in {"passed", "not_required"}:
        report("allow-listed experiments did not pass; stopping before artifact and replay")
        return _finalize_validation(
            source=source,
            state=state,
            change=change,
            selection=selection,
            gates=gates,
            experiment_receipts=experiment_receipts,
        )
    report("building, installing and executing candidate wheel outside checkout")
    candidate_gate = _candidate_wheel_gate(source, runner=runner)
    gates.append(candidate_gate)
    if candidate_gate.status != "passed":
        report("candidate wheel did not pass; stopping before replay")
        return _finalize_validation(
            source=source,
            state=state,
            change=change,
            selection=selection,
            gates=gates,
            experiment_receipts=experiment_receipts,
        )

    # An identical second producer run must reuse exact provider publications.
    # This proves resumability instead of treating one green run as enough.
    report("replaying identical trusted-deep publication")
    gates.append(
        _run_gate_command(
            "trusted_deep_replay_publication",
            analyze_command,
            root=source,
            timeout=time_budget_seconds + 15 * 60,
            runner=runner,
            environment={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            progress=report,
        )
    )
    replay_review_gate, replay_review = _fresh_review_gate(state, change=change)
    gates.append(
        ValidationGate(
            "autoanalysis_replay_verdict",
            replay_review_gate.status,
            replay_review_gate.reason,
            replay_review_gate.duration_ms,
            replay_review_gate.command,
            replay_review_gate.evidence,
        )
    )
    gates.append(
        _replay_gate(
            review,
            replay_review,
            state_directory=state,
            change=change,
        )
    )
    gates.append(
        _replay_technical_disposition_gate(
            replay_review,
            change=change,
            selection=selection,
        )
    )
    report("verifying source snapshot remained unchanged")

    experiments: tuple[str, ...] = ()
    executable: tuple[str, ...] = ()
    if review is not None and getattr(review, "experiment_plan", None) is not None:
        plan = cast(Any, review).experiment_plan
        experiments = tuple(item.proposal_id for item in plan.proposals)
        executable = tuple(
            item.proposal_id
            for item in plan.proposals
            if item.planning_status == "planned" and item.runner_kind != "none"
        )

    return _finalize_validation(
        source=source,
        state=state,
        change=change,
        selection=selection,
        gates=gates,
        experiment_proposals=experiments,
        executable_experiments=executable,
        experiment_receipts=experiment_receipts,
    )


__all__ = [
    "CODE_CHANGE_VALIDATION_POLICY",
    "CODE_CHANGE_VALIDATION_SCHEMA",
    "AffectedTestSelection",
    "ChangeValidationError",
    "CodeChangeValidationResult",
    "GitChangeSnapshot",
    "ValidationGate",
    "capture_git_change",
    "select_affected_tests",
    "validate_code_change",
]
