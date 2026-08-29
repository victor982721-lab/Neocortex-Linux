"""Deterministic, data-only projections of directed module architecture graphs.

This module deliberately knows nothing about NeoCortex's concrete registries or
import-analysis implementations.  Callers provide module nodes, witnessed edges,
and the exact zero/one/many label matches produced by a versioned registry.

Two cycle semantics remain separate:

* a realizable cycle is an SCC of the original module graph, projected only
  after that SCC has been established; and
* an aggregate quotient SCC is an SCC after all exactly mapped modules sharing
  a label have been collapsed.  Its witness edges need not compose into a
  module path and therefore it is diagnostic rather than proof of a realizable
  import cycle.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

ARCHITECTURE_PROJECTION_SCHEMA = "neocortex.architecture-projection/v1"
AGGREGATE_QUOTIENT_SCC_SEMANTICS = (
    "aggregate_quotient_dependency_cycle_noncomposable-v1"
)
REALIZABLE_SCC_SEMANTICS = "projection_of_realizable_module_scc-v1"

MappingStatus = Literal["resolved", "unmapped", "overlap", "out_of_scope"]
FamilyDecisionReason = Literal[
    "allowed_same_family",
    "allowed_by_dag",
    "forbidden_dependency",
    "canonical_to_compat",
]


def _required_identifier(label: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError(f"{label} must be a non-blank, single-line identifier")
    return value


@dataclass(frozen=True, slots=True, order=True)
class ModuleEdge:
    """One independently witnessed directed module import."""

    source_module: str
    target_module: str
    witness_id: str

    def __post_init__(self) -> None:
        _required_identifier("source module", self.source_module)
        _required_identifier("target module", self.target_module)
        _required_identifier("edge witness", self.witness_id)


@dataclass(frozen=True, slots=True, order=True)
class ModuleRelation:
    """One canonical module edge with every supplied witness preserved."""

    source_module: str
    target_module: str
    witness_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _required_identifier("source module", self.source_module)
        _required_identifier("target module", self.target_module)
        if not self.witness_ids:
            raise ValueError("module relation requires at least one witness")
        if self.witness_ids != tuple(sorted(set(self.witness_ids))):
            raise ValueError("module relation witnesses must be unique and canonical")
        for witness_id in self.witness_ids:
            _required_identifier("edge witness", witness_id)


@dataclass(frozen=True, slots=True)
class CyclicModuleScc:
    """A cyclic SCC proven directly in the module graph."""

    modules: tuple[str, ...]
    shortest_cycle: tuple[str, ...]
    internal_relations: tuple[ModuleRelation, ...]
    shortest_cycle_relations: tuple[ModuleRelation, ...]

    def __post_init__(self) -> None:
        if not self.modules or self.modules != tuple(sorted(set(self.modules))):
            raise ValueError("module SCC members must be non-empty, unique, and canonical")
        if len(self.shortest_cycle) < 2 or self.shortest_cycle[0] != self.shortest_cycle[-1]:
            raise ValueError("module SCC shortest cycle must be a closed chain")
        if len(self.shortest_cycle_relations) != len(self.shortest_cycle) - 1:
            raise ValueError("module SCC shortest-cycle witnesses are incomplete")


@dataclass(frozen=True, slots=True)
class ModuleGraph:
    """Canonical module graph and its only realizable cycle observations."""

    modules: tuple[str, ...]
    relations: tuple[ModuleRelation, ...]
    cyclic_sccs: tuple[CyclicModuleScc, ...]


@dataclass(frozen=True, slots=True, order=True)
class MappingResolution:
    """Exact registry outcome for one module; no fallback label is possible."""

    module_id: str
    status: MappingStatus
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_identifier("mapping module", self.module_id)
        if self.status not in {"resolved", "unmapped", "overlap", "out_of_scope"}:
            raise ValueError("mapping status is invalid")
        if self.labels != tuple(sorted(set(self.labels))):
            raise ValueError("mapping labels must be unique and canonical")
        for item in self.labels:
            _required_identifier("mapping label", item)
        expected_size = {
            "resolved": 1,
            "unmapped": 0,
            "overlap": len(self.labels),
            "out_of_scope": 0,
        }[self.status]
        if self.status == "overlap" and len(self.labels) < 2:
            raise ValueError("overlap mapping requires at least two distinct labels")
        if self.status != "overlap" and len(self.labels) != expected_size:
            raise ValueError(f"{self.status} mapping has incompatible labels")


@dataclass(frozen=True, slots=True)
class ExactMapping:
    """One explicit resolution for every module in a graph."""

    resolutions: tuple[MappingResolution, ...]

    def __post_init__(self) -> None:
        modules = tuple(item.module_id for item in self.resolutions)
        if modules != tuple(sorted(set(modules))):
            raise ValueError("mapping resolutions must cover unique modules canonically")

    def for_module(self, module_id: str) -> MappingResolution:
        for item in self.resolutions:
            if item.module_id == module_id:
                return item
        raise KeyError(module_id)

    @property
    def resolved(self) -> tuple[MappingResolution, ...]:
        return tuple(item for item in self.resolutions if item.status == "resolved")

    @property
    def unmapped(self) -> tuple[MappingResolution, ...]:
        return tuple(item for item in self.resolutions if item.status == "unmapped")

    @property
    def overlapping(self) -> tuple[MappingResolution, ...]:
        return tuple(item for item in self.resolutions if item.status == "overlap")

    @property
    def out_of_scope(self) -> tuple[MappingResolution, ...]:
        return tuple(item for item in self.resolutions if item.status == "out_of_scope")

    @property
    def in_scope_is_complete(self) -> bool:
        return not self.unmapped and not self.overlapping


@dataclass(frozen=True, slots=True, order=True)
class ProjectedEdge:
    """All module relations inducing one directed label edge."""

    source_label: str
    target_label: str
    module_relations: tuple[ModuleRelation, ...]

    def __post_init__(self) -> None:
        _required_identifier("projected source label", self.source_label)
        _required_identifier("projected target label", self.target_label)
        if not self.module_relations:
            raise ValueError("projected edge requires module witnesses")
        if self.module_relations != tuple(sorted(set(self.module_relations))):
            raise ValueError("projected edge module witnesses must be canonical")

    @property
    def witness_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    witness_id
                    for relation in self.module_relations
                    for witness_id in relation.witness_ids
                }
            )
        )


@dataclass(frozen=True, slots=True, order=True)
class UnresolvedModuleRelation:
    relation: ModuleRelation
    source_status: MappingStatus
    target_status: MappingStatus


@dataclass(frozen=True, slots=True)
class RealizableSccProjection:
    """A module SCC whose every member resolved to exactly one label."""

    module_scc: CyclicModuleScc
    labels: tuple[str, ...]
    semantics: str = REALIZABLE_SCC_SEMANTICS

    def __post_init__(self) -> None:
        if not self.labels or self.labels != tuple(sorted(set(self.labels))):
            raise ValueError("realizable SCC labels must be non-empty and canonical")


@dataclass(frozen=True, slots=True)
class UnresolvedSccProjection:
    """A real module SCC for which a label-level conclusion is unavailable."""

    module_scc: CyclicModuleScc
    unmapped_modules: tuple[str, ...]
    overlapping_modules: tuple[str, ...]
    out_of_scope_modules: tuple[str, ...]

    def __post_init__(self) -> None:
        unresolved = (
            *self.unmapped_modules,
            *self.overlapping_modules,
            *self.out_of_scope_modules,
        )
        if not unresolved:
            raise ValueError("unresolved SCC projection requires an unresolved module")
        for values in (
            self.unmapped_modules,
            self.overlapping_modules,
            self.out_of_scope_modules,
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError("unresolved SCC module lists must be canonical")


@dataclass(frozen=True, slots=True)
class AggregateQuotientScc:
    """An SCC of collapsed labels, not proof of a composable module cycle."""

    labels: tuple[str, ...]
    shortest_cycle: tuple[str, ...]
    internal_edges: tuple[ProjectedEdge, ...]
    shortest_cycle_edges: tuple[ProjectedEdge, ...]
    realizable_module_components: tuple[tuple[str, ...], ...]
    semantics: str = AGGREGATE_QUOTIENT_SCC_SEMANTICS

    def __post_init__(self) -> None:
        if len(self.labels) < 2 or self.labels != tuple(sorted(set(self.labels))):
            raise ValueError("aggregate quotient SCC requires canonical cross-label members")
        if len(self.shortest_cycle) < 3 or self.shortest_cycle[0] != self.shortest_cycle[-1]:
            raise ValueError("aggregate quotient SCC shortest cycle must be closed")
        if len(self.shortest_cycle_edges) != len(self.shortest_cycle) - 1:
            raise ValueError("aggregate quotient shortest-cycle witnesses are incomplete")


@dataclass(frozen=True, slots=True)
class ArchitectureProjection:
    """Exact mapping projection over a canonical module graph."""

    graph: ModuleGraph
    mapping: ExactMapping
    projected_edges: tuple[ProjectedEdge, ...]
    unresolved_relations: tuple[UnresolvedModuleRelation, ...]
    realizable_sccs: tuple[RealizableSccProjection, ...]
    unresolved_sccs: tuple[UnresolvedSccProjection, ...]
    aggregate_quotient_sccs: tuple[AggregateQuotientScc, ...]

    @property
    def cross_label_edges(self) -> tuple[ProjectedEdge, ...]:
        return tuple(
            item for item in self.projected_edges if item.source_label != item.target_label
        )


@dataclass(frozen=True, slots=True, order=True)
class FamilyDependency:
    """One immediate source-family to dependency-family edge in an allowed DAG."""

    source_family: str
    target_family: str

    def __post_init__(self) -> None:
        _required_identifier("dependency source family", self.source_family)
        _required_identifier("dependency target family", self.target_family)


@dataclass(frozen=True, slots=True)
class FamilyDag:
    """A validated, explicit DAG; import permission uses its reachability."""

    families: tuple[str, ...]
    direct_dependencies: tuple[FamilyDependency, ...]
    compat_families: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        raw_families = self.families
        raw_dependencies = self.direct_dependencies
        raw_compat = self.compat_families
        families = tuple(sorted(set(raw_families)))
        dependencies = tuple(sorted(set(raw_dependencies)))
        compat = tuple(sorted(set(raw_compat)))
        if not families or len(families) != len(raw_families):
            raise ValueError("family DAG identities must be non-empty and unique")
        if len(dependencies) != len(raw_dependencies):
            raise ValueError("family DAG dependencies cannot repeat")
        if len(compat) != len(raw_compat):
            raise ValueError("compat family identities cannot repeat")
        for family in families:
            _required_identifier("family DAG identity", family)
        family_set = set(families)
        if not set(compat) <= family_set:
            raise ValueError("compat family is absent from the family DAG")
        for item in dependencies:
            if item.source_family not in family_set or item.target_family not in family_set:
                raise ValueError("family DAG dependency references an unknown family")
            if item.source_family == item.target_family:
                raise ValueError("family DAG cannot contain self dependencies")
        object.__setattr__(self, "families", families)
        object.__setattr__(self, "direct_dependencies", dependencies)
        object.__setattr__(self, "compat_families", compat)

        pairs = tuple((item.source_family, item.target_family) for item in dependencies)
        if _cyclic_components(families, pairs):
            raise ValueError("family dependency policy must be acyclic")
        for source in families:
            if source in compat:
                continue
            if set(self.reachable_dependencies(source)) & set(compat):
                raise ValueError("family DAG allows a canonical-to-compat dependency")

    def reachable_dependencies(self, source_family: str) -> tuple[str, ...]:
        if source_family not in self.families:
            raise KeyError(source_family)
        adjacency = _adjacency(
            self.families,
            (
                (item.source_family, item.target_family)
                for item in self.direct_dependencies
            ),
        )
        reached: set[str] = set()
        queue = deque(adjacency[source_family])
        while queue:
            target = queue.popleft()
            if target in reached:
                continue
            reached.add(target)
            queue.extend(adjacency[target])
        return tuple(sorted(reached))

    def allows(self, source_family: str, target_family: str) -> bool:
        if source_family not in self.families or target_family not in self.families:
            raise KeyError(source_family if source_family not in self.families else target_family)
        return source_family == target_family or target_family in self.reachable_dependencies(
            source_family
        )


@dataclass(frozen=True, slots=True)
class FamilyEdgeDecision:
    edge: ProjectedEdge
    allowed: bool
    reason: FamilyDecisionReason


@dataclass(frozen=True, slots=True)
class FamilyDagEvaluation:
    """Mapping coverage and per-edge decisions against one validated family DAG."""

    projection: ArchitectureProjection
    dag: FamilyDag
    decisions: tuple[FamilyEdgeDecision, ...]

    @property
    def forbidden_edges(self) -> tuple[FamilyEdgeDecision, ...]:
        return tuple(item for item in self.decisions if not item.allowed)

    @property
    def canonical_to_compat_edges(self) -> tuple[FamilyEdgeDecision, ...]:
        return tuple(item for item in self.decisions if item.reason == "canonical_to_compat")

    @property
    def dag_forbidden_edges(self) -> tuple[FamilyEdgeDecision, ...]:
        return tuple(item for item in self.decisions if item.reason == "forbidden_dependency")

    @property
    def is_accepted(self) -> bool:
        """Whether mapping coverage and the family-edge policy pass.

        Realizable module SCCs remain a separate blocking dimension exposed by
        ``projection.realizable_sccs``; this property intentionally does not
        relabel aggregate quotient SCCs as failures.
        """

        return self.projection.mapping.in_scope_is_complete and not self.forbidden_edges


def _canonical_relations(edges: Iterable[ModuleEdge]) -> tuple[ModuleRelation, ...]:
    witnesses: dict[tuple[str, str], set[str]] = {}
    for edge in edges:
        witnesses.setdefault((edge.source_module, edge.target_module), set()).add(
            edge.witness_id
        )
    return tuple(
        ModuleRelation(source, target, tuple(sorted(values)))
        for (source, target), values in sorted(witnesses.items())
    )


def _adjacency(
    nodes: Iterable[str], pairs: Iterable[tuple[str, str]]
) -> dict[str, tuple[str, ...]]:
    result: dict[str, set[str]] = {node: set() for node in nodes}
    for source, target in pairs:
        if source not in result or target not in result:
            raise ValueError("graph edge references a node outside its declared domain")
        result[source].add(target)
    return {node: tuple(sorted(targets)) for node, targets in sorted(result.items())}


def _cyclic_components(
    nodes: Sequence[str], pairs: Iterable[tuple[str, str]]
) -> tuple[tuple[str, ...], ...]:
    canonical_nodes = tuple(sorted(set(nodes)))
    canonical_pairs = tuple(sorted(set(pairs)))
    adjacency = _adjacency(canonical_nodes, canonical_pairs)
    reverse: dict[str, list[str]] = {node: [] for node in canonical_nodes}
    for source, targets in adjacency.items():
        for target in targets:
            reverse[target].append(source)
    for sources in reverse.values():
        sources.sort()

    visited: set[str] = set()
    finish_order: list[str] = []
    for start in canonical_nodes:
        if start in visited:
            continue
        traversal_stack: list[tuple[str, bool]] = [(start, False)]
        while traversal_stack:
            node, expanded = traversal_stack.pop()
            if expanded:
                finish_order.append(node)
                continue
            if node in visited:
                continue
            visited.add(node)
            traversal_stack.append((node, True))
            for target in reversed(adjacency[node]):
                if target not in visited:
                    traversal_stack.append((target, False))

    assigned: set[str] = set()
    components: list[tuple[str, ...]] = []
    self_edges = {source for source, target in canonical_pairs if source == target}
    for start in reversed(finish_order):
        if start in assigned:
            continue
        assigned.add(start)
        members: list[str] = []
        reverse_stack = [start]
        while reverse_stack:
            node = reverse_stack.pop()
            members.append(node)
            for source in reversed(reverse[node]):
                if source not in assigned:
                    assigned.add(source)
                    reverse_stack.append(source)
        component = tuple(sorted(members))
        if not component:
            raise RuntimeError("SCC traversal produced an empty component")
        if len(component) > 1 or any(member in self_edges for member in component):
            components.append(component)
    return tuple(sorted(components))


def _shortest_path(
    adjacency: Mapping[str, Sequence[str]], start: str, target: str
) -> tuple[str, ...] | None:
    parents: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if node == target:
            chain: list[str] = []
            current: str | None = node
            while current is not None:
                chain.append(current)
                current = parents[current]
            return tuple(reversed(chain))
        for adjacent in adjacency.get(node, ()):
            if adjacent not in parents:
                parents[adjacent] = node
                queue.append(adjacent)
    return None


def _shortest_cycle(
    component: Sequence[str], pairs: Iterable[tuple[str, str]]
) -> tuple[str, ...]:
    selected = tuple(sorted(set(component)))
    selected_set = set(selected)
    adjacency = _adjacency(
        selected,
        (
            (source, target)
            for source, target in pairs
            if source in selected_set and target in selected_set
        ),
    )
    candidates: list[tuple[str, ...]] = []
    for start in selected:
        if start in adjacency[start]:
            candidates.append((start, start))
        for target in adjacency[start]:
            if target == start:
                continue
            path = _shortest_path(adjacency, target, start)
            if path is not None:
                candidates.append((start, *path))
    if not candidates:
        raise ValueError("cyclic component does not contain a closed path")
    return min(candidates, key=lambda item: (len(item), item))


def analyze_module_graph(
    modules: Iterable[str], edges: Iterable[ModuleEdge]
) -> ModuleGraph:
    """Canonicalize a complete module graph and calculate its real cyclic SCCs."""

    canonical_modules = tuple(sorted(set(modules)))
    for module in canonical_modules:
        _required_identifier("module graph node", module)
    relations = _canonical_relations(edges)
    module_set = set(canonical_modules)
    if any(
        item.source_module not in module_set or item.target_module not in module_set
        for item in relations
    ):
        raise ValueError("module relation escapes the declared graph domain")
    pairs = tuple((item.source_module, item.target_module) for item in relations)
    relation_index = {(item.source_module, item.target_module): item for item in relations}
    sccs: list[CyclicModuleScc] = []
    for component in _cyclic_components(canonical_modules, pairs):
        selected = set(component)
        internal = tuple(
            item
            for item in relations
            if item.source_module in selected and item.target_module in selected
        )
        shortest = _shortest_cycle(component, pairs)
        shortest_relations = tuple(
            relation_index[(source, target)]
            for source, target in pairwise(shortest)
        )
        sccs.append(CyclicModuleScc(component, shortest, internal, shortest_relations))
    return ModuleGraph(canonical_modules, relations, tuple(sccs))


def resolve_exact_mapping(
    modules: Iterable[str],
    declared_matches: Mapping[str, Sequence[str]],
    *,
    in_scope_modules: Iterable[str] | None = None,
) -> ExactMapping:
    """Resolve explicit matches without name inference or a default label."""

    canonical_modules = tuple(sorted(set(modules)))
    scope = (
        set(canonical_modules)
        if in_scope_modules is None
        else set(in_scope_modules)
    )
    if not scope <= set(canonical_modules):
        raise ValueError("mapping scope contains a module outside the graph")
    resolutions: list[MappingResolution] = []
    for module in canonical_modules:
        if module not in scope:
            resolutions.append(MappingResolution(module, "out_of_scope"))
            continue
        raw_labels = declared_matches.get(module, ())
        if isinstance(raw_labels, str):
            raise TypeError("mapping matches must be a sequence of labels, not one string")
        labels = tuple(sorted(set(raw_labels)))
        if not labels:
            status: MappingStatus = "unmapped"
        elif len(labels) == 1:
            status = "resolved"
        else:
            status = "overlap"
        resolutions.append(MappingResolution(module, status, labels))
    return ExactMapping(tuple(resolutions))


def _projected_edges(
    graph: ModuleGraph, mapping: ExactMapping
) -> tuple[tuple[ProjectedEdge, ...], tuple[UnresolvedModuleRelation, ...]]:
    resolution = {item.module_id: item for item in mapping.resolutions}
    grouped: dict[tuple[str, str], list[ModuleRelation]] = {}
    unresolved: list[UnresolvedModuleRelation] = []
    for relation in graph.relations:
        source = resolution[relation.source_module]
        target = resolution[relation.target_module]
        if source.status == target.status == "resolved":
            grouped.setdefault((source.labels[0], target.labels[0]), []).append(relation)
        else:
            unresolved.append(
                UnresolvedModuleRelation(relation, source.status, target.status)
            )
    projected = tuple(
        ProjectedEdge(source, target, tuple(sorted(relations)))
        for (source, target), relations in sorted(grouped.items())
    )
    return projected, tuple(sorted(unresolved))


def project_module_graph(graph: ModuleGraph, mapping: ExactMapping) -> ArchitectureProjection:
    """Project a module graph while preserving realizable and aggregate semantics."""

    if tuple(item.module_id for item in mapping.resolutions) != graph.modules:
        raise ValueError("mapping must resolve every module in the projected graph")
    projected_edges, unresolved_relations = _projected_edges(graph, mapping)
    resolution = {item.module_id: item for item in mapping.resolutions}

    realizable: list[RealizableSccProjection] = []
    unresolved_sccs: list[UnresolvedSccProjection] = []
    for component in graph.cyclic_sccs:
        items = tuple(resolution[module] for module in component.modules)
        if all(item.status == "resolved" for item in items):
            labels = tuple(sorted({item.labels[0] for item in items}))
            realizable.append(RealizableSccProjection(component, labels))
            continue
        unresolved_sccs.append(
            UnresolvedSccProjection(
                component,
                tuple(sorted(item.module_id for item in items if item.status == "unmapped")),
                tuple(sorted(item.module_id for item in items if item.status == "overlap")),
                tuple(
                    sorted(item.module_id for item in items if item.status == "out_of_scope")
                ),
            )
        )

    cross_edges = tuple(
        item for item in projected_edges if item.source_label != item.target_label
    )
    label_nodes = tuple(sorted({item.labels[0] for item in mapping.resolved}))
    label_pairs = tuple((item.source_label, item.target_label) for item in cross_edges)
    edge_index = {(item.source_label, item.target_label): item for item in cross_edges}
    aggregate: list[AggregateQuotientScc] = []
    for label_component in _cyclic_components(label_nodes, label_pairs):
        selected = set(label_component)
        internal = tuple(
            item
            for item in cross_edges
            if item.source_label in selected and item.target_label in selected
        )
        shortest = _shortest_cycle(label_component, label_pairs)
        shortest_edges = tuple(
            edge_index[(source, target)]
            for source, target in pairwise(shortest)
        )
        linked_components = tuple(
            item.module_scc.modules
            for item in realizable
            if len(item.labels) > 1 and set(item.labels) <= selected
        )
        aggregate.append(
            AggregateQuotientScc(
                label_component,
                shortest,
                internal,
                shortest_edges,
                tuple(sorted(linked_components)),
            )
        )
    return ArchitectureProjection(
        graph,
        mapping,
        projected_edges,
        unresolved_relations,
        tuple(realizable),
        tuple(unresolved_sccs),
        tuple(aggregate),
    )


def evaluate_family_dag(
    projection: ArchitectureProjection, dag: FamilyDag
) -> FamilyDagEvaluation:
    """Evaluate every exactly projected edge against an explicit family DAG."""

    resolved_labels = {item.labels[0] for item in projection.mapping.resolved}
    unknown = resolved_labels - set(dag.families)
    if unknown:
        raise ValueError(f"mapping resolved families absent from DAG: {sorted(unknown)!r}")
    compat = set(dag.compat_families)
    decisions: list[FamilyEdgeDecision] = []
    for edge in projection.projected_edges:
        if edge.source_label == edge.target_label:
            decisions.append(FamilyEdgeDecision(edge, True, "allowed_same_family"))
        elif edge.source_label not in compat and edge.target_label in compat:
            decisions.append(FamilyEdgeDecision(edge, False, "canonical_to_compat"))
        elif dag.allows(edge.source_label, edge.target_label):
            decisions.append(FamilyEdgeDecision(edge, True, "allowed_by_dag"))
        else:
            decisions.append(FamilyEdgeDecision(edge, False, "forbidden_dependency"))
    return FamilyDagEvaluation(projection, dag, tuple(decisions))


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        try:
            _defined_value.__module__ = (
                "_04_Nucleo_Operativo.platform.shared.architecture_projection"
            )
        except (AttributeError, TypeError):
            pass
del _defined_value


__all__ = [
    "AGGREGATE_QUOTIENT_SCC_SEMANTICS",
    "ARCHITECTURE_PROJECTION_SCHEMA",
    "REALIZABLE_SCC_SEMANTICS",
    "AggregateQuotientScc",
    "ArchitectureProjection",
    "CyclicModuleScc",
    "ExactMapping",
    "FamilyDag",
    "FamilyDagEvaluation",
    "FamilyDependency",
    "FamilyEdgeDecision",
    "MappingResolution",
    "ModuleEdge",
    "ModuleGraph",
    "ModuleRelation",
    "ProjectedEdge",
    "RealizableSccProjection",
    "UnresolvedModuleRelation",
    "UnresolvedSccProjection",
    "analyze_module_graph",
    "evaluate_family_dag",
    "project_module_graph",
    "resolve_exact_mapping",
]
