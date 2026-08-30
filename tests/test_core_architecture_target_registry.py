"""Executable contracts for the canonical Core target architecture."""

from __future__ import annotations

from collections import Counter
from itertools import pairwise
from pathlib import Path

import grimp

from neocortex.code.contracts.target_registry import (
    CORE_FAMILY_DAG_SCHEMA,
    CORE_RESPONSIBILITY_REGISTRY_SCHEMA,
    FORBIDDEN_FAMILY_EDGE_BASELINE,
    RESPONSIBILITY_MODULES,
    TARGET_FAMILIES,
    TARGET_FAMILY_DEPENDENCIES,
    TARGET_FAMILY_LAYERS,
    TARGET_RESPONSIBILITY_IDS,
    core_architecture_target_fingerprint,
    core_architecture_target_payload,
    forbidden_family_edge_baseline,
    matching_target_families,
    matching_target_responsibilities,
    registered_core_modules,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = REPOSITORY_ROOT / "neocortex"


def _module_id(path: Path) -> str:
    return ".".join(path.relative_to(REPOSITORY_ROOT).with_suffix("").parts).removesuffix(".__init__")


def test_registry_exhaustively_assigns_every_current_core_module() -> None:
    observed = tuple(sorted(_module_id(path) for path in CORE_ROOT.rglob("*.py")))
    registered = registered_core_modules()
    product_registered = tuple(
        sorted(module for modules in RESPONSIBILITY_MODULES.values() for module in modules)
    )
    assert observed == registered == product_registered
    assert len(observed) == 454
    assert sum(len(items) for items in RESPONSIBILITY_MODULES.values()) == 454
    for module in observed:
        families = matching_target_families(module)
        responsibilities = matching_target_responsibilities(module)
        assert len(families) == 1
        assert len(responsibilities) == 1
        assert responsibilities[0].partition(".")[0] == families[0]


def test_target_vocabulary_is_canonical_and_legacy_free() -> None:
    payload = core_architecture_target_payload()
    assert len(TARGET_RESPONSIBILITY_IDS) == 45
    assert len(TARGET_FAMILIES) == 12
    assert TARGET_FAMILY_DEPENDENCIES == tuple(pairwise(TARGET_FAMILY_LAYERS))
    assert len(FORBIDDEN_FAMILY_EDGE_BASELINE) == 28
    assert sum(item.direct_module_edges for item in FORBIDDEN_FAMILY_EDGE_BASELINE) == 228
    assert payload["responsibility_registry"]["schema"] == CORE_RESPONSIBILITY_REGISTRY_SCHEMA
    assert payload["family_dag"]["schema"] == CORE_FAMILY_DAG_SCHEMA
    assert "compatibility_matrix" not in payload
    assert "compatibility_modules" not in payload["responsibility_registry"]
    assert core_architecture_target_fingerprint() == (
        "core-architecture-target-v1:sha256:"
        "1eaf2f2358fd67c918c7557cf48665d01649b36be95cdd036675947900f3744b"
    )


def test_forbidden_family_edge_baseline_matches_the_current_import_graph() -> None:
    graph = grimp.build_graph(
        "neocortex",
        include_external_packages=False,
        exclude_type_checking_imports=False,
        cache_dir=None,
    )
    rank = {family: index for index, family in enumerate(TARGET_FAMILY_LAYERS)}
    forbidden: Counter[tuple[str, str]] = Counter()
    for importer in sorted(graph.modules):
        source_matches = matching_target_families(importer)
        if len(source_matches) != 1:
            continue
        source = source_matches[0]
        for imported in graph.find_modules_directly_imported_by(importer):
            target_matches = matching_target_families(imported)
            if len(target_matches) != 1:
                continue
            target = target_matches[0]
            if source != target and rank[source] >= rank[target]:
                forbidden[source, target] += 1
    assert dict(forbidden) == forbidden_family_edge_baseline()
