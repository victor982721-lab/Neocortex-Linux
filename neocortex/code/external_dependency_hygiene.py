"""Bounded Deptry adapter for project dependency-declaration evidence.

The adapter receives an already isolated source stage and one verified PEP 621
configuration.  It never imports or executes staged content and only returns
advisory external-evidence contracts; the caller remains responsible for
publication and gate evaluation.
"""

from __future__ import annotations
import importlib.metadata
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from neocortex.runtime.control.bounded_subprocess import run_bounded_capture
from .code_external_evidence import ExternalEvidenceFile
from .external_evidence_models import (
    ExternalProviderFinding,
    ExternalProviderMetric,
    ExternalProviderRelation,
    external_metric_identity,
    external_relation_identity,
    external_signature,
)
from neocortex.semantic.semantic_models import fingerprint_bytes

DEPTRY_PROVIDER_ID = "deptry-project-dependencies"
DEPTRY_PROVIDER_SCHEMA = "neocortex.deptry-project-dependencies/v1"
DEPENDENCY_HYGIENE_CATEGORY = "dependency_hygiene"
DEPENDENCY_DECLARATION_GATE = "dependency_declaration_integrity"

# Deptry normally learns package-to-import mappings from installed distribution
# metadata.  NeoCortex intentionally audits its base candidate without installing
# every optional multimodal extra, so the import names whose distributions differ
# must remain explicit and versioned instead of becoming false DEP001 findings.
DEPTRY_PACKAGE_MODULE_NAME_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Pillow", ("PIL",)),
    ("PyMuPDF", ("fitz",)),
    ("pdfminer.six", ("pdfminer",)),
    ("faster-whisper", ("faster_whisper",)),
    ("nudenet", ("nudenet",)),
    ("numpy", ("numpy",)),
    ("PySide6", ("PySide6",)),
    ("pytesseract", ("pytesseract",)),
)

DEPTRY_LIMITATIONS = (
    "deptry_0_25_static_import_analysis_cannot_observe_all_dynamic_imports",
    "transitive_classification_depends_on_installed_distribution_metadata",
    "only_exact_staged_py_sources_and_the_verified_pep621_config_are_analyzed",
    "project_level_pyproject_issues_are_metrics_not_file_findings",
    "advisory_evidence_has_no_content_mutation_authority",
)

_SUPPORTED_CODES = frozenset({"DEP001", "DEP002", "DEP003", "DEP004", "DEP005"})
_GATE_CODES = frozenset({"DEP001", "DEP003", "DEP004"})
_PROJECT_CODES = frozenset({"DEP002", "DEP005"})
_PYTHON_CODES = _SUPPORTED_CODES - _PROJECT_CODES
_PYPROJECT_NAME = "pyproject.toml"
# Deptry 0.25.x compiles exclusions with Rust's ``regex`` crate, which rejects
# look-around expressions such as ``(?!)`` by panicking inside its native
# extension.  NUL cannot occur in a filesystem path on supported platforms, so
# this valid Rust-regex pattern preserves the exact staged inventory without
# matching any source path.
_NEVER_EXCLUDE_PATTERN = r"\x00"
_TIMEOUT_SECONDS = 180.0
_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024
_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_REPORT_BYTES = 8 * 1024 * 1024
_MAX_STDOUT_BYTES = 8 * 1024 * 1024
_MAX_STDERR_BYTES = 128 * 1024
_MAX_FILES = 2_000
_MAX_INPUT_BYTES = 512 * 1024 * 1024
_MAX_ISSUES = 10_000
_MAX_PATH_BYTES = 32_768
_MAX_MODULE_BYTES = 1_024
_MAX_MESSAGE_BYTES = 4_096
_PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")

_Classification = Literal["gate", "advisory"]
_LocationKind = Literal["python", "project"]


def _package_module_name_map_argument() -> str:
    return ",".join(
        f"{package}={'|'.join(modules)}" for package, modules in DEPTRY_PACKAGE_MODULE_NAME_MAP
    )


@dataclass(frozen=True, slots=True)
class DependencyHygieneExecution:
    """Normalized bounded evidence returned to the provider wrapper."""

    findings: tuple[ExternalProviderFinding, ...]
    metrics: tuple[ExternalProviderMetric, ...]
    relations: tuple[ExternalProviderRelation, ...]
    stdout_bytes: int
    stderr_bytes: int
    process_invocations: int
    limitations: tuple[str, ...]
    counters: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class _VerifiedConfig:
    raw: bytes
    project_key: str


@dataclass(frozen=True, slots=True)
class _DependencyIssue:
    identity: str
    code: str
    module: str
    relative_path: str
    line: int | None
    column: int | None
    message: str
    classification: _Classification
    location_kind: _LocationKind
    declared: bool
    runtime: bool
    dev: bool
    transitive: bool
    owner: ExternalEvidenceFile | None

    def metadata(self) -> dict[str, object]:
        return {
            "provider_schema": DEPTRY_PROVIDER_SCHEMA,
            "issue_identity": self.identity,
            "code": self.code,
            "module": self.module,
            "path": self.relative_path,
            "line": self.line,
            "column": self.column,
            "message": self.message,
            "classification": self.classification,
            "location_kind": self.location_kind,
            "declared": self.declared,
            "runtime": self.runtime,
            "dev": self.dev,
            "transitive": self.transitive,
            "mutation_authority": False,
        }


def _is_reparse_point(path: Path) -> bool:
    metadata = os.lstat(path)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & reparse)


def _required_text(value: object, *, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise ValueError(f"Deptry {label} is invalid")
    return value


def _normalized_project_name(value: object) -> str:
    name = _required_text(value, label="project name", maximum=256)
    if _PROJECT_NAME_PATTERN.fullmatch(name) is None:
        raise ValueError("Deptry project name is not a canonical PEP 621 name")
    return re.sub(r"[-_.]+", "-", name).casefold()


def _read_verified_config(path: Path) -> _VerifiedConfig:
    try:
        supplied = path.absolute()
        supplied_metadata = os.lstat(supplied)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("Deptry project configuration cannot be resolved") from exc
    if _is_reparse_point(supplied) or not stat.S_ISREG(supplied_metadata.st_mode):
        raise ValueError("Deptry project configuration is not a regular file")
    try:
        resolved = path.resolve(strict=True)
        before = os.lstat(resolved)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("Deptry project configuration cannot be resolved") from exc
    if resolved.name.casefold() != _PYPROJECT_NAME:
        raise ValueError("Deptry requires an explicit pyproject.toml configuration")
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Deptry project configuration is not a regular file")
    if before.st_size > _MAX_CONFIG_BYTES:
        raise ValueError("Deptry project configuration exceeds its byte bound")
    raw = resolved.read_bytes()
    after = os.lstat(resolved)
    if (
        len(raw) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise ValueError("Deptry project configuration changed during verification")
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("Deptry project configuration is malformed") from exc
    project = parsed.get("project")
    if not isinstance(project, Mapping):
        raise ValueError("Deptry requires a PEP 621 project table")
    dynamic = project.get("dynamic", [])
    if not isinstance(dynamic, list) or any(not isinstance(item, str) for item in dynamic):
        raise ValueError("Deptry project dynamic declarations are invalid")
    if {"dependencies", "optional-dependencies"}.intersection(dynamic):
        raise ValueError("Deptry does not accept dynamically loaded dependency declarations")
    dependencies = project.get("dependencies")
    optional = project.get("optional-dependencies")
    if not isinstance(dependencies, list) or any(
        not isinstance(item, str) for item in dependencies
    ):
        raise ValueError("Deptry project dependencies are invalid")
    if not isinstance(optional, Mapping):
        raise ValueError("Deptry project optional dependencies are invalid")
    dev = optional.get("dev")
    if not isinstance(dev, list) or any(not isinstance(item, str) for item in dev):
        raise ValueError("Deptry requires the explicit optional dependency group dev")
    project_name = _normalized_project_name(project.get("name"))
    return _VerifiedConfig(raw, f"project:{project_name}")


def _canonical_python_relative(value: str) -> str:
    if "\\" in value or len(value.encode("utf-8")) > _MAX_PATH_BYTES:
        raise ValueError("Deptry staged relative path is not canonical")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or not relative.parts
        or any(part in {"", ".", ".."} or ":" in part for part in relative.parts)
        or relative.suffix.casefold() != ".py"
    ):
        raise ValueError("Deptry staged relative path is not an analyzable Python source")
    return value


def _verify_staged_file(path: Path, owner: ExternalEvidenceFile) -> None:
    before = os.lstat(path)
    if _is_reparse_point(path) or not stat.S_ISREG(before.st_mode) or before.st_size != owner.size:
        raise ValueError("Deptry staged source is not an exact regular file")
    raw = path.read_bytes()
    after = os.lstat(path)
    if (
        len(raw) != owner.size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise ValueError("Deptry staged source changed during verification")
    observed = fingerprint_bytes(raw)
    if observed.xxh3_128 != owner.raw_xxh3_128 or observed.xxh3_64_guard != owner.raw_xxh3_64_guard:
        raise ValueError("Deptry staged source fingerprint disagrees with its inventory owner")


def _validate_exact_stage(
    stage_root: Path,
    staged: Mapping[str, ExternalEvidenceFile],
) -> tuple[Path, dict[str, ExternalEvidenceFile]]:
    try:
        resolved_stage = stage_root.resolve(strict=True)
        source_root = (resolved_stage / "source").resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("Deptry exact source stage cannot be resolved") from exc
    if (
        _is_reparse_point(resolved_stage)
        or not resolved_stage.is_dir()
        or _is_reparse_point(source_root)
        or not source_root.is_dir()
    ):
        raise ValueError("Deptry exact source stage is not a regular directory")
    if len(staged) > _MAX_FILES:
        raise ValueError("Deptry exact source stage exceeds its file bound")
    if sum(item.size for item in staged.values()) > _MAX_INPUT_BYTES:
        raise ValueError("Deptry exact source stage exceeds its byte bound")

    owners: dict[str, ExternalEvidenceFile] = {}
    relative_paths: set[str] = set()
    for raw_path, owner in staged.items():
        relative_path = _canonical_python_relative(owner.relative_path)
        relative_key = relative_path.casefold()
        if relative_key in relative_paths:
            raise ValueError("Deptry exact source stage duplicates a relative path")
        relative_paths.add(relative_key)
        expected = source_root.joinpath(*PurePosixPath(relative_path).parts)
        normalized_expected = os.path.normcase(os.path.abspath(expected))
        normalized_key = os.path.normcase(os.path.abspath(raw_path))
        if normalized_key != raw_path or normalized_key != normalized_expected:
            raise ValueError("Deptry staged path does not match its exact inventory owner")
        try:
            _verify_staged_file(expected, owner)
        except OSError as exc:
            raise ValueError("Deptry staged source is missing") from exc
        owners[normalized_expected] = owner

    observed: set[str] = set()
    entries = 0
    for current, directories, filenames in os.walk(source_root, followlinks=False):
        entries += len(directories) + len(filenames)
        if entries > _MAX_FILES * 8 + 1:
            raise ValueError("Deptry exact source stage exceeds its entry bound")
        current_path = Path(current)
        for directory in directories:
            path = current_path / directory
            if _is_reparse_point(path):
                raise ValueError("Deptry exact source stage contains a reparse directory")
        for filename in filenames:
            path = current_path / filename
            if _is_reparse_point(path) or not path.is_file():
                raise ValueError("Deptry exact source stage contains an unsafe file")
            if path.suffix.casefold() != ".py":
                raise ValueError("Deptry exact source stage contains a non-Python file")
            observed.add(os.path.normcase(os.path.abspath(path)))
    if observed != set(owners):
        raise ValueError("Deptry exact source stage and inventory ownership disagree")
    return source_root, owners


def _verify_stage_unchanged(owners: Mapping[str, ExternalEvidenceFile]) -> None:
    for path, owner in owners.items():
        _verify_staged_file(Path(path), owner)


def _deptry_version() -> str:
    try:
        version = importlib.metadata.version("deptry")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ValueError("Deptry 0.25.x is unavailable") from exc
    components = version.split(".", 2)
    if len(components) < 2 or components[:2] != ["0", "25"]:
        raise ValueError(f"Deptry version is unsupported: {version[:128]}")
    return version


def _write_verified_config(path: Path, raw: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(raw)
    if path.read_bytes() != raw:
        raise ValueError("Deptry scratch configuration copy disagrees")


def _unexpected_exit(completed: subprocess.CompletedProcess[bytes]) -> ValueError:
    raw = completed.stderr or completed.stdout
    detail = " ".join(raw.decode("utf-8", errors="replace").split())[:2_048]
    prefix = f"deptry_unexpected_exit:{completed.returncode}"
    return ValueError(prefix if not detail else f"{prefix}:{detail}")


def _read_report(path: Path) -> list[object]:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise ValueError("Deptry JSON report was not produced") from exc
    if _is_reparse_point(path) or not stat.S_ISREG(before.st_mode):
        raise ValueError("Deptry JSON report is not a regular file")
    if before.st_size > _MAX_REPORT_BYTES:
        raise ValueError("Deptry JSON report exceeds its byte bound")
    raw = path.read_bytes()
    after = os.lstat(path)
    if (
        len(raw) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise ValueError("Deptry JSON report changed during bounded read")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Deptry JSON report is malformed") from exc
    if not isinstance(payload, list):
        raise ValueError("Deptry JSON report is not an array")
    if len(payload) > _MAX_ISSUES:
        raise ValueError("Deptry JSON report exceeds its issue bound")
    return payload


def _mapping(value: object, *, label: str, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"Deptry {label} has an incompatible schema")
    return value


def _location_number(value: object, *, label: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"Deptry {label} is invalid")
    return value


def _owner_for_reported_path(
    value: str,
    *,
    stage_root: Path,
    source_root: Path,
    config_paths: frozenset[str],
    owners: Mapping[str, ExternalEvidenceFile],
) -> tuple[_LocationKind, str, ExternalEvidenceFile | None]:
    reported = Path(value)
    candidates: list[Path]
    if reported.is_absolute():
        candidates = [reported]
    else:
        candidates = [stage_root / reported, source_root / reported]
    normalized_candidates = {
        os.path.normcase(os.path.abspath(candidate)) for candidate in candidates
    }
    for normalized in normalized_candidates:
        owner = owners.get(normalized)
        if owner is not None:
            return "python", owner.relative_path, owner
    if normalized_candidates.intersection(config_paths) or (
        not reported.is_absolute()
        and PurePosixPath(value.replace("\\", "/")).as_posix().casefold() == _PYPROJECT_NAME
    ):
        return "project", _PYPROJECT_NAME, None
    raise ValueError(f"Deptry reported an unowned path: {value[:512]}")


def _issue_semantics(code: str) -> tuple[_Classification, bool, bool, bool, bool]:
    classification: _Classification = "gate" if code in _GATE_CODES else "advisory"
    declared = code in {"DEP002", "DEP004", "DEP005"}
    runtime = True
    dev = code == "DEP004"
    transitive = code == "DEP003"
    return classification, declared, runtime, dev, transitive


def _normalize_issue(
    value: object,
    *,
    stage_root: Path,
    source_root: Path,
    config_paths: frozenset[str],
    owners: Mapping[str, ExternalEvidenceFile],
) -> _DependencyIssue:
    item = _mapping(
        value,
        label="issue",
        fields=frozenset({"error", "module", "location"}),
    )
    error = _mapping(
        item.get("error"),
        label="issue error",
        fields=frozenset({"code", "message"}),
    )
    location = _mapping(
        item.get("location"),
        label="issue location",
        fields=frozenset({"file", "line", "column"}),
    )
    code = _required_text(error.get("code"), label="issue code", maximum=16)
    if code not in _SUPPORTED_CODES:
        raise ValueError(f"Deptry issue code is unsupported: {code}")
    module = _required_text(item.get("module"), label="issue module", maximum=_MAX_MODULE_BYTES)
    message = _required_text(
        error.get("message"), label="issue message", maximum=_MAX_MESSAGE_BYTES
    )
    reported_path = _required_text(
        location.get("file"), label="issue path", maximum=_MAX_PATH_BYTES
    )
    location_kind, relative_path, owner = _owner_for_reported_path(
        reported_path,
        stage_root=stage_root,
        source_root=source_root,
        config_paths=config_paths,
        owners=owners,
    )
    raw_line = location.get("line")
    raw_column = location.get("column")
    line: int | None
    column: int | None
    if location_kind == "python":
        if code not in _PYTHON_CODES:
            raise ValueError("Deptry project-level issue was reported on Python content")
        line = _location_number(raw_line, label="issue line", minimum=1)
        column = _location_number(raw_column, label="issue column", minimum=0)
    else:
        if code not in _PROJECT_CODES:
            raise ValueError("Deptry Python issue was reported on project configuration")
        if raw_line is not None or raw_column is not None:
            raise ValueError("Deptry project-level issue location must be null")
        line = None
        column = None
    classification, declared, runtime, dev, transitive = _issue_semantics(code)
    identity = external_signature(
        "deptry-issue-v1",
        {
            "provider_id": DEPTRY_PROVIDER_ID,
            "code": code,
            "module": module,
            "path": relative_path,
            "line": line,
            "column": column,
            "message": message,
        },
    )
    return _DependencyIssue(
        identity,
        code,
        module,
        relative_path,
        line,
        column,
        message,
        classification,
        location_kind,
        declared,
        runtime,
        dev,
        transitive,
        owner,
    )


def _finding(issue: _DependencyIssue) -> ExternalProviderFinding:
    if issue.owner is None or issue.line is None or issue.column is None:
        raise ValueError("Deptry file finding requires one exact Python owner")
    identity = external_signature(
        "external-finding-v1",
        {
            "provider_id": DEPTRY_PROVIDER_ID,
            "issue_identity": issue.identity,
        },
    )
    gate_authority = DEPENDENCY_DECLARATION_GATE if issue.classification == "gate" else "advisory"
    return ExternalProviderFinding(
        identity,
        issue.owner.version_id,
        issue.relative_path,
        DEPENDENCY_HYGIENE_CATEGORY,
        issue.code,
        "error" if issue.classification == "gate" else "warning",
        issue.message,
        True,
        1.0,
        None,
        gate_authority,
        issue.line,
        issue.column,
        issue.line,
        issue.column,
        metadata=issue.metadata(),
    )


def _project_metric(
    project_key: str,
    *,
    name: str,
    value: int,
    metadata: Mapping[str, object],
) -> ExternalProviderMetric:
    unit = "count"
    return ExternalProviderMetric(
        external_metric_identity(
            DEPTRY_PROVIDER_ID,
            subject_kind="project",
            subject_key=project_key,
            category=DEPENDENCY_HYGIENE_CATEGORY,
            metric_name=name,
            unit=unit,
        ),
        "project",
        project_key,
        DEPENDENCY_HYGIENE_CATEGORY,
        name,
        value,
        unit,
        metadata=metadata,
    )


def _counters(
    issues: tuple[_DependencyIssue, ...],
    *,
    duplicate_report_rows: int,
) -> dict[str, int]:
    codes = Counter(item.code for item in issues)
    return {
        "dependency_issue_count": len(issues),
        "dependency_duplicate_report_row_count": duplicate_report_rows,
        "dependency_gate_issue_count": sum(item.classification == "gate" for item in issues),
        "dependency_advisory_issue_count": sum(
            item.classification == "advisory" for item in issues
        ),
        "dependency_python_issue_count": sum(item.location_kind == "python" for item in issues),
        "dependency_project_issue_count": sum(item.location_kind == "project" for item in issues),
        **{
            f"dependency_{code.casefold()}_count": codes.get(code, 0)
            for code in sorted(_SUPPORTED_CODES)
        },
    }


def _deduplicate_report_issues(
    issues: tuple[_DependencyIssue, ...],
) -> tuple[tuple[_DependencyIssue, ...], int]:
    unique: dict[str, _DependencyIssue] = {}
    duplicate_rows = 0
    for issue in issues:
        previous = unique.setdefault(issue.identity, issue)
        if previous != issue:
            raise ValueError("Deptry JSON report issue identity collided")
        if previous is not issue:
            duplicate_rows += 1
    return tuple(sorted(unique.values(), key=lambda item: item.identity)), duplicate_rows


def _metrics(
    project_key: str,
    issues: tuple[_DependencyIssue, ...],
    counters: Mapping[str, int],
) -> tuple[ExternalProviderMetric, ...]:
    result: list[ExternalProviderMetric] = []
    for issue in issues:
        suffix = issue.identity.rsplit(":", 1)[-1]
        result.append(
            _project_metric(
                project_key,
                name=f"dependency_issue_{issue.code.casefold()}_{suffix}",
                value=1,
                metadata=issue.metadata(),
            )
        )
    for name, value in counters.items():
        aggregate_metadata: dict[str, object] = {
            "provider_schema": DEPTRY_PROVIDER_SCHEMA,
            "aggregate": True,
            "mutation_authority": False,
        }
        code_match = re.fullmatch(r"dependency_(dep00[1-5])_count", name)
        if code_match is not None:
            code = code_match.group(1).upper()
            aggregate_metadata.update(
                {
                    "code": code,
                    "classification": "gate" if code in _GATE_CODES else "advisory",
                }
            )
        result.append(
            _project_metric(
                project_key,
                name=name,
                value=value,
                metadata=aggregate_metadata,
            )
        )
    return tuple(sorted(result, key=lambda item: item.portable_metric_id))


def _module_from_relative(relative_path: str) -> str:
    path = PurePosixPath(relative_path)
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else "__root__"


def _relations(
    project_key: str,
    issues: tuple[_DependencyIssue, ...],
) -> tuple[ExternalProviderRelation, ...]:
    grouped: dict[str, list[_DependencyIssue]] = defaultdict(list)
    for issue in issues:
        if issue.owner is not None:
            grouped[_module_from_relative(issue.relative_path)].append(issue)
    result: list[ExternalProviderRelation] = []
    for module, module_issues in sorted(grouped.items()):
        codes = sorted({item.code for item in module_issues})
        dependency_modules = sorted({item.module for item in module_issues}, key=str.casefold)
        paths = sorted({item.relative_path for item in module_issues}, key=str.casefold)
        version_ids = sorted(
            {item.owner.version_id for item in module_issues if item.owner is not None}
        )
        metadata = {
            "provider_schema": DEPTRY_PROVIDER_SCHEMA,
            "issue_count": len(module_issues),
            "codes": codes,
            "dependency_modules": dependency_modules,
            "paths": paths,
            "version_ids": version_ids,
            "classification": (
                "gate"
                if any(item.classification == "gate" for item in module_issues)
                else "advisory"
            ),
            "mutation_authority": False,
        }
        relation_kind = "dependency_hygiene_scope"
        result.append(
            ExternalProviderRelation(
                external_relation_identity(
                    DEPTRY_PROVIDER_ID,
                    relation_kind=relation_kind,
                    source_kind="project",
                    source_key=project_key,
                    target_kind="module",
                    target_key=module,
                ),
                relation_kind,
                "project",
                project_key,
                "module",
                module,
                confidence=1.0,
                target_version_id=version_ids[0] if len(version_ids) == 1 else None,
                metadata=metadata,
            )
        )
    return tuple(sorted(result, key=lambda item: item.portable_relation_id))


def execute_deptry_dependency_hygiene(
    stage_root: Path,
    staged: Mapping[str, ExternalEvidenceFile],
    config_path: Path,
    environment: Mapping[str, str],
) -> DependencyHygieneExecution:
    """Run Deptry 0.25.x on the exact stage and normalize every DEP001..DEP005 issue."""

    source_root, owners = _validate_exact_stage(stage_root, staged)
    verified_config = _read_verified_config(config_path)
    _deptry_version()
    resolved_stage = stage_root.resolve(strict=True)
    resolved_config = config_path.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="neocortex-deptry-", dir=resolved_stage) as temporary:
        scratch = Path(temporary)
        scratch_config = scratch / _PYPROJECT_NAME
        report_path = scratch / "deptry-report.json"
        _write_verified_config(scratch_config, verified_config.raw)
        command = (
            sys.executable,
            "-I",
            "-m",
            "deptry",
            "source",
            "--config",
            str(scratch_config),
            "--json-output",
            str(report_path),
            "--no-ansi",
            "--optional-dependencies-dev-groups",
            "dev",
            "--package-module-name-map",
            _package_module_name_map_argument(),
            "--ignore-notebooks",
            "--exclude",
            _NEVER_EXCLUDE_PATTERN,
        )
        controlled_environment = dict(environment)
        controlled_environment.update(
            {
                "NO_COLOR": "1",
                "PIP_NO_INDEX": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONUTF8": "1",
                "UV_OFFLINE": "1",
            }
        )
        for name in ("TEMP", "TMP", "TMPDIR"):
            controlled_environment[name] = str(scratch)
        completed = run_bounded_capture(
            command,
            timeout_seconds=_TIMEOUT_SECONDS,
            stdout_limit_bytes=_MAX_STDOUT_BYTES,
            stderr_limit_bytes=_MAX_STDERR_BYTES,
            cwd=resolved_stage,
            environment=controlled_environment,
            memory_limit_bytes=_MEMORY_LIMIT_BYTES if os.name == "nt" else None,
        )
        if completed.returncode not in {0, 1}:
            raise _unexpected_exit(completed)
        raw_issues = _read_report(report_path)
        if bool(raw_issues) != (completed.returncode == 1):
            raise ValueError("Deptry exit status and JSON issue count disagree")
        config_paths = frozenset(
            {
                os.path.normcase(os.path.abspath(resolved_config)),
                os.path.normcase(os.path.abspath(scratch_config)),
            }
        )
        normalized_issues = tuple(
            sorted(
                (
                    _normalize_issue(
                        item,
                        stage_root=resolved_stage,
                        source_root=source_root,
                        config_paths=config_paths,
                        owners=owners,
                    )
                    for item in raw_issues
                ),
                key=lambda item: item.identity,
            )
        )
        issues, duplicate_report_rows = _deduplicate_report_issues(normalized_issues)
        _verify_stage_unchanged(owners)

    counters = _counters(issues, duplicate_report_rows=duplicate_report_rows)
    findings = tuple(
        sorted(
            (_finding(issue) for issue in issues if issue.owner is not None),
            key=lambda item: item.portable_finding_id,
        )
    )
    metrics = _metrics(verified_config.project_key, issues, counters)
    relations = _relations(verified_config.project_key, issues)
    return DependencyHygieneExecution(
        findings,
        metrics,
        relations,
        len(completed.stdout),
        len(completed.stderr),
        1,
        DEPTRY_LIMITATIONS,
        counters,
    )


__all__ = [
    "DEPENDENCY_DECLARATION_GATE",
    "DEPENDENCY_HYGIENE_CATEGORY",
    "DEPTRY_LIMITATIONS",
    "DEPTRY_PACKAGE_MODULE_NAME_MAP",
    "DEPTRY_PROVIDER_ID",
    "DEPTRY_PROVIDER_SCHEMA",
    "DependencyHygieneExecution",
    "execute_deptry_dependency_hygiene",
]
