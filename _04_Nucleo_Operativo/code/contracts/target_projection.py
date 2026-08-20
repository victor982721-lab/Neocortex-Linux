"""Pure projection of a module graph onto the exhaustive Core target.

The isolated Grimp worker passes its generic graph/projection implementation
and the data-only target registry explicitly.  This module therefore imports no
NeoCortex package at import time and remains safe to load beside a staged source
tree without executing that tree.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

ProjectionPayload = Callable[..., dict[str, object]]
ProjectedEdgeId = Callable[[str, Any], str]


def _core_family_dag(projection: Any, registry: Any) -> Any:
    return projection.FamilyDag(
        families=registry.TARGET_FAMILY_LAYERS,
        direct_dependencies=tuple(
            projection.FamilyDependency(source, target)
            for source, target in registry.TARGET_FAMILY_DEPENDENCIES
        ),
        compat_families=("compat",),
    )


def _registered_labels(
    registry: Any,
) -> tuple[
    tuple[str, ...],
    dict[str, tuple[str, ...]],
    dict[str, tuple[str, ...]],
]:
    modules = registry.registered_core_modules()
    responsibilities = {
        module: registry.matching_target_responsibilities(module) for module in modules
    }
    families = {module: registry.matching_target_families(module) for module in modules}
    return modules, responsibilities, families


def _family_decision_payloads(
    evaluation: Any,
    projected_edge_id: ProjectedEdgeId,
) -> list[dict[str, object]]:
    return [
        {
            "edge_id": projected_edge_id("core_target_family", item.edge),
            "source_family": item.edge.source_label,
            "target_family": item.edge.target_label,
            "allowed": item.allowed,
            "reason": item.reason,
            "direct_module_edges": len(item.edge.module_relations),
            "witness_ids": list(item.edge.witness_ids),
        }
        for item in evaluation.decisions
    ]


def _forbidden_family_counts(evaluation: Any) -> dict[tuple[str, str], int]:
    return {
        (item.edge.source_label, item.edge.target_label): len(item.edge.module_relations)
        for item in evaluation.dag_forbidden_edges
    }


def _family_baseline_comparison(
    current: Mapping[tuple[str, str], int],
    registry: Any,
) -> list[dict[str, object]]:
    baseline = registry.forbidden_family_edge_baseline()
    return [
        {
            "source_family": source,
            "target_family": target,
            "baseline_direct_module_edges": baseline.get((source, target), 0),
            "current_direct_module_edges": current.get((source, target), 0),
            "regression_direct_module_edges": max(
                0,
                current.get((source, target), 0) - baseline.get((source, target), 0),
            ),
            "resolved_direct_module_edges": max(
                0,
                baseline.get((source, target), 0) - current.get((source, target), 0),
            ),
        }
        for source, target in sorted(set(baseline) | set(current))
    ]


def _payload_int(payload: Mapping[str, object], field: str) -> int:
    value = payload.get(field)
    if type(value) is not int:
        raise ValueError(f"Core target {field} is not an integer")
    return value


def _core_scope_modules(modules: Sequence[str], registry: Any) -> tuple[str, ...]:
    root = registry.CORE_MODULE_ROOT
    return tuple(
        sorted(module for module in modules if module == root or module.startswith(root + "."))
    )


def build_core_target_projection(
    modules: Sequence[str],
    production_imports: Sequence[Any],
    *,
    projection: Any,
    registry: Any,
    projection_payload: ProjectionPayload,
    projected_edge_id: ProjectedEdgeId,
) -> dict[str, object]:
    registered, responsibility_matches, family_matches = _registered_labels(registry)
    module_set = set(modules)
    registered_set = set(registered)
    compatibility_set = set(registry.COMPATIBILITY_MODULES)
    core_modules = _core_scope_modules(modules, registry)
    present_registered = tuple(sorted(registered_set & module_set))
    missing_registered = tuple(sorted(registered_set - module_set))
    unregistered_core = tuple(sorted(set(core_modules) - registered_set))
    graph = projection.analyze_module_graph(
        modules,
        (
            projection.ModuleEdge(item.importer, item.imported, item.relation_id)
            for item in production_imports
        ),
    )
    responsibility_mapping = projection.resolve_exact_mapping(
        graph.modules,
        responsibility_matches,
        in_scope_modules=tuple(sorted(set(core_modules) - compatibility_set)),
    )
    family_mapping = projection.resolve_exact_mapping(
        graph.modules,
        family_matches,
        in_scope_modules=core_modules,
    )
    responsibility_projection = projection.project_module_graph(
        graph,
        responsibility_mapping,
    )
    family_projection = projection.project_module_graph(graph, family_mapping)
    family_evaluation = projection.evaluate_family_dag(
        family_projection,
        _core_family_dag(projection, registry),
    )
    responsibility = projection_payload(
        responsibility_projection,
        label_kind="target_responsibility",
    )
    responsibility["resolution_policy"] = "exhaustive-core-module-to-responsibility-v1"
    family = projection_payload(family_projection, label_kind="core_target_family")
    decisions = _family_decision_payloads(family_evaluation, projected_edge_id)
    current_forbidden = _forbidden_family_counts(family_evaluation)
    comparison = _family_baseline_comparison(current_forbidden, registry)
    canonical_to_compat = sum(
        _payload_int(item, "direct_module_edges")
        for item in decisions
        if item["reason"] == "canonical_to_compat"
    )
    family.update(
        {
            "resolution_policy": "exhaustive-core-module-to-responsibility-v1",
            "dag": registry.core_architecture_target_payload()["family_dag"],
            "edge_decisions": decisions,
            "transition_baseline": comparison,
        }
    )
    family_counters = family["counters"]
    if not isinstance(family_counters, dict):
        raise ValueError("Core target family counters are invalid")
    family_counters.update(
        {
            "forbidden_direct_module_edges": sum(current_forbidden.values()),
            "baseline_forbidden_direct_module_edges": sum(
                item.direct_module_edges for item in registry.FORBIDDEN_FAMILY_EDGE_BASELINE
            ),
            "regression_direct_module_edges": sum(
                _payload_int(item, "regression_direct_module_edges") for item in comparison
            ),
            "resolved_direct_module_edges": sum(
                _payload_int(item, "resolved_direct_module_edges") for item in comparison
            ),
            "canonical_to_compat_direct_module_edges": canonical_to_compat,
        }
    )
    return {
        "schema": projection.ARCHITECTURE_PROJECTION_SCHEMA,
        "policy_id": "neocortex.core-target-projection/v1",
        "registry": {
            "schema": registry.CORE_RESPONSIBILITY_REGISTRY_SCHEMA,
            "fingerprint": registry.core_architecture_target_fingerprint(),
        },
        "scope": {
            "registered_modules": list(registered),
            "present_registered_modules": list(present_registered),
            "missing_registered_modules": list(missing_registered),
            "unregistered_core_modules": list(unregistered_core),
            "compatibility_modules": list(registry.COMPATIBILITY_MODULES),
        },
        "target_responsibility": responsibility,
        "target_family": family,
    }


__all__ = ["build_core_target_projection"]
