"""Isolated JSON worker for static architecture and complexity evidence.

Providers execute this file directly with isolated Python.  Executing the file
rather than ``python -m`` keeps the installed ``_04_Nucleo_Operativo`` package
out of ``sys.modules`` while Grimp locates the staged package with the same
name.  Project content is parsed statically and is never imported.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import stat
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from . import code_architecture_contracts as _contracts
    from .platform.shared import architecture_projection as _projection
    from .platform.shared import capability_registry as _capability_registry
elif __package__:
    from . import code_architecture_contracts as _contracts
    from .platform.shared import architecture_projection as _projection
    from .platform.shared import capability_registry as _capability_registry
else:  # Direct isolated worker execution; do not import the staged package.
    def _load_control_plane_module(alias: str, path: Path) -> Any:
        spec = importlib.util.spec_from_file_location(alias, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"architecture control-plane module is unavailable: {path.name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(spec.name, None)
            raise
        return module

    _control_plane_root = Path(__file__).parent
    _contracts = _load_control_plane_module(
        "_neocortex_code_architecture_contracts",
        _control_plane_root / "code_architecture_contracts.py",
    )
    _projection = _load_control_plane_module(
        "_neocortex_architecture_projection",
        _control_plane_root / "platform" / "shared" / "architecture_projection.py",
    )
    _capability_registry = _load_control_plane_module(
        "_neocortex_capability_registry",
        _control_plane_root / "platform" / "shared" / "capability_registry.py",
    )

GRIMP_WORKER_SCHEMA = "neocortex.external-architecture-worker/grimp-v2"
COMPLEXIPY_WORKER_SCHEMA = "neocortex.external-architecture-worker/complexipy-v1"
WORKER_ERROR_SCHEMA = "neocortex.external-architecture-worker/error-v1"

CAPABILITY_PROJECTION_POLICY_ID = (
    "neocortex.capability-architecture-projection/transitional-v1"
)
CAPABILITY_PROJECTION_SCOPE_POLICY = (
    "exact-capability-registry-modules-canonical-and-legacy-v1"
)
CAPABILITY_OWNER_RESOLUTION_POLICY = "capability-logical-owner-exact-v1"
CAPABILITY_FAMILY_RESOLUTION_POLICY = (
    "canonical-target-or-exact-source-compatibility-v1"
)
CAPABILITY_FAMILY_DAG_SCHEMA = "neocortex.architecture-family-dag/v1"
CAPABILITY_FAMILY_DAG_POLICY_ID = "neocortex.formats-family-dependencies/transitional-v1"
CAPABILITY_FAMILY_DAG_FINGERPRINT_PREFIX = "architecture-family-dag-v1:sha256:"
CAPABILITY_CANONICAL_FAMILY = "_04.capabilities.formats"
CAPABILITY_COMPATIBILITY_FAMILY = "_04.compat.formats"

DEFAULT_MAX_FILES = 4096
DEFAULT_MAX_INPUT_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
HARD_MAX_FILES = 16_384
HARD_MAX_INPUT_BYTES = 512 * 1024 * 1024
HARD_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
_MAX_IMPORT_LINE_CHARS = 1000
_EXCLUDED_PATH_PARTS = frozenset((*_contracts.EXCLUDED_ARCHITECTURE_NAMESPACES, "__pycache__"))


class WorkerContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class WorkerLimits:
    max_files: int
    max_input_bytes: int
    max_output_bytes: int

    def __post_init__(self) -> None:
        if not 1 <= self.max_files <= HARD_MAX_FILES:
            raise WorkerContractError("invalid_limit", "max-files is outside its hard bound")
        if not 1 <= self.max_input_bytes <= HARD_MAX_INPUT_BYTES:
            raise WorkerContractError("invalid_limit", "max-input-bytes is outside its hard bound")
        if not 1024 <= self.max_output_bytes <= HARD_MAX_OUTPUT_BYTES:
            raise WorkerContractError("invalid_limit", "max-output-bytes is outside its hard bound")

    def as_payload(self) -> dict[str, int]:
        return {
            "max_files": self.max_files,
            "max_input_bytes": self.max_input_bytes,
            "max_output_bytes": self.max_output_bytes,
        }


@dataclass(frozen=True, slots=True)
class StagedPythonFile:
    path: Path
    relative_path: str
    module: str
    size: int
    sha256: str


def _is_reparse_point(path: Path) -> bool:
    metadata = os.lstat(path)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(int(getattr(metadata, "st_file_attributes", 0)) & reparse)


def _inside_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _module_from_relative_path(relative_path: str) -> str:
    parts = relative_path.split("/")
    filename = parts.pop()
    if filename == "__init__.py":
        return ".".join(parts)
    if not filename.endswith(".py"):
        raise WorkerContractError("unsupported_input", "architecture input is not Python")
    return ".".join((*parts, filename[:-3]))


def _read_stable_file(path: Path, *, expected_size: int | None = None) -> bytes:
    before = os.lstat(path)
    if _is_reparse_point(path) or not stat.S_ISREG(before.st_mode):
        raise WorkerContractError("unsafe_input", "architecture input is not a regular file")
    if expected_size is not None and before.st_size != expected_size:
        raise WorkerContractError("input_changed", "architecture input changed during analysis")
    raw = path.read_bytes()
    after = os.lstat(path)
    if (
        len(raw) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise WorkerContractError("input_changed", "architecture input changed during analysis")
    return raw


def _validate_root(root: Path) -> Path:
    absolute = root.absolute()
    if not absolute.exists() or not absolute.is_dir() or _is_reparse_point(absolute):
        raise WorkerContractError("invalid_root", "staged root must be a regular directory")
    resolved = absolute.resolve(strict=True)
    for package in _contracts.PRODUCTION_ROOT_PACKAGES:
        package_root = resolved / package
        initializer = package_root / "__init__.py"
        if (
            not package_root.is_dir()
            or _is_reparse_point(package_root)
            or not initializer.is_file()
            or _is_reparse_point(initializer)
        ):
            raise WorkerContractError(
                "missing_production_package",
                f"exact production package is unavailable: {package}",
            )
    return resolved


def _collect_inputs(root: Path, limits: WorkerLimits) -> tuple[StagedPythonFile, ...]:
    candidates: list[Path] = []
    checked_directories: set[Path] = set()
    for package in _contracts.PRODUCTION_ROOT_PACKAGES:
        package_root = root / package
        for path in package_root.rglob("*.py"):
            relative = path.relative_to(root)
            if any(part in _EXCLUDED_PATH_PARTS for part in relative.parts[:-1]):
                continue
            resolved = path.resolve(strict=True)
            if not _inside_root(resolved, root):
                raise WorkerContractError("unsafe_input", "architecture input escapes staged root")
            for parent in (path.parent, *path.parents):
                if parent == root:
                    break
                if parent in checked_directories:
                    continue
                checked_directories.add(parent)
                if _is_reparse_point(parent):
                    raise WorkerContractError(
                        "unsafe_input", "architecture input traverses a reparse point"
                    )
            candidates.append(path)
    candidates.sort(key=lambda item: item.relative_to(root).as_posix())
    if len(candidates) > limits.max_files:
        raise WorkerContractError("input_file_bound_exceeded", "Python file bound exceeded")

    total_bytes = 0
    inputs: list[StagedPythonFile] = []
    for path in candidates:
        metadata = os.lstat(path)
        if metadata.st_size < 0:
            raise WorkerContractError("unsafe_input", "architecture input has invalid size")
        total_bytes += metadata.st_size
        if total_bytes > limits.max_input_bytes:
            raise WorkerContractError("input_byte_bound_exceeded", "Python byte bound exceeded")
        raw = _read_stable_file(path, expected_size=metadata.st_size)
        relative_path = path.relative_to(root).as_posix()
        inputs.append(
            StagedPythonFile(
                path,
                relative_path,
                _module_from_relative_path(relative_path),
                len(raw),
                hashlib.sha256(raw).hexdigest(),
            )
        )
    if not inputs:
        raise WorkerContractError("empty_domain", "production Python domain is empty")
    return tuple(inputs)


def _validate_inputs_unchanged(inputs: Sequence[StagedPythonFile]) -> None:
    for item in inputs:
        raw = _read_stable_file(item.path, expected_size=item.size)
        if hashlib.sha256(raw).hexdigest() != item.sha256:
            raise WorkerContractError("input_changed", "architecture input changed during analysis")


def _input_manifest(inputs: Sequence[StagedPythonFile]) -> dict[str, object]:
    digest = hashlib.sha256()
    for item in inputs:
        digest.update(item.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(item.sha256.encode("ascii"))
        digest.update(b"\n")
    return {
        "file_count": len(inputs),
        "total_bytes": sum(item.size for item in inputs),
        "content_manifest_sha256": digest.hexdigest(),
    }


@contextmanager
def _staged_import_path(root: Path) -> Iterator[None]:
    value = os.fspath(root)
    sys.path.insert(0, value)
    try:
        yield
    finally:
        try:
            sys.path.remove(value)
        except ValueError:
            pass


def _tool_version(distribution: str) -> str:
    try:
        version = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError as error:
        raise WorkerContractError(
            "tool_unavailable", f"required static tool is unavailable: {distribution}"
        ) from error
    if not version or len(version.encode("utf-8")) > 256:
        raise WorkerContractError("invalid_tool_version", "static tool version is invalid")
    return version


def _normalize_import_details(
    raw_details: Iterable[Mapping[str, object]],
) -> tuple[_contracts.ImportLineDetail, ...]:
    details: set[_contracts.ImportLineDetail] = set()
    for raw in raw_details:
        line_number = raw.get("line_number")
        if not isinstance(line_number, int) or line_number < 1:
            continue
        line_contents = raw.get("line_contents")
        normalized = " ".join(str(line_contents or "").split())[:_MAX_IMPORT_LINE_CHARS]
        details.add(_contracts.ImportLineDetail(line_number, normalized))
    return tuple(sorted(details))


def _module_path(module: str, inputs_by_module: Mapping[str, StagedPythonFile]) -> str | None:
    item = inputs_by_module.get(module)
    return None if item is None else item.relative_path


def _grimp_imports(graph: Any, modules: Sequence[str]) -> tuple[_contracts.ModuleImport, ...]:
    imports: list[_contracts.ModuleImport] = []
    for importer in modules:
        try:
            imported_modules = graph.find_modules_directly_imported_by(importer)
        except Exception as error:  # Grimp owns the concrete extension exception types.
            raise WorkerContractError("grimp_query_failed", "Grimp import query failed") from error
        for imported in sorted(imported_modules):
            if not (
                _contracts.is_production_module(imported)
                or imported.partition(".")[0] in _contracts.EXCLUDED_ARCHITECTURE_NAMESPACES
            ):
                continue
            try:
                raw_details = graph.get_import_details(importer=importer, imported=imported)
            except Exception as error:
                raise WorkerContractError(
                    "grimp_query_failed", "Grimp detail query failed"
                ) from error
            imports.append(
                _contracts.ModuleImport(importer, imported, _normalize_import_details(raw_details))
            )
    return tuple(sorted(imports, key=lambda item: (item.importer, item.imported)))


def _cycle_payloads(
    modules: Sequence[str], imports: Sequence[_contracts.ModuleImport]
) -> tuple[dict[str, object], ...]:
    known = set(_contracts.KNOWN_CYCLE_BASELINE)
    payloads: list[dict[str, object]] = []
    for component in _contracts.cyclic_strongly_connected_components(modules, imports):
        cycle_chain = _contracts.shortest_cycle_chain(component, imports)
        payloads.append(
            {
                "cycle_id": _contracts.stable_architecture_id("import-cycle-v1", *component),
                "modules": list(component),
                "module_count": len(component),
                "shortest_cycle_chain": list(cycle_chain),
                "baseline_state": "known_baseline" if component in known else "new",
            }
        )
    return tuple(payloads)


def _capability_family_dag() -> Any:
    canonical_families = {
        item.architecture_family_id
        for item in _capability_registry.CAPABILITY_REGISTRY.capabilities
    }
    compatibility_families = {
        item.compatibility_family_id
        for item in _capability_registry.CAPABILITY_REGISTRY.capabilities
    }
    if canonical_families != {CAPABILITY_CANONICAL_FAMILY} or compatibility_families != {
        CAPABILITY_COMPATIBILITY_FAMILY
    }:
        raise WorkerContractError(
            "projection_policy_drift",
            "capability families drifted from the transitional dependency policy",
        )
    return _projection.FamilyDag(
        families=(CAPABILITY_CANONICAL_FAMILY, CAPABILITY_COMPATIBILITY_FAMILY),
        direct_dependencies=(
            _projection.FamilyDependency(
                CAPABILITY_COMPATIBILITY_FAMILY,
                CAPABILITY_CANONICAL_FAMILY,
            ),
        ),
        compat_families=(CAPABILITY_COMPATIBILITY_FAMILY,),
    )


def capability_family_dag_manifest() -> dict[str, object]:
    """Return the independently versioned transitional family policy."""

    dag = _capability_family_dag()
    contract: dict[str, object] = {
        "schema": CAPABILITY_FAMILY_DAG_SCHEMA,
        "policy_id": CAPABILITY_FAMILY_DAG_POLICY_ID,
        "edge_semantics": "importer-may-depend-on-reachable-dependency-v1",
        "families": list(dag.families),
        "direct_dependencies": [
            {
                "source_family": item.source_family,
                "target_family": item.target_family,
            }
            for item in dag.direct_dependencies
        ],
        "compat_families": list(dag.compat_families),
    }
    canonical = json.dumps(
        contract,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        **contract,
        "fingerprint": (
            CAPABILITY_FAMILY_DAG_FINGERPRINT_PREFIX
            + hashlib.sha256(canonical).hexdigest()
        ),
    }


def _registered_capability_labels() -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    dict[str, tuple[str, ...]],
    dict[str, tuple[str, ...]],
]:
    canonical_modules: set[str] = set()
    legacy_modules: set[str] = set()
    owner_matches: dict[str, set[str]] = {}
    family_matches: dict[str, set[str]] = {}
    for capability in _capability_registry.CAPABILITY_REGISTRY.capabilities:
        for binding in capability.modules:
            canonical_modules.add(binding.canonical_module_id)
            owner_matches.setdefault(binding.canonical_module_id, set()).add(
                capability.logical_owner_id
            )
            family_matches.setdefault(binding.canonical_module_id, set()).add(
                capability.architecture_family_id
            )
            if binding.legacy_module_id is None:
                continue
            legacy_modules.add(binding.legacy_module_id)
            owner_matches.setdefault(binding.legacy_module_id, set()).add(
                capability.logical_owner_id
            )
            family_matches.setdefault(binding.legacy_module_id, set()).add(
                capability.compatibility_family_id
            )
    if canonical_modules & legacy_modules:
        raise WorkerContractError(
            "projection_registry_overlap",
            "canonical and legacy capability module scopes overlap",
        )
    return (
        tuple(sorted(canonical_modules)),
        tuple(sorted(legacy_modules)),
        {module: tuple(sorted(labels)) for module, labels in sorted(owner_matches.items())},
        {module: tuple(sorted(labels)) for module, labels in sorted(family_matches.items())},
    )


def _module_relation_payload(relation: Any) -> dict[str, object]:
    return {
        "source_module": relation.source_module,
        "target_module": relation.target_module,
        "witness_ids": list(relation.witness_ids),
    }


def _module_cycle_id(component: Any) -> str:
    return _contracts.stable_architecture_id("import-cycle-v1", *component.modules)


def _module_scc_payload(component: Any) -> dict[str, object]:
    return {
        "cycle_id": _module_cycle_id(component),
        "modules": list(component.modules),
        "module_count": len(component.modules),
        "shortest_cycle_chain": list(component.shortest_cycle),
        "internal_relations": [
            _module_relation_payload(item) for item in component.internal_relations
        ],
        "shortest_cycle_relations": [
            _module_relation_payload(item) for item in component.shortest_cycle_relations
        ],
    }


def _projected_edge_id(label_kind: str, edge: Any) -> str:
    return _contracts.stable_architecture_id(
        f"{label_kind}-projected-edge-v1",
        edge.source_label,
        edge.target_label,
    )


def _projected_edge_payload(label_kind: str, edge: Any) -> dict[str, object]:
    return {
        "edge_id": _projected_edge_id(label_kind, edge),
        "source_label": edge.source_label,
        "target_label": edge.target_label,
        "module_relations": [
            _module_relation_payload(item) for item in edge.module_relations
        ],
        "witness_ids": list(edge.witness_ids),
    }


def _projection_payload(projection: Any, *, label_kind: str) -> dict[str, object]:
    mapping_resolutions = [
        {
            "module_id": item.module_id,
            "status": item.status,
            "labels": list(item.labels),
        }
        for item in projection.mapping.resolutions
    ]
    projected_edges = [
        _projected_edge_payload(label_kind, item) for item in projection.projected_edges
    ]
    unresolved_relations = [
        {
            "relation": _module_relation_payload(item.relation),
            "source_status": item.source_status,
            "target_status": item.target_status,
        }
        for item in projection.unresolved_relations
    ]
    realizable_sccs = [
        {
            "module_cycle_id": _module_cycle_id(item.module_scc),
            "modules": list(item.module_scc.modules),
            "labels": list(item.labels),
            "semantics": item.semantics,
            "authority": "gate",
        }
        for item in projection.realizable_sccs
    ]
    unresolved_sccs = [
        {
            "module_cycle_id": _module_cycle_id(item.module_scc),
            "modules": list(item.module_scc.modules),
            "unmapped_modules": list(item.unmapped_modules),
            "overlapping_modules": list(item.overlapping_modules),
            "out_of_scope_modules": list(item.out_of_scope_modules),
            "authority": "module-cycle-gate",
        }
        for item in projection.unresolved_sccs
    ]
    aggregate_sccs = [
        {
            "aggregate_scc_id": _contracts.stable_architecture_id(
                f"{label_kind}-aggregate-quotient-scc-v1", *item.labels
            ),
            "labels": list(item.labels),
            "shortest_cycle": list(item.shortest_cycle),
            "internal_edge_ids": [
                _projected_edge_id(label_kind, edge) for edge in item.internal_edges
            ],
            "shortest_cycle_edge_ids": [
                _projected_edge_id(label_kind, edge)
                for edge in item.shortest_cycle_edges
            ],
            "realizable_module_components": [
                list(component) for component in item.realizable_module_components
            ],
            "semantics": item.semantics,
            "authority": "diagnostic",
        }
        for item in projection.aggregate_quotient_sccs
    ]
    status_counts = {
        status: sum(item["status"] == status for item in mapping_resolutions)
        for status in ("resolved", "unmapped", "overlap", "out_of_scope")
    }
    return {
        "label_kind": label_kind,
        "mapping_resolutions": mapping_resolutions,
        "projected_edges": projected_edges,
        "unresolved_relations": unresolved_relations,
        "realizable_sccs": realizable_sccs,
        "unresolved_sccs": unresolved_sccs,
        "aggregate_quotient_sccs": aggregate_sccs,
        "counters": {
            "resolved_modules": status_counts["resolved"],
            "unmapped_modules": status_counts["unmapped"],
            "overlapping_modules": status_counts["overlap"],
            "out_of_scope_modules": status_counts["out_of_scope"],
            "projected_edges": len(projected_edges),
            "unresolved_relations": len(unresolved_relations),
            "realizable_sccs": len(realizable_sccs),
            "unresolved_sccs": len(unresolved_sccs),
            "aggregate_quotient_sccs": len(aggregate_sccs),
        },
    }


def _capability_projection_payload(
    modules: Sequence[str], production_imports: Sequence[_contracts.ModuleImport]
) -> dict[str, object]:
    canonical_modules, legacy_modules, owner_matches, family_matches = (
        _registered_capability_labels()
    )
    registered_modules = tuple(sorted((*canonical_modules, *legacy_modules)))
    registered_set = set(registered_modules)
    present_modules = tuple(sorted(registered_set & set(modules)))
    missing_modules = tuple(sorted(registered_set - set(modules)))
    graph = _projection.analyze_module_graph(
        modules,
        (
            _projection.ModuleEdge(item.importer, item.imported, item.relation_id)
            for item in production_imports
        ),
    )
    owner_mapping = _projection.resolve_exact_mapping(
        graph.modules,
        owner_matches,
        in_scope_modules=present_modules,
    )
    family_mapping = _projection.resolve_exact_mapping(
        graph.modules,
        family_matches,
        in_scope_modules=present_modules,
    )
    owner_projection = _projection.project_module_graph(graph, owner_mapping)
    family_projection = _projection.project_module_graph(graph, family_mapping)
    family_evaluation = _projection.evaluate_family_dag(
        family_projection,
        _capability_family_dag(),
    )
    owner_payload = _projection_payload(owner_projection, label_kind="logical_owner")
    owner_payload["resolution_policy"] = CAPABILITY_OWNER_RESOLUTION_POLICY
    family_payload = _projection_payload(family_projection, label_kind="target_family")
    decisions = [
        {
            "edge_id": _projected_edge_id("target_family", item.edge),
            "source_family": item.edge.source_label,
            "target_family": item.edge.target_label,
            "allowed": item.allowed,
            "reason": item.reason,
            "witness_ids": list(item.edge.witness_ids),
        }
        for item in family_evaluation.decisions
    ]
    forbidden_ids = [str(item["edge_id"]) for item in decisions if item["allowed"] is False]
    canonical_to_compat_ids = [
        str(item["edge_id"])
        for item in decisions
        if item["reason"] == "canonical_to_compat"
    ]
    family_payload.update(
        {
            "resolution_policy": CAPABILITY_FAMILY_RESOLUTION_POLICY,
            "dag": capability_family_dag_manifest(),
            "edge_decisions": decisions,
            "forbidden_edge_ids": forbidden_ids,
            "canonical_to_compat_edge_ids": canonical_to_compat_ids,
        }
    )
    family_counters = family_payload["counters"]
    if not isinstance(family_counters, dict):
        raise WorkerContractError(
            "internal_contract_error", "family projection counters are invalid"
        )
    family_counters.update(
        {
            "edge_decisions": len(decisions),
            "forbidden_edges": len(forbidden_ids),
            "canonical_to_compat_edges": len(canonical_to_compat_ids),
        }
    )
    return {
        "schema": _projection.ARCHITECTURE_PROJECTION_SCHEMA,
        "policy_id": CAPABILITY_PROJECTION_POLICY_ID,
        "capability_registry": {
            "schema": _capability_registry.CAPABILITY_REGISTRY_SCHEMA,
            "fingerprint": _capability_registry.capability_registry_fingerprint(),
        },
        "scope": {
            "policy": CAPABILITY_PROJECTION_SCOPE_POLICY,
            "canonical_modules": list(canonical_modules),
            "legacy_modules": list(legacy_modules),
            "registered_modules": list(registered_modules),
            "present_registered_modules": list(present_modules),
            "missing_registered_modules": list(missing_modules),
        },
        "module_graph": {
            "semantics": "directed-production-module-import-scc-v1",
            "cyclic_sccs": [
                _module_scc_payload(item) for item in graph.cyclic_sccs
            ],
        },
        "logical_owner": owner_payload,
        "target_family": family_payload,
    }


def analyze_grimp(root: Path, limits: WorkerLimits) -> dict[str, object]:
    inputs = _collect_inputs(root, limits)
    version = _tool_version("grimp")
    try:
        import grimp
    except ImportError as error:
        raise WorkerContractError("tool_unavailable", "Grimp cannot be imported") from error

    already_loaded = [name for name in _contracts.PRODUCTION_ROOT_PACKAGES if name in sys.modules]
    if already_loaded:
        raise WorkerContractError(
            "unsafe_worker_invocation",
            "worker must execute directly so staged packages remain unloaded",
        )
    try:
        with _staged_import_path(root):
            graph = grimp.build_graph(
                *_contracts.PRODUCTION_ROOT_PACKAGES,
                include_external_packages=True,
                exclude_type_checking_imports=False,
                cache_dir=None,
            )
    except Exception as error:
        raise WorkerContractError("grimp_analysis_failed", "Grimp graph build failed") from error
    if any(name in sys.modules for name in _contracts.PRODUCTION_ROOT_PACKAGES):
        raise WorkerContractError(
            "target_content_imported", "static graph build imported staged project content"
        )

    modules = tuple(
        sorted(module for module in graph.modules if _contracts.is_production_module(module))
    )
    imports = _grimp_imports(graph, modules)
    production_imports = tuple(
        item for item in imports if _contracts.is_production_module(item.imported)
    )
    incoming: dict[str, int] = dict.fromkeys(modules, 0)
    outgoing: dict[str, int] = dict.fromkeys(modules, 0)
    for item in production_imports:
        outgoing[item.importer] += 1
        incoming[item.imported] += 1
    cycles = _cycle_payloads(modules, production_imports)
    cycle_ids: dict[str, list[str]] = {module: [] for module in modules}
    for cycle in cycles:
        raw_modules = cycle["modules"]
        if not isinstance(raw_modules, list):
            raise WorkerContractError("internal_contract_error", "cycle modules are invalid")
        for module in raw_modules:
            cycle_ids[str(module)].append(str(cycle["cycle_id"]))
    inputs_by_module = {item.module: item for item in inputs}
    module_metrics = [
        {
            "module": module,
            "relative_path": _module_path(module, inputs_by_module),
            "fan_in": incoming[module],
            "fan_out": outgoing[module],
            "cycle_ids": sorted(cycle_ids[module]),
        }
        for module in modules
    ]
    evaluations = _contracts.evaluate_architecture_contracts(modules, imports)
    projections = _capability_projection_payload(modules, production_imports)
    _validate_inputs_unchanged(inputs)
    return {
        "schema": GRIMP_WORKER_SCHEMA,
        "status": "ready",
        "mode": "grimp",
        "tool": {"name": "grimp", "version": version, "api": "build_graph"},
        "analysis_contract": {
            "static_only": True,
            "imports_project_content": False,
            "executes_project_content": False,
            "uses_network": False,
            "cache": "disabled",
            "include_external_packages": True,
            "exclude_type_checking_imports": False,
            "comparable_relations": "production_to_production_module_import",
        },
        "limits": limits.as_payload(),
        "inputs": _input_manifest(inputs),
        "architecture": _contracts.architecture_contract_manifest(),
        "counters": {
            "modules": len(modules),
            "production_relations": len(production_imports),
            "policy_only_external_relations": len(imports) - len(production_imports),
            "cyclic_components": len(cycles),
            "contract_violations": sum(len(item.violations) for item in evaluations),
        },
        "module_metrics": module_metrics,
        "relations": [item.as_payload() for item in production_imports],
        "cycles": list(cycles),
        "projections": projections,
        "contract_evaluations": [item.as_payload() for item in evaluations],
    }


def _complexity_lines(function: Any) -> list[dict[str, int]]:
    contributions = {
        (int(item.line), int(item.complexity))
        for item in function.line_complexities
        if int(item.complexity) != 0
    }
    return [{"line": line, "complexity": complexity} for line, complexity in sorted(contributions)]


def _required_metric_int(metric: Mapping[str, object], field: str) -> int:
    value = metric.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkerContractError("internal_contract_error", "complexity metric is invalid")
    return value


def _function_metric_order(metric: Mapping[str, object]) -> tuple[str, int, int, str]:
    return (
        str(metric["relative_path"]),
        _required_metric_int(metric, "start_line"),
        _required_metric_int(metric, "end_line"),
        str(metric["symbol"]),
    )


def analyze_complexipy(root: Path, limits: WorkerLimits) -> dict[str, object]:
    inputs = _collect_inputs(root, limits)
    version = _tool_version("complexipy")
    try:
        import complexipy
    except ImportError as error:
        raise WorkerContractError("tool_unavailable", "complexipy cannot be imported") from error

    module_metrics: list[dict[str, object]] = []
    function_metrics: list[dict[str, object]] = []
    for item in inputs:
        try:
            result = complexipy.file_complexity(
                os.fspath(item.path), check_script=True, no_ignore=True
            )
        except Exception as error:
            raise WorkerContractError(
                "complexipy_analysis_failed",
                f"complexipy could not analyze {item.relative_path}",
            ) from error
        functions = sorted(
            result.functions,
            key=lambda value: (
                int(value.line_start),
                int(value.line_end),
                str(value.name),
            ),
        )
        values = [int(function.complexity) for function in functions]
        module_total = int(result.complexity)
        if module_total != sum(values):
            raise WorkerContractError(
                "complexipy_contract_mismatch",
                "complexipy file total does not equal its function observations",
            )
        module_metrics.append(
            {
                "metric_id": _contracts.stable_architecture_id(
                    "cognitive-complexity-module-v1", item.relative_path
                ),
                "metric": "cognitive_complexity",
                "scope": "module",
                "module": item.module,
                "relative_path": item.relative_path,
                "total": module_total,
                "maximum": max(values, default=0),
                "function_count": len(functions),
            }
        )
        for function in functions:
            name = str(function.name)
            start_line = int(function.line_start)
            end_line = int(function.line_end)
            value = int(function.complexity)
            function_metrics.append(
                {
                    "metric_id": _contracts.stable_architecture_id(
                        "cognitive-complexity-symbol-v1",
                        item.relative_path,
                        name,
                        start_line,
                        end_line,
                    ),
                    "metric": "cognitive_complexity",
                    "scope": "module_script" if name == "<module>" else "symbol",
                    "module": item.module,
                    "relative_path": item.relative_path,
                    "symbol": name,
                    "start_line": start_line,
                    "end_line": end_line,
                    "value": value,
                    "lines": _complexity_lines(function),
                }
            )
    _validate_inputs_unchanged(inputs)
    return {
        "schema": COMPLEXIPY_WORKER_SCHEMA,
        "status": "ready",
        "mode": "complexipy",
        "tool": {"name": "complexipy", "version": version, "api": "file_complexity"},
        "analysis_contract": {
            "static_only": True,
            "imports_project_content": False,
            "executes_project_content": False,
            "uses_network": False,
            "loads_project_configuration": False,
            "check_script": True,
            "no_ignore": True,
            "snapshot": False,
            "diff": False,
            "autofix": False,
        },
        "limits": limits.as_payload(),
        "inputs": _input_manifest(inputs),
        "architecture_contract_schema": _contracts.ARCHITECTURE_CONTRACT_SCHEMA,
        "architecture_baseline_id": _contracts.ARCHITECTURE_BASELINE_ID,
        "counters": {
            "modules": len(module_metrics),
            "function_observations": len(function_metrics),
            "cognitive_complexity_total": sum(
                _required_metric_int(item, "total") for item in module_metrics
            ),
        },
        "module_metrics": sorted(module_metrics, key=lambda value: str(value["module"])),
        "function_metrics": sorted(
            function_metrics,
            key=_function_metric_order,
        ),
    }


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _emit(payload: Mapping[str, object], max_output_bytes: int) -> None:
    encoded = _canonical_json(payload)
    if len(encoded) > max_output_bytes:
        error = {
            "schema": WORKER_ERROR_SCHEMA,
            "status": "error",
            "error": {
                "code": "output_byte_bound_exceeded",
                "message": "architecture worker output exceeds its declared bound",
                "required_bytes": len(encoded),
                "max_output_bytes": max_output_bytes,
            },
        }
        encoded = _canonical_json(error)
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.write(b"\n")
        raise SystemExit(2)
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.write(b"\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("mode", choices=("grimp", "complexipy"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-input-bytes", type=int, default=DEFAULT_MAX_INPUT_BYTES)
    parser.add_argument("--max-output-bytes", type=int, default=DEFAULT_MAX_OUTPUT_BYTES)
    return parser


def _fail(error: WorkerContractError, max_output_bytes: int) -> NoReturn:
    payload = {
        "schema": WORKER_ERROR_SCHEMA,
        "status": "error",
        "error": {"code": error.code, "message": str(error)},
    }
    encoded = _canonical_json(payload)
    if len(encoded) <= max_output_bytes:
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.write(b"\n")
    raise SystemExit(2)


def main(arguments: Sequence[str] | None = None) -> int:
    namespace = _parser().parse_args(arguments)
    try:
        limits = WorkerLimits(
            namespace.max_files,
            namespace.max_input_bytes,
            namespace.max_output_bytes,
        )
        root = _validate_root(namespace.root)
        if namespace.mode == "grimp":
            payload = analyze_grimp(root, limits)
        else:
            payload = analyze_complexipy(root, limits)
        _emit(payload, limits.max_output_bytes)
    except WorkerContractError as error:
        _fail(error, max(1024, min(namespace.max_output_bytes, HARD_MAX_OUTPUT_BYTES)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
