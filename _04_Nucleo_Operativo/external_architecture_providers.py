"""Bounded adapters for external architecture and complexity evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from .bounded_subprocess import run_bounded_capture
from .code_architecture_contracts import (
    ARCHITECTURE_BASELINE_ID,
    ARCHITECTURE_CONTRACT_SCHEMA,
    PRODUCTION_ROOT_PACKAGES,
)
from .code.contracts.target_registry import CORE_RESPONSIBILITY_REGISTRY_SCHEMA
from .code_external_evidence import ExternalEvidenceFile
from .external_evidence_models import (
    ExternalProviderFinding,
    ExternalProviderMetric,
    ExternalProviderRelation,
    external_metric_identity,
    external_relation_identity,
    external_signature,
)

RUFF_ANALYZE_PROVIDER_ID = "ruff-analyze-imports"
GRIMP_ARCHITECTURE_PROVIDER_ID = "grimp-architecture"
COMPLEXIPY_COGNITIVE_PROVIDER_ID = "complexipy-cognitive"

_RUFF_SCHEMA = "neocortex.ruff-analyze-imports/v1"
_GRIMP_WORKER_SCHEMA = "neocortex.external-architecture-worker/grimp-v3"
_COMPLEXIPY_WORKER_SCHEMA = "neocortex.external-architecture-worker/complexipy-v1"
_TIMEOUT_SECONDS = 180.0
_STDOUT_LIMIT_BYTES = 8 * 1024 * 1024
_STDERR_LIMIT_BYTES = 128 * 1024
_RUFF_MEMORY_BYTES = 512 * 1024 * 1024
_WORKER_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
_MAX_FILES = 2_000
_MAX_INPUT_BYTES = 512 * 1024 * 1024
_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_PRODUCTION_ROOTS = frozenset(PRODUCTION_ROOT_PACKAGES)


@dataclass(frozen=True, slots=True)
class ArchitectureProviderExecution:
    findings: tuple[ExternalProviderFinding, ...]
    metrics: tuple[ExternalProviderMetric, ...]
    relations: tuple[ExternalProviderRelation, ...]
    stdout_bytes: int
    stderr_bytes: int
    process_invocations: int


def _unexpected_exit(
    tool: str,
    completed: subprocess.CompletedProcess[bytes],
) -> ValueError:
    raw = completed.stderr or completed.stdout
    detail = " ".join(raw.decode("utf-8", errors="replace").split())[:2048]
    message = f"{tool}_unexpected_exit:{completed.returncode}"
    return ValueError(message if not detail else f"{message}:{detail}")


def _decode_object(raw: bytes, *, owner: str) -> Mapping[str, object]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{owner} JSON output is malformed") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{owner} JSON output is not an object")
    return payload


def _required_list(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} is not a list")
    return value


def _required_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} is not an object")
    return value


def _required_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{label} is invalid")
    return value


def _required_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} is invalid")
    return value


def _owners_by_relative(
    staged: Mapping[str, ExternalEvidenceFile],
) -> dict[str, ExternalEvidenceFile]:
    result: dict[str, ExternalEvidenceFile] = {}
    for owner in staged.values():
        key = owner.relative_path.replace("\\", "/").casefold()
        if key in result:
            raise ValueError("architecture staged relative path is duplicated")
        result[key] = owner
    return result


def _relative_graph_path(
    value: str,
    *,
    stage_root: Path,
    staged: Mapping[str, ExternalEvidenceFile],
) -> str:
    candidate = Path(value)
    if candidate.is_absolute():
        key = os.path.normcase(os.path.abspath(candidate))
        owner = staged.get(key)
        if owner is None:
            raise ValueError("architecture tool reported an unowned absolute path")
        return owner.relative_path.replace("\\", "/")
    normalized = value.replace("\\", "/")
    parts = list(PurePosixPath(normalized).parts)
    while parts and parts[0] in {".", "source"}:
        parts.pop(0)
    relative = PurePosixPath(*parts).as_posix()
    owner = _owners_by_relative(staged).get(relative.casefold())
    if owner is None:
        source_candidate = stage_root / "source" / Path(*parts)
        owner = staged.get(os.path.normcase(os.path.abspath(source_candidate)))
    if owner is None:
        raise ValueError("architecture tool reported an unowned relative path")
    return owner.relative_path.replace("\\", "/")


def _module_from_relative(relative_path: str) -> str:
    path = PurePosixPath(relative_path)
    if path.suffix.casefold() not in {".py", ".pyi"}:
        raise ValueError("architecture relation path is not Python")
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts or parts[0] not in _PRODUCTION_ROOTS:
        raise ValueError("architecture relation escapes the production domain")
    return ".".join(parts)


def _metric(
    provider_id: str,
    *,
    subject_kind: Literal["symbol", "module", "run", "contract"],
    subject_key: str,
    category: str,
    name: str,
    value: int,
    version_id: int | None = None,
    metadata: Mapping[str, object] | None = None,
) -> ExternalProviderMetric:
    unit = "count"
    return ExternalProviderMetric(
        external_metric_identity(
            provider_id,
            subject_kind=subject_kind,
            subject_key=subject_key,
            category=category,
            metric_name=name,
            unit=unit,
        ),
        subject_kind,
        subject_key,
        category,
        name,
        value,
        unit,
        version_id=version_id,
        metadata={} if metadata is None else metadata,
    )


def _relation(
    provider_id: str,
    source: str,
    target: str,
    *,
    metadata: Mapping[str, object] | None = None,
) -> ExternalProviderRelation:
    return ExternalProviderRelation(
        external_relation_identity(
            provider_id,
            relation_kind="module_import",
            source_kind="module",
            source_key=source,
            target_kind="module",
            target_key=target,
        ),
        "module_import",
        "module",
        source,
        "module",
        target,
        confidence=1.0,
        metadata={} if metadata is None else metadata,
    )


def execute_ruff_analyze_imports(
    stage_root: Path,
    staged: Mapping[str, ExternalEvidenceFile],
    environment: Mapping[str, str],
) -> ArchitectureProviderExecution:
    """Run Ruff Analyze as an isolated differential import-graph oracle."""

    command = (
        sys.executable,
        "-I",
        "-m",
        "ruff",
        "analyze",
        "graph",
        "--quiet",
        "--isolated",
        "--target-version",
        "py313",
        "--no-preview",
        "--color",
        "never",
        "source",
    )
    completed = run_bounded_capture(
        command,
        timeout_seconds=_TIMEOUT_SECONDS,
        stdout_limit_bytes=_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes=_STDERR_LIMIT_BYTES,
        cwd=stage_root,
        environment=environment,
        memory_limit_bytes=_RUFF_MEMORY_BYTES if os.name == "nt" else None,
    )
    if completed.returncode != 0:
        raise _unexpected_exit("ruff_analyze", completed)
    payload = _decode_object(completed.stdout, owner="ruff analyze")
    relations: dict[str, ExternalProviderRelation] = {}
    for raw_source, raw_targets in sorted(payload.items(), key=lambda item: str(item[0])):
        source_path = _relative_graph_path(
            _required_text(raw_source, label="ruff graph source"),
            stage_root=stage_root,
            staged=staged,
        )
        source_module = _module_from_relative(source_path)
        for raw_target in _required_list(raw_targets, label="ruff graph targets"):
            target_path = _relative_graph_path(
                _required_text(raw_target, label="ruff graph target"),
                stage_root=stage_root,
                staged=staged,
            )
            target_module = _module_from_relative(target_path)
            item = _relation(
                RUFF_ANALYZE_PROVIDER_ID,
                source_module,
                target_module,
                metadata={
                    "provider_schema": _RUFF_SCHEMA,
                    "source_path": source_path,
                    "target_path": target_path,
                    "oracle": "differential",
                },
            )
            existing = relations.get(item.portable_relation_id)
            if existing is not None and existing != item:
                raise ValueError("ruff analyze relation identity collision")
            relations[item.portable_relation_id] = item
    return ArchitectureProviderExecution(
        (),
        (),
        tuple(sorted(relations.values(), key=lambda item: item.portable_relation_id)),
        len(completed.stdout),
        len(completed.stderr),
        1,
    )


def _execute_worker(
    mode: Literal["grimp", "complexipy"],
    stage_root: Path,
    environment: Mapping[str, str],
) -> tuple[Mapping[str, object], int, int]:
    worker = Path(__file__).with_name("external_architecture_worker.py").resolve(strict=True)
    command = (
        sys.executable,
        "-I",
        str(worker),
        mode,
        "--root",
        str(stage_root / "source"),
        "--max-files",
        str(_MAX_FILES),
        "--max-input-bytes",
        str(_MAX_INPUT_BYTES),
        "--max-output-bytes",
        str(_MAX_OUTPUT_BYTES),
    )
    completed = run_bounded_capture(
        command,
        timeout_seconds=_TIMEOUT_SECONDS,
        stdout_limit_bytes=_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes=_STDERR_LIMIT_BYTES,
        cwd=stage_root,
        environment=environment,
        memory_limit_bytes=_WORKER_MEMORY_BYTES if os.name == "nt" else None,
    )
    if completed.returncode != 0:
        raise _unexpected_exit(mode, completed)
    payload = _decode_object(completed.stdout, owner=mode)
    expected = _GRIMP_WORKER_SCHEMA if mode == "grimp" else _COMPLEXIPY_WORKER_SCHEMA
    if payload.get("schema") != expected or payload.get("status") != "ready":
        raise ValueError(f"{mode} worker contract is incompatible")
    inputs = _required_mapping(payload.get("inputs"), label=f"{mode} inputs")
    if _required_int(inputs.get("file_count"), label=f"{mode} input count") <= 0:
        raise ValueError(f"{mode} worker did not cover its production domain")
    return payload, len(completed.stdout), len(completed.stderr)


def _owner_for_relative(
    relative_path: object,
    staged: Mapping[str, ExternalEvidenceFile],
) -> ExternalEvidenceFile:
    path = _required_text(relative_path, label="architecture relative path")
    owner = _owners_by_relative(staged).get(path.replace("\\", "/").casefold())
    if owner is None:
        raise ValueError("architecture worker reported an unowned path")
    return owner


def _contract_finding(
    violation: Mapping[str, object],
    contract_id: str,
    staged: Mapping[str, ExternalEvidenceFile],
    module_paths: Mapping[str, str],
) -> ExternalProviderFinding:
    importer = _required_text(violation.get("importer"), label="violation importer")
    imported = _required_text(violation.get("imported"), label="violation imported")
    chain = tuple(
        _required_text(item, label="violation import chain")
        for item in _required_list(violation.get("import_chain"), label="violation chain")
    )
    if len(chain) < 2 or chain[0] != importer or chain[-1] != imported:
        raise ValueError("architecture violation chain is inconsistent")
    owner_path = module_paths.get(importer)
    if owner_path is None:
        raise ValueError("architecture violation importer has no owned path")
    owner = _owner_for_relative(owner_path, staged)
    details = _required_list(violation.get("details"), label="violation details")
    first_detail = _required_mapping(details[0], label="violation detail") if details else {}
    line = (
        1
        if not first_detail
        else _required_int(first_detail.get("line_number"), label="violation line")
    )
    message = _required_text(violation.get("message"), label="violation message")
    metadata = {
        "contract_schema": ARCHITECTURE_CONTRACT_SCHEMA,
        "architecture_baseline_id": ARCHITECTURE_BASELINE_ID,
        "contract_id": contract_id,
        "importer": importer,
        "imported": imported,
        "import_chain": list(chain),
        "details": details,
        "worker_metadata": violation.get("metadata") or {},
    }
    identity = external_signature(
        "external-finding-v1",
        {
            "provider_id": GRIMP_ARCHITECTURE_PROVIDER_ID,
            "path": owner.relative_path,
            "category": "architecture",
            "code": contract_id,
            "message": message,
            "start_line": line,
            "start_column": 0,
            "end_line": line,
            "end_column": 0,
        },
    )
    return ExternalProviderFinding(
        identity,
        owner.version_id,
        owner.relative_path,
        "architecture",
        contract_id,
        "warning",
        message,
        True,
        1.0,
        None,
        "architecture_contract",
        line,
        0,
        line,
        0,
        metadata=metadata,
    )


def _core_target_finding(
    *,
    module: str,
    code: str,
    message: str,
    staged: Mapping[str, ExternalEvidenceFile],
    module_paths: Mapping[str, str],
    metadata: Mapping[str, object],
) -> ExternalProviderFinding:
    owner_path = module_paths.get(module) or module_paths.get(
        "_04_Nucleo_Operativo.code.contracts.target_registry"
    )
    if owner_path is None:
        raise ValueError("Core target finding has no owned source module")
    owner = _owner_for_relative(owner_path, staged)
    identity = external_signature(
        "external-finding-v1",
        {
            "provider_id": GRIMP_ARCHITECTURE_PROVIDER_ID,
            "path": owner.relative_path,
            "category": "architecture",
            "code": code,
            "message": message,
            "start_line": 1,
            "start_column": 0,
            "end_line": 1,
            "end_column": 0,
        },
    )
    return ExternalProviderFinding(
        identity,
        owner.version_id,
        owner.relative_path,
        "architecture",
        code,
        "warning",
        message,
        True,
        1.0,
        None,
        "architecture_target_contract",
        1,
        0,
        1,
        0,
        metadata=dict(metadata),
    )


def _family_edge_source_module(
    family_payload: Mapping[str, object],
    source_family: str,
    target_family: str,
) -> str:
    for raw_edge in _required_list(
        family_payload.get("projected_edges"), label="Core projected family edges"
    ):
        edge = _required_mapping(raw_edge, label="Core projected family edge")
        if edge.get("source_label") != source_family or edge.get("target_label") != target_family:
            continue
        relations = _required_list(
            edge.get("module_relations"), label="Core projected module relations"
        )
        if not relations:
            break
        relation = _required_mapping(relations[0], label="Core projected module relation")
        return _required_text(relation.get("source_module"), label="Core projected source module")
    return "_04_Nucleo_Operativo.code.contracts.target_registry"


@dataclass(frozen=True, slots=True)
class _CoreTargetProjection:
    fingerprint: str
    scope: Mapping[str, object]
    responsibility: Mapping[str, object]
    family: Mapping[str, object]


def _read_core_target_projection(payload: Mapping[str, object]) -> _CoreTargetProjection:
    projections = _required_mapping(payload.get("projections"), label="Grimp projections")
    target = _required_mapping(projections.get("core_target"), label="Core target projection")
    registry = _required_mapping(target.get("registry"), label="Core target registry")
    if registry.get("schema") != CORE_RESPONSIBILITY_REGISTRY_SCHEMA:
        raise ValueError("Core target registry schema is incompatible")
    return _CoreTargetProjection(
        _required_text(registry.get("fingerprint"), label="Core target registry fingerprint"),
        _required_mapping(target.get("scope"), label="Core target scope"),
        _required_mapping(
            target.get("target_responsibility"),
            label="Core responsibility projection",
        ),
        _required_mapping(target.get("target_family"), label="Core family projection"),
    )


def _core_target_metric_values(target: _CoreTargetProjection) -> dict[str, int]:
    responsibility = _required_mapping(
        target.responsibility.get("counters"), label="Core responsibility counters"
    )
    family = _required_mapping(target.family.get("counters"), label="Core family counters")
    scope_lists = {
        "registered_module_count": ("registered_modules", "registered Core modules"),
        "missing_registered_module_count": (
            "missing_registered_modules",
            "missing registered Core modules",
        ),
        "unregistered_core_module_count": (
            "unregistered_core_modules",
            "unregistered Core modules",
        ),
        "compatibility_module_count": (
            "compatibility_modules",
            "Core compatibility modules",
        ),
    }
    values = {
        name: len(_required_list(target.scope.get(field), label=label))
        for name, (field, label) in scope_lists.items()
    }
    counter_fields = {
        "responsibility_unmapped_module_count": (
            responsibility,
            "unmapped_modules",
            "Core responsibility unmapped modules",
        ),
        "responsibility_overlapping_module_count": (
            responsibility,
            "overlapping_modules",
            "Core responsibility overlapping modules",
        ),
        "family_unmapped_module_count": (
            family,
            "unmapped_modules",
            "Core family unmapped modules",
        ),
        "family_overlapping_module_count": (
            family,
            "overlapping_modules",
            "Core family overlapping modules",
        ),
        "forbidden_family_direct_edge_count": (
            family,
            "forbidden_direct_module_edges",
            "Core forbidden family edges",
        ),
        "baseline_forbidden_family_direct_edge_count": (
            family,
            "baseline_forbidden_direct_module_edges",
            "Core baseline forbidden family edges",
        ),
        "family_regression_direct_edge_count": (
            family,
            "regression_direct_module_edges",
            "Core family regression edges",
        ),
        "family_resolved_direct_edge_count": (
            family,
            "resolved_direct_module_edges",
            "Core family resolved edges",
        ),
        "canonical_to_compat_direct_edge_count": (
            family,
            "canonical_to_compat_direct_module_edges",
            "Core canonical-to-compat edges",
        ),
    }
    values.update(
        {
            name: _required_int(counters.get(field), label=label)
            for name, (counters, field, label) in counter_fields.items()
        }
    )
    return values


def _core_mapping_findings(
    projection_name: str,
    projection: Mapping[str, object],
    *,
    staged: Mapping[str, ExternalEvidenceFile],
    module_paths: Mapping[str, str],
) -> list[ExternalProviderFinding]:
    findings: list[ExternalProviderFinding] = []
    resolutions = _required_list(
        projection.get("mapping_resolutions"),
        label=f"Core {projection_name} mapping resolutions",
    )
    for raw_resolution in resolutions:
        resolution = _required_mapping(
            raw_resolution, label=f"Core {projection_name} mapping resolution"
        )
        status = resolution.get("status")
        module = _required_text(resolution.get("module_id"), label="Core mapped module")
        if status not in {"unmapped", "overlap"} or not module.startswith("_04_Nucleo_Operativo"):
            continue
        findings.append(
            _core_target_finding(
                module=module,
                code=f"core_target_{projection_name}_{status}",
                message=f"Core module {module} has {status} {projection_name} ownership",
                staged=staged,
                module_paths=module_paths,
                metadata={"module": module, "projection": projection_name},
            )
        )
    return findings


def _core_family_transition_findings(
    family: Mapping[str, object],
    *,
    staged: Mapping[str, ExternalEvidenceFile],
    module_paths: Mapping[str, str],
) -> list[ExternalProviderFinding]:
    findings: list[ExternalProviderFinding] = []
    comparisons = _required_list(
        family.get("transition_baseline"), label="Core family transition baseline"
    )
    for raw_comparison in comparisons:
        comparison = _required_mapping(raw_comparison, label="Core family baseline comparison")
        regression = _required_int(
            comparison.get("regression_direct_module_edges"),
            label="Core family edge regression",
        )
        if regression == 0:
            continue
        source = _required_text(
            comparison.get("source_family"), label="Core regression source family"
        )
        target = _required_text(
            comparison.get("target_family"), label="Core regression target family"
        )
        findings.append(
            _core_target_finding(
                module=_family_edge_source_module(family, source, target),
                code="core_target_family_regression",
                message=(
                    f"Core family dependency {source} -> {target} increased "
                    f"by {regression} direct module edges"
                ),
                staged=staged,
                module_paths=module_paths,
                metadata=dict(comparison),
            )
        )
    return findings


def _core_canonical_to_compat_findings(
    family: Mapping[str, object],
    *,
    staged: Mapping[str, ExternalEvidenceFile],
    module_paths: Mapping[str, str],
) -> list[ExternalProviderFinding]:
    findings: list[ExternalProviderFinding] = []
    decisions = _required_list(family.get("edge_decisions"), label="Core family edge decisions")
    for raw_decision in decisions:
        decision = _required_mapping(raw_decision, label="Core family edge decision")
        if decision.get("reason") != "canonical_to_compat":
            continue
        source = _required_text(decision.get("source_family"), label="Core canonical source family")
        target = _required_text(
            decision.get("target_family"), label="Core compatibility target family"
        )
        findings.append(
            _core_target_finding(
                module=_family_edge_source_module(family, source, target),
                code="core_target_canonical_to_compat",
                message=f"Canonical Core family {source} depends on {target}",
                staged=staged,
                module_paths=module_paths,
                metadata=dict(decision),
            )
        )
    return findings


def _core_target_evidence(
    payload: Mapping[str, object],
    staged: Mapping[str, ExternalEvidenceFile],
    module_paths: Mapping[str, str],
) -> tuple[list[ExternalProviderFinding], list[ExternalProviderMetric]]:
    target = _read_core_target_projection(payload)
    metrics = [
        _metric(
            GRIMP_ARCHITECTURE_PROVIDER_ID,
            subject_kind="contract",
            subject_key=target.fingerprint,
            category="architecture_target",
            name=name,
            value=value,
            metadata={"registry_schema": CORE_RESPONSIBILITY_REGISTRY_SCHEMA},
        )
        for name, value in sorted(_core_target_metric_values(target).items())
    ]
    findings = _core_mapping_findings(
        "responsibility",
        target.responsibility,
        staged=staged,
        module_paths=module_paths,
    )
    findings.extend(
        _core_mapping_findings(
            "family",
            target.family,
            staged=staged,
            module_paths=module_paths,
        )
    )
    findings.extend(
        _core_family_transition_findings(
            target.family,
            staged=staged,
            module_paths=module_paths,
        )
    )
    findings.extend(
        _core_canonical_to_compat_findings(
            target.family,
            staged=staged,
            module_paths=module_paths,
        )
    )
    return findings, metrics


def execute_grimp_architecture(
    stage_root: Path,
    staged: Mapping[str, ExternalEvidenceFile],
    environment: Mapping[str, str],
) -> ArchitectureProviderExecution:
    """Run the isolated Grimp worker and normalize graph/contract evidence."""

    payload, stdout_bytes, stderr_bytes = _execute_worker("grimp", stage_root, environment)
    raw_modules = _required_list(payload.get("module_metrics"), label="grimp module metrics")
    raw_cycles = _required_list(payload.get("cycles"), label="grimp cycles")
    cycle_sizes: dict[str, int] = {}
    for raw_cycle in raw_cycles:
        cycle = _required_mapping(raw_cycle, label="grimp cycle")
        modules = tuple(
            _required_text(item, label="grimp cycle module")
            for item in _required_list(cycle.get("modules"), label="grimp cycle modules")
        )
        for module in modules:
            cycle_sizes[module] = max(cycle_sizes.get(module, 0), len(modules))
    metrics: list[ExternalProviderMetric] = []
    module_paths: dict[str, str] = {}
    for raw_module in raw_modules:
        item = _required_mapping(raw_module, label="grimp module metric")
        module = _required_text(item.get("module"), label="grimp module")
        if module.partition(".")[0] not in _PRODUCTION_ROOTS:
            raise ValueError("grimp module escapes the production domain")
        relative = item.get("relative_path")
        owner = None if relative is None else _owner_for_relative(relative, staged)
        if owner is not None:
            module_paths[module] = owner.relative_path
        cycle_ids = _required_list(item.get("cycle_ids"), label="grimp cycle ids")
        values = (
            ("module_fan_in", _required_int(item.get("fan_in"), label="grimp fan in")),
            ("module_fan_out", _required_int(item.get("fan_out"), label="grimp fan out")),
            ("module_scc_size", cycle_sizes.get(module, 0)),
            ("module_cycle_membership", int(bool(cycle_ids))),
        )
        for name, value in values:
            metrics.append(
                _metric(
                    GRIMP_ARCHITECTURE_PROVIDER_ID,
                    subject_kind="module",
                    subject_key=module,
                    category="architecture",
                    name=name,
                    value=value,
                    version_id=None if owner is None else owner.version_id,
                    metadata={"relative_path": relative, "cycle_ids": cycle_ids},
                )
            )
    counters = _required_mapping(payload.get("counters"), label="grimp counters")
    for name, key in (
        ("internal_module_count", "modules"),
        ("internal_import_edge_count", "production_relations"),
        ("cyclic_scc_count", "cyclic_components"),
    ):
        metrics.append(
            _metric(
                GRIMP_ARCHITECTURE_PROVIDER_ID,
                subject_kind="run",
                subject_key="production",
                category="architecture",
                name=name,
                value=_required_int(counters.get(key), label=f"grimp {key}"),
                metadata={"architecture_contract_schema": ARCHITECTURE_CONTRACT_SCHEMA},
            )
        )
    relations: list[ExternalProviderRelation] = []
    for raw_relation in _required_list(payload.get("relations"), label="grimp relations"):
        item = _required_mapping(raw_relation, label="grimp relation")
        if item.get("relation") != "module_import":
            raise ValueError("grimp relation kind is incompatible")
        importer = _required_text(item.get("importer"), label="grimp importer")
        imported = _required_text(item.get("imported"), label="grimp imported")
        relations.append(
            _relation(
                GRIMP_ARCHITECTURE_PROVIDER_ID,
                importer,
                imported,
                metadata={"details": item.get("details") or []},
            )
        )
    findings: list[ExternalProviderFinding] = []
    for raw_evaluation in _required_list(
        payload.get("contract_evaluations"), label="grimp contract evaluations"
    ):
        evaluation = _required_mapping(raw_evaluation, label="grimp contract evaluation")
        contract = _required_mapping(evaluation.get("contract"), label="grimp contract")
        contract_id = _required_text(contract.get("contract_id"), label="contract id")
        violations = _required_list(evaluation.get("violations"), label="contract violations")
        for name, value in (
            ("architecture_contract_evaluated", 1),
            ("architecture_contract_violations", len(violations)),
        ):
            metrics.append(
                _metric(
                    GRIMP_ARCHITECTURE_PROVIDER_ID,
                    subject_kind="contract",
                    subject_key=contract_id,
                    category="architecture",
                    name=name,
                    value=value,
                    metadata={
                        "contract_schema": ARCHITECTURE_CONTRACT_SCHEMA,
                        "status": evaluation.get("status"),
                        "authority": contract.get("authority"),
                    },
                )
            )
        findings.extend(
            _contract_finding(
                _required_mapping(item, label="architecture violation"),
                contract_id,
                staged,
                module_paths,
            )
            for item in violations
        )
    target_findings, target_metrics = _core_target_evidence(
        payload,
        staged,
        module_paths,
    )
    findings.extend(target_findings)
    metrics.extend(target_metrics)
    return ArchitectureProviderExecution(
        tuple(sorted(findings, key=lambda item: item.portable_finding_id)),
        tuple(sorted(metrics, key=lambda item: item.portable_metric_id)),
        tuple(sorted(relations, key=lambda item: item.portable_relation_id)),
        stdout_bytes,
        stderr_bytes,
        1,
    )


def execute_complexipy_cognitive(
    stage_root: Path,
    staged: Mapping[str, ExternalEvidenceFile],
    environment: Mapping[str, str],
) -> ArchitectureProviderExecution:
    """Run Complexipy through the isolated worker and normalize its metrics."""

    payload, stdout_bytes, stderr_bytes = _execute_worker("complexipy", stage_root, environment)
    metrics: list[ExternalProviderMetric] = []
    for raw_module in _required_list(
        payload.get("module_metrics"), label="complexipy module metrics"
    ):
        item = _required_mapping(raw_module, label="complexipy module metric")
        module = _required_text(item.get("module"), label="complexipy module")
        owner = _owner_for_relative(item.get("relative_path"), staged)
        metadata = {
            "relative_path": owner.relative_path,
            "function_count": _required_int(
                item.get("function_count"), label="complexipy function count"
            ),
        }
        for name, key in (
            ("module_cognitive_complexity_total", "total"),
            ("module_cognitive_complexity_max", "maximum"),
        ):
            metrics.append(
                _metric(
                    COMPLEXIPY_COGNITIVE_PROVIDER_ID,
                    subject_kind="module",
                    subject_key=module,
                    category="complexity",
                    name=name,
                    value=_required_int(item.get(key), label=f"complexipy {key}"),
                    version_id=owner.version_id,
                    metadata=metadata,
                )
            )
    for raw_function in _required_list(
        payload.get("function_metrics"), label="complexipy function metrics"
    ):
        item = _required_mapping(raw_function, label="complexipy function metric")
        module = _required_text(item.get("module"), label="complexipy function module")
        symbol = _required_text(item.get("symbol"), label="complexipy symbol")
        start = _required_int(item.get("start_line"), label="complexipy start line")
        end = _required_int(item.get("end_line"), label="complexipy end line")
        owner = _owner_for_relative(item.get("relative_path"), staged)
        subject_key = f"{module}:{symbol}:{start}:{end}"
        metrics.append(
            _metric(
                COMPLEXIPY_COGNITIVE_PROVIDER_ID,
                subject_kind="symbol",
                subject_key=subject_key,
                category="complexity",
                name="cognitive_complexity",
                value=_required_int(item.get("value"), label="complexipy complexity"),
                version_id=owner.version_id,
                metadata={
                    "module": module,
                    "symbol": symbol,
                    "relative_path": owner.relative_path,
                    "start_line": start,
                    "end_line": end,
                    "scope": item.get("scope"),
                    "lines": item.get("lines") or [],
                },
            )
        )
    return ArchitectureProviderExecution(
        (),
        tuple(sorted(metrics, key=lambda item: item.portable_metric_id)),
        (),
        stdout_bytes,
        stderr_bytes,
        1,
    )


__all__ = [
    "COMPLEXIPY_COGNITIVE_PROVIDER_ID",
    "GRIMP_ARCHITECTURE_PROVIDER_ID",
    "RUFF_ANALYZE_PROVIDER_ID",
    "ArchitectureProviderExecution",
    "execute_complexipy_cognitive",
    "execute_grimp_architecture",
    "execute_ruff_analyze_imports",
]
