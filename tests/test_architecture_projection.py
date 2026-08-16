"""Focused contracts for the reusable, data-only architecture projection core."""

from __future__ import annotations

import pytest

from _04_Nucleo_Operativo.platform.shared.architecture_projection import (
    AGGREGATE_QUOTIENT_SCC_SEMANTICS,
    REALIZABLE_SCC_SEMANTICS,
    ArchitectureProjection,
    FamilyDag,
    FamilyDependency,
    ModuleEdge,
    analyze_module_graph,
    evaluate_family_dag,
    project_module_graph,
    resolve_exact_mapping,
)


def _projection(
    modules: tuple[str, ...],
    edges: tuple[ModuleEdge, ...],
    matches: dict[str, tuple[str, ...]],
    *,
    scope: tuple[str, ...] | None = None,
) -> ArchitectureProjection:
    graph = analyze_module_graph(modules, edges)
    mapping = resolve_exact_mapping(graph.modules, matches, in_scope_modules=scope)
    return project_module_graph(graph, mapping)


def test_disconnected_bidirectional_edges_are_only_an_aggregate_quotient_scc() -> None:
    projection = _projection(
        ("a.one", "a.two", "b.one", "b.two"),
        (
            ModuleEdge("a.one", "b.one", "edge-a1-b1"),
            ModuleEdge("b.two", "a.two", "edge-b2-a2"),
        ),
        {
            "a.one": ("owner-a",),
            "a.two": ("owner-a",),
            "b.one": ("owner-b",),
            "b.two": ("owner-b",),
        },
    )

    assert projection.graph.cyclic_sccs == ()
    assert projection.realizable_sccs == ()
    assert len(projection.aggregate_quotient_sccs) == 1
    quotient = projection.aggregate_quotient_sccs[0]
    assert quotient.labels == ("owner-a", "owner-b")
    assert quotient.shortest_cycle == ("owner-a", "owner-b", "owner-a")
    assert quotient.semantics == AGGREGATE_QUOTIENT_SCC_SEMANTICS
    assert quotient.realizable_module_components == ()
    assert {
        witness
        for edge in quotient.internal_edges
        for witness in edge.witness_ids
    } == {"edge-a1-b1", "edge-b2-a2"}


def test_real_module_cycle_projects_to_a_realizable_owner_scc_and_links_quotient() -> None:
    projection = _projection(
        ("a.one", "b.one"),
        (
            ModuleEdge("b.one", "a.one", "edge-b-a"),
            ModuleEdge("a.one", "b.one", "edge-a-b"),
            ModuleEdge("a.one", "b.one", "edge-a-b-second-witness"),
        ),
        {"a.one": ("owner-a",), "b.one": ("owner-b",)},
    )

    assert len(projection.graph.cyclic_sccs) == 1
    module_scc = projection.graph.cyclic_sccs[0]
    assert module_scc.modules == ("a.one", "b.one")
    assert module_scc.shortest_cycle == ("a.one", "b.one", "a.one")
    assert module_scc.shortest_cycle_relations[0].witness_ids == (
        "edge-a-b",
        "edge-a-b-second-witness",
    )
    assert len(projection.realizable_sccs) == 1
    assert projection.realizable_sccs[0].labels == ("owner-a", "owner-b")
    assert projection.realizable_sccs[0].semantics == REALIZABLE_SCC_SEMANTICS
    assert projection.aggregate_quotient_sccs[0].realizable_module_components == (
        ("a.one", "b.one"),
    )


def test_module_self_loop_remains_realizable_without_fabricating_quotient_coupling() -> None:
    projection = _projection(
        ("a.one",),
        (ModuleEdge("a.one", "a.one", "self-import"),),
        {"a.one": ("owner-a",)},
    )

    assert projection.graph.cyclic_sccs[0].shortest_cycle == ("a.one", "a.one")
    assert projection.realizable_sccs[0].labels == ("owner-a",)
    assert projection.aggregate_quotient_sccs == ()
    assert projection.projected_edges[0].source_label == "owner-a"
    assert projection.projected_edges[0].target_label == "owner-a"
    assert projection.projected_edges[0].witness_ids == ("self-import",)


def test_unmapped_overlap_and_out_of_scope_stay_explicit_without_a_default() -> None:
    modules = ("mapped", "outside", "overlap", "unmapped")
    projection = _projection(
        modules,
        (
            ModuleEdge("mapped", "unmapped", "mapped-unmapped"),
            ModuleEdge("unmapped", "mapped", "unmapped-mapped"),
            ModuleEdge("overlap", "mapped", "overlap-mapped"),
            ModuleEdge("outside", "mapped", "outside-mapped"),
        ),
        {
            "mapped": ("family-a",),
            "overlap": ("family-b", "family-c"),
            "outside": ("ignored-outside-family",),
        },
        scope=("mapped", "overlap", "unmapped"),
    )

    assert tuple(item.module_id for item in projection.mapping.unmapped) == ("unmapped",)
    assert tuple(item.module_id for item in projection.mapping.overlapping) == ("overlap",)
    assert projection.mapping.overlapping[0].labels == ("family-b", "family-c")
    assert tuple(item.module_id for item in projection.mapping.out_of_scope) == ("outside",)
    assert projection.mapping.for_module("outside").labels == ()
    assert projection.realizable_sccs == ()
    assert projection.unresolved_sccs[0].unmapped_modules == ("unmapped",)
    assert {
        (item.relation.source_module, item.source_status, item.target_status)
        for item in projection.unresolved_relations
    } == {
        ("mapped", "resolved", "unmapped"),
        ("outside", "out_of_scope", "resolved"),
        ("overlap", "overlap", "resolved"),
        ("unmapped", "unmapped", "resolved"),
    }


def test_family_dag_distinguishes_reachable_forbidden_and_canonical_compat_edges() -> None:
    modules = (
        "base.from",
        "compat.from",
        "compat.to",
        "top.from",
        "top.same",
        "top.to",
    )
    projection = _projection(
        modules,
        (
            ModuleEdge("top.from", "top.same", "same-family"),
            ModuleEdge("top.from", "base.from", "transitive-allowed"),
            ModuleEdge("base.from", "top.to", "reverse-forbidden"),
            ModuleEdge("top.from", "compat.to", "canonical-compat"),
            ModuleEdge("compat.from", "base.from", "compat-canonical"),
        ),
        {
            "base.from": ("base",),
            "compat.from": ("compat",),
            "compat.to": ("compat",),
            "top.from": ("top",),
            "top.same": ("top",),
            "top.to": ("top",),
        },
    )
    dag = FamilyDag(
        ("top", "middle", "base", "compat"),
        (
            FamilyDependency("top", "middle"),
            FamilyDependency("middle", "base"),
            FamilyDependency("compat", "top"),
        ),
        ("compat",),
    )

    evaluation = evaluate_family_dag(projection, dag)
    decisions = {
        (item.edge.source_label, item.edge.target_label): (item.allowed, item.reason)
        for item in evaluation.decisions
    }
    assert decisions == {
        ("base", "top"): (False, "forbidden_dependency"),
        ("compat", "base"): (True, "allowed_by_dag"),
        ("top", "base"): (True, "allowed_by_dag"),
        ("top", "compat"): (False, "canonical_to_compat"),
        ("top", "top"): (True, "allowed_same_family"),
    }
    assert evaluation.dag_forbidden_edges[0].edge.witness_ids == ("reverse-forbidden",)
    assert evaluation.canonical_to_compat_edges[0].edge.witness_ids == (
        "canonical-compat",
    )
    assert evaluation.is_accepted is False


def test_family_policy_rejects_cycles_unknown_nodes_and_canonical_to_compat_paths() -> None:
    with pytest.raises(ValueError, match="must be acyclic"):
        FamilyDag(
            ("a", "b"),
            (FamilyDependency("a", "b"), FamilyDependency("b", "a")),
        )
    with pytest.raises(ValueError, match="unknown family"):
        FamilyDag(("a",), (FamilyDependency("a", "missing"),))
    with pytest.raises(ValueError, match="canonical-to-compat"):
        FamilyDag(
            ("canonical", "compat"),
            (FamilyDependency("canonical", "compat"),),
            ("compat",),
        )


def test_projection_is_deterministic_for_reordered_inputs_and_mapping_candidates() -> None:
    modules = ("a.one", "a.two", "b.one", "b.two", "overlap")
    edges = (
        ModuleEdge("a.one", "b.one", "witness-2"),
        ModuleEdge("b.two", "a.two", "witness-3"),
        ModuleEdge("a.one", "b.one", "witness-1"),
    )
    first = _projection(
        modules,
        edges,
        {
            "a.one": ("a",),
            "a.two": ("a",),
            "b.one": ("b",),
            "b.two": ("b",),
            "overlap": ("z", "y"),
        },
    )
    second = _projection(
        tuple(reversed(modules)),
        tuple(reversed(edges)),
        {
            "overlap": ("y", "z"),
            "b.two": ("b",),
            "b.one": ("b",),
            "a.two": ("a",),
            "a.one": ("a",),
        },
    )

    assert first == second
    edge = next(
        item
        for item in first.projected_edges
        if (item.source_label, item.target_label) == ("a", "b")
    )
    assert edge.witness_ids == ("witness-1", "witness-2")
